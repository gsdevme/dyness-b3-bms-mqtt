"""Home Assistant device-based discovery (``07-home-assistant.md``).

The component tables, the module 2 example, the enum value sets and the list of payload keys
that are not entities are read from ``docs/specs/`` at test time, so the builder is checked
against the spec itself rather than against a copy of it. The golden files pin the complete
output on top of that (TEST-31).
"""

import json
import re
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dyness_b3_bms_mqtt import discovery
from dyness_b3_bms_mqtt.discovery import (
    CAN_STATE_OPTIONS,
    HEALTH_OPTIONS,
    MODULE_COMPONENTS,
    REMOVED_COMPONENTS,
    SERVICE_COMPONENTS,
    STACK_COMPONENTS,
    SUPPORT_URL,
    Component,
    RemovedComponent,
    build_discovery,
)
from dyness_b3_bms_mqtt.linkstate import CanState
from dyness_b3_bms_mqtt.protocol import FLAG_NAMES
from dyness_b3_bms_mqtt.status import HealthState
from tests.plugins.golden import Golden
from tests.support.settings import make_settings

ROOT = Path(__file__).resolve().parents[2]
SPECS = ROOT / "docs" / "specs"
GOLDEN_DISCOVERY = ROOT / "tests" / "golden" / "discovery"
VERSION = "0.1.0"

NAMED_ENV = {
    "MODULE_COUNT": "2",
    "MODULE_NAMES": "1:Upper,2:Lower",
    "MQTT_BASE_TOPIC": "garage/battery",
    "HA_DISCOVERY_PREFIX": "ha",
    "HA_NODE_ID": "garage_bank",
    "HA_DEVICE_NAME": "Garage battery",
    "EXPIRE_AFTER_S": "90",
}
SCENARIOS: Mapping[str, Mapping[str, str]] = {"default": {}, "named": NAMED_ENV}

ROOT_KEYS = {
    "device",
    "origin",
    "state_topic",
    "availability",
    "availability_mode",
    "qos",
    "components",
}
DEVICE_KEYS = {"identifiers", "name", "manufacturer", "model", "via_device"}
COMPONENT_KEYS = {
    "platform",
    "unique_id",
    "name",
    "device_class",
    "unit_of_measurement",
    "state_class",
    "entity_category",
    "enabled_by_default",
    "suggested_display_precision",
    "options",
    "payload_on",
    "payload_off",
    "state_topic",
    "value_template",
    "json_attributes_topic",
    "json_attributes_template",
    "expire_after",
}
ACRONYMS = {"BMS", "CAN", "MOS", "MQTT", "TX", "RX"}
UNITS = {"mV", "V", "A", "%", "°C", "kWh", "s"}

type Payload = dict[str, Any]


def b_template(key: str) -> str:
    return f"{{{{ None if value_json.{key} is none else ('ON' if value_json.{key} else 'OFF') }}}}"


def flags_template(key: str) -> str:
    return f"{{{{ {{'active_flags': value_json.{key}_flags}} | tojson }}}}"


def build(**env: str) -> list[tuple[str, str]]:
    return build_discovery(make_settings(**env), VERSION)


def devices(**env: str) -> list[tuple[str, Payload]]:
    """Return the non-empty discovery messages of ``env`` with their payloads parsed."""
    return [(topic, json.loads(payload)) for topic, payload in build(**env) if payload]


def stack_and_modules(**env: str) -> tuple[Payload, list[Payload]]:
    (_, stack), *modules = devices(**env)
    return stack, [payload for _, payload in modules]


def all_components(**env: str) -> Iterator[tuple[Payload, str, Payload]]:
    for _, payload in devices(**env):
        for key, options in payload["components"].items():
            yield payload, key, options


def render(template: str, state: Mapping[str, object]) -> str:
    """Render one of the builder's Jinja templates the way Home Assistant would.

    Only the constructs the builder emits are understood: ``value_json.<key>``,
    ``is none``, conditional expressions, dict literals and a trailing ``| tojson``.
    """
    match = re.fullmatch(r"\{\{ (.+) \}\}", template)
    assert match, template
    expression = match.group(1)
    tojson = expression.endswith(" | tojson")
    expression = expression.removesuffix(" | tojson").replace(" is none", " is None")
    value = eval(expression, {"__builtins__": {}}, {"value_json": SimpleNamespace(**state)})
    return json.dumps(value) if tojson else str(value)


@cache
def spec_text(name: str) -> str:
    return (SPECS / name).read_text(encoding="utf-8")


def requirement(name: str, ident: str) -> list[str]:
    """Return the lines of requirement ``ident`` in spec ``name``, up to the next one."""
    lines = spec_text(name).splitlines()
    start = next(i for i, line in enumerate(lines) if f"**{ident}**" in line)
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith(("* **", "#", "Example:", "Template codes:"))
        ),
        len(lines),
    )
    return lines[start:end]


def section(name: str, heading: str) -> list[str]:
    lines = spec_text(name).splitlines()
    start = lines.index(heading)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("#"))
    return lines[start:end]


def table_rows(lines: list[str]) -> list[list[str]]:
    """Return the cells of each table body row whose first cell starts with a code span."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in lines
        if re.match(r"\s*\| `", line)
    ]


def expand(key_cell: str, name_cell: str = "") -> list[tuple[str, str]]:
    """Expand a key cell (and its name) into ``(key, name)`` pairs.

    Handles a single key, a comma list and a ``first … last`` numbered range.
    """
    keys = re.findall(r"`([^`]+)`", key_cell)
    if "…" not in key_cell:
        return [(key, name_cell) for key in keys]
    first, last = keys
    lo_match = re.search(r"[0-9]+", first)
    hi_match = re.search(r"[0-9]+", last)
    assert lo_match and hi_match
    width = len(lo_match.group())
    name_first = name_cell.split(" … ")[0]
    name_match = re.search(r"[0-9]+", name_first)
    pairs = []
    for n in range(int(lo_match.group()), int(hi_match.group()) + 1):
        key = f"{first[: lo_match.start()]}{n:0{width}d}{first[lo_match.end() :]}"
        name = (
            f"{name_first[: name_match.start()]}{n}{name_first[name_match.end() :]}"
            if name_match
            else name_cell
        )
        pairs.append((key, name))
    return pairs


def dash(cell: str) -> str | None:
    return None if cell == "—" else cell


type Row = tuple[
    str, str, str, str | None, str | None, str | None, str | None, int | None, str, bool
]


def spec_components(ident: str) -> list[Row]:
    """Return the rows of component table ``ident`` (HA-10, HA-11 or HA-12), expanded."""
    rows: list[Row] = []
    for cells in table_rows(requirement("07-home-assistant.md", ident)):
        if ident == "HA-12":
            key, name, platform, dc, unit, sc, precision, template, enabled = cells
            category = "diagnostic"
        else:
            key, name, platform, dc, unit, sc, category, precision, template, enabled = cells
        rows.extend(
            (
                k,
                n,
                platform,
                dash(dc),
                dash(unit),
                dash(sc),
                dash(category),
                None if precision == "—" else int(precision),
                template,
                enabled == "yes",
            )
            for k, n in expand(key, name)
        )
    return rows


def as_row(component: Component) -> Row:
    return (
        component.key,
        component.name,
        component.platform,
        component.device_class,
        component.unit,
        component.state_class,
        component.category,
        component.precision,
        component.template,
        component.enabled,
    )


def spec_enum_options() -> dict[str, list[str]]:
    """Return HA-13's ``key -> options`` value sets."""
    text = " ".join(" ".join(requirement("07-home-assistant.md", "HA-13")).split())
    sets = text.split("MQTT-18:", 1)[1].strip().removesuffix(".")
    options = {}
    for part in sets.split(";"):
        left, right = part.split("→")
        (key,) = re.findall(r"`([^`]+)`", left)
        options[key] = re.findall(r"`([^`]+)`", right)
    return options


def expected_options(
    row: Row,
    *,
    unique_id: str,
    expire_after: int,
    attributes_topic: str,
    service_topic: str | None,
) -> Payload:
    """Return the options HA-09, HA-12, HA-13, HA-16 and HA-17 give one table row."""
    key, name, platform, dc, unit, sc, category, precision, template, enabled = row
    options: Payload = {"platform": platform, "unique_id": unique_id, "name": name}
    for option, value in (
        ("device_class", dc),
        ("unit_of_measurement", unit),
        ("state_class", sc),
        ("entity_category", category),
        ("suggested_display_precision", precision),
    ):
        if value is not None:
            options[option] = value
    if not enabled:
        options["enabled_by_default"] = False
    if dc == "enum":
        options["options"] = spec_enum_options()[key]
    if platform == "binary_sensor":
        options["payload_on"] = "ON"
        options["payload_off"] = "OFF"
    options["value_template"] = (
        f"{{{{ value_json.{key} }}}}" if template == "S" else b_template(key)
    )
    if template == "B+":
        options["json_attributes_topic"] = attributes_topic
        options["json_attributes_template"] = flags_template(key)
    if service_topic is not None:
        options["state_topic"] = service_topic
    options["expire_after"] = expire_after
    return options


def payload_keys(ident: str) -> set[str]:
    return {
        key for cells in table_rows(requirement("06-mqtt.md", ident)) for key, _ in expand(cells[0])
    }


def not_exposed() -> dict[str, set[str]]:
    """Return section 5.1's ``payload kind -> keys`` that are not entities."""
    excluded: dict[str, set[str]] = {}
    for line in section("07-home-assistant.md", "### 5.1 Not exposed as entities"):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[1].startswith("`"):
            excluded.setdefault(cells[0], set()).update(re.findall(r"`([^`]+)`", cells[1]))
    return excluded


@pytest.mark.req("HA-02", "TEST-31")
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_discovery_matches_golden(scenario: str, golden: Golden) -> None:
    settings = make_settings(**SCENARIOS[scenario])
    messages = build_discovery(settings, VERSION)
    assert messages == build_discovery(settings, VERSION)
    names = set()
    for topic, payload in messages:
        name = f"{topic.split('/')[-2]}.json"
        names.add(name)
        golden(
            f"discovery/{scenario}/{name}",
            {"topic": topic, "payload": json.loads(payload) if payload else ""},
        )
    assert {path.name for path in (GOLDEN_DISCOVERY / scenario).glob("*.json")} == names


@pytest.mark.req("HA-06", "HA-09", "HA-14", "HA-16")
def test_example_components_match_spec() -> None:
    text = spec_text("07-home-assistant.md")
    match = re.search(r"Example: module 2.*?```json\n(.*?)```", text, re.DOTALL)
    assert match
    example = json.loads(match.group(1))
    _, modules = stack_and_modules()
    actual = modules[1]
    assert {k: v for k, v in actual.items() if k != "components"} == {
        k: v for k, v in example.items() if k != "components"
    }
    assert len(example["components"]) == 4
    for key, options in example["components"].items():
        assert actual["components"][key] == options, key


@pytest.mark.req("HA-09", "HA-10", "HA-11", "HA-12", "HA-13", "HA-17")
def test_components_match_spec_tables() -> None:
    assert [as_row(c) for c in MODULE_COMPONENTS] == spec_components("HA-10")
    assert [as_row(c) for c in STACK_COMPONENTS] == spec_components("HA-11")
    assert [as_row(c) for c in SERVICE_COMPONENTS] == spec_components("HA-12")
    assert not any(c.service for c in (*MODULE_COMPONENTS, *STACK_COMPONENTS))
    assert all(c.service for c in SERVICE_COMPONENTS)
    assert sum(c.enabled for c in SERVICE_COMPONENTS) == 6

    for env in SCENARIOS.values():
        settings = make_settings(**env)
        node, base = settings.ha_node_id, settings.mqtt_base_topic
        stack, modules = stack_and_modules(**env)
        for m, module in enumerate(modules, start=1):
            rows = spec_components("HA-10")
            assert list(module["components"]) == [row[0] for row in rows]
            for row in rows:
                assert module["components"][row[0]] == expected_options(
                    row,
                    unique_id=f"{node}_m{m}_{row[0]}",
                    expire_after=settings.expire_after_s,
                    attributes_topic=f"{base}/module/{m}/state",
                    service_topic=None,
                ), (m, row[0])
        stack_rows = [(row, None) for row in spec_components("HA-11")] + [
            (row, f"{base}/service/state") for row in spec_components("HA-12")
        ]
        assert list(stack["components"]) == [row[0] for row, _ in stack_rows]
        for row, service_topic in stack_rows:
            assert stack["components"][row[0]] == expected_options(
                row,
                unique_id=f"{node}_stack_{row[0]}",
                expire_after=settings.expire_after_s,
                attributes_topic="",
                service_topic=service_topic,
            ), row[0]


@pytest.mark.req("HA-03", "HA-04")
def test_devices_and_order() -> None:
    messages = build()
    assert [topic for topic, _ in messages] == [
        "homeassistant/device/dyness_b3_stack/config",
        "homeassistant/device/dyness_b3_m1/config",
        "homeassistant/device/dyness_b3_m2/config",
        "homeassistant/device/dyness_b3_m3/config",
        "homeassistant/device/dyness_b3_m4/config",
    ]
    stack, modules = stack_and_modules()
    assert stack["device"] == {
        "identifiers": ["dyness_b3_stack"],
        "name": "Dyness B3 Stack",
        "manufacturer": "Dyness",
        "model": "B3 \N{MULTIPLICATION SIGN}3",
    }
    assert [module["device"] for module in modules] == [
        {
            "identifiers": [f"dyness_b3_m{m}"],
            "name": f"Dyness B3 Module {m}",
            "manufacturer": "Dyness",
            "model": "B3",
            "via_device": "dyness_b3_stack",
        }
        for m in (1, 2, 3)
    ]

    named = devices(**NAMED_ENV)
    assert [topic for topic, _ in named] == [
        "ha/device/garage_bank_stack/config",
        "ha/device/garage_bank_m1/config",
        "ha/device/garage_bank_m2/config",
    ]
    assert [payload["device"]["name"] for _, payload in named] == [
        "Garage battery Stack",
        "Garage battery Upper",
        "Garage battery Lower",
    ]
    assert named[0][1]["device"]["model"] == "B3 \N{MULTIPLICATION SIGN}2"
    assert {payload["device"].get("via_device") for _, payload in named[1:]} == {
        "garage_bank_stack"
    }

    _, partly = stack_and_modules(MODULE_COUNT="4", MODULE_NAMES="2:Second")
    assert [module["device"]["name"] for module in partly] == [
        "Dyness B3 Module 1",
        "Dyness B3 Second",
        "Dyness B3 Module 3",
        "Dyness B3 Module 4",
    ]
    stack, modules = stack_and_modules(MODULE_COUNT="4")
    assert set(stack) == ROOT_KEYS - {"availability_mode"}
    assert all(set(module) == ROOT_KEYS for module in modules)
    for payload in (stack, *modules):
        assert set(payload["device"]) <= DEVICE_KEYS
        assert not {"sw_version", "serial_number"} & set(payload["device"])


@pytest.mark.req("HA-05", "HA-06", "HA-14")
def test_origin_and_shared_options() -> None:
    assert SUPPORT_URL == "https://github.com/gsdevme/dyness-b3-bms-mqtt"
    for env in SCENARIOS.values():
        base = make_settings(**env).mqtt_base_topic
        status = {
            "topic": f"{base}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        stack, modules = stack_and_modules(**env)
        for payload in (stack, *modules):
            assert payload["origin"] == {
                "name": "dyness-b3-bms-mqtt",
                "sw_version": VERSION,
                "support_url": SUPPORT_URL,
            }
            assert payload["qos"] == 0
        assert stack["state_topic"] == f"{base}/stack/state"
        assert stack["availability"] == [status]
        assert "availability_mode" not in stack
        for m, module in enumerate(modules, start=1):
            assert module["state_topic"] == f"{base}/module/{m}/state"
            assert module["availability"] == [
                status,
                {**status, "topic": f"{base}/module/{m}/availability"},
            ]
            assert module["availability_mode"] == "all"
    for _, _, options in all_components(**NAMED_ENV):
        assert not {"availability", "availability_mode", "qos"} & set(options)
    other = json.loads(build_discovery(make_settings(), "9.9.9")[0][1])
    assert other["origin"]["sw_version"] == "9.9.9"


@pytest.mark.req("HA-07", "HA-21")
def test_unique_ids_and_no_object_id() -> None:
    seen: set[str] = set()
    for payload, key, options in all_components(MODULE_COUNT="4"):
        (identifier,) = payload["device"]["identifiers"]
        assert options["unique_id"] == f"{identifier}_{key}"
        assert options["unique_id"] not in seen
        seen.add(options["unique_id"])
        assert set(re.findall(r"value_json\.(\w+)", options["value_template"])) == {key}
        assert set(options) <= COMPONENT_KEYS
        assert not {"object_id", "default_entity_id"} & set(options)
    assert len(seen) == 4 * len(MODULE_COMPONENTS) + len(STACK_COMPONENTS) + len(SERVICE_COMPONENTS)

    def ids(**env: str) -> list[str]:
        return [options["unique_id"] for _, _, options in all_components(**env)]

    assert ids(HA_DEVICE_NAME="Elsewhere", MODULE_NAMES="1:A,2:B,3:C") == ids()
    assert ids(HA_NODE_ID="other") == [uid.replace("dyness_b3_", "other_", 1) for uid in ids()]
    assert {key for _, key, _ in all_components()} == {
        c.key for c in (*MODULE_COMPONENTS, *STACK_COMPONENTS, *SERVICE_COMPONENTS)
    }


@pytest.mark.req("HA-08")
def test_names_sentence_case_unpadded() -> None:
    for payload, _, options in all_components(**NAMED_ENV):
        name = options["name"]
        words = name.split()
        assert words[0] in ACRONYMS or words[0] == words[0].capitalize(), name
        assert all(word in ACRONYMS or word == word.lower() for word in words[1:]), name
        assert not re.search(r"\b0[0-9]", name), name
        assert not UNITS & set(words), name
        assert payload["device"]["name"] not in name
        assert "Garage" not in name and "Dyness" not in name
    for payload in (p for _, p in devices(MODULE_COUNT="4")):
        names = [options["name"] for options in payload["components"].values()]
        assert len(names) == len(set(names))

    module = {c.key: c.name for c in MODULE_COMPONENTS}
    stack = {c.key: c.name for c in STACK_COMPONENTS}
    assert module["cell_08_mv"] == "Cell 8 voltage"
    assert module["temp_1_c"] == "Temperature 1"
    for module_key, stack_key in (
        ("cell_min_mv", "lowest_mv"),
        ("cell_max_mv", "highest_mv"),
        ("cell_min_no", "lowest_cell"),
        ("cell_max_no", "highest_cell"),
        ("spread_mv", "spread_mv"),
        ("current_a", "total_current_a"),
        ("soc", "bms_soc"),
        ("soh", "bms_soh"),
        ("chg_limit_a", "chg_limit_a"),
        ("dis_limit_a", "dis_limit_a"),
        ("bms_alarm", "alert_any_bms_alarm"),
        ("bms_protection", "alert_any_bms_protection"),
        ("alert_cell_spread", "alert_any_cell_spread"),
        ("alert_cell_low", "alert_any_cell_low"),
    ):
        assert module[module_key] == stack[stack_key], (module_key, stack_key)


@pytest.mark.req("HA-09", "HA-15")
def test_binary_templates_render_on_off_none() -> None:
    binaries = 0
    for _, key, options in all_components():
        template = options["value_template"]
        if options["platform"] == "binary_sensor":
            binaries += 1
            assert template == b_template(key)
            assert (options["payload_on"], options["payload_off"]) == ("ON", "OFF")
            assert render(template, {key: True}) == "ON"
            assert render(template, {key: False}) == "OFF"
            assert render(template, {key: None}) == "None"
        else:
            assert options["platform"] == "sensor"
            assert template == f"{{{{ value_json.{key} }}}}"
            assert render(template, {key: None}) == "None"
            assert render(template, {key: 3}) == "3"
            assert not {"payload_on", "payload_off"} & set(options)
    assert binaries == 3 * 8 + 5 + 1


@pytest.mark.req("HA-16")
@pytest.mark.parametrize(
    ("env", "expected"),
    [({}, 35), ({"PUBLISH_INTERVAL_S": "20"}, 65), ({"EXPIRE_AFTER_S": "90"}, 90)],
)
def test_expire_after_everywhere(env: dict[str, str], expected: int) -> None:
    assert make_settings(**env).expire_after_s == expected
    components = list(all_components(**env))
    assert len(components) == 3 * len(MODULE_COMPONENTS) + len(STACK_COMPONENTS) + len(
        SERVICE_COMPONENTS
    )
    for _, key, options in components:
        assert options["expire_after"] == expected, key


@pytest.mark.req("HA-13")
def test_enum_options() -> None:
    spec = spec_enum_options()
    assert list(HEALTH_OPTIONS) == spec["health"] == [state.value for state in HealthState]
    assert list(CAN_STATE_OPTIONS) == spec["can_state"]
    assert set(CAN_STATE_OPTIONS) == {s.published for s in CanState if s.published is not None}
    assert spec["local_log"] == ["ok", "disabled", "error"]
    enums = {key: options for _, key, options in all_components() if "options" in options}
    assert set(enums) == {"health", "can_state", "local_log"}
    for key, options in enums.items():
        assert options["options"] == spec[key]
        assert options["device_class"] == "enum"
        assert not {"state_class", "unit_of_measurement"} & set(options)
    assert {c.key: c.options for c in SERVICE_COMPONENTS if c.options} == {
        key: tuple(values) for key, values in spec.items()
    }


@pytest.mark.req("HA-17")
def test_bms_flag_attributes() -> None:
    attributed = set()
    for payload, key, options in all_components(**NAMED_ENV):
        if "json_attributes_topic" not in options:
            assert "json_attributes_template" not in options
            continue
        attributed.add((payload["device"]["identifiers"][0], key))
        assert options["json_attributes_topic"] == payload["state_topic"]
        template = options["json_attributes_template"]
        assert template == flags_template(key)
        flags = list(FLAG_NAMES[:2])
        assert json.loads(render(template, {f"{key}_flags": flags})) == {"active_flags": flags}
        assert json.loads(render(template, {f"{key}_flags": []})) == {"active_flags": []}
        assert json.loads(render(template, {f"{key}_flags": None})) == {"active_flags": None}
    assert attributed == {
        (f"garage_bank_m{m}", key) for m in (1, 2) for key in ("bms_alarm", "bms_protection")
    }
    _, modules = stack_and_modules(**NAMED_ENV)
    assert modules[0]["state_topic"] == "garage/battery/module/1/state"


@pytest.mark.req("HA-07", "HA-10", "HA-11", "HA-12")
def test_every_payload_key_exposed_except_7_5_1() -> None:
    excluded = not_exposed()
    assert set(excluded) == {"Module", "Stack", "Service"}
    module_keys = payload_keys("MQTT-16")
    stack_keys = payload_keys("MQTT-17")
    service_keys = payload_keys("MQTT-18")
    assert len(module_keys) == 53
    assert "local_log" in service_keys
    assert excluded["Module"] <= module_keys
    assert excluded["Stack"] <= stack_keys
    assert excluded["Service"] <= service_keys
    assert {c.key for c in MODULE_COMPONENTS} == module_keys - excluded["Module"]
    assert {c.key for c in STACK_COMPONENTS} == stack_keys - excluded["Stack"]
    assert {c.key for c in SERVICE_COMPONENTS} == service_keys - excluded["Service"]
    assert len(MODULE_COMPONENTS) == len({c.key for c in MODULE_COMPONENTS})
    stack_and_service = [c.key for c in (*STACK_COMPONENTS, *SERVICE_COMPONENTS)]
    assert len(stack_and_service) == len(set(stack_and_service))


@pytest.mark.req("HA-20", "HA-04")
@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_removals_for_surplus_modules(count: int) -> None:
    messages = build(MODULE_COUNT=str(count))
    live, removals = messages[: count + 1], messages[count + 1 :]
    assert all(payload for _, payload in live)
    assert removals == [
        (f"homeassistant/device/dyness_b3_m{m}/config", "") for m in range(count + 1, 5)
    ]
    named = build(**{**NAMED_ENV, "MODULE_COUNT": str(count), "MODULE_NAMES": "1:Upper"})
    assert named[count + 1 :] == [
        (f"ha/device/garage_bank_m{m}/config", "") for m in range(count + 1, 5)
    ]


@pytest.mark.req("HA-01", "HA-02")
def test_disabled_publishes_nothing() -> None:
    assert build(HA_DISCOVERY="false") == []
    assert build(HA_DISCOVERY="false", MODULE_COUNT="1") == []


@pytest.mark.req("HA-22", "HA-21")
def test_removed_components_list_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    live_module = {c.key for c in MODULE_COMPONENTS}
    live_stack = {c.key for c in (*STACK_COMPONENTS, *SERVICE_COMPONENTS)}
    for removed in REMOVED_COMPONENTS:
        assert removed.key not in (live_module if removed.device == "module" else live_stack)
    assert REMOVED_COMPONENTS == ()

    before = stack_and_modules()
    monkeypatch.setattr(
        discovery,
        "REMOVED_COMPONENTS",
        (
            RemovedComponent("module", "cell_16_mv", "sensor"),
            RemovedComponent("stack", "old_flag", "binary_sensor"),
        ),
    )
    stack, modules = stack_and_modules()
    assert stack["components"] == {
        **before[0]["components"],
        "old_flag": {"platform": "binary_sensor"},
    }
    for module, previous in zip(modules, before[1], strict=True):
        assert module["components"] == {
            **previous["components"],
            "cell_16_mv": {"platform": "sensor"},
        }
