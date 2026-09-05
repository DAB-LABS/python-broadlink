"""Capture windows and packet helpers, against a scripted universal remote.

The fake replaces ``send_packet`` on a real device instance, decodes the
command the class framed, and behaves like an RM4 Pro as measured on the
bench: it holds one code per learning session, answers ``check_data`` with
``StorageError`` -5 until a code lands, and drops presses while not armed.
"""

from __future__ import annotations

import asyncio
import struct
from contextlib import aclosing

import pytest

import broadlink
from broadlink import exceptions as e
from broadlink.remote import (
    CapturedSignal,
    SignalKind,
    data_to_pulses,
    parse_packet,
    pulses_to_data,
)
from tests.oracle.harness import HOST, MAC, make_response

IR = pulses_to_data([9000, 4500, 560, 560, 560, 1690])
RF = pulses_to_data([300, 900, 300, 900], kind=SignalKind.RF_433)

CMD_SEND = 0x02
CMD_LEARN = 0x03
CMD_CHECK = 0x04
CMD_SWEEP = 0x19
CMD_CHECK_FREQ = 0x1A
CMD_FIND_RF = 0x1B
CMD_CANCEL_SWEEP = 0x1E


class FakeRM:
    """A scripted RM: one receiver, one code per arm, silent expiry."""

    def __init__(self, device: broadlink.Device, framing: str) -> None:
        self.device = device
        self.framing = framing  # "rmmini" (<I) or "rmminib" (<HI)
        self.commands: list[tuple[int, bytes]] = []
        self.armed = False
        self.rf_frequency: float | None = None
        self.pending: bytes | None = None
        self.sweeping = False
        self.sweep_answers: list[tuple[bool, float]] = []
        self.timeouts_to_raise = 0
        self.nothing_yet_code = -5  # -10 on some older firmware
        device.send_packet = self.send_packet  # type: ignore[method-assign]

    # -- what the test does to the device
    def press(self, packet: bytes) -> bool:
        """A remote is pressed at the device. Captured only while armed."""
        if not self.armed:
            return False
        self.pending = packet
        self.armed = False  # One code per learning session.
        return True

    def expire(self) -> None:
        """The device leaves learning mode without telling anyone."""
        self.armed = False

    # -- fake transport
    async def send_packet(self, packet_type: int, payload: bytes) -> bytes:
        assert packet_type == 0x6A
        if self.framing == "rmmini":
            command = struct.unpack("<I", payload[:4])[0]
            data = payload[4:]
        else:
            command = struct.unpack("<I", payload[2:6])[0]
            data = payload[6:]
        self.commands.append((command, bytes(data)))
        body, error = self.handle(command, data)
        if error == "timeout":
            raise e.NetworkTimeoutError(-4000, "No response received")
        if self.framing == "rmmini":
            resp = b"\x01\x00\x00\x00" + body
        else:
            resp = struct.pack("<H", len(body) + 4) + b"\x00\x00\x00\x00" + body
        return make_response(self.device, resp, error)

    def handle(self, command: int, data: bytes) -> tuple[bytes, int | str]:
        if command == CMD_LEARN:
            self.armed = True
            self.rf_frequency = None
            return b"", 0
        if command == CMD_FIND_RF:
            self.armed = True
            self.rf_frequency = struct.unpack("<I", data[:4])[0] / 1000 if data else None
            return b"", 0
        if command == CMD_CHECK:
            if self.timeouts_to_raise:
                self.timeouts_to_raise -= 1
                return b"", "timeout"
            if self.pending is None:
                return b"", self.nothing_yet_code
            code, self.pending = self.pending, None
            return code, 0
        if command == CMD_SEND:
            return b"", 0
        if command == CMD_SWEEP:
            self.sweeping = True
            return b"", 0
        if command == CMD_CANCEL_SWEEP:
            self.sweeping = False
            return b"", 0
        if command == CMD_CHECK_FREQ:
            found, freq = (
                self.sweep_answers.pop(0) if self.sweep_answers else (False, 0.0)
            )
            return bytes([found]) + struct.pack("<I", int(freq * 1000)), 0
        raise AssertionError(f"unexpected command 0x{command:02x}")

    def count(self, command: int) -> int:
        return sum(1 for c, _ in self.commands if c == command)


def make(
    cls_name: str = "rm4pro", devtype: int = 0x649B
) -> tuple[broadlink.Device, FakeRM]:
    cls = getattr(broadlink, cls_name)
    device = cls(HOST, MAC, devtype, name="Bench", model="Test", manufacturer="Test")
    framing = "rmmini" if cls_name in {"rmmini", "rmpro", "rm"} else "rmminib"
    return device, FakeRM(device, framing)


def run(coro):
    return asyncio.run(coro)


async def press_later(fake: FakeRM, packet: bytes, delay: float) -> bool:
    await asyncio.sleep(delay)
    return fake.press(packet)


# Every delay below is a multiple of this unit. The windows and presses are
# tens of milliseconds apart, which is plenty on a laptop but tight on a
# loaded CI runner, so the unit is deliberately generous.
UNIT = 0.03

FAST = dict(poll_interval=UNIT, rearm_interval=10.0)


# ------------------------------------------------------------- IR windows


@pytest.mark.parametrize(
    "cls_name,devtype",
    [("rm4pro", 0x649B), ("rmpro", 0x272A), ("rm4mini", 0x51DA), ("rm5plus", 0x5224)],
)
def test_capture_yields_first_signal_and_closes(cls_name, devtype):
    device, fake = make(cls_name, devtype)

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, IR, 3 * UNIT))
        signals = [s async for s in device.capture(window=2, **FAST)]
        return signals

    signals = run(go())
    assert len(signals) == 1
    sig = signals[0]
    assert isinstance(sig, CapturedSignal)
    assert sig.packet == IR
    assert sig.kind is SignalKind.IR
    assert sig.pulses == data_to_pulses(IR)
    assert sig.frequency_mhz is None
    assert fake.commands[0][0] == CMD_LEARN
    assert fake.count(CMD_LEARN) == 1
    assert fake.count(CMD_CHECK) >= 2
    assert device.capture_active is False


def test_capture_window_elapses_with_nothing():
    device, fake = make()
    signals = run(_collect(device.capture(window=5 * UNIT, **FAST)))
    assert signals == []
    assert fake.count(CMD_LEARN) == 1
    assert fake.count(CMD_CHECK) >= 3
    assert device.capture_active is False


async def _collect(gen):
    return [s async for s in gen]


def test_capture_keeps_going_and_rearms_after_each_code():
    device, fake = make()

    async def go():
        loop = asyncio.get_running_loop()
        loop.create_task(press_later(fake, IR, 2 * UNIT))
        loop.create_task(press_later(fake, RF, 6 * UNIT))
        return [
            s
            async for s in device.capture(
                window=12 * UNIT, stop_after_first=False, **FAST
            )
        ]

    signals = run(go())
    assert [s.packet for s in signals] == [IR, RF]
    # Armed once at the start and once after each code.
    assert fake.count(CMD_LEARN) == 3


def test_press_between_code_and_rearm_is_lost_but_next_is_not():
    """The device holds one code per session; a second press before the
    window re-arms is gone, as measured on the bench."""
    device, fake = make()

    async def go():
        loop = asyncio.get_running_loop()
        results = []

        async def presses():
            await asyncio.sleep(2 * UNIT)
            results.append(fake.press(IR))
            results.append(fake.press(RF))  # Device not armed: lost.
            await asyncio.sleep(3 * UNIT)
            results.append(fake.press(RF))  # Re-armed by then.

        loop.create_task(presses())
        signals = [
            s
            async for s in device.capture(
                window=10 * UNIT, stop_after_first=False, **FAST
            )
        ]
        return results, signals

    results, signals = run(go())
    assert results == [True, False, True]
    assert [s.packet for s in signals] == [IR, RF]


def test_send_during_window_rearms():
    device, fake = make()

    async def go():
        async def send_then_press():
            await asyncio.sleep(2 * UNIT)
            await device.send_data(IR)
            fake.expire()  # Whatever the send did to the session, assume the worst.
            await asyncio.sleep(3 * UNIT)
            return fake.press(RF)

        loop = asyncio.get_running_loop()
        task = loop.create_task(send_then_press())
        signals = [s async for s in device.capture(window=20 * UNIT, **FAST)]
        return await task, signals

    pressed, signals = run(go())
    assert pressed is True
    assert [s.packet for s in signals] == [RF]
    assert fake.count(CMD_SEND) == 1
    assert fake.count(CMD_LEARN) == 2
    learn_positions = [i for i, (c, _) in enumerate(fake.commands) if c == CMD_LEARN]
    send_position = next(i for i, (c, _) in enumerate(fake.commands) if c == CMD_SEND)
    assert learn_positions[0] < send_position < learn_positions[1]


def test_timed_rearm_recovers_from_silent_expiry():
    device, fake = make()

    async def go():
        async def expire_then_press():
            await asyncio.sleep(2 * UNIT)
            fake.expire()
            assert fake.press(IR) is False  # Lost: the device is deaf.
            await asyncio.sleep(5 * UNIT)  # Past the re-arm interval.
            return fake.press(IR)

        loop = asyncio.get_running_loop()
        task = loop.create_task(expire_then_press())
        signals = [
            s
            async for s in device.capture(
                window=30 * UNIT, poll_interval=1 * UNIT, rearm_interval=4 * UNIT
            )
        ]
        return await task, signals

    pressed, signals = run(go())
    assert pressed is True
    assert len(signals) == 1
    assert fake.count(CMD_LEARN) >= 2


def test_open_ended_window_runs_until_closed():
    device, fake = make()

    async def go():
        got = []
        async with aclosing(
            device.capture(window=0, stop_after_first=False, **FAST)
        ) as gen:
            asyncio.get_running_loop().create_task(press_later(fake, IR, 2 * UNIT))
            async for s in gen:
                got.append(s)
                if len(got) == 1:
                    break
        return got

    got = run(go())
    assert len(got) == 1
    assert device.capture_active is False
    # Closing sends nothing further to the device.
    assert fake.commands[-1][0] in (CMD_CHECK, CMD_LEARN)


def test_second_window_is_refused():
    device, _fake = make()

    async def go():
        task = asyncio.get_running_loop().create_task(
            _collect(device.capture(window=1, **FAST))
        )
        await asyncio.sleep(2 * UNIT)
        with pytest.raises(e.CaptureInProgressError):
            await _collect(device.capture(window=1, **FAST))
        assert device.capture_active is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert device.capture_active is False


def test_dropped_window_does_not_block_the_next_one():
    """Breaking out of ``async for`` without closing the generator leaves
    it to asyncio's finalizer; the next capture() gives that a turn and
    proceeds."""
    device, fake = make()

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, IR, 2 * UNIT))
        got = None
        async for s in device.capture(window=1, stop_after_first=False, **FAST):
            got = s
            break  # No reference kept; the generator is collectable.
        asyncio.get_running_loop().create_task(press_later(fake, RF, 2 * UNIT))
        second = [s async for s in device.capture(window=1, **FAST)]
        return got, second

    got, second = run(go())
    assert got.packet == IR
    assert [s.packet for s in second] == [RF]
    assert device.capture_active is False


def test_held_window_is_not_taken_by_the_next_one():
    """A window the consumer still holds is refused to a newcomer, even if
    the consumer is not inside the generator at that instant, until the
    consumer closes it."""
    device, fake = make()

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, IR, 2 * UNIT))
        first = device.capture(window=1, stop_after_first=False, **FAST)
        async for _ in first:
            break  # Still referenced by ``first``.
        with pytest.raises(e.CaptureInProgressError):
            await _collect(device.capture(window=1, **FAST))
        assert device.capture_active is True
        # A refused attempt must not have displaced the live window.
        with pytest.raises(e.CaptureInProgressError):
            await _collect(device.capture_rf(window=1, frequency=433.92, **FAST))
        assert device.capture_active is True
        await first.aclose()
        assert device.capture_active is False
        asyncio.get_running_loop().create_task(press_later(fake, RF, 2 * UNIT))
        return [s async for s in device.capture(window=1, **FAST)]

    second = run(go())
    assert [s.packet for s in second] == [RF]


def test_paused_consumer_keeps_its_window():
    """The README's own loop awaits between signals. An intruder calling
    capture() during that pause must be refused, and the consumer must
    keep receiving."""
    device, fake = make()

    async def go():
        loop = asyncio.get_running_loop()
        got = []
        intruder = {"error": None, "signals": None}

        async def consumer():
            async with aclosing(
                device.capture(window=30 * UNIT, stop_after_first=False, **FAST)
            ) as window:
                async for s in window:
                    got.append(s)
                    await asyncio.sleep(4 * UNIT)  # Paused, not running.

        async def intrude():
            await asyncio.sleep(3 * UNIT)  # During the consumer's pause.
            try:
                intruder["signals"] = await _collect(device.capture(window=1, **FAST))
            except e.CaptureInProgressError as err:
                intruder["error"] = err

        loop.create_task(press_later(fake, IR, 2 * UNIT))
        loop.create_task(press_later(fake, RF, 12 * UNIT))
        task = loop.create_task(consumer())
        await intrude()
        await task
        return got, intruder

    got, intruder = run(go())
    assert isinstance(intruder["error"], e.CaptureInProgressError)
    assert intruder["signals"] is None
    assert [s.packet for s in got] == [IR, RF]


def test_unreferenced_unstarted_window_does_not_block():
    device, fake = make()

    async def go():
        device.capture(window=1, **FAST)  # created and dropped, never iterated
        asyncio.get_running_loop().create_task(press_later(fake, IR, 2 * UNIT))
        return [s async for s in device.capture(window=1, **FAST)]

    assert len(run(go())) == 1


def test_undecodable_packet_does_not_end_the_window():
    """A returned packet whose declared length runs into a truncated escape
    is logged and skipped; the window re-arms and the next signal lands."""
    device, fake = make()
    bad = bytes([0x26, 0x00, 0x03, 0x00, 0x10, 0x00])  # escape with no bytes after

    async def go():
        loop = asyncio.get_running_loop()
        loop.create_task(press_later(fake, bad, 2 * UNIT))
        loop.create_task(press_later(fake, IR, 6 * UNIT))
        return [s async for s in device.capture(window=1, **FAST)]

    signals = run(go())
    assert [s.packet for s in signals] == [IR]
    assert fake.count(CMD_LEARN) >= 2  # Re-armed after the bad one.


def test_older_firmware_read_error_means_nothing_yet():
    device, fake = make()
    fake.nothing_yet_code = -10  # ReadError

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, IR, 3 * UNIT))
        return [s async for s in device.capture(window=1, **FAST)]

    signals = run(go())
    assert len(signals) == 1
    assert fake.count(CMD_CHECK) >= 2


def test_capture_rf_abandoned_then_ir_window():
    device, fake = make()

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, RF, 2 * UNIT))
        async for _ in device.capture_rf(
            window=1, frequency=433.92, stop_after_first=False, **FAST
        ):
            break  # Dropped, not held.
        asyncio.get_running_loop().create_task(press_later(fake, IR, 2 * UNIT))
        return [s async for s in device.capture(window=1, **FAST)]

    signals = run(go())
    assert [s.kind for s in signals] == [SignalKind.IR]
    assert device.capture_active is False


def test_transport_timeouts_rearm_then_give_up():
    device, fake = make()
    fake.timeouts_to_raise = 2

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, IR, 5 * UNIT))
        return [s async for s in device.capture(window=1, **FAST)]

    signals = run(go())
    assert len(signals) == 1
    assert fake.count(CMD_LEARN) >= 2  # Re-armed after the timeouts.

    device, fake = make()
    fake.timeouts_to_raise = 3
    with pytest.raises(e.NetworkTimeoutError):
        run(_collect(device.capture(window=1, **FAST)))
    assert device.capture_active is False


def test_capture_rejects_bad_arguments():
    device, _ = make()
    with pytest.raises(ValueError):
        run(_collect(device.capture(window=-1)))
    with pytest.raises(ValueError):
        run(_collect(device.capture(poll_interval=0)))
    with pytest.raises(ValueError):
        run(_collect(device.capture(rearm_interval=0)))


# ------------------------------------------------------------- RF windows


def test_capture_rf_with_known_frequency_skips_the_sweep():
    device, fake = make()

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, RF, 3 * UNIT))
        return [s async for s in device.capture_rf(window=1, frequency=433.92, **FAST)]

    signals = run(go())
    assert len(signals) == 1
    sig = signals[0]
    assert sig.kind is SignalKind.RF_433
    assert sig.frequency_mhz == 433.92
    assert sig.packet == RF
    assert fake.count(CMD_SWEEP) == 0
    assert fake.commands[0] == (CMD_FIND_RF, struct.pack("<I", 433920))
    assert fake.rf_frequency == 433.92


def test_capture_rf_yields_despite_odd_type_byte():
    # The device returns 0xB1 for 433 MHz; the window armed RF, so it tags
    # the signal RF_433 from context and does not choke on the byte.
    device, fake = make()
    odd = bytes([0xB1]) + RF[1:]

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, odd, 3 * UNIT))
        return [s async for s in device.capture_rf(window=1, frequency=433.92, **FAST)]

    signals = run(go())
    assert len(signals) == 1
    assert signals[0].kind is SignalKind.RF_433
    assert signals[0].type_byte == 0xB1
    assert signals[0].frequency_mhz == 433.92


def test_capture_rf_below_400mhz_is_tagged_315():
    device, fake = make()

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, RF, 3 * UNIT))
        return [s async for s in device.capture_rf(window=1, frequency=315.0, **FAST)]

    signals = run(go())
    assert signals[0].kind is SignalKind.RF_315


def test_capture_rf_sweeps_then_learns():
    device, fake = make()
    fake.sweep_answers = [(False, 0.0), (False, 0.0), (True, 433.92)]

    async def go():
        asyncio.get_running_loop().create_task(press_later(fake, RF, 8 * UNIT))
        return [s async for s in device.capture_rf(window=1, **FAST)]

    signals = run(go())
    assert len(signals) == 1
    assert signals[0].frequency_mhz == 433.92
    kinds = [c for c, _ in fake.commands]
    assert kinds[0] == CMD_SWEEP
    assert kinds.count(CMD_CHECK_FREQ) == 3
    find = kinds.index(CMD_FIND_RF)
    assert find > kinds.index(CMD_CHECK_FREQ)
    assert fake.commands[find][1] == struct.pack("<I", 433920)
    assert fake.count(CMD_CANCEL_SWEEP) == 0


def test_capture_rf_sweep_that_never_locks_is_cancelled():
    device, fake = make()
    signals = run(_collect(device.capture_rf(window=5 * UNIT, **FAST)))
    assert signals == []
    assert fake.count(CMD_SWEEP) == 1
    assert fake.count(CMD_CANCEL_SWEEP) == 1
    assert fake.count(CMD_FIND_RF) == 0
    assert fake.sweeping is False
    assert device.capture_active is False


def test_send_during_sweep_restarts_it():
    device, fake = make()
    fake.sweep_answers = [(False, 0.0)] * 10 + [(True, 315.0)]

    async def go():
        async def send():
            await asyncio.sleep(2 * UNIT)
            await device.send_data(IR)

        loop = asyncio.get_running_loop()
        loop.create_task(send())
        loop.create_task(press_later(fake, RF, 20 * UNIT))
        return [s async for s in device.capture_rf(window=1, **FAST)]

    signals = run(go())
    assert len(signals) == 1
    assert signals[0].frequency_mhz == 315.0
    assert fake.count(CMD_SWEEP) == 2


def test_capture_rf_refused_while_ir_window_open():
    device, _fake = make()

    async def go():
        task = asyncio.get_running_loop().create_task(
            _collect(device.capture(window=1, **FAST))
        )
        await asyncio.sleep(2 * UNIT)
        with pytest.raises(e.CaptureInProgressError):
            await _collect(device.capture_rf(window=1, frequency=433.92, **FAST))
        with pytest.raises(e.CaptureInProgressError):
            await _collect(device.capture_rf(window=1, **FAST))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert device.capture_active is False


def test_rf_capture_is_only_on_pro_classes():
    assert hasattr(broadlink.rmpro, "capture_rf")
    assert hasattr(broadlink.rm4pro, "capture_rf")
    assert not hasattr(broadlink.rm4mini, "capture_rf")
    assert not hasattr(broadlink.rmmini, "capture_rf")
    assert not hasattr(broadlink.rm5plus, "capture_rf")


# --------------------------------------------------------- packet helpers


def test_pulses_to_data_writes_kind_and_repeat():
    data = pulses_to_data([1000, 2000], kind=SignalKind.RF_315, repeat=3)
    assert data[0] == 0xD7
    assert data[1] == 3
    assert isinstance(data, bytes)
    assert pulses_to_data([1000])[:2] == bytes([0x26, 0])


def test_pulses_to_data_rejects_bad_repeat():
    with pytest.raises(ValueError):
        pulses_to_data([1000], repeat=256)
    with pytest.raises(ValueError):
        pulses_to_data([1000], repeat=-1)


def test_parse_packet_round_trip():
    pulses = [9000, 4500, 560, 1690, 40000]
    for kind in SignalKind:
        packet = pulses_to_data(pulses, kind=kind, repeat=1)
        parsed = parse_packet(packet)
        assert parsed.kind is kind
        assert parsed.repeat == 1
        assert parsed.pulses == data_to_pulses(packet)
        for a, b in zip(pulses, parsed.pulses, strict=True):
            assert abs(a - b) <= 16


def test_parse_packet_rejects_unknown_type_and_short_data():
    with pytest.raises(ValueError):
        parse_packet(bytes([0x99, 0, 1, 0, 10]))
    with pytest.raises(ValueError):
        parse_packet(bytes([0x26, 0]))


def test_classify_tolerates_the_rf_bytes_the_device_sends():
    # The RM4 Pro returns 0xB1 for a 433 MHz capture, not the 0xB2 the
    # library writes; both must classify as RF_433 (bench, 2026-09-04).
    assert SignalKind.classify(0xB2) is SignalKind.RF_433
    assert SignalKind.classify(0xB1) is SignalKind.RF_433
    assert SignalKind.classify(0xD7) is SignalKind.RF_315
    assert SignalKind.classify(0xD1) is SignalKind.RF_315
    assert SignalKind.classify(0x26) is SignalKind.IR
    with pytest.raises(ValueError):
        SignalKind.classify(0x99)


def test_parse_packet_keeps_raw_type_byte():
    packet = bytes([0xB1, 0, 2, 0, 10, 20])
    parsed = parse_packet(packet)
    assert parsed.kind is SignalKind.RF_433
    assert parsed.type_byte == 0xB1


def test_from_packet_kind_override_never_drops_a_signal():
    # A capture window passes the band it armed; an unexpected type byte
    # must not raise, and the raw byte is preserved.
    weird = bytes([0xB1, 0, 2, 0, 10, 20])
    sig = CapturedSignal.from_packet(weird, 433.92, kind=SignalKind.RF_433)
    assert sig.kind is SignalKind.RF_433
    assert sig.type_byte == 0xB1
    assert sig.frequency_mhz == 433.92
    # Truly unknown byte with no hint still raises.
    with pytest.raises(ValueError):
        CapturedSignal.from_packet(bytes([0x99, 0, 1, 0, 10]))


def test_signal_kind_flags():
    assert SignalKind.IR.is_rf is False
    assert SignalKind.RF_433.is_rf is True
    assert SignalKind.RF_315.is_rf is True
    assert SignalKind(0x26) is SignalKind.IR


def test_captured_signal_from_packet():
    sig = CapturedSignal.from_packet(RF, 433.92)
    assert sig.kind is SignalKind.RF_433
    assert sig.repeat == 0
    assert sig.pulses == data_to_pulses(RF)
    assert sig.frequency_mhz == 433.92
    assert sig.captured_at > 0
