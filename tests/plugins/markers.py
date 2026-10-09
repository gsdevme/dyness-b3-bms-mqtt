"""Run-time behaviour of the suite's markers (TEST-01, TEST-02, TEST-03, TEST-11).

The markers are registered in ``pyproject.toml``; this plugin gives them their meaning:

* ``hardware`` tests are skipped unless pytest is given ``--hardware``. ``--hardware-poll``
  additionally allows them to transmit the module poll; without it they stay listen-only
  (TEST-38).
* ``mosquitto`` tests are skipped unless a TCP connection to ``MQTT_HOST``:``MQTT_PORT``
  succeeds within ``BROKER_CONNECT_TIMEOUT_S``.
* ``integration`` tests may sleep and are capped at ``INTEGRATION_TIMEOUT_S`` by a
  pytest-timeout ``timeout`` marker; a stricter per-test ``timeout`` is kept. Every other
  test has ``time.sleep`` replaced by a function that fails the test.

Every skip carries its reason so ``-ra`` reports why the test did not run.
"""

import os
import socket
import time
from collections.abc import Mapping
from typing import NoReturn

import pytest

INTEGRATION_TIMEOUT_S = 10.0
BROKER_CONNECT_TIMEOUT_S = 1.0
DEFAULT_MQTT_HOST = "localhost"
DEFAULT_MQTT_PORT = 1883


def broker_address(environ: Mapping[str, str]) -> tuple[str, int]:
    """Return the ``(host, port)`` of the broker the ``mosquitto`` tests use.

    Read from ``MQTT_HOST`` and ``MQTT_PORT``. An unset or empty variable falls back to the
    service default from ``docs/specs/08-configuration.md``.
    """
    host = environ.get("MQTT_HOST") or DEFAULT_MQTT_HOST
    port = int(environ.get("MQTT_PORT") or DEFAULT_MQTT_PORT)
    return host, port


def broker_reachable(host: str, port: int) -> bool:
    """Return whether a TCP connection to ``host``:``port`` succeeds within the timeout."""
    try:
        with socket.create_connection((host, port), timeout=BROKER_CONNECT_TIMEOUT_S):
            return True
    except OSError:
        return False


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--hardware`` and ``--hardware-poll``."""
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


def _within_cap(marker: pytest.Mark | None) -> bool:
    """Return whether a ``timeout`` marker already limits a test to at most the cap.

    A missing marker, a zero (pytest-timeout's "no timeout") or a non-numeric value does not.
    """
    if marker is None:
        return False
    seconds = marker.args[0] if marker.args else marker.kwargs.get("timeout")
    return isinstance(seconds, int | float) and 0 < seconds <= INTEGRATION_TIMEOUT_S


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply the integration timeout and the hardware and mosquitto skips.

    An integration test's effective timeout is the smaller of its own ``timeout`` marker and
    ``INTEGRATION_TIMEOUT_S``: unless its own limit is already within the cap, the cap
    marker is put first so it becomes the closest ``timeout`` marker. The broker is probed
    at most once per session, and only when a ``mosquitto`` test was collected.
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
    if mosquitto:
        host, port = broker_address(os.environ)
        if not broker_reachable(host, port):
            broker_skip = pytest.mark.skip(reason=f"broker unreachable at {host}:{port}")
            for item in mosquitto:
                item.add_marker(broker_skip)


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
