# Changelog

All notable changes to this project are recorded here. The format follows
Keep a Changelog; versions follow Semantic Versioning.

## Unreleased

This is the first release of `python-broadlink`, a maintained fork of
`mjg59/python-broadlink` (PyPI `broadlink`, last released as 0.19.0). The
history below starts at that fork point.

### Changed

- **The library is asynchronous.** Every method that talks to a device is
  now a coroutine: `await device.auth()`, `await device.send_data(...)`,
  `await device.check_sensors()`, and so on. Discovery is
  `await broadlink.discover(...)`, `broadlink.hello(...)` and `setup(...)`
  are coroutines, and `xdiscover(...)` is an async generator. The packet
  helpers (`pulses_to_data`, `data_to_pulses`), CRC and datetime helpers
  stay synchronous. There is no synchronous compatibility layer: a call
  without `await` returns a coroutine and does nothing.
- Each device keeps one UDP endpoint for its lifetime (the previous
  version opened a socket per call) and serializes requests on it with an
  `asyncio.Lock`. The old code declared a lock but never acquired it.
  `async with device:` or `await device.aclose()` releases the endpoint;
  it reopens on the next call.
- When a device reports that the session key has expired, the library
  re-authenticates once and repeats the request. Callers no longer need
  their own re-auth loop.
- Retry and timeout behaviour is unchanged: a request is repeated every
  second until `timeout` elapses, then `NetworkTimeoutError` is raised.
- `dooya.set_percentage_and_wait` sleeps with `asyncio.sleep`.
- The CLI tools run their body under `asyncio.run`.
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
