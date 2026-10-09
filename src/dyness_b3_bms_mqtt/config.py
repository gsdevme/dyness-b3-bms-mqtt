"""Environment settings: names, defaults, parsing and validation (``08-configuration.md``).

Every variable the service reads is listed once, in spec order, in ``_VARIABLES`` below;
``DEFAULTS`` and ``VARIABLES`` are derived from it and nothing else in the package restates
a name or a default. ``load_settings`` turns an environment mapping into an immutable
``Settings`` or raises ``ConfigError`` listing every invalid variable at once (CFG-03).
Parsing is deliberately strict: an integer is ASCII digits with an optional leading ``-``,
and a decimal may add a ``.`` and fraction digits. Underscores, signs other than ``-``,
exponents, ``nan``, ``inf``, non-ASCII digits and surrounding whitespace are all rejected
rather than guessed at.

The only file system access is the ``MQTT_CA_FILE`` readability check, which uses
``os.access`` and never opens the file (CFG-07).
"""

import logging
import math
import operator
import os
import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

EX_CONFIG: Final = 78
REDACTED: Final = "<redacted>"
UNSET: Final = "(unset)"
SECRETS: Final = frozenset({"MQTT_PASSWORD"})
INTERFACES: Final = ("socketcan", "gs_usb", "slcan", "virtual")
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR")
MAX_MODULES: Final = 4
CLIENT_ID_PREFIX: Final = "dyness-b3-bms-mqtt"

_log = logging.getLogger(__name__)

_INTEGER = re.compile(r"-?[0-9]+")
_ADAPTER_INDEX = re.compile(r"[0-9]+")
_DECIMAL = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_USB_ID = re.compile(r"[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOPIC = re.compile(r"[A-Za-z0-9_-](?:[A-Za-z0-9_/-]{0,62}[A-Za-z0-9_-])?")
_NODE_ID = re.compile(r"[a-z0-9_]{1,32}")
_NOT_CLIENT_ID = re.compile(r"[^A-Za-z0-9_-]")
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})
_ABOVE_ZERO: Final = 1e-9


class _Invalid(ValueError):
    """A single variable failed validation; the message is the reason shown to the user."""


type Parser = Callable[[str], object]


def _number(value: float) -> str:
    """Return ``value`` without a trailing ``.0`` when it is a whole number."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _integer(lo: int, hi: int, *, or_zero: bool = False) -> Callable[[str], int]:
    """Return a parser for a plain ASCII integer in ``lo..hi``, or also ``0`` if ``or_zero``."""
    span = f"0 or between {lo} and {hi}" if or_zero else f"between {lo} and {hi}"

    def parse(raw: str) -> int:
        if not _INTEGER.fullmatch(raw):
            raise _Invalid("must be an integer")
        try:
            value = int(raw)
        except ValueError:
            value = hi + 1
        if not (lo <= value <= hi or (or_zero and value == 0)):
            raise _Invalid(f"must be {span}")
        return value

    return parse


def _decimal(lo: float, hi: float | None = None) -> Callable[[str], float]:
    """Return a parser for a finite plain decimal in ``lo..hi`` (no upper limit if ``None``).

    A ``lo`` of ``_ABOVE_ZERO`` stands for the spec's "> 0".
    """

    def parse(raw: str) -> float:
        if not _DECIMAL.fullmatch(raw):
            raise _Invalid("must be a decimal number")
        value = float(raw)
        if not math.isfinite(value):
            raise _Invalid("must be finite")
        if hi is not None and not lo <= value <= hi:
            raise _Invalid(f"must be between {_number(lo)} and {_number(hi)}")
        if value < lo:
            raise _Invalid(
                "must be greater than 0" if lo == _ABOVE_ZERO else f"must be at least {_number(lo)}"
            )
        return value

    return parse


def _boolean(raw: str) -> bool:
    """Parse a CFG-04 boolean, ASCII and case-insensitive."""
    folded = raw.lower() if raw.isascii() else raw
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise _Invalid("must be one of 1/0, true/false, yes/no, on/off")


def _choice(options: tuple[str, ...], *, fold: bool = False) -> Parser:
    """Return a parser accepting one of ``options``; ``fold`` makes it ASCII case-insensitive."""

    def parse(raw: str) -> str:
        value = raw.upper() if fold and raw.isascii() else raw
        if value not in options:
            raise _Invalid(f"must be one of {', '.join(options)}")
        return value

    return parse


def _matching(pattern: re.Pattern[str], reason: str) -> Parser:
    """Return a parser accepting strings that fully match ``pattern``."""

    def parse(raw: str) -> str:
        if not pattern.fullmatch(raw):
            raise _Invalid(reason)
        return raw

    return parse


def _text(longest: int) -> Parser:
    """Return a parser for free text of 1..``longest`` characters."""

    def parse(raw: str) -> str:
        if len(raw) > longest:
            raise _Invalid(f"must be 1 to {longest} characters")
        return raw

    return parse


def _usb_id(raw: str) -> str:
    """Parse a ``vendor:product`` USB id, returned in lower case."""
    if not _USB_ID.fullmatch(raw):
        raise _Invalid("must be hhhh:hhhh hexadecimal")
    return raw.lower()


_module_number = _integer(1, MAX_MODULES)


def _module_names(raw: str) -> Mapping[int, str]:
    """Parse ``n:Name,...`` into a mapping ordered by module number.

    Whitespace around each number and name is ignored.

    The module numbers are checked against ``MAX_MODULES`` here and against
    ``MODULE_COUNT`` in ``_check_relations``.
    """
    names: dict[int, str] = {}
    for entry in raw.split(","):
        number, colon, name = (part.strip() for part in entry.partition(":"))
        if not colon:
            raise _Invalid(f"entry {entry!r} is not n:Name")
        try:
            module = _module_number(number)
        except _Invalid as exc:
            raise _Invalid(f"module number {number!r} {exc}") from None
        if module in names:
            raise _Invalid(f"module {module} is named twice")
        if not 1 <= len(name) <= 32:
            raise _Invalid(f"name for module {module} must be 1 to 32 characters")
        if name in names.values():
            raise _Invalid(f"name {name!r} is used twice")
        names[module] = name
    return MappingProxyType(dict(sorted(names.items())))


_TOPIC_REASON = "must be 1 to 64 characters [A-Za-z0-9_/-] without a leading or trailing /"

_VARIABLES: Final[tuple[tuple[str, str | None, Parser], ...]] = (
    ("CAN_INTERFACE", "socketcan", _choice(INTERFACES)),
    ("CAN_CHANNEL", "can0", str),
    ("CAN_BITRATE", "500000", _integer(10000, 1000000)),
    ("CAN_RESTART_MS", "100", _integer(0, 60000)),
    ("CAN_LISTEN_ONLY", "false", _boolean),
    ("CAN_USB_ID", "1d50:606f", _usb_id),
    ("MODULE_COUNT", "3", _integer(1, MAX_MODULES)),
    ("MODULE_NAMES", None, _module_names),
    ("POLL_STAGGER_S", "0.3", _decimal(0.05, 5)),
    ("POLL_RETRY_S", "5", _decimal(1, 60)),
    ("POLL_MAX_RETRIES", "3", _integer(0, 20)),
    ("POLL_BACKOFF_S", "60", _decimal(_ABOVE_ZERO)),
    ("START_GRACE_S", "20", _decimal(5, 120)),
    ("CELL_STALE_S", "10", _decimal(2, 300)),
    ("MODULE_STALE_S", "10", _decimal(2, 300)),
    ("BUS_SILENCE_S", "15", _decimal(_ABOVE_ZERO)),
    ("BUS_REOPEN_ATTEMPTS", "2", _integer(0, 10)),
    ("RECOVERY_RESET_S", "300", _decimal(60)),
    ("DEAD_AFTER", "3", _integer(1, 20)),
    ("DEAD_RETRY_S", "60", _decimal(10, 3600)),
    ("DEAD_USB_RESET_S", "900", _decimal(_ABOVE_ZERO)),
    ("ERROR_SUSPEND_COUNT", "10", _integer(1, 1000)),
    ("ERROR_WINDOW_S", "60", _decimal(5, 3600)),
    ("MQTT_HOST", "localhost", str),
    ("MQTT_PORT", "1883", _integer(1, 65535)),
    ("MQTT_USERNAME", None, str),
    ("MQTT_PASSWORD", None, str),
    ("MQTT_CLIENT_ID", None, _matching(_CLIENT_ID, "must be 1 to 64 characters [A-Za-z0-9_-]")),
    ("MQTT_TLS", "false", _boolean),
    ("MQTT_CA_FILE", None, str),
    ("MQTT_KEEPALIVE_S", "30", _decimal(5, 300)),
    ("MQTT_BASE_TOPIC", "dyness_b3", _matching(_TOPIC, _TOPIC_REASON)),
    ("PUBLISH_INTERVAL_S", "10", _decimal(1, 300)),
    ("HA_DISCOVERY", "true", _boolean),
    ("HA_DISCOVERY_PREFIX", "homeassistant", _matching(_TOPIC, _TOPIC_REASON)),
    ("HA_NODE_ID", "dyness_b3", _matching(_NODE_ID, "must be 1 to 32 characters [a-z0-9_]")),
    ("HA_DEVICE_NAME", "Dyness B3", _text(64)),
    ("EXPIRE_AFTER_S", "0", _integer(10, 3600, or_zero=True)),
    ("ALERT_CELL_SPREAD_MV", "50", _integer(1, 1000)),
    ("ALERT_CELL_LOW_MV", "3000", _integer(2000, 3600)),
    ("ALERT_CURRENT_IMBALANCE_PCT", "20", _decimal(1, 100)),
    ("ALERT_IMBALANCE_MIN_CURRENT_A", "10", _decimal(0, 200)),
    ("ALERT_DEBOUNCE_S", "30", _decimal(0, 600)),
    ("ALERT_HYSTERESIS_PCT", "10", _decimal(0, 50)),
    ("ALERT_CELL_LOW_HYSTERESIS_MV", "50", _integer(0, 500)),
    ("LOG_LEVEL", "INFO", _choice(LOG_LEVELS, fold=True)),
    ("LOCAL_LOG", "true", _boolean),
    ("LOCAL_LOG_INTERVAL_S", "10", _decimal(1, 300)),
    ("LOCAL_LOG_RETENTION_DAYS", "90", _integer(1, 3650)),
    ("LOCAL_LOG_COMPRESS_AFTER_DAYS", "1", _integer(0, 3650)),
)

DEFAULTS: Final[Mapping[str, str | None]] = MappingProxyType(
    {name: default for name, default, _ in _VARIABLES}
)
"""Every variable name mapped to its default, in spec order; ``None`` means unset.

``MQTT_CLIENT_ID`` is ``None`` here because its default depends on the hostname and is
computed by ``load_settings``.
"""

VARIABLES: Final[tuple[str, ...]] = tuple(DEFAULTS)


type ErrorList = list[tuple[str, str, str]]


class ConfigError(Exception):
    """The environment holds one or more invalid variables.

    ``errors`` lists ``(name, shown_value, reason)`` in spec order, where ``shown_value``
    is the ``repr`` of the raw value, or ``REDACTED`` for a secret. ``str()`` gives one
    ``NAME=shown: reason`` line per error.
    """

    def __init__(self, errors: ErrorList) -> None:
        """Hold ``errors``; the message never contains a secret value."""
        self.errors = errors
        super().__init__("\n".join(f"{n}={shown}: {reason}" for n, shown, reason in errors))


@dataclass(frozen=True, slots=True)
class Settings:
    """The validated configuration, one field per variable in spec order (CFG-01).

    Optional variables that are not provided are ``None``; ``module_names`` is then an
    empty mapping. ``expire_after_s`` holds the resolved value, never the automatic ``0``.
    """

    can_interface: str
    can_channel: str
    can_bitrate: int
    can_restart_ms: int
    can_listen_only: bool
    can_usb_id: str
    module_count: int
    module_names: Mapping[int, str]
    poll_stagger_s: float
    poll_retry_s: float
    poll_max_retries: int
    poll_backoff_s: float
    start_grace_s: float
    cell_stale_s: float
    module_stale_s: float
    bus_silence_s: float
    bus_reopen_attempts: int
    recovery_reset_s: float
    dead_after: int
    dead_retry_s: float
    dead_usb_reset_s: float
    error_suspend_count: int
    error_window_s: float
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None = field(repr=False)
    mqtt_client_id: str
    mqtt_tls: bool
    mqtt_ca_file: str | None
    mqtt_keepalive_s: float
    mqtt_base_topic: str
    publish_interval_s: float
    ha_discovery: bool
    ha_discovery_prefix: str
    ha_node_id: str
    ha_device_name: str
    expire_after_s: int
    alert_cell_spread_mv: int
    alert_cell_low_mv: int
    alert_current_imbalance_pct: float
    alert_imbalance_min_current_a: float
    alert_debounce_s: float
    alert_hysteresis_pct: float
    alert_cell_low_hysteresis_mv: int
    log_level: str
    local_log: bool
    local_log_interval_s: float
    local_log_retention_days: int
    local_log_compress_after_days: int

    def module_name(self, module: int) -> str:
        """Return the ``MODULE_NAMES`` entry for ``module``, else ``Module <module>``."""
        return self.module_names.get(module, f"Module {module}")


def _readable(path: str) -> bool:
    """Return whether ``path`` is a regular file this process may read, without opening it."""
    return os.path.isfile(path) and os.access(path, os.R_OK)


def _default_client_id(hostname: str) -> str:
    """Return the default MQTT client id built from the short form of ``hostname``.

    Characters outside ``[A-Za-z0-9_-]`` become ``-`` and the result is cut to 64
    characters, so the default is always a valid client id.
    """
    short = _NOT_CLIENT_ID.sub("-", hostname.split(".", 1)[0])
    return f"{CLIENT_ID_PREFIX}-{short}"[:64] if short else CLIENT_ID_PREFIX


def _shown(name: str, raw: str) -> str:
    """Return how the raw value of ``name`` may appear in a log line."""
    return REDACTED if name in SECRETS else repr(raw)


def _check_relations(
    values: dict[str, Any],
    raw: Mapping[str, str],
    readable: Callable[[str], bool],
) -> ErrorList:
    """Return the errors of rules that involve another variable or the file system.

    A rule is only checked when every variable it reads parsed successfully, so one bad
    value is reported once rather than again through each rule that depends on it.
    """
    errors: ErrorList = []

    def fail(name: str, reason: str) -> None:
        errors.append((name, _shown(name, raw[name]), reason))

    def valid(*names: str) -> bool:
        return all(name in values for name in names)

    if (
        valid("CAN_INTERFACE", "CAN_CHANNEL")
        and values["CAN_INTERFACE"] == "gs_usb"
        and not _ADAPTER_INDEX.fullmatch(values["CAN_CHANNEL"])
    ):
        fail("CAN_CHANNEL", "must be the adapter index as a decimal integer for gs_usb")
    if valid("MODULE_COUNT", "MODULE_NAMES") and values["MODULE_NAMES"] is not None:
        count = values["MODULE_COUNT"]
        surplus = sorted(m for m in values["MODULE_NAMES"] if m > count)
        if surplus:
            fail("MODULE_NAMES", f"module {surplus[0]} is above MODULE_COUNT ({count})")
    if values["MQTT_CA_FILE"] is not None and not readable(values["MQTT_CA_FILE"]):
        fail("MQTT_CA_FILE", "must be an existing readable file")
    ordered = (
        ("POLL_BACKOFF_S", operator.ge, ">=", "POLL_RETRY_S"),
        ("BUS_SILENCE_S", operator.ge, ">=", "MODULE_STALE_S"),
        ("DEAD_USB_RESET_S", operator.ge, ">=", "DEAD_RETRY_S"),
        ("LOCAL_LOG_COMPRESS_AFTER_DAYS", operator.le, "<=", "LOCAL_LOG_RETENTION_DAYS"),
    )
    for name, holds, relation, other in ordered:
        if valid(name, other) and not holds(values[name], values[other]):
            fail(name, f"must be {relation} {other} ({_number(values[other])})")
    if values["MQTT_PASSWORD"] is not None and values["MQTT_USERNAME"] is None:
        fail("MQTT_PASSWORD", "requires MQTT_USERNAME")
    return errors


def load_settings(
    environ: Mapping[str, str],
    *,
    hostname: Callable[[], str] = socket.gethostname,
    readable: Callable[[str], bool] = _readable,
) -> Settings:
    """Return the settings in ``environ`` (CFG-01..05, CFG-12).

    Only ``environ`` is consulted, never ``os.environ``. An unset or empty variable takes
    its default. ``hostname`` supplies the default ``MQTT_CLIENT_ID``; ``readable`` checks
    ``MQTT_CA_FILE``.

    Raises:
        ConfigError: one or more variables are invalid; every failure is listed.

    """
    values: dict[str, Any] = {}
    raw: dict[str, str] = {}
    errors: ErrorList = []
    for name, default, parse in _VARIABLES:
        text = environ.get(name) or default
        if text is None and name == "MQTT_CLIENT_ID":
            text = _default_client_id(hostname())
        if text is None:
            values[name] = None
            continue
        raw[name] = text
        try:
            values[name] = parse(text)
        except _Invalid as exc:
            errors.append((name, _shown(name, text), str(exc)))
    errors += _check_relations(values, raw, readable)
    if errors:
        position = {name: index for index, name in enumerate(VARIABLES)}
        raise ConfigError(sorted(errors, key=lambda error: position[error[0]]))
    if values["EXPIRE_AFTER_S"] == 0:
        values["EXPIRE_AFTER_S"] = math.ceil(3 * values["PUBLISH_INTERVAL_S"] + 5)
    if values["MODULE_NAMES"] is None:
        values["MODULE_NAMES"] = MappingProxyType({})
    return Settings(**{name.lower(): value for name, value in values.items()})


def _display(value: object) -> str:
    """Return ``value`` as ``describe`` shows it."""
    if value is None:
        return UNSET
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, Mapping):
        return ",".join(f"{key}:{item}" for key, item in value.items()) or UNSET
    return str(value)


def describe(settings: Settings) -> list[str]:
    """Return one ``NAME=value`` line per variable in spec order, secrets redacted (CFG-06)."""
    lines: list[str] = []
    for name in VARIABLES:
        value = getattr(settings, name.lower())
        shown = REDACTED if name in SECRETS and value is not None else _display(value)
        lines.append(f"{name}={shown}")
    return lines


def check_config(environ: Mapping[str, str]) -> int:
    """Validate ``environ`` and log the outcome; return 0 or ``EX_CONFIG`` (CFG-06).

    A valid configuration is logged at ``INFO``, one variable per line; each invalid
    variable is logged at ``ERROR`` with its shown value and reason.
    """
    try:
        settings = load_settings(environ)
    except ConfigError as exc:
        for name, shown, reason in exc.errors:
            _log.error("invalid configuration: %s=%s: %s", name, shown, reason)
        return EX_CONFIG
    for line in describe(settings):
        _log.info("configuration: %s", line)
    return 0
