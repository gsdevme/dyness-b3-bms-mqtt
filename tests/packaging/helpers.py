"""Run the root helpers ``can-up`` and ``recover`` unprivileged in a fake system (TEST-19).

A sandbox is a temporary root holding ``sys/``, ``dev/``, an ``etc/default`` directory for
the environment file and a state directory, selected by the helpers' test seams
``SYSFS_ROOT``, ``DEV_ROOT``, ``ENV_FILE`` and ``STATE_DIRECTORY``. External commands are
fakes in a ``bin`` directory that comes first on ``PATH``: each fake appends its argv to
``FAKE_LOG`` (one line per call, fields separated by tabs) and replays the scripted
``<name>.stdout``, ``<name>.stderr`` and ``<name>.status`` files next to it.

The rest of ``PATH`` is a ``tools`` directory of links to only the ordinary utilities the
helpers use, so a real ``ip`` or ``usbreset`` installed on the machine can never run and a
helper that starts depending on a new tool fails loudly. The helpers' environment is built
from scratch, so the developer's own ``CAN_*`` variables never leak in.

Helpers are run with ``subprocess.run`` and no timeout: a timeout makes ``Popen.wait`` poll
with ``time.sleep``, which the suite forbids outside integration tests (TEST-11). Test
modules bound the run time with a pytest-timeout marker instead.
"""

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"
TOOLS = (
    "awk",
    "basename",
    "cat",
    "chmod",
    "date",
    "dirname",
    "grep",
    "head",
    "mktemp",
    "mv",
    "printenv",
    "readlink",
    "rm",
    "sed",
    "tail",
    "tr",
)
USB_DEVICES = "devices/platform/soc/3f980000.usb/usb1"
FAKE = """#!/bin/sh
here=${0%/*}
name=${0##*/}
{
    printf '%s' "$name"
    for arg in "$@"; do
        printf '\\t%s' "$arg"
    done
    printf '\\n'
} >> "$FAKE_LOG"
if [ -f "$here/$name.stdout" ]; then cat "$here/$name.stdout"; fi
if [ -f "$here/$name.stderr" ]; then cat "$here/$name.stderr" >&2; fi
if [ -f "$here/$name.status" ]; then exit "$(cat "$here/$name.status")"; fi
exit 0
"""


def ip_details(
    *, up: bool = True, bitrate: int = 500000, restart_ms: int = 100, listen_only: bool = False
) -> str:
    """Return ``ip -details link show dev can0`` output for a gs_usb link in the given state."""
    flags = "NOARP,UP,LOWER_UP,ECHO" if up else "NOARP,ECHO"
    state = "UP" if up else "DOWN"
    modes = "<LISTEN-ONLY,BERR-REPORTING> " if listen_only else ""
    return (
        f"3: can0: <{flags}> mtu 16 qdisc pfifo_fast state {state} mode DEFAULT group default"
        " qlen 10\n"
        "    link/can  promiscuity 0 allmulti 0 minmtu 0 maxmtu 0 \n"
        f"    can {modes}state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms {restart_ms} \n"
        f"\t  bitrate {bitrate} sample-point 0.875\n"
        "\t  tq 125 prop-seg 6 phase-seg1 7 phase-seg2 2 sjw 1 brp 6\n"
        "\t  gs_usb: tseg1 1..16 tseg2 1..8 sjw 1..4 brp 1..1024 brp_inc 1\n"
        "\t  clock 48000000 \n"
    )


@dataclass(frozen=True)
class Sandbox:
    """A fake system root, its fake-command directory, the fake call log and the helper env."""

    root: Path
    bin: Path
    log: Path
    env: Mapping[str, str]

    @property
    def sysfs(self) -> Path:
        """Return the fake ``/sys`` (``SYSFS_ROOT``)."""
        return Path(self.env["SYSFS_ROOT"])

    @property
    def dev(self) -> Path:
        """Return the fake ``/dev`` (``DEV_ROOT``)."""
        return Path(self.env["DEV_ROOT"])

    @property
    def env_file(self) -> Path:
        """Return the environment file path (``ENV_FILE``); it does not exist until written."""
        return Path(self.env["ENV_FILE"])

    @property
    def state(self) -> Path:
        """Return the state directory (``STATE_DIRECTORY``)."""
        return Path(self.env["STATE_DIRECTORY"])

    def script(self, name: str, *, stdout: str = "", stderr: str = "", status: int = 0) -> None:
        """Script what the fake ``name`` prints and returns on every call."""
        (self.bin / f"{name}.stdout").write_text(stdout)
        (self.bin / f"{name}.stderr").write_text(stderr)
        (self.bin / f"{name}.status").write_text(f"{status}\n")

    def calls(self) -> list[list[str]]:
        """Return the argv of every fake call so far, command name first, in call order."""
        if not self.log.exists():
            return []
        return [line.split("\t") for line in self.log.read_text().splitlines()]

    def sleeps(self) -> list[float]:
        """Return the seconds passed to every fake ``sleep`` call so far."""
        return [float(call[1]) for call in self.calls() if call[0] == "sleep"]

    def run(self, helper: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run ``packaging/<helper>`` with ``args`` and the sandbox env overlaid with ``env``."""
        return subprocess.run(
            [str(PACKAGING / helper), *args],
            env={**self.env, **env},
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_shell(self, script: str) -> subprocess.CompletedProcess[str]:
        """Run ``script`` with ``/bin/sh -c`` in the sandbox env, to exercise the fakes."""
        return subprocess.run(
            ["/bin/sh", "-c", script],
            env=dict(self.env),
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

    def add_netdev(self, name: str = "can0") -> Path:
        """Create a bare ``/sys/class/net/<name>`` with no USB device behind it."""
        path = self.sysfs / "class" / "net" / name
        path.mkdir(parents=True)
        return path

    def add_usb_device(
        self,
        name: str = "1-1",
        *,
        usb_id: str = "1d50:606f",
        busnum: int = 1,
        devnum: int = 4,
        netdev: str | None = "can0",
        driver: str = "gs_usb",
    ) -> Path:
        """Create a USB device ``name`` with one interface ``<name>:1.0`` bound to ``driver``.

        The layout follows the real sysfs: the device lives under ``devices/``, is linked from
        ``bus/usb/devices/``, and when ``netdev`` is given ``class/net/<netdev>`` links to the
        interface's ``net/<netdev>`` directory, whose ``device`` link points back at the
        interface. Returns the device directory.
        """
        vendor, product = usb_id.split(":")
        device = self.sysfs / USB_DEVICES / name
        interface = device / f"{name}:1.0"
        interface.mkdir(parents=True)
        for attribute, value in (
            ("idVendor", vendor),
            ("idProduct", product),
            ("busnum", str(busnum)),
            ("devnum", str(devnum)),
            ("authorized", "1"),
        ):
            (device / attribute).write_text(f"{value}\n")
        drivers = self.sysfs / "bus" / "usb" / "drivers" / driver
        drivers.mkdir(parents=True, exist_ok=True)
        for control in ("bind", "unbind"):
            (drivers / control).touch()
        (interface / "driver").symlink_to(drivers)
        links = self.sysfs / "bus" / "usb" / "devices"
        links.mkdir(parents=True, exist_ok=True)
        (links / name).symlink_to(device)
        (links / interface.name).symlink_to(interface)
        node = self.dev / "bus" / "usb" / f"{busnum:03d}" / f"{devnum:03d}"
        node.parent.mkdir(parents=True, exist_ok=True)
        node.touch()
        if netdev is not None:
            net = interface / "net" / netdev
            net.mkdir(parents=True)
            (net / "device").symlink_to(interface)
            classes = self.sysfs / "class" / "net"
            classes.mkdir(parents=True, exist_ok=True)
            (classes / netdev).symlink_to(net)
        return device


def make_sandbox(tmp_path: Path, fakes: tuple[str, ...] = ("ip", "usbreset", "sleep")) -> Sandbox:
    """Build a sandbox in ``tmp_path`` with the given fake commands, each exiting 0 silently."""
    root = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    tools = tmp_path / "tools"
    for directory in (root / "sys", root / "dev", root / "etc" / "default", bin_dir, tools):
        directory.mkdir(parents=True)
    state = root / "var" / "lib" / "dyness-b3-bms-mqtt"
    state.mkdir(parents=True)
    for fake in fakes:
        path = bin_dir / fake
        path.write_text(FAKE)
        path.chmod(0o755)
    for tool in TOOLS:
        found = shutil.which(tool)
        if found is not None:
            (tools / tool).symlink_to(found)
    log = tmp_path / "fake.log"
    env = {
        "PATH": os.pathsep.join((str(bin_dir), str(tools))),
        "FAKE_LOG": str(log),
        "SYSFS_ROOT": str(root / "sys"),
        "DEV_ROOT": str(root / "dev"),
        "ENV_FILE": str(root / "etc" / "default" / "dyness-b3-bms-mqtt"),
        "STATE_DIRECTORY": str(state),
        "LC_ALL": "C",
    }
    return Sandbox(root=root, bin=bin_dir, log=log, env=env)
