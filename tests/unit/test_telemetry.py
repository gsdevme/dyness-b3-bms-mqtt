"""Tests of the latest-value telemetry store (``telemetry``): 04-telemetry.md.

Expected numbers are worked out by hand from the vendored vectors in ``tests/vectors/`` and
the formulas in the spec, not taken from the module under test.
"""

import ast
import dataclasses
import inspect
import logging
import math
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

from dyness_b3_bms_mqtt import status as status_module
from dyness_b3_bms_mqtt import telemetry
from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.protocol import (
    CellExtremes,
    CellTemperatures,
    CellVoltages,
    ChargeLimits,
    EnergyThroughput,
    ModuleFrame,
    MosBoardCycles,
    PackStatus,
    StackFrame,
    StackMeasurements,
    StackModuleCount,
    StackSocSoh,
    StatusFlags,
    TempExtremes,
    decode,
)
from dyness_b3_bms_mqtt.status import RUNNING_STATES, HealthState, ModuleStatus
from dyness_b3_bms_mqtt.telemetry import ModuleSnapshot, Snapshot, StackSnapshot, TelemetryStore
from tests.fakes.clock import FakeClock
from tests.support.settings import make_settings
from tests.vectors.loader import VECTORS, VectorLine, load

OK = ModuleStatus.OK
SILENT = ModuleStatus.SILENT
CELLS_STALE = ModuleStatus.CELLS_STALE

MODULE_FIELDS = (
    "module",
    "online",
    "cells_fresh",
    "bcast_age_s",
    "cell_age_s",
    "cells_mv",
    "cell_max_mv",
    "cell_max_no",
    "cell_min_mv",
    "cell_min_no",
    "cell_avg_mv",
    "spread_mv",
    "pack_v",
    "current_a",
    "soc",
    "soh",
    "cycles",
    "energy_kwh",
    "chg_limit_a",
    "dis_limit_a",
    "temps_c",
    "temp_mos_c",
    "temp_board_c",
    "temp_max_c",
    "temp_min_c",
    "flags",
    "flags_raw",
    "chg_mos_on",
    "dis_mos_on",
    "balancing",
)
STACK_FIELDS = (
    "lowest_mv",
    "lowest_module",
    "lowest_cell",
    "highest_mv",
    "highest_module",
    "highest_cell",
    "spread_mv",
    "total_current_a",
    "bms_current_a",
    "avg_soc",
    "bms_soc",
    "bms_soh",
    "voltage_v",
    "temp_c",
    "chg_limit_v",
    "chg_limit_a",
    "dis_limit_a",
    "dis_limit_v",
    "module_count_bms",
    "modules_online",
    "any_alarm",
    "any_protection",
)
IDENTITY_AND_AGES = ("module", "online", "cells_fresh", "bcast_age_s", "cell_age_s")
SPEC_FLAG_NAMES = (
    *("CellV_CutH", "CellV_CutL", "PackV_CutH", "PackV_CutL", "Temp_CutH", "Temp_CutL"),
    *("Cur_CutH", "CellV_AlarmH", "CellV_AlarmL", "PackV_AlarmH", "PackV_AlarmL"),
    *("Temp_AlarmH", "Temp_AlarmL", "Cur_AlarmH", "DisMOS_on", "ChgMOS_on", "Short"),
    *("PwrDown", "MOS_TempH", "InternalComErr", "Reverse", "Balancing", "CellDiff_Cut"),
    *("CellDiff_Alarm", "InvalidCell", "ChgMOS_Err", "DisMOS_Err"),
)

T1 = VECTORS / "t1-examples.txt"
T2 = VECTORS / "t2-examples.txt"
POLL_CAPTURE = VECTORS / "captures" / "t2-poll-60s.txt"

M1_CELLS_T2 = (3321, 3320, 3316, 3322, 3320, 3322, 3298, 3320, 3318, 3319, 3321, 3321, 3315)
M1_CELLS_T2_ALL = (*M1_CELLS_T2, 3320, 3320)


def all_ok(count: int = 3) -> dict[int, ModuleStatus]:
    return dict.fromkeys(range(1, count + 1), OK)


def replay(store: TelemetryStore, path: Path, at: float) -> int:
    """Ingest every decodable frame of a vector file at monotonic time ``at``."""
    _, lines = load(path)
    count = 0
    for line in lines:
        if isinstance(line, VectorLine):
            frame = decode(line.can_id, line.extended, line.data)
            if frame is not None:
                store.ingest(frame, at)
                count += 1
    return count


def flags_of(*names: str) -> StatusFlags:
    return StatusFlags(
        1,
        0x24,
        MappingProxyType({name: name in names for name in SPEC_FLAG_NAMES}),
        "0000000000000000",
    )


def with_module(frames: Iterable[ModuleFrame], module: int) -> list[ModuleFrame]:
    return [dataclasses.replace(frame, module=module) for frame in frames]


def cell_frames(module: int, cells: tuple[int, ...]) -> list[ModuleFrame]:
    """Return ``0x11``-``0x14`` for 15 cell voltages (cell 1 first) and a ``0x15``."""
    out: list[ModuleFrame] = []
    for index, code in enumerate((0x11, 0x12, 0x13, 0x14)):
        first = index * 4 + 1
        chunk = {n: cells[n - 1] for n in range(first, min(first + 4, 16))}
        out.append(CellVoltages(module, code, MappingProxyType(chunk)))
    out.append(CellTemperatures(module, 0x15, (20.0, 20.5, 21.0)))
    return out


def broadcast_frames(
    module: int,
    *,
    current_raw_a: float = -1.0,
    soc: int = 50,
    soh: int = 97,
    extremes: tuple[int, int | None, int, int | None] = (3310, 3, 3300, 4),
) -> list[ModuleFrame]:
    """Return one of each broadcast frame (``0x16``, ``0x21``-``0x24``) for ``module``."""
    max_mv, max_no, min_mv, min_no = extremes
    return [
        MosBoardCycles(module, 0x16, 25.0, 22.0, 100),
        PackStatus(module, 0x21, 49.5, current_raw_a, 3305, 37.5),
        CellExtremes(module, 0x22, max_mv, max_no, min_mv, min_no, 37.5),
        TempExtremes(module, 0x23, 21.0, 1, 20.0, 2, soh, soc),
        dataclasses.replace(flags_of("DisMOS_on", "ChgMOS_on"), module=module),
    ]


def stack_frames() -> list[StackFrame]:
    return [
        ChargeLimits(0x351, 53.0, 112.5, 37.5, 45.0),
        StackSocSoh(0x355, 18, 97),
        StackMeasurements(0x356, 49.7, 13.1, 18.0),
        StackModuleCount(0x359, 3, "0000000003000001"),
    ]


def ingest_all(
    store: TelemetryStore, frames: Iterable[ModuleFrame | StackFrame], at: float
) -> None:
    for frame in frames:
        store.ingest(frame, at)


def full_store(
    settings: Settings, at: float, cells: tuple[int, ...] = (3300,) * 15
) -> TelemetryStore:
    """Return a store holding every frame for modules 1..MODULE_COUNT and the stack."""
    store = TelemetryStore(settings)
    for module in range(1, settings.module_count + 1):
        ingest_all(store, cell_frames(module, cells), at)
        ingest_all(store, broadcast_frames(module), at)
        store.ingest(EnergyThroughput(module, 0x17, 3000.0), at)
    ingest_all(store, stack_frames(), at)
    return store


def module_values(snapshot: ModuleSnapshot) -> dict[str, object]:
    return {f.name: getattr(snapshot, f.name) for f in dataclasses.fields(snapshot)}


@pytest.mark.req("TEL-03", "TEL-08", "TEL-10", "HEALTH-05", "HEALTH-20")
def test_snapshot_shape(clock: FakeClock) -> None:
    assert [m.name for m in ModuleStatus] == ["OK", "CELLS_STALE", "SILENT"]
    assert [m.value for m in ModuleStatus] == ["ok", "cells_stale", "silent"]
    assert [s.value for s in HealthState] == [
        "starting",
        "healthy",
        "degraded",
        "stale",
        "recovering",
        "dead",
    ]
    assert isinstance(RUNNING_STATES, frozenset)
    assert set(RUNNING_STATES) == {HealthState.STARTING, HealthState.HEALTHY, HealthState.DEGRADED}
    assert status_module.__doc__

    assert [f.name for f in dataclasses.fields(Snapshot)] == ["modules", "stack"]
    assert tuple(f.name for f in dataclasses.fields(ModuleSnapshot)) == MODULE_FIELDS
    assert tuple(f.name for f in dataclasses.fields(StackSnapshot)) == STACK_FIELDS

    store = TelemetryStore(make_settings(MODULE_COUNT="4"))
    snap = store.snapshot(clock(), all_ok(4))
    assert [m.module for m in snap.modules] == [1, 2, 3, 4]
    assert all(len(m.cells_mv) == 15 and len(m.temps_c) == 3 for m in snap.modules)
    for frozen in (snap, snap.modules[0], snap.stack):
        name = dataclasses.fields(frozen)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(frozen, name, None)


@pytest.mark.req(
    "TEL-07", "TEL-08", "TEL-09", "TEL-11", "TEL-18", "TEL-19", "TEL-20", "TEL-22", "TEL-26"
)
def test_module_fields_from_vectors(clock: FakeClock) -> None:
    store = TelemetryStore(make_settings())
    replay(store, T1, clock())
    clock.advance(1.0)
    replay(store, T2, clock())
    clock.advance(2.04)

    module = store.snapshot(clock(), all_ok()).modules[0]

    expected_flags = dict.fromkeys(SPEC_FLAG_NAMES, False) | {"DisMOS_on": True, "ChgMOS_on": True}
    assert module_values(module) == {
        "module": 1,
        "online": True,
        "cells_fresh": True,
        "bcast_age_s": 3.0,
        "cell_age_s": 2.0,
        "cells_mv": M1_CELLS_T2_ALL,
        "cell_max_mv": 3320,
        "cell_max_no": 6,
        "cell_min_mv": 3296,
        "cell_min_no": 7,
        "cell_avg_mv": 3316,
        "spread_mv": 24,
        "pack_v": 49.74,
        "current_a": 4.9,
        "soc": 37,
        "soh": 97,
        "cycles": 1035,
        "energy_kwh": 3623.04,
        "chg_limit_a": 37.5,
        "dis_limit_a": 37.5,
        "temps_c": (17.7, 18.0, 17.4),
        "temp_mos_c": 23.8,
        "temp_board_c": 19.5,
        "temp_max_c": 18.0,
        "temp_min_c": 17.4,
        "flags": expected_flags,
        "flags_raw": "0000030000000000",
        "chg_mos_on": True,
        "dis_mos_on": True,
        "balancing": False,
    }
    assert list(module.flags or {}) == list(SPEC_FLAG_NAMES)
    assert store.snapshot(clock(), all_ok()).modules[1].balancing is True


@pytest.mark.req("TEL-04", "TEL-05", "TEL-07")
def test_stale_values_become_none_at_the_boundary(clock: FakeClock) -> None:
    start = clock()
    store = full_store(make_settings(), start)

    fresh = store.snapshot(start + 10.0, all_ok())
    stale = store.snapshot(start + 10.1, all_ok())

    m_fresh, m_stale = fresh.modules[0], stale.modules[0]
    assert m_fresh.cells_fresh is True
    assert m_fresh.cells_mv == (3300,) * 15
    assert m_fresh.temps_c == (20.0, 20.5, 21.0)
    assert m_fresh.pack_v == 49.5
    assert m_fresh.energy_kwh == 3000.0
    assert fresh.stack.voltage_v == 49.7
    assert fresh.stack.bms_soc == 18

    assert m_stale.online is True
    assert m_stale.cells_fresh is False
    assert (m_stale.bcast_age_s, m_stale.cell_age_s) == (10.1, 10.1)
    assert m_stale.cells_mv == (None,) * 15
    assert m_stale.temps_c == (None,) * 3
    broadcast = ("cell_max_mv", "spread_mv", "pack_v", "current_a", "soc", "cycles", "flags")
    for name in (*broadcast, "flags_raw", "balancing", "energy_kwh"):
        assert getattr(m_stale, name) is None, name
    stack = stale.stack
    assert (stack.voltage_v, stack.bms_soc, stack.chg_limit_v, stack.module_count_bms) == (
        None,
        None,
        None,
        None,
    )
    assert stack.lowest_mv is None
    assert stack.total_current_a is None

    short_cells = full_store(make_settings(CELL_STALE_S="5"), start)
    module = short_cells.snapshot(start + 5.1, all_ok()).modules[0]
    assert module.energy_kwh is None
    assert module.cells_fresh is False
    assert module.pack_v == 49.5


@pytest.mark.req("TEL-04", "TEL-05", "TEL-06", "TEL-07", "TEL-16")
def test_offline_module_nulls_everything_but_identity_and_ages(clock: FakeClock) -> None:
    store = full_store(make_settings(), clock())
    clock.advance(1.25)
    store.ingest(cell_frames(2, (3300,) * 15)[0], clock())
    store.ingest(broadcast_frames(2)[-1], clock())
    clock.advance(0.5)

    snap = store.snapshot(clock(), {1: OK, 2: SILENT, 3: OK})

    module = snap.modules[1]
    assert (module.module, module.online, module.cells_fresh) == (2, False, False)
    assert (module.bcast_age_s, module.cell_age_s) == (0.5, 1.8)
    for name in MODULE_FIELDS:
        if name in IDENTITY_AND_AGES:
            continue
        value = getattr(module, name)
        if name == "cells_mv":
            assert value == (None,) * 15
        elif name == "temps_c":
            assert value == (None,) * 3
        else:
            assert value is None, name
    assert snap.stack.modules_online == 2
    assert snap.stack.total_current_a is None

    never = TelemetryStore(make_settings()).snapshot(clock(), all_ok())
    assert [(m.bcast_age_s, m.cell_age_s, m.cells_fresh) for m in never.modules] == [
        (None, None, False)
    ] * 3
    assert all(m.pack_v is None and m.cells_mv == (None,) * 15 for m in never.modules)
    assert never.stack.modules_online == 3


@pytest.mark.req("TEL-06", "TEL-16")
def test_cells_stale_status_keeps_module_online(clock: FakeClock) -> None:
    store = full_store(make_settings(), clock())

    snap = store.snapshot(clock() + 2.0, {1: CELLS_STALE, 2: OK, 3: OK})

    module = snap.modules[0]
    assert module.online is True
    assert module.cells_fresh is True
    assert module.cells_mv == (3300,) * 15
    assert module.pack_v == 49.5
    assert snap.stack.modules_online == 3


@pytest.mark.req(
    "TEL-10", "TEL-12", "TEL-13", "TEL-14", "TEL-15", "TEL-16", "TEL-17", "TEL-18", "TEL-19"
)
def test_stack_derived_values(clock: FakeClock) -> None:
    listen_only = TelemetryStore(make_settings())
    replay(listen_only, T1, clock())

    from_0x22 = listen_only.snapshot(clock(), all_ok()).stack
    assert (from_0x22.lowest_mv, from_0x22.lowest_module, from_0x22.lowest_cell) == (3282, 2, 8)
    assert (from_0x22.highest_mv, from_0x22.highest_module, from_0x22.highest_cell) == (
        3326,
        3,
        11,
    )
    assert from_0x22.spread_mv == 44

    replay(listen_only, T2, clock())
    stack = listen_only.snapshot(clock(), all_ok()).stack

    assert dataclasses.asdict(stack) == {
        "lowest_mv": 3286,
        "lowest_module": 2,
        "lowest_cell": 8,
        "highest_mv": 3327,
        "highest_module": 3,
        "highest_cell": 11,
        "spread_mv": 41,
        "total_current_a": 13.1,
        "bms_current_a": 13.1,
        "avg_soc": 18,
        "bms_soc": 18,
        "bms_soh": 97,
        "voltage_v": 49.7,
        "temp_c": 18.0,
        "chg_limit_v": 53.0,
        "chg_limit_a": 112.5,
        "dis_limit_a": 37.5,
        "dis_limit_v": 45.0,
        "module_count_bms": 3,
        "modules_online": 3,
        "any_alarm": False,
        "any_protection": False,
    }

    store = full_store(make_settings(), clock())
    store.ingest(ChargeLimits(0x351, 53.0, -112.5, -37.5, 45.0), clock())
    store.ingest(StackMeasurements(0x356, 49.7, -20.0, 18.0), clock())
    limits = store.snapshot(clock(), all_ok()).stack
    assert (limits.chg_limit_a, limits.dis_limit_a) == (112.5, 37.5)
    assert limits.bms_current_a == -20.0
    assert limits.total_current_a == 3.0


@pytest.mark.req("TEL-12")
def test_ties_resolve_to_lowest_module_then_cell(clock: FakeClock) -> None:
    store = TelemetryStore(make_settings())
    one = [3300] * 15
    one[4], one[8], one[1], one[13] = 3200, 3200, 3400, 3400
    two = [3300] * 15
    two[2], two[0] = 3200, 3400
    ingest_all(store, cell_frames(2, tuple(two)), clock())
    ingest_all(store, cell_frames(1, tuple(one)), clock())
    for module in (1, 2, 3):
        ingest_all(store, broadcast_frames(module), clock())

    stack = store.snapshot(clock(), all_ok()).stack
    assert (stack.lowest_mv, stack.lowest_module, stack.lowest_cell) == (3200, 1, 5)
    assert (stack.highest_mv, stack.highest_module, stack.highest_cell) == (3400, 1, 2)

    fallback = TelemetryStore(make_settings())
    for module in (3, 2, 1):
        ingest_all(fallback, broadcast_frames(module, extremes=(3400, module, 3200, 9)), clock())
    stack = fallback.snapshot(clock(), all_ok()).stack
    assert (stack.lowest_mv, stack.lowest_module, stack.lowest_cell) == (3200, 1, 9)
    assert (stack.highest_mv, stack.highest_module, stack.highest_cell) == (3400, 1, 1)


@pytest.mark.req("TEL-12", "TEL-13")
def test_no_candidates_gives_none(clock: FakeClock) -> None:
    empty = TelemetryStore(make_settings()).snapshot(clock(), all_ok()).stack
    silent = full_store(make_settings(), clock()).snapshot(
        clock(), dict.fromkeys((1, 2, 3), SILENT)
    )

    for stack in (empty, silent.stack):
        lowest = (stack.lowest_mv, stack.lowest_module, stack.lowest_cell)
        highest = (stack.highest_mv, stack.highest_module, stack.highest_cell)
        assert lowest == highest == (None, None, None)
        assert stack.spread_mv is None
        assert stack.avg_soc is None
        assert stack.any_alarm is None
        assert stack.any_protection is None
    assert silent.stack.modules_online == 0


@pytest.mark.req("TEL-15")
def test_avg_soc_uses_builtin_round(clock: FakeClock) -> None:
    store = TelemetryStore(make_settings())
    ingest_all(store, broadcast_frames(1, soc=37), clock())
    ingest_all(store, broadcast_frames(2, soc=8), clock())

    assert store.snapshot(clock(), all_ok()).stack.avg_soc == 22

    ingest_all(store, broadcast_frames(3, soc=8), clock())
    assert store.snapshot(clock(), all_ok()).stack.avg_soc == 18

    ingest_all(store, broadcast_frames(2, soc=10), clock())
    ingest_all(store, broadcast_frames(3, soc=101), clock())
    assert store.snapshot(clock(), all_ok()).stack.avg_soc == 24


@pytest.mark.req("TEL-09", "TEL-17")
def test_raw_rollups_count_cut_only_for_protection(clock: FakeClock) -> None:
    def rollups(*names: str) -> tuple[bool | None, bool | None]:
        store = TelemetryStore(make_settings())
        ingest_all(store, broadcast_frames(1), clock())
        store.ingest(flags_of(*names), clock())
        stack = store.snapshot(clock(), {1: OK, 2: SILENT, 3: SILENT}).stack
        return stack.any_alarm, stack.any_protection

    assert rollups() == (False, False)
    assert rollups("DisMOS_on", "ChgMOS_on", "Balancing") == (False, False)
    assert rollups("Short", "PwrDown", "MOS_TempH", "InvalidCell", "ChgMOS_Err") == (False, False)
    assert rollups("CellV_AlarmH") == (True, False)
    assert rollups("CellDiff_Alarm") == (True, False)
    assert rollups("CellV_CutH") == (False, True)
    assert rollups("CellDiff_Cut") == (False, True)
    assert rollups("Cur_AlarmH", "Cur_CutH") == (True, True)


@pytest.mark.req("TEL-18", "TEL-20", "TEL-21")
def test_negative_zero_is_published_as_zero(clock: FakeClock) -> None:
    store = TelemetryStore(make_settings())
    for module in (1, 2, 3):
        ingest_all(store, broadcast_frames(module, current_raw_a=0.0), clock())
    store.ingest(ChargeLimits(0x351, 53.0, -0.0, -0.0, 45.0), clock())
    store.ingest(StackMeasurements(0x356, 49.7, -0.0, -0.0), clock())

    snap = store.snapshot(clock(), all_ok())

    values = [m.current_a for m in snap.modules]
    stack = snap.stack
    values += [stack.total_current_a, stack.bms_current_a, stack.temp_c]
    values += [stack.chg_limit_a, stack.dis_limit_a]
    for value in values:
        assert value is not None
        assert value == 0.0
        assert math.copysign(1.0, value) == 1.0


@pytest.mark.req("TEL-12", "TEL-22", "TEL-23")
def test_zero_cells_and_minus_40_temperatures(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="dyness_b3_bms_mqtt.telemetry")
    store = TelemetryStore(make_settings())
    cells = (3300,) * 7 + (0,) + (3300,) * 7
    zero_frame = cell_frames(1, cells)[1]
    ingest_all(store, cell_frames(1, cells), clock())
    store.ingest(CellTemperatures(1, 0x15, (-40.0, 20.0, -40.0)), clock())
    ingest_all(store, cell_frames(2, (3300,) * 14 + (3250,)), clock())
    ingest_all(store, broadcast_frames(1, extremes=(3310, 3, 0, 8)), clock())
    ingest_all(store, broadcast_frames(2), clock())

    snap = store.snapshot(clock(), all_ok())
    module = snap.modules[0]
    assert module.cells_mv == (3300,) * 7 + (None,) + (3300,) * 7
    assert module.temps_c == (None, 20.0, None)
    assert (snap.stack.lowest_mv, snap.stack.lowest_module, snap.stack.lowest_cell) == (
        3250,
        2,
        15,
    )
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "module 1" in caplog.records[0].getMessage()
    assert "cell 8" in caplog.records[0].getMessage()

    store.ingest(zero_frame, clock())
    store.ingest(zero_frame, clock())
    assert len(caplog.records) == 1

    store.ingest(cell_frames(1, (3300,) * 15)[1], clock())
    store.ingest(zero_frame, clock())
    assert len(caplog.records) == 2

    store.ingest(dataclasses.replace(zero_frame, module=2), clock())
    assert len(caplog.records) == 3

    all_zero = TelemetryStore(make_settings())
    ingest_all(all_zero, cell_frames(1, (0,) * 15), clock())
    for module_no in (1, 2, 3):
        ingest_all(all_zero, broadcast_frames(module_no, extremes=(3400, 2, 3100, 6)), clock())
    stack = all_zero.snapshot(clock(), all_ok()).stack
    assert (stack.lowest_mv, stack.lowest_module, stack.lowest_cell) == (3100, 1, 6)


@pytest.mark.req("TEL-21", "TEL-24", "TEL-25")
def test_out_of_range_numbers_and_percentages(clock: FakeClock) -> None:
    store = TelemetryStore(make_settings())
    ingest_all(
        store, broadcast_frames(1, soc=101, soh=255, extremes=(3400, None, 3100, None)), clock()
    )
    ingest_all(store, broadcast_frames(2, soc=100, soh=100), clock())
    for module in (1, 2, 3):
        store.ingest(EnergyThroughput(module, 0x17, 42949672.95), clock())
    store.ingest(StackSocSoh(0x355, 65535, 101), clock())
    store.ingest(ChargeLimits(0x351, 6553.5, -3276.8, 3276.7, 6553.5), clock())
    store.ingest(StackMeasurements(0x356, 655.35, -3276.8, 3276.7), clock())

    snap = store.snapshot(clock(), {1: OK, 2: OK, 3: SILENT})

    one, two = snap.modules[0], snap.modules[1]
    assert (one.soc, one.soh) == (None, None)
    assert (two.soc, two.soh) == (100, 100)
    assert (one.cell_max_mv, one.cell_max_no, one.cell_min_mv, one.cell_min_no) == (
        3400,
        None,
        3100,
        None,
    )
    assert one.spread_mv == 300
    stack = snap.stack
    assert (stack.bms_soc, stack.bms_soh) == (None, None)
    assert stack.avg_soc == 100
    assert (stack.lowest_mv, stack.lowest_module, stack.lowest_cell) == (3100, 1, None)
    assert (stack.highest_mv, stack.highest_module, stack.highest_cell) == (3400, 1, None)
    assert (stack.chg_limit_a, stack.dis_limit_a) == (3276.8, 3276.7)

    numbers = [v for v in dataclasses.asdict(stack).values() if isinstance(v, float)]
    for module_snapshot in snap.modules:
        for value in module_values(module_snapshot).values():
            items = value if isinstance(value, tuple) else (value,)
            numbers += [v for v in items if isinstance(v, float)]
    assert numbers
    assert all(math.isfinite(v) for v in numbers)


@pytest.mark.req("TEL-02")
def test_ignores_modules_above_count_and_keeps_no_history(clock: FakeClock) -> None:
    store = TelemetryStore(make_settings(MODULE_COUNT="2"))
    for module in (1, 2, 3, 4):
        ingest_all(store, broadcast_frames(module, soc=10 * module), clock())
        ingest_all(store, cell_frames(module, (3000 + module,) * 15), clock())

    snap = store.snapshot(clock(), {1: OK, 2: OK, 3: OK, 4: OK})
    assert [m.module for m in snap.modules] == [1, 2]
    assert snap.stack.modules_online == 2
    assert snap.stack.avg_soc == 15
    assert snap.stack.highest_mv == 3002

    store.ingest(PackStatus(1, 0x21, 50.0, -2.0, 3300, 37.5), clock() + 1.0)
    store.ingest(PackStatus(1, 0x21, 51.0, -3.0, 3301, 37.5), clock() + 2.0)
    later = store.snapshot(clock() + 2.0, {1: OK, 2: OK})
    assert (later.modules[0].pack_v, later.modules[0].current_a) == (51.0, 3.0)
    assert later.modules[0].bcast_age_s == 0.0

    def stored_frames() -> int:
        return sum(len(v) for v in vars_of(store) if isinstance(v, Mapping))

    size = stored_frames()
    for step in range(100):
        ingest_all(store, broadcast_frames(1, soc=step % 100), clock() + 3.0 + step)
        store.ingest(StackSocSoh(0x355, step % 100, 97), clock() + 3.0 + step)
    assert stored_frames() == size + 1


def vars_of(store: TelemetryStore) -> list[object]:
    """Return the store's attribute values and the values one level inside its dicts."""
    values: list[object] = []
    for value in vars(store).values():
        values.append(value)
        if isinstance(value, Mapping):
            values.extend(value.values())
    return values


@pytest.mark.req("TEL-01")
def test_deterministic(clock: FakeClock) -> None:
    def run() -> Snapshot:
        store = TelemetryStore(make_settings())
        replay(store, T1, 100.0)
        replay(store, T2, 101.5)
        return store.snapshot(103.0, {1: OK, 2: CELLS_STALE, 3: SILENT})

    first, second = run(), run()
    assert first == second
    assert repr(first) == repr(second)

    tree = ast.parse(inspect.getsource(telemetry))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"time", "datetime", "threading", "socket", "asyncio", "os", "random", "can"}
    assert imported.isdisjoint(forbidden), imported & forbidden


@pytest.mark.req("TEST-42")
def test_ten_thousand_frames_under_one_second() -> None:
    _, lines = load(POLL_CAPTURE)
    received = [line for line in lines if isinstance(line, VectorLine)]
    assert received
    frames = (received * (10_000 // len(received) + 1))[:10_000]
    store = TelemetryStore(make_settings())

    started = time.perf_counter()
    for at, line in enumerate(frames):
        decoded = decode(line.can_id, line.extended, line.data)
        if decoded is not None:
            store.ingest(decoded, float(at))
    store.snapshot(float(len(frames)), all_ok())
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"10 000 frames took {elapsed:.3f} s"
