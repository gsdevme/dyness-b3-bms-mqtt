"""Alert evaluation: debounce, hysteresis, flag alerts, roll-ups and change reporting."""

import ast
import dataclasses
import inspect
from collections.abc import Mapping
from types import MappingProxyType

import pytest

from dyness_b3_bms_mqtt import alerts
from dyness_b3_bms_mqtt.alerts import (
    ALARM_FLAGS,
    MODULE_ALERTS,
    PROTECTION_FLAGS,
    ROLLUPS,
    STACK_ALERTS,
    Alert,
    AlertChange,
    AlertState,
    AlertThresholds,
    changes,
    evaluate,
    format_change,
    imbalance_pct,
)
from dyness_b3_bms_mqtt.protocol import FLAG_NAMES
from dyness_b3_bms_mqtt.telemetry import ModuleSnapshot, Snapshot, StackSnapshot
from tests.support.settings import make_settings

UNKNOWN = Alert(None, None, None, None)

_STACK_FIELDS: dict[str, object] = {f.name: None for f in dataclasses.fields(StackSnapshot)}
STACK = StackSnapshot(**{**_STACK_FIELDS, "modules_online": 3})  # type: ignore[arg-type]


def flags_of(*names: str) -> Mapping[str, bool]:
    return MappingProxyType({name: name in names for name in FLAG_NAMES})


def module(
    number: int,
    *,
    spread: int | None = None,
    cell_min: int | None = None,
    cells: tuple[int | None, ...] | None = None,
    current: float | None = None,
    flags: Mapping[str, bool] | None = None,
) -> ModuleSnapshot:
    """Return a module snapshot with only the alert inputs set; everything else is None."""
    fields: dict[str, object] = {f.name: None for f in dataclasses.fields(ModuleSnapshot)}
    fields.update(
        module=number,
        online=True,
        cells_fresh=cells is not None,
        cells_mv=cells if cells is not None else (None,) * 15,
        temps_c=(None,) * 3,
        spread_mv=spread,
        cell_min_mv=cell_min,
        current_a=current,
        flags=flags,
    )
    return ModuleSnapshot(**fields)  # type: ignore[arg-type]


def snap(*modules: ModuleSnapshot) -> Snapshot:
    return Snapshot(tuple(modules), STACK)


def spreads(*values: int | None) -> Snapshot:
    return snap(*(module(m, spread=v) for m, v in enumerate(values, start=1)))


def currents(*values: float | None) -> Snapshot:
    return snap(*(module(m, current=v) for m, v in enumerate(values, start=1)))


def thresholds(**env: str) -> AlertThresholds:
    return AlertThresholds.from_settings(make_settings(**env))


class Timeline:
    """Feeds snapshots through ``evaluate`` at explicit monotonic times."""

    def __init__(self, limits: AlertThresholds, module_count: int = 3) -> None:
        self.limits = limits
        self.state = AlertState.initial(module_count)

    def at(self, now: float, snapshot: Snapshot) -> AlertState:
        self.state = evaluate(snapshot, self.state, now, self.limits)
        return self.state

    def spread(self, now: float, value: int | None) -> Alert:
        return self.at(now, spreads(value, 30, 30)).module(1)["cell_spread"]

    def cell_low(self, now: float, snapshot_module: ModuleSnapshot) -> Alert:
        return self.at(now, snap(snapshot_module, module(2), module(3))).module(1)["cell_low"]

    def imbalance(self, now: float, *values: float | None) -> Alert:
        return self.at(now, currents(*values)).stack["current_imbalance"]


@pytest.mark.req("ALERT-01", "ALERT-02")
def test_initial_state_is_unknown() -> None:
    state = AlertState.initial(3)

    assert len(state.modules) == 3
    for number in (1, 2, 3):
        assert tuple(state.module(number)) == MODULE_ALERTS
        assert all(alert == UNKNOWN for alert in state.module(number).values())
    assert tuple(state.stack) == STACK_ALERTS
    assert all(alert == UNKNOWN for alert in state.stack.values())
    assert all(alert.state is None for alert in state.stack.values())
    assert state.module(1) is state.modules[0]
    for invalid in (0, 4):
        with pytest.raises(ValueError, match="module"):
            state.module(invalid)

    assert dataclasses.is_dataclass(state)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.stack = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        state.stack["current_imbalance"] = UNKNOWN  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        UNKNOWN.state = True  # type: ignore[misc]

    assert MODULE_ALERTS == ("cell_spread", "cell_low", "bms_alarm", "bms_protection")
    assert STACK_ALERTS == (
        "current_imbalance",
        "any_cell_spread",
        "any_cell_low",
        "any_bms_alarm",
        "any_bms_protection",
    )
    assert dict(ROLLUPS) == {
        "any_cell_spread": "cell_spread",
        "any_cell_low": "cell_low",
        "any_bms_alarm": "bms_alarm",
        "any_bms_protection": "bms_protection",
    }


@pytest.mark.req("ALERT-01")
def test_module_is_pure() -> None:
    tree = ast.parse(inspect.getsource(alerts))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    forbidden = {
        "time",
        "datetime",
        "threading",
        "socket",
        "asyncio",
        "os",
        "random",
        "can",
        "paho",
        "logging",
        "subprocess",
        "pathlib",
        "io",
        "sys",
    }
    assert imported.isdisjoint(forbidden), imported & forbidden
    assert called.isdisjoint({"open", "print", "input"}), called

    limits = thresholds()
    previous = AlertState.initial(3)
    snapshot = spreads(60, 30, None)
    first = evaluate(snapshot, previous, 1000.0, limits)
    assert evaluate(snapshot, previous, 1000.0, limits) == first
    assert previous == AlertState.initial(3)


@pytest.mark.req("ALERT-14", "ALERT-04", "ALERT-05")
def test_unknown_takes_instant_result() -> None:
    timeline = Timeline(thresholds())

    assert timeline.spread(1000.0, 60) == Alert(True, None, 60, None)
    assert timeline.spread(1001.0, None) == UNKNOWN
    assert timeline.spread(1002.0, 30) == Alert(False, None, 30, None)
    assert timeline.spread(1003.0, None) == UNKNOWN
    assert timeline.spread(1004.0, 48) == Alert(False, None, 48, None)

    restarted = Timeline(thresholds())
    assert restarted.cell_low(1000.0, module(1, cell_min=2990)) == Alert(True, None, 2990, None)
    assert restarted.cell_low(1001.0, module(1)) == UNKNOWN
    assert restarted.cell_low(1002.0, module(1, cell_min=3020)) == Alert(False, None, 3020, None)


@pytest.mark.req("ALERT-12", "ALERT-04")
def test_short_condition_never_fires_held_condition_fires() -> None:
    timeline = Timeline(thresholds(ALERT_DEBOUNCE_S="31"))
    assert timeline.spread(999.0, 30).state is False

    assert timeline.spread(1000.0, 60) == Alert(False, 1000.0, 60, None)
    assert timeline.spread(1015.0, 70) == Alert(False, 1000.0, 70, None)
    assert timeline.spread(1030.9, 60) == Alert(False, 1000.0, 60, None)
    assert timeline.spread(1030.95, 48) == Alert(False, None, 48, None)

    assert timeline.spread(1031.0, 60) == Alert(False, 1031.0, 60, None)
    assert timeline.spread(1061.9, 60).state is False
    assert timeline.spread(1062.0, 60) == Alert(True, None, 60, None)

    assert timeline.spread(1100.0, 40) == Alert(True, 1100.0, 40, None)
    assert timeline.spread(1130.9, 40).state is True
    assert timeline.spread(1130.95, 51) == Alert(True, None, 51, None)
    assert timeline.spread(1131.0, 45) == Alert(True, 1131.0, 45, None)
    assert timeline.spread(1162.0, 45) == Alert(False, None, 45, None)


@pytest.mark.req("ALERT-11", "ALERT-12")
def test_recovery_inside_band_does_not_clear() -> None:
    timeline = Timeline(thresholds())
    assert timeline.spread(1000.0, 60).state is True

    for t, value in ((1001.0, 50), (1020.0, 46), (1040.0, 50), (1100.0, 46)):
        assert timeline.spread(t, value) == Alert(True, None, value, None)
    assert timeline.spread(1200.0, 45) == Alert(True, 1200.0, 45, None)
    assert timeline.spread(1230.0, 45) == Alert(False, None, 45, None)

    for t, value in ((1231.0, 50), (1300.0, 46), (1400.0, 50)):
        assert timeline.spread(t, value) == Alert(False, None, value, None)
    assert timeline.spread(1401.0, 51) == Alert(False, 1401.0, 51, None)
    assert timeline.spread(1431.0, 51).state is True

    exact = Timeline(thresholds(ALERT_HYSTERESIS_PCT="0", ALERT_DEBOUNCE_S="0"))
    assert exact.spread(1000.0, 51).state is True
    assert exact.spread(1001.0, 50).state is False
    assert exact.spread(1002.0, 51).state is True


@pytest.mark.req("ALERT-13", "ALERT-14", "ALERT-04", "ALERT-05", "ALERT-09")
def test_stale_input_gives_unknown_and_cancels_pending() -> None:
    timeline = Timeline(thresholds())
    assert timeline.spread(1000.0, 30).state is False
    assert timeline.spread(1001.0, 60).pending_since == 1001.0
    assert timeline.spread(1020.0, None) == UNKNOWN
    assert timeline.spread(1040.0, 60) == Alert(True, None, 60, None)

    assert timeline.spread(1050.0, 30).pending_since == 1050.0
    assert timeline.spread(1060.0, None) == UNKNOWN
    assert timeline.spread(1061.0, 30) == Alert(False, None, 30, None)

    on = Timeline(thresholds(), module_count=2)
    alive = on.at(
        1000.0, snap(module(1, spread=60, cell_min=2900, flags=flags_of("Short")), module(2))
    )
    assert [alive.module(1)[key].state for key in MODULE_ALERTS] == [True, True, False, True]
    offline = dataclasses.replace(module(1), online=False)
    stale = on.at(1001.0, snap(offline, module(2)))
    assert dict(stale.module(1)) == dict.fromkeys(MODULE_ALERTS, UNKNOWN)


@pytest.mark.req("ALERT-12", "ALERT-14")
def test_zero_debounce_is_immediate() -> None:
    timeline = Timeline(thresholds(ALERT_DEBOUNCE_S="0"))
    assert timeline.spread(1000.0, 30) == Alert(False, None, 30, None)
    assert timeline.spread(1000.5, 51) == Alert(True, None, 51, None)
    assert timeline.spread(1001.0, 46) == Alert(True, None, 46, None)
    assert timeline.spread(1001.5, 45) == Alert(False, None, 45, None)

    low = Timeline(thresholds(ALERT_DEBOUNCE_S="0"))
    assert low.cell_low(1000.0, module(1, cell_min=3100)).state is False
    assert low.cell_low(1000.5, module(1, cell_min=2999)) == Alert(True, None, 2999, None)


@pytest.mark.req("ALERT-05", "ALERT-11")
def test_cell_low_sources_and_absolute_margin() -> None:
    limits = thresholds()
    sources = Timeline(limits)
    cells = (3300, None, 2990, *([3310] * 12))
    assert sources.cell_low(1000.0, module(1, cells=cells, cell_min=3200)).value == 2990
    blank = module(1, cells=(None,) * 15, cell_min=3200)
    assert blank.cells_fresh
    assert sources.cell_low(1001.0, blank).value == 3200
    stale_cells = dataclasses.replace(module(1, cells=cells, cell_min=3210), cells_fresh=False)
    assert sources.cell_low(1002.0, stale_cells).value == 3210
    assert sources.cell_low(1003.0, module(1)) == UNKNOWN

    timeline = Timeline(thresholds(ALERT_DEBOUNCE_S="0"))
    assert timeline.cell_low(1000.0, module(1, cell_min=2999)).state is True
    for t, mv in ((1001.0, 3000), (1002.0, 3020), (1003.0, 3049)):
        assert timeline.cell_low(t, module(1, cell_min=mv)) == Alert(True, None, mv, None)
    assert timeline.cell_low(1004.0, module(1, cell_min=3050)) == Alert(False, None, 3050, None)
    assert timeline.cell_low(1005.0, module(1, cell_min=3000)).state is False
    assert timeline.cell_low(1006.0, module(1, cell_min=2999)).state is True

    no_margin = Timeline(thresholds(ALERT_DEBOUNCE_S="0", ALERT_CELL_LOW_HYSTERESIS_MV="0"))
    assert no_margin.cell_low(1000.0, module(1, cell_min=2999)).state is True
    assert no_margin.cell_low(1001.0, module(1, cell_min=3000)).state is False

    wide = Timeline(
        thresholds(ALERT_HYSTERESIS_PCT="50", ALERT_DEBOUNCE_S="0", ALERT_CELL_LOW_MV="3000")
    )
    assert wide.cell_low(1000.0, module(1, cell_min=2999)).state is True
    assert wide.cell_low(1001.0, module(1, cell_min=3050)).state is False


@pytest.mark.req("ALERT-06")
def test_imbalance_formula_and_gating() -> None:
    known, pct = imbalance_pct((20.0, 20.0, 50.0), 10.0)
    assert known is True
    assert pct == pytest.approx(200 / 3)
    assert pct is not None
    assert round(pct, 1) == 66.7
    assert imbalance_pct((-20.0, -20.0, -50.0), 10.0) == (True, pct)

    assert imbalance_pct((20.0, None, 50.0), 10.0) == (False, None)
    assert imbalance_pct((3.0, 3.0, 3.9), 10.0) == (True, None)
    assert imbalance_pct((-3.0, -3.0, -3.9), 10.0) == (True, None)
    assert imbalance_pct((5.0, -5.0, 0.0), 0.0) == (True, None)
    assert sum((0.1, 0.2, -0.3)) != 0
    assert imbalance_pct((0.1, 0.2, -0.3), 0.0) == (True, None)
    assert sum((-10.0, -9.9, 29.9)) < 10.0
    boundary = imbalance_pct((-10.0, -9.9, 29.9), 10.0)
    assert boundary[0] is True
    assert boundary[1] == pytest.approx((29.9 - 10.0 / 3) / (10.0 / 3) * 100)
    assert imbalance_pct((10.0, 9.9, -29.9), 10.0)[1] == pytest.approx(boundary[1])
    assert imbalance_pct((-10.0, -9.9, 29.8), 10.0) == (True, None)
    assert imbalance_pct((4.0, 3.0, 3.0), 10.0)[1] == pytest.approx(20.0)
    assert imbalance_pct((25.0,), 10.0) == (True, 0.0)
    assert imbalance_pct((-0.5,), 0.0) == (True, 0.0)
    assert imbalance_pct((None,), 0.0) == (False, None)

    limits = thresholds()
    unknown = evaluate(currents(20.0, None, 50.0), AlertState.initial(3), 1000.0, limits)
    assert unknown.stack["current_imbalance"] == UNKNOWN
    gated = evaluate(currents(3.0, 3.0, 3.0), AlertState.initial(3), 1000.0, limits)
    assert gated.stack["current_imbalance"] == Alert(False, None, None, None)
    high = evaluate(currents(20.0, 20.0, 50.0), AlertState.initial(3), 1000.0, limits)
    assert high.stack["current_imbalance"] == Alert(True, None, pct, None)

    single = evaluate(currents(80.0), AlertState.initial(1), 1000.0, limits)
    assert single.stack["current_imbalance"] == Alert(False, None, 0.0, None)


@pytest.mark.req("ALERT-06", "ALERT-11", "ALERT-12", "ALERT-13", "ALERT-14")
def test_imbalance_alert_timeline() -> None:
    timeline = Timeline(thresholds())
    assert timeline.imbalance(1000.0, 39.5, 60.5, 50.0) == Alert(True, None, 21.0, None)

    assert timeline.imbalance(1010.0, 1.0, 1.0, 1.0) == Alert(True, 1010.0, None, None)
    assert timeline.imbalance(1039.9, 3.0, 3.0, 3.0).pending_since == 1010.0
    assert timeline.imbalance(1040.0, 3.0, 3.0, 3.0) == Alert(False, None, None, None)
    assert timeline.imbalance(1100.0, 1.0, 1.0, 1.0) == Alert(False, None, None, None)

    assert timeline.imbalance(1200.0, 40.0, 60.0, 50.0) == Alert(False, None, 20.0, None)
    assert timeline.imbalance(1201.0, 39.5, 60.5, 50.0) == Alert(False, 1201.0, 21.0, None)
    assert timeline.imbalance(1231.0, 39.5, 60.5, 50.0).state is True

    assert timeline.imbalance(1240.0, 40.5, 59.5, 50.0) == Alert(True, None, 19.0, None)
    assert timeline.imbalance(1300.0, 41.0, 59.0, 50.0) == Alert(True, 1300.0, 18.0, None)
    assert timeline.imbalance(1310.0, 41.0, None, 50.0) == UNKNOWN
    assert timeline.imbalance(1311.0, 41.0, 59.0, 50.0) == Alert(False, None, 18.0, None)
    assert timeline.imbalance(1312.0, 39.5, 60.5, 50.0).pending_since == 1312.0


@pytest.mark.req("ALERT-07", "ALERT-08", "ALERT-09", "ALERT-15")
def test_flag_alerts_follow_flags_directly() -> None:
    assert ALARM_FLAGS == (
        "CellV_AlarmH",
        "CellV_AlarmL",
        "PackV_AlarmH",
        "PackV_AlarmL",
        "Temp_AlarmH",
        "Temp_AlarmL",
        "Cur_AlarmH",
        "CellDiff_Alarm",
    )
    assert PROTECTION_FLAGS == (
        "CellV_CutH",
        "CellV_CutL",
        "PackV_CutH",
        "PackV_CutL",
        "Temp_CutH",
        "Temp_CutL",
        "Cur_CutH",
        "Short",
        "PwrDown",
        "MOS_TempH",
        "InternalComErr",
        "Reverse",
        "CellDiff_Cut",
        "InvalidCell",
        "ChgMOS_Err",
        "DisMOS_Err",
    )

    timeline = Timeline(thresholds())

    def flag_alerts(now: float, flags: Mapping[str, bool] | None) -> tuple[Alert, Alert]:
        alerts_now = timeline.at(now, snap(module(1, flags=flags), module(2), module(3)))
        return alerts_now.module(1)["bms_alarm"], alerts_now.module(1)["bms_protection"]

    assert flag_alerts(1000.0, flags_of("CellV_AlarmL")) == (
        Alert(True, None, None, ("CellV_AlarmL",)),
        Alert(False, None, None, ()),
    )
    assert flag_alerts(1000.5, flags_of()) == (
        Alert(False, None, None, ()),
        Alert(False, None, None, ()),
    )
    assert flag_alerts(1001.0, flags_of("DisMOS_on", "ChgMOS_on", "Balancing")) == (
        Alert(False, None, None, ()),
        Alert(False, None, None, ()),
    )
    for protection in ("Short", "CellV_CutL", "CellDiff_Cut", "DisMOS_Err"):
        assert flag_alerts(1002.0, flags_of(protection))[1] == Alert(
            True, None, None, (protection,)
        )
    assert flag_alerts(1003.0, flags_of(*reversed(FLAG_NAMES))) == (
        Alert(True, None, None, ALARM_FLAGS),
        Alert(True, None, None, PROTECTION_FLAGS),
    )
    assert flag_alerts(1003.1, flags_of()) == (
        Alert(False, None, None, ()),
        Alert(False, None, None, ()),
    )
    assert flag_alerts(1004.0, None) == (UNKNOWN, UNKNOWN)


@pytest.mark.req("ALERT-10")
def test_rollups() -> None:
    limits = thresholds()

    def stack(*modules: ModuleSnapshot) -> Mapping[str, Alert]:
        return evaluate(snap(*modules), AlertState.initial(len(modules)), 1000.0, limits).stack

    mixed = stack(
        module(1, spread=60, cell_min=3200, flags=flags_of("Temp_AlarmH")),
        module(2, spread=30, cell_min=2900, flags=flags_of()),
        module(3),
    )
    assert mixed["any_cell_spread"] == Alert(True, None, None, None)
    assert mixed["any_cell_low"] == Alert(True, None, None, None)
    assert mixed["any_bms_alarm"] == Alert(True, None, None, None)
    assert mixed["any_bms_protection"] == UNKNOWN

    off = stack(
        module(1, spread=30, cell_min=3200, flags=flags_of()),
        module(2, spread=30, cell_min=3200, flags=flags_of()),
    )
    assert all(off[key] == Alert(False, None, None, None) for key in ROLLUPS)

    timeline = Timeline(limits)
    timeline.at(999.0, spreads(30, 30, 30))
    pending = timeline.at(1000.0, spreads(60, 30, 30))
    assert pending.module(1)["cell_spread"].pending_since == 1000.0
    assert pending.stack["any_cell_spread"] == Alert(False, None, None, None)
    assert timeline.at(1029.9, spreads(60, 30, 30)).stack["any_cell_spread"].state is False
    fired = timeline.at(1030.0, spreads(60, 30, 30))
    assert fired.module(1)["cell_spread"].state is True
    assert fired.stack["any_cell_spread"] == Alert(True, None, None, None)
    assert timeline.at(1031.0, spreads(None, 30, 30)).stack["any_cell_spread"] == UNKNOWN


@pytest.mark.req("ALERT-01", "ALERT-09")
def test_changes_report_state_and_flag_changes_not_values() -> None:
    limits = thresholds()
    initial = AlertState.initial(2)
    first = evaluate(
        snap(module(1, spread=30, flags=flags_of()), module(2, spread=60)), initial, 1000.0, limits
    )
    reported = changes(initial, first)
    assert [(c.scope, c.key) for c in reported] == [
        (1, "cell_spread"),
        (1, "bms_alarm"),
        (1, "bms_protection"),
        (2, "cell_spread"),
        ("stack", "any_cell_spread"),
    ]
    assert reported[0] == AlertChange(1, "cell_spread", None, False, 30, None, True, False)
    assert reported[1] == AlertChange(1, "bms_alarm", None, False, None, (), True, True)
    assert reported[3] == AlertChange(2, "cell_spread", None, True, 60, None, True, False)
    assert reported[4] == AlertChange(
        "stack", "any_cell_spread", None, True, None, None, True, False
    )
    assert first.stack["any_bms_alarm"] == UNKNOWN

    values_only = evaluate(
        snap(module(1, spread=35, flags=flags_of()), module(2, spread=70)), first, 1001.0, limits
    )
    assert changes(first, values_only) == []
    pending = evaluate(
        snap(module(1, spread=60, flags=flags_of()), module(2, spread=70)), first, 1002.0, limits
    )
    assert pending.module(1)["cell_spread"].pending_since == 1002.0
    assert changes(first, pending) == []

    alarm = evaluate(
        snap(module(1, spread=30, flags=flags_of("Temp_AlarmH")), module(2, spread=70)),
        first,
        1003.0,
        limits,
    )
    more = evaluate(
        snap(module(1, spread=30, flags=flags_of("Temp_AlarmH", "CellV_AlarmL")), module(2)),
        alarm,
        1004.0,
        limits,
    )
    assert changes(alarm, more) == [
        AlertChange(1, "bms_alarm", True, True, None, ("CellV_AlarmL", "Temp_AlarmH"), False, True),
        AlertChange(2, "cell_spread", True, None, None, None, True, False),
        AlertChange("stack", "any_cell_spread", True, None, None, None, True, False),
    ]
    assert changes(more, more) == []


@pytest.mark.req("ALERT-17")
def test_change_log_format() -> None:
    limits = thresholds(ALERT_CURRENT_IMBALANCE_PCT="20.5")
    expected = {
        AlertChange(2, "cell_low", False, True, 2994, None, True, False): (
            "alert module 2 cell_low: off -> on (value=2994 mV, threshold=3000 mV)"
        ),
        AlertChange(1, "cell_spread", True, None, None, None, True, False): (
            "alert module 1 cell_spread: on -> unknown (value=null, threshold=50 mV)"
        ),
        AlertChange("stack", "current_imbalance", None, True, 200 / 3, None, True, False): (
            "alert stack current_imbalance: unknown -> on (value=66.7%, threshold=20.5%)"
        ),
        AlertChange("stack", "current_imbalance", True, False, None, None, True, False): (
            "alert stack current_imbalance: on -> off (value=null, threshold=20.5%)"
        ),
        AlertChange(3, "bms_protection", False, True, None, ("CellV_CutL", "Short"), True, True): (
            "alert module 3 bms_protection: off -> on (flags=[CellV_CutL, Short])"
        ),
        AlertChange(3, "bms_protection", True, False, None, (), True, True): (
            "alert module 3 bms_protection: on -> off (flags=[])"
        ),
        AlertChange(3, "bms_alarm", True, None, None, None, True, True): (
            "alert module 3 bms_alarm: on -> unknown (flags=null)"
        ),
        AlertChange(
            1, "bms_alarm", True, True, None, ("CellV_AlarmL", "Temp_AlarmH"), False, True
        ): ("alert module 1 bms_alarm flags: [CellV_AlarmL, Temp_AlarmH]"),
        AlertChange("stack", "any_cell_low", False, True, None, None, True, False): (
            "alert stack any_cell_low: off -> on"
        ),
    }
    for change, line in expected.items():
        assert format_change(change, limits) == line

    timeline = Timeline(thresholds(ALERT_DEBOUNCE_S="0"))
    before = timeline.at(1000.0, snap(module(1), module(2, cell_min=3100), module(3)))
    after = timeline.at(1001.0, snap(module(1), module(2, cell_min=2994), module(3)))
    assert [format_change(c, limits) for c in changes(before, after)] == [
        "alert module 2 cell_low: off -> on (value=2994 mV, threshold=3000 mV)",
        "alert stack any_cell_low: unknown -> on",
    ]


@pytest.mark.req("ALERT-18")
def test_thresholds_come_from_settings() -> None:
    assert thresholds() == AlertThresholds(
        cell_spread_mv=50,
        cell_low_mv=3000,
        current_imbalance_pct=20.0,
        imbalance_min_current_a=10.0,
        debounce_s=30.0,
        hysteresis_pct=10.0,
        cell_low_hysteresis_mv=50,
    )
    custom = thresholds(
        ALERT_CELL_SPREAD_MV="70",
        ALERT_CELL_LOW_MV="2900",
        ALERT_CURRENT_IMBALANCE_PCT="35.5",
        ALERT_IMBALANCE_MIN_CURRENT_A="2.5",
        ALERT_DEBOUNCE_S="0",
        ALERT_HYSTERESIS_PCT="20",
        ALERT_CELL_LOW_HYSTERESIS_MV="100",
    )
    assert custom == AlertThresholds(70, 2900, 35.5, 2.5, 0.0, 20.0, 100)

    timeline = Timeline(custom)
    assert timeline.spread(1000.0, 70).state is False
    assert timeline.spread(1001.0, 71).state is True
    assert timeline.spread(1002.0, 57).state is True
    assert timeline.spread(1003.0, 56).state is False
    assert timeline.cell_low(1004.0, module(1, cell_min=2950)).state is False
    assert timeline.cell_low(1005.0, module(1, cell_min=2899)).state is True
    assert timeline.cell_low(1006.0, module(1, cell_min=2999)).state is True
    assert timeline.cell_low(1007.0, module(1, cell_min=3000)).state is False
    assert timeline.imbalance(1008.0, 1.0, 1.0, 1.0) == Alert(False, None, 0.0, None)
    assert timeline.imbalance(1009.0, 1.0, 1.0, 1.6).state is False
    assert timeline.imbalance(1010.0, 1.0, 1.0, 1.8).state is True
    assert timeline.imbalance(1011.0, 1.0, 1.0, 1.6).state is True
    assert timeline.imbalance(1012.0, 0.5, 0.5, 1.0) == Alert(False, None, None, None)

    numbers = {
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(alerts)))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    }
    assert numbers <= {0, 1, 100}, numbers
