# 07 — Home Assistant (`HA`)

Home Assistant MQTT discovery for the stack, its modules and the service itself, using
device-based discovery (one config message per device, Home Assistant 2024.11 or newer).
In this document `<B>` is `MQTT_BASE_TOPIC`, `<P>` is `HA_DISCOVERY_PREFIX`, `<N>` is
`HA_NODE_ID` and `<m>` is a module number. Topics, QoS and the state JSON are owned by
`06-mqtt.md`.

Discovery keys are written out in full here (`device`, `components`, `platform`,
`unique_id`, …) for readability. Home Assistant accepts either the full names or their
documented abbreviations (`dev`, `cmps`, `p`, `uniq_id`, …); the implementation MAY use
either, but MUST NOT mix both spellings of the same key in one payload.

## 1. Enabling

* **HA-01** When `HA_DISCOVERY` is false the service MUST NOT publish anything under `<P>`
  (including removals) and MUST NOT subscribe to `<P>/status`. State and availability are
  still published as in `06-mqtt.md`.
* **HA-02** The discovery builder MUST be a pure function of the settings and the package
  version, returning `(topic, payload)` pairs, so its output can be compared against golden
  files (`11-testing.md`).

## 2. Devices

* **HA-03** The service MUST publish one stack device and one device per module
  1..`MODULE_COUNT`:

  | Device | Discovery topic | `identifiers` | `name` | `manufacturer` | `model` | `via_device` |
  |---|---|---|---|---|---|---|
  | Stack | `<P>/device/<N>_stack/config` | `["<N>_stack"]` | `<HA_DEVICE_NAME> Stack` | `Dyness` | `B3 ×<MODULE_COUNT>` | — |
  | Module `m` | `<P>/device/<N>_m<m>/config` | `["<N>_m<m>"]` | `<HA_DEVICE_NAME> Module <m>`, or `<HA_DEVICE_NAME> <name>` when `MODULE_NAMES` names module `m` | `Dyness` | `B3` | `<N>_stack` |

  `×` is U+00D7. The service diagnostics (section 5) are components of the stack device.
  No `sw_version` or `serial_number` is set: the B3 reports neither (see
  `../dyness-debug/docs/protocol.md`), and the service version is not the battery's.
* **HA-04** Discovery messages MUST be published retained with QoS 1, the stack device
  first and then modules in ascending order.
* **HA-05** Every discovery payload MUST contain `origin` with `name`
  `dyness-b3-bms-mqtt`, `sw_version` the package version and `support_url` the project
  repository URL.
* **HA-06** Each discovery payload MUST set these shared options at the root, inherited by
  every component unless the component overrides them: `state_topic` (module:
  `<B>/module/<m>/state`, stack: `<B>/stack/state`), `availability` (HA-14),
  `availability_mode` (module devices only) and `qos` 0. **UNVERIFIED** Home Assistant's
  documentation shows `state_topic` and `qos` as shared root options of device-based
  discovery; inheritance of `availability` from the root has not been confirmed. If
  testing against Home Assistant shows it is not inherited, `availability` and
  `availability_mode` MUST be repeated in every component instead.

Example: module 2 **with the default values** (`MQTT_BASE_TOPIC`, `HA_NODE_ID`,
`HA_DEVICE_NAME`, `PUBLISH_INTERVAL_S`, `EXPIRE_AFTER_S` automatic), abbreviated to four of
its components:

```json
{
  "device": {
    "identifiers": ["dyness_b3_m2"],
    "name": "Dyness B3 Module 2",
    "manufacturer": "Dyness",
    "model": "B3",
    "via_device": "dyness_b3_stack"
  },
  "origin": {
    "name": "dyness-b3-bms-mqtt",
    "sw_version": "0.1.0",
    "support_url": "https://github.com/gsdevme/dyness-b3-bms-mqtt"
  },
  "state_topic": "dyness_b3/module/2/state",
  "availability": [
    {"topic": "dyness_b3/status", "payload_available": "online", "payload_not_available": "offline"},
    {"topic": "dyness_b3/module/2/availability", "payload_available": "online", "payload_not_available": "offline"}
  ],
  "availability_mode": "all",
  "qos": 0,
  "components": {
    "cell_08_mv": {
      "platform": "sensor",
      "unique_id": "dyness_b3_m2_cell_08_mv",
      "name": "Cell 8 voltage",
      "device_class": "voltage",
      "unit_of_measurement": "mV",
      "state_class": "measurement",
      "suggested_display_precision": 0,
      "value_template": "{{ value_json.cell_08_mv }}",
      "expire_after": 35
    },
    "balancing": {
      "platform": "binary_sensor",
      "unique_id": "dyness_b3_m2_balancing",
      "name": "Balancing",
      "device_class": "running",
      "payload_on": "ON",
      "payload_off": "OFF",
      "value_template": "{{ None if value_json.balancing is none else ('ON' if value_json.balancing else 'OFF') }}",
      "expire_after": 35
    },
    "bms_alarm": {
      "platform": "binary_sensor",
      "unique_id": "dyness_b3_m2_bms_alarm",
      "name": "BMS alarm",
      "device_class": "problem",
      "payload_on": "ON",
      "payload_off": "OFF",
      "value_template": "{{ None if value_json.bms_alarm is none else ('ON' if value_json.bms_alarm else 'OFF') }}",
      "json_attributes_topic": "dyness_b3/module/2/state",
      "json_attributes_template": "{{ {'active_flags': value_json.bms_alarm_flags} | tojson }}",
      "expire_after": 35
    },
    "bcast_age_s": {
      "platform": "sensor",
      "unique_id": "dyness_b3_m2_bcast_age_s",
      "name": "Broadcast age",
      "device_class": "duration",
      "unit_of_measurement": "s",
      "state_class": "measurement",
      "entity_category": "diagnostic",
      "enabled_by_default": false,
      "suggested_display_precision": 1,
      "value_template": "{{ value_json.bcast_age_s }}",
      "expire_after": 35
    }
  }
}
```

## 3. Entities

* **HA-07** Every component key MUST equal the JSON key it reads (`06-mqtt.md`), and its
  `unique_id` MUST be `<N>_m<m>_<key>` on a module device and `<N>_stack_<key>` on the stack
  device (service components included). `object_id` / `default_entity_id` MUST NOT be set,
  so entity ids follow the device and entity names.
* **HA-08** Entity `name` is the entity's own name; Home Assistant prefixes the device
  name (MQTT entities always have `has_entity_name` behaviour). Names MUST be sentence case,
  MUST NOT contain the device name or the unit, and MUST number cells and sensors without
  padding ("Cell 8 voltage", not "Cell 08 voltage"). The same quantity MUST have the same
  name on every device ("Cell spread", "Lowest cell voltage", "BMS alarm"), so dashboards
  and entity ids are predictable.
* **HA-09** Each component MUST have exactly the options given by its row in the tables of
  section 4 and 5: `platform`, `unique_id`, `name`, `device_class`, `unit_of_measurement`,
  `state_class`, `entity_category`, `suggested_display_precision` and `enabled_by_default`
  (omitted where the table shows "—", and `enabled_by_default` omitted when "yes"), the
  `value_template` given by its template code (below), `expire_after` (HA-16), and for
  `binary_sensor` `payload_on` = `ON` and `payload_off` = `OFF`.

Template codes: **S** is `{{ value_json.<key> }}`; **B** is
`{{ None if value_json.<key> is none else ('ON' if value_json.<key> else 'OFF') }}`;
**B+** is **B** plus the JSON attributes of HA-17. Precision "—" means the option is
omitted.

## 4. Module and stack entities

* **HA-10** Each module device MUST have exactly these components:

  | Key | Name | Platform | Device class | Unit | State class | Category | Precision | Template | Enabled |
  |---|---|---|---|---|---|---|---|---|---|
  | `cell_01_mv` … `cell_15_mv` | Cell 1 voltage … Cell 15 voltage | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `cell_min_mv` | Lowest cell voltage | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `cell_min_no` | Lowest cell | sensor | — | — | — | — | — | S | yes |
  | `cell_max_mv` | Highest cell voltage | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `cell_max_no` | Highest cell | sensor | — | — | — | — | — | S | yes |
  | `cell_avg_mv` | Average cell voltage | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `spread_mv` | Cell spread | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `pack_v` | Pack voltage | sensor | voltage | V | measurement | — | 2 | S | yes |
  | `current_a` | Current | sensor | current | A | measurement | — | 1 | S | yes |
  | `soc` | State of charge | sensor | battery | % | measurement | — | 0 | S | yes |
  | `soh` | State of health | sensor | — | % | measurement | — | 0 | S | yes |
  | `cycles` | Cycles | sensor | — | — | total_increasing | — | 0 | S | yes |
  | `energy_kwh` | Lifetime energy throughput | sensor | energy | kWh | total_increasing | — | 2 | S | yes |
  | `chg_limit_a` | Charge current limit | sensor | current | A | measurement | — | 1 | S | yes |
  | `dis_limit_a` | Discharge current limit | sensor | current | A | measurement | — | 1 | S | yes |
  | `temp_1_c` … `temp_3_c` | Temperature 1 … Temperature 3 | sensor | temperature | °C | measurement | — | 1 | S | yes |
  | `temp_mos_c` | MOS temperature | sensor | temperature | °C | measurement | — | 1 | S | yes |
  | `temp_board_c` | Board temperature | sensor | temperature | °C | measurement | — | 1 | S | yes |
  | `temp_max_c` | Highest temperature | sensor | temperature | °C | measurement | — | 1 | S | yes |
  | `temp_min_c` | Lowest temperature | sensor | temperature | °C | measurement | — | 1 | S | yes |
  | `chg_mos_on` | Charge MOS | binary_sensor | power | — | — | — | — | B | yes |
  | `dis_mos_on` | Discharge MOS | binary_sensor | power | — | — | — | — | B | yes |
  | `balancing` | Balancing | binary_sensor | running | — | — | — | — | B | yes |
  | `bms_alarm` | BMS alarm | binary_sensor | problem | — | — | — | — | B+ | yes |
  | `bms_protection` | BMS protection | binary_sensor | problem | — | — | — | — | B+ | yes |
  | `alert_cell_spread` | Cell spread alert | binary_sensor | problem | — | — | — | — | B | yes |
  | `alert_cell_low` | Low cell alert | binary_sensor | problem | — | — | — | — | B | yes |
  | `cells_fresh` | Cell data fresh | binary_sensor | — | — | — | diagnostic | — | B | yes |
  | `flags_raw` | BMS flags raw | sensor | — | — | — | diagnostic | — | S | yes |
  | `bcast_age_s` | Broadcast age | sensor | duration | s | measurement | diagnostic | 1 | S | no |
  | `cell_age_s` | Cell data age | sensor | duration | s | measurement | diagnostic | 1 | S | no |

* **HA-11** The stack device MUST have exactly these components (plus section 5):

  | Key | Name | Platform | Device class | Unit | State class | Category | Precision | Template | Enabled |
  |---|---|---|---|---|---|---|---|---|---|
  | `lowest_mv` | Lowest cell voltage | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `lowest_module` | Lowest cell module | sensor | — | — | — | — | — | S | yes |
  | `lowest_cell` | Lowest cell | sensor | — | — | — | — | — | S | yes |
  | `highest_mv` | Highest cell voltage | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `highest_module` | Highest cell module | sensor | — | — | — | — | — | S | yes |
  | `highest_cell` | Highest cell | sensor | — | — | — | — | — | S | yes |
  | `spread_mv` | Cell spread | sensor | voltage | mV | measurement | — | 0 | S | yes |
  | `total_current_a` | Current | sensor | current | A | measurement | — | 1 | S | yes |
  | `bms_current_a` | BMS current | sensor | current | A | measurement | — | 1 | S | yes |
  | `avg_soc` | Average module state of charge | sensor | battery | % | measurement | — | 0 | S | yes |
  | `bms_soc` | State of charge | sensor | battery | % | measurement | — | 0 | S | yes |
  | `bms_soh` | State of health | sensor | — | % | measurement | — | 0 | S | yes |
  | `voltage_v` | Voltage | sensor | voltage | V | measurement | — | 2 | S | yes |
  | `temp_c` | Temperature | sensor | temperature | °C | measurement | — | 1 | S | yes |
  | `chg_limit_v` | Charge voltage limit | sensor | voltage | V | measurement | — | 1 | S | yes |
  | `chg_limit_a` | Charge current limit | sensor | current | A | measurement | — | 1 | S | yes |
  | `dis_limit_a` | Discharge current limit | sensor | current | A | measurement | — | 1 | S | yes |
  | `dis_limit_v` | Discharge voltage limit | sensor | voltage | V | measurement | — | 1 | S | yes |
  | `modules_online` | Modules online | sensor | — | — | measurement | — | 0 | S | yes |
  | `imbalance_pct` | Current imbalance | sensor | — | % | measurement | — | 1 | S | yes |
  | `alert_current_imbalance` | Current imbalance alert | binary_sensor | problem | — | — | — | — | B | yes |
  | `alert_any_cell_spread` | Cell spread alert | binary_sensor | problem | — | — | — | — | B | yes |
  | `alert_any_cell_low` | Low cell alert | binary_sensor | problem | — | — | — | — | B | yes |
  | `alert_any_bms_alarm` | BMS alarm | binary_sensor | problem | — | — | — | — | B | yes |
  | `alert_any_bms_protection` | BMS protection | binary_sensor | problem | — | — | — | — | B | yes |
  | `module_count_bms` | BMS module count | sensor | — | — | — | diagnostic | — | S | yes |

## 5. Service diagnostics

* **HA-12** The stack device MUST also carry these service components. Each MUST override
  `state_topic` with `<B>/service/state` and MUST have `entity_category` `diagnostic`.
  Only the five that indicate whether the service and bus are working are enabled by
  default; the rest are counters for troubleshooting and are disabled to keep the recorder
  quiet.

  | Key | Name | Platform | Device class | Unit | State class | Precision | Template | Enabled |
  |---|---|---|---|---|---|---|---|---|
  | `health` | Health | sensor | enum | — | — | — | S | yes |
  | `health_reason` | Health reason | sensor | — | — | — | — | S | yes |
  | `can_state` | CAN state | sensor | enum | — | — | — | S | yes |
  | `bus_age_s` | Bus age | sensor | duration | s | measurement | 1 | S | yes |
  | `polling_suspended` | Polling suspended | binary_sensor | problem | — | — | — | B | yes |
  | `can_berr_tx` | CAN TX error counter | sensor | — | — | measurement | 0 | S | no |
  | `can_berr_rx` | CAN RX error counter | sensor | — | — | measurement | 0 | S | no |
  | `can_restarts` | CAN bus-off restarts | sensor | — | — | total_increasing | 0 | S | no |
  | `error_frames` | CAN error frames | sensor | — | — | total_increasing | 0 | S | no |
  | `dropped_frames` | Dropped frames | sensor | — | — | total_increasing | 0 | S | no |
  | `echo_frames` | Echoed poll frames | sensor | — | — | total_increasing | 0 | S | no |
  | `malformed_frames` | Malformed frames | sensor | — | — | total_increasing | 0 | S | no |
  | `unknown_frames` | Unknown frames | sensor | — | — | total_increasing | 0 | S | no |
  | `polls_sent` | Polls sent | sensor | — | — | total_increasing | 0 | S | no |
  | `escalation` | Recovery escalation | sensor | — | — | measurement | 0 | S | no |
  | `service_restarts` | Service restarts | sensor | — | — | total_increasing | 0 | S | no |
  | `last_exit_code` | Last exit code | sensor | — | — | — | — | S | no |
  | `last_exit_result` | Last exit result | sensor | — | — | — | — | S | no |
  | `uptime_s` | Uptime | sensor | duration | s | — | 0 | S | no |
  | `mqtt_reconnects` | MQTT reconnects | sensor | — | — | total_increasing | 0 | S | no |
  | `version` | Version | sensor | — | — | — | — | S | no |

* **HA-13** The `enum` sensors MUST list `options` exactly as the value sets in MQTT-18:
  `health` → `starting`, `healthy`, `degraded`, `stale`, `recovering`, `dead`;
  `can_state` → `error-active`, `error-warning`, `error-passive`, `bus-off`, `stopped`,
  `down`, `absent`.

### 5.1 Not exposed as entities

Every key of `06-mqtt.md` appears in sections 4 and 5 except:

| Payload | Key | Why |
|---|---|---|
| Module | `module`, `name` | Identity; carried by the device |
| Module | `ts` | Timestamp of the message; `expire_after` covers liveness |
| Module | `online` | Already the module availability topic (HA-14) |
| Module | `bms_alarm_flags`, `bms_protection_flags` | Attributes of `bms_alarm` / `bms_protection` (HA-17) |
| Stack | `ts` | As above |
| Stack | `any_alarm`, `any_protection` | Raw roll-ups (TEL-17) covered by `alert_any_bms_alarm` / `alert_any_bms_protection` (the latter also counts `fault` bits, ALERT-08, whereas `any_protection` counts `cut` bits only); kept in the payload for non-HA consumers |
| Service | `ts` | As above |

## 6. Availability and expiry

* **HA-14** Module device components MUST use `availability` =
  [`<B>/status`, `<B>/module/<m>/availability`] with `availability_mode` `all`. Stack device
  components, including service diagnostics, MUST use `availability` = [`<B>/status`]
  only, so the service diagnostics stay visible while the bus is down. Every availability
  entry MUST set `payload_available` `online` and `payload_not_available` `offline`.
* **HA-15** Sensor templates MUST render `None` when the JSON value is `null`; Home
  Assistant's MQTT sensor and binary sensor treat a rendered `None` as state *unknown*. A
  binary sensor's template (code **B**) MUST render exactly `ON`, `OFF` or `None`. A plain
  truthiness template is not allowed because it would turn `null` into `OFF`.
* **HA-16** Every component, `sensor` and `binary_sensor` alike, MUST set `expire_after` to
  the resolved value of `EXPIRE_AFTER_S` (`08-configuration.md`), so entities become
  unavailable if state stops arriving while the broker still says `online`.
* **HA-17** `bms_alarm` and `bms_protection` on module devices (code **B+**) MUST set
  `json_attributes_topic` to the module state topic and `json_attributes_template` to
  `{{ {'active_flags': value_json.bms_alarm_flags} | tojson }}` (respectively
  `bms_protection_flags`), giving an `active_flags` attribute that lists the active flag
  names, an empty list when off, or `null` when unknown.

## 7. Republishing

* **HA-18** Discovery MUST be published on every successful MQTT connect, as part of the
  sequence in MQTT-09.
* **HA-19** When `<P>/status` = `online` is received, the service MUST wait a random delay
  drawn uniformly from 0–5 s, then publish, in this order: all discovery messages (HA-04),
  every module availability, then every module state, the stack state and the service
  state. The delay MUST be scheduled on the monotonic clock without blocking the main loop,
  the random source MUST be injectable for tests, and a further `online` received while a
  republish is pending MUST NOT schedule a second one. The delay spreads the load when many
  devices answer a Home Assistant restart at once.
* **HA-20** Each time discovery is published (HA-18, HA-19) the service MUST also publish an
  empty retained payload to
  `<P>/device/<N>_m<m>/config` for every `m` in `MODULE_COUNT`+1..4, so lowering
  `MODULE_COUNT` removes the surplus devices. (Changing `HA_NODE_ID` leaves the old devices
  behind; remove them by hand.)

## 8. Stability

* **HA-21** A `unique_id`, once released, MUST never be changed or reused for a different
  quantity; renaming the entity `name` is allowed. Adding components is allowed at any
  time.
* **HA-22** **UNVERIFIED** To remove a component from a device, the first release without it
  MUST still include the component key with only `platform` set (for example
  `"cell_16_mv": {"platform": "sensor"}`), which Home Assistant's device-based discovery
  documents as the removal signal; later releases MAY drop the key. Whether simply omitting
  a component also removes the entity has not been tested. Removed components MUST be kept
  in one explicit list in the discovery builder.
