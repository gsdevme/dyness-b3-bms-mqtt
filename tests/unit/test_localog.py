"""The local CSV and events history and its housekeeping (``12-local-logging.md``).

Every test writes into a fresh state directory with a fake wall clock reading
2026-10-09T00:00:00Z. Disk faults are simulated by replacing the ``open`` and ``gzip_open``
that ``localog`` uses, so no test needs a full filesystem.
"""

import ast
import csv
import errno
import gzip
import json
import logging
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from dyness_b3_bms_mqtt import localog
from dyness_b3_bms_mqtt.alerts import Alert, AlertState
from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.discovery import LOCAL_LOG_OPTIONS
from dyness_b3_bms_mqtt.linkstate import CanState
from dyness_b3_bms_mqtt.localog import (
    MODULE_COLUMNS,
    STACK_COLUMNS,
    LocalLog,
    LocalLogStatus,
    day_name,
    housekeeping_plan,
)
from dyness_b3_bms_mqtt.payloads import (
    MODULE_KEYS,
    STACK_KEYS,
    ServiceInfo,
    module_payload,
    service_payload,
    stack_payload,
)
from dyness_b3_bms_mqtt.status import HealthState
from dyness_b3_bms_mqtt.telemetry import Snapshot, TelemetryStore
from tests.fakes.clock import FakeClock
from tests.fakes.disk import FullDisk
from tests.support.settings import make_settings
from tests.support.spec import requirement

DAY = 86400.0
SPEC = "12-local-logging.md"
TODAY = "2026-10-09"
FULL = OSError(errno.ENOSPC, "No space left on device")


def spec_columns(ident: str) -> tuple[str, ...]:
    """Return the column names listed in the code block of requirement ``ident``."""
    lines = requirement(SPEC, ident)
    fences = [i for i, line in enumerate(lines) if line.strip().startswith("```")]
    block = " ".join(lines[fences[0] + 1 : fences[1]])
    return tuple(name.strip() for name in block.split(",") if name.strip())


def sample(settings: Settings) -> tuple[Snapshot, AlertState]:
    """Return a snapshot with module 1 online, modules 2 and 3 offline, and its alerts.

    Module 1 and the stack carry a ``-0.0`` current and a ``-0.0`` temperature, which the
    files must show as ``0.0``.
    """
    empty = TelemetryStore(settings).snapshot(0.0, {})
    module_1 = replace(
        empty.modules[0],
        online=True,
        cells_fresh=True,
        bcast_age_s=0.4,
        cell_age_s=1.2,
        cells_mv=(3301,) * 14 + (3290,),
        cell_min_mv=3290,
        cell_min_no=15,
        cell_max_mv=3301,
        cell_max_no=1,
        cell_avg_mv=3300,
        spread_mv=11,
        pack_v=49.5,
        current_a=-0.0,
        soc=87,
        soh=100,
        cycles=12,
        energy_kwh=3.2,
        chg_limit_a=50.0,
        dis_limit_a=60.0,
        temps_c=(21.0, 21.5, -0.0),
        temp_mos_c=22.0,
        temp_board_c=23.0,
        flags_raw="0002832000000000",
        chg_mos_on=True,
        dis_mos_on=True,
        balancing=False,
    )
    stack = replace(
        empty.stack,
        modules_online=1,
        lowest_mv=3290,
        lowest_module=1,
        lowest_cell=15,
        total_current_a=-0.0,
        voltage_v=49.5,
        any_alarm=True,
        any_protection=False,
    )
    snapshot = Snapshot((module_1, *empty.modules[1:]), stack)
    initial = AlertState.initial(settings.module_count)
    alerts_1 = {
        **initial.module(1),
        "bms_alarm": Alert(True, flags=("CellV_AlarmL",)),
        "bms_protection": Alert(False, flags=()),
    }
    stack_alerts = {**initial.stack, "current_imbalance": Alert(False, value=-0.04)}
    return snapshot, AlertState((alerts_1, *initial.modules[1:]), stack_alerts)


def csv_text(value: object) -> str:
    """Return the LOG-06 encoding of one payload value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        value += 0.0
    return json.dumps(value)


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def logs(state_dir: Path) -> Path:
    return state_dir / "logs"


def listing(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def touch(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(f"{name}\n", encoding="utf-8")


def dated(prefix: str, age: int, suffix: str = ".csv") -> str:
    """Return the file name of ``prefix`` dated ``age`` days before 2026-10-09."""
    return f"{prefix}-{date(2026, 10, 9) - timedelta(days=age)}{suffix}"


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> FullDisk:
    """Route every file ``localog`` opens through a :class:`FullDisk`."""
    fake = FullDisk()
    monkeypatch.setattr(localog, "open", fake.open, raising=False)
    return fake


def own(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the records logged by ``localog``."""
    return [r for r in caplog.records if r.name == localog.__name__]


def status(log: LocalLog) -> LocalLogStatus:
    """Return ``log.status`` afresh, out of reach of the type checker's narrowing."""
    return log.status


def started(settings: Settings, state_dir: Path, wall: FakeClock, **kw: Any) -> LocalLog:
    log = LocalLog(settings, state_dir, wall, **kw)
    log.housekeep(wall())
    return log


@pytest.mark.req("LOG-06", "LOG-07", "LOG-08")
def test_columns_match_json_keys() -> None:
    assert spec_columns("LOG-07") == MODULE_COLUMNS
    assert spec_columns("LOG-08") == STACK_COLUMNS
    assert len(MODULE_COLUMNS) == 41
    assert len(STACK_COLUMNS) == 24
    assert set(MODULE_COLUMNS) <= set(MODULE_KEYS)
    assert len(set(MODULE_COLUMNS)) == len(MODULE_COLUMNS)
    assert [k for k in STACK_KEYS if not k.startswith("alert_")] == list(STACK_COLUMNS)


@pytest.mark.req("LOG-01", "LOG-05", "LOG-12")
def test_header_once_and_append(state_dir: Path, wall: FakeClock) -> None:
    settings = make_settings()
    snapshot, alerts = sample(settings)
    first = started(settings, state_dir, wall)
    assert logs(state_dir).is_dir()
    first.write(snapshot, alerts, wall())

    modules = logs(state_dir) / f"modules-{TODAY}.csv"
    stack = logs(state_dir) / f"stack-{TODAY}.csv"
    text = modules.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(MODULE_COLUMNS)
    assert stack.read_text(encoding="utf-8").splitlines()[0] == ",".join(STACK_COLUMNS)
    assert text.count("\n") == 4
    assert "\r" not in text
    first.close("stopping")

    wall.advance(10.0)
    second = started(settings, state_dir, wall)
    second.write(snapshot, alerts, wall())
    second.close("stopping")
    lines = modules.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 7
    assert lines.count(",".join(MODULE_COLUMNS)) == 1
    assert len(stack.read_text(encoding="utf-8").splitlines()) == 3

    empty = logs(state_dir) / f"stack-{TODAY}.csv"
    empty.write_text("", encoding="utf-8")
    third = started(settings, state_dir, wall)
    third.write(snapshot, alerts, wall())
    third.close("stopping")
    assert empty.read_text(encoding="utf-8").splitlines()[0] == ",".join(STACK_COLUMNS)


@pytest.mark.req("LOG-06", "LOG-07", "LOG-08", "LOG-11", "LOG-12")
def test_rows_per_module_and_stack_with_nulls_empty(state_dir: Path, wall: FakeClock) -> None:
    settings = make_settings(MODULE_NAMES="1:Upper,2:Middle")
    snapshot, alerts = sample(settings)
    log = started(settings, state_dir, wall)
    wall.advance(7.6)
    log.write(snapshot, alerts, wall())

    modules = rows(logs(state_dir) / f"modules-{TODAY}.csv")
    assert [r["module"] for r in modules] == ["1", "2", "3"]
    for ms, row in zip(snapshot.modules, modules, strict=True):
        payload = module_payload(
            ms, alerts.module(ms.module), settings.module_name(ms.module), wall()
        )
        assert row == {c: csv_text(payload[c]) for c in MODULE_COLUMNS}
    one, two = modules[0], modules[1]
    assert one["ts"] == "2026-10-09T00:00:07Z"
    assert (one["online"], one["cells_fresh"]) == ("true", "true")
    assert (one["cell_01_mv"], one["cell_15_mv"], one["pack_v"]) == ("3301", "3290", "49.5")
    assert (one["current_a"], one["temp_3_c"]) == ("0.0", "0.0")
    assert (one["flags_raw"], one["bms_alarm"], one["bms_protection"]) == (
        "0002832000000000",
        "true",
        "false",
    )
    assert (two["online"], two["cells_fresh"]) == ("false", "false")
    assert all(two[c] == "" for c in MODULE_COLUMNS[4:])

    (stack,) = rows(logs(state_dir) / f"stack-{TODAY}.csv")
    payload = stack_payload(snapshot.stack, alerts.stack, wall())
    assert stack == {c: csv_text(payload[c]) for c in STACK_COLUMNS}
    assert stack["ts"] == one["ts"]
    assert (stack["total_current_a"], stack["imbalance_pct"]) == ("0.0", "0.0")
    assert (stack["any_alarm"], stack["any_protection"], stack["bms_soc"]) == (
        "true",
        "false",
        "",
    )
    raw = (logs(state_dir) / f"modules-{TODAY}.csv").read_text(encoding="utf-8")
    raw += (logs(state_dir) / f"stack-{TODAY}.csv").read_text(encoding="utf-8")
    assert "-0.0" not in raw


@pytest.mark.req("LOG-04", "LOG-09")
def test_day_roll_at_utc_midnight(state_dir: Path, wall: FakeClock, disk: FullDisk) -> None:
    settings = make_settings()
    snapshot, alerts = sample(settings)
    wall.advance(DAY - 1.0)
    log = started(settings, state_dir, wall)
    log.write(snapshot, alerts, wall())
    log.event(wall(), "before midnight")
    assert len(disk.appended) == 3

    wall.advance(1.0)
    log.write(snapshot, alerts, wall())
    log.event(wall(), "after midnight")

    assert len(disk.appended) == 6
    assert all(handle.closed for handle in disk.appended[:3])
    assert not any(handle.closed for handle in disk.appended[3:])
    directory = logs(state_dir)
    for day, ts, line in (
        ("2026-10-09", "2026-10-09T23:59:59Z", "before midnight"),
        ("2026-10-10", "2026-10-10T00:00:00Z", "after midnight"),
    ):
        assert [r["ts"] for r in rows(directory / f"modules-{day}.csv")] == [ts] * 3
        assert [r["ts"] for r in rows(directory / f"stack-{day}.csv")] == [ts]
        events = (directory / f"events-{day}.log").read_text(encoding="utf-8")
        assert events == f"{ts} {line}\n"
    log.close("stopping")
    assert all(handle.closed for handle in disk.appended)


@pytest.mark.req("LOG-09", "LOG-10", "LOG-12")
def test_events_file_lines(state_dir: Path, wall: FakeClock, disk: FullDisk) -> None:
    log = started(make_settings(), state_dir, wall)
    events = logs(state_dir) / f"events-{TODAY}.log"
    wall.advance(1.4)
    log.event(wall(), "health: none -> starting (start)")
    assert events.read_text(encoding="utf-8") == (
        "2026-10-09T00:00:01Z health: none -> starting (start)\n"
    )
    wall.advance(5.0)
    line = "alert module 2 cell_low: off -> on (value=2994 mV, threshold=3000 mV)"
    log.event(wall(), line)
    assert events.read_text(encoding="utf-8").splitlines()[-1] == f"2026-10-09T00:00:06Z {line}"

    wall.advance(1.0)
    log.close("stopping on SIGTERM: exit status 0")
    assert events.read_text(encoding="utf-8").splitlines() == [
        "2026-10-09T00:00:01Z health: none -> starting (start)",
        f"2026-10-09T00:00:06Z {line}",
        "2026-10-09T00:00:07Z stopping on SIGTERM: exit status 0",
    ]
    assert disk.appended
    assert all(handle.closed for handle in disk.appended)


@pytest.mark.req("LOG-14", "LOG-15", "LOG-16")
def test_housekeeping_plan_gzip_and_delete_boundaries(tmp_path: Path, wall: FakeClock) -> None:
    names = [dated("modules", age) for age in range(-1, 6)]
    names += [dated("stack", age, ".csv.gz") for age in range(-1, 6)]
    names += [dated("events", age, ".log") for age in (1, 2, 4)]
    names += [dated("events", 4, ".log.gz"), dated("modules", 2, ".csv.gz")]
    others = [
        "notes.txt",
        "cells-2026-09-01.csv",
        "modules-2026-09-01.csv.bak",
        "modules-2026-9-01.csv",
        "stack-2026-02-30.csv",
        "events-2026-09-01.csv",
    ]
    touch(tmp_path, *names, *others)
    before = listing(tmp_path)

    to_gzip, to_delete = housekeeping_plan(tmp_path, wall(), 1, 3)

    assert listing(tmp_path) == before
    assert sorted(p.name for p in to_gzip) == sorted(
        [dated("modules", 2), dated("modules", 3), dated("events", 2, ".log")]
    )
    assert sorted(p.name for p in to_delete) == sorted(
        [
            dated("modules", 4),
            dated("modules", 5),
            dated("stack", 4, ".csv.gz"),
            dated("stack", 5, ".csv.gz"),
            dated("events", 4, ".log"),
            dated("events", 4, ".log.gz"),
        ]
    )
    assert all(p.parent == tmp_path for p in (*to_gzip, *to_delete))

    to_gzip, to_delete = housekeeping_plan(tmp_path, wall(), 0, 1)
    assert sorted(p.name for p in to_gzip) == sorted(
        [dated("modules", 1), dated("events", 1, ".log")]
    )
    assert dated("modules", 2) in {p.name for p in to_delete}
    assert not {p.name for p in (*to_gzip, *to_delete)} & {*others, dated("modules", 0)}

    late = wall() + DAY - 1.0
    assert housekeeping_plan(tmp_path, late, 1, 3) == housekeeping_plan(tmp_path, wall(), 1, 3)


@pytest.mark.req("LOG-13", "LOG-14", "LOG-15", "LOG-16", "LOG-17")
def test_housekeeping_skips_today_and_is_idempotent(state_dir: Path, wall: FakeClock) -> None:
    settings = make_settings(LOCAL_LOG_COMPRESS_AFTER_DAYS="1", LOCAL_LOG_RETENTION_DAYS="3")
    directory = logs(state_dir)
    touch(
        directory,
        dated("modules", 0),
        dated("events", 0, ".log"),
        dated("stack", -2),
        dated("modules", 1),
        dated("modules", 2),
        dated("stack", 3),
        dated("stack", 4, ".csv.gz"),
        "README",
    )
    (directory / (dated("stack", 3) + ".gz")).write_bytes(b"left by an interrupted run")
    originals = listing(directory)

    log = started(settings, state_dir, wall)
    assert status(log) is LocalLogStatus.OK

    after = listing(directory)
    for name in (
        dated("modules", 0),
        dated("events", 0, ".log"),
        dated("stack", -2),
        dated("modules", 1),
        "README",
    ):
        assert after[name] == originals[name]
    for name in (dated("modules", 2), dated("stack", 3)):
        assert name not in after
        assert gzip.decompress(after[f"{name}.gz"]) == originals[name]
    assert dated("stack", 4, ".csv.gz") not in after

    log.housekeep(wall())
    wall.advance(DAY - 1.0)
    log.housekeep(wall())
    assert listing(directory) == after
    assert status(log) is LocalLogStatus.OK


@pytest.mark.req("LOG-15", "LOG-18", "LOG-19")
def test_gzip_failure_keeps_original(
    state_dir: Path, wall: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    old = dated("modules", 2)
    touch(logs(state_dir), old)

    calls: list[Path] = []

    def failing(path: Path, mode: str) -> Any:
        calls.append(path)
        if len(calls) > 1:
            return gzip.open(path, mode)
        with gzip.open(path, "wb") as handle:
            handle.write(b"partial")
        raise FULL

    with caplog.at_level(logging.DEBUG):
        log = started(make_settings(), state_dir, wall, gzip_open=failing)

    assert status(log) is LocalLogStatus.ERROR
    assert (logs(state_dir) / old).read_text(encoding="utf-8") == f"{old}\n"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert old in errors[0] and "No space left on device" in errors[0]

    log.housekeep(wall())
    assert status(log) is LocalLogStatus.OK
    assert not (logs(state_dir) / old).exists()
    assert gzip.decompress((logs(state_dir) / f"{old}.gz").read_bytes()) == f"{old}\n".encode()


@pytest.mark.req("LOG-18", "LOG-19")
def test_write_oserror_sets_error_status_and_never_raises(
    state_dir: Path,
    wall: FakeClock,
    disk: FullDisk,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings()
    snapshot, alerts = sample(settings)
    log = started(settings, state_dir, wall)
    log.write(snapshot, alerts, wall())
    modules = logs(state_dir) / f"modules-{TODAY}.csv"
    events = logs(state_dir) / f"events-{TODAY}.log"
    log.event(wall(), "first")
    size = modules.stat().st_size

    disk.full = True
    with caplog.at_level(logging.DEBUG):
        log.write(snapshot, alerts, wall())
        assert status(log) is LocalLogStatus.ERROR
        grown = modules.stat().st_size
        assert size < grown
        for _ in range(3):
            wall.advance(10.0)
            log.write(snapshot, alerts, wall())
            log.event(wall(), "dropped")
        disk.full = False
        log.write(snapshot, alerts, wall())
        log.event(wall(), "still dropped")

    assert status(log) is LocalLogStatus.ERROR
    assert modules.stat().st_size == grown
    assert events.read_text(encoding="utf-8") == "2026-10-09T00:00:00Z first\n"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "write" in message
    assert str(modules) in message
    assert "No space left on device" in message
    assert all(r.levelno <= logging.DEBUG for r in caplog.records if r is not errors[0])


@pytest.mark.req("LOG-19", "LOG-20")
def test_recovers_after_housekeeping_reopen(
    state_dir: Path,
    wall: FakeClock,
    disk: FullDisk,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings()
    snapshot, alerts = sample(settings)
    log = started(settings, state_dir, wall)
    log.event(wall(), "first")
    disk.full = True
    log.event(wall(), "a line long enough to be cut in half")
    assert status(log) is LocalLogStatus.ERROR
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        log.housekeep(wall())
        assert status(log) is LocalLogStatus.ERROR
        disk.full = False
        wall.advance(3600.0)
        log.housekeep(wall())
        assert status(log) is LocalLogStatus.OK
        log.event(wall(), "after")
        log.write(snapshot, alerts, wall())

    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.levelno > logging.DEBUG]
    assert records == [(logging.INFO, "local log recovered")]
    events = (logs(state_dir) / f"events-{TODAY}.log").read_text(encoding="utf-8")
    cut = "2026-10-09T00:00:00Z a line long enough to be cut in half\n"
    cut = cut[: len(cut) // 2] + "\n"
    assert events.splitlines() == [
        "2026-10-09T00:00:00Z first",
        cut[:-1],
        "2026-10-09T01:00:00Z after",
    ]
    assert len(rows(logs(state_dir) / f"modules-{TODAY}.csv")) == 3

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        disk.full = True
        log.event(wall(), "second episode")
    assert status(log) is LocalLogStatus.ERROR
    assert [r.levelno for r in caplog.records if r.levelno > logging.DEBUG] == [logging.ERROR]


@pytest.mark.req("LOG-02", "LOG-21")
def test_disabled_without_state_directory_warns_once(
    tmp_path: Path,
    wall: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = make_settings()
    snapshot, alerts = sample(settings)
    with caplog.at_level(logging.DEBUG):
        log = LocalLog(settings, None, wall)
        log.housekeep(wall())
        log.write(snapshot, alerts, wall())
        log.event(wall(), "ignored")
        log.housekeep(wall())
        log.close("stopping")

    assert status(log) is LocalLogStatus.DISABLED
    assert [(r.levelno, r.getMessage()) for r in own(caplog)] == [
        (logging.WARNING, "STATE_DIRECTORY is unset; local logging is disabled")
    ]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.req("LOG-02", "LOG-21")
def test_disabled_by_setting_touches_nothing(
    state_dir: Path, wall: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    touch(logs(state_dir), dated("modules", 30))
    before = listing(logs(state_dir))
    settings = make_settings(LOCAL_LOG="false")
    snapshot, alerts = sample(settings)
    with caplog.at_level(logging.DEBUG):
        log = started(settings, state_dir, wall)
        log.write(snapshot, alerts, wall())
        log.event(wall(), "ignored")
        log.close("stopping")

    assert status(log) is LocalLogStatus.DISABLED
    assert own(caplog) == []
    assert listing(logs(state_dir)) == before
    assert [p.name for p in state_dir.iterdir()] == ["logs"]


@pytest.mark.req("LOG-21", "MQTT-18", "HA-12")
def test_service_payload_local_log_field(wall: FakeClock) -> None:
    assert [s.value for s in LocalLogStatus] == list(LOCAL_LOG_OPTIONS)
    info = ServiceInfo(
        version="0.1.0",
        health=HealthState.HEALTHY,
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
        local_log=LocalLogStatus.OK,
    )
    for status in LocalLogStatus:
        payload = service_payload(replace(info, local_log=status), wall())
        assert payload["local_log"] == status.value
        assert type(payload["local_log"]) is str


@pytest.mark.req("LOG-12")
def test_no_fsync() -> None:
    source = Path(localog.__file__).read_text(encoding="utf-8")
    forbidden = {"fsync", "fdatasync", "sync"}
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    assert not names & forbidden


@pytest.mark.req("LOG-04")
def test_day_name() -> None:
    assert day_name("modules", 1791504000.0) == "modules-2026-10-09.csv"
    assert day_name("stack", 1791504000.0 + DAY - 0.5) == "stack-2026-10-09.csv"
    assert day_name("events", 1791504000.0 + DAY) == "events-2026-10-10.log"
    with pytest.raises(ValueError, match="cells"):
        day_name("cells", 1791504000.0)
