"""The Dyness B3 CAN protocol: frame classification, decoding and the guarded poll builder.

Implements ``docs/specs/01-protocol.md``. The module is pure: no I/O, no clock and no
configuration. :func:`classify` sorts every received frame into one :class:`FrameClass`;
:func:`decode` turns ``MODULE`` frames (29-bit, big-endian) and ``STACK`` frames (11-bit,
little-endian) into immutable values in engineering units, rounded as the spec requires.

:func:`build_poll` is the only producer of a frame for transmission (PROTO-25..28). It
returns a :class:`PollFrame`, which refuses any identifier other than the four module polls,
so the BMS parameter-write identifier ``0x18F26677`` can never be built. No ``can.Message``
is constructed here; the transport builds one from the ``PollFrame``.

Units and conventions that are easy to get wrong:

* The B3 is 15S: slot 16 of ``0x14`` and T4 of ``0x15`` are dropped.
* ``0x22`` cell numbers are 0-based on the wire and 1-based here.
* ``0x21`` current keeps its wire sign (negative when charging) as ``current_raw_a``;
  ``0x356`` current is positive when charging. Neither is flipped here.
"""

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import ClassVar

MODULE_ID_BASE = 0x18F21000
MODULE_ID_MASK = 0xFFFFF000
POLL_ID_MASK = 0xFFFFF0FF
POLL_ID_PATTERN = 0x18F21077
POLL_DATA = b"\x01\x00\x00\x00\x00\x00\x00\x00"
PARAMETER_WRITE_ID = 0x18F26677
HEARTBEAT_ID = 0x18F20103
VALID_POLL_IDS = frozenset({0x18F21177, 0x18F21277, 0x18F21377, 0x18F21477})
CELL_CODES = (0x11, 0x12, 0x13, 0x14, 0x15)
ENERGY_CODE = 0x17
BROADCAST_CODES = (0x16, 0x21, 0x22, 0x23, 0x24)
MODULE_CODES = frozenset((*CELL_CODES, ENERGY_CODE, *BROADCAST_CODES))
STACK_IDS = frozenset({0x351, 0x355, 0x356, 0x359, 0x35C, 0x35F, 0x70D})
IGNORED_IDS = frozenset({0x305, 0x30F, 0x35A, 0x35E, 0x370, 0x371})
CELLS_PER_MODULE = 15
FRAME_LENGTH = 8
MAX_MODULES = 4

_TEMP_OFFSET = 400
_CURRENT_OFFSET = 4000


class FrameClass(Enum):
    """The class of a received frame (01-protocol.md section 2)."""

    ECHO = "echo"
    MODULE = "module"
    STACK = "stack"
    HEARTBEAT = "heartbeat"
    IGNORED = "ignored"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"


BUS_ACTIVITY = frozenset(FrameClass) - {FrameClass.ECHO}
"""Classes that prove the bus is alive (PROTO-05): everything except our own echoes."""


class FlagCategory(StrEnum):
    """Category of a ``0x24`` flag, used by telemetry and alert aggregates (PROTO-16)."""

    PROTECTION = "protection"
    ALARM = "alarm"
    STATUS = "status"
    FAULT = "fault"


FLAG_TABLE: tuple[tuple[str, int, int, FlagCategory], ...] = (
    ("CellV_CutH", 0, 0, FlagCategory.PROTECTION),
    ("CellV_CutL", 0, 1, FlagCategory.PROTECTION),
    ("PackV_CutH", 0, 2, FlagCategory.PROTECTION),
    ("PackV_CutL", 0, 3, FlagCategory.PROTECTION),
    ("Temp_CutH", 0, 4, FlagCategory.PROTECTION),
    ("Temp_CutL", 0, 5, FlagCategory.PROTECTION),
    ("Cur_CutH", 0, 6, FlagCategory.PROTECTION),
    ("CellV_AlarmH", 1, 0, FlagCategory.ALARM),
    ("CellV_AlarmL", 1, 1, FlagCategory.ALARM),
    ("PackV_AlarmH", 1, 2, FlagCategory.ALARM),
    ("PackV_AlarmL", 1, 3, FlagCategory.ALARM),
    ("Temp_AlarmH", 1, 4, FlagCategory.ALARM),
    ("Temp_AlarmL", 1, 5, FlagCategory.ALARM),
    ("Cur_AlarmH", 1, 6, FlagCategory.ALARM),
    ("DisMOS_on", 2, 0, FlagCategory.STATUS),
    ("ChgMOS_on", 2, 1, FlagCategory.STATUS),
    ("Short", 2, 2, FlagCategory.FAULT),
    ("PwrDown", 2, 3, FlagCategory.FAULT),
    ("MOS_TempH", 2, 4, FlagCategory.FAULT),
    ("InternalComErr", 2, 5, FlagCategory.FAULT),
    ("Reverse", 2, 6, FlagCategory.FAULT),
    ("Balancing", 2, 7, FlagCategory.STATUS),
    ("CellDiff_Cut", 3, 4, FlagCategory.PROTECTION),
    ("CellDiff_Alarm", 3, 5, FlagCategory.ALARM),
    ("InvalidCell", 4, 0, FlagCategory.FAULT),
    ("ChgMOS_Err", 4, 4, FlagCategory.FAULT),
    ("DisMOS_Err", 4, 5, FlagCategory.FAULT),
)
"""The 27 named ``0x24`` bits as ``(name, byte, bit, category)``, bit 0 least significant."""

FLAG_NAMES: tuple[str, ...] = tuple(name for name, _, _, _ in FLAG_TABLE)
FLAG_CATEGORY: Mapping[str, FlagCategory] = MappingProxyType(
    {name: category for name, _, _, category in FLAG_TABLE}
)


@dataclass(frozen=True, slots=True)
class ModuleFrame:
    """A decoded module frame: ``module`` is the CAN address 1..4, ``code`` the low id byte."""

    module: int
    code: int


@dataclass(frozen=True, slots=True)
class CellVoltages(ModuleFrame):
    """``0x11``-``0x14``: 1-based cell number to mV for the cells the frame carries."""

    cells_mv: Mapping[int, int]


@dataclass(frozen=True, slots=True)
class CellTemperatures(ModuleFrame):
    """``0x15``: cell temperatures T1..T3 in degrees C (T4 is not fitted and dropped)."""

    temps_c: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class MosBoardCycles(ModuleFrame):
    """``0x16``: MOSFET and board temperatures in degrees C and the cycle count."""

    mos_c: float
    board_c: float
    cycles: int


@dataclass(frozen=True, slots=True)
class EnergyThroughput(ModuleFrame):
    """``0x17``: lifetime energy throughput in kWh."""

    energy_kwh: float


@dataclass(frozen=True, slots=True)
class PackStatus(ModuleFrame):
    """``0x21``: pack V, current in A with the wire sign (negative = charging), mV, A."""

    pack_v: float
    current_raw_a: float
    avg_cell_mv: int
    max_discharge_a: float


@dataclass(frozen=True, slots=True)
class CellExtremes(ModuleFrame):
    """``0x22``: highest and lowest cell (1-based, ``None`` if out of range) and charge limit."""

    cell_max_mv: int
    cell_max_no: int | None
    cell_min_mv: int
    cell_min_no: int | None
    max_charge_a: float


@dataclass(frozen=True, slots=True)
class TempExtremes(ModuleFrame):
    """``0x23``: temperature extremes, their unverified raw sensor numbers, SOH and SOC (%)."""

    temp_max_c: float
    temp_max_sensor_raw: int
    temp_min_c: float
    temp_min_sensor_raw: int
    soh: int
    soc: int


@dataclass(frozen=True, slots=True)
class StatusFlags(ModuleFrame):
    """``0x24``: the 27 named flags in :data:`FLAG_TABLE` order and all 8 bytes as hex."""

    flags: Mapping[str, bool]
    flags_raw: str


@dataclass(frozen=True, slots=True)
class StackFrame:
    """A decoded inverter (stack) frame, identified by its 11-bit ``can_id``."""

    can_id: int


@dataclass(frozen=True, slots=True)
class ChargeLimits(StackFrame):
    """``0x351``: charge V, charge A, discharge A, discharge V (currents signed)."""

    chg_v: float
    chg_a: float
    dis_a: float
    dis_v: float


@dataclass(frozen=True, slots=True)
class StackSocSoh(StackFrame):
    """``0x355``: stack SOC and SOH in %."""

    soc: int
    soh: int


@dataclass(frozen=True, slots=True)
class StackMeasurements(StackFrame):
    """``0x356``: stack V, current in A (positive = charging, unmodified) and degrees C."""

    voltage_v: float
    current_a: float
    temp_c: float


@dataclass(frozen=True, slots=True)
class StackModuleCount(StackFrame):
    """``0x359``: module count (byte 4); the unconfirmed alarm bytes only as ``raw_hex``."""

    module_count: int
    raw_hex: str


@dataclass(frozen=True, slots=True)
class ChargeRequest(StackFrame):
    """``0x35C``: request byte and its unverified bit 7 (charge) and bit 6 (discharge)."""

    request_raw: int
    charge_enable: bool
    discharge_enable: bool


@dataclass(frozen=True, slots=True)
class StackCapacity(StackFrame):
    """``0x35F``: stack capacity in Ah (bytes 4-5; bytes 2-3 are not a firmware version)."""

    capacity_ah: int


@dataclass(frozen=True, slots=True)
class StackWords(StackFrame):
    """``0x70D``: four raw little-endian words (observed 100, 15, 105, 225)."""

    words: tuple[int, int, int, int]


@dataclass(slots=True)
class FrameCounters:
    """Diagnostic counters of dropped frames (PROTO-04), published as service diagnostics."""

    echo_frames: int = 0
    malformed_frames: int = 0
    unknown_frames: int = 0

    def count(self, cls: FrameClass) -> None:
        """Count one frame of class ``cls``; classes without a counter are ignored."""
        if cls is FrameClass.ECHO:
            self.echo_frames += 1
        elif cls is FrameClass.MALFORMED:
            self.malformed_frames += 1
        elif cls is FrameClass.UNKNOWN:
            self.unknown_frames += 1


def is_poll(can_id: int, extended: bool, data: bytes | bytearray) -> bool:
    """Return whether a frame matches the poll pattern for any module 0..15 (PROTO-27)."""
    return extended and (can_id & POLL_ID_MASK) == POLL_ID_PATTERN and data == POLL_DATA


def classify(can_id: int, extended: bool, data: bytes | bytearray) -> FrameClass:
    """Classify a received frame by (identifier, extended flag) and DLC (PROTO-01, -02).

    Rows of the 01-protocol.md section 2 table are evaluated top to bottom. Never raises.
    """
    if is_poll(can_id, extended, data):
        return FrameClass.ECHO
    candidate = _is_module_id(can_id, extended) or (not extended and can_id in STACK_IDS)
    if candidate and len(data) == FRAME_LENGTH:
        return FrameClass.MODULE if extended else FrameClass.STACK
    if extended and can_id == HEARTBEAT_ID:
        return FrameClass.HEARTBEAT
    if not extended and can_id in IGNORED_IDS:
        return FrameClass.IGNORED
    if candidate:
        return FrameClass.MALFORMED
    return FrameClass.UNKNOWN


def decode(can_id: int, extended: bool, data: bytes | bytearray) -> ModuleFrame | StackFrame | None:
    """Decode a ``MODULE`` or ``STACK`` frame; return ``None`` for every other class.

    Never raises. Module numbers 1..4 are all decoded regardless of ``MODULE_COUNT``
    (PROTO-06).
    """
    cls = classify(can_id, extended, data)
    payload = bytes(data)
    if cls is FrameClass.MODULE:
        return _decode_module((can_id >> 8) & 0xF, can_id & 0xFF, payload)
    if cls is FrameClass.STACK:
        return _decode_stack(can_id, payload)
    return None


def _is_module_id(can_id: int, extended: bool) -> bool:
    return (
        extended
        and (can_id & MODULE_ID_MASK) == MODULE_ID_BASE
        and 1 <= (can_id >> 8) & 0xF <= MAX_MODULES
        and can_id & 0xFF in MODULE_CODES
    )


def _scaled(raw: int, divisor: int, places: int, offset: int = 0) -> float:
    """Return ``(raw - offset) / divisor`` rounded to ``places``, never ``-0.0``."""
    return round((raw - offset) / divisor, places) + 0.0


def _temp(raw: int) -> float:
    return _scaled(raw, 10, 1, _TEMP_OFFSET)


def _cell_number(wire: int) -> int | None:
    return wire + 1 if wire < CELLS_PER_MODULE else None


def _decode_module(module: int, code: int, data: bytes) -> ModuleFrame:
    if code in (0x11, 0x12, 0x13, 0x14):
        first = (code - 0x11) * 4 + 1
        values = struct.unpack(">HHHH", data)
        cells = {
            number: mv
            for number, mv in enumerate(values, start=first)
            if number <= CELLS_PER_MODULE
        }
        return CellVoltages(module, code, MappingProxyType(cells))
    if code == 0x15:
        t1, t2, t3, _t4 = struct.unpack(">HHHH", data)
        return CellTemperatures(module, code, (_temp(t1), _temp(t2), _temp(t3)))
    if code == 0x16:
        mos, board, cycles = struct.unpack(">HHH2x", data)
        return MosBoardCycles(module, code, _temp(mos), _temp(board), cycles)
    if code == ENERGY_CODE:
        (energy,) = struct.unpack(">I4x", data)
        return EnergyThroughput(module, code, _scaled(energy, 100, 2))
    if code == 0x21:
        volts, current, avg_mv, discharge = struct.unpack(">HHHH", data)
        return PackStatus(
            module,
            code,
            pack_v=_scaled(volts, 100, 2),
            current_raw_a=_scaled(current, 10, 1, _CURRENT_OFFSET),
            avg_cell_mv=avg_mv,
            max_discharge_a=_scaled(discharge, 10, 1),
        )
    if code == 0x22:
        max_mv, max_no, min_mv, min_no, charge = struct.unpack(">HBHBH", data)
        return CellExtremes(
            module,
            code,
            cell_max_mv=max_mv,
            cell_max_no=_cell_number(max_no),
            cell_min_mv=min_mv,
            cell_min_no=_cell_number(min_no),
            max_charge_a=_scaled(charge, 10, 1),
        )
    if code == 0x23:
        t_max, s_max, t_min, s_min, soh, soc = struct.unpack(">HBHBBB", data)
        return TempExtremes(module, code, _temp(t_max), s_max, _temp(t_min), s_min, soh, soc)
    flags = {name: bool(data[byte] >> bit & 1) for name, byte, bit, _ in FLAG_TABLE}
    return StatusFlags(module, code, MappingProxyType(flags), data.hex())


def _decode_stack(can_id: int, data: bytes) -> StackFrame:
    if can_id == 0x351:
        chg_v, chg_a, dis_a, dis_v = struct.unpack("<HhhH", data)
        return ChargeLimits(
            can_id,
            chg_v=_scaled(chg_v, 10, 1),
            chg_a=_scaled(chg_a, 10, 1),
            dis_a=_scaled(dis_a, 10, 1),
            dis_v=_scaled(dis_v, 10, 1),
        )
    if can_id == 0x355:
        soc, soh = struct.unpack("<HH4x", data)
        return StackSocSoh(can_id, soc=soc, soh=soh)
    if can_id == 0x356:
        volts, current, temp = struct.unpack("<Hhh2x", data)
        return StackMeasurements(
            can_id,
            voltage_v=_scaled(volts, 100, 2),
            current_a=_scaled(current, 10, 1),
            temp_c=_scaled(temp, 10, 1),
        )
    if can_id == 0x359:
        (count,) = struct.unpack("<4xB3x", data)
        return StackModuleCount(can_id, module_count=count, raw_hex=data.hex())
    if can_id == 0x35C:
        (request,) = struct.unpack("<B7x", data)
        return ChargeRequest(
            can_id,
            request_raw=request,
            charge_enable=bool(request & 0x80),
            discharge_enable=bool(request & 0x40),
        )
    if can_id == 0x35F:
        (capacity,) = struct.unpack("<4xH2x", data)
        return StackCapacity(can_id, capacity_ah=capacity)
    words = struct.unpack("<HHHH", data)
    return StackWords(can_id, words=(words[0], words[1], words[2], words[3]))


@dataclass(frozen=True, slots=True)
class PollFrame:
    """The only frame the service may transmit: extended ``0x18F21m77``, data ``01 00 .. 00``.

    Construction raises ``ValueError`` for any identifier other than the four module polls
    in :data:`VALID_POLL_IDS` (so never ``0x18F26677``) and for any data other than
    :data:`POLL_DATA`, including through ``dataclasses.replace``. Obtain one from
    :func:`build_poll`.
    """

    arbitration_id: int
    data: bytes
    extended: ClassVar[bool] = True

    def __post_init__(self) -> None:
        """Refuse anything that is not exactly one of the four module polls (PROTO-28)."""
        if type(self.arbitration_id) is not int or self.arbitration_id not in VALID_POLL_IDS:
            raise ValueError(f"refusing to build a frame with id {self.arbitration_id!r}")
        if self.arbitration_id == PARAMETER_WRITE_ID:
            raise ValueError("refusing to build the BMS parameter-write frame")
        if type(self.data) is not bytes or self.data != POLL_DATA:
            raise ValueError(f"refusing to build a poll with data {self.data!r}")


def build_poll(module: int, module_count: int) -> PollFrame:
    """Return the poll for ``module``, the only frame the service may transmit (PROTO-25).

    Raises ``ValueError`` unless ``module_count`` is an ``int`` (not a ``bool``) in 1..4 and
    ``module`` is an ``int`` (not a ``bool``) in 1..``module_count`` (PROTO-26). The id is
    built with bitwise OR from the validated module and the result is re-checked against
    the poll pattern with explicit ``if ... raise`` guards that ``python -O`` keeps
    (PROTO-27, PROTO-28).
    """
    if type(module_count) is not int or not 1 <= module_count <= MAX_MODULES:
        raise ValueError(f"module_count must be an int in 1..{MAX_MODULES}, got {module_count!r}")
    if type(module) is not int or not 1 <= module <= module_count:
        raise ValueError(f"module must be an int in 1..{module_count}, got {module!r}")
    arbitration_id = MODULE_ID_BASE | (module << 8) | (POLL_ID_PATTERN & 0xFF)
    if arbitration_id != POLL_ID_PATTERN | (module << 8):
        raise ValueError(f"poll id {arbitration_id:#x} does not match module {module}")
    if not is_poll(arbitration_id, True, POLL_DATA) or arbitration_id == PARAMETER_WRITE_ID:
        raise ValueError(f"poll id {arbitration_id:#x} does not match the poll pattern")
    return PollFrame(arbitration_id, POLL_DATA)
