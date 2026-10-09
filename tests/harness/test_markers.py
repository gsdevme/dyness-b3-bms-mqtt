"""Tests of the marker plugin (``tests/plugins/markers.py``) and the fake clock.

The plugin behaviour is checked in a throwaway pytest run in a subprocess, so the outer
test's own ``time.sleep`` guard cannot leak into the inner run.
"""

import os
import socket
import tomllib
from pathlib import Path

import pytest

from tests.fakes.clock import FakeClock
from tests.plugins.markers import (
    INTEGRATION_TIMEOUT_S,
    broker_address,
    broker_reachable,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def harness(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """Return a pytester directory configured like the project: same markers, strict."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        markers: list[str] = tomllib.load(handle)["tool"]["pytest"]["ini_options"]["markers"]
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    pytester.makeini(
        "[pytest]\n"
        "addopts = --strict-markers --strict-config -ra\n"
        "markers =\n" + "".join(f"    {marker}\n" for marker in markers)
    )
    pytester.makeconftest('pytest_plugins = ["tests.plugins.markers"]\n')
    return pytester


@pytest.mark.req("TEST-11")
def test_fake_clock() -> None:
    assert FakeClock()() == 0.0
    clock = FakeClock(1000.0)
    assert clock() == clock.now() == 1000.0
    clock.advance(2.5)
    assert clock.now() == 1002.5
    clock.sleep(0.5)
    assert clock() == 1003.0
    clock.advance(0)
    assert clock() == 1003.0
    with pytest.raises(ValueError, match="negative"):
        clock.advance(-0.001)
    assert clock() == 1003.0


@pytest.mark.req("TEST-11")
def test_clock_fixtures(clock: FakeClock, wall: FakeClock) -> None:
    assert clock() == 1000.0
    assert wall() == 1791504000.0


@pytest.mark.req("TEST-11")
def test_sleep_raises_outside_integration(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import time

        def test_sleeps():
            time.sleep(0)
        """
    )
    result = harness.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*time.sleep(0) called in a test not marked integration*"])


@pytest.mark.req("TEST-11", "TEST-02")
def test_integration_may_sleep_and_gets_timeout(harness: pytest.Pytester) -> None:
    assert INTEGRATION_TIMEOUT_S == 10.0
    harness.makepyfile(
        """
        import time

        import pytest

        @pytest.mark.integration
        def test_sleeps(request):
            time.sleep(0.01)
            assert request.node.get_closest_marker("timeout").args == (10.0,)

        @pytest.mark.integration
        @pytest.mark.timeout(60)
        def test_cannot_raise_the_cap(request):
            assert request.node.get_closest_marker("timeout").args == (10.0,)

        @pytest.mark.integration
        @pytest.mark.timeout(0)
        def test_cannot_disable_the_cap(request):
            assert request.node.get_closest_marker("timeout").args == (10.0,)

        @pytest.mark.integration
        @pytest.mark.timeout(2)
        def test_keeps_a_stricter_timeout(request):
            assert request.node.get_closest_marker("timeout").args == (2,)
        """
    )
    result = harness.runpytest_subprocess()
    result.assert_outcomes(passed=4)


@pytest.mark.req("TEST-01", "TEST-02", "TEST-03")
def test_hardware_skipped_without_flag(harness: pytest.Pytester) -> None:
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.hardware
        def test_needs_stack():
            pass
        """
    )
    result = harness.runpytest_subprocess()
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["SKIPPED*--hardware not given*"])

    harness.runpytest_subprocess("--hardware").assert_outcomes(passed=1)
    harness.runpytest_subprocess("--hardware", "--hardware-poll").assert_outcomes(passed=1)


@pytest.mark.req("TEST-01", "TEST-02", "TEST-03")
def test_mosquitto_skipped_when_broker_unreachable(
    harness: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert broker_address({}) == ("localhost", 1883)
    assert broker_address({"MQTT_HOST": "", "MQTT_PORT": ""}) == ("localhost", 1883)
    assert broker_address({"MQTT_HOST": "broker.lan", "MQTT_PORT": "1884"}) == ("broker.lan", 1884)
    harness.makepyfile(
        """
        import pytest

        @pytest.mark.mosquitto
        def test_needs_broker():
            pass
        """
    )
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
        monkeypatch.setenv("MQTT_PORT", str(port))
        assert not broker_reachable("127.0.0.1", port)
        result = harness.runpytest_subprocess()
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines([f"SKIPPED*broker unreachable at 127.0.0.1:{port}*"])

    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        port = listening.getsockname()[1]
        monkeypatch.setenv("MQTT_PORT", str(port))
        assert broker_reachable("127.0.0.1", port)
        harness.runpytest_subprocess().assert_outcomes(passed=1)


@pytest.mark.mosquitto
@pytest.mark.req("TEST-02")
def test_configured_broker_accepts_connections() -> None:
    assert broker_reachable(*broker_address(os.environ))
