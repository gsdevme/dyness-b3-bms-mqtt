"""Suite-wide plugins and fixtures."""

import itertools
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from dyness_b3_bms_mqtt.canbus import BusLike, PythonCanTransport
from tests.fakes.clock import FakeClock
from tests.fakes.notify import NotifySocket
from tests.support.txguard import TxGuard

pytest_plugins = ["pytester", "tests.plugins.markers", "tests.plugins.reqtrace"]

_channel_numbers = itertools.count(1)


@pytest.fixture
def clock() -> FakeClock:
    """Return a monotonic fake clock reading 1000 s."""
    return FakeClock(1000.0)


@pytest.fixture
def wall() -> FakeClock:
    """Return a wall-clock fake reading 2026-10-09T00:00:00Z (Unix seconds)."""
    return FakeClock(1791504000.0)


@pytest.fixture
def can_channel() -> str:
    """Return a python-can ``virtual`` channel name no other test uses (TEST-13)."""
    return f"test-{next(_channel_numbers)}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Return a fresh, empty state directory for this test (TEST-13)."""
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def notify_socket() -> Iterator[NotifySocket]:
    """Yield a fake ``NOTIFY_SOCKET`` bound for this test and close it afterwards (TEST-20)."""
    sock = NotifySocket()
    yield sock
    sock.close()


@pytest.fixture(autouse=True)
def tx_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[TxGuard]:
    """Wrap every bus a ``PythonCanTransport`` opens and fail on any bad transmission (TEST-28)."""
    guard = TxGuard()
    construct = PythonCanTransport._construct_bus

    def guarded(self: PythonCanTransport, kwargs: Mapping[str, Any]) -> BusLike:
        settings = self._settings
        return guard.wrap(
            construct(self, kwargs),
            listen_only=settings.can_listen_only,
            module_count=settings.module_count,
        )

    monkeypatch.setattr(PythonCanTransport, "_construct_bus", guarded)
    yield guard
    if guard.violations:
        pytest.fail(f"tx_guard: forbidden transmissions: {guard.violations}")
