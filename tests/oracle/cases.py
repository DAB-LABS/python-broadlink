"""The oracle cases: one entry per public method per device class.

Each case names a device class and product id, a method with arguments, and
the canned response payloads (plaintext, before encryption) the fake device
answers with, in order. ``record.py`` runs them against the library and
freezes the outcome in ``fixtures.json``; ``test_oracle.py`` replays them
and compares.

Canned payloads are shaped for each class's decoder so the method exercises
its full parse path. Comments say which bytes each decoder reads.
"""

from __future__ import annotations

import json
import struct

from broadlink.helpers import CRC16


def hexb(*parts: bytes | bytearray) -> str:
    return b"".join(bytes(p) for p in parts).hex()


def b(value: bytes | bytearray) -> dict:
    """Wrap bytes for JSON storage."""
    return {"__bytes__": bytes(value).hex()}


# ---------------------------------------------------------------- payload builders


def rmmini_payload(body: bytes) -> str:
    """rmmini._send returns payload[4:]; the first four bytes echo the command."""
    return hexb(b"\x01\x00\x00\x00", body)


def rmminib_payload(body: bytes) -> str:
    """rmminib._send reads p_len at [0:2] and returns payload[6:p_len+2]."""
    p_len = len(body) + 4
    return hexb(struct.pack("<H", p_len), b"\x00\x00\x00\x00", body)


def hysen_payload(body: bytes) -> str:
    """hysen.send_request: [len][body][crc16(body)]; returns body."""
    p_len = len(body) + 2
    return hexb(struct.pack("<H", p_len), body, CRC16.calculate(body).to_bytes(2, "little"))


def hvac_payload(data: bytes) -> str:
    """hvac._decode: [len][bb 00 07 00 00 00][d_len][data][crc16 poly 0x9BE4]."""
    p_len = 10 + len(data)
    head = struct.pack("<H", p_len) + bytes([0xBB, 0x00, 0x07, 0x00, 0x00, 0x00])
    head += struct.pack("<H", len(data))
    body = head + data
    crc = CRC16.calculate(body[0x02:p_len], polynomial=0x9BE4)
    return hexb(body, crc.to_bytes(2, "little"))


def json12_payload(state: dict) -> str:
    """12-byte header: js_len at 0x08, JSON at 0x0C (sp4, lb2, s3)."""
    data = json.dumps(state, separators=(",", ":")).encode()
    head = struct.pack("<HHHBBI", 0xA5A5, 0x5A5A, 0, 1, 0x0B, len(data))
    return hexb(head, data)


def json14_payload(state: dict) -> str:
    """14-byte header: js_len at 0x0A, JSON at 0x0E (sp4b, bg1, lb1)."""
    data = json.dumps(state, separators=(",", ":")).encode()
    head = struct.pack("<HHHHBBI", 12 + len(data), 0xA5A5, 0x5A5A, 0, 1, 0x0B, len(data))
    return hexb(head, data)


def a1_payload(temp=(23, 5), hum=(45, 2), light=2, air=1, noise=0) -> str:
    p = bytearray(0x10)
    p[0x04], p[0x05] = temp
    p[0x06], p[0x07] = hum
    p[0x08] = light
    p[0x0A] = air
    p[0x0C] = noise
    return p.hex()


def a2_payload() -> str:
    p = bytearray(0x18)
    p[0x0D:0x0F] = (12).to_bytes(2, "big")  # pm10
    p[0x0F:0x11] = (7).to_bytes(2, "big")  # pm2_5
    p[0x11:0x13] = (3).to_bytes(2, "big")  # pm1
    p[0x13:0x15] = (235).to_bytes(2, "big")  # temperature
    p[0x15:0x17] = (452).to_bytes(2, "big")  # humidity
    return p.hex()


def mp1s_payload() -> str:
    """mp1s.get_state slices payload.hex()[4:-6] and reads BCD digit pairs."""
    digits = "".join(str(i % 10) for i in range(54))
    return "0000" + digits + "000000"


def s1c_payload() -> str:
    def sensor(status, order, stype, name, serial):
        s = bytearray(83)
        s[0] = status
        s[1] = order
        s[3] = stype
        s[4 : 4 + len(name)] = name.encode()
        s[26:30] = serial
        return bytes(s)

    p = bytearray(6)
    p[4] = 2
    return hexb(
        p,
        sensor(1, 1, 0x31, "Front door", b"\x01\x02\x03\x04"),
        sensor(0, 2, 0x21, "Hall", b"\x0a\x0b\x0c\x0d"),
        sensor(0, 3, 0x91, "", b"\x00\x00\x00\x00"),  # empty serial: filtered out
    )


def hysen_status_body() -> bytes:
    body = bytearray(48)
    body[3] = 0x01  # remote_lock
    body[4] = 0b1101_0001  # heating_cooling=1, temp_manual=1, active=1, offset add=0, power=1
    body[5] = 43  # room temp 21.5
    body[6] = 44  # thermostat temp 22.0
    body[7] = 0x21  # loop_mode 2, auto_mode 1
    body[8] = 0  # sensor
    body[9] = 42  # osv
    body[10] = 2  # dif
    body[11] = 35  # svh
    body[12] = 5  # svl
    body[13:15] = (-5).to_bytes(2, "big", signed=True)  # room_temp_adj -0.5
    body[15] = 0  # fre
    body[16] = 1  # poweron
    body[17] = 0x20  # unknown (offset raw 2)
    body[18] = 50  # external temp 25.0
    body[19], body[20], body[21], body[22] = 14, 30, 5, 3
    for i in range(8):
        body[2 * i + 23] = 6 + i
        body[2 * i + 24] = 15
        body[i + 39] = 40 + i
    return bytes(body)


def hvac_state_data() -> bytes:
    data = bytearray(2 + 13)
    s = memoryview(data)[2:]
    s[0x00] = (int(24) - 8 << 3) | 2  # target 24, swing_v POS2
    s[0x01] = (7 << 5) | 0b100  # swing_h OFF
    s[0x03] = 2 << 5  # speed MID
    s[0x04] = 1 << 6  # preset TURBO (bits 6-7; bit 7 doubles as the half degree)
    s[0x05] = (1 << 5) | (1 << 2)  # mode COOL, sleep
    s[0x08] = (1 << 5) | (1 << 2) | 0b11  # power, clean, health
    s[0x0A] = (1 << 4)  # display
    return bytes(data)


def hvac_info_data() -> bytes:
    data = bytearray(2 + 22)
    s = memoryview(data)[2:]
    s[0x01] = 1
    s[0x05] = 26
    s[0x15] = 5
    return bytes(data)


def fw_payload(version: int) -> str:
    p = bytearray(8)
    p[4:6] = version.to_bytes(2, "little")
    return p.hex()


def rm_update_payload(name: str, locked: bool) -> str:
    body = bytearray(0x88)
    body[0x48 : 0x48 + len(name)] = name.encode()
    body[0x87] = int(locked)
    return rmmini_payload(bytes(body))


def rmminib_update_payload(name: str, locked: bool) -> str:
    body = bytearray(0x88)
    body[0x48 : 0x48 + len(name)] = name.encode()
    body[0x87] = int(locked)
    return rmminib_payload(bytes(body))


IR_CODE = bytes.fromhex("2600180012341234123412340d05")
EMPTY = "00" * 16

# ------------------------------------------------------------------------ cases


def case(cls, devtype, method, *args, responses=(), attrs=(), setup=None, **kwargs):
    entry = {
        "cls": cls,
        "devtype": devtype,
        "method": method,
        "args": list(args),
        "kwargs": kwargs,
        "responses": list(responses),
    }
    if attrs:
        entry["attrs"] = list(attrs)
    if setup:
        entry["setup"] = setup
    return entry


def all_cases() -> list[dict]:
    cases: list[dict] = []
    add = cases.append

    # Device base -------------------------------------------------------
    add(case("Device", 0x0000, "get_fwversion", responses=[fw_payload(0x1234)]))
    add(case("Device", 0x0000, "set_name", "Living room", responses=[EMPTY], attrs=["name"]))
    add(case("Device", 0x0000, "set_lock", True, responses=[EMPTY], attrs=["is_locked"]))
    add(case("Device", 0x0000, "set_lock", False, responses=[EMPTY], attrs=["is_locked"],
             setup={"name": "Kitchen"}))
    add(case("Device", 0x0000, "get_type"))

    # RM family ---------------------------------------------------------
    for cls, devtype, payload in (("rmmini", 0x2737, rmmini_payload),
                                  ("rmpro", 0x272A, rmmini_payload),
                                  ("rmminib", 0x5F36, rmminib_payload),
                                  ("rm4mini", 0x51DA, rmminib_payload),
                                  ("rm4pro", 0x6026, rmminib_payload),
                                  ("rm", 0x2712, rmmini_payload),
                                  ("rm4", 0x62BE, rmminib_payload)):
        add(case(cls, devtype, "send_data", b(IR_CODE), responses=[payload(b"")]))
        add(case(cls, devtype, "enter_learning", responses=[payload(b"")]))
        add(case(cls, devtype, "check_data", responses=[payload(IR_CODE)]))
        upd = rmminib_update_payload if payload is rmminib_payload else rm_update_payload
        add(case(cls, devtype, "update", responses=[upd("Bedroom RM", True)],
                 attrs=["name", "is_locked"]))

    for cls, devtype in (("rmpro", 0x272A), ("rm", 0x2712)):
        add(case(cls, devtype, "check_sensors", responses=[rmmini_payload(bytes([23, 4]))]))
        add(case(cls, devtype, "check_temperature", responses=[rmmini_payload(bytes([23, 4]))]))

    for cls, devtype in (("rm4mini", 0x51DA), ("rm4pro", 0x6026), ("rm4", 0x62BE)):
        body = bytes([24, 35, 51, 20])
        add(case(cls, devtype, "check_sensors", responses=[rmminib_payload(body)]))
        add(case(cls, devtype, "check_temperature", responses=[rmminib_payload(body)]))
        add(case(cls, devtype, "check_humidity", responses=[rmminib_payload(body)]))

    for cls, devtype, payload in (("rmpro", 0x272A, rmmini_payload),
                                  ("rm4pro", 0x6026, rmminib_payload),
                                  ("rm", 0x2712, rmmini_payload),
                                  ("rm4", 0x62BE, rmminib_payload)):
        add(case(cls, devtype, "sweep_frequency", responses=[payload(b"")]))
        found = bytes([1]) + struct.pack("<I", 433920)
        add(case(cls, devtype, "check_frequency", responses=[payload(found)]))
        add(case(cls, devtype, "check_frequency",
                 responses=[payload(bytes([0]) + struct.pack("<I", 0))]))
        add(case(cls, devtype, "find_rf_packet", responses=[payload(b"")]))
        add(case(cls, devtype, "find_rf_packet", 433.92, responses=[payload(b"")]))
        add(case(cls, devtype, "cancel_sweep_frequency", responses=[payload(b"")]))

    # Switches ----------------------------------------------------------
    add(case("sp1", 0x0000, "set_power", True, responses=[EMPTY]))
    add(case("sp1", 0x0000, "set_power", False, responses=[EMPTY]))
    on = "00000000" + "01" + "00" * 11
    off = "00" * 16
    for cls, devtype in (("sp2", 0x2711), ("sp3s", 0x947A)):
        add(case(cls, devtype, "set_power", True, responses=[EMPTY]))
        add(case(cls, devtype, "check_power", responses=[on]))
        add(case(cls, devtype, "check_power", responses=[off]))
    add(case("sp2s", 0x2728, "get_energy",
             responses=["00000000" + (1234).to_bytes(3, "little").hex() + "00" * 9]))
    add(case("sp3s", 0x947A, "get_energy",
             responses=["0000000000" + "341200" + "00" * 8]))  # bytes 5..7 = 34 12 00
    nl_on = "00000000" + "03" + "00" * 11  # power bit0, nightlight bit1
    add(case("sp3", 0x753E, "set_power", True, responses=[nl_on, EMPTY]))
    add(case("sp3", 0x753E, "set_nightlight", True, responses=[on, EMPTY]))
    add(case("sp3", 0x753E, "check_power", responses=[nl_on]))
    add(case("sp3", 0x753E, "check_nightlight", responses=[nl_on]))

    sp4_state = {"pwr": 1, "ntlight": 0, "indicator": 1, "ntlbrightness": 50,
                 "maxworktime": 0, "childlock": 0}
    add(case("sp4", 0x7579, "get_state", responses=[json12_payload(sp4_state)]))
    add(case("sp4", 0x7579, "set_power", True, responses=[json12_payload(sp4_state)]))
    add(case("sp4", 0x7579, "set_nightlight", False, responses=[json12_payload(sp4_state)]))
    add(case("sp4", 0x7579, "set_state", pwr=True, ntlbrightness=25, childlock=True,
             responses=[json12_payload(sp4_state)]))
    add(case("sp4", 0x7579, "check_power", responses=[json12_payload(sp4_state)]))
    add(case("sp4", 0x7579, "check_nightlight", responses=[json12_payload(sp4_state)]))
    sp4b_state = dict(sp4_state, current=120, volt=230500, power=27600,
                      totalconsum=-1, overload=0)
    add(case("sp4b", 0x5115, "get_state", responses=[json14_payload(sp4b_state)]))
    add(case("sp4b", 0x5115, "set_state", pwr=False, responses=[json14_payload(sp4b_state)]))
    add(case("sp4b", 0x5115, "check_power", responses=[json14_payload(sp4b_state)]))

    bg_state = {"pwr": 1, "pwr1": 1, "pwr2": 0, "maxworktime": 60, "maxworktime1": 60,
                "maxworktime2": 0, "idcbrightness": 50}
    add(case("bg1", 0x51E3, "get_state", responses=[json14_payload(bg_state)]))
    add(case("bg1", 0x51E3, "set_state", pwr1=True, maxworktime2=15,
             responses=[json14_payload(bg_state)]))
    add(case("ehc31", 0x6480, "get_state", responses=[json14_payload(bg_state)]))
    add(case("ehc31", 0x6480, "set_state", pwr3=True, childlock=True, childlock4=False,
             responses=[json14_payload(bg_state)]))

    add(case("mp1", 0x4EB5, "set_power_mask", 0b0101, True, responses=[EMPTY]))
    add(case("mp1", 0x4EB5, "set_power", 1, True, responses=[EMPTY]))
    add(case("mp1", 0x4EB5, "set_power", 3, False, responses=[EMPTY]))
    raw = "00" * 14 + "0b" + "00"  # payload[0x0E] = 0b1011
    add(case("mp1", 0x4EB5, "check_power_raw", responses=[raw]))
    add(case("mp1", 0x4EB5, "check_power", responses=[raw]))
    add(case("mp1s", 0x4F1B, "get_state", responses=[mp1s_payload()]))
    add(case("mp1s", 0x4F1B, "check_power", responses=[raw]))

    # Sensors -----------------------------------------------------------
    add(case("a1", 0x2714, "check_sensors", responses=[a1_payload()]))
    add(case("a1", 0x2714, "check_sensors", responses=[a1_payload(light=9, air=9, noise=9)]))
    add(case("a1", 0x2714, "check_sensors_raw", responses=[a1_payload()]))
    add(case("a2", 0x4F60, "check_sensors_raw", responses=[a2_payload()]))

    # Lights ------------------------------------------------------------
    lb_state = {"red": 128, "blue": 255, "green": 128, "pwr": 1, "brightness": 75,
                "colortemp": 2700, "hue": 240, "saturation": 50,
                "transitionduration": 1500, "maxworktime": 0, "bulb_colormode": 1,
                "bulb_scenes": "[]", "bulb_scene": "", "bulb_sceneidx": 255}
    add(case("lb1", 0x60C7, "get_state", responses=[json14_payload(lb_state)]))
    add(case("lb1", 0x60C7, "set_state", pwr=True, brightness=50, bulb_colormode=1,
             bulb_scene="", responses=[json14_payload(lb_state)]))
    add(case("lb2", 0xA4F4, "get_state", responses=[json12_payload(lb_state)]))
    add(case("lb2", 0xA4F4, "set_state", pwr=False, red=1, green=2, blue=3,
             transitionduration=200, responses=[json12_payload(lb_state)]))

    # Climate -----------------------------------------------------------
    status = hysen_payload(hysen_status_body())
    ack = hysen_payload(bytes([0x01, 0x06, 0x00, 0x02, 0x21, 0x00]))
    add(case("hysen", 0x4EAD, "send_request", [0x01, 0x03, 0x00, 0x00, 0x00, 0x08],
             responses=[status]))
    add(case("hysen", 0x4EAD, "get_temp", responses=[status]))
    add(case("hysen", 0x4EAD, "get_external_temp", responses=[status]))
    add(case("hysen", 0x4EAD, "get_full_status", responses=[status]))
    add(case("hysen", 0x4EAD, "set_mode", 1, 2, responses=[ack]))
    add(case("hysen", 0x4EAD, "set_mode", 0, 0, 1, responses=[ack]))
    add(case("hysen", 0x4EAD, "set_advanced", 0, 0, 42, 2, 35, 5, -0.5, 0, 1,
             responses=[ack]))
    add(case("hysen", 0x4EAD, "switch_to_auto", responses=[ack]))
    add(case("hysen", 0x4EAD, "switch_to_manual", responses=[ack]))
    add(case("hysen", 0x4EAD, "set_temp", 21.5, responses=[ack]))
    add(case("hysen", 0x4EAD, "set_power", 1, 0, 1, responses=[ack]))
    add(case("hysen", 0x4EAD, "set_time", 14, 30, 5, 3, responses=[ack]))
    sched_wd = [{"start_hour": 6 + i, "start_minute": 15, "temp": 20 + i} for i in range(6)]
    sched_we = [{"start_hour": 8, "start_minute": 0, "temp": 21},
                {"start_hour": 22, "start_minute": 30, "temp": 17.5}]
    add(case("hysen", 0x4EAD, "set_schedule", sched_wd, sched_we, responses=[ack]))
    # A corrupted CRC must be rejected.
    bad = bytearray.fromhex(status)
    bad[-1] ^= 0xFF
    add(case("hysen", 0x4EAD, "get_temp", responses=[bad.hex()]))

    add(case("hvac", 0x4E2A, "get_state", responses=[hvac_payload(hvac_state_data())]))
    add(case("hvac", 0x4E2A, "get_ac_info", responses=[hvac_payload(hvac_info_data())]))
    add(case("hvac", 0x4E2A, "get_state", responses=[hvac_payload(b"\x00\x00\x01")]))
    add(case("hvac", 0x4E2A, "set_state", True, 22.5, 1, 2, 0, 7, 0, False, False, True,
             False, False, False, responses=[hvac_payload(hvac_state_data())]))
    add(case("hvac", 0x4E2A, "set_state", True, 24, 4, 3, 2, 0, 0, False, False, True,
             False, False, False, responses=[hvac_payload(hvac_state_data())]))
    add(case("hvac", 0x4E2A, "set_state", True, 24, 2, 1, 1, 0, 0, False, False, True,
             False, False, False, responses=[hvac_payload(hvac_state_data())]))

    # Covers ------------------------------------------------------------
    pos = "00000000" + "32" + "00" * 11  # payload[4] = 50
    for m in ("open", "close", "stop", "get_percentage"):
        add(case("dooya", 0x4E4D, m, responses=[pos]))
    add(case("dooya", 0x4E4D, "set_percentage_and_wait", 50, responses=[pos, pos]))
    pos2 = "00" * 0x11 + "28" + "00" * 6  # resp[0x11] = 40
    for m in ("open", "close", "stop", "get_percentage"):
        add(case("dooya2", 0x4F6E, m, responses=[pos2]))
    add(case("dooya2", 0x4F6E, "set_percentage", 40, responses=[pos2]))
    posw = "00" * 0x0E + "1e" + "00"  # resp[0x0E] = 30
    for m in ("get_position", "open", "close", "stop"):
        add(case("wser", 0x4F6E, m, responses=[posw]))
    add(case("wser", 0x4F6E, "set_position", 30, responses=[posw]))

    # Hub and alarm -----------------------------------------------------
    subs1 = {"total": 3, "list": [{"did": "a1", "pwr1": 1}, {"did": "a2", "pwr1": 0}]}
    subs2 = {"total": 3, "list": [{"did": "a2", "pwr1": 0}, {"did": "a3", "pwr1": 1}]}
    add(case("s3", 0xA59C, "get_subdevices", 2,
             responses=[json12_payload(subs1), json12_payload(subs2)]))
    add(case("s3", 0xA59C, "get_state", responses=[json12_payload({"pwr1": 1})]))
    add(case("s3", 0xA59C, "get_state", "a1", responses=[json12_payload({"pwr1": 1})]))
    add(case("s3", 0xA59C, "set_state", "a1", True, None, False,
             responses=[json12_payload({"pwr1": 1, "pwr3": 0})]))
    add(case("S1C", 0x2722, "get_sensors_status", responses=[s1c_payload()]))

    # Error path shared by every class: a non-zero device error code.
    add(case("rm4mini", 0x51DA, "check_data", responses=[]))  # no response canned
    return cases


def error_cases() -> list[dict]:
    """Cases whose canned response carries a device error code."""
    return [
        {"cls": "rmmini", "devtype": 0x2737, "method": "enter_learning",
         "args": [], "kwargs": {}, "responses": [EMPTY], "error_code": 0xFFFB},
        {"cls": "sp2", "devtype": 0x2711, "method": "check_power",
         "args": [], "kwargs": {}, "responses": [EMPTY], "error_code": 0xFFF9},
    ]
