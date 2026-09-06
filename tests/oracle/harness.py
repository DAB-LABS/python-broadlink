"""Harness that records what device methods send and what they decode.

Every public method on a device class ends up calling ``Device.send_packet``
with a packet type and a plaintext payload, and then decoding whatever the
device answers. The transport (framing, encryption, retries) lives in
``send_packet`` itself and is tested separately. This harness replaces
``send_packet`` on one device instance so that:

- each call is recorded as ``(packet_type, payload)`` before encryption, and
- each call is answered with a well-formed response frame carrying the next
  canned payload, encrypted with the device's current session key so that the
  method's own ``decrypt`` sees exactly those bytes.

The recorded sequence and the method's return value are the "oracle": a
later reimplementation of the same method (for example, an asynchronous one)
must produce the same sequence and the same result from the same canned
responses. Results are normalized to plain JSON so they can be stored.

The runner accepts awaitables so the same cases can drive an asynchronous
``send_packet`` later without changing the cases.

Deliberate departures from 0.19.0, re-recorded on purpose and reviewed in
the pull request that made them:

- ``a2.check_sensors_raw`` (1.0.4): the request frame follows the SP4/LB1
  layout (length 12, four-byte data length) instead of the 0.19.0 frame
  the device rejected with error -5. Upstream #826.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
from dataclasses import dataclass, field
from typing import Any

import broadlink
from broadlink.device import Device

# A fixed identity so recorded bytes never depend on random state.
MAC = bytes.fromhex("a043b05510f7")
HOST = ("192.0.2.10", 80)


def pad16(payload: bytes) -> bytes:
    """Pad to the AES block size, as the device does before encrypting."""
    return bytes(payload) + bytes((16 - len(payload)) % 16)


def make_response(device: Device, payload: bytes, error: int = 0) -> bytes:
    """Build a response frame the way a device would answer ``send_packet``.

    Only the parts the device classes read are meaningful: the error code
    at 0x22:0x24 and the encrypted payload from 0x38. The frame checksum is
    filled in so the frame would also pass ``send_packet``'s own check.
    """
    frame = bytearray(0x38)
    frame[0x00:0x08] = bytes.fromhex("5aa5aa555aa5aa55")
    frame[0x22:0x24] = (error & 0xFFFF).to_bytes(2, "little")
    frame[0x24:0x26] = device.devtype.to_bytes(2, "little")
    frame[0x2A:0x30] = device.mac[::-1]
    frame.extend(device.encrypt(pad16(payload)))
    checksum = sum(frame, 0xBEAF) & 0xFFFF
    frame[0x20:0x22] = checksum.to_bytes(2, "little")
    return bytes(frame)


@dataclass
class Recorder:
    """Replacement ``send_packet`` that records requests and serves responses."""

    device: Device
    responses: list[bytes]
    error: int = 0
    sent: list[tuple[int, bytes]] = field(default_factory=list)

    def __call__(self, packet_type: int, payload: bytes) -> bytes:
        self.sent.append((packet_type, bytes(payload)))
        if not self.responses:
            raise AssertionError(
                f"method sent more packets than canned responses ({len(self.sent)} sent)"
            )
        return make_response(self.device, self.responses.pop(0), self.error)

    async def async_call(self, packet_type: int, payload: bytes) -> bytes:
        return self(packet_type, payload)


def normalize(value: Any) -> Any:
    """Turn a method result into plain JSON-compatible data."""
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": bytes(value).hex()}
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def build_device(cls_name: str, devtype: int) -> Device:
    """Instantiate a device class by name with the fixed test identity."""
    cls = getattr(broadlink, cls_name)
    return cls(HOST, MAC, devtype, name="Bench", model="Test", manufacturer="Test")


def run_case(case: dict) -> dict:
    """Execute one case and return the recorded outcome.

    ``case`` has: ``cls``, ``devtype``, ``method``, ``args``, ``kwargs``,
    ``responses`` (list of hex payloads), and optionally ``setup`` (attribute
    values applied before the call) and ``attrs`` (attribute names to record
    after the call).
    """
    device = build_device(case["cls"], case["devtype"])
    for name, value in case.get("setup", {}).items():
        setattr(device, name, value)

    responses = [bytes.fromhex(r) for r in case.get("responses", [])]
    recorder = Recorder(device, responses, case.get("error_code", 0))
    target = device.send_packet
    if inspect.iscoroutinefunction(target):
        device.send_packet = recorder.async_call  # type: ignore[method-assign]
    else:
        device.send_packet = recorder  # type: ignore[method-assign]

    method = getattr(device, case["method"])
    args = [decode_arg(a) for a in case.get("args", [])]
    kwargs = {k: decode_arg(v) for k, v in case.get("kwargs", {}).items()}

    outcome: dict[str, Any] = {}
    try:
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(_await(result))
        outcome["result"] = normalize(result)
    except Exception as err:
        outcome["error"] = f"{type(err).__name__}: {err}"

    outcome["sent"] = [[ptype, payload.hex()] for ptype, payload in recorder.sent]
    outcome["unused_responses"] = len(recorder.responses)
    attrs = case.get("attrs", [])
    if attrs:
        outcome["attrs"] = {a: normalize(getattr(device, a)) for a in attrs}
    return outcome


async def _await(awaitable):
    return await awaitable


def decode_arg(value: Any) -> Any:
    """Cases store bytes arguments as {"__bytes__": hex}."""
    if isinstance(value, dict) and set(value) == {"__bytes__"}:
        return bytes.fromhex(value["__bytes__"])
    if isinstance(value, list):
        return [decode_arg(v) for v in value]
    if isinstance(value, dict):
        return {k: decode_arg(v) for k, v in value.items()}
    return value
