"""State payload builders and JSON encoding (``06-mqtt.md`` section 4).

The key sets and JSON types are read from the MQTT-16/17/18 tables in ``docs/specs/`` at
test time. The golden files pin the complete payloads of a fixed simulator scenario and of
a store that never received anything (TEST-31).
"""

import dataclasses
import json
import math
import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from dyness_b3_bms_mqtt import protocol
from dyness_b3_bms_mqtt.alerts import Alert, AlertState, AlertThresholds, evaluate
from dyness_b3_bms_mqtt.canbus import PythonCanTransport, RxFrame
from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.linkstate import CanState
from dyness_b3_bms_mqtt.payloads import (
    MODULE_KEYS,
    SERVICE_KEYS,
    STACK_KEYS,
    ServiceInfo,
    encode,
    module_payload,
    service_payload,
    stack_payload,
)
from dyness_b3_bms_mqtt.status import HealthState, ModuleStatus
from dyness_b3_bms_mqtt.telemetry import Snapshot, TelemetryStore
from tests.fakes.clock import FakeClock
from tests.plugins.golden import Golden
from tests.sim.stack import SimulatedStack
from tests.support.settings import make_settings
from tests.support.spec import payload_keys, payload_rows

TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
SCENARIO_ENV = {"MODULE_NAMES": "1:Upper,2:Middle"}
MODULE_3_FLAGS = bytes([0, 0x02, 0x83, 0x20, 0, 0, 0, 0])
"""``CellV_AlarmL`` and ``CellDiff_Alarm`` on, both MOSFETs on, balancing."""

SERVICE_INFO = ServiceInfo(
    version="0.1.0",
    health=HealthState.DEGRADED,
    health_reason="module 3 cells stale",
    can_state=CanState.ERROR_ACTIVE,
    can_berr_tx=0,
    can_berr_rx=3,
    can_restarts=0,
    bus_age_s=0.04,
    error_frames=2,
    dropped_frames=None,
    echo_frames=120,
    malformed_frames=1,
    unknown_frames=0,
    polls_sent=120,
    polling_suspended=False,
    escalation=1,
    service_restarts=2,
    last_exit_code="ABRT",
    last_exit_result="signal",
    uptime_s=3725.9,
    mqtt_reconnects=1,
    local_log="disabled",
)
UNKNOWN_SERVICE = ServiceInfo(
    version="0.1.0",
    health=HealthState.STARTING,
    health_reason=None,
    can_state=CanState.UNKNOWN,
    can_berr_tx=None,
    can_berr_rx=None,
    can_restarts=None,
    bus_age_s=None,
    error_frames=0,
    dropped_frames=None,
    echo_frames=0,
    malformed_frames=0,
    unknown_frames=0,
    polls_sent=0,
    polling_suspended=False,
    escalation=0,
    service_restarts=0,
    last_exit_code=None,
    last_exit_result=None,
    uptime_s=0.0,
    mqtt_reconnects=0,
    local_log="disabled",
)

type Payload = dict[str, Any]


def all_ok(settings: Settings) -> dict[int, ModuleStatus]:
    return dict.fromkeys(range(1, settings.module_count + 1), ModuleStatus.OK)


def drain(transport: PythonCanTransport, store: TelemetryStore) -> None:
    """Decode and ingest every frame waiting on the transport, at its receive time."""
    while (frame := transport.recv(0)) is not None:
        assert isinstance(frame, RxFrame)
        decoded = protocol.decode(frame.arbitration_id, frame.extended, frame.data)
        if decoded is not None:
            store.ingest(decoded, frame.at)


def sim_scenario(can_channel: str, clock: FakeClock) -> tuple[Settings, Snapshot, AlertState]:
    """Run the fixed scenario: three modules polled once each, module 3 alarming.

    The simulator runs for 3 s on the fake clock in 0.1 s steps, with frames drained and
    ingested after every step so their ages differ deterministically.
    """
    settings = make_settings(CAN_CHANNEL=can_channel, **SCENARIO_ENV)
    store = TelemetryStore(settings)
    with (
        SimulatedStack(can_channel, clock) as sim,
        PythonCanTransport(settings, clock, clock.sleep) as transport,
    ):
        sim.set_flags(3, MODULE_3_FLAGS)
        transport.open()
        for module in range(1, settings.module_count + 1):
            transport.send_poll(module)
        for _ in range(30):
            sim.advance(0.1)
            drain(transport, store)
    snapshot = store.snapshot(clock(), all_ok(settings))
    alerts = evaluate(
        snapshot,
        AlertState.initial(settings.module_count),
        clock(),
        AlertThresholds.from_settings(settings),
    )
    return settings, snapshot, alerts


def build_all(
    settings: Settings, snapshot: Snapshot, alerts: AlertState, ts: float
) -> Iterator[tuple[str, Payload]]:
    """Yield ``(name, payload)`` for every module, the stack and the service."""
    for module in snapshot.modules:
        yield (
            f"module_{module.module}",
            module_payload(
                module, alerts.module(module.module), settings.module_name(module.module), ts
            ),
        )
    yield "stack", stack_payload(snapshot.stack, alerts.stack, ts)


def type_matches(json_type: str, value: object) -> bool:
    """Return whether ``value`` has the JSON type named by a schema table's type cell."""
    if json_type.startswith("integer, or string"):
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, str)
    if json_type.startswith("integer"):
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type.startswith("number"):
        return isinstance(value, int | float) and not isinstance(value, bool)
    if json_type.startswith("boolean"):
        return isinstance(value, bool)
    if json_type.startswith("array of strings"):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if json_type.startswith("string"):
        return isinstance(value, str)
    raise AssertionError(f"unknown JSON type cell {json_type!r}")


def check_against_table(ident: str, payload: Mapping[str, object]) -> None:
    """Check ``payload`` against the spec table: types, and no null where the spec says never."""
    rows = payload_rows(ident)
    assert set(payload) == {key for key, _ in rows}
    for key, cells in rows:
        json_type, null_when = cells[1], cells[3]
        value = payload[key]
        if value is None:
            assert null_when != "never" and not null_when.startswith("never ("), key
        else:
            assert type_matches(json_type, value), (key, json_type, value)


@pytest.mark.req("MQTT-15", "MQTT-16")
def test_module_keys_exact(can_channel: str, clock: FakeClock, wall: FakeClock) -> None:
    assert set(MODULE_KEYS) == payload_keys("MQTT-16")
    assert len(MODULE_KEYS) == len(set(MODULE_KEYS))
    _, snapshot, alerts = sim_scenario(can_channel, clock)
    empty_settings = make_settings()
    empty = TelemetryStore(empty_settings).snapshot(clock(), {})
    for module, module_alerts in (
        (snapshot.modules[0], alerts.module(1)),
        (snapshot.modules[2], alerts.module(3)),
        (empty.modules[0], AlertState.initial(3).module(1)),
    ):
        payload = module_payload(module, module_alerts, "Module 1", wall())
        assert tuple(payload) == MODULE_KEYS
        check_against_table("MQTT-16", payload)


@pytest.mark.req("MQTT-15", "MQTT-17")
def test_stack_keys_exact(can_channel: str, clock: FakeClock, wall: FakeClock) -> None:
    assert set(STACK_KEYS) == payload_keys("MQTT-17")
    assert len(STACK_KEYS) == len(set(STACK_KEYS))
    _, snapshot, alerts = sim_scenario(can_channel, clock)
    empty = TelemetryStore(make_settings()).snapshot(clock(), {})
    for stack, stack_alerts in (
        (snapshot.stack, alerts.stack),
        (empty.stack, AlertState.initial(3).stack),
    ):
        payload = stack_payload(stack, stack_alerts, wall())
        assert tuple(payload) == STACK_KEYS
        check_against_table("MQTT-17", payload)


@pytest.mark.req("MQTT-15", "MQTT-18")
def test_service_keys_exact(wall: FakeClock) -> None:
    assert set(SERVICE_KEYS) == payload_keys("MQTT-18")
    assert "local_log" in SERVICE_KEYS
    assert len(SERVICE_KEYS) == len(set(SERVICE_KEYS))
    for info in (SERVICE_INFO, UNKNOWN_SERVICE):
        payload = service_payload(info, wall())
        assert tuple(payload) == SERVICE_KEYS
        check_against_table("MQTT-18", payload)
    known = service_payload(SERVICE_INFO, wall())
    assert known["health"] == "degraded"
    assert known["can_state"] == "error-active"
    assert known["bus_age_s"] == 0.0
    assert known["uptime_s"] == 3725
    assert known["last_exit_code"] == "ABRT"
    changed = service_payload(
        dataclasses.replace(SERVICE_INFO, bus_age_s=1.26, last_exit_code=75), wall()
    )
    assert changed["bus_age_s"] == 1.3
    assert changed["last_exit_code"] == 75
    unknown = service_payload(UNKNOWN_SERVICE, wall())
    assert unknown["can_state"] is None
    assert unknown["health"] == "starting"
    assert unknown["health_reason"] is None


@pytest.mark.req("MQTT-14", "MQTT-16", "MQTT-17", "MQTT-22")
def test_encoding_rules(can_channel: str, clock: FakeClock, wall: FakeClock) -> None:
    text = encode({"b": None, "a": [True, False], "ü": "Modül 1", "c": 0.0, "n": 1})
    assert text == '{"a":[true,false],"b":null,"c":0.0,"n":1,"ü":"Modül 1"}'
    assert isinstance(text, str)
    assert text.encode().decode("utf-8") == text
    with pytest.raises(ValueError, match="JSON compliant"):
        encode({"x": math.nan})
    with pytest.raises(ValueError, match="JSON compliant"):
        encode({"x": math.inf})

    settings, snapshot, alerts = sim_scenario(can_channel, clock)
    for name, payload in build_all(settings, snapshot, alerts, wall()):
        encoded = encode(payload)
        assert json.loads(encoded) == payload, name
        assert list(json.loads(encoded)) == sorted(payload), name
        assert ", " not in encoded and ": " not in encoded
        assert len(encoded.encode()) < 4096, name
    module_3 = module_payload(snapshot.modules[2], alerts.module(3), "Module 3", wall())
    assert module_3["bms_alarm"] is True
    assert module_3["bms_alarm_flags"] == ["CellV_AlarmL", "CellDiff_Alarm"]
    assert module_3["bms_protection"] is False
    assert module_3["bms_protection_flags"] == []
    assert module_3["balancing"] is True

    unknown = module_payload(
        TelemetryStore(make_settings()).snapshot(clock(), {}).modules[0],
        AlertState.initial(3).module(1),
        "Modül 1",
        wall(),
    )
    assert unknown["bms_alarm"] is None
    assert unknown["bms_alarm_flags"] is None
    assert all(unknown[f"cell_{n:02d}_mv"] is None for n in range(1, 16))
    assert '"name":"Modül 1"' in encode(unknown)


@pytest.mark.req("MQTT-17")
def test_imbalance_pct_one_decimal(wall: FakeClock, clock: FakeClock) -> None:
    stack = TelemetryStore(make_settings()).snapshot(clock(), {}).stack
    alerts = dict(AlertState.initial(3).stack)
    alerts["current_imbalance"] = Alert(True, value=12.345678)
    payload = stack_payload(stack, alerts, wall())
    assert payload["imbalance_pct"] == 12.3
    assert payload["alert_current_imbalance"] is True
    alerts["current_imbalance"] = Alert(False, value=-0.04)
    assert stack_payload(stack, alerts, wall())["imbalance_pct"] == 0.0
    assert str(stack_payload(stack, alerts, wall())["imbalance_pct"]) == "0.0"
    alerts["current_imbalance"] = Alert(False, value=None)
    assert stack_payload(stack, alerts, wall())["imbalance_pct"] is None


@pytest.mark.req("MQTT-15")
def test_ts_format(can_channel: str, clock: FakeClock) -> None:
    wall = FakeClock(1791530130.999)
    settings, snapshot, alerts = sim_scenario(can_channel, clock)
    payloads = [
        *(payload for _, payload in build_all(settings, snapshot, alerts, wall())),
        service_payload(SERVICE_INFO, wall()),
    ]
    for payload in payloads:
        assert payload["ts"] == "2026-10-09T07:15:30Z"
        assert TS.fullmatch(payload["ts"])
    wall.advance(0.001)
    assert service_payload(SERVICE_INFO, wall())["ts"] == "2026-10-09T07:15:31Z"


@pytest.mark.req("MQTT-14", "MQTT-15", "MQTT-16", "MQTT-17", "MQTT-18", "TEST-31")
def test_state_golden(can_channel: str, clock: FakeClock, wall: FakeClock, golden: Golden) -> None:
    settings, snapshot, alerts = sim_scenario(can_channel, clock)
    for name, payload in build_all(settings, snapshot, alerts, wall()):
        golden(f"state/sim/{name}.json", json.loads(encode(payload)))
    golden("state/sim/service.json", json.loads(encode(service_payload(SERVICE_INFO, wall()))))

    empty_settings = make_settings()
    empty = TelemetryStore(empty_settings).snapshot(clock(), {})
    initial = AlertState.initial(empty_settings.module_count)
    golden(
        "state/empty/module_1.json",
        json.loads(encode(module_payload(empty.modules[0], initial.module(1), "Module 1", wall()))),
    )
    golden(
        "state/empty/stack.json",
        json.loads(encode(stack_payload(empty.stack, initial.stack, wall()))),
    )
    golden("state/empty/service.json", json.loads(encode(service_payload(UNKNOWN_SERVICE, wall()))))
