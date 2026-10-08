"""Replay the recorded oracle: every device method must send the same bytes
and decode the same result it did when the fixtures were recorded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.oracle.harness import run_case

FIXTURES = Path(__file__).parent / "oracle" / "fixtures.json"
ENTRIES = json.loads(FIXTURES.read_text())


def _ident(entry: dict) -> str:
    c = entry["case"]
    return f"{c['cls']}.{c['method']}"


@pytest.mark.parametrize("entry", ENTRIES, ids=[_ident(e) for e in ENTRIES])
def test_oracle(entry: dict) -> None:
    outcome = run_case(entry["case"])
    assert outcome == entry["expect"]


def test_every_public_method_is_covered() -> None:
    """Fail when a device class grows a public method the oracle does not know."""
    import inspect

    import broadlink
    from broadlink.device import Device

    covered = {(e["case"]["cls"], e["case"]["method"]) for e in ENTRIES}
    # Methods on Device itself that need a live socket are covered in
    # test_transport.py, not here.
    transport_level = {
        "auth",
        "hello",
        "ping",
        "send_packet",
        "encrypt",
        "decrypt",
        "update_aes",
        "aclose",
    }
    # Capture windows drive several requests over time; they are covered
    # with a scripted device in test_capture.py.
    transport_level |= {"capture", "capture_rf"}
    # New in this library, so 0.19.0 has nothing to record. It sends the
    # request 0.19.0's cancel_sweep_frequency sends, checked below against
    # that method's fixtures.
    transport_level |= {"cancel_learning"}
    missing = []
    for name, cls in inspect.getmembers(broadlink, inspect.isclass):
        if not issubclass(cls, Device):
            continue
        for meth, _ in inspect.getmembers(cls, inspect.isfunction):
            if meth.startswith("_") or meth in transport_level:
                continue
            # Inherited methods are covered on the class that defines them
            # or on a subclass case; require at least one case per class/method
            # pair where the method is defined on that class.
            if meth not in cls.__dict__:
                continue
            if (name, meth) not in covered:
                missing.append(f"{name}.{meth}")
    assert not missing, f"public methods without an oracle case: {missing}"


CANCEL_SWEEP_ENTRIES = [
    e for e in ENTRIES if e["case"]["method"] == "cancel_sweep_frequency"
]


@pytest.mark.parametrize(
    "entry", CANCEL_SWEEP_ENTRIES, ids=[e["case"]["cls"] for e in CANCEL_SWEEP_ENTRIES]
)
def test_cancel_learning_sends_what_cancel_sweep_frequency_sent(entry: dict) -> None:
    """cancel_learning is 0.19.0's cancel_sweep_frequency request, byte for byte."""
    case = dict(entry["case"], method="cancel_learning")
    assert run_case(case)["sent"] == entry["expect"]["sent"]
