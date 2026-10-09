"""Tests of the dependency-free ``sd_notify`` client: 09-service.md SVC-05, SVC-07, SVC-09.

Every datagram goes to a fake ``NOTIFY_SOCKET`` (TEST-20); the watchdog cadence is driven by
explicit ``now`` readings, never by sleeping.
"""

import logging
import os
from pathlib import Path

import pytest

from dyness_b3_bms_mqtt.sdnotify import Notifier
from tests.fakes.notify import NotifySocket

PID = 4242


@pytest.mark.req("SVC-05", "SVC-07")
def test_noop_without_socket(notify_socket: NotifySocket) -> None:
    for environ in ({}, {"NOTIFY_SOCKET": ""}):
        notifier = Notifier({**environ, "WATCHDOG_USEC": "3000000"}, PID)
        assert notifier.notify("STATUS=x") is False
        assert notifier.ready() is False
        assert notifier.status("HEALTHY: ok") is False
        assert notifier.stopping() is False
        assert notifier.watchdog(1000.0) is False
    assert notify_socket.messages() == []


@pytest.mark.req("SVC-05", "SVC-09", "TEST-20")
def test_datagrams_received(notify_socket: NotifySocket) -> None:
    notifier = Notifier({"NOTIFY_SOCKET": notify_socket.path}, PID)
    assert notifier.ready() is True
    assert notifier.status("DEGRADED: m2 cells_stale") is True
    assert notifier.stopping() is True
    assert notifier.notify("EXTEND_TIMEOUT_USEC=1000000") is True
    assert notify_socket.messages() == [
        "READY=1",
        "STATUS=DEGRADED: m2 cells_stale",
        "STOPPING=1",
        "EXTEND_TIMEOUT_USEC=1000000",
    ]


@pytest.mark.req("SVC-05", "TEST-20")
def test_abstract_namespace() -> None:
    sock = NotifySocket(abstract=True)
    try:
        assert sock.path.startswith("@")
        notifier = Notifier({"NOTIFY_SOCKET": sock.path}, PID)
        assert notifier.status("STARTING: opening bus") is True
        assert sock.messages() == ["STATUS=STARTING: opening bus"]
    finally:
        sock.close()


@pytest.mark.req("SVC-05")
def test_send_failure_logged_debug_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "absent.sock"
    notifier = Notifier({"NOTIFY_SOCKET": str(missing)}, PID)
    with caplog.at_level(logging.DEBUG, logger="dyness_b3_bms_mqtt.sdnotify"):
        assert notifier.status("HEALTHY: 3/3 modules ok") is False
        assert notifier.stopping() is False
    records = [r for r in caplog.records if r.name == "dyness_b3_bms_mqtt.sdnotify"]
    assert len(records) == 2
    assert all(r.levelno == logging.DEBUG for r in records)
    assert "STOPPING=1" in records[1].getMessage()


@pytest.mark.req("SVC-05", "TEST-20")
def test_ready_once(notify_socket: NotifySocket) -> None:
    notifier = Notifier({"NOTIFY_SOCKET": notify_socket.path}, PID)
    assert notifier.ready() is True
    assert notifier.ready() is False
    assert notifier.ready() is False
    assert notify_socket.messages() == ["READY=1"]


@pytest.mark.req("SVC-07", "TEST-20")
def test_watchdog_cadence_and_pid(notify_socket: NotifySocket) -> None:
    base = {"NOTIFY_SOCKET": notify_socket.path, "WATCHDOG_USEC": "3000000"}

    notifier = Notifier(base, PID)
    sent = [
        now for now in (1000.0, 1000.5, 1000.999, 1001.0, 1001.5, 1002.25) if notifier.watchdog(now)
    ]
    assert sent == [1000.0, 1001.0, 1002.25]

    matching = Notifier({**base, "WATCHDOG_PID": str(PID)}, PID)
    assert matching.watchdog(2000.0) is True

    other = Notifier({**base, "WATCHDOG_PID": str(PID + 1)}, PID)
    assert other.watchdog(3000.0) is False
    assert other.watchdog(4000.0) is False

    assert notify_socket.messages() == ["WATCHDOG=1"] * 4


@pytest.mark.req("SVC-07")
def test_no_watchdog_without_usec(notify_socket: NotifySocket) -> None:
    for extra in ({}, {"WATCHDOG_USEC": ""}, {"WATCHDOG_USEC": "0"}, {"WATCHDOG_USEC": "soon"}):
        notifier = Notifier({"NOTIFY_SOCKET": notify_socket.path, **extra}, os.getpid())
        assert notifier.watchdog(1000.0) is False
        assert notifier.watchdog(5000.0) is False
    assert notify_socket.messages() == []
