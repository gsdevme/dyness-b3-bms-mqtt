# 11 — Testing (`TEST`)

How the service is tested without hardware, how the traceability rule in `00-overview.md`
§4.4 is enforced, and the manual acceptance run on the real stack. Requirement IDs in this
file are verified by the harness's own tests (`tests/harness/`): the simulator, fakes and
traceability plugin each have tests that carry `@pytest.mark.req("TEST-NN")`.

## 1. Test stack

* **TEST-01** The suite MUST use pytest and MUST run with `uv run pytest` from a fresh clone
  with no CAN hardware, no `can0`, no MQTT broker, no network access and no root. Tests that
  need any of those MUST be behind the markers in TEST-02 and not run by default.
* **TEST-02** The following markers MUST be registered in `pyproject.toml` and pytest MUST
  run with `--strict-markers`:

  | Marker | Meaning | Default |
  |---|---|---|
  | `req("ID", ...)` | Test verifies the named requirement(s) | Always runs |
  | `integration` | Uses real wall-clock time (see TEST-11) | Always runs |
  | `mosquitto` | Needs a real MQTT broker | Deselected unless pytest is given `--mosquitto`; with it the broker at `MQTT_HOST`:`MQTT_PORT` MUST accept a TCP connection within a 10 s wait or every `mosquitto` test fails |
  | `hardware` | Needs the real stack on the configured `CAN_CHANNEL` | Skipped unless pytest is given `--hardware` |

* **TEST-03** A test skipped by the `hardware` rule MUST report the reason (`--hardware` not
  given), and a `mosquitto` test failed because the broker is unreachable MUST report the
  host, port and wait. `mosquitto` tests are never skipped: a run either deselects them or
  requires the broker, so no run can pass with them silently not run.
* **TEST-04** The default suite SHOULD complete in under 60 s on a CI runner and under 5 min
  on a Pi Zero 2.

Suggested layout (not a requirement): `tests/unit/`, `tests/integration/`,
`tests/hardware/`, `tests/harness/` (tests of the fakes and plugin), `tests/fakes/`,
`tests/sim/`, `tests/vectors/`, `tests/golden/`, `tests/plugins/reqtrace.py`.

## 2. Traceability enforcement

This is the mechanism behind `00-overview.md` §4.4.

* **TEST-05** A pytest plugin (`tests/plugins/reqtrace.py`, loaded from `conftest.py`) MUST
  parse every `docs/specs/*.md` file for requirement definitions: a bold identifier
  `**PREFIX-NN**` where `PREFIX` is in the table in `00-overview.md` §4.1. A requirement's
  text runs from its identifier to the next identifier, heading, table row, blank line or
  code fence.
  Each requirement MUST be classified as:

  | Status | Rule |
  |---|---|
  | `withdrawn` | Text contains `(withdrawn)` |
  | `manual` | Text contains the tag `**MANUAL**` |
  | `must` | Text contains `MUST` or `MUST NOT` |
  | `should` | Anything else (`SHOULD`, `MAY`) |

  Identifiers that appear only as references (not bold), or inside code spans or fenced
  code, are not definitions. Classification ignores code spans, so a `MUST` or
  `**MANUAL**` quoted in backticks does not count.

  Because a requirement's text ends at the first blank line or table row, a requirement
  whose lead paragraph is followed by a table, a code block or a further paragraph is
  classified from its lead paragraph alone. The `MUST` (or `**MANUAL**` tag) that classifies
  such a requirement therefore has to be in its lead sentence; one that appears only after
  that point does not count.
* **TEST-06** The plugin MUST fail the session (non-zero exit) when any of these holds, and
  print every offending identifier grouped by spec file:
  1. a `must` requirement is not referenced by any collected test's `req` marker;
  2. a `req` marker names an identifier that is not defined, or is `withdrawn`;
  3. an identifier is defined more than once;
  4. a `req` argument does not match `^[A-Z]+-[0-9]{2}$`.

  Rule 1 fails the session only with the plugin's `--req-strict` option, which
  `pyproject.toml` supplies through `addopts` so every default run is strict; without it
  rule 1 is a warning. Rules 2–4 always fail.
* **TEST-07** Only tests without the `hardware` or `mosquitto` markers MUST count towards
  covering a `must` requirement, so that the default run alone satisfies §4.4. A test counts
  when it is collected, whether it passes or fails (failures already fail the session).
* **TEST-08** Rule 1 of TEST-06 MUST apply only when the whole default suite is collected.
  When the run is narrowed (test paths, `-k`, `-m`, `--deselect`, `--lf`, `--stepwise`,
  `--stepwise-skip`, a collection error, or any other deselection of a test that could
  cover a requirement), the plugin MUST downgrade rule 1 to
  a warning; rules 2–4 MUST still fail. Deselecting the `mosquitto` tests when
  `--mosquitto` is not given does not narrow the run, because they never cover (TEST-07).
* **TEST-09** At the end of every session the plugin MUST print a one-line summary (counts of
  must / covered / manual / should). With `--req-report` it MUST print the full matrix:
  identifier, status, and the node IDs of covering tests, followed by the list of `manual`
  identifiers for ticking against the hardware acceptance checklist in §10.
* **TEST-10** The plugin MUST have its own tests using `pytester` with a throwaway
  `docs/specs/` tree covering each failure rule and the narrowed-run downgrade.

## 3. Time and determinism

* **TEST-11** Unit tests MUST NOT sleep or read the real clock. Every component takes an
  injected monotonic `Clock` (see `04-telemetry.md` and `03-health-and-recovery.md`) and a
  `FakeClock` fixture provides `now()` and `advance(seconds)`. An autouse fixture MUST make
  `time.sleep` raise in any test not marked `integration`. Tests marked `integration` MAY use
  short real time but MUST each finish within 10 s wall-clock, enforced by a per-test timeout.
  Every other test MUST be stopped by a suite-wide pytest-timeout ceiling of 120 s
  (`timeout` in `pyproject.toml`), so a hung test fails instead of stalling the run.
* **TEST-12** Any randomness in production code (the discovery republish jitter in
  `07-home-assistant.md`) MUST come from an injectable source so tests can fix it.
* **TEST-13** Tests MUST be independent of order and of each other: each simulated bus uses a
  unique `virtual` channel name, and each test gets a fresh temporary state directory.

## 4. Unit-test fixtures

### 4.1 Frame vectors

* **TEST-14** The repository MUST vendor the capture lines it needs under `tests/vectors/`.
  Tests MUST NOT read `../dyness-debug` or any path outside the repository.
* **TEST-15** Vector files MUST keep the capture line format of the source
  (`<time> <ID hex> <X|S> <data bytes hex>`; `X` = 29-bit, `S` = 11-bit) and start with a
  comment header naming the source file and line range in
  `dyness-debug/data/2026-10-08/` (`t1-listen-only-sniff.txt`, `t2-poll-60s.txt`). Together
  they MUST include at least one real frame of every module frame type in `01-protocol.md`
  for each of modules 1–3, every inverter frame type, the heartbeat, and a poll with its echo.
* **TEST-16** Each vector file MUST have a sibling `*.expected.json` holding the decoded
  values. Expected values MUST be written from `01-protocol.md` and the worked examples in
  `../dyness-debug/docs/protocol.md`, never generated by running the decoder under test.
* **TEST-17** A separate `tests/vectors/synthetic.txt` MUST hold hand-made frames, each
  commented with its purpose: wrong DLC (0, 7 and 8+ where the type allows), module numbers
  0 and 5–15, unknown frame codes, an 11-bit frame with a module-like ID, and
  `0x18F26677`.

### 4.2 Fakes

* **TEST-18** `FakePublisher` MUST implement the same publisher protocol as the paho-based
  implementation (`06-mqtt.md`) and record every publish as `(topic, payload, qos, retain)` in
  order. It MUST let a test: set connected or disconnected (publishes while disconnected are
  recorded as dropped, not delivered), fire the connect event, and deliver a message on
  `homeassistant/status`.
* **TEST-19** Root helper tests (`can-up`, `recover`, USB reset; see `09-service.md`) MUST run
  unprivileged against a fake filesystem tree: a temporary root holding `sys/class/net/…`,
  `sys/bus/usb/devices/…` and `dev/bus/usb/…`, selected by the test-only root overrides
  `SYSFS_ROOT` and `DEV_ROOT` (SVC-18), and a temporary environment file selected by the
  test-only override `ENV_FILE` (SVC-14), none of which are part of the
  `08-configuration.md` interface. External commands (`ip`, `usbreset`) MUST be replaced by
  fakes on `PATH` that record their argv and return scripted output.
* **TEST-20** A fake `NOTIFY_SOCKET` (a temporary `AF_UNIX` datagram socket) MUST be available
  to assert the `READY=1`, `WATCHDOG=1`, `STATUS=` and `STOPPING=1` datagrams.

## 5. Simulated stack

The simulator follows `../dyness-debug/tests/test_reader.py` `frames(m)`, extended to be
time-driven and fault-injectable.

* **TEST-21** `SimulatedStack` MUST run on a python-can `virtual` bus and model, for each
  module 1..`MODULE_COUNT`:
  * the broadcast frames (`0x16`, `0x21`–`0x24`) at about 1.7 Hz each, unsolicited;
  * the cell stream (`0x11`–`0x15`, `0x17`) at about 1 Hz, starting only after that module
    receives a poll and then continuing without further polls;
  * and, for the stack, the heartbeat `0x18F20103` at 10 Hz and the inverter frames at
    about 1 Hz.
* **TEST-22** The simulator MUST be driven by a `FakeClock` in unit tests (`advance(dt)` emits
  every frame due in that interval, in timestamp order, without threads). In `integration`
  tests it MAY run on a thread in real time.
* **TEST-23** The simulator MUST record every frame it receives from the service and MUST
  optionally echo it back, to model the adapter echoing its own transmissions.
* **TEST-24** The simulator MUST offer these knobs, each switchable at any simulated time:

  | Knob | Effect |
  |---|---|
  | `silence_module(m)` / `resume_module(m)` | Module `m` sends nothing |
  | `silence_bus()` / `resume_bus()` | Nothing at all is sent |
  | `stop_cell_stream(m)` | Cell stream for `m` stops until the next poll |
  | `inject_error_frames(n)` | Emit `n` CAN error frames |
  | `set_cells(m, mv[15])` | Cell voltages for `m`; `0x22` max/min follow, 0-based on the wire |
  | `set_flags(m, bytes)` | Raw `0x24` payload for `m` |
  | `set_current(m, amps)` | Module current in published sign (simulator negates on the wire) |
  | `set_soc(m, pct)` | `0x23` SOC and the inverter `0x355` mean |

  Defaults MUST be realistic values taken from the vectors, with module 2 cell 8 and module 3
  cell 9 about 25 mV below their neighbours.
* **TEST-25** The simulator MUST encode frames with its own code under `tests/`. It MUST NOT
  import the package's `protocol` module, so a decoder bug cannot cancel out.
* **TEST-26** `FlakyBus` MUST wrap a python-can bus and, on demand, raise
  `OSError(errno.ENODEV)` or `OSError(errno.ENETDOWN)` from `recv` and `send`, either once or
  until cleared, and fail the next N `open` attempts. It MUST record whether `shutdown()` was
  called.

## 6. Component test matrix

* **TEST-27** Each area MUST have at least the tests below, all marked with the requirement
  IDs they verify.

| Spec | Unit under test | Required tests |
|---|---|---|
| `01-protocol.md` (PROTO) | `protocol.classify`, `protocol.decode` | Property test that neither raises for any input (PROTO-03); every vendored real frame decodes to its `*.expected.json` values; every synthetic frame is dropped, and wrong-DLC frames are counted; 0-based `0x22` cell numbers become 1-based; raw `0x21` current sign is negated; slot 16 and T4 are ignored; every `0x24` bit maps to its flag name |
| `01-protocol.md` (PROTO) | `protocol.build_poll` | Property test (TEST-28) |
| `02-can-transport.md` (CAN) | `canbus` | Interface selection per `CAN_INTERFACE`; filters applied; error frames surfaced; `shutdown()` on every exit path including exceptions (`FlakyBus`); bus-state parsing from recorded `ip -j -d -s link show` output for every state, including the interface being absent |
| `03-health-and-recovery.md` (HEALTH) | `health` (inputs `start`, `on_frame`, `on_error_frame`, `on_bus_exception`, `on_bus_open`, `step`) | Table-driven: every transition in the state table, and every timeout tested at just below and just above its boundary; poll re-try and back-off ladder; error-frame window suspending polls; escalation counter read, increment and reset; DEAD mode reopen and periodic exit |
| `04-telemetry.md` (TEL) | `telemetry` | Snapshot fields from vectors; stale → `null` at the boundary; derived values (spread, stack lowest/highest with module and cell, sums and means); rounding |
| `05-alerts.md` (ALERT) | `alerts` | Timelines on a `FakeClock`: condition shorter than `ALERT_DEBOUNCE_S` never fires; held condition fires; recovery inside the hysteresis band does not clear; stale input gives unknown; imbalance gating on `ALERT_IMBALANCE_MIN_CURRENT_A` |
| `06-mqtt.md` (MQTT) | `mqtt`, `app` with `FakePublisher` | Order on connect (status `online`, subscribe `<P>/status`, discovery, availability, state; MQTT-09); order on HA `online`; QoS and retain per topic; nothing queued while disconnected; latest state after reconnect; immediate publish on alert or availability change |
| `07-home-assistant.md` (HA) | `discovery` | Golden files (TEST-31) for each device with defaults and with `MODULE_NAMES`; empty retained configs for modules `MODULE_COUNT`+1..4 (HA-20); `HA_DISCOVERY=false` publishes nothing; every `unique_id` unique and stable |
| `08-configuration.md` (CFG) | `config` | Each variable at both range limits and just outside; every boolean spelling; empty = unset; cross-variable rules; secrets redacted in logs; exit 78 without opening bus or broker; `--check-config` |
| `09-service.md` (SVC) | helpers, `sdnotify` | `can-up` idempotency (no `ip` calls when already correct); `recover` per `EXIT_STATUS`; USB reset fallback order on the fake tree; sd_notify datagrams and watchdog cadence |
| `10-installation.md` (INST) | `Makefile`, packaging | Static checks: unit file, udev rule and example env file parse; example env lists every variable in `08-configuration.md` |
| `12-local-logging.md` (LOG) | `localog`, `app` | Columns equal the MQTT keys; header once, append on restart; nulls as empty fields; UTC day roll; events lines; interval independent of `PUBLISH_INTERVAL_S`; housekeeping gzip and delete at the age boundaries, today's files untouched, idempotent; `OSError` on write (disk full) sets `error` without raising and recovers at the next housekeeping; disabled without `STATE_DIRECTORY`; no `fsync` (static check) |

* **TEST-28** A Hypothesis property test MUST show that no input to `build_poll` other than a
  module number in 1..`module_count` with `module_count` in 1..4 (PROTO-26) produces a frame, and that every produced frame is exactly
  `0x18F21m77` with data `01 00 00 00 00 00 00 00`. In addition, an autouse fixture MUST wrap
  every bus the service opens in any test and fail the test if any other frame is sent,
  if `0x18F26677` is ever sent, or if anything is sent while `CAN_LISTEN_ONLY=true`.
* **TEST-29** `app.run()` MUST be tested end-to-end with the `SimulatedStack`, `FakeClock`,
  `FakePublisher` and a temporary state directory, asserting the process exit code and the
  `bus.shutdown()` call for: clean SIGTERM (0), bus silence escalating to link bounce (75) and
  then USB reset (76), the escalation counter reaching DEAD mode (no exit until the periodic
  USB reset), invalid configuration (78), and an unexpected exception (1).
* **TEST-30** An `integration` test SHOULD run the full service against a threaded
  `SimulatedStack` in real time for a few seconds and check that the cells for every module
  appear in published state.

## 7. Golden files

* **TEST-31** Discovery payloads and the module, stack and service state JSON produced from
  a fixed simulator scenario and fixed clocks MUST be compared against files in
  `tests/golden/` (`discovery/` and `state/`). Comparison MUST be on parsed JSON; files MUST
  be written with sorted keys and 2-space indent so diffs are readable.
* **TEST-32** A missing or differing golden file MUST fail the test. Running
  `uv run pytest --update-golden` MUST rewrite the golden files instead of comparing; the
  regenerated files are reviewed as part of the commit that changes the behaviour, together
  with the spec change (`00-overview.md` §4.4).

## 8. Lint, types and CI

* **TEST-33** `make check` MUST run, and fail on the first failure of: `ruff check`,
  `ruff format --check`, `mypy --strict` over `src/` and `tests/`, and `uv run pytest`.
* **TEST-34** The repository MUST contain `.github/workflows/ci.yml` running `make check` on
  `ubuntu-latest` for every `push` event to `main` and every `pull_request` event targeting
  `main` (a PR branch is covered by the `pull_request` event alone, so a commit never runs
  the workflow twice), with uv installed by `astral-sh/setup-uv`, Python 3.14, and
  `uv sync --frozen`. CI MUST use only the python-can `virtual` interface: no `vcan`, kernel
  modules or `sudo`.
* **TEST-35** CI SHOULD run the `mosquitto` tests in a second job against an
  `eclipse-mosquitto:1.6` service container (pulled from `public.ecr.aws/docker/library/`,
  which has no Docker Hub pull limit) on port 1883, with `MQTT_HOST` and `MQTT_PORT`
  pointing at it, running `uv run --frozen pytest --mosquitto -m mosquitto`. Mosquitto 2.x
  only accepts anonymous remote connections when given a configuration file, which a GitHub
  Actions service container cannot be given, so the 1.6 image (which allows them by default)
  is used. The repository's `docker-compose.yml` SHOULD run the same image on the same port,
  so the same command works locally after `docker compose up -d`.
* **TEST-36** Line coverage of the pure modules (`protocol`, `telemetry`, `alerts`, `health`,
  `discovery`, `config`) SHOULD be at least 90 %. Neither the suite nor CI measures
  coverage at present.

## 9. Integration tests

* **TEST-37** Tests marked `mosquitto` MUST use the real paho client against the broker and
  check: retained discovery and availability, the LWT `offline` after an abrupt socket close,
  explicit `offline` on clean stop, and reconnect after the broker connection drops.
* **TEST-38** Tests marked `hardware` MUST run listen-only unless `--hardware-poll` is also
  given, and MUST NOT transmit anything other than the poll frame. With the real stack they
  check that broadcast frames arrive from every module within `START_GRACE_S`.

## 10. Hardware acceptance

* **TEST-39** **MANUAL** The checklist below MUST be run on the real stack before the first
  unattended deployment and again after any change to `01-protocol.md`, `02-can-transport.md`,
  `03-health-and-recovery.md` or `09-service.md`. Results MUST be recorded in a dated file
  `docs/acceptance/YYYY-MM-DD-hardware-acceptance.md` with pass/fail and notes per step, and
  any **UNVERIFIED** fact it confirms or contradicts MUST be updated in the spec.
* **TEST-40** **MANUAL** Every requirement tagged **MANUAL** in `docs/specs/` MUST be covered
  by at least one step below; the `--req-report` list (TEST-09) is the cross-check.

Watch the service with `make logs`, `systemctl status dyness-b3-bms-mqtt`, `ip -d -s link
show can0` and the Home Assistant devices. Times are given by variable name; see
`08-configuration.md` for values.

| # | Step | Expected observation |
|---|---|---|
| 1 | Fresh install with the README quick-start commands (INST-15), `CAN_LISTEN_ONLY=true`, start the service | Service `active (running)` (INST-15); `can0` up in listen-only mode (`ip -d link`); TX packet counter stays 0; health reaches HEALTHY or DEGRADED with cells stale; module broadcast entities (pack V, current, SOC, min/max cell) populate in HA; cell sensors stay unknown unless a cell stream is still running from an earlier poll; inverter shows no CAN fault; **UNVERIFIED** facts in `01-protocol.md` checked against real frames |
| 2 | Set `CAN_LISTEN_ONLY=false`, `sudo make restart` | Exactly one poll per module logged at start, staggered by `POLL_STAGGER_S`; TX counter rises by `MODULE_COUNT` plus echoes; all 15 cells per module populate within `START_GRACE_S`; M2 #8 and M3 #9 read lowest at rest as in `../dyness-debug`; inverter charge/discharge limits unchanged; frames still received an hour later (no RX stall; if it stalls and the escalation reaches exit 76, the USB reset brings the adapter back without a replug or the log names the failed step, SVC-19) |
| 3 | Unplug the adapter for 2 min, then replug | STALE within `BUS_SILENCE_S`; reopen attempts; exit 75, restart, DEAD while the adapter is absent (module availability offline, service diagnostics show dead and the reason); on replug the udev rule brings `can0` up and the service returns to HEALTHY without intervention; HA module entities flip unavailable then back |
| 4 | As step 3 but leave unplugged past `DEAD_USB_RESET_S` | Exit 76 logged; `recover` attempts a USB reset, logs that no device was found and exits 0; service restarts into DEAD again |
| 5 | `sudo ip link set can0 down` | STALE; in-process reopen fails; exit 75; `recover` bounces the link and `can-up` restores it; HEALTHY again; escalation counter resets after `RECOVERY_RESET_S` healthy |
| 6 | Stop the broker for 2 min, then start it | No health transition, no exit, watchdog keeps being fed; journal shows disconnect and reconnect; after reconnect `status` is `online`, discovery and availability republished, current state published, no backlog replayed |
| 7 | Restart Home Assistant | On HA `online`, discovery, availability and state republished after the jitter delay; all entities return with history intact; no duplicate entities |
| 8 | `sudo kill -STOP <pid>` | After `WatchdogSec` systemd logs a watchdog timeout and kills the process; restart follows after `RestartSec`; `status` goes `offline` via the LWT once the broker's keepalive expires; service returns to HEALTHY; `NRestarts` incremented |
| 9 | Set `MQTT_PORT=0`, restart | Exit 78; journal names `MQTT_PORT`, its value and the reason; unit `failed` with no restart loop (`NRestarts` unchanged); bus not opened (no TX). Restore the value and restart |
| 10 | `sudo systemctl stop dyness-b3-bms-mqtt` | Exit 0 within `TimeoutStopSec`; explicit `offline` published; `bus.shutdown()` logged; `recover` does nothing |
| 11 | Power-cycle the Pi | Service starts on boot without login; `can0` comes up; polls sent; HEALTHY; HA entities return |
| 12 | 24 h soak in normal operation | No unexpected exits (`NRestarts` 0, `last_exit_code` and `last_exit_result` diagnostics unchanged); RSS from `systemctl status` within TEST-41 and not growing by more than 5 MB between hour 1 and hour 24; average CPU within TEST-41 (`ps -o %cpu,rss`); journal free of repeated warnings |
| 13 | Local log housekeeping: `sudo ls -l /var/lib/dyness-b3-bms-mqtt/logs`, then create `modules-<date>.csv` dated two days ago and `stack-<date>.csv` dated `LOCAL_LOG_RETENTION_DAYS` + 1 days ago (`sudo touch`), `sudo make restart`, list again | Today's `events-` file exists, and today's `modules-` and `stack-` files have one header line each and rows every `LOCAL_LOG_INTERVAL_S` (`sudo tail`); values match the MQTT state for the same `ts`; after the restart the two-day-old file is gzipped (original gone), the over-retention file is deleted, today's files are untouched and still have a single header; `local_log` shows `ok` in HA |
| 14 | Disk full: check `sudo tune2fs -l` on the root device shows a non-zero reserved block count (so root and the journal keep working), fill the root filesystem for the service user (`df -B1 /` for `Avail`, then `sudo fallocate -l <Avail> /var/tmp/fill`), wait two `LOCAL_LOG_INTERVAL_S`, then `sudo rm /var/tmp/fill` | While full: exactly one `ERROR` line naming the log file and the error; `local_log` shows `error`; module, stack and service state keep publishing; no exit or health change; watchdog fed. After freeing space: `local_log` back to `ok` and `local log recovered` logged at the next hourly housekeeping; rows resume; after `sudo make restart` the service starts with `local_log` `ok` (LOG-22) |
| 15 | `systemd-analyze verify /etc/systemd/system/dyness-b3-bms-mqtt.service` (SVC-02) | No unknown directives reported |
| 16 | Edit `/etc/default/dyness-b3-bms-mqtt`, note its `sha256sum`, run `sudo make install` again (INST-16) | Install finishes without errors; the env file's `sha256sum` is unchanged; the service was restarted |
| 17 | Userspace `gs_usb` listen-only (CAN-17): stop the service, move the SH-C31A to the macOS development machine still wired to the stack, `uv sync --extra gs-usb`, `CAN_INTERFACE=gs_usb CAN_CHANNEL=0 uv run pytest tests/hardware --hardware`; then move the adapter back (replug) and start the service | The bus opens without `LISTEN_ONLY_REFUSED` (the adapter reports listen-only, CAN-16); broadcasts arrive from every module within `START_GRACE_S`; inverter shows no CAN fault; back on the Pi `can0` comes up and the service returns to HEALTHY |

## 11. Performance

* **TEST-41** **MANUAL** On a Raspberry Pi Zero 2 W with `MODULE_COUNT=3` and normal bus
  traffic, the service SHOULD average below 5 % CPU and stay below 60 MB RSS over the 24 h
  soak (step 12).
* **TEST-42** A unit test SHOULD decode and store 10 000 vendored frames in under 1 s on a CI
  runner, as an early warning for regressions that would threaten TEST-41.
