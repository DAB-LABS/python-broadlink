"""Transport layer: framing, encryption, checksums, discovery and auth.

These tests replace the UDP socket with a fake so the exact bytes that leave
``send_packet`` and ``scan`` can be checked, and so response validation can
be exercised with corrupted frames.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

import broadlink
from broadlink import device as device_module
from broadlink import exceptions as e
from broadlink.device import Device
from tests.oracle.harness import HOST, MAC, make_response

INIT_KEY = bytes.fromhex("097628343fe99e23765c1513accf8b02")
INIT_VECT = bytes.fromhex("562e17996d093d28ddb3ba695a2e6f58")


class FakeTransport:
    """Datagram transport stand-in: records sendto, feeds canned replies."""

    def __init__(self, protocol, local_addr, remote_addr, broadcast, replies):
        self.protocol = protocol
        self.local_addr = local_addr or ("0.0.0.0", 0)
        self.remote_addr = remote_addr
        self.broadcast = broadcast
        self.replies = list(replies)
        self.sent: list[tuple[bytes, tuple[str, int] | None]] = []
        self.closed = False

    def sendto(self, data, addr=None):
        self.sent.append((bytes(data), addr or self.remote_addr))
        # Each send releases the next canned reply, if any, exactly like a
        # device answering one request.
        if self.replies:
            self.protocol.queue.put_nowait(self.replies.pop(0))

    def get_extra_info(self, name):
        if name == "sockname":
            return (self.local_addr[0], self.local_addr[1] or 40000)
        return None

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeNet:
    """Replacement for broadlink.device._open_endpoint."""

    def __init__(self):
        self.replies: list[tuple[bytes, tuple[str, int]]] = []
        self.endpoints: list[FakeTransport] = []

    async def __call__(self, local_addr=None, remote_addr=None, broadcast=False):
        protocol = device_module._Protocol()
        transport = FakeTransport(protocol, local_addr, remote_addr, broadcast, self.replies)
        self.replies = []
        protocol.connection_made(transport)
        self.endpoints.append(transport)
        return transport, protocol


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()
    monkeypatch.setattr(device_module, "_open_endpoint", fake)
    monkeypatch.setattr(broadlink, "_open_endpoint", fake)
    # Keep the retry loop from waiting on real time.
    monkeypatch.setattr(device_module, "DEFAULT_RETRY_INTVL", 0.005)
    return fake


def run(coro):
    return asyncio.run(coro)


def fixed_device(cls=Device, devtype=0x2737) -> Device:
    dev = cls(HOST, MAC, devtype, name="Bench")
    dev.count = 0x8000
    return dev


# ------------------------------------------------------------------ send_packet


def test_send_packet_wire_bytes(net):
    dev = fixed_device()
    dev.id = 0x00000001
    payload = bytes([0x01]) + bytes(15)
    net.replies = [(make_response(dev, bytes(16)), HOST)]

    resp = run(dev.send_packet(0x6A, payload))

    ep = net.endpoints[-1]
    assert ep.remote_addr == HOST
    assert len(ep.sent) == 1
    frame, addr = ep.sent[0]
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


def test_send_packet_pads_payload_to_block(net):
    dev = fixed_device()
    net.replies = [(make_response(dev, b""), HOST)]
    run(dev.send_packet(0x6A, bytes(20)))
    frame = net.endpoints[-1].sent[0][0]
    assert len(frame) == 0x38 + 32
    assert dev.decrypt(frame[0x38:]) == bytes(32)


def test_send_packet_counter_wraps_with_high_bit(net):
    dev = fixed_device()
    dev.count = 0xFFFF
    net.replies = [(make_response(dev, b""), HOST)]
    run(dev.send_packet(0x6A, b""))
    assert dev.count == 0x8000


def test_send_packet_reuses_one_endpoint_and_serializes(net):
    dev = fixed_device()

    async def go():
        net.replies = [(make_response(dev, b""), HOST)]
        await dev.send_packet(0x6A, b"")
        ep = net.endpoints[-1]
        ep.replies = [(make_response(dev, b""), HOST), (make_response(dev, b""), HOST)]
        await asyncio.gather(dev.send_packet(0x6A, b"a"), dev.send_packet(0x6A, b"b"))
        return ep

    ep = run(go())
    assert len(net.endpoints) == 1
    assert len(ep.sent) == 3
    # Counters are consecutive: the lock kept the two concurrent calls apart.
    counts = [int.from_bytes(f[0x28:0x2A], "little") for f, _ in ep.sent]
    assert counts == [0x8001, 0x8002, 0x8003]


def test_aclose_then_reopen(net):
    dev = fixed_device()

    async def go():
        net.replies = [(make_response(dev, b""), HOST)]
        await dev.send_packet(0x6A, b"")
        await dev.aclose()
        assert net.endpoints[-1].closed
        net.replies = [(make_response(dev, b""), HOST)]
        async with dev:
            await dev.send_packet(0x6A, b"")

    run(go())
    assert len(net.endpoints) == 2
    assert net.endpoints[-1].closed  # the context manager closed it


def test_send_packet_retries_then_times_out(net):
    dev = fixed_device()
    dev.timeout = 0.02
    net.replies = []  # never answers
    with pytest.raises(e.NetworkTimeoutError) as err:
        run(dev.send_packet(0x6A, b""))
    assert err.value.errno == -4000
    assert len(net.endpoints[-1].sent) >= 2  # resent at least once


def test_send_packet_rejects_short_response(net):
    dev = fixed_device()
    net.replies = [(bytes(0x10), HOST)]
    with pytest.raises(e.DataValidationError) as err:
        run(dev.send_packet(0x6A, b""))
    assert err.value.errno == -4007


def test_send_packet_rejects_bad_checksum(net):
    dev = fixed_device()
    frame = bytearray(make_response(dev, b""))
    frame[0x20] ^= 0xFF
    net.replies = [(bytes(frame), HOST)]
    with pytest.raises(e.DataValidationError) as err:
        run(dev.send_packet(0x6A, b""))
    assert err.value.errno == -4008


def test_stale_reply_is_drained_before_a_request(net):
    dev = fixed_device()

    async def go():
        net.replies = [(make_response(dev, b""), HOST)]
        await dev.send_packet(0x6A, b"")
        ep = net.endpoints[-1]
        # A late packet shows up between requests; it must not be taken as
        # the answer to the next one.
        stale = bytearray(make_response(dev, b""))
        stale[0x20] ^= 0xFF  # corrupt so it would fail validation if used
        ep.protocol.queue.put_nowait((bytes(stale), HOST))
        ep.replies = [(make_response(dev, bytes([7]) + bytes(15)), HOST)]
        resp = await dev.send_packet(0x6A, b"")
        return dev.decrypt(resp[0x38:])[0]

    assert run(go()) == 7


def stamped(dev: Device, payload: bytes, count: int, error: int = 0) -> bytes:
    """A response frame that echoes a packet counter, as real firmware does."""
    frame = bytearray(make_response(dev, payload, error))
    frame[0x28:0x2A] = count.to_bytes(2, "little")
    checksum = sum(frame, 0xBEAF) - sum(frame[0x20:0x22]) & 0xFFFF
    frame[0x20:0x22] = checksum.to_bytes(2, "little")
    return bytes(frame)


def test_late_reply_to_timed_out_request_is_not_taken_as_next_reply(net):
    """The defect that 0.19.0 could not have because it threw its socket away
    after every call: a slow answer to request 1 arriving after request 1
    timed out must not be returned as the answer to request 2."""
    dev = fixed_device()
    dev.timeout = 0.02

    async def go():
        await dev._endpoint()
        ep = net.endpoints[-1]
        with pytest.raises(e.NetworkTimeoutError):
            await dev.send_packet(0x6A, b"")  # request 1, count 0x8001, no answer
        # Its late reply lands while request 2 (count 0x8002) is waiting.
        late = stamped(dev, bytes([1]) + bytes(15), 0x8001)
        good = stamped(dev, bytes([2]) + bytes(15), 0x8002)
        ep.replies = [late, good] and []
        ep.protocol.queue.put_nowait((late, HOST))

        async def answer_later():
            await asyncio.sleep(0.005)
            ep.protocol.queue.put_nowait((good, HOST))

        asyncio.get_running_loop().create_task(answer_later())
        dev.timeout = 1
        resp = await dev.send_packet(0x6A, b"")
        return dev.decrypt(resp[0x38:])[0]

    assert run(go()) == 2


def test_reply_with_unknown_counter_is_accepted(net):
    """Firmware that does not echo the counter must keep working."""
    dev = fixed_device()

    async def go():
        net.replies = [(stamped(dev, bytes([5]) + bytes(15), 0x0000), HOST)]
        resp = await dev.send_packet(0x6A, b"")
        return dev.decrypt(resp[0x38:])[0]

    assert run(go()) == 5


def test_aclose_fails_inflight_request_fast(net):
    dev = fixed_device()
    dev.timeout = 5
    net.replies = []

    async def go():
        task = asyncio.get_running_loop().create_task(dev.send_packet(0x6A, b""))
        await asyncio.sleep(0.01)
        t0 = asyncio.get_running_loop().time()
        await dev.aclose()
        with pytest.raises(e.ConnectionClosedError):
            await task
        return asyncio.get_running_loop().time() - t0

    assert run(go()) < 1.0


def test_host_change_reopens_endpoint(net):
    dev = fixed_device()

    async def go():
        net.replies = [(make_response(dev, b""), HOST)]
        await dev.send_packet(0x6A, b"")
        dev.host = ("192.0.2.99", 80)
        net.replies = [(make_response(dev, b""), ("192.0.2.99", 80))]
        await dev.send_packet(0x6A, b"")

    run(go())
    assert [ep.remote_addr for ep in net.endpoints] == [HOST, ("192.0.2.99", 80)]
    assert net.endpoints[0].closed


# ------------------------------------------------------------------------- auth


def test_auth_uses_initial_key_and_installs_session_key(net):
    dev = fixed_device()
    dev.id = 99  # stale session; auth must reset it before sending
    dev.update_aes(bytes(range(16)))  # stale key

    session_id = 0x0000BEEF
    session_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    # The auth response payload: id at 0:4, key at 4:20, encrypted with the
    # INITIAL key, which is what the device expects auth to be decrypted with.
    fresh = fixed_device()
    reply = make_response(fresh, session_id.to_bytes(4, "little") + session_key)
    net.replies = [(reply, HOST)]

    assert run(dev.auth()) is True

    frame = net.endpoints[-1].sent[0][0]
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


def test_auth_surfaces_device_error(net):
    dev = fixed_device()
    net.replies = [(make_response(dev, bytes(20), error=0xFFF9), HOST)]
    with pytest.raises(e.AuthorizationError):
        run(dev.auth())


def test_expired_session_is_reauthenticated_once(net):
    dev = fixed_device()
    dev.id = 5
    session_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    fresh = fixed_device()
    auth_reply = make_response(fresh, (0x42).to_bytes(4, "little") + session_key)

    async def go():
        # First request: device says the control key expired (-7).
        net.replies = [(make_response(dev, b"", error=0xFFF9), HOST)]
        await dev._endpoint()
        ep = net.endpoints[-1]
        # After the expired reply the library must auth (reply 2, under the
        # initial key) and resend (reply 3, under the new session key).
        renewed = fixed_device()
        renewed.update_aes(session_key)
        ep.replies = [
            (auth_reply, HOST),
            (make_response(renewed, bytes([9]) + bytes(15)), HOST),
        ]
        ep.replies.insert(0, (make_response(dev, b"", error=0xFFF9), HOST))
        resp = await dev.send_packet(0x6A, bytes(16))
        return ep, resp

    ep, resp = run(go())
    types = [int.from_bytes(f[0x26:0x28], "little") for f, _ in ep.sent]
    assert types == [0x6A, 0x65, 0x6A]
    assert dev.id == 0x42
    assert resp[0x22:0x24] == b"\x00\x00"
    assert dev.decrypt(resp[0x38:])[0] == 9


def test_reauth_is_not_attempted_twice(net):
    dev = fixed_device()

    async def go():
        await dev._endpoint()
        ep = net.endpoints[-1]
        expired = (make_response(dev, b"", error=0xFFF9), HOST)
        ep.replies = [expired, expired]  # request fails, auth fails
        return await dev.send_packet(0x6A, b"")

    with pytest.raises(e.AuthorizationError):
        run(go())


def test_concurrent_callers_share_one_reauth(net):
    dev = fixed_device()
    dev.id = 5
    session_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    fresh = fixed_device()
    auth_reply = make_response(fresh, (0x42).to_bytes(4, "little") + session_key)
    renewed = fixed_device()
    renewed.update_aes(session_key)
    counters = {"value": 1}

    async def go():
        await dev._endpoint()
        ep = net.endpoints[-1]
        authed = {"done": False}

        def sendto(data, addr=None):
            ep.sent.append((bytes(data), addr or ep.remote_addr))
            ptype = int.from_bytes(data[0x26:0x28], "little")
            if ptype == 0x65:
                authed["done"] = True
                reply = auth_reply
            elif not authed["done"]:
                reply = make_response(dev, b"", error=0xFFF9)  # -7 expired
            else:
                n = counters["value"]
                counters["value"] += 1
                reply = make_response(renewed, bytes([n]) + bytes(15))
            ep.protocol.queue.put_nowait((reply, ep.remote_addr))

        ep.sendto = sendto
        a, b = await asyncio.gather(dev.send_packet(0x6A, b"a"), dev.send_packet(0x6A, b"b"))
        return ep, {dev.decrypt(a[0x38:])[0], dev.decrypt(b[0x38:])[0]}

    ep, values = run(go())
    types = [int.from_bytes(f[0x26:0x28], "little") for f, _ in ep.sent]
    assert types.count(0x65) == 1  # exactly one auth despite two expired requests
    assert values == {1, 2}


def test_logged_out_code_triggers_reauth(net):
    dev = fixed_device()
    session_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    fresh = fixed_device()
    auth_reply = make_response(fresh, (0x42).to_bytes(4, "little") + session_key)
    renewed = fixed_device()
    renewed.update_aes(session_key)

    async def go():
        await dev._endpoint()
        ep = net.endpoints[-1]
        ep.replies = [
            (make_response(dev, b"", error=0xFFFE), HOST),  # -2 logged out
            (auth_reply, HOST),
            (make_response(renewed, bytes([3]) + bytes(15)), HOST),
        ]
        resp = await dev.send_packet(0x6A, b"")
        return dev.decrypt(resp[0x38:])[0]

    assert run(go()) == 3


# ------------------------------------------------------------------- discovery


def hello_response(devtype: int, mac: bytes, name: str, locked: bool) -> bytes:
    frame = bytearray(0x80)
    frame[0x34:0x36] = devtype.to_bytes(2, "little")
    frame[0x3A:0x40] = mac[::-1]
    frame[0x40 : 0x40 + len(name)] = name.encode()
    frame[0x7F] = int(locked)
    return bytes(frame)


async def collect(aiter):
    return [x async for x in aiter]


def test_scan_builds_hello_packet_and_parses_replies(net):
    other = bytes.fromhex("34ea34000001")
    net.replies = [
        (hello_response(0x6026, MAC, "Bedroom RM", False), ("192.0.2.10", 80)),
        (hello_response(0x6026, MAC, "Bedroom RM", False), ("192.0.2.10", 80)),  # dup
        (hello_response(0x2711, other, "Plug", True), ("192.0.2.11", 80)),
    ]

    async def go():
        found = []
        async for entry in device_module.scan(timeout=0.02, local_ip_address="192.0.2.2"):
            found.append(entry)
            ep = net.endpoints[-1]
            # replies are released one per send; pull the rest through
            while ep.replies:
                ep.protocol.queue.put_nowait(ep.replies.pop(0))
        return found

    found = run(go())
    assert found == [
        (0x6026, ("192.0.2.10", 80), MAC, "Bedroom RM", False),
        (0x2711, ("192.0.2.11", 80), other, "Plug", True),
    ]
    ep = net.endpoints[-1]
    assert ep.local_addr == ("192.0.2.2", 0)
    assert ep.broadcast is True
    packet, addr = ep.sent[0]
    assert addr == ("255.255.255.255", 80)
    assert len(packet) == 0x30
    assert packet[0x26] == 6
    assert packet[0x18:0x1C] == socket.inet_aton("192.0.2.2")[::-1]
    assert packet[0x1C:0x1E] == (40000).to_bytes(2, "little")  # bound port
    body = bytearray(packet)
    body[0x20:0x22] = b"\x00\x00"
    assert packet[0x20:0x22] == (sum(body, 0xBEAF) & 0xFFFF).to_bytes(2, "little")
    assert ep.closed


def test_discover_and_hello_build_devices(net):
    net.replies = [
        (hello_response(0x6026, MAC, "Bedroom RM", False), ("192.0.2.10", 80)),
    ]
    devices = run(broadlink.discover(timeout=0.02))
    assert len(devices) == 1
    dev = devices[0]
    assert isinstance(dev, broadlink.rm4pro)
    assert dev.host == ("192.0.2.10", 80)
    assert dev.mac == MAC
    assert dev.name == "Bedroom RM"
    assert dev.model == "RM4 pro"
    assert dev.manufacturer == "Broadlink"

    net.replies = [
        (hello_response(0x6026, MAC, "Bedroom RM", True), ("192.0.2.10", 80)),
    ]
    dev = run(broadlink.hello("192.0.2.10", timeout=0.02))
    assert dev.is_locked is True
    assert net.endpoints[-1].sent[0][1] == ("192.0.2.10", 80)
    assert net.endpoints[-1].closed


def test_hello_times_out(net):
    net.replies = []
    with pytest.raises(e.NetworkTimeoutError):
        run(broadlink.hello("192.0.2.10", timeout=0.02))


def test_device_hello_validates_identity(net):
    dev = fixed_device(broadlink.rm4pro, 0x6026)
    net.replies = [(hello_response(0x6026, MAC, "Renamed", True), HOST)]
    assert run(dev.hello()) is True
    assert dev.name == "Renamed"
    assert dev.is_locked is True

    net.replies = [
        (hello_response(0x6026, bytes.fromhex("000000000001"), "Other", False), HOST)
    ]
    with pytest.raises(e.DataValidationError):
        run(dev.hello())

    net.replies = [(hello_response(0x2711, MAC, "Other", False), HOST)]
    with pytest.raises(e.DataValidationError):
        run(dev.hello())


def test_ping_packet(net):
    dev = fixed_device()
    run(dev.ping())
    packet, addr = net.endpoints[-1].sent[0]
    assert addr == HOST
    assert len(packet) == 0x30
    assert packet[0x26] == 1
    assert net.endpoints[-1].closed


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


def test_setup_packet(net):
    run(broadlink.setup("MyWifi", "hunter2", 3, ip_address="192.0.2.255"))
    packet, addr = net.endpoints[-1].sent[0]
    assert addr == ("192.0.2.255", 80)
    assert net.endpoints[-1].broadcast is True
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
    assert net.endpoints[-1].closed


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
