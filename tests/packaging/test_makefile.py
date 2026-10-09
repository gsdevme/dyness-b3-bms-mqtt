"""The root ``Makefile``'s install, uninstall and operational targets (INST-01..10, 12).

Targets run for real against a ``DESTDIR`` staging root, with every system command
(``apt-get``, ``curl``, ``uv``, ``systemctl``, ``udevadm``, ``systemd-run``, ``journalctl``)
replaced by a recording fake from ``tests.packaging.helpers``. The Makefile's test seams are
its command-line variables: ``EUID``, ``UNAME_M`` and ``OS_RELEASE`` stand in for the host,
``DESTDIR`` prefixes every path written, and an empty ``INSTALL_OWNER`` lets an unprivileged
user run ``install``. Dry runs (``make -n``) check the real paths and the order of steps.

``make`` is run with an environment built from scratch so a ``MAKEFLAGS`` inherited from
``make check`` cannot leak in. Every test skips without GNU make 3.82 or later.
"""

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.packaging.helpers import FAKE, PACKAGING, Sandbox, make_sandbox

ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("make")
SERVICE = "dyness-b3-bms-mqtt"
OPT = "/opt/dyness-b3-bms-mqtt"
VENV = f"{OPT}/.venv"
ENV_FILE = "/etc/default/dyness-b3-bms-mqtt"
UNIT = "/etc/systemd/system/dyness-b3-bms-mqtt.service"
UDEV_RULE = "/etc/udev/rules.d/80-dyness-b3-can.rules"
HELPERS = "/usr/local/lib/dyness-b3-bms-mqtt"
SYSTEM_FAKES = ("apt-get", "curl", "systemctl", "udevadm", "systemd-run", "journalctl")
EXTRA_TOOLS = ("install", "mkdir", "id", "uname", "env", "sh")
DEBIAN_12 = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n'
SYNC = [
    "uv",
    "sync",
    "--project",
    str(ROOT),
    "--frozen",
    "--no-dev",
    "--no-editable",
    "--compile-bytecode",
    "--python",
    "3.14",
    "--reinstall-package",
    SERVICE,
]
CHECK_CONFIG = [
    "systemd-run",
    "--quiet",
    "--pipe",
    "--wait",
    "--collect",
    "-p",
    "DynamicUser=yes",
    "-p",
    f"EnvironmentFile=-{ENV_FILE}",
    f"{VENV}/bin/dyness-b3-bms-mqtt",
    "--check-config",
]
INSTALL_CALLS = [
    ["apt-get", "update"],
    [
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "ca-certificates",
        "curl",
        "iproute2",
        "usbutils",
    ],
    ["uv", "python", "install", "--no-bin", "3.14"],
    SYNC,
    ["udevadm", "control", "--reload"],
    ["systemctl", "daemon-reload"],
    CHECK_CONFIG,
    ["systemctl", "enable", SERVICE],
    ["systemctl", "restart", SERVICE],
    ["systemctl", "status", "--no-pager", SERVICE],
]
FAKE_UV = """#!/bin/sh
{
    printf '%s' "${0##*/}"
    for arg in "$@"; do
        printf '\\t%s' "$arg"
    done
    printf '\\n'
} >> "$FAKE_LOG"
printf '%s\\t%s\\t%s\\t%s\\t%s\\n' "$(umask)" "${UV_PYTHON_INSTALL_DIR-}" \\
    "${UV_PYTHON_PREFERENCE-}" "${UV_LINK_MODE-}" "${UV_PROJECT_ENVIRONMENT-}" >> "$FAKE_LOG.uv"
umask 077
case "$1" in
python)
    home="$UV_PYTHON_INSTALL_DIR/cpython-3.14"
    mkdir -p "$home/bin" "$home/lib/python3.14"
    printf '#!/bin/sh\\n' > "$home/bin/python3.14"
    chmod 0700 "$home/bin/python3.14"
    printf 'os\\n' > "$home/lib/python3.14/os.py"
    ;;
sync)
    package="$UV_PROJECT_ENVIRONMENT/lib/python3.14/site-packages/dyness_b3_bms_mqtt"
    mkdir -p "$UV_PROJECT_ENVIRONMENT/bin" "$package"
    printf '#!/bin/sh\\n' > "$UV_PROJECT_ENVIRONMENT/bin/dyness-b3-bms-mqtt"
    chmod 0700 "$UV_PROJECT_ENVIRONMENT/bin/dyness-b3-bms-mqtt"
    printf 'home\\n' > "$UV_PROJECT_ENVIRONMENT/pyvenv.cfg"
    printf '\\n' > "$package/__init__.py"
    ;;
esac
"""
FAILING_SYSTEMCTL = """#!/bin/sh
{
    printf '%s' "${0##*/}"
    for arg in "$@"; do
        printf '\\t%s' "$arg"
    done
    printf '\\n'
} >> "$FAKE_LOG"
case "$1" in
disable)
    echo "Failed to disable unit: Unit file dyness-b3-bms-mqtt.service does not exist." >&2
    exit 1
    ;;
clean)
    echo "Failed to clean unit dyness-b3-bms-mqtt.service: Unit not found." >&2
    exit 1
    ;;
esac
exit 0
"""
OPT_FILES = {
    f"{OPT}/python/cpython-3.14/bin/python3.14",
    f"{OPT}/python/cpython-3.14/lib/python3.14/os.py",
    f"{VENV}/bin/dyness-b3-bms-mqtt",
    f"{VENV}/lib/python3.14/site-packages/dyness_b3_bms_mqtt/__init__.py",
    f"{VENV}/pyvenv.cfg",
}
EXECUTABLES = {
    f"{OPT}/python/cpython-3.14/bin/python3.14",
    f"{VENV}/bin/dyness-b3-bms-mqtt",
}


def gnu_make_version() -> tuple[int, int] | None:
    """Return the ``(major, minor)`` version of the GNU make on ``PATH``, or None."""
    if MAKE is None:
        return None
    banner = subprocess.run(
        [MAKE, "--version"], capture_output=True, text=True, check=False, env={"LC_ALL": "C"}
    ).stdout
    match = re.match(r"GNU Make (\d+)\.(\d+)", banner)
    return (int(match[1]), int(match[2])) if match else None


_VERSION = gnu_make_version()

pytestmark = [
    pytest.mark.skipif(
        _VERSION is None or _VERSION < (3, 82), reason="needs GNU make 3.82 or later"
    ),
    pytest.mark.timeout(60),
]


@dataclass(frozen=True)
class System:
    """A fake Pi: a sandbox of fake commands, a ``DESTDIR`` root and its ``os-release``."""

    box: Sandbox
    dest: Path
    os_release: Path

    def path(self, absolute: str) -> Path:
        """Return where the system path ``absolute`` lives under ``DESTDIR``."""
        return self.dest / absolute.lstrip("/")

    def make(
        self,
        *targets: str,
        euid: int | None = 0,
        cwd: Path = ROOT,
        umask: int = 0o022,
        makefile: Path | None = None,
        **variables: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``make`` on the repository Makefile with the fake host settings.

        ``variables`` are extra command-line assignments, applied after (so overriding) the
        defaults ``EUID`` (left to the Makefile when ``euid`` is None), ``UNAME_M=aarch64``,
        ``OS_RELEASE``, ``DESTDIR`` and an empty ``INSTALL_OWNER``. Run from a ``cwd`` other
        than the checkout, the Makefile is selected with ``-C``; ``makefile`` runs another
        Makefile with ``-f`` instead.
        """
        assigned = {
            **({} if euid is None else {"EUID": str(euid)}),
            "UNAME_M": "aarch64",
            "OS_RELEASE": str(self.os_release),
            "DESTDIR": str(self.dest),
            "INSTALL_OWNER": "",
            **variables,
        }
        assert MAKE is not None
        return subprocess.run(
            [
                MAKE,
                "--no-print-directory",
                *(
                    ["-f", str(makefile)]
                    if makefile is not None
                    else []
                    if cwd == ROOT
                    else ["-C", str(ROOT)]
                ),
                *targets,
                *(f"{name}={value}" for name, value in assigned.items()),
            ],
            env=dict(self.box.env),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            umask=umask,
        )

    def files(self) -> set[str]:
        """Return every non-directory under ``DESTDIR`` as an absolute system path."""
        if not self.dest.exists():
            return set()
        return {
            "/" + str(path.relative_to(self.dest))
            for path in self.dest.rglob("*")
            if not path.is_dir() or path.is_symlink()
        }

    def uv_env(self) -> list[list[str]]:
        """Return umask and uv variables seen by each fake ``uv`` call, in call order."""
        log = Path(f"{self.box.log}.uv")
        if not log.exists():
            return []
        return [line.split("\t") for line in log.read_text().splitlines()]


@pytest.fixture
def system(tmp_path: Path) -> Iterator[System]:
    """Return a fake Debian 12 aarch64 system with an empty ``DESTDIR`` with a space in it."""
    box = make_sandbox(tmp_path, fakes=SYSTEM_FAKES)
    (box.bin / "uv").write_text(FAKE_UV)
    (box.bin / "uv").chmod(0o755)
    tools = Path(box.env["PATH"].split(os.pathsep)[1])
    for tool in EXTRA_TOOLS:
        found = shutil.which(tool)
        assert found is not None, tool
        (tools / tool).symlink_to(found)
    os_release = tmp_path / "os-release"
    os_release.write_text(DEBIAN_12)
    yield System(box=box, dest=tmp_path / "dest dir", os_release=os_release)


def dry_run(*args: str, cwd: Path = ROOT) -> list[str]:
    """Return the lines ``make -n`` prints for ``args``, run with a scratch environment."""
    assert MAKE is not None
    result = subprocess.run(
        [MAKE, "--no-print-directory", "-n", *args],
        env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def positions(lines: list[str], fragments: list[str]) -> list[int]:
    """Return the index of the first line containing each fragment, searching in order."""
    found: list[int] = []
    start = 0
    for fragment in fragments:
        index = next(
            (n for n in range(start, len(lines)) if fragment in lines[n]),
            None,
        )
        assert index is not None, f"{fragment!r} not found in order in:\n" + "\n".join(lines)
        found.append(index)
        start = index + 1
    return found


def mode(path: Path) -> int:
    """Return the permission bits of ``path``."""
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.mark.req("INST-02")
@pytest.mark.parametrize("target", ["install", "uninstall", "restart", "check-config"])
def test_requires_root(system: System, target: str) -> None:
    result = system.make(target, euid=1000)
    assert result.returncode != 0
    assert "run with sudo" in result.stderr
    assert system.box.calls() == []
    assert system.files() == set()

    if os.geteuid() != 0:
        default = system.make(target, euid=None)
        assert default.returncode != 0
        assert "run with sudo" in default.stderr
        assert system.box.calls() == []


@pytest.mark.req("INST-01")
@pytest.mark.parametrize(
    ("arch", "os_release", "message"),
    [
        ("x86_64", DEBIAN_12, "aarch64"),
        ("armv7l", DEBIAN_12, "aarch64"),
        ("aarch64", 'ID=debian\nVERSION_ID="11"\n', "Debian 12"),
        ("aarch64", "ID=raspbian\nID_LIKE=debian\nVERSION_ID=11\n", "Debian 12"),
        ("aarch64", 'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n', "Debian 12"),
        ("aarch64", 'ID=fedora\nVERSION_ID="40"\n', "Debian"),
        ("aarch64", 'ID=debian\nPRETTY_NAME="Debian trixie/sid"\n', "Debian 12"),
        ("aarch64", None, "os-release"),
    ],
    ids=[
        "x86_64",
        "armv7l",
        "debian-11",
        "raspbian-11",
        "ubuntu-24.04",
        "fedora",
        "no-version-id",
        "no-os-release",
    ],
)
def test_preflight_rejects_arch_and_old_debian(
    system: System, arch: str, os_release: str | None, message: str
) -> None:
    if os_release is None:
        system.os_release.unlink()
    else:
        system.os_release.write_text(os_release)
    result = system.make("install", UNAME_M=arch)
    assert result.returncode != 0
    assert message in result.stderr
    assert system.box.calls() == []
    assert system.files() == set()


@pytest.mark.req("INST-01")
@pytest.mark.parametrize(
    "os_release",
    [
        DEBIAN_12,
        "ID=debian\nVERSION_ID=13\n",
        "ID=raspbian\nID_LIKE=debian\nVERSION_ID='12'\n",
        'ID=something\nID_LIKE="ubuntu debian"\nVERSION_ID="12"\n',
    ],
    ids=["debian-12", "debian-13", "raspbian-12", "id-like-debian-12"],
)
def test_preflight_accepts_debian_12_or_later(system: System, os_release: str) -> None:
    system.os_release.write_text(os_release)
    result = system.make("preflight")
    assert result.returncode == 0, result.stderr


@pytest.mark.req("INST-04", "INST-05", "INST-06")
def test_install_dry_run_order_and_flags() -> None:
    lines = dry_run("install")
    uv_env = (
        f'UV_PYTHON_INSTALL_DIR="{OPT}/python" UV_PYTHON_PREFERENCE=only-managed UV_LINK_MODE=copy'
    )
    steps = [
        "run with sudo",
        "aarch64",
        "os-release",
        "apt-get update",
        "apt-get install -y --no-install-recommends ca-certificates curl iproute2 usbutils",
        "curl -LsSf https://astral.sh/uv/install.sh"
        ' | env UV_INSTALL_DIR="/usr/local/bin" UV_NO_MODIFY_PATH=1 sh',
        f"umask 022 && {uv_env} uv python install --no-bin 3.14",
        f'umask 022 && {uv_env} UV_PROJECT_ENVIRONMENT="{VENV}" uv sync --project "{ROOT}" '
        + " ".join(SYNC[4:]),
        f'chmod -R a+rX "{OPT}"',
        f'install -D -m 0755 -o root -g root "{PACKAGING}/can-up" "{HELPERS}/can-up"',
        f'install -D -m 0755 -o root -g root "{PACKAGING}/recover" "{HELPERS}/recover"',
        f'install -D -m 0644 -o root -g root "{PACKAGING}/{SERVICE}.service" "{UNIT}"',
        f'install -D -m 0644 -o root -g root "{PACKAGING}/80-dyness-b3-can.rules" "{UDEV_RULE}"',
        "udevadm control --reload",
        f'install -D -m 0600 -o root -g root "{PACKAGING}/{SERVICE}.env" "{ENV_FILE}"',
        "systemctl daemon-reload",
        " ".join(CHECK_CONFIG),
        f"systemctl enable {SERVICE}",
        f"systemctl restart {SERVICE}",
        f"systemctl status --no-pager {SERVICE}",
        "sudo make restart",
    ]
    positions(lines, steps)

    env_line = next(line for line in lines if f"{SERVICE}.env" in line)
    assert env_line.index(f'[ -e "{ENV_FILE}" ]') < env_line.index("install -D -m 0600")
    assert "kept" in env_line
    assert "command -v uv" in next(line for line in lines if "astral.sh" in line)

    uv_calls = [line for line in lines if re.search(r"\buv (python|sync)\b", line)]
    assert len(uv_calls) == 2
    for line in uv_calls:
        assert line.startswith(f"umask 022 && {uv_env} "), line

    assert not [line for line in lines if re.search(r"(^|[\s\"])packaging/", line)]


@pytest.mark.req("INST-04")
@pytest.mark.parametrize("target", ["install", "uninstall", "check-config", "dev", "check"])
def test_make_dash_C_equivalent(tmp_path: Path, target: str) -> None:
    inside = dry_run(target)
    assert dry_run("-C", str(ROOT), target, cwd=tmp_path) == inside
    assert dry_run("-f", str(ROOT / "Makefile"), target, cwd=tmp_path) == inside


@pytest.mark.req("INST-04")
def test_make_dash_C_installs_the_same_files(system: System, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = system.make("install", cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert system.path(f"{HELPERS}/can-up").read_bytes() == (PACKAGING / "can-up").read_bytes()
    assert list(elsewhere.iterdir()) == []


@pytest.mark.req("INST-03", "INST-05", "INST-06", "INST-12")
def test_install_into_destdir_with_fakes(system: System) -> None:
    result = system.make("install", umask=0o077)
    assert result.returncode == 0, result.stderr

    assert system.box.calls() == INSTALL_CALLS
    python_dir = str(system.path(f"{OPT}/python"))
    assert system.uv_env() == [
        ["0022", python_dir, "only-managed", "copy", ""],
        ["0022", python_dir, "only-managed", "copy", str(system.path(VENV))],
    ]

    assert system.files() == {
        f"{HELPERS}/can-up",
        f"{HELPERS}/recover",
        ENV_FILE,
        UNIT,
        UDEV_RULE,
        *OPT_FILES,
    }
    sources = {
        f"{HELPERS}/can-up": ("can-up", 0o755),
        f"{HELPERS}/recover": ("recover", 0o755),
        UNIT: (f"{SERVICE}.service", 0o644),
        UDEV_RULE: ("80-dyness-b3-can.rules", 0o644),
        ENV_FILE: (f"{SERVICE}.env", 0o600),
    }
    for target, (source, expected) in sources.items():
        assert system.path(target).read_bytes() == (PACKAGING / source).read_bytes(), target
        assert mode(system.path(target)) == expected, target

    opt = system.path(OPT)
    for path in [opt, *opt.rglob("*")]:
        system_path = "/" + str(path.relative_to(system.dest))
        expected = 0o755 if path.is_dir() or system_path in EXECUTABLES else 0o644
        assert mode(path) == expected, system_path

    assert ENV_FILE in result.stdout
    assert "sudo make restart" in result.stdout


@pytest.mark.req("INST-06")
def test_install_fetches_uv_only_when_missing(system: System) -> None:
    (system.box.bin / "uv").unlink()
    result = system.make("install")
    assert result.returncode != 0
    assert system.box.calls()[:3] == [
        INSTALL_CALLS[0],
        INSTALL_CALLS[1],
        ["curl", "-LsSf", "https://astral.sh/uv/install.sh"],
    ]


@pytest.mark.req("INST-06", "INST-07", "CFG-08")
def test_reinstall_keeps_env_file_byte_for_byte(system: System) -> None:
    first = system.make("install")
    assert first.returncode == 0, first.stderr

    env_file = system.path(ENV_FILE)
    edited = b"MQTT_HOST=broker.lan\nMQTT_PASSWORD='p$w \"d'\n# no trailing newline"
    env_file.write_bytes(edited)
    env_file.chmod(0o640)
    state = system.path("/var/lib/private/dyness-b3-bms-mqtt/recovery.json")
    state.parent.mkdir(parents=True)
    state.write_text("{}\n")
    stale = system.path(f"{HELPERS}/recover")
    stale.write_text("old\n")

    second = system.make("install")
    assert second.returncode == 0, second.stderr
    assert env_file.read_bytes() == edited
    assert mode(env_file) == 0o640
    assert state.read_text() == "{}\n"
    assert stale.read_bytes() == (PACKAGING / "recover").read_bytes()
    assert f"kept existing {ENV_FILE}" in second.stdout
    assert system.box.calls() == INSTALL_CALLS + INSTALL_CALLS


@pytest.mark.req("INST-07", "CFG-08")
@pytest.mark.parametrize("dangling", [False, True], ids=["to-file", "dangling"])
def test_reinstall_keeps_env_symlink(system: System, tmp_path: Path, dangling: bool) -> None:
    target = tmp_path / "settings.env"
    if not dangling:
        target.write_text("MQTT_HOST=broker.lan\n")
    link = system.path(ENV_FILE)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    result = system.make("install")
    assert result.returncode == 0, result.stderr
    assert link.is_symlink()
    assert link.readlink() == target
    assert target.exists() is not dangling
    if not dangling:
        assert target.read_text() == "MQTT_HOST=broker.lan\n"
    assert f"kept existing {ENV_FILE}" in result.stdout


@pytest.mark.req("INST-08")
@pytest.mark.parametrize("purge", ["0", "1"])
def test_uninstall_on_empty_system_succeeds(system: System, purge: str) -> None:
    (system.box.bin / "systemctl").write_text(FAILING_SYSTEMCTL)
    result = system.make("uninstall", PURGE=purge)
    assert result.returncode == 0, result.stderr
    assert system.files() == set()
    calls = system.box.calls()
    assert calls[0] == ["systemctl", "disable", "--now", SERVICE]
    assert ["systemctl", "daemon-reload"] in calls
    assert ["udevadm", "control", "--reload"] in calls


@pytest.mark.req("INST-08")
@pytest.mark.parametrize("purge", ["0", "1"])
def test_uninstall_purge(system: System, purge: str) -> None:
    assert system.make("install").returncode == 0
    uv = system.path("/usr/local/bin/uv")
    uv.parent.mkdir(parents=True)
    uv.write_text(FAKE)
    states = [
        system.path("/var/lib/dyness-b3-bms-mqtt"),
        system.path("/var/lib/private/dyness-b3-bms-mqtt"),
    ]
    for state in states:
        state.mkdir(parents=True, exist_ok=True)
        (state / "recovery.json").write_text("{}\n")
    log = system.box.log
    log.unlink()

    result = system.make("uninstall", PURGE=purge)
    assert result.returncode == 0, result.stderr

    calls = system.box.calls()
    expected = [["systemctl", "disable", "--now", SERVICE]]
    if purge == "1":
        expected.append(["systemctl", "clean", "--what=state", SERVICE])
    expected += [["systemctl", "daemon-reload"], ["udevadm", "control", "--reload"]]
    assert calls == expected

    kept = {"/usr/local/bin/uv"}
    if purge == "0":
        kept |= {ENV_FILE, *(f"/{s.relative_to(system.dest)}/recovery.json" for s in states)}
    assert system.files() == kept
    for gone in (OPT, HELPERS):
        assert not system.path(gone).exists()

    positions(dry_run("uninstall", "PURGE=1"), ["systemctl clean --what=state", f'rm -f "{UNIT}"'])


@pytest.mark.req("INST-03", "INST-08")
@pytest.mark.parametrize("assignment", ["SERVICE=", "OPT_DIR=/opt", "STATE_DIRS=/var/lib"])
def test_fixed_paths_cannot_be_overridden(system: System, assignment: str) -> None:
    name = assignment.split("=")[0]
    assert MAKE is not None
    dry = subprocess.run(
        [MAKE, "--no-print-directory", "-n", "uninstall", "PURGE=1", assignment],
        env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode != 0
    assert dry.stdout == ""
    assert f"{name} is fixed by the Makefile and cannot be set on the command line" in dry.stderr

    assert system.make("install").returncode == 0
    before = system.files()
    system.box.log.unlink()
    result = system.make("uninstall", assignment, PURGE="1")
    assert result.returncode != 0
    assert f"{name} is fixed" in result.stderr
    assert system.box.calls() == []
    assert system.files() == before


@pytest.mark.req("INST-03", "INST-08")
@pytest.mark.parametrize(
    ("line", "replacement"),
    [
        ("override OPT_DIR := /opt/$(SERVICE)", "override OPT_DIR := /opt"),
        ("override SERVICE := dyness-b3-bms-mqtt", "override SERVICE :="),
        (
            "override HELPER_DIR := /usr/local/lib/$(SERVICE)",
            "override HELPER_DIR := /usr/../dyness-b3-bms-mqtt",
        ),
        (
            "override UNIT_FILE := /etc/systemd/system/$(SERVICE).service",
            "override UNIT_FILE := /etc/systemd/system",
        ),
    ],
    ids=["opt", "empty-service", "dot-dot", "unit-dir"],
)
def test_uninstall_guard_refuses_unexpected_paths(
    system: System, tmp_path: Path, line: str, replacement: str
) -> None:
    text = (ROOT / "Makefile").read_text()
    assert text.count(line) == 1
    edited = tmp_path / "Makefile"
    edited.write_text(text.replace(line, replacement))
    assert system.make("install").returncode == 0
    before = system.files()
    system.box.log.unlink()

    result = system.make("uninstall", PURGE="1", makefile=edited)
    assert result.returncode != 0
    assert "refusing to uninstall: unexpected path" in result.stderr
    assert system.box.calls() == []
    assert system.files() == before


@pytest.mark.req("INST-08")
def test_uninstall_rejects_unknown_purge_value(system: System) -> None:
    assert system.make("install").returncode == 0
    log = system.box.log
    log.unlink()
    result = system.make("uninstall", PURGE="yes")
    assert result.returncode != 0
    assert "PURGE" in result.stderr
    assert system.box.calls() == []
    assert system.path(UNIT).exists()


@pytest.mark.req("INST-09")
def test_operational_targets(system: System) -> None:
    system.box.script("systemctl", stdout="inactive\n", status=3)
    status = system.make("status", euid=1000)
    assert status.returncode == 0, status.stderr
    assert "inactive" in status.stdout

    logs = system.make("logs", euid=1000)
    assert logs.returncode == 0, logs.stderr

    system.box.script("systemctl")
    restart = system.make("restart")
    assert restart.returncode == 0, restart.stderr

    assert system.box.calls() == [
        ["systemctl", "status", "--no-pager", SERVICE],
        ["journalctl", "-u", SERVICE, "-f", "-n", "100"],
        ["systemctl", "restart", SERVICE],
    ]
    assert system.files() == set()

    assert [line for line in dry_run("dev") if "sync" in line] == [
        f"uv --directory {ROOT} sync --all-extras"
    ]


@pytest.mark.req("INST-06", "INST-09", "INST-10")
def test_check_config_uses_systemd_parser(system: System) -> None:
    ok = system.make("check-config")
    assert ok.returncode == 0, ok.stderr
    assert system.box.calls() == [CHECK_CONFIG]

    system.box.log.unlink()
    system.box.script("systemd-run", stderr="ERROR MQTT_PORT: not an integer\n", status=78)
    bad = system.make("check-config")
    assert bad.returncode != 0
    assert "MQTT_PORT: not an integer" in bad.stderr
    assert "Error 78" in bad.stderr

    system.box.log.unlink()
    install = system.make("install")
    assert install.returncode != 0
    assert system.box.calls() == INSTALL_CALLS[: INSTALL_CALLS.index(CHECK_CONFIG) + 1]

    sourced = [
        argument
        for line in (ROOT / "Makefile").read_text().splitlines()
        if line.startswith("\t")
        for argument in re.findall(
            r"(?:^\t[@+-]*|[;&|({]|\bthen|\bdo)\s*(?:\.|source)\s+(\S+)", line
        )
    ]
    assert sourced
    assert all("OS_RELEASE" in argument for argument in sourced), sourced
