# dyness-b3-bms-mqtt

A service for a Raspberry Pi Zero 2 W that reads the battery-management data from a stack of
Dyness B3 modules over CAN and publishes it to MQTT, with Home Assistant discovery so every
value turns up as an entity. It runs under systemd, recovers from adapter and link failures
on its own, and reports its own health.

It grew out of a private diagnostic repository (dyness-debug) that worked out the protocol
and found one weak cell in each of modules 2 and 3 (M2 #8, M3 #9). Everything this project
needs from that work is in `docs/specs/01-protocol.md` and the captures under
`tests/vectors/`. This service keeps an eye on those cells and on the stack as a whole.

**Status (10 Oct 2026):** v0.1.1 is released as a prerelease; hardware acceptance is still
pending. The behaviour is specified in `docs/specs/`; the acceptance run on the real stack is
described in [`docs/acceptance/README.md`](docs/acceptance/README.md).

## Hardware

| Part | Detail |
|---|---|
| Batteries | 3 × Dyness B3, 48 V / 75 Ah, **15S** LFP (stack 225 Ah / 10.8 kWh) |
| Inverter | Solis RHI-3.6K-48ES-5G, battery type "Dyness LV", closed loop over CAN |
| Adapter | DSD TECH SH-C31A USB-CAN (CANable 2.0, candleLight firmware): [Amazon UK](https://www.amazon.co.uk/dp/B0CDGD1FSH). Works with the Linux kernel `gs_usb` driver as delivered |
| Host | Raspberry Pi Zero 2 W running Raspberry Pi OS 64-bit (Bookworm or later; tested on Trixie) |
| Home Assistant | 2024.11 or newer, with an MQTT broker |

The BMS talks **CAN** on RJ45 pins 4/5 with the Solis DIP setting, not RS485; RS485 stays
silent. Module numbers are CAN addresses (`0x18F21m??`), not shelf positions.

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
sudo apt-get install -y curl make
curl -L https://github.com/gsdevme/dyness-b3-bms-mqtt/archive/refs/tags/v0.3.1.tar.gz | tar xz  # x-release-please-version
cd dyness-b3-bms-mqtt-0.3.1  # x-release-please-version
sudo make install
sudo nano /etc/default/dyness-b3-bms-mqtt   # set MQTT_HOST etc.
sudo make check-config
sudo make restart
make logs
```

Upgrade: run `sudo make upgrade` in the existing checkout; it unpacks the newest release
next to it and installs that. Removal: `sudo make uninstall`, or `sudo make uninstall PURGE=1` to also remove
configuration and state.

`make install` installs `uv` and Python 3.14 if needed, the service, the `can0` set-up helper
and a udev rule. Running it again upgrades in place and never overwrites your settings.

For a cautious first run, set `CAN_LISTEN_ONLY=true`: the service then transmits nothing,
and the cell sensors stay empty until you turn polling back on.

Shut the Pi down with `sudo poweroff` before pulling its power: hard power-offs have
corrupted files on the SD card, including the wifi profile.

## What appears in Home Assistant

* **Dyness B3 Stack**: lowest and highest cell (with module and cell number), stack spread,
  total current, stack voltage, SOC, SOH, charge and discharge limits from the BMS, alerts,
  and the service's own diagnostics (CAN state, health, local log, error counters, restarts,
  version).
* **Dyness B3 Module 1–3** (or the names in `MODULE_NAMES`): 15 cell voltages, min/max cell
  and spread, temperatures, pack voltage, current, SOC, SOH, cycles, lifetime energy,
  current limits, MOSFET, balancing, alarm and protection flags.
* Alert binary sensors for cell spread, low cell, module current imbalance and any BMS alarm
  or protection.

On by default, the Pi also keeps its own history in `/var/lib/dyness-b3-bms-mqtt/logs`: daily
CSV files of module and stack values plus an events file, gzip-compressed after a while and
deleted after a retention period. It needs neither MQTT nor Home Assistant; see
[`docs/specs/12-local-logging.md`](docs/specs/12-local-logging.md).

Current is positive when charging. A module that stops talking shows as unavailable rather
than holding its last value.

## Configuration

All settings are environment variables in `/etc/default/dyness-b3-bms-mqtt`. Every variable,
its default and its validation is in [`docs/specs/08-configuration.md`](docs/specs/08-configuration.md).
An invalid setting stops the service with exit status 78 and the journal says which one.
MQTT topics and JSON payloads are in [`docs/specs/06-mqtt.md`](docs/specs/06-mqtt.md).

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

The tests marked `mosquitto` run only with `--mosquitto`, against a real broker that must be
reachable at `MQTT_HOST`:`MQTT_PORT` (default `localhost:1883`). `docker-compose.yml` starts
the same broker CI uses:

```sh
docker compose up -d
uv run pytest --mosquitto -m mosquitto
```

Tests run against a simulated battery stack on python-can's `virtual` bus.

During hardware acceptance, the `verify-mqtt` Claude Code skill
([`.claude/skills/verify-mqtt/`](.claude/skills/verify-mqtt/SKILL.md)) checks a running
deployment's MQTT output against the spec.

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
