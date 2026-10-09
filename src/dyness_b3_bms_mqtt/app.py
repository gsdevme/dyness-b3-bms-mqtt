"""The service process: wiring, the main loop, signals and exit codes (``09-service.md``).

:func:`main` is the console entry point. It answers ``--version`` and ``--check-config``
itself and otherwise hands the process environment to :func:`run`, which validates the
configuration (exit 78 without touching the bus or the broker, CFG-03) and runs an
:class:`App`.

:class:`App` is one synchronous loop. Each iteration receives at most one frame with the
fixed :data:`~dyness_b3_bms_mqtt.canbus.RECV_TIMEOUT_S` (or idles that long while the bus is
closed after a fault), handles the MQTT events, steps the health machine and carries out its
actions in order, evaluates alerts every :data:`ALERT_INTERVAL_S`, publishes on the
``PUBLISH_INTERVAL_S`` schedule, appends the local history every ``LOCAL_LOG_INTERVAL_S``
and runs its housekeeping every :data:`~dyness_b3_bms_mqtt.localog.HOUSEKEEPING_INTERVAL_S`,
queries the link state every :data:`LINK_STATE_INTERVAL_S` and finally feeds the systemd
watchdog, so only a completed iteration can keep the service alive (HEALTH-32). Health
transitions, alert changes, polls and the exit are logged and also appended to the local
events file (LOG-09).

Everything with a side effect outside the process comes from :class:`Runtime`: the clocks,
the sleep, the random source, the environment, the transport and the publisher. Tests build
one around fakes; :meth:`Runtime.production` builds the real one.

The process exits only with 0 (clean stop on a signal), 1 (unhandled exception), 75 or 76
(the health machine's ``Exit`` action) and 78 (invalid configuration). Every exit path shuts
the CAN bus down (CAN-24).
"""

import logging
import os
import random
import signal
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import FrameType
from typing import Final, Self

from dyness_b3_bms_mqtt import __version__, alerts, protocol
from dyness_b3_bms_mqtt.canbus import (
    RECV_TIMEOUT_S,
    CanTransport,
    FaultKind,
    PythonCanTransport,
    RxErrorFrame,
    RxFrame,
    TransportFault,
)
from dyness_b3_bms_mqtt.config import (
    EX_CONFIG,
    ConfigError,
    Settings,
    check_config,
    describe,
    load_settings,
)
from dyness_b3_bms_mqtt.discovery import build_discovery
from dyness_b3_bms_mqtt.health import (
    ADAPTER_ABSENT,
    Action,
    Exit,
    HealthMachine,
    Poll,
    ReopenBus,
    SetAvailability,
    SetEscalation,
    SuspendPolling,
    Transition,
)
from dyness_b3_bms_mqtt.linkstate import LinkState, unknown_link_state
from dyness_b3_bms_mqtt.localog import HOUSEKEEPING_INTERVAL_S, LocalLog
from dyness_b3_bms_mqtt.logsetup import configure_logging
from dyness_b3_bms_mqtt.mqtt import OFFLINE, ONLINE, MqttEvent, PahoPublisher, Publisher
from dyness_b3_bms_mqtt.payloads import (
    ServiceInfo,
    encode,
    module_payload,
    service_payload,
    stack_payload,
)
from dyness_b3_bms_mqtt.protocol import FrameClass, FrameCounters
from dyness_b3_bms_mqtt.sdnotify import Notifier
from dyness_b3_bms_mqtt.state_store import RecoveryRecord, StateStore
from dyness_b3_bms_mqtt.telemetry import Snapshot, TelemetryStore
from dyness_b3_bms_mqtt.topics import Topics

LINK_STATE_INTERVAL_S: Final = 10.0
"""How often the main loop queries the kernel's view of the CAN link (CAN-31)."""

ALERT_INTERVAL_S: Final = 0.5
"""Minimum gap between alert evaluations; at most a second apart (ALERT-03)."""

HA_JITTER_MAX_S: Final = 5.0
"""Upper bound of the random delay before a Home Assistant republish (HA-19)."""

EXIT_OK: Final = 0
EXIT_CRASH: Final = 1
STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
USAGE: Final = "dyness-b3-bms-mqtt [--version | --check-config]"
STATE_QOS: Final = 0
RETAINED_QOS: Final = 1

_log = logging.getLogger(__name__)

type Clock = Callable[[], float]
type Sleep = Callable[[float], None]
type TransportFactory = Callable[[Settings, Clock, Sleep], CanTransport]
type PublisherFactory = Callable[[Settings], Publisher]


@dataclass(frozen=True)
class Runtime:
    """Everything the application takes from the outside world.

    ``clock`` is monotonic seconds for every age and timer; ``wall`` is Unix seconds, used
    only for published and persisted timestamps. ``sleep`` is the only way the application
    waits. ``random_uniform(a, b)`` is the jitter source (TEST-12). ``environ`` supplies
    ``NOTIFY_SOCKET``, ``WATCHDOG_*`` and ``STATE_DIRECTORY``; ``pid`` is this process.
    ``transport_factory(settings, clock, sleep)`` and ``publisher_factory(settings)`` build
    the CAN transport and the MQTT publisher without opening or connecting anything.
    ``install_signals`` installs the SIGTERM, SIGINT and SIGHUP handlers; tests that drive
    :meth:`App.request_stop` themselves turn it off.
    """

    clock: Clock
    wall: Clock
    sleep: Sleep
    random_uniform: Callable[[float, float], float]
    environ: Mapping[str, str]
    pid: int
    transport_factory: TransportFactory
    publisher_factory: PublisherFactory
    install_signals: bool = True

    @classmethod
    def production(cls) -> Self:
        """Return the runtime of the real service: real clocks, python-can and paho."""
        return cls(
            clock=time.monotonic,
            wall=time.time,
            sleep=time.sleep,
            random_uniform=random.uniform,
            environ=os.environ,
            pid=os.getpid(),
            transport_factory=PythonCanTransport,
            publisher_factory=PahoPublisher,
        )


class App:
    """One run of the service for validated ``settings``; see the module docstring.

    Construct it, then call :meth:`run` once. :meth:`request_stop` may be called at any
    time, from a signal handler or another thread; it only sets a flag that the loop checks
    at the start of every iteration.
    """

    def __init__(self, settings: Settings, runtime: Runtime) -> None:
        """Read the stored recovery state and build the collaborators.

        Nothing is opened or connected yet. The ``restarts`` count is incremented and saved
        when ``last_exit.json`` records a previous exit with a code other than 0, a missing
        code included (HEALTH-24, SVC-04).
        """
        self._settings = settings
        self._runtime = runtime
        self._clock = runtime.clock
        self._topics = Topics(settings)
        self._modules = tuple(range(1, settings.module_count + 1))
        self._notifier = Notifier(runtime.environ, runtime.pid)
        self._store = StateStore.from_environ(runtime.environ, runtime.wall)
        record = self._store.load_recovery()
        self._last_exit = self._store.load_last_exit()
        self._restarts = record.restarts
        if self._last_exit is not None and self._last_exit.code != 0:
            self._restarts += 1
            self._store.save_recovery(RecoveryRecord(record.escalation, self._restarts))
        self._health = HealthMachine(settings, record.escalation)
        self._transport = runtime.transport_factory(settings, runtime.clock, runtime.sleep)
        self._publisher = runtime.publisher_factory(settings)
        self._telemetry = TelemetryStore(settings)
        self._alerts = alerts.AlertState.initial(settings.module_count)
        self._history = LocalLog(settings, self._store.directory, runtime.wall)
        self._next_history = 0.0
        self._next_housekeeping = 0.0
        self._thresholds = alerts.AlertThresholds.from_settings(settings)
        self._next_alerts = 0.0
        self._counters = FrameCounters()
        self._availability = dict.fromkeys(self._modules, False)
        self._link: LinkState = unknown_link_state(runtime.clock())
        self._stop_signal: int | None = None
        self._stop_requested = False
        self._bus_open = False
        self._started_at = 0.0
        self._last_activity: float | None = None
        self._error_frames = 0
        self._polls_sent = 0
        self._connects = 0
        self._next_publish = 0.0
        self._ha_republish_at: float | None = None
        self._next_link = 0.0

    def request_stop(self, signum: int | None = None, frame: FrameType | None = None) -> None:
        """Ask the loop to run the clean-stop sequence; safe from a signal handler (SVC-11)."""
        self._stop_signal = signum
        self._stop_requested = True

    def run(self) -> int:
        """Run until a signal, an ``Exit`` action or a crash; return the exit status.

        The bus is shut down on every path, including an unhandled exception, which is
        logged with its traceback and gives status 1 (HEALTH-28, CAN-24).
        """
        if self._runtime.install_signals:
            for sig in STOP_SIGNALS:
                signal.signal(sig, self.request_stop)
        try:
            code = self._serve()
        except Exception:
            message = f"unhandled exception; exiting with status {EXIT_CRASH}"
            _log.exception("%s", message)
            self._ignore_signals()
            self._notifier.stopping()
            self._close_history(message)
            code = EXIT_CRASH
        finally:
            self._transport.close()
            self._stop_publisher()
        return code

    def _serve(self) -> int:
        """Start up, loop until told to stop, then run the clean-stop sequence."""
        self._start()
        while True:
            if self._stop_requested:
                message = f"stopping on {self._stop_cause()}: exit status {EXIT_OK}"
                return self._clean_stop(EXIT_OK, None, message)
            exit_action = self._iterate()
            if exit_action is not None:
                code, reason = exit_action.code, exit_action.reason
                return self._clean_stop(code, reason, f"exiting with status {code}: {reason}")

    def _stop_cause(self) -> str:
        """Return the name of the signal that asked for the stop, or ``stop request``."""
        if self._stop_signal is None:
            return "stop request"
        try:
            return signal.Signals(self._stop_signal).name
        except ValueError:
            return f"signal {self._stop_signal}"

    def _start(self) -> None:
        """Run housekeeping, open the bus, start health and MQTT, then tell systemd (SVC-06).

        Housekeeping comes first so the log directory exists before any write (LOG-13).
        """
        now = self._clock()
        self._started_at = now
        self._history.housekeep(self._runtime.wall())
        self._next_housekeeping = now + HOUSEKEEPING_INTERVAL_S
        self._next_history = now + self._settings.local_log_interval_s
        open_ok, detail = self._first_open()
        self._health.start(self._clock(), open_ok, detail)
        self._publisher.start()
        self._notifier.ready()
        self._next_publish = now + self._settings.publish_interval_s

    def _first_open(self) -> tuple[bool, str]:
        """Open the bus once; return ``(ok, detail)`` for :meth:`HealthMachine.start`."""
        try:
            self._transport.open()
        except TransportFault as fault:
            _log.warning("can bus open failed: %s", fault)
            if fault.kind is FaultKind.DEVICE_GONE:
                return False, ADAPTER_ABSENT
            return False, _detail(fault)
        self._bus_open = True
        return True, ""

    def _iterate(self) -> Exit | None:
        """Run one main-loop iteration; return the ``Exit`` action if one was due."""
        self._receive()
        now = self._clock()
        self._handle_mqtt_events(now)
        exit_action = self._carry_out(self._health.step(now))
        if exit_action is not None:
            return exit_action
        now = self._clock()
        self._evaluate_alerts(now)
        self._publish_periodic(now)
        self._write_history(now)
        self._housekeep(now)
        self._query_link(now)
        self._notifier.watchdog(self._clock())
        return None

    def _query_link(self, now: float) -> None:
        """Query the link state every ``LINK_STATE_INTERVAL_S``; log a change (CAN-31)."""
        if now < self._next_link:
            return
        self._next_link = now + LINK_STATE_INTERVAL_S
        previous, self._link = self._link, self._transport.link_state()
        if self._link.state is not previous.state:
            _log.info("can link state: %s -> %s", previous.state.value, self._link.state.value)

    def _evaluate_alerts(self, now: float) -> None:
        """Evaluate alerts every ``ALERT_INTERVAL_S``; log and publish changes (ALERT-03, -16).

        Only the modules whose alerts changed are published, with the stack state.
        """
        if now < self._next_alerts:
            return
        self._next_alerts = now + ALERT_INTERVAL_S
        snapshot = self._snapshot(now)
        previous = self._alerts
        self._alerts = alerts.evaluate(snapshot, previous, now, self._thresholds)
        found = alerts.changes(previous, self._alerts)
        for change in found:
            self._event(alerts.format_change(change, self._thresholds))
        if found:
            modules = sorted({c.scope for c in found if isinstance(c.scope, int)})
            self._publish_states(now, modules, service=False)

    def _receive(self) -> None:
        """Receive and dispatch at most one frame, or idle while the bus is closed."""
        if not self._bus_open:
            self._runtime.sleep(RECV_TIMEOUT_S)
            return
        try:
            item = self._transport.recv(RECV_TIMEOUT_S)
        except TransportFault as fault:
            self._bus_fault(fault)
            return
        if isinstance(item, RxErrorFrame):
            self._error_frames += 1
            self._health.on_error_frame(item.at)
        elif isinstance(item, RxFrame):
            self._frame(item)

    def _frame(self, frame: RxFrame) -> None:
        """Classify one frame, feed the health machine and the telemetry store (PROTO-05)."""
        cls = protocol.classify(frame.arbitration_id, frame.extended, frame.data)
        self._counters.count(cls)
        if cls is FrameClass.ECHO:
            return
        self._last_activity = frame.at
        if cls is FrameClass.MALFORMED:
            self._health.on_frame(protocol.HEARTBEAT_ID, frame.at)
            return
        self._health.on_frame(frame.arbitration_id, frame.at)
        decoded = protocol.decode(frame.arbitration_id, frame.extended, frame.data)
        if decoded is not None:
            self._telemetry.ingest(decoded, frame.at)

    def _bus_fault(self, fault: TransportFault) -> None:
        """Report a receive or send fault once, close the bus and idle one receive timeout."""
        _log.warning("can bus fault: %s", fault)
        self._health.on_bus_exception(self._clock(), _detail(fault))
        self._transport.close()
        self._bus_open = False
        self._runtime.sleep(RECV_TIMEOUT_S)

    def _carry_out(self, actions: Sequence[Action]) -> Exit | None:
        """Carry out the health machine's actions in the order returned (HEALTH-21)."""
        for action in actions:
            match action:
                case Transition():
                    self._transition(action)
                case SetAvailability(module=module, online=online):
                    self._set_availability(module, online)
                case SetEscalation(value=value):
                    self._store.save_recovery(RecoveryRecord(value, self._restarts))
                case SuspendPolling(reason=reason):
                    _log.warning("%s", reason)
                    self._publish_service(self._clock())
                case Poll(module=module):
                    self._poll(module)
                case ReopenBus():
                    self._reopen()
                case Exit():
                    return action
        return None

    def _transition(self, action: Transition) -> None:
        """Log a state change, publish the service state and update ``STATUS=`` (SVC-08)."""
        old = "none" if action.old is None else action.old.value
        self._event(f"health: {old} -> {action.new.value} ({action.reason})")
        summary = action.reason.split(": ", 1)[-1]
        self._notifier.status(f"{action.new.name}: {summary}")
        self._publish_service(self._clock())

    def _set_availability(self, module: int, online: bool) -> None:
        """Record and publish a module's availability with its state and the stack's."""
        self._availability[module] = online
        if not self._publisher.connected:
            return
        self._publish_availability(module)
        snapshot = self._snapshot(self._clock())
        ts = self._runtime.wall()
        self._publish_module(module, snapshot, ts)
        self._publish_stack(snapshot, ts)

    def _poll(self, module: int) -> None:
        """Send one poll; a full transmit queue is only a warning (03 section 5.1).

        Nothing is sent once a fault earlier in the same step has closed the bus, so one
        fault is reported to the health machine once.
        """
        if not self._bus_open:
            return
        try:
            self._transport.send_poll(module)
        except TransportFault as fault:
            if fault.kind is FaultKind.TX_BLOCKED:
                _log.warning("poll module %d not sent: %s", module, fault)
                return
            self._bus_fault(fault)
            return
        self._polls_sent += 1
        self._event(f"poll module {module}")

    def _reopen(self) -> None:
        """Close, wait and reopen the bus, then report the result (the ``ReopenBus`` action)."""
        _log.info("reopening the can bus")
        try:
            self._transport.reopen()
        except TransportFault as fault:
            _log.warning("can bus reopen failed: %s", fault)
            self._bus_open = False
            self._health.on_bus_open(self._clock(), False, _detail(fault))
            return
        self._bus_open = True
        self._health.on_bus_open(self._clock(), True, "")

    def _handle_mqtt_events(self, now: float) -> None:
        """Act on what the publisher reported since the last iteration (MQTT-05, MQTT-09)."""
        events = self._publisher.take_events()
        if MqttEvent.CONNECTED in events:
            self._connects += 1
            self._on_connect(now)
        if (
            MqttEvent.HA_ONLINE in events
            and self._settings.ha_discovery
            and self._ha_republish_at is None
        ):
            self._ha_republish_at = now + self._runtime.random_uniform(0.0, HA_JITTER_MAX_S)
        if self._ha_republish_at is not None and now >= self._ha_republish_at:
            self._ha_republish_at = None
            self._publish_discovery()
            self._publish_everything(now)

    def _on_connect(self, now: float) -> None:
        """Publish the MQTT-09 connect sequence."""
        self._publish(self._topics.status, ONLINE, qos=RETAINED_QOS, retain=True)
        if self._settings.ha_discovery:
            self._publisher.subscribe(self._topics.ha_status, RETAINED_QOS)
        self._publish_discovery()
        self._publish_everything(now)

    def _publish_discovery(self) -> None:
        """Publish every discovery message and surplus removal, if enabled (HA-01, HA-20)."""
        if not self._settings.ha_discovery or not self._publisher.connected:
            return
        messages = build_discovery(self._settings, __version__)
        for topic, payload in messages:
            self._publish(topic, payload, qos=RETAINED_QOS, retain=True)
        _log.info("discovery published: %d messages", len(messages))

    def _publish_everything(self, now: float) -> None:
        """Publish every availability, every module state, the stack and the service."""
        for module in self._modules:
            self._publish_availability(module)
        self._publish_states(now, self._modules)

    def _publish_periodic(self, now: float) -> None:
        """Publish all state on the ``PUBLISH_INTERVAL_S`` schedule (MQTT-19)."""
        if now < self._next_publish:
            return
        self._next_publish = _next_due(self._next_publish, now, self._settings.publish_interval_s)
        self._publish_states(now, self._modules)

    def _write_history(self, now: float) -> None:
        """Append the history rows every ``LOCAL_LOG_INTERVAL_S``, connected or not (LOG-11)."""
        if now < self._next_history:
            return
        interval = self._settings.local_log_interval_s
        self._next_history = _next_due(self._next_history, now, interval)
        self._history.write(self._snapshot(now), self._alerts, self._runtime.wall())

    def _housekeep(self, now: float) -> None:
        """Run the local history housekeeping every ``HOUSEKEEPING_INTERVAL_S`` (LOG-13)."""
        if now < self._next_housekeeping:
            return
        self._next_housekeeping = _next_due(self._next_housekeeping, now, HOUSEKEEPING_INTERVAL_S)
        self._history.housekeep(self._runtime.wall())

    def _event(self, message: str) -> None:
        """Log ``message`` at ``INFO`` and append it to the local events file (LOG-09)."""
        _log.info("%s", message)
        self._history.event(self._runtime.wall(), message)

    def _publish_states(self, now: float, modules: Iterable[int], *, service: bool = True) -> None:
        """Publish the given module states, the stack state and optionally the service state."""
        if not self._publisher.connected:
            return
        snapshot = self._snapshot(now)
        ts = self._runtime.wall()
        for module in modules:
            self._publish_module(module, snapshot, ts)
        self._publish_stack(snapshot, ts)
        if service:
            self._publish_service(now)

    def _snapshot(self, now: float) -> Snapshot:
        """Return the telemetry as of ``now`` with the published module statuses (TEL-06)."""
        return self._telemetry.snapshot(now, self._health.published_statuses())

    def _publish_availability(self, module: int) -> None:
        online = ONLINE if self._availability[module] else OFFLINE
        self._publish(
            self._topics.module_availability(module), online, qos=RETAINED_QOS, retain=True
        )

    def _publish_module(self, module: int, snapshot: Snapshot, ts: float) -> None:
        payload = module_payload(
            snapshot.modules[module - 1],
            self._alerts.module(module),
            self._settings.module_name(module),
            ts,
        )
        self._publish(self._topics.module_state(module), encode(payload), qos=STATE_QOS)

    def _publish_stack(self, snapshot: Snapshot, ts: float) -> None:
        payload = stack_payload(snapshot.stack, self._alerts.stack, ts)
        self._publish(self._topics.stack_state, encode(payload), qos=STATE_QOS)

    def _publish_service(self, now: float, reason: str | None = None) -> None:
        """Publish the service state; ``reason`` overrides ``health_reason`` (HEALTH-31)."""
        if not self._publisher.connected:
            return
        payload = service_payload(self._service_info(now, reason), self._runtime.wall())
        self._publish(self._topics.service_state, encode(payload), qos=STATE_QOS)

    def _service_info(self, now: float, reason: str | None) -> ServiceInfo:
        link = self._link
        last_exit = self._last_exit
        return ServiceInfo(
            version=__version__,
            health=self._health.state,
            health_reason=self._health.reason if reason is None else reason,
            can_state=link.state,
            can_berr_tx=link.berr_tx,
            can_berr_rx=link.berr_rx,
            can_restarts=link.restarts,
            bus_age_s=None if self._last_activity is None else now - self._last_activity,
            error_frames=self._error_frames,
            dropped_frames=link.rx_dropped,
            echo_frames=self._counters.echo_frames,
            malformed_frames=self._counters.malformed_frames,
            unknown_frames=self._counters.unknown_frames,
            polls_sent=self._polls_sent,
            polling_suspended=self._health.polling_suspended,
            escalation=self._health.escalation,
            service_restarts=self._restarts,
            last_exit_code=None if last_exit is None else last_exit.code,
            last_exit_result=None if last_exit is None else last_exit.result,
            uptime_s=now - self._started_at,
            mqtt_reconnects=max(self._connects - 1, 0),
            local_log=self._history.status,
        )

    def _publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
        """Publish one message, never while disconnected (MQTT-21)."""
        if self._publisher.connected:
            self._publisher.publish(topic, payload, qos=qos, retain=retain)

    def _clean_stop(self, code: int, reason: str | None, message: str) -> int:
        """Log ``message``, run the SVC-11 steps up to the bus shutdown and return ``code``.

        ``STOPPING=1`` goes first; then the retained ``offline`` status and the final service
        state are published and the local history is closed with ``message`` as its exit
        event (LOG-10). :meth:`run` then shuts the bus down and stops the publisher.
        """
        _log.info("%s", message)
        self._ignore_signals()
        self._notifier.stopping()
        self._publish(self._topics.status, OFFLINE, qos=RETAINED_QOS, retain=True)
        self._publish_service(self._clock(), reason)
        self._close_history(message)
        return code

    def _close_history(self, message: str) -> None:
        """Close the local history with its exit event; a failure must not mask the exit."""
        try:
            self._history.close(message)
        except Exception:
            _log.exception("local log close failed")

    def _ignore_signals(self) -> None:
        """Ignore further stop signals while shutting down (SVC-12)."""
        if self._runtime.install_signals:
            for sig in STOP_SIGNALS:
                signal.signal(sig, signal.SIG_IGN)

    def _stop_publisher(self) -> None:
        """Stop MQTT; a failure here must not mask the exit status."""
        try:
            self._publisher.stop()
        except Exception:
            _log.exception("mqtt stop failed")


def _next_due(due: float, now: float, interval: float) -> float:
    """Return the deadline one ``interval`` after ``due``, or after ``now`` if that has passed."""
    due += interval
    return due if due > now else now + interval


def _detail(fault: TransportFault) -> str:
    """Return the health-machine detail of a fault: ``<kind>: <message>`` (03 section 5.1)."""
    return f"{fault.kind}: {fault.message}"


def run(environ: Mapping[str, str], runtime: Runtime) -> int:
    """Validate ``environ`` and run the service; return the exit status.

    Invalid configuration is logged variable by variable and gives 78 before the transport
    or the publisher is even built (CFG-03). Otherwise the root log level follows
    ``LOG_LEVEL`` and the effective configuration is logged (CFG-06).
    """
    try:
        settings = load_settings(environ)
    except ConfigError as exc:
        for name, shown, reason in exc.errors:
            _log.error("invalid configuration: %s=%s: %s", name, shown, reason)
        _log.error("exiting with status %d: invalid configuration", EX_CONFIG)
        return EX_CONFIG
    logging.getLogger().setLevel(settings.log_level.upper())
    _log.info("dyness-b3-bms-mqtt %s starting", __version__)
    for line in describe(settings):
        _log.info("configuration: %s", line)
    return App(settings, runtime).run()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the console command; ``argv`` defaults to ``sys.argv[1:]``.

    ``--version`` prints the version, ``--check-config`` validates the environment and exits
    0 or 78 (CFG-06), no argument runs the service, and anything else is a configuration
    error (78).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(__version__)
        return EXIT_OK
    configure_logging("INFO")
    if args == ["--check-config"]:
        return check_config(os.environ)
    if args:
        _log.error("unknown argument(s): %s; usage: %s", " ".join(args), USAGE)
        return EX_CONFIG
    return run(os.environ, Runtime.production())
