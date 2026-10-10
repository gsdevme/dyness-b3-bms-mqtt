# 04 — Telemetry (`TEL`)

This document defines the data model between the decoder and everything that publishes:
which values the service knows about each module and the stack, where each value comes
from, how derived values are computed, the sign convention, units and rounding, and how
staleness turns into `null`.

Frame layouts, byte order and scaling are owned by `01-protocol.md`; the byte columns
below only say which bytes feed which field. Freshness of modules (module status,
broadcast age, cell age) is owned by `03-health-and-recovery.md`. The JSON key for each
field is defined in `06-mqtt.md`; section 9 maps the two where they differ.

## 1. Store and snapshots

* **TEL-01** The telemetry store MUST be pure: no I/O other than logging, no threads, and
  no clock reads other than an injected monotonic `Clock`
  (`Callable[[], float]`, seconds). Given the same frames at the same clock times it MUST
  produce identical snapshots, so tests drive it with a fake clock.
* **TEL-02** The store MUST keep, per module and per frame id, and per stack frame id, only
  the latest decoded value and the monotonic time it was received. It MUST NOT keep
  history. Frames from module numbers outside 1..`MODULE_COUNT` MUST be ignored.
* **TEL-03** The store MUST expose
  `snapshot(now: float, status: Mapping[int, ModuleStatus]) -> Snapshot`, where `status`
  is the per-module status from `03-health-and-recovery.md` and `Snapshot` is a frozen
  dataclass `Snapshot(modules: tuple[ModuleSnapshot, ...], stack: StackSnapshot)` with one
  `ModuleSnapshot` per module 1..`MODULE_COUNT`, in module order. `ModuleSnapshot` and
  `StackSnapshot` MUST be frozen dataclasses with exactly the fields in sections 2 and 4.

## 2. Freshness

* **TEL-04** Every value MUST belong to exactly one freshness group below and MUST be fresh
  only while its source frame is fresh:

  | Group | Frames | Age used | Stale when |
  |---|---|---|---|
  | Broadcast | `0x16`, `0x21`, `0x22`, `0x23`, `0x24` | Age of that frame | Age > `MODULE_STALE_S`, or the module is not online |
  | Cell stream | `0x11`–`0x15` | Oldest of the five | Any of the five never received, the oldest age > `CELL_STALE_S`, or the module is not online (`cells_fresh` is false) |
  | Energy | `0x17` | Age of `0x17` | Age > `CELL_STALE_S`, or the module is not online |
  | Stack | `0x351`, `0x355`, `0x356`, `0x359` | Age of that frame | Age > `MODULE_STALE_S` |

  A frame never received since start-up is stale. The stack group reuses
  `MODULE_STALE_S` because the inverter-facing frames are broadcast at a similar rate and
  `08-configuration.md` defines no separate threshold.
* **TEL-05** A stale value MUST be `None` in the snapshot (published as `null`). The store
  MUST NOT substitute a last-known value, a zero or a default.
* **TEL-06** `online` MUST be true exactly when the module status from
  `03-health-and-recovery.md` is anything other than SILENT. While the health state is
  `STALE`, `RECOVERING` or `DEAD`, the application MUST pass `SILENT` for every module so
  that `online` always equals module availability (HEALTH-30). `cells_fresh` MUST be true
  exactly when `online` is true, all five of `0x11`–`0x15` have been received since
  start-up, and the oldest of them is no older than `CELL_STALE_S`; this matches
  HEALTH-04, where a cell frame never received is aged from the bus open and so keeps the
  module `CELLS_STALE`. When `online` is false, every field of the `ModuleSnapshot` other
  than `module`, `online`, `cells_fresh`, `bcast_age_s` and `cell_age_s` MUST be `None`.
* **TEL-07** `bcast_age_s` and `cell_age_s` MUST be the module broadcast age (newest of its
  frames) and cell age (oldest of its frames) of `03-health-and-recovery.md` HEALTH-04,
  computed at `now` and rounded to 1 dp, but counting only frames actually received: each
  is `None` until a frame of its group has arrived, and while only some of `0x11`–`0x15`
  have arrived `cell_age_s` is the age of the oldest of those. They are diagnostics and are
  never nulled for being stale; whether the cell stream is fresh is `cells_fresh` (TEL-06),
  not `cell_age_s` alone.

## 3. ModuleSnapshot

* **TEL-08** `ModuleSnapshot` MUST contain these fields, decoded as described in
  `01-protocol.md`:

  | Field | Type | Unit | Source frame: bytes | Group |
  |---|---|---|---|---|
  | `module` | `int` | — | Module number from the CAN id | — |
  | `online` | `bool` | — | TEL-06 | — |
  | `cells_fresh` | `bool` | — | TEL-06 | — |
  | `bcast_age_s` | `float \| None` | s | TEL-07 | — |
  | `cell_age_s` | `float \| None` | s | TEL-07 | — |
  | `cells_mv` | `tuple[int \| None, ...]` (length 15) | mV | `0x11`–`0x14`: 4 × u16 per frame, cells 1–15 (slot 16 ignored, TEL-22) | Cell stream |
  | `cell_max_mv` | `int \| None` | mV | `0x22`: 0–1 | Broadcast |
  | `cell_max_no` | `int \| None` | — | `0x22`: 2, plus 1 (TEL-24) | Broadcast |
  | `cell_min_mv` | `int \| None` | mV | `0x22`: 3–4 | Broadcast |
  | `cell_min_no` | `int \| None` | — | `0x22`: 5, plus 1 (TEL-24) | Broadcast |
  | `cell_avg_mv` | `int \| None` | mV | `0x21`: 4–5 | Broadcast |
  | `spread_mv` | `int \| None` | mV | TEL-11 | Broadcast |
  | `pack_v` | `float \| None` | V | `0x21`: 0–1 | Broadcast |
  | `current_a` | `float \| None` | A | `0x21`: 2–3, sign per TEL-18 | Broadcast |
  | `soc` | `int \| None` | % | `0x23`: 7 | Broadcast |
  | `soh` | `int \| None` | % | `0x23`: 6 | Broadcast |
  | `cycles` | `int \| None` | — | `0x16`: 4–5 | Broadcast |
  | `energy_kwh` | `float \| None` | kWh | `0x17`: 0–3 | Energy |
  | `chg_limit_a` | `float \| None` | A | `0x22`: 6–7 | Broadcast |
  | `dis_limit_a` | `float \| None` | A | `0x21`: 6–7 | Broadcast |
  | `temps_c` | `tuple[float \| None, ...]` (length 3) | °C | `0x15`: 0–5, sensors 1–3 (T4 ignored, TEL-22) | Cell stream |
  | `temp_mos_c` | `float \| None` | °C | `0x16`: 0–1 | Broadcast |
  | `temp_board_c` | `float \| None` | °C | `0x16`: 2–3 | Broadcast |
  | `temp_max_c` | `float \| None` | °C | `0x23`: 0–1 | Broadcast |
  | `temp_min_c` | `float \| None` | °C | `0x23`: 3–4 | Broadcast |
  | `flags` | `Mapping[str, bool] \| None` | — | `0x24`: 0–4, TEL-09 | Broadcast |
  | `flags_raw` | `str \| None` | — | `0x24`: 0–7 as 16 lowercase hex digits, no separators | Broadcast |
  | `chg_mos_on` | `bool \| None` | — | `flags["ChgMOS_on"]` | Broadcast |
  | `dis_mos_on` | `bool \| None` | — | `flags["DisMOS_on"]` | Broadcast |
  | `balancing` | `bool \| None` | — | `flags["Balancing"]` | Broadcast |

  The `0x23` sensor-number bytes (2 and 5) are not part of the snapshot: their numbering
  base is **UNVERIFIED** (see `01-protocol.md`).
* **TEL-09** `flags` MUST contain exactly the following names, each `True` when its bit is
  set. The table order is the canonical order used wherever flag names are listed. Bits not
  in the table MUST be ignored (they remain visible in `flags_raw`). The class column is
  used by TEL-17 and `05-alerts.md`.

  | Name | Byte | Bit | Class |
  |---|---|---|---|
  | `CellV_CutH` | 0 | 0 | protection |
  | `CellV_CutL` | 0 | 1 | protection |
  | `PackV_CutH` | 0 | 2 | protection |
  | `PackV_CutL` | 0 | 3 | protection |
  | `Temp_CutH` | 0 | 4 | protection |
  | `Temp_CutL` | 0 | 5 | protection |
  | `Cur_CutH` | 0 | 6 | protection |
  | `CellV_AlarmH` | 1 | 0 | alarm |
  | `CellV_AlarmL` | 1 | 1 | alarm |
  | `PackV_AlarmH` | 1 | 2 | alarm |
  | `PackV_AlarmL` | 1 | 3 | alarm |
  | `Temp_AlarmH` | 1 | 4 | alarm |
  | `Temp_AlarmL` | 1 | 5 | alarm |
  | `Cur_AlarmH` | 1 | 6 | alarm |
  | `DisMOS_on` | 2 | 0 | status |
  | `ChgMOS_on` | 2 | 1 | status |
  | `Short` | 2 | 2 | fault |
  | `PwrDown` | 2 | 3 | fault |
  | `MOS_TempH` | 2 | 4 | fault |
  | `InternalComErr` | 2 | 5 | fault |
  | `Reverse` | 2 | 6 | fault |
  | `Balancing` | 2 | 7 | status |
  | `CellDiff_Cut` | 3 | 4 | protection |
  | `CellDiff_Alarm` | 3 | 5 | alarm |
  | `InvalidCell` | 4 | 0 | fault |
  | `ChgMOS_Err` | 4 | 4 | fault |
  | `DisMOS_Err` | 4 | 5 | fault |

  Only the `CellV_CutH`, `CellV_AlarmH`, `ChgMOS_on`, `DisMOS_on` and `Balancing` bits have
  been seen to change on real hardware; the meaning of the others is **UNVERIFIED**.

## 4. StackSnapshot

* **TEL-10** `StackSnapshot` MUST contain these fields:

  | Field | Type | Unit | Source | Group |
  |---|---|---|---|---|
  | `lowest_mv` | `int \| None` | mV | TEL-12 | Derived |
  | `lowest_module` | `int \| None` | — | TEL-12 | Derived |
  | `lowest_cell` | `int \| None` | — | TEL-12 | Derived |
  | `highest_mv` | `int \| None` | mV | TEL-12 | Derived |
  | `highest_module` | `int \| None` | — | TEL-12 | Derived |
  | `highest_cell` | `int \| None` | — | TEL-12 | Derived |
  | `spread_mv` | `int \| None` | mV | TEL-13 | Derived |
  | `total_current_a` | `float \| None` | A | TEL-14 | Derived |
  | `bms_current_a` | `float \| None` | A | `0x356`: 2–3 (signed), sign per TEL-18 | Stack |
  | `avg_soc` | `int \| None` | % | TEL-15 | Derived |
  | `bms_soc` | `int \| None` | % | `0x355`: 0–1 | Stack |
  | `bms_soh` | `int \| None` | % | `0x355`: 2–3 | Stack |
  | `voltage_v` | `float \| None` | V | `0x356`: 0–1 | Stack |
  | `temp_c` | `float \| None` | °C | `0x356`: 4–5 (signed) | Stack |
  | `chg_limit_v` | `float \| None` | V | `0x351`: 0–1 | Stack |
  | `chg_limit_a` | `float \| None` | A | `0x351`: 2–3, TEL-19 | Stack |
  | `dis_limit_a` | `float \| None` | A | `0x351`: 4–5, TEL-19 | Stack |
  | `dis_limit_v` | `float \| None` | V | `0x351`: 6–7 | Stack |
  | `module_count_bms` | `int \| None` | — | `0x359`: 4 | Stack |
  | `modules_online` | `int` | — | TEL-16 | Derived |
  | `any_alarm` | `bool \| None` | — | TEL-17 | Derived |
  | `any_protection` | `bool \| None` | — | TEL-17 | Derived |

## 5. Derived values

* **TEL-11** Module `cell_max_mv`, `cell_max_no`, `cell_min_mv` and `cell_min_no` MUST come
  from frame `0x22`, and module `spread_mv` MUST be `cell_max_mv − cell_min_mv` from the
  same `0x22` frame. These need no poll, so they are available in listen-only mode. They
  are not recomputed from `cells_mv`: the BMS samples `0x22` and the cell stream at
  different moments, so the two can differ by a few mV.
* **TEL-12** Stack `lowest_*` and `highest_*` MUST be chosen from one candidate set built
  per online module:
  * if the module's `cells_fresh` is true and at least one entry of `cells_mv` is not
    `None`, every non-`None` entry of `cells_mv` with its 1-based cell number;
  * otherwise, if its `0x22` values are fresh, the pair (`cell_min_mv`, `cell_min_no`) for
    the lowest and (`cell_max_mv`, `cell_max_no`) for the highest;
  * otherwise nothing.

  `lowest_mv` is the smallest candidate voltage, `highest_mv` the largest; `*_module` and
  `*_cell` identify the candidate. Ties MUST resolve to the lowest module number, then the
  lowest cell number. If no module contributes a candidate, all six fields MUST be `None`.
  Using `0x22` as a fallback keeps the stack extremes populated in listen-only mode and
  while a module's cell stream is being re-polled.
* **TEL-13** Stack `spread_mv` MUST be `highest_mv − lowest_mv`, or `None` if either is
  `None`.
* **TEL-14** `total_current_a` MUST be the sum of `current_a` over modules
  1..`MODULE_COUNT`, summed before rounding, and MUST be `None` if any of them is `None`.
  `bms_current_a` is the BMS's own figure from `0x356`; on hardware the two agree.
* **TEL-15** `avg_soc` MUST be the arithmetic mean of the non-`None` module `soc` values,
  rounded with Python's built-in `round()` to an integer, or `None` if all are `None`.
  It is the service's counterpart of `bms_soc`, which the BMS computes the same way.
* **TEL-16** `modules_online` MUST be the number of modules 1..`MODULE_COUNT` whose
  `online` is true. It is never `None`.
* **TEL-17** `any_alarm` MUST be true if any module's `flags` has a set bit of class
  `alarm` (TEL-09); `any_protection` MUST be true if any module's `flags` has a set bit of
  class `protection`. Each is false when at least one module has non-`None` `flags` and
  none has such a bit set, and `None` when every module's `flags` is `None`. These are raw
  roll-ups without debounce; the alert roll-ups in `05-alerts.md` additionally count
  `fault` bits for protection.

## 6. Sign convention

* **TEL-18** Every published current MUST be positive when the battery is charging and
  negative when discharging. Module `current_a` MUST therefore be the negation of the
  `0x21` value (`0x21` is negative when charging). `bms_current_a` MUST be the `0x356` value
  unchanged (already positive when charging).
* **TEL-19** Current limits (`chg_limit_a`, `dis_limit_a` on modules and stack) are
  magnitudes: they MUST be published as the non-negative decoded value with no sign
  applied, whatever the direction they limit.

## 7. Units and rounding

* **TEL-20** Values MUST be published in these units, rounded with Python's built-in
  `round()` after scaling:

  | Quantity | Unit | Rounding |
  |---|---|---|
  | Cell voltages, cell spread, cell average | mV | integer |
  | Pack, stack and limit voltages | V | 2 dp |
  | Currents and current limits | A | 1 dp |
  | Temperatures | °C | 1 dp |
  | Energy | kWh | 2 dp |
  | SOC, SOH | % | integer |
  | Cycles, cell and module numbers, counts | — | integer |
  | Ages | s | 1 dp |

* **TEL-21** A rounded value of negative zero MUST be published as `0` / `0.0`. Snapshot
  values MUST never be NaN or infinite.

## 8. Ignored and invalid data

* **TEL-22** Cell slot 16 (`0x14` bytes 6–7) and temperature sensor 4 (`0x15` bytes 6–7)
  MUST be ignored: the B3 is 15S and T4 is unused (reads −40 °C).
* **TEL-23** **UNVERIFIED** A cell reading of 0 mV in slots 1..15 MUST be stored as `None`
  in `cells_mv`, excluded from TEL-12, and logged at `WARNING` once each time it appears for
  that module and slot. Likewise a temperature of exactly −40.0 °C from `0x15` sensors 1–3
  MUST be `None`. Neither has been seen on the B3; both are the BMS's "no reading" values
  for unused slots.
* **TEL-24** A `0x22` cell number that is not in 1..15 after adding 1 MUST make the
  corresponding `cell_max_no` / `cell_min_no` `None`; the voltage is still used.
* **TEL-25** A `soc`, `soh`, `bms_soc` or `bms_soh` greater than 100 MUST be `None`.

## 9. Numbering and field-to-key mapping

* **TEL-26** Cell numbers, module numbers and temperature sensor numbers MUST be 1-based in
  every snapshot field and in all published data. The 0-based `0x22` numbers MUST be
  converted exactly once, by the decoder (`01-protocol.md` PROTO-11), before telemetry sees
  them; the store receives 1-based numbers and does not convert again.

Snapshot fields map one-to-one to the JSON keys in `06-mqtt.md` with the same name, except:

| Snapshot field | JSON key(s) |
|---|---|
| `ModuleSnapshot.cells_mv[i]` | `cell_01_mv` … `cell_15_mv` (`i` = 0..14) |
| `ModuleSnapshot.temps_c[i]` | `temp_1_c` … `temp_3_c` (`i` = 0..2) |
| `ModuleSnapshot.flags` | Not published directly; feeds `bms_alarm_flags` / `bms_protection_flags` (`05-alerts.md`) |

`ts` and module `name` are added by the publisher (`06-mqtt.md`); alert keys come from
`05-alerts.md`.
