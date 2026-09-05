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
> RM Max and RM5 Plus. Version 1.0 will be asynchronous; see `CHANGELOG.md`.
> Upstream's credit and MIT license are preserved.

## Version 1.0 is asynchronous

Every call that reaches a device is a coroutine and must be awaited. This
is the whole change from the original library's API; method names,
arguments and return values are the same.

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
- **Environment sensors**: A1
- **Alarm kits**: S1C, S2KIT
- **Light bulbs**: LB1, LB26 R1, LB27 R1, SB800TD, LEDVANCE SMART+ WIFI CEILING TW 24W
- **Curtain motors**: Dooya DT360E-45/20
- **Thermostats**: Hysen HY02B05H
- **Hubs**: S3

## Installation

Use pip3 to install the latest version of this module.

```
pip3 install python-broadlink
```

If the original `broadlink` distribution is also installed in the same
environment, remove it first (`pip3 uninstall broadlink`); both provide the
`broadlink` package.

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
await broadlink.setup('myssid', 'mynetworkpass', 3)
```

Security mode options are (0 = none, 1 = WEP, 2 = WPA1, 3 = WPA2, 4 = WPA1/2)

#### Advanced options

You may need to specify a broadcast address if setup is not working.
```python3
await broadlink.setup('myssid', 'mynetworkpass', 3, ip_address='192.168.0.255')
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
devices = await broadlink.discover(local_ip_address='192.168.0.100')
```

Using the broadcast address of your subnet:
```python3
devices = await broadlink.discover(discover_ip_address='192.168.0.255')
```

If the device is locked, it may not be discoverable with broadcast. In such cases, you can use the unicast version `broadlink.hello()` for direct discovery:
```python3
device = await broadlink.hello('192.168.0.16')
```

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
ok = device.check_frequency()
if ok:
    print('Frequency found!')
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
device at a time.

`CapturedSignal` carries the device's own `packet` bytes (ready for
`send_data`), the decoded `pulses` in microseconds at the correct tick, the
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
state = device.check_power()
```

### Checking energy consumption
```python3
state = device.get_energy()
```

## Power strips

### Setting power state
```python3
await device.set_power(1, True)  # Example socket. It could be 2 or 3.
await device.set_power(1, False)
```

### Checking power state
```python3
state = device.check_power()
```

## Light bulbs

### Fetching data
```python3
state = device.get_state()
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
