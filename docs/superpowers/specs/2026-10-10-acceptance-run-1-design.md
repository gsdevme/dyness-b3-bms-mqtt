# dyness-b3-bms-mqtt — first hardware acceptance run

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** the target OS named by INST-15 (`10-installation.md`), the host rows of
  `README.md` and `00-overview.md`, and `docs/acceptance/README.md`; the record is
  `docs/acceptance/2026-10-10-hardware-acceptance.md`

## What the run found

The first acceptance run on the Pi Zero 2 W was partial: the automated hardware checks and
steps 1, 2, 3, 4, 5, 11, 15 and 16 of TEST-39 passed on v0.1.2 to v0.3.0; the other nine
steps were not run. Adapter loss was handled end to end: exit 75 with a link bounce, exit 76 with
a USB reset that reported the missing device and exited 0, DEAD while absent, and HEALTHY
again after the replug with no manual action (SVC-21). A downed link (step 5), taken before
`RECOVERY_RESET_S` had cleared the step 4 escalation, went through exit 76 and `recover`'s
USB reset back to HEALTHY in 42 s. That was the first real run of the SVC-18 reset: its
`usbreset` step failed with `No such device found`, because `usbreset` does not take the
`/dev/bus/usb/BBB/DDD` path that `recover` and SVC-18 give it, and the sysfs `authorized`
fallback did the reset. The fix to `recover` and SVC-18 is in PR #18. The installed unit passed
`systemd-analyze verify` (SVC-02). The hour with polling showed no RX
stall (SVC-19 has no stall to recover from yet).

The run produced four changes, each with its own design doc: `can-up` retrying without
`restart-ms` (PR #11, v0.2.0), the `verify-mqtt` skill (PR #13, v0.2.0), `make upgrade`
(PR #14, v0.3.0) and `can-up` logging to the journal under udev (PR #15, v0.3.0).

The fresh image was Pi OS Lite 64-bit **Trixie** (Debian 13.7, kernel
`6.18.50+rpt-rpi-v8`), not the Bookworm that INST-15 named.

## Spec changes

* INST-15 no longer names Bookworm. It requires a fresh Pi OS Lite 64-bit image and records
  that Trixie (Debian 13) was tested on 2026-10-10 and Bookworm is untested. INST-01 still
  sets the floor at Debian 12, so Bookworm stays supported.
* The host rows in `README.md` and `00-overview.md` add "tested on Trixie".
* `docs/acceptance/README.md` "Before you start" says Trixie is what was tested, adds making
  the journal persistent and shutting down with `sudo poweroff` before pulling power. The
  record template gains an "Incidental observations" table and a note to mark steps
  `not run` and say when a run is partial.
* The README quick start notes `sudo poweroff` before pulling power.

`09-service.md` still calls systemd 252 (Bookworm) the target. That is the lowest version
the unit has to work on, so it stays.

## Candidate follow-ups

Seen during the run, with no fix and no decision taken:

1. The installer's closing hint says to edit the env file even on a reinstall, where the
   file is kept.
2. `recovery.json does not exist` is logged at WARNING on every fresh start, where there is
   nothing to recover.
3. python-can logs `Created a socket` at INFO and `Could not access SocketCAN device` at
   ERROR on every reopen retry, so a DEAD service adds an ERROR line every minute.
4. After a replug the service waits for the next 60 s DEAD retry instead of reopening when
   the link reappears (link up 14:38:53, reopen 14:39:10).
5. A 2 s link blip produced 38 alert `off → unknown → off` lines.
6. The MQTT disconnect line is logged twice.

## Risks

* Bookworm has not been run on hardware. INST-01's version check still accepts it, so a
  regression there would only show on a future run on that image.
