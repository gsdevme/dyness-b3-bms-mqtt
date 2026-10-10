# 06 — MQTT (`MQTT`)

How the service connects to the broker, which topics it publishes and subscribes to, the
exact JSON of the state payloads, and when they are published. In the tables below `<B>` is
`MQTT_BASE_TOPIC`, `<P>` is `HA_DISCOVERY_PREFIX`, `<N>` is `HA_NODE_ID` and `<m>` is a
module number.

## 1. Connection

* **MQTT-01** The client MUST use paho-mqtt 2.x with
  `callback_api_version=CallbackAPIVersion.VERSION2` and protocol MQTT 3.1.1
  (`MQTTv311`). MQTT 5 is not used because 3.1.1 is supported by every broker a Home
  Assistant user is likely to run (including older Mosquitto add-ons) and nothing here needs
  a 5.0 feature; a later version MAY switch to MQTT 5.
* **MQTT-02** The client id MUST be `MQTT_CLIENT_ID`, with a clean session. When
  `MQTT_USERNAME` is set, `username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)` MUST be called
  (password `None` when unset). `MQTT_PASSWORD` MUST NOT appear in any log line.
* **MQTT-03** When `MQTT_TLS` is true the client MUST call `tls_set()` with
  `ca_certs=MQTT_CA_FILE` when set, otherwise the system CA store. Certificate and host
  name verification MUST NOT be disabled.
* **MQTT-04** The client MUST connect with `connect_async(MQTT_HOST, MQTT_PORT,
  keepalive=math.ceil(MQTT_KEEPALIVE_S))`, call
  `reconnect_delay_set(min_delay=1, max_delay=60)` and run the network loop with
  `loop_start()`, so an unreachable broker at start-up neither blocks nor fails start-up and
  paho retries with exponential back-off.
* **MQTT-05** paho callbacks (`on_connect`, `on_connect_fail`, `on_disconnect`,
  `on_message`) run on paho's thread and MUST only, under one lock, set or clear the
  `connected` event, add to the pending events (`MqttEvent.CONNECTED` on a successful
  connect, `MqttEvent.HA_ONLINE` when `<P>/status` carries `online`) and append log
  notices. The main loop MUST call `take_events()` every iteration, which logs the pending notices on the main
  thread and returns the pending events. All publishing and subscribing MUST happen on the
  main loop.
* **MQTT-06** Broker unavailability, authentication failure or any MQTT error MUST NOT stop
  CAN reading, change the health state, delay the watchdog or cause an exit. Connects and
  disconnects MUST be logged from the main loop via `take_events()` (MQTT-05, CFG-11): a
  successful connect and a disconnect (with the reason code) at `INFO`; a connect refused
  by the broker (with the reason code) and a connect that fails before any CONNACK (DNS,
  TCP, TLS) at `WARNING`.
* **MQTT-07** `mqtt_reconnects` (service payload) MUST count successful connects after the
  first since process start.

## 2. Last will, online and offline

* **MQTT-08** Before connecting, the client MUST set the will
  `<B>/status` = `offline`, retained, QoS 1.
* **MQTT-09** On every successful connect (first or reconnect) the main loop MUST, in this
  order: publish `<B>/status` = `online` (retained, QoS 1); subscribe to `<P>/status`
  (QoS 1) if `HA_DISCOVERY` is true; publish discovery (`07-home-assistant.md`) if
  `HA_DISCOVERY` is true; publish `<B>/module/<m>/availability` for every module; publish
  every module state, the stack state and the service state.
* **MQTT-10** On clean stop (SIGTERM) the service MUST publish `<B>/status` = `offline`
  (retained, QoS 1), wait up to 2 s for it to be acknowledged, then `disconnect()` and
  `loop_stop()`. It MUST NOT wait longer if the broker is unreachable. This is the
  publisher's `stop()`, the last step of the clean-stop sequence, after `bus.shutdown()`
  (`09-service.md` SVC-11). SVC-11 step 2 has already published the retained `offline`
  without waiting, so it is sent twice; the duplicate is accepted.

## 3. Topics

* **MQTT-11** The service MUST use exactly these topics:

  | Topic | Direction | Payload | Retain | QoS |
  |---|---|---|---|---|
  | `<B>/status` | Publish (and will) | `online` / `offline` | Yes | 1 |
  | `<B>/module/<m>/availability` | Publish | `online` / `offline` | Yes | 1 |
  | `<B>/module/<m>/state` | Publish | Module JSON, MQTT-16 | No | 0 |
  | `<B>/stack/state` | Publish | Stack JSON, MQTT-17 | No | 0 |
  | `<B>/service/state` | Publish | Service JSON, MQTT-18 | No | 0 |
  | `<P>/device/<N>_stack/config` | Publish | Discovery, `07-home-assistant.md` | Yes | 1 |
  | `<P>/device/<N>_m<m>/config` | Publish | Discovery, `07-home-assistant.md` | Yes | 1 |
  | `<P>/status` | Subscribe (only if `HA_DISCOVERY`) | `online` / `offline` | — | 1 |

  `<m>` MUST be the decimal module number without padding, 1..`MODULE_COUNT`. The one
  exception is the empty retained payload published to `<P>/device/<N>_m<m>/config` for
  `m` in `MODULE_COUNT`+1..4 to remove surplus devices (HA-20). State is not
  retained: a retained reading would show an old value as current after a restart;
  `expire_after` and the immediate publish after connect cover the gap.
* **MQTT-12** `<B>/module/<m>/availability` MUST be `online` when the module's `online`
  (TEL-06) is true and `offline` otherwise, and MUST be published whenever it changes
  (MQTT-20). During STALE, RECOVERING and DEAD (`03-health-and-recovery.md`, HEALTH-30)
  every module is `offline`.
* **MQTT-13** A message on `<P>/status` with payload exactly `online` MUST trigger the
  republish described in `07-home-assistant.md`. Any other payload MUST be ignored.

## 4. JSON encoding

* **MQTT-14** State payloads MUST be UTF-8 JSON objects encoded compactly
  (`separators=(",", ":")`), with sorted keys, `ensure_ascii=False` and `allow_nan=False`.
  Missing or stale values MUST be `null`. Booleans MUST be JSON `true`/`false`.
* **MQTT-15** Every key in the schemas below MUST be present in every payload of that kind,
  with `null` where the value is unknown; keys MUST NOT be added or omitted dynamically.
  `ts` MUST be the wall-clock time the payload was built, ISO 8601 UTC with second
  precision and a `Z` suffix (`2026-10-09T07:15:30Z`), from an injected wall clock.

### 4.1 Module state

* **MQTT-16** `<B>/module/<m>/state` MUST have exactly these keys. "Field" refers to
  `ModuleSnapshot` (`04-telemetry.md`) unless another source is named.

  | Key | JSON type | Unit | `null` when | Source |
  |---|---|---|---|---|
  | `module` | integer | — | never | `module` |
  | `name` | string | — | never | `MODULE_NAMES` entry for `m`, else `Module <m>` |
  | `ts` | string | — | never | MQTT-15 |
  | `online` | boolean | — | never | `online` |
  | `cells_fresh` | boolean | — | never | `cells_fresh` |
  | `bcast_age_s` | number | s | never received | `bcast_age_s` |
  | `cell_age_s` | number | s | never received | `cell_age_s` |
  | `cell_01_mv` … `cell_15_mv` | integer | mV | cells stale or invalid (TEL-23) | `cells_mv[0..14]` |
  | `cell_min_mv` | integer | mV | stale | `cell_min_mv` |
  | `cell_min_no` | integer 1..15 | — | stale or out of range | `cell_min_no` |
  | `cell_max_mv` | integer | mV | stale | `cell_max_mv` |
  | `cell_max_no` | integer 1..15 | — | stale or out of range | `cell_max_no` |
  | `cell_avg_mv` | integer | mV | stale | `cell_avg_mv` |
  | `spread_mv` | integer | mV | stale | `spread_mv` |
  | `pack_v` | number | V | stale | `pack_v` |
  | `current_a` | number, + = charging | A | stale | `current_a` |
  | `soc` | integer 0..100 | % | stale | `soc` |
  | `soh` | integer 0..100 | % | stale | `soh` |
  | `cycles` | integer | — | stale | `cycles` |
  | `energy_kwh` | number | kWh | stale | `energy_kwh` |
  | `chg_limit_a` | number ≥ 0 | A | stale | `chg_limit_a` |
  | `dis_limit_a` | number ≥ 0 | A | stale | `dis_limit_a` |
  | `temp_1_c`, `temp_2_c`, `temp_3_c` | number | °C | cells stale or invalid | `temps_c[0..2]` |
  | `temp_mos_c` | number | °C | stale | `temp_mos_c` |
  | `temp_board_c` | number | °C | stale | `temp_board_c` |
  | `temp_max_c` | number | °C | stale | `temp_max_c` |
  | `temp_min_c` | number | °C | stale | `temp_min_c` |
  | `flags_raw` | string, 16 hex digits | — | stale | `flags_raw` |
  | `chg_mos_on` | boolean | — | stale | `chg_mos_on` |
  | `dis_mos_on` | boolean | — | stale | `dis_mos_on` |
  | `balancing` | boolean | — | stale | `balancing` |
  | `bms_alarm` | boolean | — | unknown | Alert `bms_alarm` (ALERT-07) |
  | `bms_alarm_flags` | array of strings | — | unknown | Its active flag list (ALERT-09) |
  | `bms_protection` | boolean | — | unknown | Alert `bms_protection` (ALERT-08) |
  | `bms_protection_flags` | array of strings | — | unknown | Its active flag list (ALERT-09) |
  | `alert_cell_spread` | boolean | — | unknown | Alert `cell_spread` (ALERT-04) |
  | `alert_cell_low` | boolean | — | unknown | Alert `cell_low` (ALERT-05) |

### 4.2 Stack state

* **MQTT-17** `<B>/stack/state` MUST have exactly these keys. "Field" refers to
  `StackSnapshot` unless another source is named.

  | Key | JSON type | Unit | `null` when | Source |
  |---|---|---|---|---|
  | `ts` | string | — | never | MQTT-15 |
  | `modules_online` | integer | — | never | `modules_online` |
  | `module_count_bms` | integer | — | stale | `module_count_bms` |
  | `lowest_mv` | integer | mV | no candidate (TEL-12) | `lowest_mv` |
  | `lowest_module` | integer | — | no candidate | `lowest_module` |
  | `lowest_cell` | integer 1..15 | — | no candidate or number unknown | `lowest_cell` |
  | `highest_mv` | integer | mV | no candidate | `highest_mv` |
  | `highest_module` | integer | — | no candidate | `highest_module` |
  | `highest_cell` | integer 1..15 | — | no candidate or number unknown | `highest_cell` |
  | `spread_mv` | integer | mV | either extreme unknown | `spread_mv` |
  | `total_current_a` | number, + = charging | A | any module current stale | `total_current_a` |
  | `bms_current_a` | number, + = charging | A | stale | `bms_current_a` |
  | `avg_soc` | integer 0..100 | % | no module SOC | `avg_soc` |
  | `bms_soc` | integer 0..100 | % | stale | `bms_soc` |
  | `bms_soh` | integer 0..100 | % | stale | `bms_soh` |
  | `voltage_v` | number | V | stale | `voltage_v` |
  | `temp_c` | number | °C | stale | `temp_c` |
  | `chg_limit_v` | number | V | stale | `chg_limit_v` |
  | `chg_limit_a` | number ≥ 0 | A | stale | `chg_limit_a` |
  | `dis_limit_a` | number ≥ 0 | A | stale | `dis_limit_a` |
  | `dis_limit_v` | number | V | stale | `dis_limit_v` |
  | `any_alarm` | boolean | — | no module flags | `any_alarm` |
  | `any_protection` | boolean | — | no module flags | `any_protection` |
  | `alert_current_imbalance` | boolean | — | unknown | Alert `current_imbalance` (ALERT-06) |
  | `imbalance_pct` | number | % | ALERT-06 | `imbalance_pct` (ALERT-06) |
  | `alert_any_cell_spread` | boolean | — | unknown | Alert `any_cell_spread` (ALERT-10) |
  | `alert_any_cell_low` | boolean | — | unknown | Alert `any_cell_low` (ALERT-10) |
  | `alert_any_bms_alarm` | boolean | — | unknown | Alert `any_bms_alarm` (ALERT-10) |
  | `alert_any_bms_protection` | boolean | — | unknown | Alert `any_bms_protection` (ALERT-10) |

### 4.3 Service state

* **MQTT-18** `<B>/service/state` MUST have exactly these keys. Their meaning is owned by
  the spec named in the source column; this table fixes the key, type and encoding.

  | Key | JSON type | Unit | `null` when | Source |
  |---|---|---|---|---|
  | `ts` | string | — | never | MQTT-15 |
  | `version` | string | — | never | Package version |
  | `health` | string: `starting`, `healthy`, `degraded`, `stale`, `recovering`, `dead` | — | never | Health state, lower case (`03-health-and-recovery.md`) |
  | `health_reason` | string | — | before the first transition (every transition carries a reason, HEALTH-18) | `03-health-and-recovery.md` |
  | `can_state` | string: `error-active`, `error-warning`, `error-passive`, `bus-off`, `stopped`, `down`, `absent` | — | state `UNKNOWN` (not queried yet, not supported by `CAN_INTERFACE`, or the query failed) | Bus-state query, lower case and hyphenated (`02-can-transport.md`) |
  | `can_berr_tx` | integer | — | unavailable | Bus-state query |
  | `can_berr_rx` | integer | — | unavailable | Bus-state query |
  | `can_restarts` | integer | — | unavailable | Kernel bus-off restarts, bus-state query |
  | `bus_age_s` | number, 1 dp | s | no frame yet | `03-health-and-recovery.md` |
  | `error_frames` | integer | — | never | CAN error frames since process start (`02-can-transport.md`) |
  | `dropped_frames` | integer | — | unavailable | Kernel RX dropped count, bus-state query |
  | `echo_frames` | integer | — | never | `ECHO` frames since process start (PROTO-04) |
  | `malformed_frames` | integer | — | never | `MALFORMED` frames since process start (PROTO-04) |
  | `unknown_frames` | integer | — | never | `UNKNOWN` frames since process start (PROTO-04) |
  | `polls_sent` | integer | — | never | Polls since process start |
  | `polling_suspended` | boolean | — | never | `03-health-and-recovery.md` |
  | `escalation` | integer | — | never (an unreadable or absent `recovery.json` counts as 0, HEALTH-25/26) | `escalation` in `recovery.json` (HEALTH-24) |
  | `service_restarts` | integer | — | never (as `escalation`) | `restarts` in `recovery.json` (HEALTH-24) |
  | `last_exit_code` | integer, or string signal name (e.g. `"ABRT"`) | — | no `last_exit.json`, or its `code` is `null` | `code` in `last_exit.json` (SVC-15) |
  | `last_exit_result` | string | — | no `last_exit.json`, or its `result` is `null` | `result` in `last_exit.json`: systemd `SERVICE_RESULT` (SVC-15) |
  | `uptime_s` | integer | s | never | Process uptime from the monotonic clock |
  | `mqtt_reconnects` | integer | — | never | MQTT-07 |
  | `local_log` | string: `ok`, `disabled`, `error` | — | never | Local logging status (`12-local-logging.md`, LOG-21) |

## 5. Cadence

* **MQTT-19** Every module state, the stack state and the service state MUST be published
  every `PUBLISH_INTERVAL_S`, measured on the monotonic clock, while connected.
* **MQTT-20** The following MUST be published immediately, outside the periodic schedule:
  * the affected module state and the stack state on an alert change (ALERT-16);
  * the module availability, that module's state and the stack state when a module's
    `online` changes;
  * the service state when `health` or `polling_suspended` changes;
  * everything listed in MQTT-09 after every connect, and the republish of
    `07-home-assistant.md` after Home Assistant reports `online`.

  An immediate publish MUST NOT reset the periodic schedule.
* **MQTT-21** While disconnected, the service MUST NOT call `publish()` and MUST NOT buffer
  or queue state. After reconnecting it publishes only the current state (latest wins).
  `max_queued_messages_set()` SHOULD be set to a small bound as a guard against paho's
  internal queue.

## 6. Size budget

* **MQTT-22** Each state payload SHOULD stay under 4 KiB. With the keys above a module state
  is about 1.3 KiB, the stack state about 0.9 KiB and the service state about 0.6 KiB; with
  `MODULE_COUNT` = 4 a full publish is under 8 KiB, negligible for the Zero 2 and any broker.
  Discovery payloads (`07-home-assistant.md`) are larger (tens of KiB per device) but are
  retained and sent rarely.

## 7. Publisher interface

* **MQTT-23** Application code MUST talk to MQTT only through this protocol, implemented by
  the paho-based client and by the test `FakePublisher` (`11-testing.md`):

  ```python
  class MqttEvent(enum.Enum):
      CONNECTED = "connected"      # a (re)connect completed
      HA_ONLINE = "ha_online"      # <P>/status == "online" received

  class Publisher(Protocol):
      def start(self) -> None: ...                 # set will, connect_async, loop_start
      def stop(self) -> None: ...                  # MQTT-10
      @property
      def connected(self) -> bool: ...
      def take_events(self) -> set[MqttEvent]: ... # returns and clears pending events
      def subscribe(self, topic: str, qos: int) -> None: ...
      def publish(self, topic: str, payload: str | bytes, *, qos: int, retain: bool) -> None: ...
  ```

  `publish()` MUST be a no-op returning immediately when not connected (MQTT-21).
