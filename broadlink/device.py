"""Support for Broadlink devices.

Transport layer. Every device method ends up in :meth:`Device.send_packet`,
which frames, encrypts and sends one request over UDP and waits for the one
reply. The protocol is strictly request and reply and the device never
speaks unprompted, so each device keeps a single datagram endpoint and an
``asyncio.Lock`` that serializes calls on it.
"""

from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import AsyncIterator
from typing import Optional, Tuple, Union

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import exceptions as e
from .const import (
    DEFAULT_BCAST_ADDR,
    DEFAULT_PORT,
    DEFAULT_RETRY_INTVL,
    DEFAULT_TIMEOUT,
)
from .protocol import Datetime

HelloResponse = Tuple[int, Tuple[str, int], bytes, str, bool]

# Device error codes that mean the session key is no longer accepted and a
# fresh auth() will fix it. -7: control key expired; -4012: control id error.
_REAUTH_CODES = {-7, -4012}


class _Protocol(asyncio.DatagramProtocol):
    """Datagram protocol that hands every received packet to a queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queue.put_nowait((data, addr))

    def error_received(self, exc: Exception) -> None:
        # ICMP unreachable and the like. Surface it as a receive of nothing;
        # the retry loop will time out and raise NetworkTimeoutError.
        pass

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if not self.closed.done():
            self.closed.set_result(None)

    def drain(self) -> None:
        """Drop anything that arrived before the current request."""
        while not self.queue.empty():
            self.queue.get_nowait()


async def _open_endpoint(
    local_addr: Optional[tuple[str, int]] = None,
    remote_addr: Optional[tuple[str, int]] = None,
    broadcast: bool = False,
) -> tuple[asyncio.DatagramTransport, _Protocol]:
    """Create a UDP endpoint. Tests replace this to fake the network."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _Protocol,
        local_addr=local_addr,
        remote_addr=remote_addr,
        family=socket.AF_INET,
        allow_broadcast=broadcast,
    )
    return transport, protocol  # type: ignore[return-value]


def _hello_packet(local_ip_address: str, port: int) -> bytearray:
    packet = bytearray(0x30)
    packet[0x08:0x14] = Datetime.pack(Datetime.now())
    packet[0x18:0x1C] = socket.inet_aton(local_ip_address)[::-1]
    packet[0x1C:0x1E] = port.to_bytes(2, "little")
    packet[0x26] = 6
    checksum = sum(packet, 0xBEAF) & 0xFFFF
    packet[0x20:0x22] = checksum.to_bytes(2, "little")
    return packet


def _parse_hello(resp: bytes, host: tuple[str, int]) -> HelloResponse:
    devtype = resp[0x34] | resp[0x35] << 8
    mac = resp[0x3A:0x40][::-1]
    name = resp[0x40:].split(b"\x00")[0].decode()
    is_locked = bool(resp[0x7F])
    return devtype, host, mac, name, is_locked


async def scan(
    timeout: float = DEFAULT_TIMEOUT,
    local_ip_address: Optional[str] = None,
    discover_ip_address: str = DEFAULT_BCAST_ADDR,
    discover_ip_port: int = DEFAULT_PORT,
) -> AsyncIterator[HelloResponse]:
    """Broadcast a hello message and yield responses as they arrive.

    The hello is repeated every ``DEFAULT_RETRY_INTVL`` seconds until
    ``timeout`` elapses. Each device is yielded once.
    """
    local_addr = (local_ip_address, 0) if local_ip_address else None
    transport, protocol = await _open_endpoint(local_addr=local_addr, broadcast=True)
    try:
        if local_ip_address:
            port = transport.get_extra_info("sockname")[1]
        else:
            local_ip_address = "0.0.0.0"
            port = 0
        packet = _hello_packet(local_ip_address, port)

        loop = asyncio.get_running_loop()
        start = loop.time()
        discovered: set[tuple[tuple[str, int], bytes, int]] = set()

        while (loop.time() - start) < timeout:
            transport.sendto(packet, (discover_ip_address, discover_ip_port))
            deadline = min(DEFAULT_RETRY_INTVL, timeout - (loop.time() - start))
            slot_end = loop.time() + deadline
            while True:
                remaining = slot_end - loop.time()
                if remaining <= 0:
                    break
                try:
                    resp, host = await asyncio.wait_for(protocol.queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if len(resp) < 0x80:
                    continue
                entry = _parse_hello(resp, host)
                key = (entry[1], entry[2], entry[0])
                if key in discovered:
                    continue
                discovered.add(key)
                yield entry
    finally:
        transport.close()


async def ping(ip_address: str, port: int = DEFAULT_PORT) -> None:
    """Send a ping packet to an address.

    This packet feeds the watchdog timer of firmwares >= v53.
    Useful to prevent reboots when the cloud cannot be reached.
    It must be sent every 2 minutes in such cases.
    """
    transport, _ = await _open_endpoint(broadcast=True)
    try:
        packet = bytearray(0x30)
        packet[0x26] = 1
        transport.sendto(packet, (ip_address, port))
    finally:
        transport.close()


class Device:
    """Controls a Broadlink device."""

    TYPE = "Unknown"

    __INIT_KEY = "097628343fe99e23765c1513accf8b02"
    __INIT_VECT = "562e17996d093d28ddb3ba695a2e6f58"

    def __init__(
        self,
        host: Tuple[str, int],
        mac: Union[bytes, str],
        devtype: int,
        timeout: float = DEFAULT_TIMEOUT,
        name: str = "",
        model: str = "",
        manufacturer: str = "",
        is_locked: bool = False,
    ) -> None:
        """Initialize the controller."""
        self.host = host
        self.mac = bytes.fromhex(mac) if isinstance(mac, str) else mac
        self.devtype = devtype
        self.timeout = timeout
        self.name = name
        self.model = model
        self.manufacturer = manufacturer
        self.is_locked = is_locked
        self.count = random.randint(0x8000, 0xFFFF)
        self.iv = bytes.fromhex(self.__INIT_VECT)
        self.id = 0
        self.type = self.TYPE  # For backwards compatibility.

        self.aes = None
        self.update_aes(bytes.fromhex(self.__INIT_KEY))

        self._lock: Optional[asyncio.Lock] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol: Optional[_Protocol] = None
        self._reauth_ok = True

    def __repr__(self) -> str:
        """Return a formal representation of the device."""
        return (
            "%s.%s(%s, mac=%r, devtype=%r, timeout=%r, name=%r, "
            "model=%r, manufacturer=%r, is_locked=%r)"
        ) % (
            self.__class__.__module__,
            self.__class__.__qualname__,
            self.host,
            self.mac,
            self.devtype,
            self.timeout,
            self.name,
            self.model,
            self.manufacturer,
            self.is_locked,
        )

    def __str__(self) -> str:
        """Return a readable representation of the device."""
        return "%s (%s / %s:%s / %s)" % (
            self.name or "Unknown",
            " ".join(filter(None, [self.manufacturer, self.model, hex(self.devtype)])),
            *self.host,
            ":".join(format(x, "02X") for x in self.mac),
        )

    async def __aenter__(self) -> "Device":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------ crypto

    def update_aes(self, key: bytes) -> None:
        """Update AES."""
        self.aes = Cipher(
            algorithms.AES(bytes(key)), modes.CBC(self.iv), backend=default_backend()
        )

    def encrypt(self, payload: bytes) -> bytes:
        """Encrypt the payload."""
        encryptor = self.aes.encryptor()
        return encryptor.update(bytes(payload)) + encryptor.finalize()

    def decrypt(self, payload: bytes) -> bytes:
        """Decrypt the payload."""
        decryptor = self.aes.decryptor()
        return decryptor.update(bytes(payload)) + decryptor.finalize()

    # ---------------------------------------------------------- session

    async def auth(self) -> bool:
        """Authenticate to the device."""
        self.id = 0
        self.update_aes(bytes.fromhex(self.__INIT_KEY))

        packet = bytearray(0x50)
        packet[0x04:0x14] = [0x31] * 16
        packet[0x1E] = 0x01
        packet[0x2D] = 0x01
        packet[0x30:0x36] = "Test 1".encode()

        response = await self.send_packet(0x65, packet, _reauth=False)
        e.check_error(response[0x22:0x24])
        payload = self.decrypt(response[0x38:])

        self.id = int.from_bytes(payload[:0x4], "little")
        self.update_aes(payload[0x04:0x14])
        return True

    async def hello(self, local_ip_address=None) -> bool:
        """Send a hello message to the device.

        Device information is checked before updating name and lock status.
        """
        responses = scan(
            timeout=self.timeout,
            local_ip_address=local_ip_address,
            discover_ip_address=self.host[0],
            discover_ip_port=self.host[1],
        )
        entry = None
        async for entry in responses:
            break
        if entry is None:
            raise e.NetworkTimeoutError(
                -4000,
                "Network timeout",
                f"No response received within {self.timeout}s",
            )
        devtype, _, mac, name, is_locked = entry

        if mac != self.mac:
            raise e.DataValidationError(
                -2040,
                "Device information is not intact",
                "The MAC address is different",
                f"Expected {self.mac} and received {mac}",
            )

        if devtype != self.devtype:
            raise e.DataValidationError(
                -2040,
                "Device information is not intact",
                "The product ID is different",
                f"Expected {self.devtype} and received {devtype}",
            )

        self.name = name
        self.is_locked = is_locked
        return True

    async def ping(self) -> None:
        """Ping the device.

        This packet feeds the watchdog timer of firmwares >= v53.
        Useful to prevent reboots when the cloud cannot be reached.
        It must be sent every 2 minutes in such cases.
        """
        await ping(self.host[0], port=self.host[1])

    async def get_fwversion(self) -> int:
        """Get firmware version."""
        packet = bytearray([0x68])
        response = await self.send_packet(0x6A, packet)
        e.check_error(response[0x22:0x24])
        payload = self.decrypt(response[0x38:])
        return payload[0x4] | payload[0x5] << 8

    async def set_name(self, name: str) -> None:
        """Set device name."""
        packet = bytearray(4)
        packet += name.encode("utf-8")
        packet += bytearray(0x50 - len(packet))
        packet[0x43] = self.is_locked
        response = await self.send_packet(0x6A, packet)
        e.check_error(response[0x22:0x24])
        self.name = name

    async def set_lock(self, state: bool) -> None:
        """Lock/unlock the device."""
        packet = bytearray(4)
        packet += self.name.encode("utf-8")
        packet += bytearray(0x50 - len(packet))
        packet[0x43] = bool(state)
        response = await self.send_packet(0x6A, packet)
        e.check_error(response[0x22:0x24])
        self.is_locked = bool(state)

    def get_type(self) -> str:
        """Return device type."""
        return self.type

    # -------------------------------------------------------- transport

    async def aclose(self) -> None:
        """Close the device's endpoint. It is reopened on the next call."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            self._protocol = None

    async def _endpoint(self) -> tuple[asyncio.DatagramTransport, _Protocol]:
        if self._transport is None or self._transport.is_closing():
            self._transport, self._protocol = await _open_endpoint(
                remote_addr=self.host
            )
        return self._transport, self._protocol  # type: ignore[return-value]

    def _frame(self, packet_type: int, payload: bytes) -> bytes:
        """Build the wire frame for one request (advances the counter)."""
        self.count = ((self.count + 1) | 0x8000) & 0xFFFF
        packet = bytearray(0x38)
        packet[0x00:0x08] = bytes.fromhex("5aa5aa555aa5aa55")
        packet[0x24:0x26] = self.devtype.to_bytes(2, "little")
        packet[0x26:0x28] = packet_type.to_bytes(2, "little")
        packet[0x28:0x2A] = self.count.to_bytes(2, "little")
        packet[0x2A:0x30] = self.mac[::-1]
        packet[0x30:0x34] = self.id.to_bytes(4, "little")

        p_checksum = sum(payload, 0xBEAF) & 0xFFFF
        packet[0x34:0x36] = p_checksum.to_bytes(2, "little")

        padding = (16 - len(payload)) % 16
        payload = self.encrypt(payload + bytes(padding))
        packet.extend(payload)

        checksum = sum(packet, 0xBEAF) & 0xFFFF
        packet[0x20:0x22] = checksum.to_bytes(2, "little")
        return bytes(packet)

    @staticmethod
    def _validate(resp: bytes) -> bytes:
        if len(resp) < 0x30:
            raise e.DataValidationError(
                -4007,
                "Received data packet length error",
                f"Expected at least 48 bytes and received {len(resp)}",
            )

        nom_checksum = int.from_bytes(resp[0x20:0x22], "little")
        real_checksum = sum(resp, 0xBEAF) - sum(resp[0x20:0x22]) & 0xFFFF

        if nom_checksum != real_checksum:
            raise e.DataValidationError(
                -4008,
                "Received data packet check error",
                f"Expected a checksum of {nom_checksum} and received {real_checksum}",
            )
        return resp

    async def _exchange(self, packet: bytes) -> bytes:
        """Send one frame and wait for one reply, resending on silence."""
        transport, protocol = await self._endpoint()
        protocol.drain()
        loop = asyncio.get_running_loop()
        start = loop.time()
        timeout = self.timeout

        while True:
            transport.sendto(packet)
            time_left = timeout - (loop.time() - start)
            wait = min(DEFAULT_RETRY_INTVL, time_left)
            try:
                resp, _ = await asyncio.wait_for(protocol.queue.get(), max(wait, 0))
            except asyncio.TimeoutError:
                if (loop.time() - start) >= timeout:
                    raise e.NetworkTimeoutError(
                        -4000,
                        "Network timeout",
                        f"No response received within {timeout}s",
                    ) from None
                continue
            return self._validate(resp)

    async def send_packet(
        self, packet_type: int, payload: bytes, *, _reauth: bool = True
    ) -> bytes:
        """Send a packet to the device and return the raw response frame.

        If the device answers that the session key is no longer valid, the
        session is re-authenticated once and the request is sent again.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            resp = await self._exchange(self._frame(packet_type, bytes(payload)))

        if _reauth and self._reauth_ok:
            code = int.from_bytes(resp[0x22:0x24], "little", signed=True)
            if code in _REAUTH_CODES:
                self._reauth_ok = False
                try:
                    await self.auth()
                    async with self._lock:
                        resp = await self._exchange(
                            self._frame(packet_type, bytes(payload))
                        )
                finally:
                    self._reauth_ok = True
        return resp
