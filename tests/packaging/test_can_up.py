"""The ``can-up`` root helper, run against a fake ``/sys``, env file and ``ip`` (SVC-14)."""

import os
import re
import stat
from pathlib import Path

import pytest

from dyness_b3_bms_mqtt.config import DEFAULTS
from tests.packaging.helpers import PACKAGING, Sandbox, ip_details, make_sandbox
from tests.support.spec import spec_text, table_rows

pytestmark = pytest.mark.timeout(30)

SETTING = re.compile(r"^([A-Z_]+)=\$\(setting \1 (\S+)\)$", re.MULTILINE)
SHARED_FUNCTIONS = ("log", "unquote", "from_file", "setting")
HELPER_SETTINGS = {
    "can-up": ["CAN_INTERFACE", "CAN_CHANNEL", "CAN_BITRATE", "CAN_RESTART_MS", "CAN_LISTEN_ONLY"],
    "recover": ["CAN_INTERFACE", "CAN_CHANNEL", "CAN_USB_ID"],
}
SHOW = ["ip", "-details", "link", "show", "dev", "can0"]


def configure(
    channel: str = "can0", bitrate: str = "500000", restart_ms: str = "100", listen: str = "off"
) -> list[list[str]]:
    """Return the three ``ip`` calls of SVC-14 step 5 for the given settings."""
    return [
        ["ip", "link", "set", "dev", channel, "down"],
        [
            "ip",
            "link",
            "set",
            "dev",
            channel,
            "type",
            "can",
            "bitrate",
            bitrate,
            "restart-ms",
            restart_ms,
            "listen-only",
            listen,
        ],
        ["ip", "link", "set", "dev", channel, "up"],
    ]


def spec_defaults() -> dict[str, str]:
    """Return each ``08-configuration.md`` variable's default cell with code spans unwrapped."""
    rows = table_rows(spec_text("08-configuration.md").splitlines())
    return {cells[0].strip("`"): cells[2].strip("`") for cells in rows}


def shell_function(text: str, name: str) -> str:
    """Return the definition of shell function ``name`` in ``text``, up to its closing brace."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert match is not None, name
    return match.group()


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    """Return a sandbox with ``can0`` present and ``ip`` reporting it down."""
    box = make_sandbox(tmp_path)
    box.add_netdev()
    box.script("ip", stdout=ip_details(up=False))
    return box


@pytest.mark.req("TEST-19")
def test_sandbox_fakes_record_and_replay(tmp_path: Path) -> None:
    box = make_sandbox(tmp_path, fakes=("ip",))
    box.script("ip", stdout="out\n", stderr="err\n", status=3)
    assert box.calls() == []
    script = 'ip -details "two words"; echo "status $?"; command -v usbreset || echo none'
    result = box.run_shell(script)
    assert result.stdout == "out\nstatus 3\nnone\n"
    assert result.stderr == "err\n"
    assert box.calls() == [["ip", "-details", "two words"]]
    assert Path(box.env["SYSFS_ROOT"]).is_relative_to(box.root)
    assert Path(box.env["DEV_ROOT"]).is_relative_to(box.root)
    assert box.env_file.is_relative_to(box.root)
    assert not box.env_file.exists()
    assert box.state.is_dir()


@pytest.mark.req("SVC-14", "SVC-16")
def test_helpers_are_executable_posix_sh() -> None:
    for helper in HELPER_SETTINGS:
        path = PACKAGING / helper
        assert path.read_text().startswith("#!/bin/sh\n"), helper
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR and mode & stat.S_IXGRP and mode & stat.S_IXOTH, helper
        assert os.access(path, os.X_OK), helper


@pytest.mark.req("SVC-14", "SVC-16")
def test_can_up_defaults_equal_spec(sandbox: Sandbox) -> None:
    table = spec_defaults()
    scripts = {helper: (PACKAGING / helper).read_text() for helper in HELPER_SETTINGS}
    for helper, names in HELPER_SETTINGS.items():
        settings = SETTING.findall(scripts[helper])
        assert [name for name, _ in settings] == names, helper
        for name, default in settings:
            assert default == DEFAULTS[name] == table[name], (helper, name)
    for function in SHARED_FUNCTIONS:
        assert shell_function(scripts["can-up"], function) == shell_function(
            scripts["recover"], function
        ), function

    result = sandbox.run("can-up")
    assert result.returncode == 0
    assert sandbox.calls() == [SHOW, *configure()]


@pytest.mark.req("SVC-14")
def test_can_up_scope_and_absent(sandbox: Sandbox) -> None:
    for interface in ("gs_usb", "slcan", "virtual"):
        result = sandbox.run("can-up", CAN_INTERFACE=interface)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", ""), interface
    sandbox.env_file.write_text("CAN_INTERFACE=gs_usb\n")
    assert sandbox.run("can-up").returncode == 0
    assert sandbox.calls() == []
    sandbox.env_file.unlink()

    result = sandbox.run("can-up", "can1")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert sandbox.calls() == []

    result = sandbox.run("can-up", "can1", CAN_CHANNEL="can1")
    assert result.returncode == 0
    assert result.stderr == "can-up: can1 absent\n"
    assert sandbox.calls() == []

    result = sandbox.run("can-up", "can0")
    assert result.returncode == 0
    assert sandbox.calls() == [SHOW, *configure()]


@pytest.mark.req("SVC-14")
@pytest.mark.parametrize("listen_only", [False, True])
def test_can_up_idempotent_no_ip_set_when_correct(sandbox: Sandbox, listen_only: bool) -> None:
    sandbox.script("ip", stdout=ip_details(listen_only=listen_only))
    result = sandbox.run("can-up", CAN_LISTEN_ONLY=str(listen_only).lower())
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert sandbox.calls() == [SHOW]


@pytest.mark.req("SVC-14")
@pytest.mark.parametrize(
    ("details", "env", "expected"),
    [
        (ip_details(up=False), {}, configure()),
        (ip_details(bitrate=250000), {}, configure()),
        (ip_details(restart_ms=0), {}, configure()),
        (ip_details(listen_only=True), {}, configure()),
        (ip_details(), {"CAN_LISTEN_ONLY": "true"}, configure(listen="on")),
        (ip_details(), {"CAN_BITRATE": "250000"}, configure(bitrate="250000")),
        (ip_details(), {"CAN_RESTART_MS": "0"}, configure(restart_ms="0")),
        ("", {}, configure()),
    ],
    ids=[
        "down",
        "bitrate",
        "restart-ms",
        "listen-only-set",
        "listen-only-wanted",
        "env-bitrate",
        "env-restart-ms",
        "no-details",
    ],
)
def test_can_up_configures_in_order(
    sandbox: Sandbox, details: str, env: dict[str, str], expected: list[list[str]]
) -> None:
    sandbox.script("ip", stdout=details)
    result = sandbox.run("can-up", **env)
    assert result.returncode == 0
    assert sandbox.calls() == [SHOW, *expected]
    assert sandbox.sleeps() == []
    settings = " ".join(expected[1][7:])
    assert result.stderr == f"can-up: can0 up: {settings}\n"


@pytest.mark.req("SVC-14")
@pytest.mark.parametrize(
    ("value", "listen"),
    [
        ("true", "on"),
        ("TRUE", "on"),
        ("1", "on"),
        ("yes", "on"),
        ("On", "on"),
        ("false", "off"),
        ("0", "off"),
        ("no", "off"),
        ("OFF", "off"),
        ("", "off"),
    ],
)
def test_can_up_listen_only_on_off(sandbox: Sandbox, value: str, listen: str) -> None:
    assert sandbox.run("can-up", CAN_LISTEN_ONLY=value).returncode == 0
    from_env = sandbox.calls()
    sandbox.log.unlink()
    sandbox.env_file.write_text(f"CAN_LISTEN_ONLY={value}\n")
    assert sandbox.run("can-up").returncode == 0
    assert sandbox.calls() == from_env == [SHOW, *configure(listen=listen)]


@pytest.mark.req("SVC-14")
def test_can_up_env_file_not_executed(sandbox: Sandbox, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    sandbox.add_netdev("can1")
    sandbox.env_file.write_text(
        "# example\n"
        f"MQTT_PASSWORD=$(touch {marker})`touch {marker}`;touch {marker}\n"
        f"MQTT_USERNAME='x' && touch {marker}\n"
        f"touch {marker}\n"
        f". {marker}\n"
        'CAN_CHANNEL="can1"\n'
        "CAN_BITRATE='250000'\n"
        "  CAN_RESTART_MS = 200  \n"
        "CAN_LISTEN_ONLY=yes\n"
    )
    result = sandbox.run("can-up", CAN_RESTART_MS="300", CAN_LISTEN_ONLY="")
    assert result.returncode == 0
    assert not marker.exists()
    assert sandbox.calls() == [
        [*SHOW[:-1], "can1"],
        *configure(channel="can1", bitrate="250000", restart_ms="300", listen="on"),
    ]

    sandbox.env_file.write_text(f"CAN_CHANNEL=$(touch {marker})\n")
    result = sandbox.run("can-up")
    assert result.returncode == 0
    assert result.stderr == f"can-up: $(touch {marker}) absent\n"
    assert not marker.exists()


@pytest.mark.req("SVC-14")
@pytest.mark.parametrize(
    ("line", "bitrate"),
    [
        ("CAN_BITRATE=250000", "250000"),
        ('CAN_BITRATE="250000"', "250000"),
        ("CAN_BITRATE='250000'", "250000"),
        ("CAN_BITRATE=\"'250000'\"", "'250000'"),
        ("CAN_BITRATE=\"250000'", "\"250000'"),
        ('CAN_BITRATE="', '"'),
        ("CAN_BITRATE=", "500000"),
        ('CAN_BITRATE=""', "500000"),
        ("#CAN_BITRATE=250000", "500000"),
        ("XCAN_BITRATE=250000", "500000"),
        ("CAN_BITRATE=125000\nCAN_BITRATE=250000", "250000"),
    ],
)
def test_can_up_env_file_quotes(sandbox: Sandbox, line: str, bitrate: str) -> None:
    sandbox.env_file.write_text(f"{line}\n")
    assert sandbox.run("can-up").returncode == 0
    assert sandbox.calls() == [SHOW, *configure(bitrate=bitrate)]


@pytest.mark.req("SVC-14", "SVC-22")
def test_can_up_always_exit_0_and_prefix(sandbox: Sandbox) -> None:
    sandbox.script(
        "ip", stderr="RTNETLINK answers: Operation not permitted\nsecond line\n", status=2
    )
    result = sandbox.run("can-up")
    assert result.returncode == 0
    assert result.stdout == ""
    assert sandbox.calls() == [SHOW, *configure()]
    lines = result.stderr.splitlines()
    assert lines
    assert all(line.startswith("can-up: ") for line in lines), lines
    for call in [SHOW, *configure()]:
        assert any(" ".join(call) in line and "status 2" in line for line in lines), call
    assert lines.count("can-up: RTNETLINK answers: Operation not permitted") == 4
    assert lines.count("can-up: second line") == 4

    sandbox.script("ip", status=127)
    unwritable = sandbox.state / "missing" / "env"
    result = sandbox.run("can-up", ENV_FILE=str(unwritable))
    assert result.returncode == 0
