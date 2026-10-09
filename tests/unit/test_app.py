"""End-to-end tests of the application wiring and main loop (``app.py``).

Every test runs the real :class:`App` over the real ``PythonCanTransport`` on python-can's
``virtual`` interface, paced by the ``SimulatedStack`` on a ``FakeClock`` (TEST-22), with a
recording ``FakePublisher`` and a fresh state directory (TEST-29). Nothing sleeps: the
injected sleep advances the fake clock. Stops are scheduled on the simulator's timeline, so
a run always ends in simulated time; a loop that stopped advancing time would hit the
module's timeout instead of hanging the suite.
"""

import errno
import json
import logging
import os
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Any

import can
import pytest

from dyness_b3_bms_mqtt import __version__, alerts, protocol
from dyness_b3_bms_mqtt.app import (
    ALERT_INTERVAL_S,
    HA_JITTER_MAX_S,
    LINK_STATE_INTERVAL_S,
    App,
    Runtime,
    main,
    run,
)
from dyness_b3_bms_mqtt.canbus import RECV_TIMEOUT_S, CanTransport, PythonCanTransport
from dyness_b3_bms_mqtt.config import Settings, load_settings
from dyness_b3_bms_mqtt.discovery import build_discovery
from dyness_b3_bms_mqtt.health import HealthMachine
from dyness_b3_bms_mqtt.linkstate import CanState, LinkState
from dyness_b3_bms_mqtt.logsetup import HANDLER_NAME
from dyness_b3_bms_mqtt.mqtt import Topics
from tests.fakes.clock import FakeClock
from tests.fakes.flaky import FlakyFactory
from tests.fakes.notify import NotifySocket
from tests.fakes.publisher import FakePublisher
from tests.fakes.transport import RecordingTransport
from tests.sim.paced import paced_factory
from tests.sim.stack import SimulatedStack

pytestmark = pytest.mark.timeout(60)

WALL0 = 1791504000.0
PID = 4242
POLL_DATA = bytes([1, 0, 0, 0, 0, 0, 0, 0])
TOPICS = Topics(load_settings({}, hostname=lambda: "testhost"))
STATUS = TOPICS.status
SERVICE = TOPICS.service_state
STACK = TOPICS.stack_state
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
LOW_CELLS = [3300] * 7 + [2900] + [3300] * 7


@dataclass(frozen=True)
class Entry:
    """One call the publisher received while connected."""

    at: float
    kind: str
    topic: str
    payload: str | bytes
    qos: int
    retain: bool


class TimedPublisher(FakePublisher):
    """A ``FakePublisher`` that also stamps every call with the fake clock."""

    def __init__(self, clock: FakeClock, timeline: list[str]) -> None:
        super().__init__()
        self.clock = clock
        self.timeline = timeline
        self.log: list[Entry] = []

    def publish(self, topic: str, payload: str | bytes, *, qos: int, retain: bool) -> None:
        if self.connected:
            self.log.append(Entry(self.clock(), "publish", topic, payload, qos, retain))
            self.timeline.append(f"publish {topic}")
        super().publish(topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic: str, qos: int) -> None:
        if self.connected:
            self.log.append(Entry(self.clock(), "subscribe", topic, "", qos, False))
        super().subscribe(topic, qos)

    def stop(self) -> None:
        self.timeline.append("publisher.stop")
        super().stop()

    def payloads(self, topic: str) -> list[tuple[float, Any]]:
        return [(e.at, json.loads(e.payload)) for e in self.log if e.topic == topic]

    def times(self, topic: str) -> list[float]:
        return [e.at for e in self.log if e.topic == topic and e.kind == "publish"]


class ScriptedLink(RecordingTransport):
    """A recording transport whose ``link_state`` comes from ``script``."""

    def __init__(self, inner: CanTransport, script: Callable[[], LinkState]) -> None:
        super().__init__(inner)
        self.script = script

    def link_state(self) -> LinkState:
        return self.script()


@dataclass
class Rig:
    """Everything one application run is wired to."""

    clock: FakeClock
    sim: SimulatedStack
    env: dict[str, str]
    timeline: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    jitter: float = 2.0
    jitter_calls: list[tuple[float, float]] = field(default_factory=list)
    transports: list[RecordingTransport] = field(default_factory=list)
    flaky: FlakyFactory = field(init=False)
    publisher: TimedPublisher = field(init=False)
    t0: float = field(init=False)

    def __post_init__(self) -> None:
        self.flaky = FlakyFactory(paced_factory(self.sim), self.timeline)
        self.publisher = TimedPublisher(self.clock, self.timeline)
        self.t0 = self.clock()

    def settings(self) -> Settings:
        return load_settings(self.env, hostname=lambda: "testhost")

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def uniform(self, low: float, high: float) -> float:
        self.jitter_calls.append((low, high))
        return self.jitter

    def transport(
        self, settings: Settings, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> RecordingTransport:
        inner = PythonCanTransport(settings, clock, sleep, bus_factory=self.flaky)
        recording = RecordingTransport(inner)
        self.transports.append(recording)
        return recording

    def runtime(self, *, install_signals: bool = False) -> Runtime:
        return Runtime(
            clock=self.clock,
            wall=lambda: WALL0 + self.clock(),
            sleep=self.sleep,
            random_uniform=self.uniform,
            environ=self.env,
            pid=PID,
            transport_factory=self.transport,
            publisher_factory=lambda settings: self.publisher,
            install_signals=install_signals,
        )

    def app(self, *, install_signals: bool = False) -> App:
        return App(self.settings(), self.runtime(install_signals=install_signals))

    def at(self, seconds: float, fn: Callable[[], object]) -> None:
        """Call ``fn`` at ``seconds`` after the rig was built, in simulated time."""
        self.sim.schedule(self.t0 + seconds, fn)

    def run(self, seconds: float = 60.0) -> int:
        """Run a new application until a stop at ``seconds`` (or an earlier exit)."""
        app = self.app()
        self.at(seconds, app.request_stop)
        return app.run()

    def health(self) -> list[str]:
        """Return the published health values with consecutive repeats removed."""
        states: list[str] = []
        for _, payload in self.publisher.payloads(SERVICE):
            if not states or states[-1] != payload["health"]:
                states.append(payload["health"])
        return states


def make_rig(channel: str, clock: FakeClock, state_dir: Path, **env: str) -> Rig:
    base = {
        "CAN_INTERFACE": "virtual",
        "CAN_CHANNEL": channel,
        "STATE_DIRECTORY": str(state_dir),
    }
    return Rig(clock, SimulatedStack(channel, clock), {**base, **env})


@pytest.fixture
def rig(can_channel: str, clock: FakeClock, state_dir: Path) -> Iterator[Rig]:
    made = make_rig(can_channel, clock, state_dir)
    yield made
    made.sim.close()


@pytest.fixture
def restore_signals() -> Iterator[None]:
    """Restore the shutdown signal handlers an ``App`` installed."""
    saved = {sig: signal.getsignal(sig) for sig in SIGNALS}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Undo the root level and the stdout handler that ``run`` and ``main`` set up."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = [h for h in root.handlers if h.get_name() != HANDLER_NAME]
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def is_valid_poll(message: Any, module_count: int) -> bool:
    module = message.arbitration_id >> 8 & 0xF
    return bool(
        message.extended
        and message.arbitration_id & 0xFFFFF0FF == 0x18F21077
        and 1 <= module <= module_count
        and message.data == POLL_DATA
    )


def kill_self(sig: signal.Signals) -> Callable[[], None]:
    return lambda: os.kill(os.getpid(), sig)


def recovery(state_dir: Path) -> Any:
    return json.loads((state_dir / "recovery.json").read_text(encoding="utf-8"))


# Loop skeleton, exit codes and signals


@pytest.mark.req("SVC-11", "SVC-12", "SVC-09", "CAN-24", "TEST-29", "HEALTH-27")
def test_sigterm_clean_stop_exit_0(
    rig: Rig, restore_signals: None, notify_socket: NotifySocket
) -> None:
    rig.env["NOTIFY_SOCKET"] = notify_socket.path
    rig.publisher.set_connected(True)
    app = rig.app(install_signals=True)
    rig.at(30.0, kill_self(signal.SIGTERM))

    assert app.run() == 0

    assert rig.flaky.opened and rig.flaky.all_shut_down()
    assert rig.publisher.stopped
    assert [signal.getsignal(sig) for sig in SIGNALS] == [signal.SIG_IGN] * 3
    assert notify_socket.messages()[-1] == "STOPPING=1"
    assert rig.timeline[-5:] == [
        f"publish {STATUS}",
        f"publish {SERVICE}",
        "shutdown",
        "publisher.stop",
        f"publish {STATUS}",
    ]
    final_at, final = rig.publisher.payloads(SERVICE)[-1]
    assert final["health"] == "healthy"
    assert rig.t0 + 30.0 <= final_at <= rig.t0 + 30.0 + RECV_TIMEOUT_S + 1e-6
    offline = [e for e in rig.publisher.log if e.topic == STATUS][-2]
    assert (offline.payload, offline.qos, offline.retain) == ("offline", 1, True)


@pytest.mark.req("TEST-29", "HEALTH-27", "HEALTH-15", "SVC-13", "CAN-24")
def test_bus_silence_exit_75_then_76(rig: Rig, state_dir: Path) -> None:
    rig.at(30.0, rig.sim.silence_bus)

    assert rig.run(seconds=300.0) == 75
    assert recovery(state_dir)["escalation"] == 1
    assert rig.flaky.all_shut_down()
    assert len(rig.flaky.opened) == 3

    rig.t0 = rig.clock()
    assert rig.run(seconds=300.0) == 76
    assert recovery(state_dir)["escalation"] == 2
    assert rig.flaky.all_shut_down()
    assert len(rig.flaky.opened) == 6


@pytest.mark.req("TEST-29", "HEALTH-15", "HEALTH-27", "HEALTH-31")
def test_dead_mode_waits_for_periodic_usb_reset(rig: Rig, state_dir: Path) -> None:
    (state_dir / "recovery.json").write_text('{"escalation": 3, "restarts": 0}')
    rig.publisher.set_connected(True)
    rig.sim.silence_bus()

    assert rig.run(seconds=2000.0) == 76

    elapsed = rig.clock() - rig.t0
    assert 900.0 <= elapsed < 900.0 + 2 * RECV_TIMEOUT_S
    assert recovery(state_dir)["escalation"] == 3
    assert rig.health() == ["dead"]
    reopens = [t for t in rig.transports[0].calls if t[0] == "reopen"]
    assert len(reopens) == 14
    assert rig.flaky.all_shut_down()
    final = rig.publisher.payloads(SERVICE)[-1][1]
    assert (final["health"], final["health_reason"]) == ("dead", "dead_usb_reset")


@pytest.mark.req("CFG-03", "TEST-29", "HEALTH-27")
def test_invalid_config_exit_78_without_bus_or_broker(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    built: list[str] = []

    def no_transport(*args: object) -> CanTransport:
        built.append("transport")
        raise AssertionError("bus opened despite invalid configuration")

    def no_publisher(settings: Settings) -> FakePublisher:
        built.append("publisher")
        raise AssertionError("broker connection started despite invalid configuration")

    runtime = Runtime(
        clock=rig.clock,
        wall=rig.clock,
        sleep=rig.sleep,
        random_uniform=rig.uniform,
        environ=rig.env,
        pid=PID,
        transport_factory=no_transport,
        publisher_factory=no_publisher,
        install_signals=False,
    )
    with caplog.at_level(logging.INFO):
        assert run({**rig.env, "MODULE_COUNT": "9", "MQTT_PORT": "0"}, runtime) == 78
    assert built == []
    assert rig.flaky.kwargs == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("MODULE_COUNT" in line for line in errors)
    assert any("MQTT_PORT" in line for line in errors)


@pytest.mark.req("HEALTH-28", "TEST-29", "SVC-09", "CAN-24")
def test_unexpected_exception_exit_1_with_traceback(
    rig: Rig, notify_socket: NotifySocket, caplog: pytest.LogCaptureFixture
) -> None:
    rig.env["NOTIFY_SOCKET"] = notify_socket.path

    def explode() -> None:
        rig.publisher.explode_on_take_events = RuntimeError("boom")

    rig.at(5.0, explode)
    with caplog.at_level(logging.INFO):
        assert rig.run() == 1

    crash = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(crash) == 1
    assert crash[0].exc_info is not None
    assert "boom" in str(crash[0].exc_info[1])
    assert rig.flaky.all_shut_down()
    assert rig.publisher.stopped
    assert notify_socket.messages()[-1] == "STOPPING=1"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (errno.ENODEV, "STATUS=DEAD: adapter_absent"),
        (errno.EACCES, "STATUS=DEAD: os_error: "),
    ],
)
@pytest.mark.req("SVC-06", "HEALTH-17", "SVC-08")
def test_adapter_absent_starts_dead(
    rig: Rig, notify_socket: NotifySocket, code: int, status: str
) -> None:
    rig.env.update(NOTIFY_SOCKET=notify_socket.path, DEAD_RETRY_S="10")
    rig.flaky.fail_next_opens(1, code)

    assert rig.run(seconds=30.0) == 0

    messages = notify_socket.messages()
    statuses = [m for m in messages if m.startswith("STATUS=")]
    assert messages[0] == "READY=1"
    assert statuses[0].startswith(status)
    assert statuses[1] == "STATUS=STARTING: bus_resumed"
    assert len(rig.flaky.opened) == 1
    assert rig.flaky.all_shut_down()


@pytest.mark.req("PROTO-29", "HEALTH-08", "HEALTH-17")
def test_every_transmit_is_a_valid_poll(rig: Rig) -> None:
    rig.sim.echo_polls = True
    rig.at(30.0, lambda: rig.sim.stop_cell_stream(2))
    rig.at(60.0, rig.sim.silence_bus)
    rig.at(80.0, rig.sim.resume_bus)

    assert rig.run(seconds=120.0) == 0

    sent = rig.sim.received
    assert len(sent) >= 7
    assert all(is_valid_poll(message, 3) for message in sent)
    assert {message.arbitration_id >> 8 & 0xF for message in sent} == {1, 2, 3}
    after_stop = [m.arbitration_id for m in sent if rig.t0 + 30.0 < m.at < rig.t0 + 60.0]
    assert after_stop == [0x18F21277]
    after_resume = [m.arbitration_id for m in sent if m.at > rig.t0 + 80.0]
    assert after_resume[:2] == [0x18F21177, 0x18F21277]
    assert after_resume[2:] in ([], [0x18F21377])
    assert rig.transports[0].polls == [m.arbitration_id >> 8 & 0xF for m in sent]


@pytest.mark.req("PROTO-31", "CAN-14")
def test_listen_only_never_calls_build_poll(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def build_poll(module: int, module_count: int) -> protocol.PollFrame:
        calls.append((module, module_count))
        raise AssertionError("build_poll reached in listen-only mode")

    monkeypatch.setattr(protocol, "build_poll", build_poll)
    rig.env["CAN_LISTEN_ONLY"] = "true"
    rig.publisher.set_connected(True)
    rig.at(40.0, rig.sim.silence_bus)
    rig.at(60.0, rig.sim.resume_bus)

    assert rig.run(seconds=120.0) == 0

    assert calls == []
    assert rig.sim.received == []
    assert rig.health() == ["starting", "healthy", "degraded", "stale", "starting", "healthy"]
    final_m1 = rig.publisher.payloads(TOPICS.module_state(1))[-1][1]
    assert final_m1["online"] is True
    assert final_m1["cells_fresh"] is False


@pytest.mark.req("PROTO-05", "PROTO-04")
def test_malformed_frames_keep_bus_alive_not_module(rig: Rig) -> None:
    rig.publisher.set_connected(True)
    rig.sim.silence_bus()
    malformed = can.Message(arbitration_id=0x18F21121, is_extended_id=True, data=bytes(4))
    for n in range(120):
        rig.at(0.5 * n, lambda: rig.sim.bus.send(malformed))

    assert rig.run(seconds=60.0) == 0

    assert "stale" not in rig.health()
    assert rig.health()[-1] == "degraded"
    availability = [e.payload for e in rig.publisher.log if e.topic.endswith("1/availability")]
    assert "online" not in availability
    final_m1 = rig.publisher.payloads(TOPICS.module_state(1))[-1][1]
    assert (final_m1["online"], final_m1["bcast_age_s"]) == (False, None)
    final = rig.publisher.payloads(SERVICE)[-1][1]
    assert final["malformed_frames"] >= 115
    assert final["bus_age_s"] is not None and final["bus_age_s"] <= 1.0


@pytest.mark.req("HEALTH-33", "CAN-24", "HEALTH-27")
def test_recv_fault_does_not_spin(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    exceptions: list[str] = []
    original = HealthMachine.on_bus_exception

    def spy(machine: HealthMachine, now: float, detail: str) -> None:
        exceptions.append(detail)
        original(machine, now, detail)

    monkeypatch.setattr(HealthMachine, "on_bus_exception", spy)

    def fault() -> None:
        rig.flaky.latest.fail_recv(errno.ENETDOWN, once=False)
        rig.flaky.fail_next_opens(2)

    rig.at(30.0, fault)

    assert rig.run(seconds=300.0) == 75

    assert len(exceptions) == 1
    assert exceptions[0].startswith("link_down: ")
    calls = rig.transports[0].calls
    first_close = calls.index(("close", ()))
    assert ("recv", (RECV_TIMEOUT_S,)) not in calls[first_close:]
    idle = rig.sleeps.count(RECV_TIMEOUT_S)
    assert idle >= int((2 * 15.0 - 2 * 2.0) / RECV_TIMEOUT_S)
    assert rig.sleeps.count(2.0) == 2
    assert rig.flaky.all_shut_down()


@pytest.mark.req("HEALTH-03", "MQTT-06", "SVC-07")
def test_broker_down_does_not_affect_health_or_watchdog(
    can_channel: str, tmp_path: Path, notify_socket: NotifySocket
) -> None:
    other = NotifySocket()
    runs: list[tuple[int, list[str], list[tuple[float, int]], list[Entry]]] = []
    try:
        for name, sock, flapping in (("down", notify_socket, False), ("flap", other, True)):
            state = tmp_path / name
            state.mkdir()
            clock = FakeClock(1000.0)
            made = make_rig(
                f"{can_channel}-{name}",
                clock,
                state,
                NOTIFY_SOCKET=sock.path,
                WATCHDOG_USEC="3000000",
            )
            try:
                if flapping:
                    made.at(20.0, partial(made.publisher.set_connected, True))
                    made.at(50.0, partial(made.publisher.set_connected, False))
                    made.at(90.0, partial(made.publisher.set_connected, True))
                made.at(40.0, made.sim.silence_bus)
                made.at(65.0, made.sim.resume_bus)
                code = made.run(seconds=120.0)
                polls = [(m.at, m.arbitration_id) for m in made.sim.received]
                runs.append((code, sock.messages(), polls, made.publisher.log))
            finally:
                made.sim.close()
    finally:
        other.close()

    (code_a, notes_a, polls_a, log_a), (code_b, notes_b, polls_b, log_b) = runs
    assert code_a == code_b == 0
    assert notes_a == notes_b
    assert polls_a == polls_b
    assert log_a == [] and log_b
    statuses = [n for n in notes_a if n.startswith("STATUS=")]
    assert [s.split(":")[0] for s in statuses] == [
        "STATUS=STARTING",
        "STATUS=HEALTHY",
        "STATUS=DEGRADED",
        "STATUS=STALE",
        "STATUS=STARTING",
        "STATUS=HEALTHY",
    ]
    assert notes_a.count("WATCHDOG=1") >= 115


# Publishing: connect sequence, Home Assistant, cadence


@pytest.mark.req("MQTT-09", "HA-18", "HA-20")
def test_connect_sequence_order(rig: Rig) -> None:
    rig.at(5.0, lambda: rig.publisher.set_connected(True))

    assert rig.run(seconds=6.0) == 0

    discovery = build_discovery(rig.settings(), __version__)
    expected = [
        ("publish", STATUS, "online", 1, True),
        ("subscribe", "homeassistant/status", "", 1, False),
        *(("publish", topic, payload, 1, True) for topic, payload in discovery),
        *(("publish", TOPICS.module_availability(m), "online", 1, True) for m in (1, 2, 3)),
    ]
    log = rig.publisher.log
    assert [(e.kind, e.topic, e.payload, e.qos, e.retain) for e in log[: len(expected)]] == (
        expected
    )
    rest = [(e.topic, e.qos, e.retain) for e in log[len(expected) : len(expected) + 5]]
    assert rest == [
        (TOPICS.module_state(1), 0, False),
        (TOPICS.module_state(2), 0, False),
        (TOPICS.module_state(3), 0, False),
        (STACK, 0, False),
        (SERVICE, 0, False),
    ]
    assert {e.at for e in log[: len(expected) + 5]} == {log[0].at}
    assert log[0].at <= rig.t0 + 5.0 + RECV_TIMEOUT_S


@pytest.mark.req("MQTT-07", "MQTT-09")
def test_reconnect_republishes_and_counts(rig: Rig) -> None:
    for at, up in ((5.0, True), (10.0, False), (15.0, True), (20.0, False), (25.0, True)):
        rig.at(at, partial(rig.publisher.set_connected, up))

    assert rig.run(seconds=30.0) == 0

    online = [e for e in rig.publisher.log if e.topic == STATUS and e.payload == "online"]
    assert len(online) == 3
    assert len(rig.publisher.times(TOPICS.discovery_stack)) == 3
    services = rig.publisher.payloads(SERVICE)
    assert [p["mqtt_reconnects"] for at, p in services if at == online[1].at] == [1]
    assert services[-1][1]["mqtt_reconnects"] == 2


@pytest.mark.req("HA-19", "TEST-12", "MQTT-20")
def test_ha_online_jittered_single_republish(rig: Rig) -> None:
    rig.jitter = 3.0
    rig.publisher.set_connected(True)
    rig.at(10.0, lambda: rig.publisher.deliver(TOPICS.ha_status, "online"))
    rig.at(11.0, lambda: rig.publisher.deliver(TOPICS.ha_status, "online"))
    rig.at(12.0, lambda: rig.publisher.deliver(TOPICS.ha_status, "offline"))
    rig.at(20.0, lambda: rig.publisher.deliver(TOPICS.ha_status, b"online"))

    assert rig.run(seconds=30.0) == 0

    assert HA_JITTER_MAX_S == 5.0
    assert rig.jitter_calls == [(0.0, HA_JITTER_MAX_S), (0.0, HA_JITTER_MAX_S)]
    times = rig.publisher.times(TOPICS.discovery_stack)
    assert len(times) == 3
    assert rig.t0 + 13.0 <= times[1] <= rig.t0 + 13.0 + RECV_TIMEOUT_S
    assert rig.t0 + 23.0 <= times[2] <= rig.t0 + 23.0 + RECV_TIMEOUT_S
    burst = [e.topic for e in rig.publisher.log if e.at == times[1]]
    discovery = [topic for topic, _ in build_discovery(rig.settings(), __version__)]
    assert burst == [
        *discovery,
        *(TOPICS.module_availability(m) for m in (1, 2, 3)),
        *(TOPICS.module_state(m) for m in (1, 2, 3)),
        STACK,
        SERVICE,
    ]


@pytest.mark.req("HA-01")
def test_discovery_disabled(rig: Rig) -> None:
    rig.env["HA_DISCOVERY"] = "false"
    rig.publisher.set_connected(True)
    rig.at(10.0, lambda: rig.publisher.deliver(TOPICS.ha_status, "online"))

    assert rig.run(seconds=30.0) == 0

    assert rig.publisher.subscriptions == []
    assert not [e for e in rig.publisher.log if e.topic.startswith("homeassistant")]
    assert rig.jitter_calls == []
    topics = {e.topic for e in rig.publisher.log}
    assert {STATUS, STACK, SERVICE, TOPICS.module_availability(1)} <= topics


@pytest.mark.req("HA-20", "HA-19")
def test_surplus_module_removals(rig: Rig) -> None:
    rig.env["MODULE_COUNT"] = "2"
    rig.publisher.set_connected(True)
    rig.at(10.0, lambda: rig.publisher.deliver(TOPICS.ha_status, "online"))

    assert rig.run(seconds=20.0) == 0

    for module in (3, 4):
        removals = [e for e in rig.publisher.log if e.topic == TOPICS.discovery_module(module)]
        assert [(e.payload, e.qos, e.retain) for e in removals] == [("", 1, True)] * 2
        assert not [e for e in rig.publisher.log if f"/module/{module}/" in e.topic]
    assert len(rig.publisher.times(TOPICS.discovery_module(2))) == 2


@pytest.mark.req("MQTT-21", "HEALTH-03")
def test_nothing_published_while_disconnected(rig: Rig) -> None:
    rig.at(20.0, lambda: rig.publisher.set_connected(True))
    rig.at(30.0, lambda: rig.publisher.set_connected(False))
    rig.at(32.0, rig.sim.silence_bus)

    assert rig.run(seconds=60.0) == 0

    assert rig.publisher.dropped == []
    assert rig.publisher.log
    assert all(rig.t0 + 20.0 <= e.at <= rig.t0 + 30.0 + RECV_TIMEOUT_S for e in rig.publisher.log)


@pytest.mark.req("MQTT-19", "MQTT-20", "HEALTH-29")
def test_periodic_and_immediate_publishes(rig: Rig) -> None:
    rig.env["PUBLISH_INTERVAL_S"] = "10"
    rig.publisher.set_connected(True)
    rig.at(33.0, lambda: rig.sim.inject_error_frames(10))
    rig.at(40.0, lambda: rig.sim.silence_module(3))

    assert rig.run(seconds=65.0) == 0

    connect_at = rig.publisher.log[0].at
    stack_times = rig.publisher.times(STACK)
    for k in range(1, 7):
        due = rig.t0 + 10.0 * k
        assert any(due <= t <= due + RECV_TIMEOUT_S for t in stack_times), due
    services = rig.publisher.payloads(SERVICE)
    healthy_at = next(at for at, p in services if p["health"] == "healthy")
    assert healthy_at < rig.t0 + 10.0
    assert healthy_at != connect_at
    suspended_at = next(at for at, p in services if p["polling_suspended"])
    assert rig.t0 + 33.0 <= suspended_at <= rig.t0 + 34.0
    dropout = next(
        e
        for e in rig.publisher.log
        if e.topic == TOPICS.module_availability(3) and e.at > rig.t0 + 40.0
    )
    assert dropout.payload == "offline"
    assert dropout.at < rig.t0 + 52.0
    burst = [e.topic for e in rig.publisher.log if e.at == dropout.at]
    assert {TOPICS.module_state(3), STACK, SERVICE} <= set(burst)


@pytest.mark.req("ALERT-03", "ALERT-16", "ALERT-17")
def test_alerts_evaluated_independently_of_publish_interval(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    evaluated: list[float] = []
    original = alerts.evaluate

    def spy(*args: Any, **kwargs: Any) -> alerts.AlertState:
        evaluated.append(rig.clock())
        return original(*args, **kwargs)

    monkeypatch.setattr(alerts, "evaluate", spy)
    rig.env.update(PUBLISH_INTERVAL_S="300", ALERT_DEBOUNCE_S="2")
    rig.publisher.set_connected(True)
    rig.at(30.0, lambda: rig.sim.set_cells(2, LOW_CELLS))

    with caplog.at_level(logging.INFO):
        assert rig.run(seconds=40.0) == 0

    assert ALERT_INTERVAL_S == 0.5
    gaps = [b - a for a, b in pairwise(evaluated)]
    assert gaps and all(ALERT_INTERVAL_S <= gap <= 1.0 for gap in gaps)
    line = "alert module 2 cell_low: off -> on (value=2900 mV, threshold=3000 mV)"
    assert line in caplog.messages
    m2 = rig.publisher.payloads(TOPICS.module_state(2))
    on_at = next(at for at, p in m2 if p["alert_cell_low"])
    assert rig.t0 + 32.0 <= on_at <= rig.t0 + 35.0
    at_change = {e.topic for e in rig.publisher.log if e.at == on_at}
    assert at_change == {TOPICS.module_state(2), STACK}
    stack = dict(rig.publisher.payloads(STACK))[on_at]
    assert stack["alert_any_cell_low"] is True


@pytest.mark.req("CFG-11", "CFG-06", "ALERT-17", "SVC-11")
def test_info_logs(rig: Rig, restore_signals: None, caplog: pytest.LogCaptureFixture) -> None:
    rig.publisher.set_connected(True)
    rig.at(20.0, lambda: rig.sim.set_cells(2, LOW_CELLS))
    rig.at(90.0, kill_self(signal.SIGHUP))
    rig.env["ALERT_DEBOUNCE_S"] = "2"

    with caplog.at_level(logging.DEBUG):
        assert run(rig.env, rig.runtime(install_signals=True)) == 0
        assert logging.getLogger().level == logging.INFO

    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    expected = [
        f"dyness-b3-bms-mqtt {__version__} starting",
        "configuration: CAN_INTERFACE=virtual",
        f"configuration: CAN_CHANNEL={rig.env['CAN_CHANNEL']}",
        "configuration: MQTT_PASSWORD=(unset)",
        "health: none -> starting (start)",
        "poll module 1",
        "poll module 3",
        "health: starting -> healthy (all_ok: 3/3 modules ok)",
        "discovery published: 5 messages",
        "alert module 2 cell_low: off -> on (value=2900 mV, threshold=3000 mV)",
        "stopping on SIGHUP: exit status 0",
    ]
    for line in expected:
        assert line in info, line
    assert len(info) < 100


@pytest.mark.req("SVC-06", "SVC-07", "SVC-08", "SVC-09", "HEALTH-32")
def test_sd_notify_sequence(rig: Rig, notify_socket: NotifySocket) -> None:
    rig.env.update(NOTIFY_SOCKET=notify_socket.path, WATCHDOG_USEC="3000000", WATCHDOG_PID=str(PID))

    assert rig.run(seconds=20.0) == 0

    messages = notify_socket.messages()
    assert messages[0] == "READY=1"
    assert messages.count("READY=1") == 1
    assert [m for m in messages if m.startswith("STATUS=")] == [
        "STATUS=STARTING: start",
        "STATUS=HEALTHY: 3/3 modules ok",
    ]
    assert messages[-1] == "STOPPING=1"
    assert 19 <= messages.count("WATCHDOG=1") <= 21
    assert set(messages) == {
        "READY=1",
        "STATUS=STARTING: start",
        "STATUS=HEALTHY: 3/3 modules ok",
        "WATCHDOG=1",
        "STOPPING=1",
    }


@pytest.mark.req("CAN-12", "HEALTH-33")
def test_recv_timeout_constant(rig: Rig) -> None:
    assert rig.run(seconds=10.0) == 0

    assert RECV_TIMEOUT_S == 0.2
    timeouts = {args for name, args in rig.transports[0].calls if name == "recv"}
    assert timeouts == {(0.2,)}


@pytest.mark.req("CAN-31", "MQTT-18")
def test_link_state_every_10s_change_logged(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    queried: list[float] = []

    def link_state() -> LinkState:
        now = rig.clock()
        queried.append(now)
        if now < rig.t0 + 25.0:
            return LinkState(CanState.ERROR_ACTIVE, "up", False, 500000, 0, 0, 0, 0, 0, now)
        return LinkState(CanState.ERROR_PASSIVE, "up", False, 500000, 128, 3, 1, 0, 7, now)

    def transport(
        settings: Settings, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> CanTransport:
        return ScriptedLink(rig.transport(settings, clock, sleep), link_state)

    rig.publisher.set_connected(True)
    app = App(rig.settings(), replace(rig.runtime(), transport_factory=transport))
    rig.at(55.0, app.request_stop)
    with caplog.at_level(logging.INFO):
        assert app.run() == 0

    assert LINK_STATE_INTERVAL_S == 10.0
    assert len(queried) == 6
    assert queried[0] <= rig.t0 + RECV_TIMEOUT_S
    gaps = [b - a for a, b in pairwise(queried)]
    assert all(10.0 <= gap <= 10.0 + RECV_TIMEOUT_S + 1e-6 for gap in gaps)
    changes = [m for m in caplog.messages if m.startswith("can link state")]
    assert changes == [
        "can link state: unknown -> error-active",
        "can link state: error-active -> error-passive",
    ]
    final = rig.publisher.payloads(SERVICE)[-1][1]
    assert (final["can_state"], final["can_berr_tx"], final["can_berr_rx"]) == (
        "error-passive",
        128,
        3,
    )
    assert (final["can_restarts"], final["dropped_frames"]) == (1, 7)


@pytest.mark.req("HEALTH-30", "MQTT-12", "TEL-06", "HEALTH-21")
def test_availability_offline_during_stale(rig: Rig) -> None:
    rig.publisher.set_connected(True)
    rig.at(30.0, rig.sim.silence_bus)
    rig.at(50.0, rig.sim.resume_bus)

    assert rig.run(seconds=70.0) == 0

    for module in (1, 2, 3):
        topic = TOPICS.module_availability(module)
        changes: list[str | bytes] = []
        for e in rig.publisher.log:
            if e.topic == topic and (not changes or changes[-1] != e.payload):
                changes.append(e.payload)
        assert changes == ["offline", "online", "offline", "online"], module
    stale_at = next(at for at, p in rig.publisher.payloads(SERVICE) if p["health"] == "stale")
    at_stale = [e for e in rig.publisher.log if e.at == stale_at]
    assert [e.payload for e in at_stale if e.topic.endswith("/availability")] == ["offline"] * 3
    assert at_stale[0].topic == SERVICE
    after = min(e.at for e in rig.publisher.log if e.at > stale_at)
    assert after >= stale_at + 2.0
    for module in (1, 2, 3):
        during = [
            p
            for at, p in rig.publisher.payloads(TOPICS.module_state(module))
            if stale_at <= at < rig.t0 + 50.0
        ]
        assert during and all(p["online"] is False for p in during)


@pytest.mark.req("HEALTH-31", "SVC-13", "SVC-11")
def test_final_state_before_exit(rig: Rig, notify_socket: NotifySocket) -> None:
    rig.env["NOTIFY_SOCKET"] = notify_socket.path
    rig.publisher.set_connected(True)
    rig.at(20.0, rig.sim.silence_bus)

    assert rig.run(seconds=300.0) == 75

    assert rig.timeline[-5:] == [
        f"publish {STATUS}",
        f"publish {SERVICE}",
        "shutdown",
        "publisher.stop",
        f"publish {STATUS}",
    ]
    final = rig.publisher.payloads(SERVICE)[-1][1]
    assert final["health"] == "recovering"
    assert final["health_reason"].startswith("reopen_exhausted")
    assert final["escalation"] == 1
    assert notify_socket.messages()[-1] == "STOPPING=1"


@pytest.mark.req("CFG-06", "CFG-03", "HEALTH-27")
def test_main_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CAN_INTERFACE", "virtual")
    monkeypatch.setenv("CAN_CHANNEL", "x")
    monkeypatch.delenv("MODULE_COUNT", raising=False)

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__

    with caplog.at_level(logging.INFO):
        assert main(["--check-config"]) == 0
    assert "configuration: CAN_CHANNEL=x" in caplog.messages

    monkeypatch.setenv("MODULE_COUNT", "9")
    assert main(["--check-config"]) == 78

    assert main(["--bogus"]) == 78
    assert main(["--version", "--check-config"]) == 78
    assert any("unknown argument" in m for m in caplog.messages)
