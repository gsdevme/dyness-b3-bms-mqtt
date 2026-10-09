# 12 — Local logging (`LOG`)

The service keeps a local history of the stack on the Pi itself: one CSV row per module and
one for the stack at a fixed interval, plus a plain-text events file, rolled per UTC day,
compressed after a while and deleted after a retention period. It exists so the weak cells
can be analysed directly (spreadsheet, pandas, `zcat | grep`) without Home Assistant's
recorder, and so the history survives a broker or Home Assistant outage. The format follows
`../dyness-debug/overnight_logger.py` (`cells-*.csv`, `events-*.log`), with columns renamed
to the MQTT JSON keys of `06-mqtt.md` so both sources read the same.

Variables referenced here (`LOCAL_LOG`, `LOCAL_LOG_INTERVAL_S`, `LOCAL_LOG_RETENTION_DAYS`,
`LOCAL_LOG_COMPRESS_AFTER_DAYS`, `MODULE_COUNT`, `PUBLISH_INTERVAL_S`) are defined, with
their defaults and validation, in `08-configuration.md`.

## 1. Location and enablement

* **LOG-01** When `LOCAL_LOG` is true and `$STATE_DIRECTORY` is set, the service MUST write
  its local history to `$STATE_DIRECTORY/logs/`, creating the directory at start-up if it
  does not exist. On the Pi that is `/var/lib/dyness-b3-bms-mqtt/logs` (`09-service.md`
  SVC-03), which `make uninstall` keeps unless `PURGE=1` is given (`10-installation.md`
  INST-08). Reading it needs `sudo`, because `DynamicUser=yes` keeps the state directory
  under the root-only `/var/lib/private/`.
* **LOG-02** When `LOCAL_LOG` is false the service MUST NOT create, open or touch anything
  under `logs/`, and the status (LOG-21) MUST be `disabled`. When `LOCAL_LOG` is true but
  `$STATE_DIRECTORY` is unset (development runs), local logging MUST be disabled in the
  same way, with one `WARNING` logged at start-up.
* **LOG-03** Local logging MUST NOT depend on MQTT: rows and events MUST be written whether
  or not the broker is connected, and whatever `HA_DISCOVERY` is set to.

## 2. Files and encoding

* **LOG-04** The service MUST write three files per UTC day, named from the UTC date of the
  `ts` of the row or line written to them:

  | File | Content |
  |---|---|
  | `modules-YYYY-MM-DD.csv` | Module history, LOG-07 |
  | `stack-YYYY-MM-DD.csv` | Stack history, LOG-08 |
  | `events-YYYY-MM-DD.log` | Events, LOG-09 |

  At the first write whose UTC date differs from that of the open file, the service MUST
  close that file and continue in the file for the new date.
* **LOG-05** Files MUST be opened in append mode and never truncated or rewritten while
  being written. A CSV file MUST get its header line (the column names in order) only when
  it is created, that is when it is absent or empty at open; an existing non-empty file is
  appended to without a new header. Files are UTF-8 with `\n` line endings; CSV files use
  `,` as the separator and quote a field only when it contains a separator, a quote or a
  line break (`csv.QUOTE_MINIMAL`).
* **LOG-06** Every CSV field MUST hold the value of the same key in the MQTT state payload
  (`06-mqtt.md` MQTT-16 or MQTT-17) built from the same snapshot, alert state and wall-clock
  time, encoded as follows: `null` as an empty field, booleans as `true` / `false`, numbers
  as their JSON text (TEL-21, so `-0.0` never appears), and strings as they are, without
  JSON quotes (LOG-05 quoting still applies). `ts` MUST be the MQTT-15 timestamp of that
  write and is the same for every row of one interval.

Column names are fixed public interface, like variable names (CFG-09): adding, removing or
renaming a column is recorded in a dated design doc, because it changes the file layout on
the next UTC day and mixes layouts within the day of the upgrade.

## 3. Module history

* **LOG-07** At every interval (LOG-11) the service MUST append one row to the modules file
  for each module 1..`MODULE_COUNT`, in module order, whether or not the module is online,
  with exactly these columns in this order:

  ```
  ts, module, online, cells_fresh,
  cell_01_mv, cell_02_mv, cell_03_mv, cell_04_mv, cell_05_mv,
  cell_06_mv, cell_07_mv, cell_08_mv, cell_09_mv, cell_10_mv,
  cell_11_mv, cell_12_mv, cell_13_mv, cell_14_mv, cell_15_mv,
  cell_min_mv, cell_min_no, cell_max_mv, cell_max_no, cell_avg_mv, spread_mv,
  pack_v, current_a, soc, soh, cycles, energy_kwh, chg_limit_a, dis_limit_a,
  temp_1_c, temp_2_c, temp_3_c, temp_mos_c, temp_board_c,
  flags_raw, bms_alarm, bms_protection
  ```

  (41 columns; the line breaks above are for reading only.) `bms_alarm` and
  `bms_protection` are the alert states of ALERT-07 and ALERT-08; the flag lists and the
  other alert keys of MQTT-16 are not logged.

## 4. Stack history

* **LOG-08** At every interval the service MUST append one row to the stack file with
  exactly these columns in this order, which are the MQTT-17 keys in table order without the
  `alert_*` keys:

  ```
  ts, modules_online, module_count_bms,
  lowest_mv, lowest_module, lowest_cell, highest_mv, highest_module, highest_cell,
  spread_mv, total_current_a, bms_current_a, avg_soc, bms_soc, bms_soh,
  voltage_v, temp_c, chg_limit_v, chg_limit_a, dis_limit_a, dis_limit_v,
  any_alarm, any_protection, imbalance_pct
  ```

  (24 columns.)

## 5. Events

* **LOG-09** The service MUST append one line to the events file for each of these events,
  when it happens rather than at the next interval: a health state transition, an alert
  change (state or active flag list, ALERT-17), a poll sent, and the exit of the process.
  Each line MUST be `<ts> <message>`, where `<ts>` is the wall-clock time of the event in
  the MQTT-15 format and `<message>` is the text of the log record for that event (CFG-11,
  ALERT-17) without the level name. For example:

  ```
  2026-10-09T07:15:30Z alert module 2 cell_low: off -> on (value=2994 mV, threshold=3000 mV)
  ```

* **LOG-10** On every exit path through which `app.run()` returns or raises after local
  logging has started (exit 0, 1, 75 and 76), the service MUST write the exit event line,
  flush and close every open local log file before the process exits. Exit 78 happens before
  local logging starts and writes nothing.

## 6. Cadence and durability

* **LOG-11** Rows MUST be written every `LOCAL_LOG_INTERVAL_S` on the monotonic clock, the
  first one interval after start-up, independently of `PUBLISH_INTERVAL_S` and of immediate
  publishes (MQTT-20). They MUST be built from the same snapshot and payload code path as
  the MQTT state payloads, so a CSV value and the published value of the same key never
  differ for the same snapshot.
* **LOG-12** The service MUST flush each file after every interval's rows and after every
  event line, and MUST NOT call `os.fsync`, `os.fdatasync` or `os.sync` on local log files:
  forcing writes on every interval would wear the SD card for no benefit. A power cut can
  therefore lose the last few seconds of history (the kernel's writeback delay).

## 7. Housekeeping

* **LOG-13** The service MUST run housekeeping once at start-up, before the first write, and
  then every 3600 s on the monotonic clock.
* **LOG-14** Housekeeping MUST consider only files in `logs/` whose name matches
  `^(modules|stack)-\d{4}-\d{2}-\d{2}\.csv(\.gz)?$` or
  `^events-\d{4}-\d{2}-\d{2}\.log(\.gz)?$`, and MUST leave every other file untouched. A
  file's age in days is today's UTC date minus the date in its name. Files with an age of 0
  or less (today's, or dated in the future after a clock change) MUST NOT be touched.
* **LOG-15** A file that is not yet compressed and whose age is greater than
  `LOCAL_LOG_COMPRESS_AFTER_DAYS` MUST be compressed with gzip to `<name>.gz`, overwriting
  any `<name>.gz` left by an interrupted run. The original MUST be deleted only after the
  `.gz` file has been completely written and closed without error.

  One run compresses at most 8 files (`MAX_COMPRESS_PER_RUN`), at gzip level 6, so a
  backlog cannot hold the main loop past the watchdog; the next hourly runs take the rest,
  and LOG-17 applies once none is left.

* **LOG-16** A file, compressed or not, whose age is greater than
  `LOCAL_LOG_RETENTION_DAYS` MUST be deleted instead of being compressed.
* **LOG-17** Housekeeping MUST be idempotent: a second run on the same UTC day after a
  successful run MUST change nothing.

With `LOCAL_LOG_COMPRESS_AFTER_DAYS` = 0, the first hourly run after midnight UTC may gzip
yesterday's file while the logger still holds it open, because a file is closed only at the
first write of the new day (LOG-04). No data is lost on Linux: everything written to it has
already been flushed, so the gzip copy reads it in full before the name is unlinked, and
nothing more is written to the old handle before it is closed.

With the default values from `08-configuration.md`, yesterday's files stay plain for direct
reading, older files are compressed, and about three months are kept. At
`LOCAL_LOG_INTERVAL_S` = 10 and `MODULE_COUNT` = 3 a day is roughly 5 MB of module rows and
1.3 MB of stack rows before compression, and compresses to well under 1 MB, so the full
retention fits in tens of MB.

## 8. Faults

* **LOG-18** Every `OSError` raised by local logging (creating the directory, opening,
  writing, flushing or closing a file, compressing or deleting during housekeeping), for
  example a full disk or a read-only filesystem, MUST be caught inside local logging. It
  MUST NOT propagate, stop or restart the service, change the health state, delay MQTT
  publishing or delay the watchdog.
* **LOG-19** On such an error the status MUST change to `error`, and on each change of
  status to `error` one `ERROR` log line MUST name the operation, the path and the error
  text. Further errors while the status is `error` MUST NOT be logged above `DEBUG`. While
  the status is `error`, rows and events MUST be dropped, not buffered.
* **LOG-20** While the status is `error`, the next housekeeping run (LOG-13) MUST close any
  open files, run housekeeping and reopen the files for the current day. When a reopened
  file does not end in `\n` (a partial line left by the failed write), the service MUST
  write a `\n` before the next row or line. If all of that succeeds the status MUST return
  to `ok` and the service MUST log `local log recovered` at `INFO`; otherwise the status
  stays `error` without a new `ERROR` line.

## 9. Status

* **LOG-21** The local logging status MUST be `disabled` as in LOG-02, `error` as in LOG-19
  and LOG-20, and `ok` otherwise, and MUST be published as `local_log` in the service state
  (`06-mqtt.md` MQTT-18), where Home Assistant shows it as a diagnostic enum sensor
  (`07-home-assistant.md` HA-12). A change of status MUST be published with the next
  periodic service state; it does not trigger an immediate publish.
* **LOG-22** **MANUAL** On the Pi, a full root filesystem MUST turn `local_log` to `error`
  while module, stack and service state keep being published, and freeing space MUST bring
  it back to `ok` at the next housekeeping run (`11-testing.md` TEST-39, step 14).
