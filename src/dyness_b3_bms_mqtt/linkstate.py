"""The kernel's view of a SocketCAN link, from ``ip -j -d -s link show`` (02-can-transport.md §7).

:func:`query_link_state` runs ``ip`` unprivileged as an argument list and never raises: every
failure becomes a :class:`LinkState` whose ``state`` is :attr:`CanState.ABSENT` or
:attr:`CanState.UNKNOWN`. :func:`parse_ip_link` turns the JSON into a :class:`LinkState`,
setting each missing or mistyped field to ``None`` on its own (CAN-28). The transport
(``canbus.py``) decides when to ask; for interfaces other than SocketCAN it never runs the
command (CAN-30).
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

IP_TIMEOUT_S = 2.0
"""Timeout of one ``ip`` run (CAN-27)."""

EXTRA_PATH = ("/usr/sbin", "/sbin")
"""Directories searched for ``ip`` after ``PATH`` (CAN-27)."""

type Runner = Callable[..., subprocess.CompletedProcess[str]]
type Which = Callable[..., str | None]


class CanState(StrEnum):
    """The CAN controller state; the value is the published lower-case hyphenated form."""

    ERROR_ACTIVE = "error-active"
    ERROR_WARNING = "error-warning"
    ERROR_PASSIVE = "error-passive"
    BUS_OFF = "bus-off"
    STOPPED = "stopped"
    DOWN = "down"
    ABSENT = "absent"
    UNKNOWN = "unknown"

    @property
    def published(self) -> str | None:
        """Return the ``can_state`` value for MQTT (MQTT-18): ``None`` for ``UNKNOWN``."""
        return None if self is CanState.UNKNOWN else self.value


_KERNEL_STATES: Mapping[str, CanState] = {
    "ERROR-ACTIVE": CanState.ERROR_ACTIVE,
    "ERROR-WARNING": CanState.ERROR_WARNING,
    "ERROR-PASSIVE": CanState.ERROR_PASSIVE,
    "BUS-OFF": CanState.BUS_OFF,
    "STOPPED": CanState.STOPPED,
}


@dataclass(frozen=True, slots=True)
class LinkState:
    """One bus-state query result; ``at`` is the monotonic time of the query.

    ``rx_dropped`` is ``stats64.rx.dropped``, frames the kernel dropped on receive.
    """

    state: CanState
    operstate: str | None
    listen_only: bool | None
    bitrate: int | None
    berr_tx: int | None
    berr_rx: int | None
    restarts: int | None
    bus_off: int | None
    rx_dropped: int | None
    at: float


def unknown_link_state(at: float) -> LinkState:
    """Return the state reported when nothing is known: every field ``None``."""
    return _bare(CanState.UNKNOWN, at)


def _bare(state: CanState, at: float) -> LinkState:
    return LinkState(state, None, None, None, None, None, None, None, None, at)


def _obj(value: object) -> Mapping[str, Any]:
    """Return ``value`` if it is a JSON object, else an empty mapping."""
    return value if isinstance(value, dict) else {}


def _int(value: object) -> int | None:
    """Return ``value`` if it is a JSON integer (not a boolean), else ``None``."""
    return value if type(value) is int else None


def _listen_only(info_data: object) -> bool | None:
    """Return the listen-only mode from ``info_data.ctrlmode``.

    ``ip -j`` omits ``ctrlmode`` when no control-mode flag is set, so a missing ``ctrlmode``
    in a present ``info_data`` means ``False``. A missing ``info_data`` or a ``ctrlmode``
    that is not a list means the mode cannot be determined.
    """
    if not isinstance(info_data, dict):
        return None
    ctrlmode = info_data.get("ctrlmode", [])
    if not isinstance(ctrlmode, list):
        return None
    return "LISTEN-ONLY" in ctrlmode


def _state(link: Mapping[str, Any], info_data: Mapping[str, Any]) -> CanState:
    flags = link.get("flags")
    if not isinstance(flags, list):
        return CanState.UNKNOWN
    if "UP" not in flags:
        return CanState.DOWN
    kernel_state = info_data.get("state")
    if not isinstance(kernel_state, str):
        return CanState.UNKNOWN
    return _KERNEL_STATES.get(kernel_state, CanState.UNKNOWN)


def parse_ip_link(stdout: str, at: float) -> LinkState:
    """Parse ``ip -j -d -s link show`` output (CAN-28); never raises.

    Output that is not JSON, or whose first array element is not an object, gives
    :func:`unknown_link_state`.
    """
    try:
        document = json.loads(stdout)
    except ValueError:
        return unknown_link_state(at)
    if not isinstance(document, list) or not document or not isinstance(document[0], dict):
        return unknown_link_state(at)
    link: Mapping[str, Any] = document[0]
    linkinfo = _obj(link.get("linkinfo"))
    raw_info_data = linkinfo.get("info_data")
    info_data = _obj(raw_info_data)
    xstats = _obj(linkinfo.get("info_xstats"))
    berr = _obj(info_data.get("berr_counter"))
    operstate = link.get("operstate")
    return LinkState(
        state=_state(link, info_data),
        operstate=operstate if isinstance(operstate, str) else None,
        listen_only=_listen_only(raw_info_data),
        bitrate=_int(_obj(info_data.get("bittiming")).get("bitrate")),
        berr_tx=_int(berr.get("tx")),
        berr_rx=_int(berr.get("rx")),
        restarts=_int(xstats.get("restarts")),
        bus_off=_int(xstats.get("bus_off")),
        rx_dropped=_int(_obj(_obj(link.get("stats64")).get("rx")).get("dropped")),
        at=at,
    )


def query_link_state(
    channel: str,
    at: float,
    *,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
    environ: Mapping[str, str] = os.environ,
) -> LinkState:
    """Run ``ip -j -d -s link show <channel>`` and return the parsed state (CAN-27, CAN-29).

    ``ip`` is resolved from ``PATH`` followed by :data:`EXTRA_PATH` and run without a shell,
    with ``LC_ALL=C`` and a :data:`IP_TIMEOUT_S` timeout. A non-zero exit whose stderr says
    ``does not exist`` gives ``ABSENT``; a missing binary, any other failure or a timeout gives
    ``UNKNOWN``. Never raises.
    """
    search = os.pathsep.join(part for part in (environ.get("PATH", ""), *EXTRA_PATH) if part)
    ip = which("ip", path=search)
    if ip is None:
        return unknown_link_state(at)
    try:
        result = runner(
            [ip, "-j", "-d", "-s", "link", "show", channel],
            capture_output=True,
            text=True,
            timeout=IP_TIMEOUT_S,
            env={**environ, "LC_ALL": "C"},
            check=False,
        )
    except OSError, ValueError, subprocess.SubprocessError:
        return unknown_link_state(at)
    if result.returncode != 0:
        if "does not exist" in (result.stderr or ""):
            return _bare(CanState.ABSENT, at)
        return unknown_link_state(at)
    return parse_ip_link(result.stdout or "", at)
