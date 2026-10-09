"""Tests of the recovery state store and UTC formatting: 03-health-and-recovery.md section 6.

Covers ``recovery.json`` (HEALTH-24..26) and ``last_exit.json`` (SVC-03, SVC-04). Every test
works in ``tmp_path`` or in memory; nothing touches ``/var/lib``.
"""

import json
import logging
import os
from pathlib import Path

import pytest

from dyness_b3_bms_mqtt.state_store import (
    LAST_EXIT_FILE,
    RECOVERY_FILE,
    LastExit,
    RecoveryRecord,
    StateStore,
)
from dyness_b3_bms_mqtt.timefmt import iso_utc
from tests.fakes.clock import FakeClock


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.req("HEALTH-01", "HEALTH-24")
def test_iso_utc_floors_to_seconds(wall: FakeClock) -> None:
    assert iso_utc(wall()) == "2026-10-09T00:00:00Z"
    assert iso_utc(wall() + 0.999) == "2026-10-09T00:00:00Z"
    assert iso_utc(wall() + 59.999) == "2026-10-09T00:00:59Z"
    assert iso_utc(wall() - 0.5) == "2026-10-08T23:59:59Z"


@pytest.mark.req("HEALTH-01", "HEALTH-24", "HEALTH-25", "SVC-03")
def test_round_trip_and_atomic_write(
    tmp_path: Path,
    wall: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    replaced: list[tuple[Path, Path]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def replace(src: str, dst: str) -> None:
        events.append("replace")
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    store = StateStore(tmp_path, wall)
    wall.advance(42.7)

    store.save_recovery(RecoveryRecord(escalation=2, restarts=5))

    target = tmp_path / RECOVERY_FILE
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "escalation": 2,
        "restarts": 5,
        "updated": "2026-10-09T00:00:42Z",
    }
    assert store.load_recovery() == RecoveryRecord(escalation=2, restarts=5)
    assert [p.name for p in tmp_path.iterdir()] == [RECOVERY_FILE]
    assert "fsync" in events
    assert events.index("fsync") < events.index("replace")
    [(src, dst)] = replaced
    assert src.parent == tmp_path
    assert src != dst
    assert dst == target

    def failing_replace(src: str, dst: str) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(os, "replace", failing_replace)
    caplog.set_level(logging.WARNING)
    store.save_recovery(RecoveryRecord(escalation=9, restarts=9))

    assert store.load_recovery() == RecoveryRecord(escalation=2, restarts=5)
    assert [p.name for p in tmp_path.iterdir()] == [RECOVERY_FILE]
    assert any(RECOVERY_FILE in message for message in warnings_of(caplog))


@pytest.mark.req("HEALTH-25")
def test_save_into_missing_directory_is_swallowed(
    tmp_path: Path, wall: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    store = StateStore(tmp_path / "absent", wall)

    store.save_recovery(RecoveryRecord(escalation=1, restarts=0))

    assert not (tmp_path / "absent").exists()
    assert any(RECOVERY_FILE in message for message in warnings_of(caplog))


@pytest.mark.req("HEALTH-25")
@pytest.mark.parametrize(
    "content",
    [
        None,
        b"",
        b"not json",
        b"[]",
        b'"text"',
        b'{"escalation": -1, "restarts": 0}',
        b'{"escalation": "1", "restarts": 0}',
        b'{"escalation": true, "restarts": 0}',
        b'{"escalation": 1.0, "restarts": 0}',
        b'{"escalation": 1, "restarts": null}',
        b'{"restarts": 0}',
        b'{"escalation": 1}',
        b"\xff\xfe\x00",
        "directory",
    ],
    ids=[
        "missing",
        "empty",
        "not-json",
        "list",
        "string",
        "negative",
        "string-count",
        "bool-count",
        "float-count",
        "null-restarts",
        "no-escalation",
        "no-restarts",
        "not-utf8",
        "unreadable",
    ],
)
def test_invalid_recovery_counts_as_zero(
    tmp_path: Path,
    wall: FakeClock,
    caplog: pytest.LogCaptureFixture,
    content: bytes | str | None,
) -> None:
    target = tmp_path / RECOVERY_FILE
    if content == "directory":
        target.mkdir()
    elif isinstance(content, bytes):
        target.write_bytes(content)
    caplog.set_level(logging.WARNING)

    record = StateStore(tmp_path, wall).load_recovery()

    assert record == RecoveryRecord(escalation=0, restarts=0)
    assert any(RECOVERY_FILE in message for message in warnings_of(caplog))


@pytest.mark.req("HEALTH-26", "SVC-03", "SVC-04")
@pytest.mark.parametrize("environ", [{}, {"STATE_DIRECTORY": ""}], ids=["unset", "empty"])
def test_memory_only_without_state_directory(
    tmp_path: Path,
    wall: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    environ: dict[str, str],
) -> None:
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.WARNING)

    store = StateStore.from_environ(environ, wall)
    first = store.load_recovery()
    store.save_recovery(RecoveryRecord(escalation=3, restarts=1))
    second = store.load_recovery()
    last_exit = store.load_last_exit()

    assert store.directory is None
    assert first == RecoveryRecord(escalation=0, restarts=0)
    assert second == RecoveryRecord(escalation=3, restarts=1)
    assert last_exit is None
    assert list(tmp_path.iterdir()) == []
    assert len(warnings_of(caplog)) == 1
    assert "STATE_DIRECTORY" in warnings_of(caplog)[0]


@pytest.mark.req("SVC-03")
def test_state_directory_from_environment(tmp_path: Path, wall: FakeClock) -> None:
    single = StateStore.from_environ({"STATE_DIRECTORY": str(tmp_path)}, wall)
    several = StateStore.from_environ({"STATE_DIRECTORY": f"{tmp_path}:/elsewhere"}, wall)

    assert single.directory == tmp_path
    assert several.directory == tmp_path


@pytest.mark.req("SVC-03", "SVC-04")
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            b'{"code": 76, "result": "exit-code", "at": "2026-10-09T12:00:00Z"}',
            LastExit(code=76, result="exit-code"),
        ),
        (b'{"code": 0, "result": "success"}', LastExit(code=0, result="success")),
        (b'{"code": "ABRT", "result": "watchdog"}', LastExit(code="ABRT", result="watchdog")),
        (b'{"code": null, "result": null}', LastExit(code=None, result=None)),
        (b'{"code": 1}', LastExit(code=1, result=None)),
        (None, None),
        (b"", None),
        (b"{", None),
        (b"[76]", None),
        (b'{"result": "exit-code"}', None),
        (b'{"code": true, "result": "exit-code"}', None),
        (b'{"code": 1.5, "result": "exit-code"}', None),
        (b'{"code": 76, "result": 3}', None),
        (b"\xff", None),
    ],
    ids=[
        "exit-code",
        "clean",
        "signal",
        "nulls",
        "no-result",
        "missing",
        "empty",
        "truncated",
        "list",
        "no-code",
        "bool-code",
        "float-code",
        "number-result",
        "not-utf8",
    ],
)
def test_last_exit(
    tmp_path: Path, wall: FakeClock, content: bytes | None, expected: LastExit | None
) -> None:
    if content is not None:
        (tmp_path / LAST_EXIT_FILE).write_bytes(content)

    assert StateStore(tmp_path, wall).load_last_exit() == expected
