"""Support for Broadlink devices.

Transport layer. Every device method ends up in :meth:`Device.send_packet`,
which frames, encrypts and sends one request over UDP and waits for the one
reply. The protocol is strictly request and reply and the device never
speaks unprompted, so each device keeps a single datagram endpoint and an
``asyncio.Lock`` that serializes calls on it.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import errno
import logging
import random
import socket
from collections.abc import AsyncGenerator

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

_LOGGER = logging.getLogger(__name__)

HelloResponse = tuple[int, tuple[str, int], bytes, str, bool]

# Device error codes that mean the session key is no longer accepted and a
# fresh auth() will fix it. -2: logged out; -7: control key expired;
# -4012: control id error.
_REAUTH_CODES = {-2, -7, -4012}

# How many recently used request counters to remember. A reply carrying one
# of them (other than the current request's) is a late or duplicate answer
# to an earlier request and is dropped rather than taken as the answer to
# the current one. 64 covers a burst of resends comfortably and ages out
# long before the 16-bit counter wraps.
_RECENT_MAX = 64

_CLOSED = (None, None)
"""Sentinel put on the receive queue when the endpoint is closed."""

_QueueItem = tuple[bytes | Exception | None, tuple[str, int] | None]
"""What the receive queue carries: a datagram with its source address, an
error the socket reported (address ``None``), or ``_CLOSED``."""


def _is_silence(item: object) -> bool:
    """True for the socket errors that mean "no device answered".

    A connected datagram socket learns from ICMP that nobody is listening
    (port unreachable, ``ConnectionRefusedError``; ``ConnectionResetError``
    on Windows) or that the host cannot be reached (``EHOSTUNREACH``, from
    a router answering for a host that is off). The original library used
    an unconnected socket that never received any of these and simply
    timed out, and callers such as Home Assistant treat a timeout more
    leniently than an ``OSError``, so these are treated as silence.
    """
    if isinstance(item, ConnectionRefusedError | ConnectionResetError):
        return True
    return isinstance(item, OSError) and item.errno == errno.EHOSTUNREACH


class _Protocol(asyncio.DatagramProtocol):
    """Datagram protocol that hands every received packet to a queue.

    Errors the socket reports go on the same queue, so the request that is
    waiting fails at once instead of waiting out its timeout.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Keep the transport; the endpoint sends through it."""
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Queue every datagram for the request that is waiting."""
        self.queue.put_nowait((data, addr))

    def error_received(self, exc: Exception) -> None:
        """Queue a send failure or an ICMP error for the waiting request."""
        self.queue.put_nowait((exc, None))

    def connection_lost(self, exc: Exception | None) -> None:
        """Wake the waiting request if asyncio closed the transport on us."""
        self.queue.put_nowait((exc, None) if exc is not None else _CLOSED)

    def drain(self) -> None:
        """Drop anything that arrived before the current request."""
        while not self.queue.empty():
            self.queue.get_nowait()

    def raise_if_error(self) -> None:
        """Raise the error a send just reported, if it reported one.

        asyncio delivers a failed ``sendto`` to ``error_received`` before
        ``sendto`` returns, so a fire-and-forget sender can check right
        after sending and raise the ``OSError`` the way a plain socket did.
        """
        while not self.queue.empty():
            item, _ = self.queue.get_nowait()
            if isinstance(item, Exception):
                raise item


async def _open_endpoint(
    local_addr: tuple[str, int] | None = None,
    remote_addr: tuple[str, int] | None = None,
    broadcast: bool = False,
) -> tuple[asyncio.DatagramTransport, _Protocol]:
    """Create a UDP endpoint. Tests replace this to fake the network.

    An endpoint with no address on either side is bound to ``0.0.0.0``
    explicitly; the proactor loop on Windows starts receiving as soon as the
    endpoint exists, which needs a bound socket.
    """
    if local_addr is None and remote_addr is None:
        local_addr = ("0.0.0.0", 0)
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _Protocol,
        local_addr=local_addr,
        remote_addr=remote_addr,
        family=socket.AF_INET,
        allow_broadcast=broadcast,
    )
    return transport, protocol  # type: ignore[return-value]


async def _resolve(host: str, port: int) -> tuple[str, int]:
    """Resolve a destination once, off the event loop.

    Sending to a hostname through an unconnected datagram socket would
    resolve it with a blocking call on the loop and hide the failure. A
    name that does not resolve raises ``socket.gaierror`` here, as the
    original library's ``sendto`` did.
    """
    loop = asyncio.get_running_loop()
    info = await loop.getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
    )
    return info[0][4][:2]  # type: ignore[return-value]


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
    local_ip_address: str | None = None,
    discover_ip_address: str = DEFAULT_BCAST_ADDR,
    discover_ip_port: int = DEFAULT_PORT,
) -> AsyncGenerator[HelloResponse]:
    """Broadcast a hello message and yield responses as they arrive.

    The hello is repeated every ``DEFAULT_RETRY_INTVL`` seconds until
    ``timeout`` elapses. Each device is yielded once.
    """
    local_addr = (local_ip_address, 0) if local_ip_address else None
    target = await _resolve(discover_ip_address, discover_ip_port)
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
            transport.sendto(packet, target)
            deadline = min(DEFAULT_RETRY_INTVL, timeout - (loop.time() - start))
            slot_end = loop.time() + deadline
            while True:
                remaining = slot_end - loop.time()
                if remaining <= 0:
                    break
                try:
                    resp, host = await asyncio.wait_for(protocol.queue.get(), remaining)
                except TimeoutError:
                    break
                if resp is None:
                    return  # The transport was closed under us.
                if isinstance(resp, Exception):
                    raise resp
                if host is None or len(resp) < 0x80:
                    continue
                entry = _parse_hello(resp, host)
                key = (entry[1], entry[2], entry[0])
                if key in discovered:
                    continue
                discovered.add(key)
                yield entry
    finally:
        transport.close()


async def send_setup_packet(
    payload: bytes, ip_address: str, port: int = DEFAULT_PORT
) -> None:
    """Broadcast one Wi-Fi provisioning packet to a device in AP mode."""
    target = await _resolve(ip_address, port)
    transport, protocol = await _open_endpoint(broadcast=True)
    try:
        transport.sendto(payload, target)
        protocol.raise_if_error()
    finally:
        transport.close()


async def ping(ip_address: str, port: int = DEFAULT_PORT) -> None:
    """Send a ping packet to an address.

    This packet feeds the watchdog timer of firmwares >= v53.
    Useful to prevent reboots when the cloud cannot be reached.
    It must be sent every 2 minutes in such cases.
    """
    target = await _resolve(ip_address, port)
    transport, protocol = await _open_endpoint(broadcast=True)
    try:
        packet = bytearray(0x30)
        packet[0x26] = 1
        transport.sendto(packet, target)
        protocol.raise_if_error()
    finally:
        transport.close()


class Device:
    """Controls a Broadlink device."""

    TYPE = "Unknown"

    __INIT_KEY = "097628343fe99e23765c1513accf8b02"
    __INIT_VECT = "562e17996d093d28ddb3ba695a2e6f58"

    def __init__(
        self,
        host: tuple[str, int],
        mac: bytes | str,
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

        self._lock = asyncio.Lock()
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _Protocol | None = None
        self._endpoint_addr: tuple[str, int] | None = None
        self._recent: collections.deque[int] = collections.deque(maxlen=_RECENT_MAX)
        self._reauth_lock = asyncio.Lock()
        self._closes = 0  # Bumped by aclose(); guards an open racing a close.
        self._auth_generation = 0

    def __repr__(self) -> str:
        """Return a formal representation of the device."""
        return (
            f"{self.__class__.__module__}.{self.__class__.__qualname__}("
            f"{self.host}, mac={self.mac!r}, devtype={self.devtype!r}, "
            f"timeout={self.timeout!r}, name={self.name!r}, "
            f"model={self.model!r}, manufacturer={self.manufacturer!r}, "
            f"is_locked={self.is_locked!r})"
        )

    def __str__(self) -> str:
        """Return a readable representation of the device."""
        ident = " ".join(filter(None, [self.manufacturer, self.model, hex(self.devtype)]))
        mac = ":".join(format(x, "02X") for x in self.mac)
        name = self.name or "Unknown"
        return f"{name} ({ident} / {self.host[0]}:{self.host[1]} / {mac})"

    async def __aenter__(self) -> Device:
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
        """Authenticate to the device.

        The session reset, the exchange and the install of the new key all
        happen while holding the request lock, so a request queued behind
        the lock is never framed with the initial key or device id 0.
        """
        packet = bytearray(0x50)
        packet[0x04:0x14] = [0x31] * 16
        packet[0x1E] = 0x01
        packet[0x2D] = 0x01
        packet[0x30:0x36] = b"Test 1"

        async with self._lock:
            self.id = 0
            self.update_aes(bytes.fromhex(self.__INIT_KEY))
            response = await self._exchange(self._frame(0x65, bytes(packet)))
            e.check_error(response[0x22:0x24])
            payload = self.decrypt(response[0x38:])
            self.id = int.from_bytes(payload[:0x4], "little")
            self.update_aes(payload[0x04:0x14])
            self._auth_generation += 1
        _LOGGER.debug("%s: authenticated, session id %d", self.host[0], self.id)
        return True

    async def hello(self, local_ip_address: str | None = None) -> bool:
        """Send a hello message to the device.

        Device information is checked before updating name and lock status.
        """
        entry = None
        async with contextlib.aclosing(
            scan(
                timeout=self.timeout,
                local_ip_address=local_ip_address,
                discover_ip_address=self.host[0],
                discover_ip_port=self.host[1],
            )
        ) as responses:
            async for entry in responses:  # noqa: B007 - first reply only
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
        """Close the device's endpoint. It is reopened on the next call.

        A request in flight fails at once with ``ConnectionClosedError``
        rather than waiting out its timeout.
        """
        self._closes += 1
        transport, protocol = self._transport, self._protocol
        self._transport = None
        self._protocol = None
        self._endpoint_addr = None
        if transport is not None:
            transport.close()
            _LOGGER.debug("%s: endpoint closed", self.host[0])
        if protocol is not None:
            protocol.queue.put_nowait(_CLOSED)  # type: ignore[arg-type]

    def _drop_endpoint(self) -> None:
        """Throw the endpoint away after a failure; the next call reopens it.

        A connected datagram socket can go bad for good (the interface
        bounced, the host's address changed), and the original library
        never noticed because it opened a socket per call. Dropping the
        endpoint whenever a request fails restores that self-healing.
        """
        transport = self._transport
        self._transport = None
        self._protocol = None
        self._endpoint_addr = None
        if transport is not None:
            transport.close()
            _LOGGER.debug("%s: endpoint dropped after a failure", self.host[0])

    async def _endpoint(self) -> tuple[asyncio.DatagramTransport, _Protocol]:
        if self._transport is not None and self._endpoint_addr != self.host:
            # The caller changed host; the connected socket points at the
            # old address, so drop it.
            await self.aclose()
        if self._transport is None or self._transport.is_closing():
            closes = self._closes
            transport, protocol = await _open_endpoint(remote_addr=self.host)
            if self._closes != closes:
                # aclose() ran while the socket was being opened.
                transport.close()
                raise e.EndpointClosedError(
                    -4013, "Endpoint closed", "The device endpoint was closed"
                )
            self._transport, self._protocol = transport, protocol
            self._endpoint_addr = self.host
            _LOGGER.debug("%s: endpoint opened", self.host[0])
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
        """Send one frame and wait for its reply, resending on silence.

        Replies carry the request's packet counter (offset 0x28), so a reply
        is matched to the request by counter. A reply whose counter belongs
        to any other recent request (a late answer, or the second answer to
        a request that was resent) is dropped; one with a counter this
        device has not used recently is accepted, for firmware that may not
        echo it.
        """
        transport, protocol = await self._endpoint()
        protocol.drain()
        loop = asyncio.get_running_loop()
        start = loop.time()
        timeout = self.timeout
        count = int.from_bytes(packet[0x28:0x2A], "little")
        self._recent.append(count)
        sends = 0

        while True:
            transport.sendto(packet)
            sends += 1
            if sends > 1:
                _LOGGER.debug("%s: no reply, resending (%d)", self.host[0], sends)
            resend_at = loop.time() + DEFAULT_RETRY_INTVL
            while True:
                now = loop.time()
                if now - start >= timeout:
                    break
                wait = min(resend_at, start + timeout) - now
                try:
                    resp, _ = await asyncio.wait_for(protocol.queue.get(), max(wait, 0))
                except TimeoutError:
                    if loop.time() - start >= timeout:
                        break
                    if loop.time() >= resend_at:
                        break  # Resend.
                    continue
                if resp is None:
                    raise e.EndpointClosedError(
                        -4013, "Endpoint closed", "The device endpoint was closed"
                    )
                if _is_silence(resp):
                    # ICMP unreachable of one kind or another: the host is up
                    # with nothing listening, the device is off or rebooting,
                    # or a router answered for it. The original library's
                    # unconnected socket never saw these, so keep waiting and
                    # let the timeout decide, as it did.
                    _LOGGER.debug(
                        "%s: unreachable (%s), still waiting", self.host[0], resp
                    )
                    continue
                if isinstance(resp, Exception):
                    # A send failure (no route, address gone) or a fatal
                    # transport error: fail now and throw the socket away.
                    _LOGGER.debug("%s: socket error: %s", self.host[0], resp)
                    self._drop_endpoint()
                    raise resp
                resp = self._validate(resp)
                reply_count = int.from_bytes(resp[0x28:0x2A], "little")
                if reply_count == count or reply_count not in self._recent:
                    return resp
                _LOGGER.debug(
                    "%s: dropped a reply for an earlier request (counter 0x%04x)",
                    self.host[0],
                    reply_count,
                )
            if loop.time() - start >= timeout:
                _LOGGER.debug("%s: no reply within %ss", self.host[0], timeout)
                self._drop_endpoint()
                raise e.NetworkTimeoutError(
                    -4000,
                    "Network timeout",
                    f"No response received within {timeout}s",
                ) from None

    async def send_packet(self, packet_type: int, payload: bytes | bytearray) -> bytes:
        """Send a packet to the device and return the raw response frame.

        If the device answers that the session key is no longer valid, the
        session is re-authenticated once and the request is sent again.
        Concurrent callers that hit the same expired key share one
        re-authentication and each retry once. If that re-authentication
        fails (for example the device has been locked in the app), the
        original reply is returned unchanged, so the caller sees the same
        error the original library raised and can run its own recovery.
        """
        async with self._lock:
            generation = self._auth_generation
            resp = await self._exchange(self._frame(packet_type, bytes(payload)))

        code = int.from_bytes(resp[0x22:0x24], "little", signed=True)
        if code in _REAUTH_CODES:
            _LOGGER.debug("%s: device answered %d, re-authenticating", self.host[0], code)
            async with self._reauth_lock:
                if self._auth_generation == generation:
                    try:
                        await self.auth()
                    except (e.NetworkTimeoutError, e.EndpointClosedError):
                        # A network failure during re-authentication is
                        # reported as what it is, not as the device's
                        # original expired-key answer.
                        raise
                    except e.BroadlinkException as err:
                        _LOGGER.debug(
                            "%s: re-authentication failed: %s", self.host[0], err
                        )
                        return resp
            async with self._lock:
                resp = await self._exchange(self._frame(packet_type, bytes(payload)))
        return resp
