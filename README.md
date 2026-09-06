# python-broadlink

A Python module and CLI for controlling Broadlink devices locally.

> **About this fork.** This repository is a maintained fork of
> [mjg59/python-broadlink](https://github.com/mjg59/python-broadlink), which
> has not accepted changes since 2024. It exists so that Home Assistant's
> Broadlink integration has a library that can take fixes and new devices.
> The distribution on PyPI is `python-broadlink`; the import name stays
> `broadlink`. The first release corrects the IR timing constant reported in
> upstream [#839](https://github.com/mjg59/python-broadlink/issues/839)
> (fix in [#841](https://github.com/mjg59/python-broadlink/pull/841)) and
> adds the devices waiting in upstream's pull request queue, including the
> RM Max and RM5 Plus. Version 1.0 is asynchronous and adds `capture()`;
> see `CHANGELOG.md`. Upstream's credit and MIT license are preserved.

## Version 1.0 is asynchronous

Every call that reaches a device is a coroutine and must be awaited. That
is the main change from the original library's API: method names and
arguments are the same, and so are return values, with the small
exceptions listed in `CHANGELOG.md` (the IR tick constant, `pulses_to_data`
returning `bytes`, the unused `Device.lock` attribute removed, `timeout`
parameters typed as floats, and the `mac` in a hello response typed as
`bytes`).

```python
import asyncio
import broadlink


async def main():
    devices = await broadlink.discover(timeout=5)
    device = devices[0]
    await device.auth()
    print(await device.check_sensors())


asyncio.run(main())
```

Calling a device method without `await` returns a coroutine object and
sends nothing; Python prints a `RuntimeWarning: coroutine ... was never
awaited` when it is garbage collected. If you need the old synchronous
behaviour, pin the original distribution (`broadlink==0.19.0`) instead.

The following devices are supported:

- **Universal remotes**: RM home, RM mini 3, RM plus, RM pro, RM pro+, RM4 mini, RM4 pro, RM4C mini, RM4S, RM4 TV mate, RM Max, RM5 plus
- **Smart plugs**: SP mini, SP mini 3, SP mini+, SP1, SP2, SP2-BR, SP2-CL, SP2-IN, SP2-UK, SP3, SP3-EU, SP3S-EU, SP3S-US, SP4L-AU, SP4L-EU, SP4L-UK, SP4M, SP4M-US, SP mini 3-AL, Ankuoo NEO, Ankuoo NEO PRO, Efergy Ego, BG AHC/U-01
- **Switches**: MCB1, SC1, SCB1E, SCB2
- **Outlets**: BG 800, BG 900
- **Power strips**: MP1-1K3S2U, MP1-1K4S, MP2
- **Environment sensors**: A1, A2
- **Alarm kits**: S1C, S2KIT
- **Light bulbs**: LB1, LB26 R1, LB27 R1, SB800TD, LEDVANCE SMART+ WIFI CEILING TW 24W
- **Curtain motors**: Dooya DT360E-45/20
- **Thermostats**: Hysen HY02/HY03
- **Hubs**: S3

## Timing

The original library converted microseconds to the device's timing units
with the constant 32.84, which is the right ratio applied the wrong way
round, and it shortened every IR code built from microsecond timings by
about 7 percent. Codes learned from a remote and replayed through the same
device were never affected, which is why it went unnoticed for years.
Version 1.0 uses 8192/269 (about 30.45 us per unit), the value implied by
`protocol.md`, and rounds to the nearest unit instead of truncating.

Measured on an RM4 Pro against an independent receiver, the same NEC frame
packed with the old constant arrived 5.4 percent short of its intended
length; packed with the corrected constant it arrived 0.6 percent short,
twice, thirteen hours apart, within 22 us of itself. Packets learned by
the device and replayed by name are unchanged. Anything that stores
microsecond timings produced by the old `data_to_pulses` (which reported
them about 7.8 percent long) and re-encodes them with the new
`pulses_to_data` will lengthen by that amount; store the device packet
instead, as `CapturedSignal.packet` does.

## Installation

Python 3.13 or newer. That is a support decision rather than a technical
one: the code runs on 3.11, but the versions tested in CI are 3.13 and
3.14 and those are the ones Home Assistant ships.

Use pip3 to install the latest version of this module.

```
pip3 install python-broadlink
```

Both this distribution and the original `broadlink` install a package named
`broadlink`, so only one can be present in an environment at a time. Pip
does not warn about this: installing one on top of the other appears to
succeed, and whichever was installed last is the one that `import broadlink`
finds. If both were installed, uninstall both (`pip3 uninstall broadlink
python-broadlink`) and reinstall this one, since `pip3 uninstall broadlink`
alone removes the shared files and leaves `python-broadlink` registered but
unimportable. This matters most where another package pins `broadlink`:
installing it into the same environment silently replaces this async
library with the original synchronous one.

## Basic functions

The examples below are written as they would appear inside an `async def`
function run with `asyncio.run(...)`, as in the snippet above. To try them
interactively, start Python with `python3 -m asyncio`, which gives you a
prompt where `await` works at the top level.

```python3
import broadlink
```

Now let's try some functions...

### Setup

In order to control the device, you need to connect it to your local network. If you have already configured the device with the Broadlink app, this step is not necessary.

1. Put the device into AP Mode.
  - Long press the reset button until the blue LED is blinking quickly.
  - Long press again until blue LED is blinking slowly.
  - Manually connect to the WiFi SSID named BroadlinkProv.
2. Connect the device to your local network with the setup function.
```python3
await broadlink.setup("myssid", "mynetworkpass", 3)
```

Security mode options are (0 = none, 1 = WEP, 2 = WPA1, 3 = WPA2, 4 = WPA1/2)

#### Advanced options

You may need to specify a broadcast address if setup is not working.
```python3
await broadlink.setup("myssid", "mynetworkpass", 3, ip_address="192.168.0.255")
```

### Discovery

Use this function to discover devices:

```python3
devices = await broadlink.discover()
```

#### Advanced options
You may need to specify `local_ip_address` or `discover_ip_address` if discovery does not return any devices.

Using the IP address of your local machine:
```python3
devices = await broadlink.discover(local_ip_address="192.168.0.100")
```

Using the broadcast address of your subnet:
```python3
devices = await broadlink.discover(discover_ip_address="192.168.0.255")
```

If the device is locked, it may not be discoverable with broadcast. In such cases, you can use the unicast version `broadlink.hello()` for direct discovery:
```python3
device = await broadlink.hello("192.168.0.16")
```

`discover()` and `hello()` raise `NetworkTimeoutError` when nothing answers
within the timeout, `socket.gaierror` when a hostname does not resolve, and
`OSError` when the socket cannot be opened or the send fails (no route, for
example), the same errors the original library raised from its socket.

If you are a perfomance freak, use `broadlink.xdiscover()` to create devices instantly:
```python3
async for device in broadlink.xdiscover():
    print(device)  # Example action. Do whatever you want here.
```

### Authentication
After discovering the device, call the `auth()` method to obtain the authentication key required for further communication:
```python3
await device.auth()
```

The session key expires on the device after a while. When a request comes
back with an expired-key answer, the library authenticates again and
repeats the request once, so a long-running program does not need to
handle that itself. If the second authentication fails, for example
because the device was locked in the app in the meantime, the call raises
the error the device gave the first time, the same `AuthorizationError`
or `ConnectionClosedError` the original library raised, and it is up to
the caller to decide what to do. In the worst case one call can wait out
three timeouts (the request, the authentication, and the repeat), each
bounded by `device.timeout`.

### Closing

Each device keeps one UDP socket open for its lifetime (the original
library opened a new one for every call). Close it when you are done with
the device, either with the context manager or explicitly:

```python3
async with device:
    await device.auth()
    print(await device.check_sensors())

# or
await device.aclose()
```

A device belongs to the event loop it first talks on: its socket and its
locks are bound to that loop, so a script that runs several
`asyncio.run(...)` calls should create the device inside each one rather
than reuse it across them.

The socket reopens by itself on the next call, so closing is cheap and
safe to do at any time. A request that is in flight when `aclose()` runs
fails with `EndpointClosedError`. A request that fails for a network
reason (a timeout, or an `OSError` from the socket such as "network is
unreachable" after an interface change, raised at once) also drops the socket, so the
next call starts fresh rather than reusing one that has gone bad, which
is how the original library behaved by opening a socket per call. An
integration that creates devices should close them when it unloads; a
device that is never closed holds its socket until it is garbage
collected.

The next steps depend on the type of device you want to control.

## Universal remotes

### Learning IR codes

Learning IR codes takes place in three steps.

1. Enter learning mode:
```python3
await device.enter_learning()
```
2. When the LED blinks, point the remote at the Broadlink device and press the button you want to learn.
3. Get the IR packet.
```python3
packet = await device.check_data()
```

### Learning RF codes

Learning RF codes takes place in six steps.

1. Sweep the frequency:
```python3
await device.sweep_frequency()
```
2. When the LED blinks, point the remote at the Broadlink device for the first time and long press the button you want to learn.
3. Check if the frequency was successfully identified:
```python3
ok, frequency = await device.check_frequency()
if ok:
    print(f"Frequency found: {frequency} MHz")
```
4. Enter learning mode:
```python3
await device.find_rf_packet()
```
5. When the LED blinks, point the remote at the Broadlink device for the second time and short press the button you want to learn.
6. Get the RF packet:
```python3
packet = await device.check_data()
```

#### Notes

Universal remotes with product id 0x2712 use the same method for learning IR and RF codes. They don't need to sweep frequency. Just call `device.enter_learning()` and `device.check_data()`.

### Canceling learning

You can exit the learning mode in the middle of the process by calling this method:
```python3
await device.cancel_sweep_frequency()
```

### Capturing signals

`capture()` wraps the arm, poll, timeout and re-arm dance above into one
async generator that yields each signal it hears as a `CapturedSignal`:

```python3
from contextlib import aclosing

async with aclosing(device.capture(window=30)) as signals:
    async for signal in signals:
        print(signal.kind, len(signal.pulses), "pulses")
        await other_device.send_data(signal.packet)
```

By default the window closes after the first signal. Pass
`stop_after_first=False` to keep it open for the whole `window` (in seconds;
`window=0` runs until the generator is closed), re-arming after each signal
because the device holds only one code per learning session. A universal
remote has a single receiver, so only one capture window can be open on a
device at a time: opening a second one raises `CaptureInProgressError`
from the new window's first iteration while the first is still held.
Always close a window you leave early
(`aclosing` above does it), otherwise it stays open until Python collects
the generator.

`CapturedSignal` carries the device's own `packet` bytes (ready for
`send_data`), the decoded `pulses` in microseconds at the corrected tick, the
`kind` (`SignalKind.IR`, `RF_433` or `RF_315`), the `repeat` count, and for
RF the `frequency_mhz` the packet itself does not record.

RF works the same way on the Pro models, with the carrier as the one extra
input:

```python3
async with aclosing(device.capture_rf(window=30, frequency=433.92)) as signals:
    async for signal in signals:
        ...
```

Pass `frequency` whenever you know it. Without it the device first sweeps
for the carrier while you hold a button down, then learns the code from a
fresh press; the sweep is unreliable on some firmware and can report a
carrier it never really locked, so the known-frequency path is preferred.

### Sending IR/RF packets
```python3
await device.send_data(packet)
```

### Fetching sensor data
```python3
data = await device.check_sensors()
```

## Switches

### Setting power state
```python3
await device.set_power(True)
await device.set_power(False)
```

### Checking power state
```python3
state = await device.check_power()
```

### Checking energy consumption
```python3
state = await device.get_energy()
```

## Power strips

### Setting power state
```python3
await device.set_power(1, True)  # Example socket. It could be 2 or 3.
await device.set_power(1, False)
```

### Checking power state
```python3
state = await device.check_power()
```

## Light bulbs

### Fetching data
```python3
state = await device.get_state()
```

### Setting state attributes
```python3
await devices[0].set_state(pwr=0)
await devices[0].set_state(pwr=1)
await devices[0].set_state(brightness=75)
await devices[0].set_state(bulb_colormode=0)
await devices[0].set_state(blue=255)
await devices[0].set_state(red=0)
await devices[0].set_state(green=128)
await devices[0].set_state(bulb_colormode=1)
```

## Environment sensors

### Fetching sensor data
```python3
data = await device.check_sensors()
```

## Hubs

### Discovering subdevices
```python3
await device.get_subdevices()
```

### Fetching data
Use the DID obtained from get_subdevices() for the input parameter to query specific sub-device.

```python3
await device.get_state(did="00000000000000000000a043b0d06963")
```

### Setting state attributes
The parameters depend on the type of subdevice that is being controlled. In this example, we are controlling LC-1 switches:

#### Turn on
```python3
await device.set_state(did="00000000000000000000a043b0d0783a", pwr=1)
await device.set_state(did="00000000000000000000a043b0d0783a", pwr1=1)
await device.set_state(did="00000000000000000000a043b0d0783a", pwr2=1)
```
#### Turn off
```python3
await device.set_state(did="00000000000000000000a043b0d0783a", pwr=0)
await device.set_state(did="00000000000000000000a043b0d0783a", pwr1=0)
await device.set_state(did="00000000000000000000a043b0d0783a", pwr2=0)
```
