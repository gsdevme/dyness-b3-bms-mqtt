# dyness-b3-bms-mqtt — implementation amendments

* **Date:** 2026-10-09
* **Status:** accepted
* **Scope:** spec gaps found while planning the implementation, and the new local history
  log (`docs/specs/12-local-logging.md`)

Planning the implementation (`docs/superpowers/plans/2026-10-09-dyness-b3-bms-mqtt-implementation.md`)
against the initial specs turned up a few places where two specs disagreed, where a
requirement could not be classified by the traceability parser, or where a test seam or CI
detail was missing. It also added one feature: a local CSV history on the Pi. This record
lists each change and why it was made. The initial design record
(`2026-10-09-dyness-b3-bms-mqtt-design.md`) is unchanged.

## 1. Changes to existing specs

### 1.1 Where the transmitted `can.Message` is built (PROTO-30, CAN-02, CAN-19, CAN-20)

PROTO-30 said no module other than `protocol.py` may construct a `can.Message` for sending,
while CAN-20 told `send_poll` in `canbus.py` to construct one. Both cannot hold.

Resolution: `protocol.py` produces only the immutable `PollFrame` (PROTO-25) and never a
`can.Message`. The only `can.Message(` construction and the only python-can `send` call in
the package are inside `PythonCanTransport.send_poll`, which builds the message from the
`PollFrame` it got from `build_poll`. CAN-02 now names the python-can implementation
`PythonCanTransport` so the static check in PROTO-30 has a fixed target.

Rejected: building the `can.Message` in `protocol.py`. It would make the pure protocol
module import python-can for transmission, and the transport would still have to call
`send`, so the TX path would be split across two modules instead of being in one place a
reviewer can read.

### 1.2 `MUST` in the lead sentence (ALERT-06, ALERT-11, TEL-04, TEST-05 note)

The TEST-05 parser ends a requirement's text at the first blank line or table row. ALERT-06,
ALERT-11 and TEL-04 each start with a lead sentence that has no `MUST`, followed by a table
or code block (ALERT-06's `MUST`s sit in the bullets after its formula; ALERT-11 and TEL-04
had none), so the parser would classify them as `should` and the suite would not demand a
test for them. Their lead sentences now carry the `MUST`, so they are classified `must` as
intended; what they require is unchanged. A note
under TEST-05 states the authoring rule so later edits do not reintroduce the problem.

Rejected: making the parser read past blank lines. Blank lines are the only reliable end
marker inside a bullet list, and extending the text would let a requirement swallow the
explanation that follows it.

### 1.3 `ENV_FILE` helper test seam (SVC-14, SVC-18, TEST-19)

`can-up` and `recover` read unset CAN settings from `/etc/default/dyness-b3-bms-mqtt`.
Tests run unprivileged and cannot write there, so the path is now overridable with
`ENV_FILE`, a test-only helper input like `SYSFS_ROOT` and `DEV_ROOT`. It is not service
configuration and is not in `08-configuration.md`.

### 1.4 CI triggers and the Mosquitto image (TEST-34, TEST-35)

TEST-34 now names the events: every `push` to any branch and every `pull_request` targeting
`main`. TEST-35 fixes the Mosquitto job's service container to `eclipse-mosquitto:1.6`:
Mosquitto 2.x refuses anonymous remote connections unless given a configuration file, and a
GitHub Actions service container starts before the checkout, so it cannot be given one. 1.6
accepts anonymous connections by default and speaks MQTT 3.1.1, which is all MQTT-01 needs.

Rejected: starting Mosquitto 2.x with `docker run` in a step after checkout. It works but
adds start-up and readiness handling to the workflow for no gain in what the tests check.

### 1.5 State directory contents (SVC-03)

SVC-03 listed what the state directory holds; it now also lists `logs/` (section 2).

## 2. Local history logging (new `12-local-logging.md`, prefix `LOG`)

### 2.1 Why

MQTT and Home Assistant's recorder are the main consumers, but the weak cells in modules 2
and 3 are best studied with the raw time series: the `../dyness-debug` analysis was done on
CSV files from `overnight_logger.py`. A local history on the Pi keeps that workflow
available without exporting from Home Assistant, survives broker and Home Assistant outages,
and does not depend on the recorder's retention or downsampling.

### 2.2 Decisions

* **Format.** CSV per UTC day for modules and for the stack, plus a plain-text events file,
  following `overnight_logger.py`. Column names are the MQTT JSON keys and values are built
  by the same payload code, so the CSV and MQTT can be joined on `ts` and never disagree.
  The raw-frame log of `overnight_logger.py` is not kept: at roughly 60 frames/s it would
  be a few hundred MB a day, which an SD card should not absorb continuously, and the
  protocol work it served is done.
* **Location.** `$STATE_DIRECTORY/logs`, so it lives under systemd's `StateDirectory=` with
  the rest of the service's state, needs no extra permissions under `DynamicUser=yes`, and
  is kept or purged by `make uninstall` exactly like the other state (INST-08).
* **Cadence.** `LOCAL_LOG_INTERVAL_S` is separate from `PUBLISH_INTERVAL_S` so the history
  can be denser or sparser than what is sent to the broker.
* **SD card wear.** Files are flushed after each write so a crash loses nothing that the
  kernel already has, but never `fsync`ed: forcing a physical write every 10 s would wear
  the card, and a power cut loses at most the kernel's writeback delay worth of history.
* **Housekeeping.** Hourly and at start-up, gzip by age and delete after a retention period.
  Age is taken from the date in the file name rather than the modification time, so it is
  deterministic in tests and unaffected by `touch` or copies. Today's files are never
  touched, so housekeeping cannot race the writer.
* **Faults.** A full or read-only SD card must not take monitoring down with it. Every
  `OSError` is contained in the logger, reported at `ERROR` once each time the status turns
  to `error` and through the new `local_log` service field (`ok` / `disabled` / `error`),
  and retried at the next housekeeping run, which may itself have freed space. A partial
  row left by a failed write is closed with a newline on reopen so the CSV stays parseable.
* **Home Assistant.** `local_log` is a diagnostic enum sensor enabled by default, because
  it is one of the few signals that says whether the service is doing its job; a silently
  broken history would otherwise go unnoticed until it was needed.

### 2.3 Configuration

Four new variables in `08-configuration.md` §9: `LOCAL_LOG`, `LOCAL_LOG_INTERVAL_S`,
`LOCAL_LOG_RETENTION_DAYS` and `LOCAL_LOG_COMPRESS_AFTER_DAYS`, with the cross-variable rule
`LOCAL_LOG_COMPRESS_AFTER_DAYS ≤ LOCAL_LOG_RETENTION_DAYS` added to CFG-12. The former §9
"Cross-variable validation" is now §10. Logging is on by default because the Pi has the
space and the history is the main reason for running the service near the batteries.

### 2.4 Knock-on changes

* `00-overview.md`: scope bullet, `LOG` prefix in §4.1, document index entry in §5.
* `06-mqtt.md` MQTT-18: `local_log` key.
* `07-home-assistant.md` HA-12 and HA-13: `local_log` entity and its enum options; six
  service entities are now enabled by default instead of five.
* `11-testing.md`: a `LOG` row in the TEST-27 matrix and hardware acceptance steps 13
  (housekeeping) and 14 (disk full) in TEST-39.

## 3. Risks

* Changing a column later changes the file layout from the next file on, and mixes layouts
  within the day of the upgrade. Column names are therefore treated as public interface,
  like variable names, and a change needs its own dated design doc.
* Compressing a day's module file (a few MB) runs on the main loop and takes about a second
  on a Pi Zero 2. That is far inside `WatchdogSec` and the kernel's CAN receive buffer, but
  a much shorter `LOCAL_LOG_INTERVAL_S` or larger stack would make it longer; hardware
  acceptance step 13 observes it.
