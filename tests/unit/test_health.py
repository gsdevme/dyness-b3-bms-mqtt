"""Tests of the pure health and recovery state machine: 03-health-and-recovery.md.

Most tests drive the machine through :class:`Rig`, a fake three-module stack stepped on a
0.5 s grid from ``T0``. Expected timings are worked out from the spec defaults
(``08-configuration.md``): stagger 0.3 s, three retries 5 s apart then back-off 60 s, grace 20 s,
cell and module staleness 10 s, bus silence 15 s, two reopen attempts, recovery reset
300 s, DEAD after 3, DEAD retry 60 s, DEAD USB reset 900 s, 10 error frames in 60 s.
"""

import ast
import inspect
import re
from collections.abc import Mapping

import pytest

from dyness_b3_bms_mqtt import health
from dyness_b3_bms_mqtt.health import (
    ACTION_ORDER,
    EXIT_LINK_BOUNCE,
    EXIT_USB_RESET,
    REASON_CODES,
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
from dyness_b3_bms_mqtt.protocol import (
    BROADCAST_CODES,
    CELL_CODES,
    ENERGY_CODE,
    HEARTBEAT_ID,
    MODULE_ID_BASE,
)
from dyness_b3_bms_mqtt.status import HealthState, ModuleStatus
from tests.support.settings import make_settings

T0 = 1000.0
GRID = 0.5
STARTING = HealthState.STARTING
HEALTHY = HealthState.HEALTHY
DEGRADED = HealthState.DEGRADED
STALE = HealthState.STALE
RECOVERING = HealthState.RECOVERING
DEAD = HealthState.DEAD
OK = ModuleStatus.OK
CELLS_STALE = ModuleStatus.CELLS_STALE
SILENT = ModuleStatus.SILENT
ALL_OK = "all_ok: 3/3 modules ok"
ALL_SILENT = "modules_not_ok: m1 silent, m2 silent, m3 silent"


def module_id(module: int, code: int) -> int:
    """Return the extended identifier of frame ``code`` from ``module``."""
    return MODULE_ID_BASE | module << 8 | code


def offline(*modules: int) -> list[Action]:
    return [SetAvailability(m, False) for m in modules]


def online(*modules: int) -> list[Action]:
    return [SetAvailability(m, True) for m in modules]


class Rig:
    """A fake three-module stack driving one machine on a 0.5 s grid.

    Each tick moves the clock 0.5 s, feeds the frames the stack is sending at that instant
    (heartbeat, one broadcast per broadcasting module, all five cell frames per module whose
    cell stream flows) and then steps the machine. A ``Poll`` starts that module's cell
    stream when ``poll_answers`` is true; ``ReopenBus`` is answered at once with
    ``on_bus_open(now, reopen_ok, ...)``. Every action is logged with its step time.
    """

    def __init__(
        self,
        *,
        escalation: int = 0,
        open_ok: bool = True,
        detail: str = "",
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = make_settings(**(env or {}))
        self.machine = HealthMachine(self.settings, escalation)
        self.modules = range(1, self.settings.module_count + 1)
        self.ticks = 0
        self.bus_alive = True
        self.broadcasting = set(self.modules)
        self.cells: set[int] = set()
        self.poll_answers = True
        self.reopen_ok = True
        self.log: list[tuple[float, Action]] = []
        self.machine.start(T0, open_ok, detail)
        self.first = self._step()

    @property
    def now(self) -> float:
        return T0 + self.ticks * GRID

    def tick(self) -> list[Action]:
        self.ticks += 1
        if self.bus_alive:
            self.machine.on_frame(HEARTBEAT_ID, self.now)
            for m in sorted(self.broadcasting):
                self.machine.on_frame(module_id(m, 0x21), self.now)
            for m in sorted(self.cells):
                for code in CELL_CODES:
                    self.machine.on_frame(module_id(m, code), self.now)
        return self._step()

    def run_to(self, t: float) -> list[Action]:
        """Tick until the clock reads ``t`` and return the actions of that last step."""
        target = round((t - T0) / GRID)
        assert target > self.ticks, f"{t} is not after {self.now}"
        actions: list[Action] = []
        while self.ticks < target:
            actions = self.tick()
        return actions

    def kill_bus(self) -> None:
        """Stop every frame; the cell streams stop with them."""
        self.bus_alive = False
        self.cells.clear()

    def at(self, t: float) -> list[Action]:
        return [a for when, a in self.log if when == t]

    def polls(self, since: float = T0) -> list[tuple[float, int]]:
        return [(t, a.module) for t, a in self.log if isinstance(a, Poll) and t >= since]

    def times(self, kind: type[Action], since: float = T0) -> list[float]:
        return [t for t, a in self.log if isinstance(a, kind) and t >= since]

    def transitions(self) -> list[tuple[HealthState | None, HealthState, str]]:
        return [(a.old, a.new, a.reason) for _, a in self.log if isinstance(a, Transition)]

    def _step(self) -> list[Action]:
        now = self.now
        actions = self.machine.step(now)
        for action in actions:
            self.log.append((now, action))
            if isinstance(action, Poll) and self.poll_answers:
                self.cells.add(action.module)
            elif isinstance(action, ReopenBus):
                detail = "" if self.reopen_ok else "OPEN_FAILED: test"
                self.machine.on_bus_open(now, self.reopen_ok, detail)
        return actions


def healthy_rig(*, escalation: int = 0, **env: str) -> Rig:
    """Return a rig that has polled every module and reached HEALTHY at ``T0 + 1.5``."""
    rig = Rig(escalation=escalation, env=env)
    rig.run_to(T0 + 1.5)
    assert rig.machine.state is HEALTHY
    return rig


@pytest.mark.req("HEALTH-01", "HEALTH-02")
def test_machine_is_pure() -> None:
    tree = ast.parse(inspect.getsource(health))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    forbidden = {
        "time",
        "datetime",
        "os",
        "socket",
        "threading",
        "asyncio",
        "logging",
        "subprocess",
        "random",
        "pathlib",
        "can",
    }
    assert imported.isdisjoint(forbidden), imported & forbidden
    assert called.isdisjoint({"open", "print", "input"}), called
    assert list(inspect.signature(HealthMachine).parameters) == ["settings", "escalation"]


@pytest.mark.req("HEALTH-19", "HEALTH-20")
def test_interface_and_action_order() -> None:
    public = {name for name in vars(HealthMachine) if not name.startswith("_")}
    inputs = {"start", "on_frame", "on_error_frame", "on_bus_exception", "on_bus_open", "step"}
    accessors = {
        "state",
        "reason",
        "escalation",
        "polling_suspended",
        "module_status",
        "published_statuses",
        "bus_age",
        "bcast_age",
        "cell_age",
    }
    assert public == inputs | accessors
    for name in ("state", "reason", "escalation", "polling_suspended"):
        attribute = vars(HealthMachine)[name]
        assert isinstance(attribute, property)
        assert attribute.fset is None
    order = [Transition, SetAvailability, SetEscalation, SuspendPolling, Poll, ReopenBus, Exit]
    assert list(ACTION_ORDER) == order
    assert (EXIT_LINK_BOUNCE, EXIT_USB_RESET) == (75, 76)


@pytest.mark.req("HEALTH-04", "HEALTH-08", "HEALTH-11")
def test_start_and_staggered_polls() -> None:
    machine = HealthMachine(make_settings(), 0)
    machine.start(T0, True, "")

    assert machine.step(T0) == [Transition(None, STARTING, "start"), Poll(1)]
    assert machine.step(T0 + 0.29) == []
    assert machine.step(T0 + 0.3) == [Poll(2)]
    assert machine.step(T0 + 0.59) == []
    assert machine.step(T0 + 0.6) == [Poll(3)]
    assert machine.step(T0 + 5.0) == []
    assert machine.state is STARTING
    assert machine.reason == "start"
    assert machine.bus_age(T0 + 2.0) == 2.0
    assert machine.bcast_age(1, T0 + 2.0) == 2.0
    assert machine.cell_age(1, T0 + 2.0) == 2.0


@pytest.mark.req("HEALTH-06", "HEALTH-08", "HEALTH-11", "PROTO-31")
def test_listen_only_never_polls_and_ignores_cells() -> None:
    rig = Rig(env={"CAN_LISTEN_ONLY": "true"})
    assert rig.first == [Transition(None, STARTING, "start")]

    assert rig.run_to(T0 + 0.5) == [Transition(STARTING, HEALTHY, ALL_OK), *online(1, 2, 3)]
    rig.run_to(T0 + 30)
    assert rig.machine.state is HEALTHY
    assert [rig.machine.module_status(m) for m in rig.modules] == [OK, OK, OK]

    rig.broadcasting.discard(2)
    rig.run_to(T0 + 200)
    assert rig.machine.state is DEGRADED
    rig.kill_bus()
    rig.run_to(T0 + 300)
    assert rig.machine.state is RECOVERING

    assert rig.polls() == []
    assert rig.cells == set()


@pytest.mark.req("HEALTH-04", "HEALTH-05", "HEALTH-13", "HEALTH-20")
def test_starting_to_healthy_needs_real_frames() -> None:
    rig = Rig()
    rig.broadcasting.clear()
    rig.poll_answers = False
    rig.cells.clear()

    rig.run_to(T0 + 5)
    assert rig.machine.state is STARTING
    assert rig.machine.module_status(1) is OK
    assert rig.machine.published_statuses() == {1: SILENT, 2: SILENT, 3: SILENT}
    assert rig.times(SetAvailability) == []

    rig.broadcasting.update(rig.modules)
    assert rig.run_to(T0 + 5.5) == online(1, 2, 3)
    assert rig.machine.state is STARTING
    assert rig.machine.module_status(1) is OK
    assert rig.machine.published_statuses() == {1: CELLS_STALE, 2: CELLS_STALE, 3: CELLS_STALE}

    for m in rig.modules:
        for code in CELL_CODES[:-1]:
            rig.machine.on_frame(module_id(m, code), T0 + 6.0)
    rig.run_to(T0 + 6.0)
    assert rig.machine.state is STARTING

    rig.cells.update(rig.modules)
    assert rig.run_to(T0 + 6.5) == [Transition(STARTING, HEALTHY, ALL_OK)]
    assert rig.machine.reason == ALL_OK


@pytest.mark.req("HEALTH-07", "HEALTH-09", "HEALTH-10", "HEALTH-13", "HEALTH-18")
def test_grace_then_degraded() -> None:
    rig = Rig()
    rig.broadcasting = {1, 2}
    rig.poll_answers = False
    rig.cells = {1}

    rig.run_to(T0 + 19.5)
    assert rig.machine.state is STARTING
    assert rig.machine.module_status(2) is CELLS_STALE
    assert rig.machine.module_status(3) is SILENT
    assert rig.polls() == [(T0, 1), (T0 + 0.5, 2), (T0 + 1.0, 3)]
    assert SetAvailability(3, False) not in [a for _, a in rig.log]

    assert rig.run_to(T0 + 20.0) == [
        Transition(STARTING, DEGRADED, "modules_not_ok: m2 cells_stale, m3 silent"),
        *offline(3),
        Poll(2),
    ]
    rig.run_to(T0 + 61)
    assert rig.polls(since=T0 + 20) == [
        (T0 + 20, 2),
        (T0 + 25, 2),
        (T0 + 30, 2),
        (T0 + 60, 3),
    ]


@pytest.mark.req("HEALTH-05", "HEALTH-07", "HEALTH-13")
def test_grace_end_needs_cell_frames() -> None:
    rig = Rig(env={"CELL_STALE_S": "30"})
    rig.poll_answers = False
    rig.cells.clear()

    rig.run_to(T0 + 19.5)
    assert rig.machine.state is STARTING
    assert [rig.machine.module_status(m) for m in rig.modules] == [OK, OK, OK]
    assert rig.machine.published_statuses() == {1: CELLS_STALE, 2: CELLS_STALE, 3: CELLS_STALE}

    reason = "modules_not_ok: m1 cells_stale, m2 cells_stale, m3 cells_stale"
    assert rig.run_to(T0 + 20.0) == [
        Transition(STARTING, DEGRADED, reason),
        Poll(1),
        Poll(2),
        Poll(3),
    ]
    rig.run_to(T0 + 60)
    assert HEALTHY not in [new for _, new, _ in rig.transitions()]


@pytest.mark.req("HEALTH-09", "HEALTH-11", "HEALTH-12")
def test_cells_stale_ladder_and_reset() -> None:
    rig = healthy_rig()
    rig.poll_answers = False
    rig.run_to(T0 + 10)
    rig.cells.discard(2)
    t0 = T0 + 20.5

    assert rig.run_to(t0) == [
        Transition(HEALTHY, DEGRADED, "modules_not_ok: m2 cells_stale"),
        Poll(2),
    ]
    rig.run_to(t0 + 135)
    assert rig.polls(since=t0) == [(t0, 2), (t0 + 5, 2), (t0 + 10, 2), (t0 + 70, 2), (t0 + 130, 2)]
    assert rig.machine.state is DEGRADED
    assert rig.times(ReopenBus) == rig.times(Exit) == []

    rig.cells.add(2)
    assert rig.run_to(t0 + 135.5) == [Transition(DEGRADED, HEALTHY, ALL_OK)]
    rig.cells.discard(2)
    t1 = t0 + 146
    rig.run_to(t1 + 20)
    assert rig.polls(since=t0 + 135.5) == [(t1, 2), (t1 + 5, 2), (t1 + 10, 2)]


@pytest.mark.req("HEALTH-04", "HEALTH-05", "HEALTH-20")
def test_cell_age_boundary() -> None:
    machine = HealthMachine(make_settings(), 0)
    machine.start(T0, True, "")
    machine.step(T0)

    def feed(t: float, *ids: int) -> None:
        for arbitration_id in (HEARTBEAT_ID, *ids):
            machine.on_frame(arbitration_id, t)

    feed(T0 + 2, module_id(1, 0x12), module_id(1, 0x16))
    feed(T0 + 3, *(module_id(1, code) for code in CELL_CODES if code != 0x12))
    feed(T0 + 1, module_id(1, ENERGY_CODE))
    feed(T0 + 9, module_id(1, 0x21))
    feed(T0 + 9, module_id(4, 0x11), module_id(4, 0x21))
    for t in (T0 + 10, T0 + 11, T0 + 12, T0 + 12.5):
        feed(t, module_id(1, 0x23))
        machine.step(t)
        if t == T0 + 12:
            assert machine.cell_age(1, t) == 10.0
            assert machine.module_status(1) is OK
    assert machine.cell_age(1, T0 + 12.5) == 10.5
    assert machine.module_status(1) is CELLS_STALE

    feed(T0 + 15, *(module_id(1, code) for code in CELL_CODES))
    machine.step(T0 + 22.5)
    assert machine.bcast_age(1, T0 + 22.5) == 10.0
    assert machine.module_status(1) is OK
    machine.step(T0 + 23.0)
    assert machine.module_status(1) is SILENT
    assert machine.cell_age(2, T0 + 23.0) == 23.0
    assert machine.bcast_age(2, T0 + 23.0) == 23.0
    with pytest.raises(KeyError):
        machine.module_status(4)
    assert set(BROADCAST_CODES).isdisjoint(CELL_CODES)
    assert ENERGY_CODE not in CELL_CODES


@pytest.mark.req("HEALTH-05", "HEALTH-10", "HEALTH-12", "HEALTH-20")
def test_silent_module_backoff_never_escalates() -> None:
    rig = healthy_rig()
    rig.poll_answers = False
    rig.run_to(T0 + 10)
    rig.broadcasting.discard(3)
    rig.cells.discard(3)
    ts = T0 + 20.5

    assert rig.run_to(ts) == [
        Transition(HEALTHY, DEGRADED, "modules_not_ok: m3 silent"),
        *offline(3),
    ]
    assert rig.machine.module_status(3) is SILENT
    assert rig.machine.published_statuses() == {1: OK, 2: OK, 3: SILENT}
    rig.run_to(ts + 1000)
    assert rig.polls(since=ts) == [(ts + 60 * k, 3) for k in range(1, 17)]
    assert rig.machine.state is DEGRADED
    assert rig.times(ReopenBus) == rig.times(Exit) == rig.times(SetEscalation) == []

    rig.broadcasting.add(3)
    rig.cells.add(3)
    assert rig.run_to(ts + 1000.5) == [Transition(DEGRADED, HEALTHY, ALL_OK), *online(3)]


@pytest.mark.req("HEALTH-04", "HEALTH-12", "HEALTH-13", "HEALTH-19")
def test_bus_silence_boundary_then_stale() -> None:
    rig = healthy_rig()
    rig.run_to(T0 + 5)
    rig.kill_bus()
    tb = T0 + 5

    assert rig.run_to(tb + 10.5) == [Transition(HEALTHY, DEGRADED, ALL_SILENT), *offline(1, 2, 3)]
    assert rig.run_to(tb + 15.0) == []
    assert rig.machine.bus_age(tb + 15.0) == 15.0
    assert rig.machine.state is DEGRADED
    assert rig.run_to(tb + 15.5) == [
        Transition(DEGRADED, STALE, "bus_silent: no frame for 15.5 s"),
        *offline(1, 2, 3),
        ReopenBus(),
    ]
    assert rig.machine.published_statuses() == {1: SILENT, 2: SILENT, 3: SILENT}


@pytest.mark.req("HEALTH-13", "HEALTH-14", "HEALTH-15", "HEALTH-19")
@pytest.mark.parametrize("reopen_ok", [True, False], ids=["reopen-ok", "reopen-fails"])
@pytest.mark.parametrize(("escalation", "code"), [(0, 75), (1, 76)], ids=["first", "repeat"])
def test_reopen_ladder_then_recovering(escalation: int, code: int, reopen_ok: bool) -> None:
    rig = healthy_rig(escalation=escalation)
    rig.run_to(T0 + 5)
    rig.kill_bus()
    rig.reopen_ok = reopen_ok
    ts = T0 + 20.5

    assert ReopenBus() in rig.run_to(ts)
    assert rig.machine.state is STALE
    assert rig.run_to(ts + 14.5) == []
    assert rig.run_to(ts + 15) == [ReopenBus()]
    assert rig.run_to(ts + 29.5) == []
    reason = "reopen_exhausted: 2/2 reopen attempts"
    assert rig.run_to(ts + 30) == [
        Transition(STALE, RECOVERING, reason),
        *offline(1, 2, 3),
        SetEscalation(escalation + 1),
        Exit(code, reason),
    ]
    assert rig.machine.escalation == escalation + 1
    assert rig.run_to(ts + 100) == []
    assert rig.machine.step(ts + 2000) == []
    assert rig.times(ReopenBus) == [ts, ts + 15]
    assert rig.times(Exit) == [ts + 30]


@pytest.mark.req("HEALTH-13", "HEALTH-15")
def test_zero_reopen_attempts_goes_straight_to_recovering() -> None:
    rig = healthy_rig(BUS_REOPEN_ATTEMPTS="0")
    rig.run_to(T0 + 5)
    rig.kill_bus()

    reason = "reopen_exhausted: 0/0 reopen attempts"
    assert rig.run_to(T0 + 20.5) == [
        Transition(DEGRADED, STALE, "bus_silent: no frame for 15.5 s"),
        Transition(STALE, RECOVERING, reason),
        *offline(1, 2, 3),
        SetEscalation(1),
        Exit(75, reason),
    ]
    assert rig.times(ReopenBus) == []


@pytest.mark.req("HEALTH-08", "HEALTH-13", "HEALTH-17")
def test_frame_after_reopen_restarts() -> None:
    rig = healthy_rig()
    rig.run_to(T0 + 5)
    rig.kill_bus()
    ts = T0 + 20.5
    rig.run_to(ts + 2.5)
    assert rig.machine.state is STALE

    rig.bus_alive = True
    assert rig.run_to(ts + 3) == [
        Transition(STALE, STARTING, "bus_resumed"),
        *online(1, 2, 3),
        Poll(1),
    ]
    assert rig.run_to(ts + 3.5) == [Poll(2)]
    assert rig.run_to(ts + 4.0) == [Poll(3)]
    assert rig.run_to(ts + 4.5) == [Transition(STARTING, HEALTHY, ALL_OK)]
    assert rig.times(ReopenBus) == [ts]


@pytest.mark.req("HEALTH-13", "HEALTH-18")
def test_bus_exception() -> None:
    detail = "OS_ERROR: [Errno 100] Network is down"
    rig = healthy_rig()
    rig.run_to(T0 + 3)
    rig.machine.on_bus_exception(T0 + 3, detail)
    rig.kill_bus()

    assert rig.run_to(T0 + 3.5) == [
        Transition(HEALTHY, STALE, f"bus_error: {detail}"),
        *offline(1, 2, 3),
        ReopenBus(),
    ]
    rig.machine.on_bus_exception(T0 + 3.5, "NOT_OPEN: closed")
    assert rig.run_to(T0 + 4) == []
    assert rig.machine.reason == f"bus_error: {detail}"

    starting = Rig()
    starting.machine.on_bus_exception(T0, "CAN_ERROR: bus error")
    assert starting.tick() == [
        Transition(STARTING, STALE, "bus_error: CAN_ERROR: bus error"),
        *offline(1, 2, 3),
        ReopenBus(),
    ]
    assert starting.tick()[0] == Transition(STALE, STARTING, "bus_resumed")


@pytest.mark.req("HEALTH-13", "HEALTH-14", "HEALTH-15")
def test_dead_mode() -> None:
    rig = Rig(escalation=3)
    rig.kill_bus()
    rig.reopen_ok = False

    assert rig.first == [
        Transition(None, DEAD, "escalation_limit: 3 >= DEAD_AFTER 3"),
        *offline(1, 2, 3),
    ]
    assert rig.run_to(T0 + 59.5) == []
    assert rig.run_to(T0 + 60) == [ReopenBus()]
    assert rig.run_to(T0 + 900) == [Exit(EXIT_USB_RESET, "dead_usb_reset")]
    assert rig.times(ReopenBus) == [T0 + 60 * k for k in range(1, 15)]
    assert rig.times(SetEscalation) == []
    assert rig.machine.escalation == 3
    assert rig.machine.state is DEAD
    assert rig.run_to(T0 + 1000) == []
    assert rig.machine.bus_age(T0 + 1000) == 1000.0


@pytest.mark.req("HEALTH-13", "HEALTH-17", "HEALTH-18")
@pytest.mark.parametrize(
    ("escalation", "open_ok", "detail", "reason"),
    [
        (3, True, "", "escalation_limit: 3 >= DEAD_AFTER 3"),
        (5, False, "adapter_absent", "escalation_limit: 5 >= DEAD_AFTER 3"),
        (0, False, "adapter_absent", "adapter_absent"),
        (2, False, "LINK_DOWN: can0 is down", "bus_open_failed: LINK_DOWN: can0 is down"),
    ],
    ids=["escalation-limit", "limit-wins", "adapter-absent", "open-failed"],
)
def test_dead_to_starting_on_frame(
    escalation: int, open_ok: bool, detail: str, reason: str
) -> None:
    rig = Rig(escalation=escalation, open_ok=open_ok, detail=detail)
    rig.kill_bus()
    assert rig.first == [Transition(None, DEAD, reason), *offline(1, 2, 3)]
    assert rig.machine.published_statuses() == {1: SILENT, 2: SILENT, 3: SILENT}
    if not open_ok:
        assert rig.machine.bus_age(T0 + 30) is None
    rig.run_to(T0 + 60.5)
    assert rig.times(ReopenBus) == [T0 + 60]

    rig.bus_alive = True
    assert rig.run_to(T0 + 61) == [
        Transition(DEAD, STARTING, "bus_resumed"),
        *online(1, 2, 3),
        Poll(1),
    ]
    rig.run_to(T0 + 62.5)
    assert rig.machine.state is HEALTHY
    assert rig.machine.escalation == escalation


@pytest.mark.req("HEALTH-16")
def test_escalation_reset_after_unbroken_healthy() -> None:
    rig = healthy_rig(escalation=2)
    rig.run_to(T0 + 100)
    rig.broadcasting.discard(2)
    rig.run_to(T0 + 111)
    assert rig.machine.state is DEGRADED
    rig.broadcasting.add(2)
    assert rig.run_to(T0 + 111.5) == [Transition(DEGRADED, HEALTHY, ALL_OK), *online(2)]

    rig.run_to(T0 + 411)
    assert rig.times(SetEscalation) == []
    assert rig.run_to(T0 + 411.5) == [SetEscalation(0)]
    rig.run_to(T0 + 1500)
    assert rig.times(SetEscalation) == [T0 + 411.5]
    assert rig.machine.escalation == 0

    clean = healthy_rig()
    clean.run_to(T0 + 1000)
    assert clean.times(SetEscalation) == []


@pytest.mark.req("HEALTH-08", "HEALTH-11", "HEALTH-22", "HEALTH-23")
def test_error_frames_suspend_polling_only() -> None:
    rig = healthy_rig()
    rig.run_to(T0 + 5)
    for _ in range(9):
        rig.machine.on_error_frame(T0 + 5)
    assert rig.tick() == []
    assert not rig.machine.polling_suspended

    rig.machine.on_error_frame(T0 + 5.5)
    assert rig.tick() == [SuspendPolling("polling_suspended: 10 error frames within 60 s")]
    assert rig.machine.polling_suspended
    assert rig.machine.state is HEALTHY
    for _ in range(50):
        rig.machine.on_error_frame(T0 + 6)
    suspended_at = T0 + 6

    rig.poll_answers = False
    rig.cells.discard(2)
    rig.run_to(T0 + 200)
    assert rig.machine.state is DEGRADED

    rig.kill_bus()
    for _ in range(100):
        if rig.machine.state is STALE:
            break
        rig.machine.on_error_frame(rig.now)
        rig.tick()
    assert rig.machine.state is STALE
    assert rig.now == T0 + 215.5

    rig.bus_alive = True
    rig.run_to(T0 + 260)
    assert rig.transitions()[-2:] == [
        (STALE, STARTING, "bus_resumed"),
        (STARTING, DEGRADED, "modules_not_ok: m1 cells_stale, m2 cells_stale, m3 cells_stale"),
    ]
    assert rig.polls(since=suspended_at) == []
    assert rig.times(SuspendPolling) == [T0 + 6]


@pytest.mark.req("HEALTH-23")
def test_error_window_slides() -> None:
    rig = healthy_rig()
    rig.run_to(T0 + 5)
    for _ in range(9):
        rig.machine.on_error_frame(T0 + 5)
    rig.run_to(T0 + 65.5)
    for _ in range(9):
        rig.machine.on_error_frame(T0 + 65.5)
    rig.tick()

    assert not rig.machine.polling_suspended
    assert rig.times(SuspendPolling) == []

    rig.run_to(T0 + 125.5)
    rig.machine.on_error_frame(T0 + 125.5)
    rig.tick()
    assert rig.machine.polling_suspended


@pytest.mark.req("HEALTH-18")
def test_every_reason_uses_a_known_code() -> None:
    assert REASON_CODES == (
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
    rigs: list[Rig] = []

    silence = healthy_rig()
    silence.run_to(T0 + 5)
    silence.kill_bus()
    silence.run_to(T0 + 60)
    rigs.append(silence)

    failure = healthy_rig()
    failure.machine.on_bus_exception(failure.now, "OS_ERROR: gone")
    failure.run_to(T0 + 10)
    for _ in range(10):
        failure.machine.on_error_frame(failure.now)
    failure.run_to(T0 + 11)
    rigs.append(failure)

    for escalation, open_ok, detail in (
        (3, True, ""),
        (0, False, "adapter_absent"),
        (0, False, "x"),
    ):
        dead = Rig(escalation=escalation, open_ok=open_ok, detail=detail)
        dead.kill_bus()
        dead.run_to(T0 + 900)
        rigs.append(dead)

    reasons = [
        a.reason
        for rig in rigs
        for _, a in rig.log
        if isinstance(a, Transition | SuspendPolling | Exit)
    ]
    reasons += [rig.machine.reason for rig in rigs]
    shape = re.compile(r"(?P<code>[a-z_]+)(?:: .+)?")
    codes = set()
    for reason in reasons:
        match = shape.fullmatch(reason)
        assert match is not None, reason
        assert match["code"] in REASON_CODES, reason
        codes.add(match["code"])
    assert codes == set(REASON_CODES)
