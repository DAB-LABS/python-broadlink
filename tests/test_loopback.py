"""The real datagram path, on loopback.

Every other transport test replaces ``_open_endpoint`` with a fake, so the
lines that talk to asyncio's real datagram transport (``_Protocol``,
``_open_endpoint``) are only exercised here. A small fake device answers on
127.0.0.1 with real sockets.
"""

from __future__ import annotations

import asyncio
import socket
import sys

import pytest

from broadlink import device as device_module
from broadlink import exceptions as e
from broadlink.device import Device
from tests.oracle.harness import MAC, make_response


class FakeDevice(asyncio.DatagramProtocol):
    """Answers every request frame with a canned payload, counter echoed."""

    def __init__(self, dev: Device, payload: bytes) -> None:
        self.dev = dev
        self.payload = payload
        self.received: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.received.append(data)
        frame = bytearray(make_response(self.dev, self.payload))
        frame[0x28:0x2A] = data[0x28:0x2A]
        checksum = sum(frame, 0xBEAF) - sum(frame[0x20:0x22]) & 0xFFFF
        frame[0x20:0x22] = checksum.to_bytes(2, "little")
        assert self.transport is not None
        self.transport.sendto(bytes(frame), addr)


async def start_fake(dev: Device, payload: bytes):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: FakeDevice(dev, payload), local_addr=("127.0.0.1", 0)
    )
    return transport, protocol, transport.get_extra_info("sockname")[:2]


def unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_request_and_concurrent_requests_over_real_sockets():
    async def go():
        dev = Device(("127.0.0.1", 1), MAC, 0x2737, name="Loopback")
        transport, fake, addr = await start_fake(dev, bytes([7]) + bytes(15))
        dev.host = addr
        try:
            async with dev:
                resp = await dev.send_packet(0x6A, b"")
                assert dev.decrypt(resp[0x38:])[0] == 7
                results = await asyncio.gather(
                    *(dev.send_packet(0x6A, bytes([i])) for i in range(20))
                )
                assert all(dev.decrypt(r[0x38:])[0] == 7 for r in results)
                assert isinstance(dev._protocol, device_module._Protocol)
            assert dev._transport is None
            return len(fake.received)
        finally:
            transport.close()

    assert asyncio.run(go()) == 21


@pytest.mark.skipif(sys.platform == "win32", reason="ICMP errors surface differently")
def test_endpoint_heals_after_a_socket_error_on_loopback(caplog):
    """A request to a loopback port nobody listens on draws an ICMP port
    unreachable, which the connected socket reports on its next read. That
    one is treated like silence (the original's unconnected socket never
    saw it) so the request times out as before, but it must have reached
    the protocol, and the device must reopen its socket for the next call
    instead of reusing the dead one."""
    caplog.set_level("DEBUG", logger="broadlink.device")

    async def go():
        dev = Device(("127.0.0.1", unused_udp_port()), MAC, 0x2737, name="Loopback")
        dev.timeout = 0.3
        try:
            with pytest.raises(e.NetworkTimeoutError):
                await dev.send_packet(0x6A, b"")
            assert dev._transport is None
            # Point it at a live fake device: the next call opens a new socket.
            transport, _, addr = await start_fake(dev, bytes([9]) + bytes(15))
            try:
                dev.host = addr
                resp = await dev.send_packet(0x6A, b"")
                return dev.decrypt(resp[0x38:])[0]
            finally:
                transport.close()
        finally:
            await dev.aclose()

    assert asyncio.run(go()) == 9
    assert "unreachable" in caplog.text
