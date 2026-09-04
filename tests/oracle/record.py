"""Record the oracle fixtures from the current library.

Run from the repository root:

    python -m tests.oracle.record

This overwrites ``tests/oracle/fixtures.json``. Only run it when the recorded
behavior is meant to change (for example, a deliberate protocol fix), and
review the diff of the fixture file in the same pull request.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import cases as case_module
from .harness import run_case

FIXTURES = Path(__file__).with_name("fixtures.json")


def build() -> list[dict]:
    entries = []
    for case in case_module.all_cases() + case_module.error_cases():
        outcome = run_case(case)
        entries.append({"case": case, "expect": outcome})
    return entries


def main() -> None:
    entries = build()
    FIXTURES.write_text(json.dumps(entries, indent=1) + "\n")
    print(f"recorded {len(entries)} cases to {FIXTURES}")


if __name__ == "__main__":
    main()
