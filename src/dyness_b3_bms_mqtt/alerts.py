"""Debounced, hysteresis-protected alerts over telemetry snapshots (``docs/specs/05-alerts.md``).

:func:`evaluate` turns a :class:`~dyness_b3_bms_mqtt.telemetry.Snapshot` and the previous
:class:`AlertState` into the next one; :func:`changes` lists what moved between two states
and :func:`format_change` renders one change as the ALERT-17 log line. The caller owns the
clock, the logging and the publishing: this module is pure, with no I/O, no threads and no
clock reads (ALERT-01).

Every alert is tri-state (``True``, ``False`` or ``None`` for unknown). The threshold
alerts (``cell_spread``, ``cell_low``, ``current_imbalance``) are debounced with
hysteresis; an unknown input makes them unknown at once and a known input after unknown
takes the instantaneous result (ALERT-13, ALERT-14). The BMS flag alerts follow the
``0x24`` flags directly (ALERT-15) and the stack roll-ups follow the module alerts in the
same evaluation (ALERT-10). Every threshold comes from :class:`AlertThresholds`, built
once from :class:`~dyness_b3_bms_mqtt.config.Settings` (ALERT-18).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, NamedTuple, Self

from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.protocol import FLAG_CATEGORY, FlagCategory
from dyness_b3_bms_mqtt.telemetry import ModuleSnapshot, Snapshot

MODULE_ALERTS = ("cell_spread", "cell_low", "bms_alarm", "bms_protection")
STACK_ALERTS = (
    "current_imbalance",
    "any_cell_spread",
    "any_cell_low",
    "any_bms_alarm",
    "any_bms_protection",
)
ROLLUPS: Mapping[str, str] = MappingProxyType(
    {
        "any_cell_spread": "cell_spread",
        "any_cell_low": "cell_low",
        "any_bms_alarm": "bms_alarm",
        "any_bms_protection": "bms_protection",
    }
)
"""Each stack roll-up and the module alert it aggregates (ALERT-10)."""

ALARM_FLAGS = tuple(n for n, c in FLAG_CATEGORY.items() if c is FlagCategory.ALARM)
"""The ``alarm`` flags behind ``bms_alarm``, in canonical TEL-09 order (ALERT-07)."""

PROTECTION_FLAGS = tuple(
    n for n, c in FLAG_CATEGORY.items() if c in (FlagCategory.PROTECTION, FlagCategory.FAULT)
)
"""The ``protection`` and ``fault`` flags behind ``bms_protection``, in TEL-09 order (ALERT-08)."""

FLAG_ALERTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"bms_alarm": ALARM_FLAGS, "bms_protection": PROTECTION_FLAGS}
)
"""Each BMS flag alert and the flags it follows (ALERT-07, ALERT-08)."""

type Scope = int | Literal["stack"]
"""A module number (1-based) or the whole stack."""

_PERCENT = 100


@dataclass(frozen=True, slots=True)
class AlertThresholds:
    """The alert thresholds, debounce and hysteresis settings (ALERT-11, ALERT-12, ALERT-18)."""

    cell_spread_mv: int
    cell_low_mv: int
    current_imbalance_pct: float
    imbalance_min_current_a: float
    debounce_s: float
    hysteresis_pct: float
    cell_low_hysteresis_mv: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Take every value from the ``ALERT_*`` settings read at start-up."""
        return cls(
            cell_spread_mv=settings.alert_cell_spread_mv,
            cell_low_mv=settings.alert_cell_low_mv,
            current_imbalance_pct=settings.alert_current_imbalance_pct,
            imbalance_min_current_a=settings.alert_imbalance_min_current_a,
            debounce_s=settings.alert_debounce_s,
            hysteresis_pct=settings.alert_hysteresis_pct,
            cell_low_hysteresis_mv=settings.alert_cell_low_hysteresis_mv,
        )


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert's state.

    ``state`` is on, off or unknown (``None``). ``pending_since`` is the monotonic time a
    debounced transition started, or ``None`` when none is pending. ``value`` is the input
    value (``None`` when unknown or, for ``current_imbalance``, below the minimum current);
    flag alerts and roll-ups have none. ``flags`` holds the active flag names of a BMS flag
    alert, empty when off and ``None`` when unknown (ALERT-09); other alerts have ``None``.
    """

    state: bool | None
    pending_since: float | None = None
    value: float | None = None
    flags: tuple[str, ...] | None = None


_UNKNOWN = Alert(None)


@dataclass(frozen=True, slots=True)
class AlertState:
    """Every module alert (``modules[m - 1]``) and every stack alert, keyed by alert name."""

    modules: tuple[Mapping[str, Alert], ...]
    stack: Mapping[str, Alert]

    @classmethod
    def initial(cls, module_count: int) -> Self:
        """Return every alert unknown with no pending transition (ALERT-01)."""
        return cls(
            tuple(_frozen(dict.fromkeys(MODULE_ALERTS, _UNKNOWN)) for _ in range(module_count)),
            _frozen(dict.fromkeys(STACK_ALERTS, _UNKNOWN)),
        )

    def module(self, module: int) -> Mapping[str, Alert]:
        """Return the alerts of module ``module`` (1-based).

        Raises:
            ValueError: ``module`` is not in 1..the number of modules.

        """
        if not 1 <= module <= len(self.modules):
            raise ValueError(f"module {module} is not in 1..{len(self.modules)}")
        return self.modules[module - 1]


@dataclass(frozen=True, slots=True)
class AlertChange:
    """A change of an alert's state or active flag list between two evaluations.

    ``value`` and ``flags`` are the new ones. ``state_changed`` and ``flags_changed`` say
    which of the two moved; a change of the input value alone is never reported.
    """

    scope: Scope
    key: str
    old: bool | None
    new: bool | None
    value: float | None
    flags: tuple[str, ...] | None
    state_changed: bool
    flags_changed: bool


class _Conditions(NamedTuple):
    """Whether a debounced alert's on and clear conditions hold for the current input."""

    on: bool
    clear: bool


def evaluate(
    snapshot: Snapshot, previous: AlertState, now: float, thresholds: AlertThresholds
) -> AlertState:
    """Return the alert state for ``snapshot`` at monotonic time ``now`` (ALERT-01).

    ``previous`` must hold the same modules as ``snapshot``, in the same order.
    """
    modules = tuple(
        _module_alerts(m, alerts, now, thresholds)
        for m, alerts in zip(snapshot.modules, previous.modules, strict=True)
    )
    known, pct = imbalance_pct(
        [m.current_a for m in snapshot.modules], thresholds.imbalance_min_current_a
    )
    stack = {
        "current_imbalance": _debounced(
            previous.stack["current_imbalance"],
            pct,
            _imbalance_conditions(known, pct, thresholds),
            now,
            thresholds.debounce_s,
        )
    }
    for rollup, key in ROLLUPS.items():
        stack[rollup] = Alert(_any(alerts[key].state for alerts in modules))
    return AlertState(modules, _frozen(stack))


def changes(previous: AlertState, current: AlertState) -> list[AlertChange]:
    """List the alerts whose state or active flag list differs, modules first, then stack."""
    scoped: list[tuple[Scope, Mapping[str, Alert], Mapping[str, Alert]]] = [
        (number, before, after)
        for number, (before, after) in enumerate(
            zip(previous.modules, current.modules, strict=True), start=1
        )
    ]
    scoped.append(("stack", previous.stack, current.stack))
    found: list[AlertChange] = []
    for scope, before, after in scoped:
        for key, new in after.items():
            old = before[key]
            state_changed = old.state != new.state
            flags_changed = old.flags != new.flags
            if state_changed or flags_changed:
                found.append(
                    AlertChange(
                        scope,
                        key,
                        old.state,
                        new.state,
                        new.value,
                        new.flags,
                        state_changed,
                        flags_changed,
                    )
                )
    return found


def imbalance_pct(
    currents: Sequence[float | None], min_current: float
) -> tuple[bool, float | None]:
    """Return ``(known, imbalance_pct)`` for the module currents (ALERT-06).

    ``currents`` are the snapshot's module ``current_a`` values, already rounded to 1 dp
    (TEL-20). ``known`` is false, with no percentage, when any current is unknown. The
    percentage is ``None`` while the stack current ``total_current_a`` (the sum rounded to
    1 dp, TEL-14) is zero or its magnitude is below ``min_current``, which is a known state
    that satisfies the clear condition. Otherwise it is the largest deviation of a module
    current from their mean as a percentage of the absolute mean, with neither the mean nor
    the result rounded; with one module it is always 0.
    """
    known = [c for c in currents if c is not None]
    if not known or len(known) != len(currents):
        return False, None
    total = sum(known)
    total_current_a = round(total, 1) + 0.0
    if total_current_a == 0 or abs(total_current_a) < min_current:
        return True, None
    mean = total / len(known)
    return True, max(abs(c - mean) for c in known) / abs(mean) * _PERCENT


def format_change(change: AlertChange, thresholds: AlertThresholds) -> str:
    """Return the ``INFO`` log message for ``change`` without the level (ALERT-17).

    A state change of a threshold alert gives its value and threshold, of a flag alert its
    active flag list, and of a roll-up neither. A flag list change alone gives the new list.
    """
    where = "stack" if change.scope == "stack" else f"module {change.scope}"
    if not change.state_changed:
        return f"alert {where} {change.key} flags: {_flag_list(change.flags)}"
    line = f"alert {where} {change.key}: {_word(change.old)} -> {_word(change.new)}"
    threshold = _threshold(change.key, thresholds)
    if threshold is not None:
        return f"{line} (value={_value(change.key, change.value)}, threshold={threshold})"
    if change.key in FLAG_ALERTS:
        return f"{line} (flags={_flag_list(change.flags)})"
    return line


def _module_alerts(
    module: ModuleSnapshot, previous: Mapping[str, Alert], now: float, t: AlertThresholds
) -> Mapping[str, Alert]:
    """Return the four module alerts for ``module`` (ALERT-04, 05, 07, 08)."""
    spread = module.spread_mv
    low = _lowest_cell(module)
    return _frozen(
        {
            "cell_spread": _debounced(
                previous["cell_spread"],
                spread,
                _greater_than(spread, t.cell_spread_mv, t.hysteresis_pct),
                now,
                t.debounce_s,
            ),
            "cell_low": _debounced(
                previous["cell_low"],
                low,
                _less_than(low, t.cell_low_mv, t.cell_low_hysteresis_mv),
                now,
                t.debounce_s,
            ),
            **{key: _flag_alert(module.flags, names) for key, names in FLAG_ALERTS.items()},
        }
    )


def _lowest_cell(module: ModuleSnapshot) -> int | None:
    """Return the ``cell_low`` input: the lowest fresh cell, else ``cell_min_mv`` (ALERT-05)."""
    cells = [mv for mv in module.cells_mv if mv is not None]
    if module.cells_fresh and cells:
        return min(cells)
    return module.cell_min_mv


def _greater_than(
    value: float | None, threshold: float, hysteresis_pct: float
) -> _Conditions | None:
    """Return the conditions of a "greater than" alert, ``None`` when unknown (ALERT-11)."""
    if value is None:
        return None
    return _Conditions(value > threshold, value <= threshold * (1 - hysteresis_pct / _PERCENT))


def _less_than(value: float | None, threshold: float, margin: float) -> _Conditions | None:
    """Return the conditions of a "less than" alert, ``None`` when unknown (ALERT-11)."""
    if value is None:
        return None
    return _Conditions(value < threshold, value >= threshold + margin)


def _imbalance_conditions(known: bool, pct: float | None, t: AlertThresholds) -> _Conditions | None:
    """Return the ``current_imbalance`` conditions; low current only clears (ALERT-06)."""
    if not known:
        return None
    if pct is None:
        return _Conditions(on=False, clear=True)
    return _greater_than(pct, t.current_imbalance_pct, t.hysteresis_pct)


def _debounced(
    previous: Alert,
    value: float | None,
    conditions: _Conditions | None,
    now: float,
    debounce_s: float,
) -> Alert:
    """Advance one debounced alert by one evaluation (ALERT-12, ALERT-13, ALERT-14).

    Unknown conditions make the alert unknown and cancel any pending transition. An unknown
    alert takes the instantaneous on condition. Otherwise the condition that would change
    the state (on while off, clear while on) must hold at every evaluation for at least
    ``debounce_s`` from its first one; any evaluation where it does not hold cancels it.
    """
    if conditions is None:
        return Alert(None, None, value)
    if previous.state is None:
        return Alert(conditions.on, None, value)
    holds = conditions.clear if previous.state else conditions.on
    if not holds:
        return Alert(previous.state, None, value)
    since = now if previous.pending_since is None else previous.pending_since
    if now - since >= debounce_s:
        return Alert(not previous.state, None, value)
    return Alert(previous.state, since, value)


def _flag_alert(flags: Mapping[str, bool] | None, names: tuple[str, ...]) -> Alert:
    """Return a BMS flag alert that follows ``flags`` directly (ALERT-07 to 09, ALERT-15)."""
    if flags is None:
        return _UNKNOWN
    active = tuple(name for name in names if flags[name])
    return Alert(bool(active), flags=active)


def _any(states: Iterable[bool | None]) -> bool | None:
    """Return on if any state is on, else unknown if any is unknown, else off (ALERT-10)."""
    seen = list(states)
    if True in seen:
        return True
    if None in seen:
        return None
    return False


def _frozen(alerts: dict[str, Alert]) -> Mapping[str, Alert]:
    """Return a read-only view of ``alerts``."""
    return MappingProxyType(alerts)


def _word(state: bool | None) -> str:
    """Return ``on``, ``off`` or ``unknown``."""
    return "unknown" if state is None else "on" if state else "off"


def _threshold(key: str, t: AlertThresholds) -> str | None:
    """Return the threshold of a threshold alert with its unit, ``None`` for other alerts."""
    match key:
        case "cell_spread":
            return f"{t.cell_spread_mv} mV"
        case "cell_low":
            return f"{t.cell_low_mv} mV"
        case "current_imbalance":
            return f"{t.current_imbalance_pct:g}%"
    return None


def _value(key: str, value: float | None) -> str:
    """Return an input value with its unit; percentages at 1 dp, ``null`` when absent."""
    if value is None:
        return "null"
    if key == "current_imbalance":
        return f"{value:.1f}%"
    return f"{value:.0f} mV"


def _flag_list(flags: tuple[str, ...] | None) -> str:
    """Return ``[A, B]``, ``[]`` or ``null``."""
    return "null" if flags is None else f"[{', '.join(flags)}]"
