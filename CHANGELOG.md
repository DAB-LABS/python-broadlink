# Changelog

All notable changes to this project are recorded here. The format follows
Keep a Changelog; versions follow Semantic Versioning.

## 1.0.1 - 2026-09-05

Fixes from an independent review of 1.0.0, most of them in the transport.
None changes the wire format or the public API.

### Fixed

- A reply to a request that had already timed out could be delivered as the
  reply to the next request on the same device, because the persistent
  endpoint (new in 1.0.0) is not thrown away between calls the way the old
  per-call socket was. Replies are now matched to their request by the
  packet counter the device echoes at offset 0x28; a reply carrying the
  counter of a request that already timed out is discarded, and a reply
  whose counter matches nothing the device sent is still accepted, so
  firmware that does not echo the counter is unaffected. Confirmed on an
  RM4 Pro, which echoes it.
- `capture()` treated only `StorageError` (-5) as "nothing captured yet".
  Some firmware answers `ReadError` (-10); both are now treated as "nothing
  yet", matching what the original CLI and Home Assistant do while polling.
  The CLI's `--learn` and `--rflearn` inherit the fix.
- Abandoning a capture generator without closing it (for example `break`
  out of `async for` to take one code) no longer blocks the next
  `capture()` on the same device: opening a new window closes an abandoned
  one. Opening a window while another is actively being iterated still
  raises `CaptureInProgressError`. A new read-only `Device.capture_active`
  property reports whether a window is open.
- Re-authentication is now shared between concurrent callers: when several
  requests hit an expired session key at once, the library authenticates
  once and every caller retries, instead of one caller re-authenticating
  and the others surfacing the raw error. The logged-out code (-2) now
  triggers re-authentication as well, matching Home Assistant's own retry.
- Changing `device.host` after the endpoint is open now reopens it against
  the new address instead of continuing to talk to the old one.
- `aclose()` while a request is in flight fails that request at once with
  `ConnectionClosedError` instead of waiting out the timeout.

### Documentation

- The README explains that `broadlink` and `python-broadlink` install the
  same package name and cannot coexist, and how to recover if both were
  installed.
- The changelog no longer describes the carried-over device commits as
  "intact" (they were squash-merged with `Co-authored-by` credit) and no
  longer overstates what the oracle records.

## 1.0.0 - 2026-09-05

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
- The CLI tools run their body under `asyncio.run`. `broadlink_cli
  --learn` and `--rflearn` use `capture()` / `capture_rf()`, so a learning
  session no longer goes deaf when the device times out partway through;
  `--window` sets how long to listen, `--keep` prints every code heard, and
  `--send --durations --repeat N` sets the repeat count. The CLI README's
  `--rfscanlearn` was a typo for `--rflearn` (mjg59/python-broadlink#803,
  #830).
- `pulses_to_data` returns `bytes` (it returned a `bytearray`, against its
  own annotation).
- The device's request lock is now a private `_lock` that is actually
  acquired; the unused public `Device.lock` attribute is gone.
- Packaging moved to `pyproject.toml`; `setup.py` and the stale
  `requirements.txt` pin are gone. The distribution name is now
  `python-broadlink`; the import name stays `broadlink`. Python 3.13 or
  newer is required.
- Continuous integration now runs `ruff` and `pytest` on Python 3.13 and
  3.14, and builds the sdist and wheel on every pull request. Releases are
  published to PyPI from version tags using trusted publishing.

### Fixed

- The IR tick constant used by `pulses_to_data` and `data_to_pulses` is now
  `TICK = 8192 / 269` (about 30.45 us), matching the device's 32768 Hz
  timebase as documented in `protocol.md`. The previous value, 32.84, was
  the inverse ratio applied the wrong way round and compressed IR codes
  built from true microsecond timings by about 7 percent. Codes learned and
  replayed through the same device were unaffected. Verified on an RM4 Pro
  against an independent receiver in both directions.
  (mjg59/python-broadlink#839, #841)
- `pulses_to_data` rounds each duration to the nearest tick instead of
  truncating, which removes up to one tick of systematic shortening per
  pulse.

### Added

- `capture()` and `capture_rf()`, async generators that own the arm, poll,
  timeout and re-arm loop of a learning session and yield each signal as a
  `CapturedSignal` (device packet, decoded pulses at the correct tick,
  kind, repeat count, and for RF the carrier frequency). They re-arm on a
  timer, because the device leaves learning mode silently, and after any
  `send_data`, because a transmission ends the session; both intervals and
  the poll cadence were set from a bench on an RM4 Pro. Only one window can
  be open per device. `capture_rf()` (Pro models only) takes the carrier
  frequency directly and falls back to the on-device sweep when it is not
  given.
- Packet helpers: `pulses_to_data` takes `kind` and `repeat`, `parse_packet`
  is its inverse, and `SignalKind` names the IR, 433 MHz and 315 MHz bands.
  A device's returned RF packet does not always use the canonical type byte
  (an RM4 Pro answers a 433 MHz capture with 0xB1, not 0xB2), so the kind is
  read by band and a capture is tagged from what it armed rather than the
  byte.
- Devices, carried over from pull requests against the original repository
  with their authors credited (the changes were squash-merged with
  `Co-authored-by` trailers naming each author): RM Max 0xAF8B (#838, Alexey Masolov);
  RM5 plus 0x5224 with a new `rm5plus` class (#831, Anil Daoud); RM mini 3
  OEM 0xA544 (#823, Bartłomiej Nogaś); RM mini 3 CMCC 0x27C8 (#802,
  shuxin); LB26 R1 0xA517 (#812, techitapart); SP mini 3-AL 0x7D15 (#805,
  bbcbbk); LEDVANCE SMART+ WIFI CEILING TW 24W 0x6498 (#799, Felipe Martins
  Diel).
- Devices reported in issues against the original repository, added by
  model name to the existing class for that family and not yet confirmed on
  hardware: MP1-1K3S2U 0x4EDA (#816) and SP4 0xA57A (#758). Please open an
  issue if either does not behave.
- `cryptography` 43 or newer is required, the first release with wheels for
  Python 3.13 (supersedes mjg59/python-broadlink#749).
- A test suite. The `tests/oracle` package records, for every public method
  of every device class, the request each one hands to the transport (its
  packet type and plaintext payload) and the result it decodes from a canned
  response, so that a later reimplementation can be checked against the
  original method by method; the framing, encryption and checksum layer is
  covered separately by `tests/test_transport.py`.
