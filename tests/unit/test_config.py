"""Tests of environment parsing and validation (``config``) against ``08-configuration.md``."""

import builtins
import dataclasses
import io
import logging
import math
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

import pytest

from dyness_b3_bms_mqtt.config import (
    DEFAULTS,
    EX_CONFIG,
    REDACTED,
    SECRETS,
    VARIABLES,
    ConfigError,
    Settings,
    check_config,
    describe,
    load_settings,
)
from tests.support.settings import make_settings

SPEC = Path(__file__).resolve().parents[2] / "docs" / "specs" / "08-configuration.md"
ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
CLIENT_ID_DEFAULT = "dyness-b3-bms-mqtt-<hostname>"
PYTHON_TYPES: dict[str, type] = {
    "int": int,
    "duration": float,
    "number": float,
    "bool": bool,
    "enum": str,
    "string": str,
    "path": str,
}
BOOLEANS = ("CAN_LISTEN_ONLY", "MQTT_TLS", "HA_DISCOVERY", "LOCAL_LOG")
EVERYTHING_SET = {
    "CAN_INTERFACE": "slcan",
    "CAN_CHANNEL": "/dev/ttyACM0",
    "CAN_BITRATE": "250000",
    "CAN_RESTART_MS": "200",
    "CAN_LISTEN_ONLY": "true",
    "CAN_USB_ID": "1d50:606f",
    "MODULE_COUNT": "2",
    "MODULE_NAMES": "1:Top,2:Bottom",
    "POLL_STAGGER_S": "0.5",
    "POLL_RETRY_S": "6",
    "POLL_MAX_RETRIES": "4",
    "POLL_BACKOFF_S": "90",
    "START_GRACE_S": "30",
    "CELL_STALE_S": "12",
    "MODULE_STALE_S": "11",
    "BUS_SILENCE_S": "16",
    "BUS_REOPEN_ATTEMPTS": "3",
    "RECOVERY_RESET_S": "600",
    "DEAD_AFTER": "4",
    "DEAD_RETRY_S": "120",
    "DEAD_USB_RESET_S": "1800",
    "ERROR_SUSPEND_COUNT": "20",
    "ERROR_WINDOW_S": "120",
    "MQTT_HOST": "broker.lan",
    "MQTT_PORT": "8883",
    "MQTT_USERNAME": "bms",
    "MQTT_PASSWORD": "hunter2-secret",
    "MQTT_CLIENT_ID": "stack-1",
    "MQTT_TLS": "yes",
    "MQTT_KEEPALIVE_S": "60",
    "MQTT_BASE_TOPIC": "home/battery",
    "PUBLISH_INTERVAL_S": "5",
    "HA_DISCOVERY": "no",
    "HA_DISCOVERY_PREFIX": "ha",
    "HA_NODE_ID": "garage_b3",
    "HA_DEVICE_NAME": "Garage battery",
    "EXPIRE_AFTER_S": "60",
    "ALERT_CELL_SPREAD_MV": "40",
    "ALERT_CELL_LOW_MV": "3100",
    "ALERT_CURRENT_IMBALANCE_PCT": "25.5",
    "ALERT_IMBALANCE_MIN_CURRENT_A": "5",
    "ALERT_DEBOUNCE_S": "15",
    "ALERT_HYSTERESIS_PCT": "5",
    "ALERT_CELL_LOW_HYSTERESIS_MV": "40",
    "LOG_LEVEL": "debug",
    "LOCAL_LOG": "off",
    "LOCAL_LOG_INTERVAL_S": "30",
    "LOCAL_LOG_RETENTION_DAYS": "30",
    "LOCAL_LOG_COMPRESS_AFTER_DAYS": "2",
}


def _spec_rows() -> list[tuple[str, str, str | None]]:
    """Return ``(name, type, default)`` for every variable table row in spec order."""
    rows: list[tuple[str, str, str | None]] = []
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match is None:
            continue
        name, kind, default = match.groups()
        if default == "*(unset)*":
            rows.append((name, kind, None))
        else:
            assert default.startswith("`") and default.endswith("`"), line
            rows.append((name, kind, default.strip("`")))
    return rows


def _typed(kind: str, text: str) -> object:
    """Return the spec default ``text`` converted to the Python type of ``kind``."""
    if kind == "int":
        return int(text)
    if kind in {"duration", "number"}:
        return float(text)
    if kind == "bool":
        return text == "true"
    return text


def _failures(env: dict[str, str], **kwargs: Any) -> list[tuple[str, str, str]]:
    """Return the errors that loading ``env`` on a virtual bus reports."""
    with pytest.raises(ConfigError) as caught:
        load_settings(
            {"CAN_INTERFACE": "virtual", "CAN_CHANNEL": "test", **env},
            hostname=lambda: "testhost",
            **kwargs,
        )
    return caught.value.errors


def _failing_names(env: dict[str, str], **kwargs: Any) -> list[str]:
    return [name for name, _, _ in _failures(env, **kwargs)]


@pytest.mark.req("CFG-01", "CFG-02")
def test_defaults_apply_when_unset_or_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MQTT_PORT", "not-a-port")
    unset = load_settings({}, hostname=lambda: "testhost")
    empty = load_settings(dict.fromkeys(VARIABLES, ""), hostname=lambda: "testhost")
    explicit = load_settings(
        {name: value for name, value in DEFAULTS.items() if value is not None},
        hostname=lambda: "testhost",
    )
    assert unset == empty == explicit
    assert unset.mqtt_port == 1883
    assert unset.mqtt_username is None
    assert unset.mqtt_password is None
    assert unset.mqtt_ca_file is None
    assert dict(unset.module_names) == {}
    assert unset.mqtt_client_id == "dyness-b3-bms-mqtt-testhost"
    assert unset.expire_after_s == 35
    with pytest.raises(dataclasses.FrozenInstanceError):
        unset.mqtt_port = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        unset.module_names[1] = "Top"  # type: ignore[index]


@pytest.mark.req("CFG-02", "CFG-09")
def test_variable_names_and_defaults_match_spec_table() -> None:
    rows = _spec_rows()
    assert [name for name, _, _ in rows] == list(VARIABLES) == list(DEFAULTS)
    assert len(set(VARIABLES)) == len(VARIABLES)
    assert [field.name for field in dataclasses.fields(Settings)] == [
        name.lower() for name in VARIABLES
    ]
    settings = load_settings({}, hostname=lambda: "testhost")
    for name, kind, default in rows:
        value = getattr(settings, name.lower())
        if name == "MQTT_CLIENT_ID":
            assert default == CLIENT_ID_DEFAULT
            assert DEFAULTS[name] is None
            assert value == "dyness-b3-bms-mqtt-testhost"
            continue
        assert DEFAULTS[name] == default, name
        if default is None:
            assert not value, name
        elif name == "EXPIRE_AFTER_S":
            assert default == "0"
            assert value == math.ceil(3 * settings.publish_interval_s + 5)
        else:
            assert type(value) is PYTHON_TYPES[kind], name
            assert value == _typed(kind, default), name


def _range_cases() -> list[Any]:
    integers: list[tuple[str, int, int, dict[str, str]]] = [
        ("CAN_BITRATE", 10000, 1000000, {}),
        ("CAN_RESTART_MS", 0, 60000, {}),
        ("MODULE_COUNT", 1, 4, {}),
        ("POLL_MAX_RETRIES", 0, 20, {}),
        ("BUS_REOPEN_ATTEMPTS", 0, 10, {}),
        ("DEAD_AFTER", 1, 20, {}),
        ("ERROR_SUSPEND_COUNT", 1, 1000, {}),
        ("MQTT_PORT", 1, 65535, {}),
        ("EXPIRE_AFTER_S", 10, 3600, {}),
        ("ALERT_CELL_SPREAD_MV", 1, 1000, {}),
        ("ALERT_CELL_LOW_MV", 2000, 3600, {}),
        ("ALERT_CELL_LOW_HYSTERESIS_MV", 0, 500, {}),
        ("LOCAL_LOG_RETENTION_DAYS", 1, 3650, {}),
        ("LOCAL_LOG_COMPRESS_AFTER_DAYS", 0, 3650, {"LOCAL_LOG_RETENTION_DAYS": "3650"}),
    ]
    decimals: list[tuple[str, str, str | None, dict[str, str]]] = [
        ("POLL_STAGGER_S", "0.05", "5", {}),
        ("POLL_RETRY_S", "1", "60", {"POLL_BACKOFF_S": "100"}),
        ("POLL_BACKOFF_S", "0", None, {}),
        ("START_GRACE_S", "5", "120", {}),
        ("CELL_STALE_S", "2", "300", {}),
        ("MODULE_STALE_S", "2", "300", {"BUS_SILENCE_S": "400"}),
        ("BUS_SILENCE_S", "0", None, {}),
        ("RECOVERY_RESET_S", "60", None, {}),
        ("DEAD_RETRY_S", "10", "3600", {"DEAD_USB_RESET_S": "4000"}),
        ("DEAD_USB_RESET_S", "0", None, {}),
        ("ERROR_WINDOW_S", "5", "3600", {}),
        ("MQTT_KEEPALIVE_S", "5", "300", {}),
        ("PUBLISH_INTERVAL_S", "1", "300", {}),
        ("ALERT_CURRENT_IMBALANCE_PCT", "1", "100", {}),
        ("ALERT_IMBALANCE_MIN_CURRENT_A", "0", "200", {}),
        ("ALERT_DEBOUNCE_S", "0", "600", {}),
        ("ALERT_HYSTERESIS_PCT", "0", "50", {}),
        ("LOCAL_LOG_INTERVAL_S", "1", "300", {}),
    ]
    cases: list[Any] = []
    for name, lo, hi, extra in integers:
        for number, ok in ((lo, True), (hi, True), (lo - 1, False), (hi + 1, False)):
            cases.append(pytest.param(name, str(number), ok, extra, id=f"{name}={number}"))
    step = Decimal("0.001")
    for name, low, high, extra in decimals:
        if high is None and low == "0":
            values = [("0", False), ("-1", False), ("1000000", True)]
        elif high is None:
            values = [(low, True), (str(Decimal(low) - step), False), ("1000000", True)]
        else:
            values = [
                (low, True),
                (high, True),
                (str(Decimal(low) - step), False),
                (str(Decimal(high) + step), False),
            ]
        for text, ok in values:
            cases.append(pytest.param(name, text, ok, extra, id=f"{name}={text}"))
    return cases


@pytest.mark.req("CFG-03", "CFG-05")
@pytest.mark.parametrize(("name", "value", "ok", "extra"), _range_cases())
def test_range_limits(name: str, value: str, ok: bool, extra: dict[str, str]) -> None:
    env = {**extra, name: value}
    if ok:
        assert getattr(make_settings(**env), name.lower()) == float(value)
    else:
        assert _failing_names(env) == [name]


@pytest.mark.req("CFG-03", "CFG-05")
@pytest.mark.parametrize(
    "value",
    [
        "0x50",
        "0o17",
        "1e3",
        "1883.0",
        "18_83",
        "+1883",
        " 1883",
        "1883 ",
        "1883\n",
        "\uff11\uff18\uff18\uff13",
        "١٨٨٣",
        "1883a",
        "-",
        "true",
    ],
)
def test_integers_are_strict_ascii(value: str) -> None:
    assert _failures({"MQTT_PORT": value}) == [("MQTT_PORT", repr(value), "must be an integer")]


@pytest.mark.req("CFG-03", "CFG-05")
def test_integers_with_huge_digit_counts_are_out_of_range() -> None:
    assert _failing_names({"MQTT_PORT": "9" * 5000}) == ["MQTT_PORT"]
    assert _failing_names({"CAN_RESTART_MS": "-" + "9" * 5000}) == ["CAN_RESTART_MS"]


@pytest.mark.req("CFG-05")
def test_durations_are_finite_plain_decimals() -> None:
    for value in ("nan", "NaN", "inf", "-inf", "Infinity", "1e1", "1E1", "10s", "1_0"):
        assert _failures({"PUBLISH_INTERVAL_S": value}) == [
            ("PUBLISH_INTERVAL_S", repr(value), "must be a decimal number")
        ], value
    for value in (" 10", "10 ", "+10", "\uff11\uff10", "0x10", ".", "-.", "10..0", "1,5"):
        assert _failing_names({"PUBLISH_INTERVAL_S": value}) == ["PUBLISH_INTERVAL_S"], value
    assert _failures({"RECOVERY_RESET_S": "9" * 400}) == [
        ("RECOVERY_RESET_S", repr("9" * 400), "must be finite")
    ]
    assert make_settings(POLL_STAGGER_S=".5").poll_stagger_s == 0.5
    assert make_settings(POLL_STAGGER_S="1.").poll_stagger_s == 1.0
    assert make_settings(POLL_STAGGER_S="0.30").poll_stagger_s == 0.3
    assert make_settings(ALERT_DEBOUNCE_S="-0").alert_debounce_s == 0.0
    assert type(make_settings(PUBLISH_INTERVAL_S="10").publish_interval_s) is float
    assert type(make_settings(CAN_RESTART_MS="10").can_restart_ms) is int


@pytest.mark.req("CFG-04")
@pytest.mark.parametrize("name", BOOLEANS)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("yes", True),
        ("YeS", True),
        ("on", True),
        ("ON", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("No", False),
        ("off", False),
        ("oFf", False),
    ],
)
def test_boolean_spellings(name: str, value: str, expected: bool) -> None:
    assert getattr(make_settings(**{name: value}), name.lower()) is expected


@pytest.mark.req("CFG-03", "CFG-04")
@pytest.mark.parametrize("name", BOOLEANS)
@pytest.mark.parametrize(
    "value",
    [
        "2",
        "-1",
        "y",
        "n",
        "t",
        "f",
        "tru",
        "enabled",
        " true",
        "true ",
        "0.0",
        "\uff59\uff45\uff53",
    ],
)
def test_bad_booleans(name: str, value: str) -> None:
    assert _failures({name: value}) == [
        (name, repr(value), "must be one of 1/0, true/false, yes/no, on/off")
    ]


@pytest.mark.req("CFG-03")
@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"CAN_INTERFACE": "kvaser"}, "CAN_INTERFACE"),
        ({"CAN_INTERFACE": "SocketCAN"}, "CAN_INTERFACE"),
        ({"CAN_INTERFACE": "gs_usb", "CAN_CHANNEL": "can0"}, "CAN_CHANNEL"),
        ({"CAN_INTERFACE": "gs_usb", "CAN_CHANNEL": "-1"}, "CAN_CHANNEL"),
        ({"CAN_INTERFACE": "gs_usb", "CAN_CHANNEL": "\uff10"}, "CAN_CHANNEL"),
        ({"CAN_INTERFACE": "gs_usb", "CAN_CHANNEL": " 0"}, "CAN_CHANNEL"),
        ({"CAN_USB_ID": "1d50606f"}, "CAN_USB_ID"),
        ({"CAN_USB_ID": "1d50:606"}, "CAN_USB_ID"),
        ({"CAN_USB_ID": "gggg:0000"}, "CAN_USB_ID"),
        ({"CAN_USB_ID": "1d50:606f "}, "CAN_USB_ID"),
        ({"MODULE_NAMES": "1Top"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "0:Zero"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "4:Four"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "5:Five", "MODULE_COUNT": "4"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "a:Top"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "-1:Top"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "9" * 5000 + ":Top"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "1:Top,1:Again"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "1:Same,2:Same"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "1:"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "1:Top,"}, "MODULE_NAMES"),
        ({"MODULE_NAMES": "1:" + "x" * 33}, "MODULE_NAMES"),
        ({"MQTT_CLIENT_ID": "has space"}, "MQTT_CLIENT_ID"),
        ({"MQTT_CLIENT_ID": "dyness.b3"}, "MQTT_CLIENT_ID"),
        ({"MQTT_CLIENT_ID": "x" * 65}, "MQTT_CLIENT_ID"),
        ({"MQTT_BASE_TOPIC": "/dyness"}, "MQTT_BASE_TOPIC"),
        ({"MQTT_BASE_TOPIC": "dyness/"}, "MQTT_BASE_TOPIC"),
        ({"MQTT_BASE_TOPIC": "dyness/+"}, "MQTT_BASE_TOPIC"),
        ({"MQTT_BASE_TOPIC": "dyness/#"}, "MQTT_BASE_TOPIC"),
        ({"MQTT_BASE_TOPIC": "dyness b3"}, "MQTT_BASE_TOPIC"),
        ({"MQTT_BASE_TOPIC": "x" * 65}, "MQTT_BASE_TOPIC"),
        ({"HA_DISCOVERY_PREFIX": "/homeassistant"}, "HA_DISCOVERY_PREFIX"),
        ({"HA_DISCOVERY_PREFIX": "home+assistant"}, "HA_DISCOVERY_PREFIX"),
        ({"HA_DISCOVERY_PREFIX": "x" * 65}, "HA_DISCOVERY_PREFIX"),
        ({"HA_NODE_ID": "Dyness"}, "HA_NODE_ID"),
        ({"HA_NODE_ID": "dyness-b3"}, "HA_NODE_ID"),
        ({"HA_NODE_ID": "x" * 33}, "HA_NODE_ID"),
        ({"HA_DEVICE_NAME": "x" * 65}, "HA_DEVICE_NAME"),
        ({"EXPIRE_AFTER_S": "5"}, "EXPIRE_AFTER_S"),
        ({"LOG_LEVEL": "TRACE"}, "LOG_LEVEL"),
        ({"LOG_LEVEL": "CRITICAL"}, "LOG_LEVEL"),
        ({"LOG_LEVEL": "INFO "}, "LOG_LEVEL"),
        ({"LOG_LEVEL": "\u0131nfo"}, "LOG_LEVEL"),
    ],
)
def test_invalid_values(env: dict[str, str], name: str) -> None:
    assert _failing_names(env) == [name]


@pytest.mark.req("CFG-03")
def test_unreadable_ca_file_is_invalid() -> None:
    seen: list[str] = []

    def unreadable(path: str) -> bool:
        seen.append(path)
        return False

    errors = _failures({"MQTT_TLS": "true", "MQTT_CA_FILE": "/etc/ssl/ca.pem"}, readable=unreadable)
    assert errors == [("MQTT_CA_FILE", "'/etc/ssl/ca.pem'", "must be an existing readable file")]
    assert seen == ["/etc/ssl/ca.pem"]


@pytest.mark.req("CFG-03")
def test_valid_specials(tmp_path: Path) -> None:
    assert make_settings(EXPIRE_AFTER_S="0", PUBLISH_INTERVAL_S="10").expire_after_s == 35
    assert make_settings(EXPIRE_AFTER_S="0", PUBLISH_INTERVAL_S="1.5").expire_after_s == 10
    assert make_settings(EXPIRE_AFTER_S="0", PUBLISH_INTERVAL_S="300").expire_after_s == 905
    assert make_settings(EXPIRE_AFTER_S="10", PUBLISH_INTERVAL_S="300").expire_after_s == 10

    named = make_settings(MODULE_NAMES="1:Top,2:Middle shelf,3:Bottom")
    assert dict(named.module_names) == {1: "Top", 2: "Middle shelf", 3: "Bottom"}
    assert [named.module_name(m) for m in (1, 2, 3)] == ["Top", "Middle shelf", "Bottom"]
    spaced = make_settings(MODULE_NAMES=" 3 : Bottom ,1:Top")
    assert list(spaced.module_names.items()) == [(1, "Top"), (3, "Bottom")]
    partial = make_settings(MODULE_NAMES="2:Middle", MODULE_COUNT="3")
    assert [partial.module_name(m) for m in (1, 2, 3)] == ["Module 1", "Middle", "Module 3"]
    four = make_settings(MODULE_NAMES="4:Spare:annex", MODULE_COUNT="4")
    assert four.module_name(4) == "Spare:annex"
    assert make_settings(MODULE_NAMES="1:" + "x" * 32).module_name(1) == "x" * 32

    assert make_settings(CAN_INTERFACE="gs_usb", CAN_CHANNEL="0").can_channel == "0"
    assert make_settings(CAN_INTERFACE="gs_usb", CAN_CHANNEL="12").can_channel == "12"
    slcan = make_settings(CAN_INTERFACE="slcan", CAN_CHANNEL="/dev/ttyACM0")
    assert slcan.can_channel == "/dev/ttyACM0"
    assert make_settings(CAN_USB_ID="1D50:606F").can_usb_id == "1d50:606f"
    assert make_settings(LOG_LEVEL="warning").log_level == "WARNING"
    assert make_settings(MQTT_BASE_TOPIC="home/dyness_b3-1").mqtt_base_topic == "home/dyness_b3-1"
    assert make_settings(HA_NODE_ID="x" * 32).ha_node_id == "x" * 32
    assert make_settings(MQTT_CLIENT_ID="x" * 64).mqtt_client_id == "x" * 64
    assert make_settings(MQTT_USERNAME="").mqtt_username is None

    ca = tmp_path / "ca.pem"
    ca.write_text("certificate\n")
    assert make_settings(MQTT_TLS="1", MQTT_CA_FILE=str(ca)).mqtt_ca_file == str(ca)
    with pytest.raises(ConfigError):
        make_settings(MQTT_CA_FILE=str(tmp_path / "missing.pem"))
    with pytest.raises(ConfigError):
        make_settings(MQTT_CA_FILE=str(tmp_path))

    def client_id(host: str) -> str:
        return load_settings({}, hostname=lambda: host).mqtt_client_id

    assert client_id("pi-zero.local") == "dyness-b3-bms-mqtt-pi-zero"
    assert client_id("bad host!") == "dyness-b3-bms-mqtt-bad-host-"
    assert client_id("kitchen_pi") == "dyness-b3-bms-mqtt-kitchen_pi"
    assert client_id("") == "dyness-b3-bms-mqtt"
    assert client_id("h" * 80) == ("dyness-b3-bms-mqtt-" + "h" * 80)[:64]
    assert load_settings({"MQTT_CLIENT_ID": "fixed"}, hostname=lambda: "x").mqtt_client_id == (
        "fixed"
    )


@pytest.mark.req("CFG-12")
@pytest.mark.parametrize(
    ("env", "failing"),
    [
        ({"POLL_RETRY_S": "10", "POLL_BACKOFF_S": "9.99"}, ["POLL_BACKOFF_S"]),
        ({"POLL_RETRY_S": "10", "POLL_BACKOFF_S": "10"}, []),
        ({"MODULE_STALE_S": "20", "BUS_SILENCE_S": "19.9"}, ["BUS_SILENCE_S"]),
        ({"MODULE_STALE_S": "20", "BUS_SILENCE_S": "20"}, []),
        ({"DEAD_RETRY_S": "100", "DEAD_USB_RESET_S": "99"}, ["DEAD_USB_RESET_S"]),
        ({"DEAD_RETRY_S": "100", "DEAD_USB_RESET_S": "100"}, []),
        (
            {"LOCAL_LOG_RETENTION_DAYS": "7", "LOCAL_LOG_COMPRESS_AFTER_DAYS": "8"},
            ["LOCAL_LOG_COMPRESS_AFTER_DAYS"],
        ),
        ({"LOCAL_LOG_RETENTION_DAYS": "7", "LOCAL_LOG_COMPRESS_AFTER_DAYS": "7"}, []),
        ({"LOCAL_LOG_RETENTION_DAYS": "7", "LOCAL_LOG_COMPRESS_AFTER_DAYS": "0"}, []),
        ({"MQTT_PASSWORD": "s3cret"}, ["MQTT_PASSWORD"]),
        ({"MQTT_PASSWORD": "s3cret", "MQTT_USERNAME": "bms"}, []),
        ({"MQTT_USERNAME": "bms"}, []),
    ],
)
def test_cross_variable_rules(env: dict[str, str], failing: list[str]) -> None:
    if failing:
        errors = _failures(env)
        assert [name for name, _, _ in errors] == failing
        assert "s3cret" not in str(errors)
    else:
        make_settings(**env)


@pytest.mark.req("CFG-03", "CFG-12")
def test_all_failures_are_reported_together() -> None:
    env = {
        "CAN_BITRATE": "fast",
        "CAN_LISTEN_ONLY": "maybe",
        "MODULE_COUNT": "9",
        "MODULE_NAMES": "4:Four",
        "POLL_RETRY_S": "abc",
        "POLL_BACKOFF_S": "1",
        "MODULE_STALE_S": "30",
        "BUS_SILENCE_S": "20",
        "MQTT_PORT": "0",
        "MQTT_PASSWORD": "s3cret",
        "LOG_LEVEL": "loud",
    }
    with pytest.raises(ConfigError) as caught:
        make_settings(**env)
    errors = caught.value.errors
    assert [name for name, _, _ in errors] == [
        "CAN_BITRATE",
        "CAN_LISTEN_ONLY",
        "MODULE_COUNT",
        "POLL_RETRY_S",
        "BUS_SILENCE_S",
        "MQTT_PORT",
        "MQTT_PASSWORD",
        "LOG_LEVEL",
    ]
    assert ("MQTT_PORT", "'0'", "must be between 1 and 65535") in errors
    assert ("MQTT_PASSWORD", REDACTED, "requires MQTT_USERNAME") in errors
    text = str(caught.value)
    assert len(text.splitlines()) == len(errors)
    for name, shown, reason in errors:
        assert f"{name}={shown}: {reason}" in text
    assert "s3cret" not in text
    assert "s3cret" not in repr(caught.value)


@pytest.mark.req("CFG-06")
def test_describe_redacts_password() -> None:
    settings = make_settings(**EVERYTHING_SET)
    lines = describe(settings)
    assert [line.split("=", 1)[0] for line in lines] == list(VARIABLES)
    shown = dict(line.split("=", 1) for line in lines)
    assert {"MQTT_PASSWORD"} == SECRETS
    assert shown["MQTT_PASSWORD"] == REDACTED
    assert "hunter2-secret" not in "\n".join(lines)
    assert "hunter2-secret" not in repr(settings)
    assert shown["MQTT_USERNAME"] == "bms"
    assert shown["CAN_LISTEN_ONLY"] == "true"
    assert shown["HA_DISCOVERY"] == "false"
    assert shown["POLL_STAGGER_S"] == "0.5"
    assert shown["POLL_RETRY_S"] == "6"
    assert shown["ALERT_CURRENT_IMBALANCE_PCT"] == "25.5"
    assert shown["MODULE_NAMES"] == "1:Top,2:Bottom"
    assert shown["LOG_LEVEL"] == "DEBUG"
    defaults = dict(line.split("=", 1) for line in describe(make_settings()))
    assert defaults["MQTT_PASSWORD"] == "(unset)"
    assert defaults["MODULE_NAMES"] == "(unset)"
    assert defaults["EXPIRE_AFTER_S"] == "35"
    assert defaults["MQTT_CLIENT_ID"] == "dyness-b3-bms-mqtt-testhost"


@pytest.mark.req("CFG-03", "CFG-06")
def test_check_config_logs_and_returns_code(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    valid = {"CAN_INTERFACE": "virtual", "MQTT_USERNAME": "bms", "MQTT_PASSWORD": "s3cret"}
    assert check_config(valid) == 0
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert all(any(f"{name}=" in message for message in infos) for name in VARIABLES)
    assert any(f"MQTT_PASSWORD={REDACTED}" in message for message in infos)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "s3cret" not in caplog.text

    caplog.clear()
    assert check_config({**valid, "MQTT_PORT": "0", "MQTT_USERNAME": ""}) == EX_CONFIG == 78
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("MQTT_PORT='0': must be between 1 and 65535" in message for message in errors)
    assert any(f"MQTT_PASSWORD={REDACTED}: requires MQTT_USERNAME" in message for message in errors)
    assert not [r for r in caplog.records if r.levelno == logging.INFO and "=" in r.getMessage()]
    assert "s3cret" not in caplog.text


@pytest.mark.req("CFG-01", "CFG-07")
def test_reads_no_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("certificate\n")
    opened: list[object] = []

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        opened.append(args[0] if args else kwargs)
        raise AssertionError(f"configuration opened a file: {args!r}")

    monkeypatch.setenv("MQTT_PORT", "not-a-port")
    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(io, "open", refuse)
    monkeypatch.setattr(os, "open", refuse)
    settings = load_settings({**EVERYTHING_SET, "MQTT_CA_FILE": str(ca)}, hostname=lambda: "x")
    assert settings.mqtt_ca_file == str(ca)
    assert settings.mqtt_port == 8883
    assert check_config({"CAN_INTERFACE": "virtual"}) == 0
    assert check_config({"MQTT_PORT": "0"}) == EX_CONFIG
    assert opened == []
