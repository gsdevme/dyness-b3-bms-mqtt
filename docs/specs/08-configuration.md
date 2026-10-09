# 08 — Configuration (`CFG`)

All configuration comes from environment variables. On the Pi they are supplied by the
systemd `EnvironmentFile=/etc/default/dyness-b3-bms-mqtt` (see `09-service.md`). There is
no config file format of its own, no CLI flags beyond `--version` and `--check-config`.

This document is the **single owner** of every variable name and default. Other specs refer
to variables by name and MUST NOT restate defaults.

## 1. General rules

* **CFG-01** The service MUST read all settings from the process environment at start-up
  into an immutable settings object. Changing the environment file takes effect on the next
  restart only.
* **CFG-02** Every variable MUST have the default listed below. An unset or empty variable
  MUST be treated as unset and take the default. (Empty therefore cannot be used to clear a
  defaulted value; for optional variables, empty means "not provided".)
* **CFG-03** Every variable MUST be validated against the type and range below. On any
  validation failure the service MUST log every failing variable with its value and reason
  (secrets redacted) and exit with status **78** (`EX_CONFIG`) without opening the CAN bus or
  the broker connection.
* **CFG-04** Boolean variables MUST accept, case-insensitively, `1/0`, `true/false`,
  `yes/no`, `on/off`. Anything else is a validation failure.
* **CFG-05** Duration variables (suffix `_S`) are decimal seconds, MUST be finite and
  `> 0` unless a range below says otherwise. Millisecond variables (suffix `_MS`) are
  integers.
* **CFG-06** The service MUST log the effective configuration at `INFO` on start-up with
  `MQTT_PASSWORD` redacted. `--check-config` MUST do the same validation and logging and
  exit 0 or 78 without starting anything else.
* **CFG-07** The service MUST NOT read any file for configuration other than
  `MQTT_CA_FILE` (when set) and the state directory described in `09-service.md`.
* **CFG-08** The repository MUST ship `packaging/dyness-b3-bms-mqtt.env`, an example file
  containing every variable, commented out, with its default and a one-line description.
  The installer copies it to `/etc/default/dyness-b3-bms-mqtt` only if that file does not
  exist (see `10-installation.md`). The installed file MUST be mode `0600`, owner `root`,
  because it may hold `MQTT_PASSWORD`.
* **CFG-09** Variable names are fixed public interface. Renaming one is a breaking change
  and MUST be recorded in a dated design doc with the old name accepted for at least one
  release with a deprecation warning.

## 2. CAN

| Variable | Type | Default | Validation / meaning |
|---|---|---|---|
| `CAN_INTERFACE` | enum | `socketcan` | One of `socketcan`, `gs_usb`, `slcan`, `virtual`. python-can interface name |
| `CAN_CHANNEL` | string | `can0` | socketcan/virtual: interface name. slcan: serial device path. gs_usb: adapter index as decimal integer |
| `CAN_BITRATE` | int | `500000` | 10000..1000000. Used by `can-up` and non-socketcan interfaces |
| `CAN_RESTART_MS` | int | `100` | 0..60000. Kernel automatic bus-off restart delay; 0 disables |
| `CAN_LISTEN_ONLY` | bool | `false` | `true`: never transmit, no polls, cell sensors only populate if something else polls |
| `CAN_USB_ID` | string | `1d50:606f` | `hhhh:hhhh` hex. USB vendor:product used by the USB-reset helper fallback when `CAN_CHANNEL` has vanished |
| `MODULE_COUNT` | int | `3` | 1..4. Modules 1..`MODULE_COUNT` are polled and published |
| `MODULE_NAMES` | string | *(unset)* | Optional. Comma list `n:Name` e.g. `1:Top,2:Middle,3:Bottom`. Each `n` MUST be in 1..`MODULE_COUNT`; names MUST be unique, 1..32 chars. Used for HA device names only |

## 3. Polling and freshness

| Variable | Type | Default | Validation / meaning |
|---|---|---|---|
| `POLL_STAGGER_S` | duration | `0.3` | 0.05..5. Gap between the initial polls of consecutive modules |
| `POLL_RETRY_S` | duration | `5` | 1..60. Gap between re-polls of a module whose cell stream is stale |
| `POLL_MAX_RETRIES` | int | `3` | 0..20. Re-polls at `POLL_RETRY_S` before falling back to `POLL_BACKOFF_S` |
| `POLL_BACKOFF_S` | duration | `60` | ≥ `POLL_RETRY_S`. Re-poll interval after retries are exhausted |
| `START_GRACE_S` | duration | `20` | 5..120. Time after bus open before module staleness is judged |
| `CELL_STALE_S` | duration | `10` | 2..300. Cell stream (`0x11`–`0x15`, `0x17`) older than this is stale |
| `MODULE_STALE_S` | duration | `10` | 2..300. Broadcast frames (`0x16`, `0x21`–`0x24`) older than this mark the module SILENT |
| `BUS_SILENCE_S` | duration | `15` | ≥ `MODULE_STALE_S`. No frame at all for this long means the bus/adapter is STALE |

## 4. Recovery

| Variable | Type | Default | Validation / meaning |
|---|---|---|---|
| `BUS_REOPEN_ATTEMPTS` | int | `2` | 0..10. In-process bus reopen attempts before exiting for external recovery |
| `RECOVERY_RESET_S` | duration | `300` | ≥ 60. Continuous HEALTHY time after which the escalation counter resets to 0 |
| `DEAD_AFTER` | int | `3` | 1..20. Escalation counter value at or above which the service enters DEAD mode instead of exiting again |
| `DEAD_RETRY_S` | duration | `60` | 10..3600. Bus reopen interval while DEAD |
| `DEAD_USB_RESET_S` | duration | `900` | ≥ `DEAD_RETRY_S`. While DEAD, exit 76 (USB reset) this often |
| `ERROR_SUSPEND_COUNT` | int | `10` | 1..1000. Error frames within `ERROR_WINDOW_S` that suspend polling for the process lifetime |
| `ERROR_WINDOW_S` | duration | `60` | 5..3600. Sliding window for the above |

## 5. MQTT

| Variable | Type | Default | Validation / meaning |
|---|---|---|---|
| `MQTT_HOST` | string | `localhost` | Non-empty |
| `MQTT_PORT` | int | `1883` | 1..65535 |
| `MQTT_USERNAME` | string | *(unset)* | Optional |
| `MQTT_PASSWORD` | string | *(unset)* | Optional. Redacted in logs. MUST NOT be set without `MQTT_USERNAME` |
| `MQTT_CLIENT_ID` | string | `dyness-b3-bms-mqtt-<hostname>` | 1..64 chars `[A-Za-z0-9_-]`. Default uses the short hostname |
| `MQTT_TLS` | bool | `false` | Enable TLS |
| `MQTT_CA_FILE` | path | *(unset)* | Optional. If set MUST exist and be readable at start-up. Only meaningful with `MQTT_TLS=true` |
| `MQTT_KEEPALIVE_S` | duration | `30` | 5..300 |
| `MQTT_BASE_TOPIC` | string | `dyness_b3` | 1..64 chars `[A-Za-z0-9_/-]`, no leading/trailing `/`, no `+`/`#` |
| `PUBLISH_INTERVAL_S` | duration | `10` | 1..300. Periodic state publish interval |

## 6. Home Assistant

| Variable | Type | Default | Validation / meaning |
|---|---|---|---|
| `HA_DISCOVERY` | bool | `true` | Publish discovery; `false` publishes state only |
| `HA_DISCOVERY_PREFIX` | string | `homeassistant` | Same charset as `MQTT_BASE_TOPIC` |
| `HA_NODE_ID` | string | `dyness_b3` | 1..32 chars `[a-z0-9_]`. Prefix of every `unique_id` and discovery object id. **Changing it creates new entities in HA** |
| `HA_DEVICE_NAME` | string | `Dyness B3` | 1..64 chars. Human name prefix for the HA devices |
| `EXPIRE_AFTER_S` | int | `0` | 0 or 10..3600. `0` = automatic: `3 × PUBLISH_INTERVAL_S + 5`, rounded up to an integer |

## 7. Alerts

| Variable | Type | Default | Validation / meaning |
|---|---|---|---|
| `ALERT_CELL_SPREAD_MV` | int | `50` | 1..1000. Per-module max−min cell voltage above which the spread alert is on |
| `ALERT_CELL_LOW_MV` | int | `3000` | 2000..3600. Any cell below this turns the low-cell alert on |
| `ALERT_CURRENT_IMBALANCE_PCT` | number | `20` | 1..100. Module current imbalance percentage (formula in `05-alerts.md`) |
| `ALERT_IMBALANCE_MIN_CURRENT_A` | number | `10` | 0..200. Imbalance is only evaluated when the absolute stack current is at least this |
| `ALERT_DEBOUNCE_S` | duration | `30` | 0..600. A condition must hold continuously this long before an alert changes state (0 = immediate) |
| `ALERT_HYSTERESIS_PCT` | number | `10` | 0..50. Percentage of the threshold by which a value must recover before a "greater than" alert (`cell_spread`, `current_imbalance`) clears |
| `ALERT_CELL_LOW_HYSTERESIS_MV` | int | `50` | 0..500. The `cell_low` alert clears only once the lowest cell is at least this many mV above `ALERT_CELL_LOW_MV` (an absolute margin; a percentage of 3000 mV would be far too wide for LFP) |

## 8. Logging

| Variable | Type | Default | Validation / meaning |
|---|---|---|---|
| `LOG_LEVEL` | enum | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive) |

* **CFG-10** Logs MUST go to stdout, one line per record, without timestamps (journald adds
  them) and without ANSI colour. Each line MUST start with the level name.
* **CFG-11** At `INFO` the service MUST log: start-up configuration, every health state
  transition, every poll sent, broker connect/disconnect, discovery (re)publish, every alert
  change, and the reason for every exit. Per-frame logging is `DEBUG` only.

## 9. Cross-variable validation

* **CFG-12** `POLL_BACKOFF_S ≥ POLL_RETRY_S`, `BUS_SILENCE_S ≥ MODULE_STALE_S`,
  `DEAD_USB_RESET_S ≥ DEAD_RETRY_S`, and (`MQTT_PASSWORD` set ⇒ `MQTT_USERNAME` set) MUST
  hold; otherwise exit 78 as in CFG-03.
* **CFG-13** `CAN_LISTEN_ONLY=true` with `CAN_INTERFACE=socketcan` relies on `can-up`
  having set the link to listen-only (see `09-service.md`); the service MUST additionally
  refuse to transmit regardless of link mode.
