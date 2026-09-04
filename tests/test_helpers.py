"""Pure helpers: pulse packing, CRC16 and the protocol datetime."""

from __future__ import annotations

import datetime as dt

import pytest

from broadlink.helpers import CRC16
from broadlink.protocol import Datetime
from broadlink.remote import data_to_pulses, pulses_to_data

# NOTE: these pin the 0.19.0 behavior of the pulse helpers, including the
# 32.84 tick that upstream issue #839 identifies as wrong. They are expected
# to change, deliberately and in the same pull request, when the tick fix
# lands; until then they document what shipped.


def test_pulses_to_data_header_and_short_pulses():
    data = pulses_to_data([328, 656], tick=32.84)
    assert data[0] == 0x26
    assert data[1] == 0x00
    assert int.from_bytes(data[2:4], "little") == 2
    assert data[4:] == bytes([9, 19])  # floor(328/32.84)=9, floor(656/32.84)=19


def test_pulses_to_data_long_pulse_uses_three_byte_form():
    data = pulses_to_data([10000], tick=32.84)
    ticks = int(10000 // 32.84)  # 304
    assert data[4:] == bytes([0, ticks >> 8, ticks & 0xFF])
    assert int.from_bytes(data[2:4], "little") == 3


def test_data_to_pulses_round_trip_at_same_tick():
    pulses = [9000, 4500, 560, 560, 560, 1690, 40000]
    data = pulses_to_data(pulses)
    back = data_to_pulses(data)
    # Both directions use the same tick, so the round trip lands within a tick.
    for a, b in zip(pulses, back, strict=True):
        assert abs(a - b) <= 33


def test_data_to_pulses_honors_declared_length():
    data = pulses_to_data([328, 656]) + b"\x0d\x05"  # trailing terminator bytes
    assert len(data_to_pulses(data)) == 2


def test_data_to_pulses_rejects_truncated_long_form():
    with pytest.raises(ValueError):
        data_to_pulses(bytes([0x26, 0x00, 0x02, 0x00, 0x00, 0x01]))


def test_crc16_known_vector():
    # CRC-16/MODBUS of "123456789" is 0x4B37.
    assert CRC16.calculate(b"123456789") == 0x4B37
    assert CRC16.calculate(b"") == 0xFFFF


def test_crc16_table_is_cached():
    CRC16._cache.pop(0xA001, None)
    t1 = CRC16.get_table(0xA001)
    t2 = CRC16.get_table(0xA001)
    assert t1 is t2
    assert len(t1) == 256


def test_datetime_pack_layout():
    tz = dt.timezone(dt.timedelta(hours=-7))
    when = dt.datetime(2026, 9, 4, 14, 30, 0, tzinfo=tz)
    data = Datetime.pack(when)
    assert len(data) == 12
    assert int.from_bytes(data[0:4], "little", signed=True) == -7
    assert int.from_bytes(data[4:6], "little") == 2026
    assert data[6] == 30
    assert data[7] == 14
    assert data[8] == 26
    assert data[9] == 5  # Friday
    assert data[10] == 4
    assert data[11] == 9


def test_datetime_round_trip_and_validation():
    tz = dt.timezone(dt.timedelta(hours=2))
    when = dt.datetime(2026, 1, 15, 8, 5, 0, tzinfo=tz)
    data = bytearray(Datetime.pack(when))
    assert Datetime.unpack(bytes(data)) == when
    data[9] = 1  # wrong weekday
    with pytest.raises(ValueError):
        Datetime.unpack(bytes(data))


def test_datetime_now_has_tzinfo():
    assert Datetime.now().tzinfo is not None
