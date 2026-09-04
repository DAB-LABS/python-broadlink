# Changelog

All notable changes to this project are recorded here. The format follows
Keep a Changelog; versions follow Semantic Versioning.

## Unreleased

This is the first release of `python-broadlink`, a maintained fork of
`mjg59/python-broadlink` (PyPI `broadlink`, last released as 0.19.0). The
history below starts at that fork point.

### Changed

- Packaging moved to `pyproject.toml`; `setup.py` and the stale
  `requirements.txt` pin are gone. The distribution name is now
  `python-broadlink`; the import name stays `broadlink`. Python 3.13 or
  newer is required.
- Continuous integration now runs `ruff` and `pytest` on Python 3.13 and
  3.14, and builds the sdist and wheel on every pull request. Releases are
  published to PyPI from version tags using trusted publishing.

### Added

- A test suite. The `tests/oracle` package records the exact request bytes
  every public method of every device class sends, and the results it
  decodes from canned responses, so that later changes to the transport
  can be checked byte for byte against the original behavior.
