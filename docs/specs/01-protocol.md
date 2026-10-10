# 01 — Protocol (`PROTO`)

The CAN protocol spoken on the Dyness B3 stack bus, as far as this service reads or writes
it: which frames exist, how each is laid out, how raw values become engineering units, and
the single frame the service is allowed to transmit.

This document is the authority for the protocol facts in this project. Untagged facts were
confirmed on real hardware on 8–9 October 2026 (see `00-overview.md` §4.3 and §6). Hardware
results win over this document: when the stack behaves differently, the spec is updated to
match. The decoder lives in `protocol.py` and is pure:
no I/O, no clock, no configuration lookups beyond the arguments it is given.

## 1. Physical and link layer

| Item | Value |
|---|---|
| Bus | CAN 2.0B (classic CAN, not CAN FD) |
| Bitrate | 500 kbit/s. The configured bitrate is `CAN_BITRATE` (see `08-configuration.md`) and must match the bus |
| Module frames | 29-bit extended identifiers, big-endian payloads |
| Inverter (stack) frames | 11-bit standard identifiers, little-endian payloads (Pylontech-style) |
| Heartbeat | `0x18F20103`, extended, all zeros |
| Payload length | 8 bytes for every frame the service decodes |

* **PROTO-01** The decoder MUST classify a frame by the pair (identifier, extended flag). An
  identifier listed below as extended MUST NOT be accepted as a standard frame and vice
  versa (for example a standard frame with id `0x351` is a stack frame; an extended frame
  with id `0x351` is UNKNOWN).

## 2. Frame classification

Every non-error frame the transport delivers (see `02-can-transport.md`) is classified into
exactly one class.

| Class | Rule | Decoded | Bus activity |
|---|---|---|---|
| `ECHO` | Matches the poll pattern of PROTO-27 (any `m` in 0..15) | No | No |
| `MODULE` | Extended, `(id & 0xFFFFF000) == 0x18F21000`, `m` in 1..4, frame code in the table of §3, DLC 8 | Yes, §3 | Yes |
| `STACK` | Standard, id in the table of §4, DLC 8 | Yes, §4 | Yes |
| `HEARTBEAT` | Extended, id `0x18F20103` | No | Yes |
| `IGNORED` | Standard, id in `0x305`, `0x30F`, `0x35A`, `0x35E`, `0x370`, `0x371` | No | Yes |
| `MALFORMED` | Would be `MODULE` or `STACK` but DLC ≠ 8 | No | Yes |
| `UNKNOWN` | Anything else, including module-range ids with `m` = 0 or 5..15 or an unlisted frame code | No | Yes |

* **PROTO-02** The decoder MUST expose `classify(can_id, extended, data) -> FrameClass`
  implementing the table above, evaluated top to bottom (the first matching row wins), and
  `decode(can_id, extended, data) -> ModuleFrame | StackFrame | None`, which returns `None`
  for every class other than `MODULE` and `STACK`.
* **PROTO-03** `classify` and `decode` MUST NOT raise for any input: any identifier in
  `0..0x1FFFFFFF`, either extended flag, and any `data` length from 0 to 64 bytes. A
  property-based test MUST cover this.
* **PROTO-04** A `MODULE` or `STACK` candidate whose DLC is not 8 MUST be classified
  `MALFORMED`, MUST NOT update any decoded value, and MUST increment the diagnostic counter
  `malformed_frames`. `UNKNOWN` frames MUST increment `unknown_frames`; `ECHO` frames MUST
  increment `echo_frames`. These counters are published as service diagnostics (see
  `04-telemetry.md`, `06-mqtt.md`).

  Informative: the transport's acceptance set (`02-can-transport.md`, CAN-08/CAN-09) does
  not include `0x305`, `0x30F`, `0x35A`, `0x35E`, `0x370` or `0x371`, and `recv` applies it
  in software on every interface, so the `IGNORED` class never occurs at runtime. For the
  same reason `unknown_frames` only counts module-range ids with a wrong module number or
  an unlisted frame code.
* **PROTO-05** The "Bus activity" column defines which frames prove the bus is alive. Every
  class marked Yes MUST be reported to the health machine as a received frame (it keeps
  `bus_age` fresh, see `03-health-and-recovery.md`). `ECHO` frames and error frames MUST
  NOT count: the adapter can echo our own polls while its receive path has stalled, and
  counting echoes would hide exactly the failure `BUS_SILENCE_S` exists to detect.
* **PROTO-06** The decoder MUST NOT consult `MODULE_COUNT`. It decodes modules 1..4; whether
  a module above `MODULE_COUNT` is used is decided by telemetry (see `04-telemetry.md`).

## 3. Module frames (extended, big-endian)

Identifier layout:

```
id = 0x18F21000 | (m << 8) | code        m = module 1..4, code = frame code (low byte)
```

`m` is bits 8–11 and `code` bits 0–7. Module 2 frame `0x21` is therefore `0x18F21221`.

* **PROTO-07** A frame MUST be accepted as a module frame only if it is extended,
  `(id & 0xFFFFF000) == 0x18F21000`, `m = (id >> 8) & 0xF` is in 1..4, `code = id & 0xFF`
  is one of the codes in the table below, and DLC is 8. `m` and `code` MUST be extracted
  exactly as written.

### 3.1 Layouts

All fields are unsigned big-endian as given by the Python `struct` format; scaled values are
computed from the unsigned raw value. `x` bytes are padding and not decoded.

| Code | Sent | `struct` | Fields (in order) | Scaling → unit |
|---|---|---|---|---|
| `0x11` | After poll | `>HHHH` | cells 1, 2, 3, 4 | raw mV |
| `0x12` | After poll | `>HHHH` | cells 5, 6, 7, 8 | raw mV |
| `0x13` | After poll | `>HHHH` | cells 9, 10, 11, 12 | raw mV |
| `0x14` | After poll | `>HHHH` | cells 13, 14, 15, slot 16 | raw mV; slot 16 ignored (PROTO-09) |
| `0x15` | After poll | `>HHHH` | T1, T2, T3, T4 | `(raw − 400) / 10` °C; T4 ignored (PROTO-10) |
| `0x16` | Broadcast | `>HHH2x` | MOSFET temp, board temp, cycle count | temps `(raw − 400) / 10` °C; cycles raw count |
| `0x17` | After poll | `>I4x` | lifetime energy throughput | `raw / 100` kWh |
| `0x21` | Broadcast | `>HHHH` | pack voltage, current, average cell voltage, max discharge current | `raw / 100` V; `(raw − 4000) / 10` A (**negative = charging**); raw mV; `raw / 10` A |
| `0x22` | Broadcast | `>HBHBH` | max cell mV, max cell number, min cell mV, min cell number, max charge current | raw mV; 0-based number (PROTO-11); raw mV; 0-based number; `raw / 10` A |
| `0x23` | Broadcast | `>HBHBBB` | max temp, max-temp sensor number, min temp, min-temp sensor number, SOH, SOC | `(raw − 400) / 10` °C; raw (PROTO-12); `(raw − 400) / 10` °C; raw; % ; % |
| `0x24` | Broadcast | `>8B` | flag bytes 0–7 | bit table §3.2 |

The decoded `ModuleFrame` carries `module`, `code` and the fields below. Names are the
decoder's; published names are owned by `04-telemetry.md`.

| Code | `ModuleFrame` fields |
|---|---|
| `0x11`–`0x14` | `cells_mv`: mapping cell number (1-based) → mV, for the cells the frame carries |
| `0x15` | `temps_c`: `[T1, T2, T3]` |
| `0x16` | `mos_c`, `board_c`, `cycles` |
| `0x17` | `energy_kwh` |
| `0x21` | `pack_v`, `current_raw_a`, `avg_cell_mv`, `max_discharge_a` |
| `0x22` | `cell_max_mv`, `cell_max_no`, `cell_min_mv`, `cell_min_no`, `max_charge_a` |
| `0x23` | `temp_max_c`, `temp_max_sensor_raw`, `temp_min_c`, `temp_min_sensor_raw`, `soh`, `soc` |
| `0x24` | `flags` (name → bool, §3.2), `flags_raw` (the 8 bytes as lower-case hex, 16 chars) |

* **PROTO-08** The decoder MUST decode each module frame exactly per the layout table:
  struct format, field order, offset and divisor. Scaled values MUST be rounded to the
  number of decimal places implied by the divisor (`/10` → 1, `/100` → 2) so that, for
  example, raw `0x136e` yields exactly `49.74`, not a binary-float neighbour.
* **PROTO-09** The B3 is 15S. The fourth value of `0x14` (slot 16, reads 0) MUST be ignored:
  it MUST NOT appear in `cells_mv` or anywhere downstream. Cells are numbered 1..15.
* **PROTO-10** `0x15` T4 is not fitted on the B3 and reads −40.0 °C. It MUST be ignored and
  MUST NOT appear in `temps_c` or anywhere downstream.
* **PROTO-11** `0x22` cell numbers are 0-based on the wire (0..14). The decoder MUST add 1 so
  `cell_max_no` and `cell_min_no` are 1-based (1..15) and match the numbering of
  `0x11`–`0x14`. A raw cell number above 14 MUST decode to `None` for that field while the
  other fields of the frame are still decoded.
* **PROTO-12** **UNVERIFIED** `0x23` sensor numbers have only been seen as 0..2, so whether
  they are 0- or 1-based is unknown. The decoder MUST expose them unmodified as
  `temp_max_sensor_raw` / `temp_min_sensor_raw` and they MUST NOT be published until the
  base is confirmed on hardware (at which point this requirement changes). The temperatures
  themselves, SOH (byte 6) and SOC (byte 7) are confirmed and are published.
* **PROTO-13** `0x21` current on the wire is **negative when the module is charging**
  (confirmed with a 62 A charge). The decoder MUST expose it with the raw sign, as
  `current_raw_a`. It MUST NOT flip the sign; telemetry flips it so that published current is
  positive when charging (see `04-telemetry.md` and the glossary in `00-overview.md`). The
  field name carries `raw` precisely so the two conventions are never confused.
* **PROTO-14** Temperatures MUST be computed as `(raw − 400) / 10` from the unsigned 16-bit
  raw value, so values below 0 °C are represented by raw values below 400 (no two's
  complement).

Informative: `0x21` max discharge current and `0x22` max charge current step with module SOC
(discharge 0 / 30 / 37.5 A; charge 37.5 / 30 / 15 / 0 A). The stack `0x351` limits are the
sum of the module limits. `0x17` is approximately cycles × 3.6 kWh.

### 3.2 `0x24` flag bits

Bits are numbered 0 = least significant. Names are exactly those used by the reference
decoder used during diagnosis. The category column is for telemetry's aggregates.

| Byte | Bit | Name | Category | Meaning |
|---|---|---|---|---|
| 0 | 0 | `CellV_CutH` | protection | Cell over-voltage cut |
| 0 | 1 | `CellV_CutL` | protection | Cell under-voltage cut |
| 0 | 2 | `PackV_CutH` | protection | Pack over-voltage cut |
| 0 | 3 | `PackV_CutL` | protection | Pack under-voltage cut |
| 0 | 4 | `Temp_CutH` | protection | Over-temperature cut |
| 0 | 5 | `Temp_CutL` | protection | Under-temperature cut |
| 0 | 6 | `Cur_CutH` | protection | Over-current cut |
| 1 | 0 | `CellV_AlarmH` | alarm | Cell over-voltage alarm |
| 1 | 1 | `CellV_AlarmL` | alarm | Cell under-voltage alarm |
| 1 | 2 | `PackV_AlarmH` | alarm | Pack over-voltage alarm |
| 1 | 3 | `PackV_AlarmL` | alarm | Pack under-voltage alarm |
| 1 | 4 | `Temp_AlarmH` | alarm | Over-temperature alarm |
| 1 | 5 | `Temp_AlarmL` | alarm | Under-temperature alarm |
| 1 | 6 | `Cur_AlarmH` | alarm | Over-current alarm |
| 2 | 0 | `DisMOS_on` | status | Discharge MOSFET closed (on) |
| 2 | 1 | `ChgMOS_on` | status | Charge MOSFET closed (on) |
| 2 | 2 | `Short` | fault | Short circuit |
| 2 | 3 | `PwrDown` | fault | Power down |
| 2 | 4 | `MOS_TempH` | fault | MOSFET over-temperature |
| 2 | 5 | `InternalComErr` | fault | Internal communication error |
| 2 | 6 | `Reverse` | fault | Reverse connection |
| 2 | 7 | `Balancing` | status | Cell balancing active |
| 3 | 4 | `CellDiff_Cut` | protection | Cell difference cut |
| 3 | 5 | `CellDiff_Alarm` | alarm | Cell difference alarm |
| 4 | 0 | `InvalidCell` | fault | Invalid cell |
| 4 | 4 | `ChgMOS_Err` | fault | Charge MOSFET failure |
| 4 | 5 | `DisMOS_Err` | fault | Discharge MOSFET failure |

* **PROTO-15** The decoder MUST produce `flags` containing exactly the 27 names above, each a
  bool taken from the listed byte and bit, plus `flags_raw` holding all 8 bytes. Bits not
  in the table (byte 0 bit 7, byte 1 bit 7, byte 3 bits 0–3 and 6–7, byte 4 bits 1–3 and
  6–7, bytes 5–7) MUST NOT be named but MUST be preserved in `flags_raw`.
* **PROTO-16** The decoder MUST expose the category of each flag (protection, alarm, status,
  fault) as a constant mapping so telemetry and alerts do not re-encode the table.

Observed values: normal `00 00 03 00 00 00 00 00` (both MOS on); balancing
`00 00 83 …`; cell over-voltage cut with charge MOS open `01 00 81 …`; alarm and cut
`01 01 81 …` (9 Oct; at ≈ 3.52 V the alarm sets, at ≈ 3.55 V the cut sets and `ChgMOS_on`
clears, both clear at ≈ 3.40–3.45 V).

## 4. Inverter / stack frames (standard, little-endian)

These are the master's frames to the inverter; they are visible anywhere on the shared bus.

| Id | `struct` | Fields | Scaling → unit | `StackFrame` fields |
|---|---|---|---|---|
| `0x351` | `<HhhH` | charge voltage limit, charge current limit, discharge current limit, discharge voltage limit | `/10` V; `/10` A (signed); `/10` A (signed); `/10` V | `chg_v`, `chg_a`, `dis_a`, `dis_v` |
| `0x355` | `<HH4x` | SOC, SOH | % ; % | `soc`, `soh` |
| `0x356` | `<Hhh2x` | voltage, current, temperature | `/100` V; `/10` A signed (**positive = charging**); `/10` °C signed | `voltage_v`, `current_a`, `temp_c` |
| `0x359` | `<4xB3x` | byte 4 = module count; all 8 bytes kept raw | count | `module_count`, `raw_hex` |
| `0x35C` | `<B7x` | request flags byte 0 | `0xC0` = charge and discharge allowed | `request_raw`, `charge_enable`, `discharge_enable` |
| `0x35F` | `<4xH2x` | bytes 4–5 = stack capacity | Ah | `capacity_ah` |
| `0x70D` | `<HHHH` | four words | raw | `words` (4 ints) |

* **PROTO-17** The decoder MUST decode each stack frame exactly per the table, with the
  rounding rule of PROTO-08. `0x356` current and temperature and `0x351` current limits
  MUST be decoded as signed 16-bit (`h`).
* **PROTO-18** `0x356` current is **positive when charging** on the wire and equals the sum of
  the module currents. The decoder MUST expose it unmodified as `current_a`; no sign flip is
  applied anywhere for this frame.
* **PROTO-19** `0x359` MUST be decoded to `module_count` (byte 4) and `raw_hex` (all 8 bytes,
  lower-case hex). The other bytes are alarm/protection bits whose meaning has not been
  confirmed on this stack; they MUST NOT be individually named or published as decoded
  values, only as `raw_hex`.
* **PROTO-20** **UNVERIFIED** `0x35C` byte 0 bit 7 is charge enable and bit 6 is discharge
  enable (Pylontech convention; only the combined value `0xC0` has been observed). The
  decoder MUST expose `request_raw` and these two bools.
* **PROTO-21** `0x35F` bytes 4–5 MUST be decoded as `capacity_ah`. Bytes 2–3 (`88 03`) MUST
  NOT be decoded as a firmware version: Dyness support has stated this value is not the
  battery firmware version.
* **PROTO-22** **UNVERIFIED** `0x70D` words are observed as 100, 15 (cells per module), 105
  (meaning unknown), 225 (stack Ah). The decoder MUST expose the four words raw as `words`;
  only word 1 (cells) and word 3 (Ah) MAY be given meaning downstream.
* **PROTO-23** Frames `0x35E`, `0x370`, `0x371` (ASCII `DYNESS-L`, ` Battery`) and `0x305`,
  `0x30F`, `0x35A` (all zeros; `0x305`/`0x30F` are the inverter's keep-alive) MUST be
  classified `IGNORED` and not decoded.
* **PROTO-24** `0x18F20103` (extended, all zeros, 10 Hz) MUST be classified `HEARTBEAT`: it
  counts as bus activity (PROTO-05) but its payload is not decoded or checked.

## 5. Timing (informative)

These observations shape the health machine (`03-health-and-recovery.md`); they are not
requirements on the decoder.

| Observation | Status |
|---|---|
| `0x16`, `0x21`–`0x24` are broadcast unsolicited by every module at about 1.7 Hz each | Confirmed |
| `0x18F20103` heartbeat at 10 Hz | Confirmed |
| Stack frames `0x351`…`0x70D` about 1 Hz each | Confirmed |
| `0x11`–`0x15` and `0x17` are only sent after a poll of that module, once per poll | Confirmed |
| After a few minutes of polling the cell stream continues at about 1 Hz per module with no further polls (still streaming 15+ minutes and overnight later) | Confirmed. The all-night stream was observed during overnight listen-only logging on 8–9 October 2026 |
| How long the cell stream persists with no poll at all, and whether it survives a BMS restart | **UNVERIFIED** |
| Polling each module at about 1 Hz causes no CAN errors and does not interrupt the inverter frames | Confirmed |
| Normal traffic exceeds 30 frames/s | Confirmed |
| The SH-C31A echoes our own transmitted frames back on receive | Confirmed (userspace `gs_usb` on macOS); kernel driver **UNVERIFIED** |

## 6. Poll frame and TX safety

The poll is the only frame the service may ever transmit (`00-overview.md` §1).

```
id   = 0x18F21077 | (m << 8)        extended          (0x18F21m77)
data = 01 00 00 00 00 00 00 00      DLC 8
```

`data[0] = 1` asks module `m` to start its cell stream.

* **PROTO-25** `protocol.build_poll(module, module_count) -> PollFrame` MUST be the only code
  in the package that produces a frame for transmission. `PollFrame` is an immutable value
  holding `arbitration_id` and `data` (8 bytes); the frame is always extended. The
  transport's `send_poll` is its only caller (see `02-can-transport.md`).
* **PROTO-26** `build_poll` MUST raise `ValueError` unless `module_count` is an `int` (not a
  `bool`) in 1..4 and `module` is an `int` (not a `bool`) in 1..`module_count`. Both bounds
  MUST be checked: the absolute protocol range 1..4 and the configured `MODULE_COUNT`.
* **PROTO-27** The poll pattern is: extended, `(id & 0xFFFFF0FF) == 0x18F21077`, DLC 8, data
  exactly `01 00 00 00 00 00 00 00`. Before returning, `build_poll` MUST verify its result
  matches the pattern, has `id == 0x18F21077 | (module << 8)`, and is not `0x18F26677`, and
  MUST raise otherwise. The check MUST be an explicit `if … raise`, not `assert`, so that
  `python -O` cannot remove it.
* **PROTO-28** It MUST be impossible to obtain a `PollFrame` for `0x18F26677` (the BMS
  parameter-write id) or for any id other than `0x18F21177`, `0x18F21277`, `0x18F21377`,
  `0x18F21477`. The range check of PROTO-26 is the real guard, not a special case:
  `0x18F26677` has the poll's shape (low byte `0x77`, `0x18F2` prefix), and an arithmetic
  construction such as `BASE + (m << 8) + 0x77` with an unvalidated `m = 0x56` produces it
  exactly. Construction MUST use bitwise OR on a validated `m`.
* **PROTO-29** A unit test MUST call `build_poll` for every `(module, module_count)` pair in
  −1..20 × −1..6 (and with non-int and bool arguments) and assert that each call either
  raises `ValueError` or returns a frame matching PROTO-27 for a valid pair. A further test
  MUST run the application with a recording fake transport through start-up, retries and
  recovery and assert that every frame handed to the transport for sending matches the poll
  pattern for a module in 1..`MODULE_COUNT`.
* **PROTO-30** A test MUST fail if any `can.Message(` construction or any call to a
  python-can `send` exists in the package outside `PythonCanTransport.send_poll` in
  `canbus.py` (static check over the source tree). `send_poll` builds its one `can.Message`
  from the `PollFrame` returned by `build_poll` (CAN-20); `protocol.py` produces only the
  `PollFrame` (PROTO-25) and constructs no `can.Message`.
* **PROTO-31** With `CAN_LISTEN_ONLY=true`, `build_poll` MUST be unreachable at runtime: the
  health machine emits no `Poll` action (see `03-health-and-recovery.md`) and the transport
  refuses to send (see `02-can-transport.md`). A test MUST run the application in
  listen-only mode through start-up, cell staleness and bus recovery with `build_poll`
  instrumented and assert it is never called.
* **PROTO-32** A received frame matching the poll pattern (our own echo, or another tool such
  as Dyness Monitor polling) MUST be classified `ECHO` and dropped. It MUST NOT be decoded as
  a module frame and MUST NOT count as bus activity (PROTO-05).

## 7. Worked examples

Real frames from the 8 October 2026 hardware captures vendored under
`tests/vectors/captures/`: `t1-listen-only-sniff.txt` (t1) and `t2-poll-60s.txt` (t2),
written `id data`. Each MUST be a decoder test vector (PROTO-33).

### 7.1 Module frames

| Source | Frame | Decoded |
|---|---|---|
| t2 | `18F21111 0c f9 0c f8 0c f4 0c fa` | Module 1, cells 1–4 = 3321, 3320, 3316, 3322 mV |
| t2 | `18F21112 0c f8 0c fa 0c e2 0c f8` | Module 1, cells 5–8 = 3320, 3322, 3298, 3320 mV |
| t2 | `18F21113 0c f6 0c f7 0c f9 0c f9` | Module 1, cells 9–12 = 3318, 3319, 3321, 3321 mV |
| t2 | `18F21114 0c f3 0c f8 0c f8 00 00` | Module 1, cells 13–15 = 3315, 3320, 3320 mV; slot 16 (0) ignored |
| t2 | `18F21211 0c dd 0c ef 0c ee 0c f3` | Module 2, cells 1–4 = 3293, 3311, 3310, 3315 mV |
| t2 | `18F21115 02 41 02 44 02 3e 00 00` | Module 1, T1 17.7 °C, T2 18.0 °C, T3 17.4 °C; T4 (raw 0 = −40.0) ignored |
| t1 | `18F21116 02 7e 02 53 04 0b 00 00` | Module 1, MOS 23.8 °C, board 19.5 °C, 1035 cycles |
| t2 | `18F21117 00 05 87 40 00 00 00 00` | Module 1, lifetime 3623.04 kWh (raw 362304) |
| t1 | `18F21121 13 6e 0f 6f 0c f4 01 77` | Module 1, 49.74 V, `current_raw_a` −4.9 A (raw 3951; charging, published +4.9 A), avg cell 3316 mV, max discharge 37.5 A |
| t1 | `18F21221 13 62 0f 76 0c ec 00 00` | Module 2, 49.62 V, `current_raw_a` −4.2 A, avg cell 3308 mV, max discharge 0.0 A |
| t1 | `18F21122 0c f8 05 0c e0 06 01 77` | Module 1, max 3320 mV `cell_max_no` 6 (wire 5), min 3296 mV `cell_min_no` 7 (wire 6), max charge 37.5 A |
| t1 | `18F21123 02 44 01 02 3e 02 61 25` | Module 1, max 18.0 °C (sensor raw 1), min 17.4 °C (sensor raw 2), SOH 97 %, SOC 37 % |
| t1 | `18F21323 02 3c 01 02 37 00 61 08` | Module 3, max 17.2 °C (sensor raw 1), min 16.7 °C (sensor raw 0), SOH 97 %, SOC 8 % |
| t1 | `18F21124 00 00 03 00 00 00 00 00` | Module 1, `DisMOS_on`, `ChgMOS_on`; all other flags false; `flags_raw` `0000030000000000` |
| t1 | `18F21224 00 00 83 00 00 00 00 00` | Module 2, `DisMOS_on`, `ChgMOS_on`, `Balancing` |

The three module currents in t1 (−4.9, −4.2, −4.0 A raw) sum to −13.1 A, the negative of
the stack `0x356` current +13.1 A below: the two sign conventions are opposite.

### 7.2 Stack and other frames

| Source | Frame | Decoded |
|---|---|---|
| t1 | `351 12 02 65 04 77 01 c2 01` | Charge 53.0 V / 112.5 A, discharge 37.5 A / 45.0 V |
| t1 | `355 12 00 61 00 00 00 00 00` | SOC 18 %, SOH 97 % |
| t1 | `356 6a 13 83 00 b4 00 00 00` | 49.70 V, +13.1 A (charging), 18.0 °C |
| t1 | `359 00 00 00 00 03 00 00 01` | `module_count` 3, `raw_hex` `0000000003000001` |
| t1 | `35C c0 00 00 00 00 00 00 00` | `request_raw` `0xc0`, charge and discharge enabled |
| t1 | `35F 00 00 88 03 e1 00 00 00` | `capacity_ah` 225 |
| t1 | `70D 64 00 0f 00 69 00 e1 00` | `words` 100, 15, 105, 225 |
| t1 | `35E 44 59 4e 45 53 53 2d 4c` | `IGNORED` (ASCII `DYNESS-L`) |
| t1 | `35A 00 00 00 00 00 00 00 00` | `IGNORED` |
| t1 | `18F20103 00 00 00 00 00 00 00 00` | `HEARTBEAT` |
| t2 | `18F21177 01 00 00 00 00 00 00 00` | `ECHO` of our poll of module 1 (logged as `TX` just before) |
| synthetic | `18F21121 13 6e 0f 6f 0c f4` (DLC 6) | `MALFORMED`, `malformed_frames` +1, nothing decoded |
| synthetic | `18F21521 13 6e 0f 6f 0c f4 01 77` | `UNKNOWN` (module 5) |

* **PROTO-33** Every frame in §7 MUST be a decoder test vector with the decoded values and
  class shown. In addition, the t1 and t2 captures MUST be replayed through the decoder in a
  test asserting that no frame is classified `MALFORMED` or `UNKNOWN`.
