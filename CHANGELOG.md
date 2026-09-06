# Changelog

All notable changes to this project are recorded here. The format follows
Keep a Changelog; versions follow Semantic Versioning.

## 1.0.4 - 2026-09-06

Fixes from a fourth review, of 1.0.3, which drove the real socket path the
test suite fakes and found two behaviours the original library had and
this one had lost. One device fix carried from upstream.

### Fixed

- A connected socket that went bad (interface bounce, host address change,
  container network restart) was never replaced: the request waited out
  its timeout and every later request did the same until `aclose()`. The
  original opened a socket per call, so it healed on the next one. Now a
  send failure the socket reports (no route, address gone) fails the
  waiting request at once with that `OSError`, and a request that fails
  for a network reason (that, or a timeout) drops the socket so the next
  call opens a fresh one. A transport asyncio closes from its side also
  wakes the waiting request instead of leaving it to time out. An ICMP
  "port unreachable" (a host that is up with nothing listening, or a
  device mid-reboot) is logged and treated as silence, since the
  original's unconnected socket never saw those, so the timeout decides
  as before.
- `discover()`, `hello()`, `ping()` and `setup()` passed hostnames straight
  to `sendto`, which resolved them with a blocking call on the event loop
  and swallowed the failure: a name that did not resolve made `hello()`
  wait out its timeout and `ping()` return without sending. The
  destination is now resolved once, off the loop, and `socket.gaierror`
  propagates as it did from the original's socket. A send failure in
  `ping()` and `setup()` is raised too.
- The A2 air quality sensor's request frame was two bytes short and
  declared the wrong length, and real units answered every read with
  error -5. The frame now follows the SP4/LB1 layout, which is byte for
  byte the packet upstream pull request #826 tested on an A2. That is the
  one oracle case re-recorded on purpose; the fix is carried on the
  strength of that report, not of hardware we have.
- `xdiscover()` closes the `scan()` generator it wraps, so the discovery
  socket is closed when the caller stops iterating rather than by the
  finalizer a few turns later (1.0.2 claimed this and only `Device.hello()`
  did it).
- Two identical captures compare equal: `CapturedSignal.captured_at` no
  longer takes part in equality or hashing.
- Async generator functions are annotated `AsyncGenerator`, which has the
  `aclose()` the library and the README call; `AsyncIterator` does not.
- The `TICK` docstring tells the same story as the README: 8192/269 from
  protocol.md's measured conversion, not a 32768 Hz clock.

### Added

- A loopback test module that drives the real datagram endpoint, including
  the socket-error path, since every other transport test fakes it.
- README: which errors `discover()` and `hello()` raise, that a failed
  request drops its socket, and that `CaptureInProgressError` can come
  from the `capture()` call or from the first iteration.

## 1.0.3 - 2026-09-06

Fixes from a third review, this one of 1.0.2. No change to the wire
format. One small API change: `pulses` on a captured signal is a tuple.

### Fixed

- When the device answered that the session key had expired and the
  re-authentication then failed (for example because the device had been
  locked in the app), the call raised `AuthenticationError` from the
  re-authentication instead of the error the device gave the request. The
  original library never re-authenticated, so a program written against
  it, Home Assistant's integration included, handles the request's own
  error and never expected the other one. The failed re-authentication is
  now logged and the request's original reply is returned, so the caller
  sees the same `AuthorizationError` or `ConnectionClosedError` it always
  did.
- The authentication generation was read before the request lock was
  taken rather than under it, so a request queued behind an `auth()`
  could observe a stale generation and skip a re-authentication it needed.
- `aclose()` racing an endpoint that was still being opened could leave
  the new socket open and unreferenced. The open now notices the close
  and fails with `EndpointClosedError`.
- After a new capture window gives the finalizer its turn, it re-checks
  that no other window claimed the device in the meantime.
- `CapturedSignal` and `ParsedPacket` are frozen dataclasses, but they
  held a list, so they could not be hashed or put in a set. `pulses` is
  now a `tuple[int, ...]`.
- `check_error` unpacks the error code as little-endian explicitly
  (`"<h"`), matching the rest of the code, instead of native order.
- The CLI closes the device it opens instead of leaving that to
  `asyncio.run`, which warned under `python -X dev`.
- The locks are created in `__init__` rather than lazily in two places.

### Changed

- `send_packet` accepts a `bytearray` payload as well as `bytes`.
- `setup()` sends its provisioning packet through a new
  `send_setup_packet()` helper in `broadlink.device` instead of reaching
  into a private function.
- README: the re-authentication contract and its worst case (one call can
  wait out up to three timeouts), the A2 sensor and the Hysen HY02/HY03
  in the device list, and the hello response's `mac` being `bytes` in the
  list of differences from 0.19.0.

## 1.0.2 - 2026-09-05

Fixes from a second, adversarial review of 1.0.1 and a re-test of the
first review's findings. No change to the wire format or the public API.

### Fixed

- 1.0.1's reply matching dropped a late reply to a request that had
  timed out, but not the second reply to a request that was resent after a
  silent second and then answered twice. That duplicate carries the counter
  of a request that succeeded, and it could still be taken as the answer
  to the next request. The library now remembers every recently used
  counter and drops any reply carrying one other than the current
  request's. A reply whose counter the device has not used recently is
  still accepted, for firmware that may not echo it.
- `auth()` reset the session id and key before taking the request lock, so
  a request already queued behind the lock could be framed with device id
  0 and the initial key. The reset, the exchange and the install of the
  new key now happen as one unit under the lock.
- 1.0.1 let a new capture window close one that a consumer had abandoned,
  using "is the generator running right now" as the test. That cannot
  tell an abandoned window from one whose consumer is awaiting something
  between signals, which the README's own example does. A new window now
  gives asyncio's finalizer one turn to close a genuinely dropped
  generator and then refuses if the old window is still alive, rather
  than taking it. A refused attempt no longer displaces the live window.
- A packet the device returned that cannot be decoded (a declared length
  running into a truncated escape) no longer ends the capture window; it
  is logged and the window re-arms.
- `aclose()` during a request now raises `EndpointClosedError`, a subclass
  of `ConnectionClosedError` with code -4013 in the error table, so a
  caller that closed the device on purpose can tell that apart from the
  device's own "logged out" answer.
- `hello()` closes the discovery generator it breaks out of instead of
  leaving the socket to the finalizer; `asyncio.TimeoutError` is spelled
  `TimeoutError`; an unused future on the protocol object is gone.

### Added

- Debug logging on the `broadlink.device` and `broadlink.remote` loggers:
  endpoint open and close, resends, dropped late replies, timeouts,
  re-authentication, capture arm and re-arm, captured packets.
- README: a "Closing" section on the persistent socket, a "Timing" section
  with the bench measurement of the tick fix (5.4 percent short before,
  0.6 percent short after, on an RM4 Pro against an independent
  receiver), a note that Python 3.13 is a support decision, and the short
  list of return-value differences from 0.19.0.

### Changed

- The code is formatted with `ruff format` and CI checks it.

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
