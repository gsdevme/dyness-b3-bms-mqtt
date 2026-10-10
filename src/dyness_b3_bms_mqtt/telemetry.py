"""The latest-value telemetry store and its snapshots (``docs/specs/04-telemetry.md``).

:class:`TelemetryStore` keeps, per module and frame code and per stack frame id, only the
newest decoded frame and the monotonic time it arrived. :meth:`TelemetryStore.snapshot`
turns that into an immutable :class:`Snapshot` at a given ``now``: every value whose source
frame is stale (TEL-04) is ``None``, module values follow the project sign and rounding
conventions, and the stack values are derived from the module snapshots.

The store is pure: it never reads a clock (callers pass ``at`` and ``now``), starts no
threads and does no I/O other than one warning per 0 mV cell reading (TEL-23).

Conventions that are easy to get wrong:

* Module ``current_a`` is the negated ``0x21`` value, so it is positive when charging; the
  ``0x356`` stack current is already positive when charging and is kept as is (TEL-18).
* Current limits are magnitudes (TEL-19).
* Every float is rounded with ``round(x, n) + 0.0`` so ``-0.0`` is never published (TEL-21).
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.protocol import (
    BROADCAST_CODES,
    CELL_CODES,
    CELLS_PER_MODULE,
    ENERGY_CODE,
    FLAG_CATEGORY,
    CellExtremes,
    CellTemperatures,
    CellVoltages,
    ChargeLimits,
    EnergyThroughput,
    FlagCategory,
    ModuleFrame,
    MosBoardCycles,
    PackStatus,
    StackFrame,
    StackMeasurements,
    StackModuleCount,
    StackSocSoh,
    StatusFlags,
    TempExtremes,
)
from dyness_b3_bms_mqtt.status import ModuleStatus

_log = logging.getLogger(__name__)

TEMPERATURE_SENSORS = 3
MAX_PERCENT = 100
NO_READING_MV = 0
NO_READING_C = -40.0
VOLTAGE_CODES = (0x11, 0x12, 0x13, 0x14)
TEMPERATURE_CODE = 0x15

_ALARM_FLAGS = tuple(n for n, c in FLAG_CATEGORY.items() if c is FlagCategory.ALARM)
_PROTECTION_FLAGS = tuple(n for n, c in FLAG_CATEGORY.items() if c is FlagCategory.PROTECTION)
_NO_CELLS: tuple[int | None, ...] = (None,) * CELLS_PER_MODULE
_NO_TEMPS: tuple[float | None, ...] = (None,) * TEMPERATURE_SENSORS

type _Received[F] = dict[int, tuple[F, float]]


@dataclass(frozen=True, slots=True)
class ModuleSnapshot:
    """One module's values at a snapshot time (TEL-08); stale values are ``None``.

    ``cells_mv`` always has 15 entries and ``temps_c`` 3, each ``None`` when stale or
    invalid. While the module is offline every field other than ``module``, ``online``,
    ``cells_fresh`` and the two ages is ``None``, or all-``None`` for the two tuples.
    """

    module: int
    online: bool
    cells_fresh: bool
    bcast_age_s: float | None
    cell_age_s: float | None
    cells_mv: tuple[int | None, ...]
    cell_max_mv: int | None
    cell_max_no: int | None
    cell_min_mv: int | None
    cell_min_no: int | None
    cell_avg_mv: int | None
    spread_mv: int | None
    pack_v: float | None
    current_a: float | None
    soc: int | None
    soh: int | None
    cycles: int | None
    energy_kwh: float | None
    chg_limit_a: float | None
    dis_limit_a: float | None
    temps_c: tuple[float | None, ...]
    temp_mos_c: float | None
    temp_board_c: float | None
    temp_max_c: float | None
    temp_min_c: float | None
    flags: Mapping[str, bool] | None
    flags_raw: str | None
    chg_mos_on: bool | None
    dis_mos_on: bool | None
    balancing: bool | None


@dataclass(frozen=True, slots=True)
class StackSnapshot:
    """The stack's BMS values and the values derived from the modules (TEL-10)."""

    lowest_mv: int | None
    lowest_module: int | None
    lowest_cell: int | None
    highest_mv: int | None
    highest_module: int | None
    highest_cell: int | None
    spread_mv: int | None
    total_current_a: float | None
    bms_current_a: float | None
    avg_soc: int | None
    bms_soc: int | None
    bms_soh: int | None
    voltage_v: float | None
    temp_c: float | None
    chg_limit_v: float | None
    chg_limit_a: float | None
    dis_limit_a: float | None
    dis_limit_v: float | None
    module_count_bms: int | None
    modules_online: int
    any_alarm: bool | None
    any_protection: bool | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Every module 1..``MODULE_COUNT`` in module order, and the stack (TEL-03)."""

    modules: tuple[ModuleSnapshot, ...]
    stack: StackSnapshot


class _Candidate(NamedTuple):
    """A cell voltage competing for the stack extremes; ``cell`` is ``None`` if unknown."""

    mv: int
    module: int
    cell: int | None


class TelemetryStore:
    """Latest decoded frames per module and stack, turned into snapshots on demand.

    Feed every decoded frame to :meth:`ingest` with its monotonic receive time and call
    :meth:`snapshot` with the current monotonic time and the module statuses from the
    health machine. Frames from modules above ``MODULE_COUNT`` are dropped (TEL-02).
    """

    def __init__(self, settings: Settings) -> None:
        """Size the store for ``settings.module_count`` and take its staleness limits."""
        self._cell_stale_s = settings.cell_stale_s
        self._module_stale_s = settings.module_stale_s
        self._modules: dict[int, _Received[ModuleFrame]] = {
            module: {} for module in range(1, settings.module_count + 1)
        }
        self._stack: _Received[StackFrame] = {}
        self._zero_cells: set[tuple[int, int]] = set()

    def ingest(self, frame: ModuleFrame | StackFrame, at: float) -> None:
        """Keep ``frame`` as the latest of its kind, received at monotonic time ``at``."""
        if isinstance(frame, StackFrame):
            self._stack[frame.can_id] = (frame, at)
            return
        received = self._modules.get(frame.module)
        if received is None:
            return
        if isinstance(frame, CellVoltages):
            self._warn_zero_cells(frame)
        received[frame.code] = (frame, at)

    def snapshot(self, now: float, status: Mapping[int, ModuleStatus]) -> Snapshot:
        """Return every value as of monotonic time ``now``.

        ``status`` maps each module number to its status (TEL-06); a module missing from it
        is treated as ``SILENT``. The result depends only on the frames ingested, their
        times, ``now`` and ``status``.
        """
        modules = tuple(
            self._module_snapshot(module, received, now, status.get(module, ModuleStatus.SILENT))
            for module, received in self._modules.items()
        )
        return Snapshot(modules, self._stack_snapshot(modules, now))

    def _warn_zero_cells(self, frame: CellVoltages) -> None:
        """Log a 0 mV cell once each time it starts reading 0 for that module and cell."""
        for cell, mv in frame.cells_mv.items():
            key = (frame.module, cell)
            if mv != NO_READING_MV:
                self._zero_cells.discard(key)
            elif key not in self._zero_cells:
                self._zero_cells.add(key)
                _log.warning("module %d cell %d reads 0 mV; publishing null", *key)

    def _module_snapshot(
        self, module: int, received: _Received[ModuleFrame], now: float, status: ModuleStatus
    ) -> ModuleSnapshot:
        """Build one module's snapshot with the freshness rules of TEL-04 and TEL-06.

        The cell stream is fresh only once all five ``CELL_CODES`` have arrived, so a cell
        frame never received counts as stale; ``cell_age_s`` stays the age of the oldest
        cell frame received so far (TEL-07).
        """
        cell_age = _age(received, CELL_CODES, now, oldest=True)
        online = status is not ModuleStatus.SILENT
        cells_fresh = (
            online
            and received.keys() >= set(CELL_CODES)
            and cell_age is not None
            and cell_age <= self._cell_stale_s
        )
        limit = self._module_stale_s if online else None
        mos = _fresh(received, 0x16, MosBoardCycles, now, limit)
        pack = _fresh(received, 0x21, PackStatus, now, limit)
        ext = _fresh(received, 0x22, CellExtremes, now, limit)
        temps = _fresh(received, 0x23, TempExtremes, now, limit)
        flags = _fresh(received, 0x24, StatusFlags, now, limit)
        energy = _fresh(
            received, ENERGY_CODE, EnergyThroughput, now, self._cell_stale_s if online else None
        )
        return ModuleSnapshot(
            module=module,
            online=online,
            cells_fresh=cells_fresh,
            bcast_age_s=_round_age(_age(received, BROADCAST_CODES, now, oldest=False)),
            cell_age_s=_round_age(cell_age),
            cells_mv=_cells(received) if cells_fresh else _NO_CELLS,
            cell_max_mv=None if ext is None else ext.cell_max_mv,
            cell_max_no=None if ext is None else ext.cell_max_no,
            cell_min_mv=None if ext is None else ext.cell_min_mv,
            cell_min_no=None if ext is None else ext.cell_min_no,
            cell_avg_mv=None if pack is None else pack.avg_cell_mv,
            spread_mv=None if ext is None else ext.cell_max_mv - ext.cell_min_mv,
            pack_v=None if pack is None else _clean(pack.pack_v, 2),
            current_a=None if pack is None else _clean(-pack.current_raw_a, 1),
            soc=None if temps is None else _percent(temps.soc),
            soh=None if temps is None else _percent(temps.soh),
            cycles=None if mos is None else mos.cycles,
            energy_kwh=None if energy is None else _clean(energy.energy_kwh, 2),
            chg_limit_a=None if ext is None else _magnitude(ext.max_charge_a),
            dis_limit_a=None if pack is None else _magnitude(pack.max_discharge_a),
            temps_c=_temperatures(received) if cells_fresh else _NO_TEMPS,
            temp_mos_c=None if mos is None else _clean(mos.mos_c, 1),
            temp_board_c=None if mos is None else _clean(mos.board_c, 1),
            temp_max_c=None if temps is None else _clean(temps.temp_max_c, 1),
            temp_min_c=None if temps is None else _clean(temps.temp_min_c, 1),
            flags=None if flags is None else flags.flags,
            flags_raw=None if flags is None else flags.flags_raw,
            chg_mos_on=None if flags is None else flags.flags["ChgMOS_on"],
            dis_mos_on=None if flags is None else flags.flags["DisMOS_on"],
            balancing=None if flags is None else flags.flags["Balancing"],
        )

    def _stack_snapshot(self, modules: tuple[ModuleSnapshot, ...], now: float) -> StackSnapshot:
        """Build the stack snapshot from the module snapshots and the stack frames."""
        limits = _fresh(self._stack, 0x351, ChargeLimits, now, self._module_stale_s)
        soc_soh = _fresh(self._stack, 0x355, StackSocSoh, now, self._module_stale_s)
        measured = _fresh(self._stack, 0x356, StackMeasurements, now, self._module_stale_s)
        count = _fresh(self._stack, 0x359, StackModuleCount, now, self._module_stale_s)
        lows, highs = _candidates(modules)
        lowest = min(lows, key=_lowest_first, default=None)
        highest = min(highs, key=_highest_first, default=None)
        flag_sets = [m.flags for m in modules if m.flags is not None]
        return StackSnapshot(
            lowest_mv=None if lowest is None else lowest.mv,
            lowest_module=None if lowest is None else lowest.module,
            lowest_cell=None if lowest is None else lowest.cell,
            highest_mv=None if highest is None else highest.mv,
            highest_module=None if highest is None else highest.module,
            highest_cell=None if highest is None else highest.cell,
            spread_mv=None if lowest is None or highest is None else highest.mv - lowest.mv,
            total_current_a=_total_current(modules),
            bms_current_a=None if measured is None else _clean(measured.current_a, 1),
            avg_soc=_average_soc(modules),
            bms_soc=None if soc_soh is None else _percent(soc_soh.soc),
            bms_soh=None if soc_soh is None else _percent(soc_soh.soh),
            voltage_v=None if measured is None else _clean(measured.voltage_v, 2),
            temp_c=None if measured is None else _clean(measured.temp_c, 1),
            chg_limit_v=None if limits is None else _clean(limits.chg_v, 2),
            chg_limit_a=None if limits is None else _magnitude(limits.chg_a),
            dis_limit_a=None if limits is None else _magnitude(limits.dis_a),
            dis_limit_v=None if limits is None else _clean(limits.dis_v, 2),
            module_count_bms=None if count is None else count.module_count,
            modules_online=sum(1 for m in modules if m.online),
            any_alarm=_any_flag(flag_sets, _ALARM_FLAGS),
            any_protection=_any_flag(flag_sets, _PROTECTION_FLAGS),
        )


def _fresh[F, T](
    received: _Received[F], key: int, kind: type[T], now: float, max_age: float | None
) -> T | None:
    """Return the latest ``key`` frame if it is a ``kind`` no older than ``max_age``.

    A ``max_age`` of ``None`` means the whole group is stale, as for an offline module.
    """
    entry = received.get(key)
    if entry is None or max_age is None or now - entry[1] > max_age:
        return None
    frame = entry[0]
    return frame if isinstance(frame, kind) else None


def _age[F](
    received: _Received[F], codes: Iterable[int], now: float, *, oldest: bool
) -> float | None:
    """Return the oldest or newest age of the received ``codes``; ``None`` if none arrived."""
    ages = [now - received[code][1] for code in codes if code in received]
    if not ages:
        return None
    return max(ages) if oldest else min(ages)


def _cells(received: _Received[ModuleFrame]) -> tuple[int | None, ...]:
    """Return cells 1..15 from the latest ``0x11``-``0x14``; 0 mV becomes ``None``."""
    by_cell: dict[int, int] = {}
    for code in VOLTAGE_CODES:
        entry = received.get(code)
        if entry is not None and isinstance(entry[0], CellVoltages):
            by_cell.update(entry[0].cells_mv)
    values = (by_cell.get(cell) for cell in range(1, CELLS_PER_MODULE + 1))
    return tuple(None if mv == NO_READING_MV else mv for mv in values)


def _temperatures(received: _Received[ModuleFrame]) -> tuple[float | None, ...]:
    """Return T1..T3 from the latest ``0x15``; exactly -40.0 degrees C becomes ``None``."""
    entry = received.get(TEMPERATURE_CODE)
    if entry is None or not isinstance(entry[0], CellTemperatures):
        return _NO_TEMPS
    return tuple(None if t == NO_READING_C else _clean(t, 1) for t in entry[0].temps_c)


def _candidates(
    modules: Iterable[ModuleSnapshot],
) -> tuple[list[_Candidate], list[_Candidate]]:
    """Return the (lowest, highest) stack-extreme candidates of TEL-12."""
    lows: list[_Candidate] = []
    highs: list[_Candidate] = []
    for m in modules:
        cells = [
            _Candidate(mv, m.module, cell)
            for cell, mv in enumerate(m.cells_mv, start=1)
            if mv is not None
        ]
        if m.online and m.cells_fresh and cells:
            lows += cells
            highs += cells
        elif m.online and m.cell_min_mv is not None and m.cell_max_mv is not None:
            lows.append(_Candidate(m.cell_min_mv, m.module, m.cell_min_no))
            highs.append(_Candidate(m.cell_max_mv, m.module, m.cell_max_no))
    return lows, highs


def _lowest_first(c: _Candidate) -> tuple[int, int, int]:
    """Order by voltage, then module, then cell (an unknown cell is alone in its module)."""
    return (c.mv, c.module, c.cell or 0)


def _highest_first(c: _Candidate) -> tuple[int, int, int]:
    """Order by descending voltage, then module, then cell."""
    return (-c.mv, c.module, c.cell or 0)


def _total_current(modules: Iterable[ModuleSnapshot]) -> float | None:
    """Return the sum of every module current (TEL-14), or ``None`` if any is unknown."""
    currents = [m.current_a for m in modules]
    known = [c for c in currents if c is not None]
    return _clean(sum(known), 1) if len(known) == len(currents) else None


def _average_soc(modules: Iterable[ModuleSnapshot]) -> int | None:
    """Return the built-in-``round`` mean of the known module SOCs (TEL-15)."""
    socs = [m.soc for m in modules if m.soc is not None]
    return round(sum(socs) / len(socs)) if socs else None


def _any_flag(flag_sets: list[Mapping[str, bool]], names: tuple[str, ...]) -> bool | None:
    """Return whether any of ``names`` is set in any flag set; ``None`` with no flag sets."""
    if not flag_sets:
        return None
    return any(flags[name] for flags in flag_sets for name in names)


def _percent(value: int) -> int | None:
    """Return ``value`` unless it is above 100 (TEL-25)."""
    return value if value <= MAX_PERCENT else None


def _magnitude(amps: float) -> float:
    """Return a current limit as a non-negative value at 1 dp (TEL-19)."""
    return _clean(abs(amps), 1)


def _clean(value: float, places: int) -> float:
    """Round ``value`` to ``places`` and turn ``-0.0`` into ``0.0`` (TEL-20, TEL-21)."""
    return round(value, places) + 0.0


def _round_age(age: float | None) -> float | None:
    """Round an age to 1 dp, keeping ``None`` for a group never received (TEL-07)."""
    return None if age is None else _clean(age, 1)
