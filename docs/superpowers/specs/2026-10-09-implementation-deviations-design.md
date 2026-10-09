# dyness-b3-bms-mqtt — implementation deviations

* **Date:** 2026-10-09
* **Status:** accepted
* **Scope:** spec text that disagreed with the implemented code, found in the task reviews

Reviewing each implementation task against `docs/specs/` found places where the living spec
and the code disagree. In each case the code was reviewed and kept, so the spec now records
what is implemented. No code changed for that; the only test edits are the requirement
markers of two tests (INST-17) and the doc block of the traceability plugin. A code change
from the final branch review is recorded as a follow-up under change 8. The earlier design
records (`2026-10-09-dyness-b3-bms-mqtt-design.md`,
`2026-10-09-implementation-amendments-design.md`) are unchanged.

## Changes

1. **TEST-09** (`11-testing.md`): the `manual` list printed by `--req-report` is ticked
   against the hardware acceptance checklist in §10. The text said §8, which is lint, types
   and CI.
2. **TEL-26** (`04-telemetry.md`): the 0-based `0x22` cell numbers are converted to 1-based
   once, by the decoder (PROTO-11), not in the store. The requirement now says they are
   converted exactly once, before telemetry sees them, which is what PROTO-11 and the
   decoder already do.
3. **HEALTH-01** (`03-health-and-recovery.md`): the health machine is not given a clock; the
   application passes it the `now` of every input and `step` call, as HEALTH-02 and §5.1
   already said. The "clock injected into the machine" wording contradicted both.
4. **`CELL_STALE_S`** (`08-configuration.md`): the cell stream used for freshness is
   `0x11`–`0x15` only (HEALTH-04); `0x17` was wrongly listed in the variable's row.
5. **HEALTH-05, HEALTH-07, §4 `STARTING` row** (`03-health-and-recovery.md`): ages measured
   from the bus open would let a module that has not been heard count as `OK`, so `STARTING`
   could end in `HEALTHY` with nothing received. The machine uses an effective status: a
   module that has not broadcast is `SILENT`, and unless `CAN_LISTEN_ONLY` it is
   `CELLS_STALE` until all five cell codes have been received since the service started.
   `published_statuses()` reports that status (and `SILENT` outside the running states,
   TEL-06), and is what the application passes to telemetry (HEALTH-20).
6. **`HA_DISCOVERY_PREFIX`, `MODULE_NAMES`** (`08-configuration.md`): the prefix is
   validated with the full `MQTT_BASE_TOPIC` rule (charset, 1..64 characters, no leading or
   trailing `/`), not only its charset, so it cannot produce empty topic levels; whitespace
   around each `MODULE_NAMES` number and name is trimmed, so `1: Top, 2: Bottom` is accepted
   as written in an env file.
7. **INST-17** (new, `10-installation.md`): the version is single-sourced from `pyproject.toml`,
   read at run time from the distribution metadata, and bumped by release-please, whose
   `extra-files` TOML updater also bumps the root package in `uv.lock` so `--frozen` installs
   keep working (INST-11). This was in the global constraints and tested, but cited as
   INST-14, which is about the console script and build backend; the two tests now cite
   INST-17.
8. **CAN-04, CAN-16/CAN-17** (`02-can-transport.md`): two python-can behaviours are now
   written down. `can.Bus` merges python-can config files and `CAN_*` environment variables
   into its arguments; the service's explicit arguments win, and only keys it does not pass
   can be added. On the macOS development path, python-can's `gs_usb` `shutdown()` with an
   `index` re-runs `start()`/`stop()` in normal mode, outside the CAN-16 listen-only window.
   A `gs_usb` USB write timeout maps to `OS_ERROR` and an absent adapter to `OPEN_FAILED`,
   as the §8 table already implies; the note makes that explicit.
   * Final-review follow-up: `open()` now passes `ignore_config=True` to `can.Bus`, so
     python-can loads no configuration file or `CAN_*`/`CAN_CONFIG` variable. Documenting
     the merge was not enough: a malformed `/etc/can.conf` or `CAN_CONFIG` raised a
     `configparser` or JSON error that §8 does not map, so the service exited 1 and
     crash-looped. The CAN-04 note now says the bus gets exactly the table's arguments.
9. **SVC-11, MQTT-10** (`09-service.md`, `06-mqtt.md`): the implemented clean stop is
   `STOPPING=1`, then the retained `offline` and final service state published without
   waiting (and the local history closed), then `bus.shutdown()`, then `publisher.stop()`,
   which publishes `offline` again, waits up to 2 s and disconnects. This releases the
   adapter before any wait on the broker; the duplicate retained `offline` is harmless and
   accepted.
10. **LOG-14, LOG-15** (`12-local-logging.md`): with `LOCAL_LOG_COMPRESS_AFTER_DAYS` = 0 the
    first hourly housekeeping after midnight can gzip yesterday's file while the logger
    still holds it open (it is closed at the first write of the new day). Every write has
    been flushed and nothing more goes to the old handle, so on Linux the gzip copy is
    complete before the name is unlinked and no data is lost.
11. **TEST-39, TEST-40, SVC-02** (`11-testing.md`, `09-service.md`,
    `docs/acceptance/README.md`): SVC-02 and INST-16 had no checklist step, so TEST-40
    (every **MANUAL** requirement covered by a step) did not hold and the acceptance README
    carried them as extra checks. Steps 15 (`systemd-analyze verify`, SVC-02) and 16
    (reinstall keeps the env file, INST-16) are added, steps 1 and 2 now name INST-15 and
    SVC-19, and the README's extra-checks table is gone. `09-service.md` §10 described SVC-02
    as "`systemd-analyze verify` is clean", stronger than SVC-02, which asks only for no
    unknown directives; it now matches SVC-02.
12. **TEST-06 rule 1** (`tests/plugins/reqtrace.py` doc block): rule 1 is no longer opt-in;
    `--req-strict` is in the pytest `addopts`, so it is on by default. The doc block said
    "opt-in".
13. **SVC-19** (`docs/acceptance/README.md`): the README credited step 4 with SVC-19, but
    step 4 exits 76 with the adapter unplugged, so it cannot show a stalled adapter coming
    back. The one-hour run with polling in step 2 is where a stall would appear and is the
    real evidence; the README now says so.

## Risks

* Changes 5 and 9 describe behaviour that only the hardware run fully exercises: start-up
  on the real stack (checklist steps 1 and 2) and the clean stop against a real broker
  (step 10). A contradiction found there is recorded with a new dated design doc, as for any
  other spec change.
* Change 10 relies on POSIX unlink semantics and is not observed by the checklist, which
  runs with the default `LOCAL_LOG_COMPRESS_AFTER_DAYS` = 1, where yesterday's file is never
  compressed.
