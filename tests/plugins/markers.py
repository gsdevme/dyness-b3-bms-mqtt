"""Run-time behaviour of the suite's markers (TEST-01, TEST-02, TEST-03, TEST-11).

The markers are registered in ``pyproject.toml``; this plugin gives them their meaning:

* ``hardware`` tests are skipped unless pytest is given ``--hardware``. ``--hardware-poll``
  additionally allows them to transmit the module poll; without it they stay listen-only
  (TEST-38). Every such skip carries its reason so ``-ra`` reports why the test did not run.
* ``mosquitto`` tests are deselected unless pytest is given ``--mosquitto``, so the default
  run neither runs nor reports them. With it the broker at ``MQTT_HOST``:``MQTT_PORT`` is a
  hard requirement: it is probed once per session, retrying for up to ``BROKER_WAIT_S``
  (a CI service container may still be starting), and if it never accepts a TCP connection
  every ``mosquitto`` test fails with the address and the wait as its reason.
* ``integration`` tests may sleep and are capped at ``INTEGRATION_TIMEOUT_S`` by a
  pytest-timeout ``timeout`` marker; a stricter per-test ``timeout`` is kept. A marker beats
  the suite-wide ``timeout`` ceiling in ``pyproject.toml``, which every other test gets.
  Every other test also has ``time.sleep`` replaced by a function that fails the test.

``INTEGRATION_TIMEOUT_S`` and ``BROKER_WAIT_S`` are read when they are applied, so a
throwaway pytest run's ``conftest.py`` may shrink them to keep the harness's own tests fast.
"""

import os
import socket
import time
from collections.abc import Mapping
from typing import NoReturn

import pytest

INTEGRATION_TIMEOUT_S = 10.0
BROKER_WAIT_S = 10.0
BROKER_CONNECT_TIMEOUT_S = 1.0
BROKER_RETRY_S = 0.25
DEFAULT_MQTT_HOST = "localhost"
DEFAULT_MQTT_PORT = 1883
_BROKER_UNREACHABLE = pytest.StashKey[str]()
_sleep = time.sleep


def broker_address(environ: Mapping[str, str]) -> tuple[str, int]:
    """Return the ``(host, port)`` of the broker the ``mosquitto`` tests use.

    Read from ``MQTT_HOST`` and ``MQTT_PORT``. An unset or empty variable falls back to the
    service default from ``docs/specs/08-configuration.md``.
    """
    host = environ.get("MQTT_HOST") or DEFAULT_MQTT_HOST
    port = int(environ.get("MQTT_PORT") or DEFAULT_MQTT_PORT)
    return host, port


def broker_reachable(host: str, port: int, wait_s: float = 0.0) -> bool:
    """Return whether a TCP connection to ``host``:``port`` succeeds within ``wait_s``.

    One connection attempt, limited to ``BROKER_CONNECT_TIMEOUT_S``, is always made; further
    attempts follow ``BROKER_RETRY_S`` apart until ``wait_s`` has passed. The pause uses the
    real ``time.sleep`` captured at import, because the probe runs during collection.
    """
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with socket.create_connection((host, port), timeout=BROKER_CONNECT_TIMEOUT_S):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
        _sleep(BROKER_RETRY_S)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--hardware``, ``--hardware-poll`` and ``--mosquitto``."""
    group = parser.getgroup("dyness", "dyness-b3-bms-mqtt markers")
    group.addoption(
        "--hardware",
        action="store_true",
        default=False,
        help="run tests marked hardware against the real stack on CAN_CHANNEL",
    )
    group.addoption(
        "--hardware-poll",
        action="store_true",
        default=False,
        help="let hardware tests transmit the module poll (otherwise listen-only)",
    )
    group.addoption(
        "--mosquitto",
        action="store_true",
        default=False,
        help="run tests marked mosquitto; they fail if MQTT_HOST:MQTT_PORT is unreachable",
    )


def _within_cap(marker: pytest.Mark | None) -> bool:
    """Return whether a ``timeout`` marker already limits a test to at most the cap.

    A missing marker, a zero (pytest-timeout's "no timeout") or a non-numeric value does not.
    """
    if marker is None:
        return False
    seconds = marker.args[0] if marker.args else marker.kwargs.get("timeout")
    return isinstance(seconds, int | float) and 0 < seconds <= INTEGRATION_TIMEOUT_S


def _require_broker(mosquitto: list[pytest.Item]) -> None:
    """Probe the broker once and mark every ``mosquitto`` test to fail if it is unreachable."""
    host, port = broker_address(os.environ)
    if broker_reachable(host, port, BROKER_WAIT_S):
        return
    reason = f"broker unreachable at {host}:{port} within {BROKER_WAIT_S:g} s (--mosquitto)"
    for item in mosquitto:
        item.stash[_BROKER_UNREACHABLE] = reason


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply the integration timeout, the hardware skip and the mosquitto selection.

    An integration test's effective timeout is the smaller of its own ``timeout`` marker and
    ``INTEGRATION_TIMEOUT_S``: unless its own limit is already within the cap, the cap
    marker is put first so it becomes the closest ``timeout`` marker. Without
    ``--mosquitto`` the ``mosquitto`` tests are deselected; with it the broker is probed
    only when such a test was collected.
    """
    cap = pytest.mark.timeout(INTEGRATION_TIMEOUT_S)
    hardware_skip = pytest.mark.skip(reason="--hardware not given")
    mosquitto = [item for item in items if item.get_closest_marker("mosquitto")]
    for item in items:
        if item.get_closest_marker("integration") and not _within_cap(
            item.get_closest_marker("timeout")
        ):
            item.add_marker(cap, append=False)
        if item.get_closest_marker("hardware") and not config.getoption("--hardware"):
            item.add_marker(hardware_skip)
    if not mosquitto:
        return
    if config.getoption("--mosquitto"):
        _require_broker(mosquitto)
        return
    config.hook.pytest_deselected(items=mosquitto)
    items[:] = [item for item in items if not item.get_closest_marker("mosquitto")]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    """Fail a ``mosquitto`` test before it runs when the required broker is unreachable."""
    reason = item.stash.get(_BROKER_UNREACHABLE, None)
    if reason is not None:
        pytest.fail(reason, pytrace=False)


def _refuse_sleep(seconds: float) -> NoReturn:
    """Fail the current test; installed as ``time.sleep`` outside integration tests."""
    pytest.fail(
        f"time.sleep({seconds!r}) called in a test not marked integration; "
        "inject a sleep function or drive a FakeClock instead (TEST-11)"
    )


@pytest.fixture(autouse=True)
def _forbid_sleep_outside_integration(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace ``time.sleep`` for the duration of any test not marked ``integration``."""
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setattr(time, "sleep", _refuse_sleep)
