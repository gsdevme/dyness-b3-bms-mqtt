# dyness-b3-bms-mqtt

A service for a Raspberry Pi Zero 2 W that reads the battery-management data from a stack of
Dyness B3 modules over CAN and publishes it to MQTT, with Home Assistant discovery so every
value turns up as an entity. It runs under systemd, recovers from adapter and link failures
on its own, and reports its own health.

It is the always-on follow-up to [dyness-debug](https://github.com/gsdevme/dyness-debug),
which worked out the protocol and found one weak cell in each of modules 2 and 3 (M2 #8,
M3 #9). This service keeps an eye on those cells and on the stack as a whole.

**Status (9 Oct 2026):** implemented, awaiting hardware acceptance. The behaviour is
specified in `docs/specs/`; the acceptance run on the real stack is described in
[`docs/acceptance/README.md`](docs/acceptance/README.md).

## Hardware

| Part | Detail |
|---|---|
| Batteries | 3 × Dyness B3, 48 V / 75 Ah, **15S** LFP (stack 225 Ah / 10.8 kWh) |
| Inverter | Solis RHI-3.6K-48ES-5G, battery type "Dyness LV", closed loop over CAN |
| Adapter | DSD TECH SH-C31A USB-CAN (CANable 2.0, candleLight firmware): [Amazon UK](https://www.amazon.co.uk/dp/B0CDGD1FSH). Works with the Linux kernel `gs_usb` driver as delivered |
| Host | Raspberry Pi Zero 2 W running Raspberry Pi OS 64-bit (Bookworm or later) |
| Home Assistant | 2024.11 or newer, with an MQTT broker |

### Cable

An Ethernet patch lead with one end cut off:

| Battery RJ45 pin | Wire colour (T568A and B) | Adapter terminal |
|---|---|---|
| 4 | Solid blue | **CAN_H** |
| 5 | Blue/white | **CAN_L** |
| 1–3, 6–8 | — | Unconnected, taped off individually |
| — | — | GND unconnected |

Set the adapter's 120 Ω termination switch to **OFF**; the bus is already terminated.

### Where to plug in

Plug into the spare **OUT** port of the last slave in the chain. Every module and the
inverter frames are visible there, and nothing else needs unplugging. Leave the DIP switches
as they are (master `0010`, slaves `0000`).

## Quick start

On the Pi:

```sh
sudo apt-get install -y git make
git clone https://github.com/gsdevme/dyness-b3-bms-mqtt.git
cd dyness-b3-bms-mqtt
sudo make install
sudo nano /etc/default/dyness-b3-bms-mqtt   # set MQTT_HOST etc.
sudo make check-config
sudo make restart
make logs
```

Upgrade: `git pull && sudo make install`. Removal: `sudo make uninstall`, or
`sudo make uninstall PURGE=1` to also remove configuration and state.

`make install` installs `uv` and Python 3.14 if needed, the service, the `can0` set-up helper
and a udev rule. Running it again upgrades in place and never overwrites your settings.

For a cautious first run, set `CAN_LISTEN_ONLY=true`: the service then transmits nothing,
and the cell sensors stay empty until you turn polling back on.

## What appears in Home Assistant

* **Dyness B3 Stack**: lowest and highest cell (with module and cell number), stack spread,
  total current, stack voltage, SOC, SOH, charge and discharge limits from the BMS, alerts,
  and the service's own diagnostics (CAN state, health, error counters, restarts, version).
* **Dyness B3 Module 1–3** (or the names in `MODULE_NAMES`): 15 cell voltages, min/max cell
  and spread, temperatures, pack voltage, current, SOC, SOH, cycles, lifetime energy,
  current limits, MOSFET, balancing, alarm and protection flags.
* Alert binary sensors for cell spread, low cell, module current imbalance and any BMS alarm
  or protection.

Current is positive when charging. A module that stops talking shows as unavailable rather
than holding its last value.

## Configuration

All settings are environment variables in `/etc/default/dyness-b3-bms-mqtt`. Every variable,
its default and its validation is in [`docs/specs/08-configuration.md`](docs/specs/08-configuration.md).
An invalid setting stops the service with exit status 78 and the journal says which one.

## How it recovers

The service polls each module once at start, then listens, and re-polls a module only if its
cell data goes stale. If the whole bus goes quiet it closes and reopens the adapter itself;
if that fails it exits so systemd's root helper can bounce the `can0` link, and on a repeat
failure reset the USB adapter. If the adapter is missing altogether it stays running in a
"dead" state, publishes why, and keeps retrying. MQTT outages never affect the CAN side, and
discovery is republished whenever Home Assistant restarts. A systemd watchdog restarts the
process if its main loop ever hangs. The details are in
[`docs/specs/03-health-and-recovery.md`](docs/specs/03-health-and-recovery.md) and
[`docs/specs/09-service.md`](docs/specs/09-service.md).

## Development

```sh
make dev        # uv sync with dev dependencies
uv run pytest   # no hardware or broker needed
make check      # ruff, mypy --strict and the tests
```

Tests run against a simulated battery stack on python-can's `virtual` bus.

## Spec-driven workflow

* [`docs/specs/`](docs/specs/) holds the living specification: what the service must do now,
  as numbered requirements (`PROTO-NN`, `HEALTH-NN`, …). Start with
  [`00-overview.md`](docs/specs/00-overview.md).
* [`docs/superpowers/specs/`](docs/superpowers/specs/) holds dated design documents recording
  why each change was made and what was rejected.
* Behaviour changes update the spec, add a design document and come with tests that name the
  requirement IDs. The test suite fails if a `MUST` requirement has no test.

## Safety

* The service never writes BMS parameters. The only frame it can transmit is the module poll
  `0x18F21m77` with data `01 00 00 00 00 00 00 00`, the same request Dyness Monitor sends.
  Parameter-write frames (`0x18F26677`) cannot be constructed by the code.
* With `CAN_LISTEN_ONLY=true` it transmits nothing at all.
* It does not use RS485 and does not control the inverter.
* This repository contains no Dyness software, manuals or parameter files.

## Credits

Protocol, captures and diagnosis: [dyness-debug](https://github.com/gsdevme/dyness-debug).
