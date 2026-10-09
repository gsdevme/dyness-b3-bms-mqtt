# 05 — Alerts (`ALERT`)

The service evaluates a small set of alerts itself, so Home Assistant gets ready-made
`binary_sensor` problem entities without template sensors or automations. Alerts are
computed from the snapshots defined in `04-telemetry.md`; their JSON keys are listed in
`06-mqtt.md` and their entities in `07-home-assistant.md`. Thresholds are environment
variables owned by `08-configuration.md`.

## 1. Model

* **ALERT-01** Alert evaluation MUST be a pure function with no I/O, no threads and no clock
  reads:

  ```python
  def evaluate(
      snapshot: Snapshot,          # 04-telemetry.md, TEL-03
      previous: AlertState,
      now: float,                  # monotonic seconds from the injected Clock
      thresholds: AlertThresholds, # built once from Settings
  ) -> AlertState: ...

  def changes(previous: AlertState, current: AlertState) -> list[AlertChange]: ...
  ```

  `AlertState` MUST be a frozen dataclass holding, for every alert of every module and of
  the stack, its state (`True`, `False` or `None`), the monotonic time its pending
  transition started (or `None`), its input value and, for the BMS flag alerts, the active
  flag names. `AlertState.initial(module_count)` MUST return every alert as `None` with no
  pending transition. `AlertChange` names the scope (module number or stack), the alert
  key, the old and new state and the input value.
* **ALERT-02** Every alert is tri-state: on (`True`), off (`False`) or unknown (`None`).
  Unknown MUST be published as JSON `null`, never as `false`.
* **ALERT-03** Alerts MUST be evaluated on a fresh snapshot at least once per second,
  independently of `PUBLISH_INTERVAL_S`, so debounce timing does not depend on the publish
  interval.

## 2. Alert catalogue

| Key | Scope | Input (`04-telemetry.md` field) | On condition | Threshold variable | Debounce and hysteresis |
|---|---|---|---|---|---|
| `cell_spread` | Module | `spread_mv` | value > threshold | `ALERT_CELL_SPREAD_MV` | Yes |
| `cell_low` | Module | Lowest cell, ALERT-05 | value < threshold | `ALERT_CELL_LOW_MV` | Yes |
| `current_imbalance` | Stack | `imbalance_pct`, ALERT-06 | value > threshold | `ALERT_CURRENT_IMBALANCE_PCT` | Yes |
| `bms_alarm` | Module | `flags` | any `alarm` flag set | — | No |
| `bms_protection` | Module | `flags` | any `cut` or `fault` flag set | — | No |
| `any_cell_spread` | Stack | Module `cell_spread` | ALERT-10 | — | Inherited |
| `any_cell_low` | Stack | Module `cell_low` | ALERT-10 | — | Inherited |
| `any_bms_alarm` | Stack | Module `bms_alarm` | ALERT-10 | — | Inherited |
| `any_bms_protection` | Stack | Module `bms_protection` | ALERT-10 | — | Inherited |

* **ALERT-04** `cell_spread` (per module): the input MUST be the module's `spread_mv`
  (from frame `0x22`, TEL-11). The on condition is `spread_mv > ALERT_CELL_SPREAD_MV`.
  Unknown when `spread_mv` is `None`.
* **ALERT-05** `cell_low` (per module): the input MUST be the smallest non-`None` entry of
  `cells_mv` when `cells_fresh` is true and at least one entry is non-`None`, otherwise
  `cell_min_mv` (from `0x22`). The on condition is `input < ALERT_CELL_LOW_MV`. Unknown when
  neither source is available.
* **ALERT-06** `current_imbalance` (stack): `imbalance_pct` MUST be computed as below, with
  `I_m` the `current_a` of module `m` for `m` = 1..`MODULE_COUNT`, `N` = `MODULE_COUNT` and
  `mean = Σ I_m / N`:

  ```
  imbalance_pct = max over m of |I_m − mean| / |mean| × 100
  ```

  * If any `I_m` is `None`, the alert MUST be unknown and `imbalance_pct` `None`.
  * Otherwise, if `|total_current_a| < ALERT_IMBALANCE_MIN_CURRENT_A` or
    `total_current_a == 0`, `imbalance_pct` MUST be `None` and the alert's input MUST count
    as satisfying the clear condition (low current is a normal state, not missing data), so
    an active alert clears after debounce and an off alert stays off.
  * Otherwise the on condition is `imbalance_pct > ALERT_CURRENT_IMBALANCE_PCT`.

  `imbalance_pct` MUST be computed from the unrounded currents and published rounded to
  1 dp. With `MODULE_COUNT` = 1 it is always 0.
* **ALERT-07** `bms_alarm` (per module): on when any flag of class `alarm` (TEL-09: `0x24`
  byte 1 bits 0–6 and byte 3 bit 5) is set, off when none is, unknown when `flags` is
  `None`. Its active flag list MUST hold the names of the set `alarm` flags.
* **ALERT-08** `bms_protection` (per module): on when any flag of class `cut` (byte 0 bits
  0–6, byte 3 bit 4) or class `fault` (`Short`, `PwrDown`, `MOS_TempH`, `InternalComErr`,
  `Reverse`, `InvalidCell`, `ChgMOS_Err`, `DisMOS_Err`) is set, off when none is, unknown
  when `flags` is `None`. Its active flag list MUST hold the names of the set `cut` and
  `fault` flags. This deliberately differs from the stack telemetry field `any_protection`
  (TEL-17), which counts `cut` bits only.
* **ALERT-09** Active flag lists MUST use the exact names and canonical order of TEL-09,
  MUST be an empty list when the alert is off, and MUST be `None` when it is unknown.
* **ALERT-10** Each stack roll-up `any_<key>` MUST be on if any module's `<key>` is on;
  otherwise unknown if any module's `<key>` is unknown; otherwise off. Roll-ups MUST NOT
  have their own debounce or hysteresis: they change in the same evaluation as the module
  alert that drives them.

## 3. Hysteresis

* **ALERT-11** For a debounced alert with threshold `T`, `H = ALERT_HYSTERESIS_PCT` and
  `M = ALERT_CELL_LOW_HYSTERESIS_MV`, the on and clear conditions MUST be:

  | Alert type | On condition | Clear condition |
  |---|---|---|
  | "greater than" (`cell_spread`, `current_imbalance`) | `v > T` | `v ≤ T × (1 − H/100)` |
  | "less than" (`cell_low`) | `v < T` | `v ≥ T + M` |

  `H` applies only to the "greater than" alerts; `cell_low` uses the absolute margin `M`
  in millivolts. While a value lies between the two conditions, neither holds and the alert
  keeps its state. With `H` = 0 (or `M` = 0) the clear condition is the exact complement of
  the on condition.

Worked example, **with the default values** from `08-configuration.md`:

| Alert | On when | Clears when | Band |
|---|---|---|---|
| `cell_spread` | spread > 50 mV | spread ≤ 45 mV | 45 < spread ≤ 50 holds state |
| `cell_low` | cell < 3000 mV | cell ≥ 3050 mV | 3000 ≤ cell < 3050 holds state |
| `current_imbalance` | > 20 % | ≤ 18 % | 18 < pct ≤ 20 holds state |

`cell_low` uses an absolute margin because a percentage of a 3000 mV threshold would be
far too wide for an LFP cell (10 % would demand a recovery to 3300 mV, which normally only
happens after a recharge). Raise `ALERT_CELL_LOW_HYSTERESIS_MV` if the alert chatters as a
weak cell relaxes after load.

## 4. Debounce

* **ALERT-12** For a debounced alert with `D = ALERT_DEBOUNCE_S`:
  * an off alert MUST turn on at the first evaluation at which the on condition has held at
    every evaluation since time `t0` and `now − t0 ≥ D`;
  * an on alert MUST turn off at the first evaluation at which the clear condition has held
    at every evaluation since `t0` and `now − t0 ≥ D`;
  * an evaluation at which the pending condition does not hold MUST cancel the pending
    transition (the next one starts a new `t0`).

  With `D` = 0 the transition happens at the first evaluation where the condition holds.
* **ALERT-13** When the input of a debounced alert is unknown (`None`, or ALERT-06's first
  case), the alert MUST become unknown at that evaluation, without debounce, and any pending
  transition MUST be cancelled. An alert that was on therefore goes unknown, not on, while
  its inputs are stale: the service cannot claim a condition it cannot see, and the module's
  availability already tells Home Assistant why.
* **ALERT-14** When an unknown debounced alert's input becomes known again, the alert MUST
  take the instantaneous result of its on condition (on if it holds, off otherwise) at that
  evaluation, without debounce. This also applies at start-up, since every alert starts
  unknown (ALERT-01). Debounce protects transitions of the battery's state, and staleness
  is a gap in observation, not a transition; requiring a fresh debounce period would hide a
  real condition for `ALERT_DEBOUNCE_S` after every restart or module dropout.
* **ALERT-15** `bms_alarm` and `bms_protection` MUST follow the `0x24` flags of the current
  snapshot directly, with no debounce and no hysteresis: the BMS already filters its own
  flags.

## 5. Publishing and logging

* **ALERT-16** When `changes()` reports any change of state (including to or from unknown)
  or of an active flag list, the service MUST publish the affected module state payload
  and the stack state payload immediately, without waiting for `PUBLISH_INTERVAL_S` (see
  `06-mqtt.md`). Changes of an input value alone MUST NOT trigger an immediate publish.
* **ALERT-17** Every alert state change MUST be logged at `INFO` with the scope, key, old
  and new state, input value and threshold, for example
  `INFO alert module 2 cell_low: off -> on (value=2994 mV, threshold=3000 mV)`. Changes of
  an active flag list MUST be logged at `INFO` with the new list.
* **ALERT-18** Alert thresholds MUST come only from the variables in the catalogue above,
  read once at start-up (CFG-01). Thresholds, the debounce time and the hysteresis
  settings (`ALERT_HYSTERESIS_PCT`, `ALERT_CELL_LOW_HYSTERESIS_MV`) MUST NOT be hard-coded.
