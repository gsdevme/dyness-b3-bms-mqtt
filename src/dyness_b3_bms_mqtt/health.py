"""The pure health and recovery state machine (``docs/specs/03-health-and-recovery.md``).

:class:`HealthMachine` decides what to poll, which modules are available, when to reopen
the bus and when to exit for external recovery. It does no I/O, never reads a clock and
never sleeps (HEALTH-02): the application reports events through the input methods with the
monotonic ``now`` of each event, calls :meth:`HealthMachine.step` once per loop iteration
and carries out the returned actions in order.

Timing conventions, applied throughout:

* Ages are ``now`` minus the time of the matching frame. Before such a frame has arrived
  they are measured from the last successful bus open (HEALTH-04); with neither, an age is
  unknown and counts as stale.
* Staleness compares with ``>`` (an age equal to its limit is still fresh); timers and
  deadlines fire at ``>=``.
* A module is online only once one of its broadcasts has been received, and only while it
  is not ``SILENT`` and the state is running (``STARTING``, ``HEALTHY`` or ``DEGRADED``).
* Every decision about module health, and :meth:`HealthMachine.published_statuses`, uses
  the effective status: a module that has never broadcast counts as ``SILENT``, and unless
  ``CAN_LISTEN_ONLY`` a module that has not yet sent all five cell frames counts as
  ``CELLS_STALE``. So ``STARTING`` reaches ``HEALTHY`` (early or when ``START_GRACE_S``
  elapses) only for modules that have really been heard.
* Running states always have a successful open or a received frame, so ``bus_age`` is
  known in them.
* A ``ReopenBus`` counts as an attempt when it is emitted, whatever the open reports
  (HEALTH-14); the next attempt waits its full interval either way.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.protocol import (
    BROADCAST_CODES,
    CELL_CODES,
    MODULE_ID_BASE,
    MODULE_ID_MASK,
)
from dyness_b3_bms_mqtt.status import RUNNING_STATES, HealthState, ModuleStatus

EXIT_LINK_BOUNCE: Final = 75
EXIT_USB_RESET: Final = 76
ADAPTER_ABSENT: Final = "adapter_absent"
"""The ``detail`` that :meth:`HealthMachine.start` takes when ``CAN_CHANNEL`` is missing."""

REASON_CODES: Final = (
    "start",
    "bus_resumed",
    "all_ok",
    "modules_not_ok",
    "bus_silent",
    "bus_error",
    "reopen_exhausted",
    "escalation_limit",
    "adapter_absent",
    "bus_open_failed",
    "dead_usb_reset",
    "polling_suspended",
)
"""The reason codes of HEALTH-18, in spec order. A reason is ``code`` or ``code: detail``."""


@dataclass(frozen=True, slots=True)
class Transition:
    """The state changed from ``old`` (``None`` at start-up) to ``new`` because of ``reason``."""

    old: HealthState | None
    new: HealthState
    reason: str


@dataclass(frozen=True, slots=True)
class Poll:
    """Send the poll frame for ``module``."""

    module: int


@dataclass(frozen=True, slots=True)
class SetAvailability:
    """Publish ``module`` as online or offline."""

    module: int
    online: bool


@dataclass(frozen=True, slots=True)
class ReopenBus:
    """Shut the bus down, wait, open it again and report the result with ``on_bus_open``."""


@dataclass(frozen=True, slots=True)
class SuspendPolling:
    """Too many error frames: polling stays off for the life of the process."""

    reason: str


@dataclass(frozen=True, slots=True)
class SetEscalation:
    """Persist ``value`` as the escalation counter."""

    value: int


@dataclass(frozen=True, slots=True)
class Exit:
    """Stop cleanly and exit with ``code`` for external recovery."""

    code: int
    reason: str


type Action = (
    Transition | Poll | SetAvailability | ReopenBus | SuspendPolling | SetEscalation | Exit
)

ACTION_ORDER: Final = (
    Transition,
    SetAvailability,
    SetEscalation,
    SuspendPolling,
    Poll,
    ReopenBus,
    Exit,
)
"""The order of action types within one ``step`` result (HEALTH-19)."""


@dataclass(slots=True)
class _Module:
    """What the machine knows about one module.

    ``status`` is the plain HEALTH-05 status at the last step. ``ladder`` is the status the
    poll ladder last saw, ``since`` when it took that value and ``retries`` how many ladder
    polls have been sent since then. ``online`` is the last availability emitted, ``None``
    before the first.
    """

    broadcast_at: float | None = None
    cells_at: dict[int, float] = field(default_factory=dict)
    status: ModuleStatus = ModuleStatus.SILENT
    ladder: ModuleStatus | None = None
    since: float = 0.0
    retries: int = 0
    last_poll: float | None = None
    online: bool | None = None


class HealthMachine:
    """The stack health state machine of 03-health-and-recovery.md section 4.

    Construct it with the settings and the escalation counter read at start-up, call
    :meth:`start` once, then feed events and call :meth:`step` every loop iteration. After
    an ``Exit`` action every further ``step`` returns ``[]`` and inputs are ignored.
    """

    def __init__(self, settings: Settings, escalation: int) -> None:
        """Prepare a machine for ``settings`` with the stored ``escalation`` counter."""
        self._settings = settings
        self._escalation = escalation
        self._modules = {m: _Module() for m in range(1, settings.module_count + 1)}
        self._state: HealthState | None = None
        self._reason = ""
        self._entered_at = 0.0
        self._pending: list[Action] = []
        self._exiting = False
        self._finished = False
        self._last_frame: float | None = None
        self._last_open: float | None = None
        self._bus_error: str | None = None
        self._resumed = False
        self._stagger: dict[int, float] = {}
        self._attempts = 0
        self._last_attempt: float | None = None
        self._errors: deque[float] = deque()
        self._suspended = False

    @property
    def state(self) -> HealthState:
        """The current state. Raises ``RuntimeError`` before :meth:`start`."""
        if self._state is None:
            raise RuntimeError("the health machine has not been started")
        return self._state

    @property
    def reason(self) -> str:
        """The reason carried by the last transition. Raises ``RuntimeError`` before start."""
        if self._state is None:
            raise RuntimeError("the health machine has not been started")
        return self._reason

    @property
    def escalation(self) -> int:
        """The escalation counter as last set by this machine."""
        return self._escalation

    @property
    def polling_suspended(self) -> bool:
        """Whether error frames have suspended polling for the life of the process."""
        return self._suspended

    def module_status(self, module: int) -> ModuleStatus:
        """Return the plain HEALTH-05 status of ``module`` at the last step.

        Ages before a module's first frame count from the bus open, so this can be ``OK``
        for a module that has not been heard yet; see :meth:`published_statuses`.
        """
        return self._modules[module].status

    def published_statuses(self) -> dict[int, ModuleStatus]:
        """Return the status to publish for every module: ``SILENT`` whenever it is offline."""
        running = self._state in RUNNING_STATES
        return {
            m: self._effective(mod) if running else ModuleStatus.SILENT
            for m, mod in self._modules.items()
        }

    def bus_age(self, now: float) -> float | None:
        """Seconds since the last bus-activity frame (or open); ``None`` with neither."""
        return _age(self._last_frame, self._last_open, now)

    def bcast_age(self, module: int, now: float) -> float | None:
        """Return the HEALTH-04 broadcast age of ``module``; ``None`` with no frame or open.

        This is the freshness age the machine judges by: before the first broadcast it
        counts from the last bus open. It must not be published; telemetry computes the
        published ``bcast_age_s``, which stays ``None`` until a frame arrives (HEALTH-20).
        """
        return _age(self._modules[module].broadcast_at, self._last_open, now)

    def cell_age(self, module: int, now: float) -> float | None:
        """Return the HEALTH-04 cell age of ``module``: its oldest of the five cell frames.

        A cell frame not yet received counts from the last bus open; ``None`` with neither.
        Like :meth:`bcast_age` this is a freshness age for the machine and must not be
        published; telemetry computes the published ``cell_age_s`` (HEALTH-20).
        """
        return self._cell_age(self._modules[module], now)

    def start(self, now: float, open_ok: bool, detail: str) -> None:
        """Enter the first state after the first bus open attempt.

        ``open_ok`` is the result of that open; when it failed, ``detail`` is
        :data:`ADAPTER_ABSENT` or a description of the error. The escalation limit is checked
        first, then the open result. Raises ``RuntimeError`` if called twice.
        """
        if self._state is not None:
            raise RuntimeError("the health machine has already been started")
        if open_ok:
            self._last_open = now
        dead_after = self._settings.dead_after
        if self._escalation >= dead_after:
            reason = f"escalation_limit: {self._escalation} >= DEAD_AFTER {dead_after}"
            self._enter(HealthState.DEAD, reason, now)
        elif not open_ok:
            reason = ADAPTER_ABSENT if detail == ADAPTER_ABSENT else f"bus_open_failed: {detail}"
            self._enter(HealthState.DEAD, reason, now)
        else:
            self._enter(HealthState.STARTING, "start", now)

    def on_frame(self, arbitration_id: int, now: float) -> None:
        """Record a received frame that counts as bus activity (PROTO-05)."""
        if self._finished or self._exiting:
            return
        self._last_frame = now
        if self._state in (HealthState.STALE, HealthState.DEAD):
            self._resumed = True
        if arbitration_id & MODULE_ID_MASK != MODULE_ID_BASE:
            return
        module = self._modules.get(arbitration_id >> 8 & 0xF)
        code = arbitration_id & 0xFF
        if module is None:
            return
        if code in BROADCAST_CODES:
            module.broadcast_at = now
        elif code in CELL_CODES:
            module.cells_at[code] = now

    def on_error_frame(self, now: float) -> None:
        """Count a CAN error frame in the sliding window (HEALTH-22, HEALTH-23)."""
        if self._suspended or self._finished or self._exiting:
            return
        window = self._settings.error_window_s
        self._errors.append(now)
        while now - self._errors[0] > window:
            self._errors.popleft()
        if len(self._errors) >= self._settings.error_suspend_count:
            self._suspended = True
            self._stagger.clear()
            reason = f"polling_suspended: {len(self._errors)} error frames within {window:g} s"
            self._pending.append(SuspendPolling(reason))

    def on_bus_exception(self, now: float, detail: str) -> None:
        """Record a receive or send fault; it moves a running state to ``STALE``."""
        if self._state in RUNNING_STATES and self._bus_error is None and not self._exiting:
            self._bus_error = detail

    def on_bus_open(self, now: float, ok: bool, detail: str) -> None:
        """Record the result of a ``ReopenBus``; a success restarts the pre-frame ages."""
        if ok:
            self._last_open = now

    def step(self, now: float) -> list[Action]:
        """Advance every timer to ``now`` and return the due actions in HEALTH-19 order.

        After each transition the new state's handler runs again at the same ``now``, so its
        entry actions and first due actions come out together. That loop is bounded by the
        number of states; in practice it settles within three handler runs.
        """
        if self._state is None or self._finished:
            return []
        for module in self._modules.values():
            module.status = self._status(module, now)
        for _ in HealthState:
            state = self._state
            self._handle(now)
            if self._state is state or self._exiting:
                break
        self._finished = self._exiting
        return self._drain()

    def _handle(self, now: float) -> None:
        """Run the handler of the current state; ``RECOVERING`` has none."""
        if self._state in RUNNING_STATES:
            self._running(now)
        elif self._state is HealthState.STALE:
            self._stale(now)
        elif self._state is HealthState.DEAD:
            self._dead(now)

    def _running(self, now: float) -> None:
        """Handle ``STARTING``, ``HEALTHY`` and ``DEGRADED`` for one step."""
        if self._bus_error is not None:
            self._enter(HealthState.STALE, f"bus_error: {self._bus_error}", now)
            return
        age = self.bus_age(now)
        if age is not None and age > self._settings.bus_silence_s:
            self._enter(HealthState.STALE, f"bus_silent: no frame for {age:.1f} s", now)
            return
        statuses = {m: self._effective(mod) for m, mod in self._modules.items()}
        for m, status in statuses.items():
            module = self._modules[m]
            if module.ladder is not status:
                module.ladder, module.since, module.retries = status, now, 0
        not_ok = {m: s for m, s in statuses.items() if s is not ModuleStatus.OK}
        if self._state is HealthState.STARTING:
            if not not_ok or now >= self._entered_at + self._settings.start_grace_s:
                self._judge(not_ok, now)
            else:
                self._sync_availability(offline_allowed=False)
                self._staggered_polls(now)
        elif self._state is HealthState.HEALTHY:
            if not_ok:
                self._judge(not_ok, now)
                return
            self._sync_availability(offline_allowed=True)
            reset_after = self._settings.recovery_reset_s
            if self._escalation > 0 and now - self._entered_at >= reset_after:
                self._escalation = 0
                self._pending.append(SetEscalation(0))
        else:
            if not not_ok:
                self._judge(not_ok, now)
                return
            self._sync_availability(offline_allowed=True)
            self._ladder_polls(now)

    def _judge(self, not_ok: Mapping[int, ModuleStatus], now: float) -> None:
        """Move to ``HEALTHY`` or ``DEGRADED`` according to the modules that are not OK."""
        if not not_ok:
            count = len(self._modules)
            self._enter(HealthState.HEALTHY, f"all_ok: {count}/{count} modules ok", now)
        else:
            detail = ", ".join(f"m{m} {status}" for m, status in not_ok.items())
            self._enter(HealthState.DEGRADED, f"modules_not_ok: {detail}", now)

    def _stale(self, now: float) -> None:
        """Reopen the bus every ``BUS_SILENCE_S`` until a frame arrives or attempts run out."""
        if self._resumed:
            self._enter(HealthState.STARTING, "bus_resumed", now)
            return
        interval = self._settings.bus_silence_s
        if self._last_attempt is not None and now - self._last_attempt < interval:
            return
        limit = self._settings.bus_reopen_attempts
        if self._attempts < limit:
            self._attempts += 1
            self._last_attempt = now
            self._pending.append(ReopenBus())
        else:
            reason = f"reopen_exhausted: {self._attempts}/{limit} reopen attempts"
            self._enter(HealthState.RECOVERING, reason, now)

    def _dead(self, now: float) -> None:
        """Retry the bus every ``DEAD_RETRY_S`` and exit for a USB reset when it is time."""
        if self._resumed:
            self._enter(HealthState.STARTING, "bus_resumed", now)
            return
        if now - self._entered_at >= self._settings.dead_usb_reset_s:
            self._pending.append(Exit(EXIT_USB_RESET, "dead_usb_reset"))
            self._exiting = True
            return
        last = self._entered_at if self._last_attempt is None else self._last_attempt
        if now - last >= self._settings.dead_retry_s:
            self._last_attempt = now
            self._pending.append(ReopenBus())

    def _enter(self, new: HealthState, reason: str, now: float) -> None:
        """Change state and queue the transition and the new state's entry actions."""
        self._pending.append(Transition(self._state, new, reason))
        self._state, self._reason, self._entered_at = new, reason, now
        self._bus_error = None
        self._resumed = False
        self._stagger = {}
        match new:
            case HealthState.STARTING:
                for module in self._modules.values():
                    module.ladder = None
                if self._may_poll():
                    stagger = self._settings.poll_stagger_s
                    self._stagger = {m: now + (m - 1) * stagger for m in self._modules}
            case HealthState.DEGRADED:
                for m, module in self._modules.items():
                    if self._effective(module) is ModuleStatus.SILENT:
                        self._set_availability(m, False)
            case HealthState.STALE:
                self._all_offline()
                self._attempts, self._last_attempt = 0, None
            case HealthState.RECOVERING:
                self._all_offline()
                previous = self._escalation
                self._escalation = previous + 1
                self._pending.append(SetEscalation(self._escalation))
                code = EXIT_LINK_BOUNCE if previous == 0 else EXIT_USB_RESET
                self._pending.append(Exit(code, reason))
                self._exiting = True
            case HealthState.DEAD:
                self._all_offline()
                self._last_attempt = None

    def _status(self, module: _Module, now: float) -> ModuleStatus:
        """Return the HEALTH-05 status of ``module`` at ``now``."""
        bcast_age = _age(module.broadcast_at, self._last_open, now)
        if bcast_age is None or bcast_age > self._settings.module_stale_s:
            return ModuleStatus.SILENT
        if not self._settings.can_listen_only:
            cell_age = self._cell_age(module, now)
            if cell_age is None or cell_age > self._settings.cell_stale_s:
                return ModuleStatus.CELLS_STALE
        return ModuleStatus.OK

    def _cell_age(self, module: _Module, now: float) -> float | None:
        """Return the age of the oldest cell frame of ``module``, as :meth:`cell_age`."""
        times = [module.cells_at.get(code, self._last_open) for code in CELL_CODES]
        known = [t for t in times if t is not None]
        return now - min(known) if len(known) == len(times) else None

    def _effective(self, module: _Module) -> ModuleStatus:
        """Return the status of ``module`` counting only frames really heard.

        A module that has never broadcast is ``SILENT``; unless ``CAN_LISTEN_ONLY``, an
        otherwise ``OK`` module that has not yet sent all five cell frames is ``CELLS_STALE``.
        """
        if module.broadcast_at is None:
            return ModuleStatus.SILENT
        heard = self._settings.can_listen_only or module.cells_at.keys() >= set(CELL_CODES)
        if module.status is ModuleStatus.OK and not heard:
            return ModuleStatus.CELLS_STALE
        return module.status

    def _may_poll(self) -> bool:
        return not self._settings.can_listen_only and not self._suspended

    def _staggered_polls(self, now: float) -> None:
        """Emit the start-up polls that are due (HEALTH-08)."""
        for m, due in sorted(self._stagger.items()):
            if now >= due:
                del self._stagger[m]
                if self._may_poll():
                    self._poll(m, now)

    def _ladder_polls(self, now: float) -> None:
        """Re-poll ``CELLS_STALE`` and ``SILENT`` modules on their ladders (HEALTH-09, -10)."""
        if not self._may_poll():
            return
        settings = self._settings
        for m, module in self._modules.items():
            if module.ladder is ModuleStatus.CELLS_STALE:
                fast = module.retries < settings.poll_max_retries
                interval = settings.poll_retry_s if fast else settings.poll_backoff_s
                anchor = module.last_poll
            elif module.ladder is ModuleStatus.SILENT:
                interval = settings.poll_backoff_s
                anchor = module.since if module.retries == 0 else module.last_poll
            else:
                continue
            if anchor is None or now >= anchor + interval:
                module.retries += 1
                self._poll(m, now)

    def _poll(self, module: int, now: float) -> None:
        self._modules[module].last_poll = now
        self._pending.append(Poll(module))

    def _sync_availability(self, *, offline_allowed: bool) -> None:
        """Emit availability for every module whose online view changed (HEALTH-07)."""
        for m, module in self._modules.items():
            online = self._effective(module) is not ModuleStatus.SILENT
            if module.online is not online and (online or offline_allowed):
                self._set_availability(m, online)

    def _all_offline(self) -> None:
        for m in self._modules:
            self._set_availability(m, False)

    def _set_availability(self, module: int, online: bool) -> None:
        self._modules[module].online = online
        self._pending.append(SetAvailability(module, online))

    def _drain(self) -> list[Action]:
        """Return the queued actions in HEALTH-19 order, one availability per module."""
        queued, self._pending = self._pending, []
        availability: dict[int, SetAvailability] = {}
        others: list[Action] = []
        for action in queued:
            if isinstance(action, SetAvailability):
                availability[action.module] = action
            else:
                others.append(action)
        actions = [*others, *(availability[m] for m in sorted(availability))]
        return sorted(actions, key=lambda action: ACTION_ORDER.index(type(action)))


def _age(at: float | None, fallback: float | None, now: float) -> float | None:
    """Return ``now - at``, or ``now - fallback`` before the first ``at``; ``None`` if neither."""
    reference = fallback if at is None else at
    return None if reference is None else now - reference
