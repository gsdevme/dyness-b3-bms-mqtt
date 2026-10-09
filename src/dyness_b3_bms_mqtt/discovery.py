"""Home Assistant device-based MQTT discovery (``docs/specs/07-home-assistant.md``).

``build_discovery`` is a pure function of the settings and the package version (HA-02): it
returns the retained ``(topic, payload)`` messages to publish, one JSON config per device, so
its output can be pinned by golden files. Publishing them (retained, QoS 1) is the caller's
job.

The component tables of HA-10, HA-11 and HA-12 are restated once below as ``Component``
rows in spec order; every option of a rendered component is derived from its row (HA-09).
Option keys are always the full Home Assistant names, never the abbreviations.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal

from dyness_b3_bms_mqtt.config import MAX_MODULES, Settings
from dyness_b3_bms_mqtt.linkstate import CanState
from dyness_b3_bms_mqtt.status import HealthState
from dyness_b3_bms_mqtt.topics import Topics

SUPPORT_URL: Final = "https://github.com/gsdevme/dyness-b3-bms-mqtt"
ORIGIN_NAME: Final = "dyness-b3-bms-mqtt"
MANUFACTURER: Final = "Dyness"
MODEL: Final = "B3"

HEALTH_OPTIONS: Final[tuple[str, ...]] = tuple(state.value for state in HealthState)
CAN_STATE_OPTIONS: Final[tuple[str, ...]] = tuple(
    published for state in CanState if (published := state.published) is not None
)
LOCAL_LOG_OPTIONS: Final[tuple[str, ...]] = ("ok", "disabled", "error")

type Platform = Literal["sensor", "binary_sensor"]
type Template = Literal["S", "B", "B+"]
type Device = Literal["module", "stack"]
type Message = tuple[str, str]

_DIAGNOSTIC: Final = "diagnostic"


@dataclass(frozen=True, slots=True)
class Component:
    """One row of the HA-10, HA-11 or HA-12 tables.

    ``key`` is both the component key and the state JSON key it reads (HA-07). ``None``
    stands for the tables' "—" and omits the option. ``template`` is the template code of
    HA-09. ``service`` components read ``<B>/service/state`` instead of the device's state
    topic (HA-12).
    """

    key: str
    name: str
    platform: Platform
    device_class: str | None
    unit: str | None
    state_class: str | None
    category: str | None
    precision: int | None
    template: Template
    enabled: bool = True
    options: tuple[str, ...] | None = None
    service: bool = False


@dataclass(frozen=True, slots=True)
class RemovedComponent:
    """A component dropped from every device of kind ``device`` (HA-22).

    It is rendered as ``{key: {"platform": platform}}``, the removal signal.
    """

    device: Device
    key: str
    platform: Platform


def _measured(
    key: str, name: str, device_class: str | None, unit: str, precision: int
) -> Component:
    """Return a ``measurement`` sensor with a unit, plain template and default category."""
    return Component(key, name, "sensor", device_class, unit, "measurement", None, precision, "S")


def _plain(key: str, name: str, category: str | None = None) -> Component:
    """Return a sensor with no device class, unit, state class or precision."""
    return Component(key, name, "sensor", None, None, None, category, None, "S")


def _binary(key: str, name: str, device_class: str | None, template: Template = "B") -> Component:
    """Return a binary sensor in the default category."""
    return Component(key, name, "binary_sensor", device_class, None, None, None, None, template)


MODULE_COMPONENTS: Final[tuple[Component, ...]] = (
    *(_measured(f"cell_{n:02d}_mv", f"Cell {n} voltage", "voltage", "mV", 0) for n in range(1, 16)),
    _measured("cell_min_mv", "Lowest cell voltage", "voltage", "mV", 0),
    _plain("cell_min_no", "Lowest cell"),
    _measured("cell_max_mv", "Highest cell voltage", "voltage", "mV", 0),
    _plain("cell_max_no", "Highest cell"),
    _measured("cell_avg_mv", "Average cell voltage", "voltage", "mV", 0),
    _measured("spread_mv", "Cell spread", "voltage", "mV", 0),
    _measured("pack_v", "Pack voltage", "voltage", "V", 2),
    _measured("current_a", "Current", "current", "A", 1),
    _measured("soc", "State of charge", "battery", "%", 0),
    _measured("soh", "State of health", None, "%", 0),
    Component("cycles", "Cycles", "sensor", None, None, "total_increasing", None, 0, "S"),
    Component(
        "energy_kwh",
        "Lifetime energy throughput",
        "sensor",
        "energy",
        "kWh",
        "total_increasing",
        None,
        2,
        "S",
    ),
    _measured("chg_limit_a", "Charge current limit", "current", "A", 1),
    _measured("dis_limit_a", "Discharge current limit", "current", "A", 1),
    *(_measured(f"temp_{n}_c", f"Temperature {n}", "temperature", "°C", 1) for n in (1, 2, 3)),
    _measured("temp_mos_c", "MOS temperature", "temperature", "°C", 1),
    _measured("temp_board_c", "Board temperature", "temperature", "°C", 1),
    _measured("temp_max_c", "Highest temperature", "temperature", "°C", 1),
    _measured("temp_min_c", "Lowest temperature", "temperature", "°C", 1),
    _binary("chg_mos_on", "Charge MOS", "power"),
    _binary("dis_mos_on", "Discharge MOS", "power"),
    _binary("balancing", "Balancing", "running"),
    _binary("bms_alarm", "BMS alarm", "problem", "B+"),
    _binary("bms_protection", "BMS protection", "problem", "B+"),
    _binary("alert_cell_spread", "Cell spread alert", "problem"),
    _binary("alert_cell_low", "Low cell alert", "problem"),
    Component(
        "cells_fresh", "Cell data fresh", "binary_sensor", None, None, None, _DIAGNOSTIC, None, "B"
    ),
    _plain("flags_raw", "BMS flags raw", _DIAGNOSTIC),
    Component(
        "bcast_age_s",
        "Broadcast age",
        "sensor",
        "duration",
        "s",
        "measurement",
        _DIAGNOSTIC,
        1,
        "S",
        enabled=False,
    ),
    Component(
        "cell_age_s",
        "Cell data age",
        "sensor",
        "duration",
        "s",
        "measurement",
        _DIAGNOSTIC,
        1,
        "S",
        enabled=False,
    ),
)
"""HA-10: the components of every module device, in table order."""

STACK_COMPONENTS: Final[tuple[Component, ...]] = (
    _measured("lowest_mv", "Lowest cell voltage", "voltage", "mV", 0),
    _plain("lowest_module", "Lowest cell module"),
    _plain("lowest_cell", "Lowest cell"),
    _measured("highest_mv", "Highest cell voltage", "voltage", "mV", 0),
    _plain("highest_module", "Highest cell module"),
    _plain("highest_cell", "Highest cell"),
    _measured("spread_mv", "Cell spread", "voltage", "mV", 0),
    _measured("total_current_a", "Current", "current", "A", 1),
    _measured("bms_current_a", "BMS current", "current", "A", 1),
    _measured("avg_soc", "Average module state of charge", "battery", "%", 0),
    _measured("bms_soc", "State of charge", "battery", "%", 0),
    _measured("bms_soh", "State of health", None, "%", 0),
    _measured("voltage_v", "Voltage", "voltage", "V", 2),
    _measured("temp_c", "Temperature", "temperature", "°C", 1),
    _measured("chg_limit_v", "Charge voltage limit", "voltage", "V", 1),
    _measured("chg_limit_a", "Charge current limit", "current", "A", 1),
    _measured("dis_limit_a", "Discharge current limit", "current", "A", 1),
    _measured("dis_limit_v", "Discharge voltage limit", "voltage", "V", 1),
    Component(
        "modules_online", "Modules online", "sensor", None, None, "measurement", None, 0, "S"
    ),
    _measured("imbalance_pct", "Current imbalance", None, "%", 1),
    _binary("alert_current_imbalance", "Current imbalance alert", "problem"),
    _binary("alert_any_cell_spread", "Cell spread alert", "problem"),
    _binary("alert_any_cell_low", "Low cell alert", "problem"),
    _binary("alert_any_bms_alarm", "BMS alarm", "problem"),
    _binary("alert_any_bms_protection", "BMS protection", "problem"),
    _plain("module_count_bms", "BMS module count", _DIAGNOSTIC),
)
"""HA-11: the stack's own components, in table order."""


def _service(
    key: str,
    name: str,
    device_class: str | None = None,
    unit: str | None = None,
    state_class: str | None = None,
    precision: int | None = None,
    *,
    platform: Platform = "sensor",
    template: Template = "S",
    enabled: bool = False,
    options: tuple[str, ...] | None = None,
) -> Component:
    """Return a diagnostic service component (HA-12), disabled by default unless ``enabled``."""
    return Component(
        key,
        name,
        platform,
        device_class,
        unit,
        state_class,
        _DIAGNOSTIC,
        precision,
        template,
        enabled=enabled,
        options=options,
        service=True,
    )


SERVICE_COMPONENTS: Final[tuple[Component, ...]] = (
    _service("health", "Health", "enum", enabled=True, options=HEALTH_OPTIONS),
    _service("health_reason", "Health reason", enabled=True),
    _service("can_state", "CAN state", "enum", enabled=True, options=CAN_STATE_OPTIONS),
    _service("bus_age_s", "Bus age", "duration", "s", "measurement", 1, enabled=True),
    _service(
        "polling_suspended",
        "Polling suspended",
        "problem",
        platform="binary_sensor",
        template="B",
        enabled=True,
    ),
    _service("local_log", "Local log", "enum", enabled=True, options=LOCAL_LOG_OPTIONS),
    _service("can_berr_tx", "CAN TX error counter", None, None, "measurement", 0),
    _service("can_berr_rx", "CAN RX error counter", None, None, "measurement", 0),
    _service("can_restarts", "CAN bus-off restarts", None, None, "total_increasing", 0),
    _service("error_frames", "CAN error frames", None, None, "total_increasing", 0),
    _service("dropped_frames", "Dropped frames", None, None, "total_increasing", 0),
    _service("echo_frames", "Echoed poll frames", None, None, "total_increasing", 0),
    _service("malformed_frames", "Malformed frames", None, None, "total_increasing", 0),
    _service("unknown_frames", "Unknown frames", None, None, "total_increasing", 0),
    _service("polls_sent", "Polls sent", None, None, "total_increasing", 0),
    _service("escalation", "Recovery escalation", None, None, "measurement", 0),
    _service("service_restarts", "Service restarts", None, None, "total_increasing", 0),
    _service("last_exit_code", "Last exit code"),
    _service("last_exit_result", "Last exit result"),
    _service("uptime_s", "Uptime", "duration", "s", None, 0),
    _service("mqtt_reconnects", "MQTT reconnects", None, None, "total_increasing", 0),
    _service("version", "Version"),
)
"""HA-12: the service diagnostics carried by the stack device, in table order."""

REMOVED_COMPONENTS: tuple[RemovedComponent, ...] = ()
"""HA-22: every component dropped from a device, kept until a later release (none yet).

A released key MUST NOT come back as a different quantity (HA-21), so an entry here is
never reused by a live component of the same device kind.
"""


def _value_template(key: str, template: Template) -> str:
    """Return the HA-09 ``value_template`` for template code ``S``, ``B`` or ``B+``."""
    if template == "S":
        return f"{{{{ value_json.{key} }}}}"
    value = f"value_json.{key}"
    return f"{{{{ None if {value} is none else ('ON' if {value} else 'OFF') }}}}"


def _component(
    component: Component,
    *,
    unique_id: str,
    expire_after: int,
    state_topic: str,
    service_topic: str,
) -> dict[str, Any]:
    """Return the discovery options of one component (HA-07, HA-09, HA-12..HA-17).

    ``state_topic`` is the device's state topic, used for the HA-17 JSON attributes;
    ``service_topic`` overrides the inherited state topic of service components.
    """
    options: dict[str, Any] = {
        "platform": component.platform,
        "unique_id": unique_id,
        "name": component.name,
    }
    for option, value in (
        ("device_class", component.device_class),
        ("unit_of_measurement", component.unit),
        ("state_class", component.state_class),
        ("entity_category", component.category),
    ):
        if value is not None:
            options[option] = value
    if not component.enabled:
        options["enabled_by_default"] = False
    if component.precision is not None:
        options["suggested_display_precision"] = component.precision
    if component.options is not None:
        options["options"] = list(component.options)
    if component.platform == "binary_sensor":
        options["payload_on"] = "ON"
        options["payload_off"] = "OFF"
    if component.service:
        options["state_topic"] = service_topic
    options["value_template"] = _value_template(component.key, component.template)
    if component.template == "B+":
        options["json_attributes_topic"] = state_topic
        options["json_attributes_template"] = (
            f"{{{{ {{'active_flags': value_json.{component.key}_flags}} | tojson }}}}"
        )
    options["expire_after"] = expire_after
    return options


def _availability(*topics: str) -> list[dict[str, str]]:
    """Return availability entries for ``topics`` with ``online``/``offline`` payloads (HA-14)."""
    return [
        {"topic": topic, "payload_available": "online", "payload_not_available": "offline"}
        for topic in topics
    ]


def _encode(payload: dict[str, Any]) -> str:
    """Return ``payload`` as compact UTF-8 JSON, keeping key order and non-ASCII characters."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _device_payload(
    settings: Settings,
    version: str,
    topics: Topics,
    *,
    identifier: str,
    device: dict[str, Any],
    kind: Device,
    state_topic: str,
    availability: list[dict[str, str]],
    components: Iterable[Component],
) -> dict[str, Any]:
    """Return one device's discovery payload with its shared root options (HA-03..HA-06).

    ``availability_mode`` is set only when there is more than one availability topic, which
    is the case for module devices.
    """
    rendered = {
        component.key: _component(
            component,
            unique_id=f"{identifier}_{component.key}",
            expire_after=settings.expire_after_s,
            state_topic=state_topic,
            service_topic=topics.service_state,
        )
        for component in components
    }
    for removed in REMOVED_COMPONENTS:
        if removed.device == kind:
            rendered[removed.key] = {"platform": removed.platform}
    payload: dict[str, Any] = {
        "device": device,
        "origin": {"name": ORIGIN_NAME, "sw_version": version, "support_url": SUPPORT_URL},
        "state_topic": state_topic,
        "availability": availability,
    }
    if len(availability) > 1:
        payload["availability_mode"] = "all"
    payload["qos"] = 0
    payload["components"] = rendered
    return payload


def build_discovery(settings: Settings, version: str) -> list[Message]:
    """Return every discovery message, in publish order (HA-02, HA-04, HA-20).

    The stack device comes first, then modules 1..``MODULE_COUNT``, then an empty payload
    (removal) for each surplus module up to the maximum of four. Nothing is returned when
    ``HA_DISCOVERY`` is off (HA-01). ``version`` is the package version for ``origin``.
    """
    if not settings.ha_discovery:
        return []
    topics = Topics(settings)
    stack_id = topics.stack_id
    stack = _device_payload(
        settings,
        version,
        topics,
        identifier=stack_id,
        device={
            "identifiers": [stack_id],
            "name": f"{settings.ha_device_name} Stack",
            "manufacturer": MANUFACTURER,
            "model": f"{MODEL} \N{MULTIPLICATION SIGN}{settings.module_count}",
        },
        kind="stack",
        state_topic=topics.stack_state,
        availability=_availability(topics.status),
        components=(*STACK_COMPONENTS, *SERVICE_COMPONENTS),
    )
    messages = [(topics.discovery_stack, _encode(stack))]
    for module in range(1, settings.module_count + 1):
        module_id = topics.module_id(module)
        payload = _device_payload(
            settings,
            version,
            topics,
            identifier=module_id,
            device={
                "identifiers": [module_id],
                "name": f"{settings.ha_device_name} {settings.module_name(module)}",
                "manufacturer": MANUFACTURER,
                "model": MODEL,
                "via_device": stack_id,
            },
            kind="module",
            state_topic=topics.module_state(module),
            availability=_availability(topics.status, topics.module_availability(module)),
            components=MODULE_COMPONENTS,
        )
        messages.append((topics.discovery_module(module), _encode(payload)))
    messages.extend(
        (topics.discovery_module(module), "")
        for module in range(settings.module_count + 1, MAX_MODULES + 1)
    )
    return messages
