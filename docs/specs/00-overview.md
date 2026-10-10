# 00 — Overview

`dyness-b3-bms-mqtt` is a long-running service for a Raspberry Pi Zero 2 that reads the
battery-management telemetry of a stack of Dyness B3 modules over CAN and publishes it to an
MQTT broker, with Home Assistant MQTT discovery so every value appears as an entity without
manual configuration.

It is the production successor to a separate diagnostic effort, which established the
protocol and found one weak cell in each of modules 2 and 3 (§6). The
service exists so those cells, and the health of the whole stack, can be watched continuously.

## 1. Scope

In scope:

* Reading all module and stack frames the Dyness BMS puts on the CAN bus, including the
  per-cell voltages that are only sent after a poll request.
* Publishing per-module, per-stack and per-service state to MQTT at a configurable interval.
* Home Assistant device-based MQTT discovery for every published value.
* In-service alert evaluation published as Home Assistant `binary_sensor` entities.
* A local daily CSV history of module and stack values, with an events file, kept on the Pi
  with compression and retention.
* Running unattended as a systemd service that restarts, recovers the CAN adapter, and
  reports its own health. "Always runs" is a primary requirement.
* A `Makefile` that installs everything onto a stock Raspberry Pi OS (64-bit) image from the
  GitHub release archive, with the version single-sourced in the package and released by
  release-please as `vX.Y.Z` prerelease tags (`10-installation.md`).
* Lint, type checks and tests run by `make check` and in CI (`11-testing.md`).

Non-goals. The service MUST NOT:

* Transmit anything on the CAN bus other than the module poll frame (see `01-protocol.md`,
  TX safety). In particular it MUST NOT send BMS parameter writes (`0x18F26677`).
* Use the RS485 pins. The B3 only speaks RS485 when its DIP switches select an RS485
  inverter protocol; with the Solis CAN setting, RS485 is silent.
* Control or influence the inverter.
* Contain or depend on Dyness binaries, decompiled code, manuals or parameter files.

## 2. Target system

| Item | Value |
|---|---|
| Batteries | 3 × Dyness B3, 48 V / 75 Ah, **15S** LFP (not 16S). Stack 225 Ah / 10.8 kWh |
| Inverter | Solis RHI-3.6K-48ES-5G, battery type "Dyness LV", closed loop over CAN |
| DIP switches | Master `0010`, slaves `0000`. Never changed by this project |
| Host | Raspberry Pi Zero 2 W, Raspberry Pi OS 64-bit (Bookworm or later; tested on Trixie), 512 MB RAM |
| Adapter | DSD TECH SH-C31A USB-CAN (CANable 2.0 design, candleLight firmware), [Amazon B0CDGD1FSH](https://www.amazon.co.uk/dp/B0CDGD1FSH) |
| Cable | RJ45 pin 4 (solid blue) → CAN_H, pin 5 (blue/white) → CAN_L, all other pins and GND unconnected. Adapter 120 Ω termination **OFF** |
| Connection point | Spare **OUT** port of the last slave. All module and inverter frames are visible there |
| Runtime | Python 3.14 managed by `uv`, Linux kernel `gs_usb` driver → SocketCAN `can0` |
| Home Assistant | 2024.11 or newer (device-based MQTT discovery) |

## 3. Glossary

| Term | Meaning |
|---|---|
| Module | One Dyness B3 unit with its own BMS. Numbered `m` = 1..4 by its CAN address. Module number is **not** a statement about physical position in the rack |
| Stack | All modules together, as seen by the inverter. Stack frames are what the master sends to the inverter |
| Cell | One of the 15 LFP cells in a module. Numbered 1..15 in this project and in all published data. The BMS uses 0-based numbers in some frames; the protocol spec says where |
| Broadcast frame | A module frame sent without being asked (`0x16`, `0x21`–`0x24`) |
| Cell stream | The module frames only sent after a poll (`0x11`–`0x15`, `0x17`). Once started it has been seen to continue overnight with no further poll; how long it persists, and whether it survives a BMS restart, is **UNVERIFIED** (`01-protocol.md` §5) |
| Poll | The single permitted transmit, `0x18F21m77` data `01 00 00 00 00 00 00 00` |
| Fresh / stale | A value is fresh when its source frame arrived within the relevant timeout (see `03-health-and-recovery.md`), otherwise stale. Stale values are published as `null` |
| Charging sign | Published current is **positive when charging**, for modules and stack alike |

## 4. Conventions used in these specifications

### 4.1 Requirement identifiers

Each spec file owns a prefix and numbers its requirements `PREFIX-NN` (two digits, zero
padded, never reused or renumbered once committed). A requirement that is withdrawn keeps its
number and is marked `(withdrawn)`.

| File | Prefix |
|---|---|
| `01-protocol.md` | `PROTO` |
| `02-can-transport.md` | `CAN` |
| `03-health-and-recovery.md` | `HEALTH` |
| `04-telemetry.md` | `TEL` |
| `05-alerts.md` | `ALERT` |
| `06-mqtt.md` | `MQTT` |
| `07-home-assistant.md` | `HA` |
| `08-configuration.md` | `CFG` |
| `09-service.md` | `SVC` |
| `10-installation.md` | `INST` |
| `11-testing.md` | `TEST` |
| `12-local-logging.md` | `LOG` |

### 4.2 Requirement language

`MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT` and `MAY` follow RFC 2119. Everything not marked
with one of these words is explanation, not a requirement.

### 4.3 Verification status of protocol facts

A requirement or table row tagged **UNVERIFIED** is derived from documentation or captures
but has not been confirmed on the real stack. Implementation MUST follow it as written; the
first hardware run MUST either confirm it or change the spec. Untagged protocol facts were
confirmed on real hardware on 8–9 October 2026 (see `01-protocol.md`).

### 4.4 Traceability

* Every `MUST` requirement MUST be referenced by at least one automated test using
  `@pytest.mark.req("PREFIX-NN")`, except requirements explicitly tagged **MANUAL**, which
  are covered by the hardware acceptance checklist in `11-testing.md`.
* The test suite MUST fail if any `MUST` requirement in `docs/specs/` is neither referenced
  by a test nor tagged **MANUAL**, and if any test references an identifier that does not
  exist.
* A behaviour change MUST change the spec in the same commit as the code and tests.

### 4.5 Living specs and dated design docs

`docs/specs/` always describes the **current** intended behaviour. The reasoning behind a
change, the alternatives rejected and the state of knowledge at the time live in dated
design documents under `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`. The living
specs are edited in place; the design docs are never edited after the fact, only added to.
Implementation plans live under `docs/superpowers/plans/`, alongside the design docs.
Hardware acceptance results are recorded per run in
`docs/acceptance/YYYY-MM-DD-hardware-acceptance.md` (`11-testing.md`).

### 4.6 Shared constants

Timeouts, thresholds and names that appear in more than one spec are defined once in
`08-configuration.md` as environment variables and referenced by variable name elsewhere.
A spec MUST NOT restate a default value that `08-configuration.md` owns. A worked example
that is explicitly labelled as using the default values (as in `05-alerts.md` §3 and
`07-home-assistant.md` §2) is exempt, since `08-configuration.md` remains the authority.

## 5. Document index

| File | Covers |
|---|---|
| `00-overview.md` | This document |
| `01-protocol.md` | CAN frame identifiers, byte layouts, scaling, the poll frame, TX safety |
| `02-can-transport.md` | Opening the bus, interface selection, filters, error frames, clean shutdown, bus-state query |
| `03-health-and-recovery.md` | Freshness, the health state machine, poll policy, recovery ladder, exit codes |
| `04-telemetry.md` | Snapshot data model, derived values, units, sign convention, staleness |
| `05-alerts.md` | Alert definitions, thresholds, debounce, hysteresis |
| `06-mqtt.md` | Broker connection, topics, JSON payloads, QoS and retain, reconnect |
| `07-home-assistant.md` | Discovery devices and entities, availability, expiry, republish |
| `08-configuration.md` | Every environment variable, defaults, validation |
| `09-service.md` | systemd unit, watchdog, root helpers, udev rule, USB reset |
| `10-installation.md` | Makefile targets, paths, uv and Python 3.14, idempotency, install from the GitHub release archive, `make upgrade` to a newer release (INST-19), single-sourced version (INST-17), release-please and `vX.Y.Z` prerelease tags (INST-18) |
| `11-testing.md` | Simulated stack, fakes, capture replay, traceability, hardware acceptance, lint, types and CI (TEST-33..35) |
| `12-local-logging.md` | Local CSV and events history on the Pi, daily files, compression, retention, write faults |

## 6. Source material

The protocol was reverse-engineered and confirmed on the real stack in a separate diagnostic
effort in October 2026 (the private dyness-debug repository), which also found the weak
cells M2 #8 and M3 #9. This repository does not depend on it:

* `01-protocol.md` is the protocol authority for this project and carries every protocol
  fact from that work. Hardware results win over it; when they differ, the spec is updated
  to match.
* `tests/vectors/captures/` holds vendored copies of the 8 October 2026 hardware captures
  (`t1-listen-only-sniff.txt`, `t2-poll-60s.txt`), used as test vectors (`11-testing.md`
  §4.1).

## 7. Architecture and source layout

All code lives in `src/dyness_b3_bms_mqtt/`. The protocol, health, telemetry, alert, payload
and discovery modules are pure; I/O is confined to the transport, publisher, state store,
local log and the application's `Runtime`.

| Module | Responsibility | Spec |
|---|---|---|
| `__init__.py` | Package marker and the single `__version__` line | `10-installation.md` |
| `__main__.py` | `python -m dyness_b3_bms_mqtt`, the same command as the console script | `09-service.md` |
| `app.py` | Entry point (`--version`, `--check-config`), wiring, the main loop, signals and exit codes | `09-service.md` |
| `config.py` | Environment variable names, defaults, parsing and validation | `08-configuration.md` |
| `protocol.py` | Frame classification, decoding and the guarded poll builder | `01-protocol.md` |
| `canbus.py` | The only code that talks to python-can: open, receive, poll, shutdown | `02-can-transport.md` |
| `linkstate.py` | The kernel's view of the SocketCAN link from `ip -j -d -s link show` | `02-can-transport.md` |
| `health.py` | Pure health and recovery state machine: poll policy, availability, reopen, exit | `03-health-and-recovery.md` |
| `state_store.py` | Persistent recovery state (`recovery.json`) in `$STATE_DIRECTORY` | `03-health-and-recovery.md` |
| `status.py` | Shared status vocabularies of the health machine and the local log | `03-health-and-recovery.md`, `12-local-logging.md` |
| `telemetry.py` | Latest-value store and immutable snapshots with staleness, sign and rounding | `04-telemetry.md` |
| `alerts.py` | Debounced, hysteresis-protected alert evaluation over snapshots | `05-alerts.md` |
| `payloads.py` | Module, stack and service JSON state payloads | `06-mqtt.md` |
| `topics.py` | The MQTT topic layout and discovery identifiers, spelt once | `06-mqtt.md` |
| `mqtt.py` | The `Publisher` interface and its paho-mqtt implementation | `06-mqtt.md` |
| `discovery.py` | Home Assistant device-based discovery messages | `07-home-assistant.md` |
| `localog.py` | Daily CSV and events history with compression and retention | `12-local-logging.md` |
| `logsetup.py` | Journald-friendly one-line stdout logging | `08-configuration.md`, `09-service.md` |
| `timefmt.py` | UTC ISO 8601 text for published and persisted timestamps | `06-mqtt.md` |
| `sdnotify.py` | Dependency-free systemd `sd_notify` client (ready, watchdog, status) | `09-service.md` |
