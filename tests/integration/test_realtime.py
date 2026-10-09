"""The whole service against a threaded simulated stack in real time (TEST-30).

The ``SimulatedStack`` is driven from a background thread that keeps its ``FakeClock`` in
step with the real monotonic clock, so its frames arrive on the ``virtual`` bus at their
real rates. The ``App`` runs on the real clock and sleep with the real python-can
transport, publishing to a ``FakePublisher``; the test stops it once every module's cells
have been published.
"""

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dyness_b3_bms_mqtt.app import App, Runtime
from dyness_b3_bms_mqtt.canbus import PythonCanTransport
from dyness_b3_bms_mqtt.config import load_settings
from dyness_b3_bms_mqtt.mqtt import Topics
from tests.fakes.clock import FakeClock
from tests.fakes.publisher import FakePublisher
from tests.sim.stack import SimulatedStack

pytestmark = pytest.mark.integration

TICK_S = 0.01
CELLS_TIMEOUT_S = 6.0


class RealTimeDriver:
    """Advance ``sim`` in step with ``time.monotonic`` on a daemon thread until stopped."""

    def __init__(self, sim: SimulatedStack) -> None:
        self.sim = sim
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> RealTimeDriver:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(TICK_S)
            now = time.monotonic()
            self.sim.advance(now - last)
            last = now


@pytest.fixture
def sim(can_channel: str) -> Iterator[SimulatedStack]:
    with SimulatedStack(can_channel, FakeClock(time.monotonic())) as stack:
        yield stack


def cells_published(publisher: FakePublisher, topics: Topics, module: int) -> bool:
    """Return whether a state of ``module`` with all 15 cell voltages was published."""
    topic = topics.module_state(module)
    for message in list(publisher.published):
        if message.topic != topic:
            continue
        payload: Any = json.loads(message.payload)
        if all(payload[f"cell_{n:02d}_mv"] is not None for n in range(1, 16)):
            return True
    return False


@pytest.mark.req("TEST-30", "MQTT-19", "HEALTH-08")
def test_realtime_cells_published(sim: SimulatedStack, can_channel: str, tmp_path: Path) -> None:
    env = {
        "CAN_INTERFACE": "virtual",
        "CAN_CHANNEL": can_channel,
        "PUBLISH_INTERVAL_S": "1",
        "STATE_DIRECTORY": str(tmp_path),
    }
    settings = load_settings(env, hostname=lambda: "testhost")
    topics = Topics(settings)
    publisher = FakePublisher(settings)
    publisher.set_connected(True)
    runtime = Runtime(
        clock=time.monotonic,
        wall=time.time,
        sleep=time.sleep,
        random_uniform=lambda low, high: low,
        environ=env,
        pid=0,
        transport_factory=PythonCanTransport,
        publisher_factory=lambda _: publisher,
        install_signals=False,
    )
    app = App(settings, runtime)
    codes: list[int] = []
    worker = threading.Thread(target=lambda: codes.append(app.run()), daemon=True)

    with RealTimeDriver(sim):
        worker.start()
        deadline = time.monotonic() + CELLS_TIMEOUT_S
        while time.monotonic() < deadline and not all(
            cells_published(publisher, topics, m) for m in (1, 2, 3)
        ):
            time.sleep(0.05)
        app.request_stop()
        worker.join(timeout=2.0)

    assert all(cells_published(publisher, topics, m) for m in (1, 2, 3))
    assert codes == [0]
    assert sorted({r.arbitration_id for r in sim.received}) == [0x18F21177, 0x18F21277, 0x18F21377]
    assert publisher.stopped
