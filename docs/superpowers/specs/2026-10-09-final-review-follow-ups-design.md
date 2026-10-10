# dyness-b3-bms-mqtt — final branch review follow-ups

* **Date:** 2026-10-09
* **Status:** accepted
* **Scope:** two code changes made from the final review of the implementation branch,
  after `2026-10-09-implementation-deviations-design.md` was written

The deviations record lists where the implementation departs from the specs and states that
no code changed for it. The final review of the branch then produced two code changes that
were first written into that record as follow-ups under its changes 8 and 10. A dated design
doc is never edited once written, so those follow-ups live here instead and the deviations
record is back to its original text. Earlier design records are otherwise unchanged.

## Changes

1. **CAN-04** (`02-can-transport.md`, `canbus.py`): `open()` passes `ignore_config=True` to
   `can.Bus`, so python-can loads no configuration file and reads no `CAN_*` or
   `CAN_CONFIG` variable. The deviations record (change 8) had only documented the merge of
   those sources with the service's own arguments; that was not enough, because a malformed
   `/etc/can.conf` or `CAN_CONFIG` raised a `configparser` or JSON error that §8 of the
   transport spec does not map, so the service exited 1 and crash-looped. The CAN-04 note
   now says the bus gets exactly the table's arguments.
2. **LOG-15, LOG-17** (`12-local-logging.md`, `localog.py`): housekeeping gzips at level 6
   instead of 9 and at most `MAX_COMPRESS_PER_RUN` = 8 files per run; the next hourly runs
   take the rest. Housekeeping runs on the main loop, so a backlog (after lowering
   `LOCAL_LOG_COMPRESS_AFTER_DAYS`, say) could otherwise outlast `WatchdogSec=30` and have
   systemd kill a healthy service. LOG-15 gains a note; LOG-17 holds once the backlog is
   cleared.

## Risks

* Both changes are covered by unit tests. Neither alters a published topic or payload.
