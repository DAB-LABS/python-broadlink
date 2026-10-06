"""Realigning RM4 Pro RF captures that started mid-pulse.

The misaligned packet below is two frames of a 433 MHz fixed-code remote as
an RM4 Pro returned it: the 0xB1 header with the carrier (433840 kHz) at
offset 4, a stray leading timing (0x35), then a short pulse, a 13.5 ms sync
gap and data. Its sync gaps land on carrier-on (even) slots. A full capture
with this shift did nothing when replayed; the same capture with the first
timing dropped worked (home-assistant/core#176041).
"""

from __future__ import annotations

import asyncio

import pytest

from broadlink.remote import SignalKind, pulses_to_data, realign_rf_packet
from tests.test_capture import FAST, UNIT, make, press_later, run

MISALIGNED = bytes.fromhex("b1c01600b09e0600350d00019a280c0d280d00019a280c0d280d")
ALIGNED = bytes.fromhex("b1c01500b09e06000d00019a280c0d280d00019a280c0d280d")


@pytest.mark.parametrize(
    ("packet", "expected"),
    [
        pytest.param(MISALIGNED, ALIGNED, id="misaligned"),
        pytest.param(ALIGNED, ALIGNED, id="already_aligned"),
        # A single long gap is not enough evidence to realign.
        pytest.param(
            bytes.fromhex("b1c00900b09e0600350d00019a280c"),
            bytes.fromhex("b1c00900b09e0600350d00019a280c"),
            id="single_gap",
        ),
        pytest.param(
            bytes.fromhex("b1c00c00b09e06000d00019a2800019a0c"),
            bytes.fromhex("b1c00c00b09e06000d00019a2800019a0c"),
            id="gaps_on_both_slot_types",
        ),
        pytest.param(
            bytes.fromhex("b1c00600b09e06000001"),
            bytes.fromhex("b1c00600b09e06000001"),
            id="truncated_extended_timing",
        ),
        pytest.param(b"\x26" + MISALIGNED[1:], b"\x26" + MISALIGNED[1:], id="ir"),
        pytest.param(b"\xb2" + MISALIGNED[1:], b"\xb2" + MISALIGNED[1:], id="0xb2"),
        pytest.param(b"\xb1\xc0", b"\xb1\xc0", id="header_only"),
    ],
)
def test_realign_rf_packet(packet: bytes, expected: bytes) -> None:
    assert realign_rf_packet(packet) == expected


def test_realign_is_idempotent() -> None:
    assert realign_rf_packet(realign_rf_packet(MISALIGNED)) == ALIGNED


def test_rf_capture_returns_the_realigned_packet() -> None:
    """A shifted capture comes out of an RF capture window aligned."""
    device, fake = make()

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, MISALIGNED, 2 * UNIT))
        return [s async for s in device.capture_rf(window=1, frequency=433.92, **FAST)]

    signals = run(go())
    assert [s.packet for s in signals] == [ALIGNED]


def test_check_data_leaves_other_packets_alone() -> None:
    """IR and canonical RF packets pass through check_data untouched."""
    device, fake = make()
    rf = pulses_to_data([300, 9000, 300, 900, 900, 300], kind=SignalKind.RF_433)

    async def go():
        await device.find_rf_packet(433.92)
        fake.press(rf)
        return await device.check_data()

    assert run(go()) == rf
