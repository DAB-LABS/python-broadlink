"""Support for covers."""
import asyncio
from typing import Sequence

from . import exceptions as e
from .device import Device


class dooya(Device):
    """Controls a Dooya curtain motor."""

    TYPE = "DT360E"

    async def _send(self, command: int, attribute: int = 0) -> int:
        """Send a packet to the device."""
        packet = bytearray(16)
        packet[0x00] = 0x09
        packet[0x02] = 0xBB
        packet[0x03] = command
        packet[0x04] = attribute
        packet[0x09] = 0xFA
        packet[0x0A] = 0x44

        resp = await self.send_packet(0x6A, packet)
        e.check_error(resp[0x22:0x24])
        payload = self.decrypt(resp[0x38:])
        return payload[4]

    async def open(self) -> int:
        """Open the curtain."""
        return await self._send(0x01)

    async def close(self) -> int:
        """Close the curtain."""
        return await self._send(0x02)

    async def stop(self) -> int:
        """Stop the curtain."""
        return await self._send(0x03)

    async def get_percentage(self) -> int:
        """Return the position of the curtain."""
        return await self._send(0x06, 0x5D)

    async def set_percentage_and_wait(self, new_percentage: int) -> None:
        """Set the position of the curtain."""
        current = await self.get_percentage()
        if current > new_percentage:
            await self.close()
            while current is not None and current > new_percentage:
                await asyncio.sleep(0.2)
                current = await self.get_percentage()

        elif current < new_percentage:
            await self.open()
            while current is not None and current < new_percentage:
                await asyncio.sleep(0.2)
                current = await self.get_percentage()
        await self.stop()


class dooya2(Device):
    """Controls a Dooya curtain motor (version 2)."""

    TYPE = "DT360E-2"

    async def _send(self, operation: int, data: Sequence = b""):
        """Send a command to the device."""
        packet = bytearray(12)
        packet[0x02] = 0xA5
        packet[0x03] = 0xA5
        packet[0x04] = 0x5A
        packet[0x05] = 0x5A
        packet[0x08] = operation
        packet[0x09] = 0x0B

        if data:
            data_len = len(data)
            packet[0x0A] = data_len & 0xFF
            packet[0x0B] = data_len >> 8
            packet += bytes(2)
            packet.extend(data)

        checksum = sum(packet, 0xBEAF) & 0xFFFF
        packet[0x06] = checksum & 0xFF
        packet[0x07] = checksum >> 8

        packet_len = len(packet) - 2
        packet[0x00] = packet_len & 0xFF
        packet[0x01] = packet_len >> 8

        resp = await self.send_packet(0x6A, packet)
        e.check_error(resp[0x22:0x24])
        payload = self.decrypt(resp[0x38:])
        return payload

    async def open(self) -> None:
        """Open the curtain."""
        await self._send(2, [0x00, 0x01, 0x00])

    async def close(self) -> None:
        """Close the curtain."""
        await self._send(2, [0x00, 0x02, 0x00])

    async def stop(self) -> None:
        """Stop the curtain."""
        await self._send(2, [0x00, 0x03, 0x00])

    async def get_percentage(self) -> int:
        """Return the position of the curtain."""
        resp = await self._send(1, [0x00, 0x06, 0x00])
        return resp[0x11]

    async def set_percentage(self, new_percentage: int) -> None:
        """Set the position of the curtain."""
        await self._send(2, [0x00, 0x09, new_percentage])


class wser(Device):
    """Controls a Wistar curtain motor"""

    TYPE = "WSER"

    async def _send(self, operation: int, data: Sequence = b""):
        """Send a command to the device."""
        packet = bytearray(12)
        packet[0x02] = 0xA5
        packet[0x03] = 0xA5
        packet[0x04] = 0x5A
        packet[0x05] = 0x5A
        packet[0x08] = operation
        packet[0x09] = 0x0B

        if data:
            data_len = len(data)
            packet[0x0A] = data_len & 0xFF
            packet[0x0B] = data_len >> 8
            packet += bytes(2)
            packet.extend(data)

        checksum = sum(packet, 0xBEAF) & 0xFFFF
        packet[0x06] = checksum & 0xFF
        packet[0x07] = checksum >> 8

        packet_len = len(packet) - 2
        packet[0x00] = packet_len & 0xFF
        packet[0x01] = packet_len >> 8

        resp = await self.send_packet(0x6A, packet)
        e.check_error(resp[0x22:0x24])
        payload = self.decrypt(resp[0x38:])
        return payload

    async def get_position(self) -> int:
        """Return the position of the curtain."""
        resp = await self._send(1, [])
        position = resp[0x0E]
        return position

    async def open(self) -> int:
        """Open the curtain."""
        resp = await self._send(2, [0x4A, 0x31, 0xA0])
        position = resp[0x0E]
        return position

    async def close(self) -> int:
        """Close the curtain."""
        resp = await self._send(2, [0x61, 0x32, 0xA0])
        position = resp[0x0E]
        return position

    async def stop(self) -> int:
        """Stop the curtain."""
        resp = await self._send(2, [0x4C, 0x73, 0xA0])
        position = resp[0x0E]
        return position

    async def set_position(self, position: int) -> int:
        """Set the position of the curtain."""
        resp = await self._send(2, [position, 0x70, 0xA0])
        position = resp[0x0E]
        return position
