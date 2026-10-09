"""Checks against the real Dyness B3 stack on ``CAN_CHANNEL`` (TEST-38).

Every test here is marked ``hardware`` and skipped unless pytest is given ``--hardware``, so
it never runs in CI. The settings come from the environment exactly as the service reads
them, except that ``CAN_LISTEN_ONLY`` is forced to ``true`` unless ``--hardware-poll`` is
also given: by default nothing at all is transmitted. With ``--hardware-poll`` the only
frame sent is the module poll, through ``send_poll``, and the autouse ``tx_guard`` fixture
fails the test on anything else (TEST-28).

Run on the Pi with the service stopped, for example
``CAN_INTERFACE=socketcan CAN_CHANNEL=can0 uv run pytest tests/hardware --hardware``.
"""

import os
import time
from collections.abc import Iterator

import pytest

from dyness_b3_bms_mqtt import protocol
from dyness_b3_bms_mqtt.canbus import RECV_TIMEOUT_S, PythonCanTransport, RxFrame
from dyness_b3_bms_mqtt.config import Settings, load_settings

pytestmark = pytest.mark.hardware

CELL_STREAM_TIMEOUT_S = 5.0

type Seen = set[tuple[int, int]]


@pytest.fixture
def hardware_settings(request: pytest.FixtureRequest) -> Settings:
    """Return the environment's settings, listen-only unless ``--hardware-poll`` is given."""
    environ = dict(os.environ)
    if not request.config.getoption("--hardware-poll"):
        environ["CAN_LISTEN_ONLY"] = "true"
    return load_settings(environ)


@pytest.fixture
def poll_allowed(request: pytest.FixtureRequest, hardware_settings: Settings) -> None:
    """Skip the test unless ``--hardware-poll`` is given and the settings allow polling."""
    if not request.config.getoption("--hardware-poll"):
        pytest.skip("--hardware-poll not given")
    if hardware_settings.can_listen_only:
        pytest.skip("CAN_LISTEN_ONLY is true in the environment")


@pytest.fixture
def transport(hardware_settings: Settings) -> Iterator[PythonCanTransport]:
    """Yield an open transport on the real bus; it is shut down afterwards (CAN-24)."""
    with PythonCanTransport(hardware_settings, time.monotonic, time.sleep) as opened:
        opened.open()
        yield opened


def module_frames(transport: PythonCanTransport, seconds: float, want: Seen) -> Seen:
    """Receive for up to ``seconds`` and return the ``(module, code)`` pairs seen.

    Returns early once every pair in ``want`` has been seen.
    """
    seen: Seen = set()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not want <= seen:
        item = transport.recv(RECV_TIMEOUT_S)
        if not isinstance(item, RxFrame):
            continue
        cls = protocol.classify(item.arbitration_id, item.extended, item.data)
        if cls is protocol.FrameClass.MODULE:
            seen.add((item.arbitration_id >> 8 & 0xF, item.arbitration_id & 0xFF))
    return seen


@pytest.mark.req("TEST-38")
def test_broadcasts_from_every_module_within_grace(
    hardware_settings: Settings, transport: PythonCanTransport
) -> None:
    modules = range(1, hardware_settings.module_count + 1)
    want = {(m, code) for m in modules for code in protocol.BROADCAST_CODES}
    seen = module_frames(transport, hardware_settings.start_grace_s, want)
    assert want <= seen, sorted(want - seen)


@pytest.mark.req("TEST-38")
def test_cell_stream_follows_poll(
    poll_allowed: None, hardware_settings: Settings, transport: PythonCanTransport
) -> None:
    modules = range(1, hardware_settings.module_count + 1)
    want = {(m, code) for m in modules for code in protocol.CELL_CODES}
    seen: Seen = set()
    for module in modules:
        transport.send_poll(module)
        seen |= module_frames(transport, hardware_settings.poll_stagger_s, want)
    seen |= module_frames(transport, CELL_STREAM_TIMEOUT_S, want)
    assert want <= seen, sorted(want - seen)
