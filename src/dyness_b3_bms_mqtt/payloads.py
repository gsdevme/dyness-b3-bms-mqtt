"""The module, stack and service state payloads of ``06-mqtt.md`` section 4.

Each builder returns a ``dict`` whose keys are exactly the schema of MQTT-16, MQTT-17 or
MQTT-18, in the spec's table order, with ``None`` wherever a value is unknown or stale;
:func:`encode` turns one into the published JSON text (MQTT-14). The builders are pure:
the caller passes the snapshot, the alert state and the wall-clock time ``ts`` (Unix
seconds), which every payload carries as ISO 8601 UTC to the second (MQTT-15).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from dyness_b3_bms_mqtt.alerts import Alert
from dyness_b3_bms_mqtt.linkstate import CanState
from dyness_b3_bms_mqtt.protocol import CELLS_PER_MODULE
from dyness_b3_bms_mqtt.status import HealthState
from dyness_b3_bms_mqtt.telemetry import TEMPERATURE_SENSORS, ModuleSnapshot, StackSnapshot
from dyness_b3_bms_mqtt.timefmt import iso_utc

type Payload = dict[str, object]
type LocalLogStatus = Literal["ok", "disabled", "error"]

_CELL_KEYS: Final = tuple(f"cell_{n:02d}_mv" for n in range(1, CELLS_PER_MODULE + 1))
_TEMP_KEYS: Final = tuple(f"temp_{n}_c" for n in range(1, TEMPERATURE_SENSORS + 1))

MODULE_KEYS: Final[tuple[str, ...]] = (
    "module",
    "name",
    "ts",
    "online",
    "cells_fresh",
    "bcast_age_s",
    "cell_age_s",
    *_CELL_KEYS,
    "cell_min_mv",
    "cell_min_no",
    "cell_max_mv",
    "cell_max_no",
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
    *_TEMP_KEYS,
    "temp_mos_c",
    "temp_board_c",
    "temp_max_c",
    "temp_min_c",
    "flags_raw",
    "chg_mos_on",
    "dis_mos_on",
    "balancing",
    "bms_alarm",
    "bms_alarm_flags",
    "bms_protection",
    "bms_protection_flags",
    "alert_cell_spread",
    "alert_cell_low",
)
"""The keys of ``<B>/module/<m>/state`` in MQTT-16 table order."""

STACK_KEYS: Final[tuple[str, ...]] = (
    "ts",
    "modules_online",
    "module_count_bms",
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
    "any_alarm",
    "any_protection",
    "alert_current_imbalance",
    "imbalance_pct",
    "alert_any_cell_spread",
    "alert_any_cell_low",
    "alert_any_bms_alarm",
    "alert_any_bms_protection",
)
"""The keys of ``<B>/stack/state`` in MQTT-17 table order."""

SERVICE_KEYS: Final[tuple[str, ...]] = (
    "ts",
    "version",
    "health",
    "health_reason",
    "can_state",
    "can_berr_tx",
    "can_berr_rx",
    "can_restarts",
    "bus_age_s",
    "error_frames",
    "dropped_frames",
    "echo_frames",
    "malformed_frames",
    "unknown_frames",
    "polls_sent",
    "polling_suspended",
    "escalation",
    "service_restarts",
    "last_exit_code",
    "last_exit_result",
    "uptime_s",
    "mqtt_reconnects",
    "local_log",
)
"""The keys of ``<B>/service/state`` in MQTT-18 table order."""

_STACK_FIELDS: Final = (
    "modules_online",
    "module_count_bms",
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
    "any_alarm",
    "any_protection",
)
"""The stack keys copied unchanged from the ``StackSnapshot`` field of the same name."""


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    """The values behind ``<B>/service/state``, gathered by the main loop (MQTT-18).

    ``None`` means unknown and is published as ``null``. ``can_state`` is published as
    ``null`` while it is ``UNKNOWN``; ``bus_age_s`` is published to 1 dp and ``uptime_s``
    as whole seconds. ``last_exit_code`` is an exit status or a signal name such as
    ``"ABRT"``.
    """

    version: str
    health: HealthState
    health_reason: str | None
    can_state: CanState
    can_berr_tx: int | None
    can_berr_rx: int | None
    can_restarts: int | None
    bus_age_s: float | None
    error_frames: int
    dropped_frames: int | None
    echo_frames: int
    malformed_frames: int
    unknown_frames: int
    polls_sent: int
    polling_suspended: bool
    escalation: int
    service_restarts: int
    last_exit_code: int | str | None
    last_exit_result: str | None
    uptime_s: float
    mqtt_reconnects: int
    local_log: LocalLogStatus


def encode(obj: Mapping[str, object]) -> str:
    """Return ``obj`` as compact JSON with sorted keys and literal non-ASCII (MQTT-14).

    Raises:
        ValueError: ``obj`` holds a NaN or infinity, which JSON cannot carry.

    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def module_payload(
    ms: ModuleSnapshot, alerts: Mapping[str, Alert], name: str, ts: float
) -> Payload:
    """Return the MQTT-16 payload of one module.

    ``alerts`` are that module's alerts (``AlertState.module(m)``) and ``name`` its display
    name (``Settings.module_name``).
    """
    payload: Payload = {
        "module": ms.module,
        "name": name,
        "ts": iso_utc(ts),
        "online": ms.online,
        "cells_fresh": ms.cells_fresh,
        "bcast_age_s": ms.bcast_age_s,
        "cell_age_s": ms.cell_age_s,
    }
    payload.update(zip(_CELL_KEYS, ms.cells_mv, strict=True))
    payload.update(
        {
            "cell_min_mv": ms.cell_min_mv,
            "cell_min_no": ms.cell_min_no,
            "cell_max_mv": ms.cell_max_mv,
            "cell_max_no": ms.cell_max_no,
            "cell_avg_mv": ms.cell_avg_mv,
            "spread_mv": ms.spread_mv,
            "pack_v": ms.pack_v,
            "current_a": ms.current_a,
            "soc": ms.soc,
            "soh": ms.soh,
            "cycles": ms.cycles,
            "energy_kwh": ms.energy_kwh,
            "chg_limit_a": ms.chg_limit_a,
            "dis_limit_a": ms.dis_limit_a,
        }
    )
    payload.update(zip(_TEMP_KEYS, ms.temps_c, strict=True))
    payload.update(
        {
            "temp_mos_c": ms.temp_mos_c,
            "temp_board_c": ms.temp_board_c,
            "temp_max_c": ms.temp_max_c,
            "temp_min_c": ms.temp_min_c,
            "flags_raw": ms.flags_raw,
            "chg_mos_on": ms.chg_mos_on,
            "dis_mos_on": ms.dis_mos_on,
            "balancing": ms.balancing,
            "bms_alarm": alerts["bms_alarm"].state,
            "bms_alarm_flags": _flag_list(alerts["bms_alarm"]),
            "bms_protection": alerts["bms_protection"].state,
            "bms_protection_flags": _flag_list(alerts["bms_protection"]),
            "alert_cell_spread": alerts["cell_spread"].state,
            "alert_cell_low": alerts["cell_low"].state,
        }
    )
    return payload


def stack_payload(ss: StackSnapshot, alerts: Mapping[str, Alert], ts: float) -> Payload:
    """Return the MQTT-17 payload; ``alerts`` are the stack alerts (``AlertState.stack``).

    ``imbalance_pct`` is the ``current_imbalance`` alert's input value rounded to 1 dp,
    ``null`` while that value is unknown or gated off (ALERT-06).
    """
    payload: Payload = {"ts": iso_utc(ts)}
    payload.update((key, getattr(ss, key)) for key in _STACK_FIELDS)
    payload.update(
        {
            "alert_current_imbalance": alerts["current_imbalance"].state,
            "imbalance_pct": _round(alerts["current_imbalance"].value),
            "alert_any_cell_spread": alerts["any_cell_spread"].state,
            "alert_any_cell_low": alerts["any_cell_low"].state,
            "alert_any_bms_alarm": alerts["any_bms_alarm"].state,
            "alert_any_bms_protection": alerts["any_bms_protection"].state,
        }
    )
    return payload


def service_payload(info: ServiceInfo, ts: float) -> Payload:
    """Return the MQTT-18 payload for ``info``."""
    return {
        "ts": iso_utc(ts),
        "version": info.version,
        "health": info.health.value,
        "health_reason": info.health_reason,
        "can_state": info.can_state.published,
        "can_berr_tx": info.can_berr_tx,
        "can_berr_rx": info.can_berr_rx,
        "can_restarts": info.can_restarts,
        "bus_age_s": _round(info.bus_age_s),
        "error_frames": info.error_frames,
        "dropped_frames": info.dropped_frames,
        "echo_frames": info.echo_frames,
        "malformed_frames": info.malformed_frames,
        "unknown_frames": info.unknown_frames,
        "polls_sent": info.polls_sent,
        "polling_suspended": info.polling_suspended,
        "escalation": info.escalation,
        "service_restarts": info.service_restarts,
        "last_exit_code": info.last_exit_code,
        "last_exit_result": info.last_exit_result,
        "uptime_s": int(info.uptime_s),
        "mqtt_reconnects": info.mqtt_reconnects,
        "local_log": info.local_log,
    }


def _flag_list(alert: Alert) -> list[str] | None:
    """Return a BMS flag alert's active flags as a JSON array, ``None`` when unknown."""
    return None if alert.flags is None else list(alert.flags)


def _round(value: float | None) -> float | None:
    """Round to 1 dp without ever producing ``-0.0`` (TEL-21); ``None`` stays ``None``."""
    return None if value is None else round(value, 1) + 0.0
