"""Tests of the marker plugin (``tests/plugins/markers.py``) and the fake clock.

The plugin behaviour is checked in a throwaway pytest run in a subprocess, so the outer
test's own ``time.sleep`` guard cannot leak into the inner run, and so the ``fast`` inner
runs may shrink the plugin's timeout cap and broker wait without touching this process.
"""

import socket
import threading
import time

import pytest

from tests.fakes.clock import FakeClock
from tests.plugins.markers import (
    BROKER_WAIT_S,
    INTEGRATION_TIMEOUT_S,
    broker_address,
    broker_reachable,
)
from tests.support.inner_run import configure_inner_run

FAST_CAP_S = 0.5
FAST_WAIT_S = 0.5
FAST = f"""
def pytest_configure(config):
    markers = config.pluginmanager.get_plugin("tests.plugins.markers")
    markers.INTEGRATION_TIMEOUT_S = {FAST_CAP_S}
    markers.BROKER_WAIT_S = {FAST_WAIT_S}
"""

MOSQUITTO_TESTS = """
import pytest

@pytest.mark.mosquitto
def test_needs_broker():
    pass

def test_unrelated():
    pass
"""


@pytest.fixture
def harness(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """Return a pytester directory configured like the project: same markers, strict."""
    configure_inner_run(pytester, monkeypatch, ["tests.plugins.markers"])
    return pytester


@pytest.fixture
def fast(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """Return a project-like pytester directory whose cap and broker wait are shrunk."""
    configure_inner_run(pytester, monkeypatch, ["tests.plugins.markers"], conftest=FAST)
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
def test_integration_may_sleep_and_gets_timeout(fast: pytest.Pytester) -> None:
    assert INTEGRATION_TIMEOUT_S == 10.0
    fast.makepyfile(
        f"""
        import time

        import pytest

        @pytest.mark.integration
        def test_sleeps_within_the_cap(request):
            time.sleep(0.05)
            assert request.node.get_closest_marker("timeout").args == ({FAST_CAP_S},)

        @pytest.mark.integration
        def test_sleeps_past_the_cap():
            time.sleep(3)

        @pytest.mark.integration
        @pytest.mark.timeout(60)
        def test_cannot_raise_the_cap():
            time.sleep(3)

        @pytest.mark.integration
        @pytest.mark.timeout(0)
        def test_cannot_disable_the_cap():
            time.sleep(3)

        @pytest.mark.integration
        @pytest.mark.timeout(0.1)
        def test_keeps_a_stricter_timeout():
            time.sleep({FAST_CAP_S} * 0.6)

        def test_unmarked_gets_the_suite_ceiling(request):
            assert request.node.get_closest_marker("timeout") is None
            assert float(request.config.getini("timeout")) == 120
        """
    )
    started = time.monotonic()
    result = fast.runpytest_subprocess()
    assert time.monotonic() - started < 10
    result.assert_outcomes(passed=2, failed=4)
    cap = f"E*Failed: Timeout (>{FAST_CAP_S}s) from pytest-timeout."
    result.stdout.fnmatch_lines(
        [
            "*def test_sleeps_past_the_cap*",
            cap,
            "*def test_cannot_raise_the_cap*",
            cap,
            "*def test_cannot_disable_the_cap*",
            cap,
            "*def test_keeps_a_stricter_timeout*",
            "E*Failed: Timeout (>0.1s) from pytest-timeout.",
        ]
    )
    result.stdout.no_fnmatch_line("*PytestAssertRewriteWarning*")


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


@pytest.mark.req("TEST-01", "TEST-02")
def test_mosquitto_deselected_without_flag(harness: pytest.Pytester) -> None:
    harness.makepyfile(MOSQUITTO_TESTS)
    result = harness.runpytest_subprocess()
    result.assert_outcomes(passed=1, deselected=1)
    result.stdout.no_fnmatch_line("*SKIPPED*")
    result.stdout.no_fnmatch_line("*broker*")


@pytest.mark.req("TEST-01", "TEST-02", "TEST-03", "TEST-35")
def test_mosquitto_fails_when_flagged_and_unreachable(
    fast: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert BROKER_WAIT_S == 10.0
    fast.makepyfile(MOSQUITTO_TESTS)
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
        monkeypatch.setenv("MQTT_PORT", str(port))
        assert not broker_reachable("127.0.0.1", port)
        result = fast.runpytest_subprocess("--mosquitto")
    result.assert_outcomes(failed=1, passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            f"broker unreachable at 127.0.0.1:{port} within {FAST_WAIT_S:g} s (--mosquitto)",
            "FAILED*::test_needs_broker*",
        ]
    )
    result.stdout.no_fnmatch_line("*SKIPPED*")


@pytest.mark.req("TEST-01", "TEST-02", "TEST-35")
def test_mosquitto_runs_when_flagged_and_reachable(
    fast: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert broker_address({}) == ("localhost", 1883)
    assert broker_address({"MQTT_HOST": "", "MQTT_PORT": ""}) == ("localhost", 1883)
    assert broker_address({"MQTT_HOST": "broker.lan", "MQTT_PORT": "1884"}) == ("broker.lan", 1884)
    fast.makepyfile(MOSQUITTO_TESTS)
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        port = listening.getsockname()[1]
        monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
        monkeypatch.setenv("MQTT_PORT", str(port))
        assert broker_reachable("127.0.0.1", port)
        fast.runpytest_subprocess("--mosquitto").assert_outcomes(passed=2)


@pytest.mark.integration
@pytest.mark.req("TEST-02")
def test_broker_probe_waits_for_a_late_listener() -> None:
    with socket.socket() as late:
        late.bind(("127.0.0.1", 0))
        port = late.getsockname()[1]
        assert not broker_reachable("127.0.0.1", port)
        timer = threading.Timer(0.5, late.listen)
        timer.start()
        try:
            started = time.monotonic()
            assert broker_reachable("127.0.0.1", port, wait_s=5.0)
            assert 0.4 < time.monotonic() - started < 5.0
        finally:
            timer.cancel()
            timer.join()
