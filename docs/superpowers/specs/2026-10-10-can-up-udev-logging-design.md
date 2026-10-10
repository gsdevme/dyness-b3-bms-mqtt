# dyness-b3-bms-mqtt — `can-up` logging under udev

* **Date:** 2026-10-10
* **Status:** accepted
* **Scope:** the `log` function of the `can-up` root helper (`packaging/can-up`, SVC-14),
  and the spec text on where udev-run helper output goes (SVC-20, SVC-22)

## Hardware observation

On a cold boot of the target Pi Zero 2 W, the udev rule (`80-dyness-b3-can.rules`,
`RUN+=…/can-up $name`) configured `can0` correctly, including the retry without
`restart-ms` from `2026-10-10-can-up-restart-ms-design.md`. Nothing from that run reached the
journal: `journalctl -b | grep can-up` was empty.

`can-up` logged only to stderr. Under the unit's `ExecStartPre=+` stderr is the journal, but
udev discards the output of `RUN+=` programs unless udev itself runs in debug mode. So
boot-time configuration, and every run after a replug or after `recover`'s USB reset (which
also goes through udev), left no trace. That loses acceptance evidence and the first thing to
look at when a link comes back misconfigured.

## Decision

`log` writes each line to stderr with the `can-up: ` prefix, as before. When
`$JOURNAL_STREAM` is unset or empty and `logger` is on `PATH`, it also pipes the same lines,
without the prefix, to `logger -t can-up -p daemon.info`. Those runs then show in
`journalctl -t can-up`.

* **`JOURNAL_STREAM` as the test.** systemd sets it for every process of a unit whose
  stdout or stderr is connected to the journal, including `ExecStartPre=` and the `can-up`
  that `recover` runs from `ExecStopPost=`. udev does not set it. Testing it avoids a second
  copy of each line under the unit, with no configuration and no guess about the caller.
* **`logger` guarded with `command -v`.** It ships in `bsdutils` (essential on Debian), so
  it is always there on the Pi; the guard keeps the helper working if it is not. Its own
  errors (no `/dev/log`, say) are discarded, because `can-up` must still exit 0 and stderr is
  still written.
* **No prefix to `logger`.** The tag carries the name, and `journalctl` prints it, so a
  prefix would show as `can-up: can-up: …`.
* **`daemon.info`.** These are status and failure notes from a system helper; failures are
  already handled by the service's bus open, so no line warrants a higher priority.

## Why stderr is kept

stderr is what the unit's journal stream and the tests read, and SVC-22 requires the prefix
there. Under the unit it is the only channel, so it cannot go. Under udev it costs nothing,
and an operator running `can-up` by hand on a terminal still sees the lines.

## `recover` is unchanged

`recover` runs only from the unit's `ExecStopPost=+`, where stderr is already the journal and
`JOURNAL_STREAM` is set, so the same change would never fire. Its `log` therefore no longer
matches `can-up`'s, and the test that compares the helpers' shared functions no longer
includes `log`; `unquote`, `from_file` and `setting` are still compared.

## Spec changes

* `09-service.md` SVC-14 gains step 8, "Journal outside systemd".
* SVC-20 notes that udev discards `RUN+=` output and points to `journalctl -b -t can-up`.
* SVC-22 notes that udev-run `can-up` lines reach the journal through `logger`.
* `docs/acceptance/README.md` "Before you start" names `journalctl -b -t can-up` as where
  boot-time link configuration shows up.

## Tests

`tests/packaging/test_can_up.py` adds a fake `logger` that records its argv in the fake call
log and appends its stdin to a file. `test_can_up_logs_to_journal_outside_systemd` (SVC-14,
SVC-20, SVC-22) runs the refused-`restart-ms` case with the udev argument `can0`:

* without `JOURNAL_STREAM`, every `logger` call is `logger -t can-up -p daemon.info` and the
  lines it receives are the stderr lines without the prefix;
* with `JOURNAL_STREAM` set, `logger` is never called;
* stderr is the same prefixed lines in both cases.

The existing tests run without a `logger` on the sandbox `PATH`, so they cover the guard.

## Risks

* A `can-up` run by hand from a root shell also goes to the journal. That is harmless and
  arguably useful.
* If systemd ever stopped setting `JOURNAL_STREAM`, lines under the unit would appear twice
  in the journal (once under the unit, once under the tag), not be lost.
