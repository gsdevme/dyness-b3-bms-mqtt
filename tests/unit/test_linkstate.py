"""Tests of the bus-state query: 02-can-transport.md section 7 (CAN-27..30).

Every ``ip`` outcome is a recorded fixture or a scripted runner result; no test runs the real
``ip`` binary.
"""

import copy
import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from dyness_b3_bms_mqtt.linkstate import (
    IP_TIMEOUT_S,
    CanState,
    LinkState,
    parse_ip_link,
    query_link_state,
    unknown_link_state,
)

AT = 1234.5

ERROR_ACTIVE_LINK: dict[str, Any] = {
    "ifindex": 3,
    "ifname": "can0",
    "flags": ["NOARP", "UP", "LOWER_UP", "ECHO"],
    "mtu": 16,
    "qdisc": "pfifo_fast",
    "operstate": "UP",
    "linkmode": "DEFAULT",
    "group": "default",
    "txqlen": 10,
    "link_type": "can",
    "promiscuity": 0,
    "linkinfo": {
        "info_kind": "can",
        "info_data": {
            "ctrlmode": ["LISTEN-ONLY"],
            "state": "ERROR-ACTIVE",
            "berr_counter": {"tx": 0, "rx": 3},
            "restart_ms": 100,
            "bittiming": {
                "bitrate": 500000,
                "sample_point": "0.875",
                "tq": 125,
                "prop_seg": 6,
                "phase_seg1": 7,
                "phase_seg2": 2,
                "sjw": 1,
                "brp": 6,
            },
            "clock": 48000000,
        },
        "info_xstats": {
            "restarts": 2,
            "bus_error": 0,
            "arbitration_lost": 0,
            "error_warning": 1,
            "error_passive": 0,
            "bus_off": 1,
        },
    },
    "num_tx_queues": 1,
    "num_rx_queues": 1,
    "parentbus": "usb",
    "parentdev": "1-1:1.0",
    "stats64": {
        "rx": {"bytes": 811040, "packets": 101380, "errors": 0, "dropped": 7, "over_errors": 0},
        "tx": {"bytes": 2400, "packets": 300, "errors": 0, "dropped": 0, "carrier_errors": 0},
    },
}


def link_json(**changes: Any) -> str:
    """Return ``ip -j`` output for the error-active link with ``info_data`` fields changed."""
    link = copy.deepcopy(ERROR_ACTIVE_LINK)
    for key, value in changes.items():
        if key == "flags":
            link["flags"] = value
        else:
            link["linkinfo"]["info_data"][key] = value
    return json.dumps([link])


def down_json() -> str:
    link = copy.deepcopy(ERROR_ACTIVE_LINK)
    link["flags"] = ["NOARP", "ECHO"]
    link["operstate"] = "DOWN"
    link["linkinfo"]["info_data"]["state"] = "STOPPED"
    return json.dumps([link])


class Runner:
    """A ``subprocess.run`` stand-in that records its calls and returns a scripted outcome."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        exc: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.exc = exc
        self.calls: list[tuple[Sequence[str], dict[str, Any]]] = []

    def __call__(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def which_ip(name: str, path: str | None = None) -> str | None:
    return "/usr/sbin/ip" if name == "ip" else None


def query(runner: Runner, environ: Mapping[str, str] | None = None) -> LinkState:
    return query_link_state(
        "can0", AT, runner=runner, which=which_ip, environ=environ or {"PATH": "/usr/bin"}
    )


STATE_FIXTURES = [
    (link_json(), CanState.ERROR_ACTIVE),
    (link_json(state="ERROR-WARNING"), CanState.ERROR_WARNING),
    (link_json(state="ERROR-PASSIVE"), CanState.ERROR_PASSIVE),
    (link_json(state="BUS-OFF"), CanState.BUS_OFF),
    (link_json(state="STOPPED"), CanState.STOPPED),
    (down_json(), CanState.DOWN),
    (link_json(state="SLEEPING"), CanState.UNKNOWN),
    ("not json", CanState.UNKNOWN),
    ("[]", CanState.UNKNOWN),
]


@pytest.mark.req("CAN-28", "CAN-29")
@pytest.mark.parametrize(("stdout", "state"), STATE_FIXTURES)
def test_each_state_from_fixture(stdout: str, state: CanState) -> None:
    result = query(Runner(stdout=stdout))
    assert result.state is state
    assert result.at == AT
    assert parse_ip_link(stdout, AT) == result


@pytest.mark.req("CAN-28")
def test_full_fixture_fields() -> None:
    assert parse_ip_link(link_json(), AT) == LinkState(
        state=CanState.ERROR_ACTIVE,
        operstate="UP",
        listen_only=True,
        bitrate=500000,
        berr_tx=0,
        berr_rx=3,
        restarts=2,
        bus_off=1,
        rx_dropped=7,
        at=AT,
    )
    down = parse_ip_link(down_json(), AT)
    assert (down.state, down.operstate, down.bitrate) == (CanState.DOWN, "DOWN", 500000)


@pytest.mark.req("CAN-28")
def test_published_names() -> None:
    assert [s.published for s in CanState] == [
        "error-active",
        "error-warning",
        "error-passive",
        "bus-off",
        "stopped",
        "down",
        "absent",
        None,
    ]
    assert str(CanState.ERROR_ACTIVE) == "error-active"


@pytest.mark.req("CAN-28")
def test_missing_ctrlmode_means_not_listen_only() -> None:
    link = copy.deepcopy(ERROR_ACTIVE_LINK)
    del link["linkinfo"]["info_data"]["ctrlmode"]
    assert parse_ip_link(json.dumps([link]), AT).listen_only is False
    assert parse_ip_link(link_json(ctrlmode=["ONE-SHOT"]), AT).listen_only is False
    assert parse_ip_link(link_json(ctrlmode=["LISTEN-ONLY", "BERR-REPORTING"]), AT).listen_only
    del link["linkinfo"]["info_data"]
    no_info = parse_ip_link(json.dumps([link]), AT)
    assert no_info.listen_only is None
    assert no_info.state is CanState.UNKNOWN
    assert parse_ip_link(link_json(ctrlmode="LISTEN-ONLY"), AT).listen_only is None


@pytest.mark.req("CAN-28")
def test_mistyped_fields_become_none_individually() -> None:
    link = copy.deepcopy(ERROR_ACTIVE_LINK)
    info = link["linkinfo"]["info_data"]
    info["bittiming"]["bitrate"] = "500000"
    info["berr_counter"]["tx"] = True
    link["linkinfo"]["info_xstats"]["bus_off"] = 1.5
    link["operstate"] = 5
    link["stats64"]["rx"] = []
    parsed = parse_ip_link(json.dumps([link]), AT)
    assert parsed == LinkState(
        state=CanState.ERROR_ACTIVE,
        operstate=None,
        listen_only=True,
        bitrate=None,
        berr_tx=None,
        berr_rx=3,
        restarts=2,
        bus_off=None,
        rx_dropped=None,
        at=AT,
    )
    bare = parse_ip_link(json.dumps([{"flags": ["UP"], "linkinfo": {"info_data": {}}}]), AT)
    assert bare == LinkState(CanState.UNKNOWN, None, False, None, None, None, None, None, None, AT)
    no_flags = parse_ip_link(json.dumps([{"flags": "UP", "operstate": "UP"}]), AT)
    assert (no_flags.state, no_flags.operstate) == (CanState.UNKNOWN, "UP")
    assert parse_ip_link(json.dumps({"ifname": "can0"}), AT) == unknown_link_state(AT)
    assert parse_ip_link(json.dumps([7]), AT) == unknown_link_state(AT)
    weird = parse_ip_link(link_json(state=7), AT)
    assert (weird.state, weird.bitrate) == (CanState.UNKNOWN, 500000)


@pytest.mark.req("CAN-29")
def test_absent_and_unknown_command_outcomes() -> None:
    absent = query(Runner(returncode=1, stderr='Device "can0" does not exist.\n'))
    assert absent == LinkState(CanState.ABSENT, None, None, None, None, None, None, None, None, AT)
    unknown = unknown_link_state(AT)
    assert query(Runner(returncode=255, stderr="RTNETLINK answers: Operation not permitted")) == (
        unknown
    )
    assert query(Runner(exc=subprocess.TimeoutExpired(["ip"], IP_TIMEOUT_S))) == unknown
    assert query(Runner(exc=FileNotFoundError(2, "No such file"))) == unknown
    assert query(Runner(exc=PermissionError(13, "denied"))) == unknown
    assert query(Runner(stdout="{truncated")) == unknown
    runner = Runner(stdout=link_json())
    missing = query_link_state(
        "can0", AT, runner=runner, which=lambda name, path=None: None, environ={}
    )
    assert missing == unknown
    assert runner.calls == []


@pytest.mark.req("CAN-27")
def test_command_is_argument_list_with_lc_all_and_timeout() -> None:
    seen: list[tuple[str, str | None]] = []

    def which(name: str, path: str | None = None) -> str | None:
        seen.append((name, path))
        return "/sbin/ip"

    runner = Runner(stdout=link_json())
    environ = {"PATH": "/usr/local/bin:/usr/bin", "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE"}
    query_link_state("can7", AT, runner=runner, which=which, environ=environ)
    assert seen == [("ip", "/usr/local/bin:/usr/bin:/usr/sbin:/sbin")]
    [(args, kwargs)] = runner.calls
    assert args == ["/sbin/ip", "-j", "-d", "-s", "link", "show", "can7"]
    assert IP_TIMEOUT_S == 2.0
    assert kwargs["timeout"] == IP_TIMEOUT_S
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["env"]["LANG"] == "de_DE.UTF-8"
    assert kwargs.get("shell", False) is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert environ["LC_ALL"] == "de_DE"
    assert "sudo" not in args
    seen.clear()
    query_link_state("can0", AT, runner=runner, which=which, environ={})
    assert seen == [("ip", "/usr/sbin:/sbin")]
