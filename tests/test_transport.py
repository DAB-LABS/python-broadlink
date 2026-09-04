"""Transport layer: framing, encryption, checksums, discovery and auth.

These tests replace the UDP socket with a fake so the exact bytes that leave
``send_packet`` and ``scan`` can be checked, and so response validation can
be exercised with corrupted frames.
"""

from __future__ import annotations

import socket

import pytest

import broadlink
from broadlink import device as device_module
from broadlink import exceptions as e
from broadlink.device import Device
from tests.oracle.harness import HOST, MAC, make_response

INIT_KEY = bytes.fromhex("097628343fe99e23765c1513accf8b02")
INIT_VECT = bytes.fromhex("562e17996d093d28ddb3ba695a2e6f58")


class FakeSocket:
    """A UDP socket stand-in: records sendto, replays canned recvfrom."""

    instances: list["FakeSocket"] = []

    def __init__(self, *args, **kwargs):
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.inbox: list[tuple[bytes, tuple[str, int]]] = list(FakeSocket.queue)
        self.timeout = None
        self.closed = False
        self.bound = None
        FakeSocket.instances.append(self)

    queue: list[tuple[bytes, tuple[str, int]]] = []

    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        self.timeout = value

    def bind(self, addr):
        self.bound = addr

    def getsockname(self):
        return self.bound or ("0.0.0.0", 0)

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))

    def recvfrom(self, size):
        if not self.inbox:
            raise socket.timeout()
        return self.inbox.pop(0)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def fake_socket(monkeypatch):
    FakeSocket.instances = []
    FakeSocket.queue = []
    monkeypatch.setattr(device_module.socket, "socket", FakeSocket)
    monkeypatch.setattr(broadlink.socket, "socket", FakeSocket)
    # Keep the retry loop from waiting on real time.
    monkeypatch.setattr(device_module, "DEFAULT_RETRY_INTVL", 0.001)
    return FakeSocket


def fixed_device(cls=Device, devtype=0x2737) -> Device:
    dev = cls(HOST, MAC, devtype, name="Bench")
    dev.count = 0x8000
    return dev


# ------------------------------------------------------------------ send_packet


def test_send_packet_wire_bytes(fake_socket):
    dev = fixed_device()
    dev.id = 0x00000001
    payload = bytes([0x01]) + bytes(15)
    fake_socket.queue = [(make_response(dev, bytes(16)), HOST)]

    resp = dev.send_packet(0x6A, payload)

    sock = fake_socket.instances[-1]
    assert len(sock.sent) == 1
    frame, addr = sock.sent[0]
    assert addr == HOST
    assert frame[0x00:0x08] == bytes.fromhex("5aa5aa555aa5aa55")
    assert frame[0x24:0x26] == (0x2737).to_bytes(2, "little")
    assert frame[0x26:0x28] == (0x6A).to_bytes(2, "little")
    assert frame[0x28:0x2A] == (0x8001).to_bytes(2, "little")  # count advanced
    assert frame[0x2A:0x30] == MAC[::-1]
    assert frame[0x30:0x34] == (1).to_bytes(4, "little")
    assert frame[0x34:0x36] == (sum(payload, 0xBEAF) & 0xFFFF).to_bytes(2, "little")
    # Encrypted payload: one AES block, decrypts back to the plaintext.
    assert len(frame) == 0x38 + 16
    assert dev.decrypt(frame[0x38:]) == payload
    # Frame checksum is computed over the frame with the checksum field zeroed.
    body = bytearray(frame)
    body[0x20:0x22] = b"\x00\x00"
    assert frame[0x20:0x22] == (sum(body, 0xBEAF) & 0xFFFF).to_bytes(2, "little")
    assert resp[0x22:0x24] == b"\x00\x00"
    assert dev.count == 0x8001


def test_send_packet_pads_payload_to_block(fake_socket):
    dev = fixed_device()
    fake_socket.queue = [(make_response(dev, b""), HOST)]
    dev.send_packet(0x6A, bytes(20))
    frame = fake_socket.instances[-1].sent[0][0]
    assert len(frame) == 0x38 + 32
    assert dev.decrypt(frame[0x38:]) == bytes(32)


def test_send_packet_counter_wraps_with_high_bit(fake_socket):
    dev = fixed_device()
    dev.count = 0xFFFF
    fake_socket.queue = [(make_response(dev, b""), HOST)]
    dev.send_packet(0x6A, b"")
    assert dev.count == 0x8000


def test_send_packet_retries_then_times_out(fake_socket, monkeypatch):
    dev = fixed_device()
    dev.timeout = 0.01
    fake_socket.queue = []  # never answers
    with pytest.raises(e.NetworkTimeoutError) as err:
        dev.send_packet(0x6A, b"")
    assert err.value.errno == -4000
    assert len(fake_socket.instances[-1].sent) >= 1


def test_send_packet_rejects_short_response(fake_socket):
    dev = fixed_device()
    fake_socket.queue = [(bytes(0x10), HOST)]
    with pytest.raises(e.DataValidationError) as err:
        dev.send_packet(0x6A, b"")
    assert err.value.errno == -4007


def test_send_packet_rejects_bad_checksum(fake_socket):
    dev = fixed_device()
    frame = bytearray(make_response(dev, b""))
    frame[0x20] ^= 0xFF
    fake_socket.queue = [(bytes(frame), HOST)]
    with pytest.raises(e.DataValidationError) as err:
        dev.send_packet(0x6A, b"")
    assert err.value.errno == -4008


# ------------------------------------------------------------------------- auth


def test_auth_uses_initial_key_and_installs_session_key(fake_socket):
    dev = fixed_device()
    dev.id = 99  # stale session; auth must reset it before sending
    dev.update_aes(bytes(range(16)))  # stale key

    session_id = 0x0000BEEF
    session_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    # The auth response payload: id at 0:4, key at 4:20, encrypted with the
    # INITIAL key, which is what the device expects auth to be decrypted with.
    fresh = fixed_device()
    reply = make_response(fresh, session_id.to_bytes(4, "little") + session_key)
    fake_socket.queue = [(reply, HOST)]

    assert dev.auth() is True

    frame = fake_socket.instances[-1].sent[0][0]
    assert frame[0x26:0x28] == (0x65).to_bytes(2, "little")
    assert frame[0x30:0x34] == bytes(4)  # id reset to 0 for the handshake
    plaintext = fresh.decrypt(frame[0x38:])
    assert plaintext[0x04:0x14] == bytes([0x31]) * 16
    assert plaintext[0x1E] == 0x01
    assert plaintext[0x2D] == 0x01
    assert plaintext[0x30:0x36] == b"Test 1"
    assert len(plaintext) == 0x50

    assert dev.id == session_id
    # The new key is in use: encrypting with it matches an independent cipher.
    probe = fixed_device()
    probe.update_aes(session_key)
    assert dev.encrypt(bytes(16)) == probe.encrypt(bytes(16))


def test_auth_surfaces_device_error(fake_socket):
    dev = fixed_device()
    fake_socket.queue = [(make_response(dev, bytes(20), error=0xFFF9), HOST)]
    with pytest.raises(e.AuthorizationError):
        dev.auth()


# ------------------------------------------------------------------- discovery


def hello_response(devtype: int, mac: bytes, name: str, locked: bool) -> bytes:
    frame = bytearray(0x80)
    frame[0x34:0x36] = devtype.to_bytes(2, "little")
    frame[0x3A:0x40] = mac[::-1]
    frame[0x40 : 0x40 + len(name)] = name.encode()
    frame[0x7F] = int(locked)
    return bytes(frame)


def test_scan_builds_hello_packet_and_parses_replies(fake_socket):
    fake_socket.queue = [
        (hello_response(0x6026, MAC, "Bedroom RM", False), ("192.0.2.10", 80)),
        (hello_response(0x6026, MAC, "Bedroom RM", False), ("192.0.2.10", 80)),  # dup
        (hello_response(0x2711, bytes.fromhex("34ea34000001"), "Plug", True),
         ("192.0.2.11", 80)),
    ]
    found = list(device_module.scan(timeout=0.01, local_ip_address="192.0.2.2"))

    assert found == [
        (0x6026, ("192.0.2.10", 80), MAC, "Bedroom RM", False),
        (0x2711, ("192.0.2.11", 80), bytes.fromhex("34ea34000001"), "Plug", True),
    ]
    sock = fake_socket.instances[-1]
    assert sock.bound == ("192.0.2.2", 0)
    packet, addr = sock.sent[0]
    assert addr == ("255.255.255.255", 80)
    assert len(packet) == 0x30
    assert packet[0x26] == 6
    assert packet[0x18:0x1C] == socket.inet_aton("192.0.2.2")[::-1]
    body = bytearray(packet)
    body[0x20:0x22] = b"\x00\x00"
    assert packet[0x20:0x22] == (sum(body, 0xBEAF) & 0xFFFF).to_bytes(2, "little")
    assert sock.closed


def test_discover_and_hello_build_devices(fake_socket):
    fake_socket.queue = [
        (hello_response(0x6026, MAC, "Bedroom RM", False), ("192.0.2.10", 80)),
    ]
    devices = broadlink.discover(timeout=0.01)
    assert len(devices) == 1
    dev = devices[0]
    assert isinstance(dev, broadlink.rm4pro)
    assert dev.host == ("192.0.2.10", 80)
    assert dev.mac == MAC
    assert dev.name == "Bedroom RM"
    assert dev.model == "RM4 pro"
    assert dev.manufacturer == "Broadlink"

    fake_socket.queue = [
        (hello_response(0x6026, MAC, "Bedroom RM", True), ("192.0.2.10", 80)),
    ]
    dev = broadlink.hello("192.0.2.10", timeout=0.01)
    assert dev.is_locked is True
    assert fake_socket.instances[-1].sent[0][1] == ("192.0.2.10", 80)


def test_hello_times_out(fake_socket):
    fake_socket.queue = []
    with pytest.raises(e.NetworkTimeoutError):
        broadlink.hello("192.0.2.10", timeout=0.01)


def test_device_hello_validates_identity(fake_socket):
    dev = fixed_device(broadlink.rm4pro, 0x6026)
    fake_socket.queue = [(hello_response(0x6026, MAC, "Renamed", True), HOST)]
    assert dev.hello() is True
    assert dev.name == "Renamed"
    assert dev.is_locked is True

    fake_socket.queue = [
        (hello_response(0x6026, bytes.fromhex("000000000001"), "Other", False), HOST)
    ]
    with pytest.raises(e.DataValidationError):
        dev.hello()

    fake_socket.queue = [(hello_response(0x2711, MAC, "Other", False), HOST)]
    with pytest.raises(e.DataValidationError):
        dev.hello()


def test_ping_packet(fake_socket):
    dev = fixed_device()
    dev.ping()
    packet, addr = fake_socket.instances[-1].sent[0]
    assert addr == HOST
    assert len(packet) == 0x30
    assert packet[0x26] == 1


# ------------------------------------------------------------------ gendevice


@pytest.mark.parametrize(
    ("devtype", "cls", "model"),
    [
        (0x2737, broadlink.rmmini, "RM mini 3"),
        (0x272A, broadlink.rmpro, "RM pro"),
        (0x5F36, broadlink.rmminib, "RM mini 3"),
        (0x51DA, broadlink.rm4mini, "RM4 mini"),
        (0x6026, broadlink.rm4pro, "RM4 pro"),
        (0x2711, broadlink.sp2s, "SP2"),
        (0x2720, broadlink.sp2, "SP mini"),
        (0x2714, broadlink.a1, "A1"),
        (0x4EAD, broadlink.hysen, "HY02/HY03"),
        (0x60C7, broadlink.lb1, "LB1"),
        (0x4EB5, broadlink.mp1, "MP1-1K4S"),
    ],
)
def test_gendevice_known_ids(devtype, cls, model):
    dev = broadlink.gendevice(devtype, HOST, MAC)
    assert type(dev) is cls
    assert dev.model == model
    assert dev.type == cls.TYPE


def test_gendevice_unknown_id_is_generic_device():
    dev = broadlink.gendevice(0xFFFF, HOST, "a043b05510f7")
    assert type(dev) is Device
    assert dev.type == "Unknown"
    assert dev.mac == MAC


def test_product_table_has_no_duplicate_ids():
    seen = {}
    for cls, products in broadlink.SUPPORTED_TYPES.items():
        for pid in products:
            assert pid not in seen, f"{pid:#06x} in both {seen[pid]} and {cls}"
            seen[pid] = cls.__name__


# ----------------------------------------------------------------------- setup


def test_setup_packet(fake_socket):
    broadlink.setup("MyWifi", "hunter2", 3, ip_address="192.0.2.255")
    packet, addr = fake_socket.instances[-1].sent[0]
    assert addr == ("192.0.2.255", 80)
    assert len(packet) == 0x88
    assert packet[0x26] == 0x14
    assert packet[68:74] == b"MyWifi"
    assert packet[100:107] == b"hunter2"
    assert packet[0x84] == 6
    assert packet[0x85] == 7
    assert packet[0x86] == 3
    body = bytearray(packet)
    body[0x20:0x22] = b"\x00\x00"
    assert packet[0x20:0x22] == (sum(body, 0xBEAF) & 0xFFFF).to_bytes(2, "little")


# ------------------------------------------------------------------ exceptions


@pytest.mark.parametrize(
    ("code", "exc"),
    [
        (0xFFFF, e.AuthenticationError),
        (0xFFF9, e.AuthorizationError),
        (0xFFFB, e.StorageError),
        (0xFFFE, e.ConnectionClosedError),
    ],
)
def test_check_error_maps_codes(code, exc):
    with pytest.raises(exc):
        e.check_error(code.to_bytes(2, "little"))


def test_check_error_passes_zero():
    e.check_error(b"\x00\x00")


def test_check_error_unknown_code():
    with pytest.raises(e.UnknownError) as err:
        e.check_error((0x1234).to_bytes(2, "little"))
    assert err.value.errno == 0x1234
