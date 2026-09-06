"""Support for sensors."""

import struct

from . import exceptions as e
from .device import Device


class a1(Device):
    """Controls a Broadlink A1."""

    TYPE = "A1"

    _SENSORS_AND_LEVELS = (
        ("light", ("dark", "dim", "normal", "bright")),
        ("air_quality", ("excellent", "good", "normal", "bad")),
        ("noise", ("quiet", "normal", "noisy")),
    )

    async def check_sensors(self) -> dict:
        """Return the state of the sensors."""
        data = await self.check_sensors_raw()
        for sensor, levels in self._SENSORS_AND_LEVELS:
            try:
                data[sensor] = levels[data[sensor]]
            except IndexError:
                data[sensor] = "unknown"
        return data

    async def check_sensors_raw(self) -> dict:
        """Return the state of the sensors in raw format."""
        packet = bytearray([0x1])
        resp = await self.send_packet(0x6A, packet)
        e.check_error(resp[0x22:0x24])
        data = self.decrypt(resp[0x38:])

        return {
            "temperature": data[0x04] + data[0x05] / 10.0,
            "humidity": data[0x06] + data[0x07] / 10.0,
            "light": data[0x08],
            "air_quality": data[0x0A],
            "noise": data[0x0C],
        }


class a2(Device):
    """Controls a Broadlink A2."""

    TYPE = "A2"

    async def _send(self, operation: int, data: bytes = b"") -> bytes:
        """Send a command to the device.

        The frame is the one the SP4 and LB1 families use: a two-byte
        length, the A5A5 5A5A marker, a checksum, the operation, 0x0B and
        a four-byte data length. The 0.19.0 code wrote a two-byte data
        length and a length field two short, and real A2 units answered
        every request with error -5 (mjg59/python-broadlink#826).
        """
        packet = bytearray(14)
        struct.pack_into(
            "<HHHHBBI",
            packet,
            0,
            12 + len(data),
            0xA5A5,
            0x5A5A,
            0,
            operation,
            0x0B,
            len(data),
        )
        packet.extend(data)
        checksum = sum(packet[0x02:], 0xBEAF) & 0xFFFF
        packet[0x06:0x08] = checksum.to_bytes(2, "little")

        resp = await self.send_packet(0x6A, packet)
        e.check_error(resp[0x22:0x24])
        return self.decrypt(resp[0x38:])

    async def check_sensors_raw(self) -> dict:
        """Return the state of the sensors in raw format."""
        data = await self._send(1)

        return {
            "temperature": data[0x13] * 256 + data[0x14],
            "humidity": data[0x15] * 256 + data[0x16],
            "pm10": data[0x0D] * 256 + data[0x0E],
            "pm2_5": data[0x0F] * 256 + data[0x10],
            "pm1": data[0x11] * 256 + data[0x12],
        }
