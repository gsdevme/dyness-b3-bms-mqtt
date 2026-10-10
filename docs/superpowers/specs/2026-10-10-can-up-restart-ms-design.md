# dyness-b3-bms-mqtt — `can-up` on devices that refuse `restart-ms`

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** the `can-up` root helper (`packaging/can-up`, SVC-14) and the spec text that
  describes `CAN_RESTART_MS`

## Hardware observation

On the target Pi Zero 2 W (kernel `6.18.50+rpt-rpi-v8`, SH-C31A adapter on the kernel
`gs_usb` driver, service 0.1.2), `can-up` logged at service start:

```text
can-up: ip link set dev can0 type can bitrate 500000 restart-ms 100 listen-only off failed with status 2
can-up: Error: Device doesn't support restart from Bus Off.
```

The kernel rejects the whole netlink request, so the bitrate and listen-only settings in the
same command were not applied either. `ip link set dev can0 up` then ran and the link came
up only because it still held the bitrate from an earlier session. `ip -d link show can0`
showed `restart-ms 0`.

## Consequences before the fix

1. On a fresh boot the link has no bitrate yet, so it comes up without one or `up` fails,
   and the service has no bus.
2. `is_correct` compared `restart-ms` with `CAN_RESTART_MS`. On this device it reads `0`
   forever, so every service start (and every udev run) took the link down and up again.

## Decision

1. **Retry without `restart-ms`.** When the combined `type can bitrate … restart-ms …
   listen-only …` command fails and `CAN_RESTART_MS` is not `0`, `can-up` retries it once
   with only `bitrate` and `listen-only`. The original failure stays logged by `run`. When
   the retry succeeds it logs one line,
   `can0 does not support automatic bus-off restart; CAN_RESTART_MS=100 ignored`, and its
   success line reports `restart-ms 0`. With `CAN_RESTART_MS=0` there is no retry and the
   behaviour is unchanged.

   The retry is triggered by any failure, not by matching the error text. The message is
   kernel extack text and may change, and the retry differs from the first command only by
   `restart-ms`, so its success is proof that `restart-ms` was the problem. A genuine
   failure (a rejected bitrate, say) fails twice, is logged twice, and the helper still
   runs `up` and exits 0 as before.

2. **`is_correct` accepts `restart-ms 0`.** The link counts as correct when `restart-ms` is
   `CAN_RESTART_MS` or `0`, with the other settings matching.

   Trade-off: `can-up` keeps no state, so it cannot tell a device that refused restart from
   a supporting device whose `restart-ms` was set to `0` by hand; the latter is left alone
   too. That only loses the kernel's automatic restart, which the health machine already
   backs up (below), and it cannot cause the bounce on every start. Probing support (a
   trial `restart-ms` set on a down link, or a state file) was rejected: a probe would need
   to take the link down to find out, which is the bounce being removed, and a state file
   adds a write path to a root helper for no change in outcome.

## Bus-off without kernel restart

On such a device kernel restarts never occur and `restarts` stays 0. A bus-off silences the
bus; the health machine treats it as silence, `ReopenBus` from `STALE` and then exit 75,
whose link bounce by `recover` (`can-up` after `ip link set … down`) restarts the
controller. `02-can-transport.md` CAN-31 and `03-health-and-recovery.md` HEALTH-22 say so.

## Spec changes

* `09-service.md` SVC-14 step 4 accepts `restart-ms 0`; step 5 adds the retry command to the
  code fence and the retry and logging rule.
* `08-configuration.md` `CAN_RESTART_MS` row: ignored, with one logged line, on devices that
  refuse automatic restart.
* `02-can-transport.md` CAN-31 and `03-health-and-recovery.md` HEALTH-22: bus-off on such
  devices goes through the escalation ladder to the exit 75 link bounce.

## Tests

`tests/packaging/helpers.py` gains `Sandbox.script_when`, which makes a fake reply
differently to calls containing a given argument, so one `ip` call can fail while the others
succeed. In `tests/packaging/test_can_up.py` (SVC-14):

* `test_can_up_retries_without_restart_ms_when_refused`: the combined command fails with
  the observed error and status 2; the retry and `up` are issued and the exact log lines,
  including the one-line notice, are asserted.
* `test_can_up_accepts_restart_ms_0_when_refused`: a link already up at `restart-ms 0` with
  the right bitrate and listen-only mode is left untouched (only the `show` call).
* `test_can_up_genuine_configure_failure`: a rejected bitrate fails both commands with no
  notice and no success line; with `CAN_RESTART_MS=0` there is no retry.
* `test_can_up_configures_in_order` now uses `restart-ms 200` for its mismatch case, since
  `0` is accepted. The all-commands-fail cases in `test_can_up.py` and `test_recover.py`
  expect the retry call.

## Risks

* A supporting device left at `restart-ms 0` by hand is not corrected by `can-up`. Bus-off
  recovery then relies on the escalation ladder, as on a refusing device.
