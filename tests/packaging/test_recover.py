"""The ``recover`` root helper and its USB reset, run in a sandbox (SVC-15..18, SVC-22)."""

import json
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dyness_b3_bms_mqtt.state_store import LastExit, StateStore
from tests.packaging.helpers import Sandbox, ip_details, make_sandbox

pytestmark = pytest.mark.timeout(30)

SHOW = ["ip", "-details", "link", "show", "dev", "can0"]
DOWN = ["ip", "link", "set", "dev", "can0", "down"]
CONFIGURE = [
    DOWN,
    [
        "ip",
        "link",
        "set",
        "dev",
        "can0",
        "type",
        "can",
        "bitrate",
        "500000",
        "restart-ms",
        "100",
        "listen-only",
        "off",
    ],
    ["ip", "link", "set", "dev", "can0", "up"],
]
CAN_UP = [SHOW, *CONFIGURE]
CAN_UP_FAILING = [SHOW, *CONFIGURE[:2], [*CONFIGURE[1][:9], *CONFIGURE[1][11:]], CONFIGURE[2]]
NETDEV_WAIT = [0.5] * 16
NETDEV_POLLS = [["sleep", "0.5"]] * len(NETDEV_WAIT)
AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PREFIXED = re.compile(r"^(recover|can-up): ")


def stderr_lines(text: str) -> list[str]:
    """Return the stderr lines, failing if any is not prefixed with a helper's name."""
    lines = text.splitlines()
    assert all(PREFIXED.match(line) for line in lines), lines
    return lines


def in_order(lines: list[str], *fragments: str) -> None:
    """Assert that each fragment appears in a log line after the previous fragment's line."""
    position = 0
    for fragment in fragments:
        position = next(
            (i for i in range(position, len(lines)) if fragment in lines[i]), len(lines)
        )
        assert position < len(lines), (fragment, lines)
        position += 1


def usbreset_call(busnum: int = 1, devnum: int = 4) -> list[str]:
    """Return the expected ``usbreset`` argv: usbutils takes ``BBB/DDD``, not a device node."""
    return ["usbreset", f"{busnum:03d}/{devnum:03d}"]


def last_exit(box: Sandbox) -> dict[str, object]:
    """Return the decoded ``last_exit.json`` after checking its mode and that it is alone."""
    path = box.state / "last_exit.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert sorted(p.name for p in box.state.iterdir()) == ["last_exit.json"]
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    """Return a sandbox with a gs_usb adapter 1-1 (bus 1, device 4) providing ``can0``."""
    box = make_sandbox(tmp_path)
    box.add_usb_device()
    box.script("ip", stdout=ip_details(up=False))
    return box


@pytest.mark.req("SVC-15")
@pytest.mark.parametrize(
    ("env", "code", "result"),
    [
        ({"EXIT_STATUS": "76", "SERVICE_RESULT": "exit-code"}, 76, "exit-code"),
        ({"EXIT_STATUS": "0", "SERVICE_RESULT": "success"}, 0, "success"),
        ({"EXIT_STATUS": "075"}, 75, None),
        ({"EXIT_STATUS": "ABRT", "SERVICE_RESULT": "watchdog"}, "ABRT", "watchdog"),
        ({}, None, None),
        ({"EXIT_STATUS": "", "SERVICE_RESULT": ""}, None, None),
        ({"EXIT_STATUS": 'x"}, "y": "\\\n', "SERVICE_RESULT": 'a"b\\c d'}, "xy", "abcd"),
    ],
)
def test_recover_writes_last_exit(
    sandbox: Sandbox, env: dict[str, str], code: int | str | None, result: str | None
) -> None:
    (sandbox.state / "last_exit.json").write_text("stale")
    before = datetime.now(UTC).replace(microsecond=0)
    run = sandbox.run("recover", CAN_INTERFACE="gs_usb", **env)
    after = datetime.now(UTC)
    assert run.returncode == 0
    data = last_exit(sandbox)
    assert list(data) == ["code", "result", "at"]
    assert data["code"] == code and type(data["code"]) is type(code)
    assert data["result"] == result
    assert isinstance(data["at"], str) and AT.match(data["at"])
    at = datetime.strptime(data["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= at <= after
    assert stderr_lines(run.stderr)[0].startswith("recover: wrote ")
    store = StateStore(sandbox.state, wall=lambda: 0.0)
    assert store.load_last_exit() == LastExit(code=code, result=result)


@pytest.mark.req("SVC-15", "SVC-17")
def test_recover_last_exit_unwritable_still_recovers(sandbox: Sandbox) -> None:
    missing = sandbox.root / "missing"
    run = sandbox.run("recover", EXIT_STATUS="75", STATE_DIRECTORY=str(missing))
    assert run.returncode == 0
    assert not missing.exists()
    lines = stderr_lines(run.stderr)
    assert "cannot write" in lines[0]
    assert sandbox.calls() == [DOWN, *CAN_UP]


@pytest.mark.req("SVC-16", "SVC-17")
def test_recover_75_bounces_link(sandbox: Sandbox) -> None:
    run = sandbox.run("recover", EXIT_STATUS="75", SERVICE_RESULT="exit-code")
    assert run.returncode == 0
    assert sandbox.calls() == [DOWN, *CAN_UP]
    assert sandbox.sleeps() == []
    assert (sandbox.sysfs / "bus/usb/drivers/gs_usb/unbind").read_text() == ""
    in_order(stderr_lines(run.stderr), "wrote", "ip link set dev can0 down", "can-up")

    sandbox.log.unlink()
    sandbox.script("ip", stderr='Cannot find device "can0"\n', status=1)
    run = sandbox.run("recover", EXIT_STATUS="75")
    assert run.returncode == 0
    assert sandbox.calls() == [DOWN, *CAN_UP_FAILING]


@pytest.mark.req("SVC-16", "SVC-18")
@pytest.mark.parametrize("step", ["ioctl", "ip-down-fails", "deauthorise", "rebind"])
def test_recover_76_usb_reset_fallback_order(sandbox: Sandbox, step: str) -> None:
    device = sandbox.sysfs / "devices/platform/soc/3f980000.usb/usb1/1-1"
    drivers = sandbox.sysfs / "bus/usb/drivers/gs_usb"
    if step == "ip-down-fails":
        sandbox.script("ip", stderr="RTNETLINK answers: Operation not permitted\n", status=2)
    if step in ("deauthorise", "rebind"):
        sandbox.script("usbreset", stderr="usbreset: ioctl failed\n", status=1)
    if step == "rebind":
        (device / "authorized").unlink()
        (device / "authorized").mkdir()
    run = sandbox.run("recover", EXIT_STATUS="76", SERVICE_RESULT="exit-code")
    assert run.returncode == 0
    lines = stderr_lines(run.stderr)
    usbreset = usbreset_call()
    unbound = (drivers / "unbind").read_text(), (drivers / "bind").read_text()
    if step == "ip-down-fails":
        assert sandbox.calls() == [DOWN, usbreset, *CAN_UP_FAILING]
        assert (device / "authorized").read_text() == "1\n"
        assert unbound == ("", "")
    elif step == "ioctl":
        assert sandbox.calls() == [DOWN, usbreset, *CAN_UP]
        assert (device / "authorized").read_text() == "1\n"
        assert unbound == ("", "")
    elif step == "deauthorise":
        assert sandbox.calls() == [DOWN, usbreset, ["sleep", "2"], *CAN_UP]
        assert (device / "authorized").read_text() == "1"
        assert unbound == ("", "")
        in_order(lines, "usbreset", "wrote 0 to", "wrote 1 to", "can-up")
    else:
        assert sandbox.calls() == [DOWN, usbreset, *CAN_UP]
        assert unbound == ("1-1:1.0", "1-1:1.0")
        in_order(lines, "usbreset", "authorized", "unbind", "bind", "can-up")
    assert last_exit(sandbox)["code"] == 76


@pytest.mark.req("SVC-18")
@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no /usr/bin/python3")
def test_recover_76_python_ioctl_without_usbreset(tmp_path: Path) -> None:
    box = make_sandbox(tmp_path, fakes=("ip", "sleep"))
    box.add_usb_device()
    box.script("ip", stdout=ip_details(up=False))
    run = box.run("recover", EXIT_STATUS="76")
    assert run.returncode == 0
    assert box.calls() == [DOWN, ["sleep", "2"], *CAN_UP]
    in_order(
        stderr_lines(run.stderr),
        "/usr/bin/python3 -c import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_WRONLY); "
        f"fcntl.ioctl(fd, 0x5514, 0); os.close(fd) {box.dev}/bus/usb/001/004",
        "wrote 0 to",
    )


@pytest.mark.req("SVC-18")
@pytest.mark.parametrize(
    ("usb_id", "chosen", "others"),
    [(None, 5, ["1-3"]), ('"1D50:606F"', 5, ["1-3"]), ("1d6b:0002", 1, [])],
)
def test_recover_fallback_scan_by_usb_id(
    tmp_path: Path, usb_id: str | None, chosen: int, others: list[str]
) -> None:
    box = make_sandbox(tmp_path)
    box.add_usb_device("1-1", usb_id="1d6b:0002", devnum=1, netdev=None, driver="hub")
    box.add_usb_device("1-3", devnum=6, netdev=None)
    box.add_usb_device("1-2", devnum=5, netdev=None)
    if usb_id is not None:
        box.env_file.write_text(f"CAN_USB_ID={usb_id}\n")
    run = box.run("recover", EXIT_STATUS="76")
    assert run.returncode == 0
    assert box.calls() == [DOWN, usbreset_call(devnum=chosen), *NETDEV_POLLS]
    lines = stderr_lines(run.stderr)
    for other in others:
        assert any(other in line and "ignored" in line for line in lines), lines
    assert lines[-2:] == ["can-up: can0 absent", "recover: can-up finished with status 0"]


@pytest.mark.req("SVC-18")
def test_recover_device_not_found(tmp_path: Path) -> None:
    box = make_sandbox(tmp_path)
    box.add_usb_device("1-1", usb_id="1d6b:0002", devnum=1, netdev=None, driver="hub")
    run = box.run("recover", EXIT_STATUS="76")
    assert run.returncode == 0
    assert box.calls() == [DOWN, *NETDEV_POLLS]
    lines = stderr_lines(run.stderr)
    assert "recover: usb device not found" in lines
    in_order(lines, "usb device not found", "can0", "can-up: can0 absent")


@pytest.mark.req("SVC-16")
def test_recover_other_codes_and_non_socketcan_do_nothing(sandbox: Sandbox) -> None:
    for code in ("0", "1", "78", "ABRT", "TERM", "", "7", "750"):
        run = sandbox.run("recover", EXIT_STATUS=code, SERVICE_RESULT="exit-code")
        assert run.returncode == 0, code
        assert stderr_lines(run.stderr), code
    run = sandbox.run("recover")
    assert run.returncode == 0
    for interface in ("gs_usb", "slcan", "virtual"):
        for code in ("75", "76"):
            run = sandbox.run("recover", EXIT_STATUS=code, CAN_INTERFACE=interface)
            assert run.returncode == 0
            assert last_exit(sandbox)["code"] == int(code)
    sandbox.env_file.write_text("CAN_INTERFACE=virtual\n")
    assert sandbox.run("recover", EXIT_STATUS="76").returncode == 0
    assert sandbox.calls() == []


@pytest.mark.req("SVC-17", "SVC-18")
def test_recover_time_budget(tmp_path: Path) -> None:
    box = make_sandbox(tmp_path)
    device = box.add_usb_device(netdev=None)
    box.script("usbreset", status=1)
    run = box.run("recover", EXIT_STATUS="76")
    assert run.returncode == 0
    assert box.sleeps() == [2.0, *NETDEV_WAIT]
    assert sum(box.sleeps()) <= 10
    assert (device / "authorized").read_text() == "1"

    box.log.unlink()
    (device / "authorized").unlink()
    (device / "authorized").mkdir()
    drivers = box.sysfs / "bus/usb/drivers/gs_usb"
    (drivers / "unbind").unlink()
    (drivers / "unbind").mkdir()
    run = box.run("recover", EXIT_STATUS="76")
    assert run.returncode == 0
    assert box.sleeps() == NETDEV_WAIT
    in_order(stderr_lines(run.stderr), "usbreset", "authorized", "unbind", "usb reset failed")


@pytest.mark.req("SVC-22", "SVC-14", "SVC-17")
def test_helper_lines_prefixed(sandbox: Sandbox) -> None:
    sandbox.script("ip", stderr="first\n\nthird\n", status=1)
    sandbox.script("usbreset", stdout="Resetting ... failed\n", stderr="oops\n", status=1)
    runs = [
        sandbox.run("recover", EXIT_STATUS="75"),
        sandbox.run("recover", EXIT_STATUS="76"),
        sandbox.run("recover", EXIT_STATUS="1"),
        sandbox.run("recover"),
        sandbox.run("can-up"),
        sandbox.run("can-up", CAN_CHANNEL="can9"),
    ]
    for run in runs:
        assert run.returncode == 0
        assert run.stdout == ""
        assert stderr_lines(run.stderr)
    usb = stderr_lines(runs[1].stderr)
    assert "recover: Resetting ... failed" in usb
    assert "recover: oops" in usb
    assert "can-up: first" in usb and "can-up: third" in usb
