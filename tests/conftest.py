"""Suite-wide plugins and fixtures."""

import pytest

from tests.fakes.clock import FakeClock

pytest_plugins = ["pytester", "tests.plugins.markers", "tests.plugins.reqtrace"]


@pytest.fixture
def clock() -> FakeClock:
    """Return a monotonic fake clock reading 1000 s."""
    return FakeClock(1000.0)


@pytest.fixture
def wall() -> FakeClock:
    """Return a wall-clock fake reading 2026-10-09T00:00:00Z (Unix seconds)."""
    return FakeClock(1791504000.0)
