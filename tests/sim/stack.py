"""A simulated 3 x Dyness B3 stack on a python-can ``virtual`` bus (TEST-21..25).

``SimulatedStack`` is driven by a ``FakeClock``: ``advance(dt)`` moves the clock through
every frame due in the interval, in timestamp order and without threads (TEST-22). Per module
it sends the broadcast frames ``0x16`` and ``0x21``-``0x24`` at about 1.7 Hz, unsolicited, and
the cell stream ``0x11``-``0x15``, ``0x17`` at about 1 Hz once the module has heard a poll; for
the stack it sends the heartbeat at 10 Hz and the inverter frames at about 1 Hz (TEST-21).

Every frame the service sends is recorded in ``received`` and, with ``echo_polls``, sent back
to model the adapter echoing its own transmissions (TEST-23). Faults and values are set with
the knobs of TEST-24, at any simulated time and also from ``schedule``. Defaults come from the
vendored captures, including the two weak cells: module 2 cell 8 and module 3 cell 9.

Timing rules: a tick that falls in a gap the simulator did not drive (the clock was moved
by something else) is dropped rather than sent late in a burst, and ticks that fall while a
module or the bus is silenced are dropped too. Polls heard while the polled module or the bus
is silenced do not start a cell stream; echoes and injected error frames model the adapter and
the controller, so silence does not suppress them.

Frames are encoded by ``tests/sim/encode.py`` only; the package's ``protocol`` module is never
imported here (TEST-25).
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Self

import can

from tests.fakes.clock import FakeClock
from tests.sim import encode

BROADCAST_PERIOD_S = 1 / 1.7
CELL_PERIOD_S = 1.0
HEARTBEAT_PERIOD_S = 0.1
INVERTER_PERIOD_S = 1.0
POLL_RESPONSE_DELAY_S = 0.04
"""Delay from a poll to the first cell-stream frame (about 38 ms in the t2 capture)."""

_NEVER = math.inf


@dataclass
class SimModule:
    """The state one simulated module reports; change it through the stack's knobs."""

    number: int
    cells_mv: list[int]
    temps_c: tuple[float, float, float]
    mos_c: float
    board_c: float
    cycles: int
    energy_kwh: float
    current_a: float
    max_discharge_a: float
    max_charge_a: float
    temp_max_c: float
    temp_max_sensor: int
    temp_min_c: float
    temp_min_sensor: int
    soh: int
    soc: int
    flags: bytes
    silenced: bool = False
    streaming: bool = False

    @property
    def pack_v(self) -> float:
        """Return the pack voltage: the sum of the cells."""
        return round(sum(self.cells_mv) / 1000, 2)

    @property
    def avg_cell_mv(self) -> int:
        """Return the mean cell voltage in mV."""
        return round(sum(self.cells_mv) / len(self.cells_mv))


def _mv(text: str) -> list[int]:
    """Return the cell voltages written as space-separated mV."""
    return [int(word) for word in text.split()]


DEFAULT_MODULES: dict[int, SimModule] = {
    1: SimModule(
        number=1,
        cells_mv=_mv("3321 3320 3316 3322 3320 3322 3298 3320 3318 3319 3321 3321 3315 3320 3320"),
        temps_c=(17.7, 18.0, 17.4),
        mos_c=23.8,
        board_c=19.5,
        cycles=1035,
        energy_kwh=3623.04,
        current_a=4.9,
        max_discharge_a=37.5,
        max_charge_a=37.5,
        temp_max_c=18.0,
        temp_max_sensor=1,
        temp_min_c=17.4,
        temp_min_sensor=2,
        soh=97,
        soc=37,
        flags=bytes([0, 0, 0x03, 0, 0, 0, 0, 0]),
    ),
    2: SimModule(
        number=2,
        cells_mv=_mv("3293 3311 3310 3315 3313 3316 3309 3286 3315 3316 3319 3305 3313 3312 3316"),
        temps_c=(17.2, 17.6, 17.2),
        mos_c=23.2,
        board_c=20.2,
        cycles=998,
        energy_kwh=3494.31,
        current_a=4.2,
        max_discharge_a=37.5,
        max_charge_a=37.5,
        temp_max_c=17.6,
        temp_max_sensor=1,
        temp_min_c=17.2,
        temp_min_sensor=2,
        soh=97,
        soc=9,
        flags=bytes([0, 0, 0x83, 0, 0, 0, 0, 0]),
    ),
    3: SimModule(
        number=3,
        cells_mv=_mv("3325 3316 3321 3320 3323 3325 3323 3316 3291 3324 3327 3320 3317 3309 3325"),
        temps_c=(16.7, 17.2, 16.8),
        mos_c=23.9,
        board_c=20.5,
        cycles=972,
        energy_kwh=3403.64,
        current_a=4.0,
        max_discharge_a=37.5,
        max_charge_a=37.5,
        temp_max_c=17.2,
        temp_max_sensor=1,
        temp_min_c=16.7,
        temp_min_sensor=0,
        soh=97,
        soc=8,
        flags=bytes([0, 0, 0x83, 0, 0, 0, 0, 0]),
    ),
}
"""Defaults from the t1/t2 captures (``tests/vectors/``); module 4, if simulated, copies 1."""


@dataclass(frozen=True, slots=True)
class Received:
    """A frame the service sent, stamped with the simulated time it was heard."""

    at: float
    arbitration_id: int
    extended: bool
    data: bytes


@dataclass
class _Emitter:
    """A periodic source of frames; ``next_at`` is ``inf`` while it is stopped."""

    order: int
    period: float
    next_at: float
    fire: Callable[[], int]


@dataclass(order=True)
class _Scheduled:
    at: float
    seq: int
    fn: Callable[[], object] = field(compare=False)


class SimulatedStack:
    """The simulated stack on ``channel``; see the module docstring for its behaviour."""

    def __init__(
        self, channel: str, clock: FakeClock, module_count: int = 3, *, echo_polls: bool = False
    ) -> None:
        if not 1 <= module_count <= 4:
            raise ValueError("module_count must be 1..4")
        self.channel = channel
        self.clock = clock
        self.module_count = module_count
        self.echo_polls = echo_polls
        self.bus = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
        self.received: list[Received] = []
        self.trace: list[tuple[float, int]] | None = None
        self.frames_sent = 0
        self._bus_silenced = False
        self._closed = False
        self._scheduled: list[_Scheduled] = []
        self._seq = 0
        self._modules = {
            m: replace(DEFAULT_MODULES[m if m in DEFAULT_MODULES else 1], number=m)
            for m in range(1, module_count + 1)
        }
        for module in self._modules.values():
            module.cells_mv = list(module.cells_mv)
        start = clock()
        self._emitters: list[_Emitter] = [
            _Emitter(0, HEARTBEAT_PERIOD_S, start + HEARTBEAT_PERIOD_S, self._heartbeat),
            _Emitter(1, INVERTER_PERIOD_S, start + 0.5, self._inverter),
        ]
        self._cell_emitters: dict[int, _Emitter] = {}
        for m in self._modules:
            self._emitters.append(
                _Emitter(1 + m, BROADCAST_PERIOD_S, start + 0.05 * m, self._broadcast_fn(m))
            )
            cells = _Emitter(5 + m, CELL_PERIOD_S, _NEVER, self._cells_fn(m))
            self._cell_emitters[m] = cells
            self._emitters.append(cells)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Shut the simulator's bus down; idempotent."""
        if not self._closed:
            self._closed = True
            self.bus.shutdown()

    def module(self, m: int) -> SimModule:
        """Return the state of module ``m``."""
        if m not in self._modules:
            raise ValueError(f"module {m} is not simulated (1..{self.module_count})")
        return self._modules[m]

    def advance(self, dt: float) -> None:
        """Move the clock forward by ``dt``, sending every frame due on the way."""
        self._run(self.clock() + dt, stop_on_frame=False)

    def advance_until_frame(self, limit: float) -> bool:
        """Advance until a frame has been sent, at most ``limit`` seconds.

        Returns ``True`` with the clock at the time of that frame, or ``False`` with the clock
        moved by ``limit`` when nothing was sent.
        """
        return self._run(self.clock() + limit, stop_on_frame=True)

    def schedule(self, at: float, fn: Callable[[], object]) -> None:
        """Call ``fn()`` when simulated time reaches ``at``; a past ``at`` runs on the next step."""
        self._seq += 1
        self._scheduled.append(_Scheduled(at, self._seq, fn))
        self._scheduled.sort()

    def silence_module(self, m: int) -> None:
        """Module ``m`` sends nothing until ``resume_module``."""
        self.module(m).silenced = True

    def resume_module(self, m: int) -> None:
        """Module ``m`` sends again."""
        self.module(m).silenced = False

    def silence_bus(self) -> None:
        """Nothing at all is sent by the stack until ``resume_bus``."""
        self._bus_silenced = True

    def resume_bus(self) -> None:
        """The stack sends again."""
        self._bus_silenced = False

    def stop_cell_stream(self, m: int) -> None:
        """The cell stream of module ``m`` stops until the next poll."""
        self.module(m).streaming = False
        self._cell_emitters[m].next_at = _NEVER

    def inject_error_frames(self, n: int, error_class: int = encode.CONTROLLER_ERROR) -> None:
        """Send ``n`` CAN error frames now."""
        for _ in range(n):
            self._send(encode.error_frame(error_class))

    def set_cells(self, m: int, mv: Sequence[int]) -> None:
        """Set the 15 cell voltages of module ``m``; ``0x21`` and ``0x22`` follow."""
        if len(mv) != encode.CELLS:
            raise ValueError(f"expected {encode.CELLS} cell voltages")
        self.module(m).cells_mv = list(mv)

    def set_flags(self, m: int, raw: bytes) -> None:
        """Set the raw 8-byte ``0x24`` payload of module ``m``."""
        if len(raw) != 8:
            raise ValueError("0x24 carries exactly 8 bytes")
        self.module(m).flags = bytes(raw)

    def set_current(self, m: int, amps: float) -> None:
        """Set module ``m``'s current, positive when charging (negated on the wire)."""
        self.module(m).current_a = amps

    def set_soc(self, m: int, pct: int) -> None:
        """Set module ``m``'s SOC; the inverter ``0x355`` reports the mean."""
        self.module(m).soc = pct

    def _run(self, target: float, *, stop_on_frame: bool) -> bool:
        self._drop_missed_ticks()
        if self._drain() and stop_on_frame:
            return True
        while True:
            due = self._next_due()
            if due is None or due > target:
                break
            if due > self.clock():
                self.clock.advance(due - self.clock())
            sent = self._fire_due(due) + self._drain()
            if sent and stop_on_frame:
                return True
        if target > self.clock():
            self.clock.advance(target - self.clock())
        return False

    def _drop_missed_ticks(self) -> None:
        now = self.clock()
        for emitter in self._emitters:
            if emitter.next_at < now:
                missed = math.ceil((now - emitter.next_at) / emitter.period - 1e-9)
                emitter.next_at += missed * emitter.period

    def _next_due(self) -> float | None:
        times = [e.next_at for e in self._emitters if e.next_at != _NEVER]
        if self._scheduled:
            times.append(self._scheduled[0].at)
        return min(times) if times else None

    def _fire_due(self, due: float) -> int:
        """Run every scheduled call, then every emitter, due at ``due``; return frames sent."""
        sent = 0
        while self._scheduled and self._scheduled[0].at <= due:
            self._scheduled.pop(0).fn()
        for emitter in sorted(self._emitters, key=lambda e: e.order):
            if emitter.next_at <= due:
                sent += emitter.fire()
                if emitter.next_at != _NEVER:
                    emitter.next_at += emitter.period
        return sent

    def _drain(self) -> int:
        """Record what the service sent, echo it if asked, start polled cell streams."""
        sent = 0
        while (message := self.bus.recv(timeout=0)) is not None:
            data = bytes(message.data)
            heard = Received(self.clock(), message.arbitration_id, message.is_extended_id, data)
            self.received.append(heard)
            if self.echo_polls:
                self._send(encode.Frame(heard.arbitration_id, heard.extended, data))
                sent += 1
            polled = encode.polled_module(heard.arbitration_id, heard.extended, data)
            if polled in self._modules and not self._is_silent(polled):
                self._start_cell_stream(polled)
        return sent

    def _start_cell_stream(self, m: int) -> None:
        module = self._modules[m]
        if not module.streaming:
            module.streaming = True
            self._cell_emitters[m].next_at = self.clock() + POLL_RESPONSE_DELAY_S

    def _is_silent(self, m: int | None = None) -> bool:
        return self._bus_silenced or (m is not None and self._modules[m].silenced)

    def _send(self, frame: encode.Frame) -> None:
        self.bus.send(
            can.Message(
                arbitration_id=frame.arbitration_id,
                is_extended_id=frame.extended,
                is_error_frame=frame.error,
                data=frame.data,
            )
        )
        self.frames_sent += 1
        if self.trace is not None:
            self.trace.append((self.clock(), frame.arbitration_id))

    def _send_all(self, frames: Sequence[encode.Frame]) -> int:
        for frame in frames:
            self._send(frame)
        return len(frames)

    def _heartbeat(self) -> int:
        return 0 if self._is_silent() else self._send_all([encode.heartbeat()])

    def _inverter(self) -> int:
        if self._is_silent():
            return 0
        modules = list(self._modules.values())
        count = len(modules)
        return self._send_all(
            [
                encode.charge_limits(53.0, 112.5, 37.5, 45.0),
                encode.stack_soc_soh(
                    round(sum(m.soc for m in modules) / count),
                    round(sum(m.soh for m in modules) / count),
                ),
                encode.stack_measurements(
                    sum(m.pack_v for m in modules) / count,
                    round(sum(m.current_a for m in modules), 1),
                    18.0,
                ),
                encode.stack_module_count(count),
                encode.charge_request(0xC0),
                encode.stack_capacity(225),
                encode.stack_words((100, 15, 105, 225)),
            ]
        )

    def _broadcast_fn(self, m: int) -> Callable[[], int]:
        def fire() -> int:
            if self._is_silent(m):
                return 0
            s = self._modules[m]
            return self._send_all(
                [
                    encode.mos_board_cycles(m, s.mos_c, s.board_c, s.cycles),
                    encode.pack_status(m, s.pack_v, s.current_a, s.avg_cell_mv, s.max_discharge_a),
                    encode.cell_extremes(m, s.cells_mv, s.max_charge_a),
                    encode.temp_extremes(
                        m,
                        s.temp_max_c,
                        s.temp_max_sensor,
                        s.temp_min_c,
                        s.temp_min_sensor,
                        s.soh,
                        s.soc,
                    ),
                    encode.status_flags(m, s.flags),
                ]
            )

        return fire

    def _cells_fn(self, m: int) -> Callable[[], int]:
        def fire() -> int:
            s = self._modules[m]
            if self._is_silent(m) or not s.streaming:
                return 0
            return self._send_all(
                [
                    *encode.cell_voltages(m, s.cells_mv),
                    encode.cell_temperatures(m, s.temps_c),
                    encode.energy(m, s.energy_kwh),
                ]
            )

        return fire
