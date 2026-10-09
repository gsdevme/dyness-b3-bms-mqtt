"""Tests of the simulated stack (11-testing.md section 5, TEST-21..25).

The simulator is checked from the outside, through a second python-can ``virtual`` bus on
the same channel. Its frames are decoded here with the package decoder as a cross-check;
the simulator itself never imports it (TEST-25).
"""

import ast
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import can
import pytest

from dyness_b3_bms_mqtt import protocol
from dyness_b3_bms_mqtt.canbus import PythonCanTransport, RxFrame
from tests.fakes.clock import FakeClock
from tests.fakes.flaky import FlakyFactory
from tests.sim.paced import SimPacedBus, paced_factory
from tests.sim.stack import Received, SimulatedStack
from tests.support.settings import make_settings

SIM_DIR = Path(__file__).resolve().parents[1] / "sim"
POLL_DATA = bytes([1, 0, 0, 0, 0, 0, 0, 0])
BROADCAST = (0x16, 0x21, 0x22, 0x23, 0x24)
CELL_STREAM = (0x11, 0x12, 0x13, 0x14, 0x15, 0x17)
INVERTER = (0x351, 0x355, 0x356, 0x359, 0x35C, 0x35F, 0x70D)
HEARTBEAT = 0x18F20103


def module_id(module: int, code: int) -> int:
    return 0x18F21000 | module << 8 | code


def poll(module: int) -> can.Message:
    return can.Message(arbitration_id=module_id(module, 0x77), data=POLL_DATA)


@pytest.fixture
def sim(can_channel: str, clock: FakeClock) -> Iterator[SimulatedStack]:
    with SimulatedStack(can_channel, clock) as stack:
        yield stack


@pytest.fixture
def listener(can_channel: str) -> Iterator[can.BusABC]:
    with can.Bus(interface="virtual", channel=can_channel) as bus:
        yield bus


def drain(bus: can.BusABC) -> list[can.Message]:
    messages = []
    while (message := bus.recv(0)) is not None:
        messages.append(message)
    return messages


def counts(messages: list[can.Message]) -> Counter[int]:
    return Counter(m.arbitration_id for m in messages if not m.is_error_frame)


def latest(messages: list[can.Message], arbitration_id: int) -> object:
    [*_, last] = [m for m in messages if m.arbitration_id == arbitration_id]
    return protocol.decode(last.arbitration_id, last.is_extended_id, bytes(last.data))


@pytest.mark.req("TEST-21", "TEST-22")
def test_sim_broadcast_rates_and_cell_stream_after_poll(
    sim: SimulatedStack, listener: can.BusABC, clock: FakeClock
) -> None:
    sim.trace = []
    sim.advance(10.0)
    assert clock() == pytest.approx(1010.0)
    seen = counts(drain(listener))
    for module in (1, 2, 3):
        for code in BROADCAST:
            assert 16 <= seen[module_id(module, code)] <= 18, (module, code)
        for code in CELL_STREAM:
            assert seen[module_id(module, code)] == 0
    assert 99 <= seen[HEARTBEAT] <= 101
    for can_id in INVERTER:
        assert 9 <= seen[can_id] <= 11
    times = [at for at, _ in sim.trace]
    assert times == sorted(times)
    assert times[0] >= 1000.0
    assert times[-1] <= 1010.0

    listener.send(poll(2))
    sim.advance(5.0)
    seen = counts(drain(listener))
    for code in CELL_STREAM:
        assert 4 <= seen[module_id(2, code)] <= 6
        assert seen[module_id(1, code)] == seen[module_id(3, code)] == 0
    sim.advance(5.0)
    seen = counts(drain(listener))
    for code in CELL_STREAM:
        assert 4 <= seen[module_id(2, code)] <= 6


@pytest.mark.req("TEST-22")
def test_sim_drops_ticks_missed_while_the_clock_jumped(
    sim: SimulatedStack, listener: can.BusABC, clock: FakeClock
) -> None:
    sim.advance(1.0)
    drain(listener)
    clock.advance(30.0)
    sim.advance(0.0)
    assert counts(drain(listener))[HEARTBEAT] <= 1
    sim.advance(1.0)
    assert 9 <= counts(drain(listener))[HEARTBEAT] <= 11


@pytest.mark.req("TEST-23")
def test_sim_records_and_echoes(can_channel: str, clock: FakeClock, listener: can.BusABC) -> None:
    with SimulatedStack(can_channel, clock, echo_polls=True) as echoing:
        echoing.silence_bus()
        listener.send(poll(1))
        listener.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x01"))
        echoing.advance(0.0)
        assert echoing.received == [
            Received(1000.0, 0x18F21177, True, POLL_DATA),
            Received(1000.0, 0x123, False, b"\x01"),
        ]
        echoes = drain(listener)
        assert [(m.arbitration_id, bytes(m.data)) for m in echoes] == [
            (0x18F21177, POLL_DATA),
            (0x123, b"\x01"),
        ]
    with SimulatedStack(can_channel, clock) as quiet:
        quiet.silence_bus()
        listener.send(poll(2))
        quiet.advance(0.5)
        assert [r.arbitration_id for r in quiet.received] == [0x18F21277]
        assert drain(listener) == []


@pytest.mark.req("TEST-24")
def test_sim_knobs(sim: SimulatedStack, listener: can.BusABC, clock: FakeClock) -> None:
    sim.silence_module(2)
    sim.advance(2.0)
    seen = counts(drain(listener))
    assert not any(seen[module_id(2, code)] for code in BROADCAST)
    assert all(seen[module_id(1, code)] for code in BROADCAST)
    sim.resume_module(2)
    sim.advance(2.0)
    seen = counts(drain(listener))
    assert all(seen[module_id(2, code)] for code in BROADCAST)

    sim.silence_bus()
    sim.advance(2.0)
    assert drain(listener) == []
    sim.resume_bus()
    sim.advance(1.0)
    assert counts(drain(listener))[HEARTBEAT] >= 9

    listener.send(poll(1))
    sim.advance(2.0)
    assert counts(drain(listener))[module_id(1, 0x11)] >= 1
    sim.stop_cell_stream(1)
    sim.advance(3.0)
    assert counts(drain(listener))[module_id(1, 0x11)] == 0
    listener.send(poll(1))
    sim.advance(2.0)
    assert counts(drain(listener))[module_id(1, 0x11)] >= 1

    sim.inject_error_frames(3)
    errors = [m for m in drain(listener) if m.is_error_frame]
    assert [m.arbitration_id for m in errors] == [0x004, 0x004, 0x004]

    cells = [3300 + n for n in range(15)]
    cells[4] = 3250
    cells[11] = 3400
    sim.set_cells(1, cells)
    sim.set_flags(3, bytes([0x01, 0, 0x83, 0x10, 0, 0, 0, 0]))
    sim.set_current(2, 10.0)
    sim.set_soc(1, 80)
    sim.advance(1.5)
    frames = drain(listener)
    voltages: dict[int, int] = {}
    for code in (0x11, 0x12, 0x13, 0x14):
        decoded = latest(frames, module_id(1, code))
        assert isinstance(decoded, protocol.CellVoltages)
        voltages.update(decoded.cells_mv)
    assert [voltages[n] for n in range(1, 16)] == cells
    extremes = latest(frames, module_id(1, 0x22))
    assert isinstance(extremes, protocol.CellExtremes)
    assert (extremes.cell_max_mv, extremes.cell_max_no) == (3400, 12)
    assert (extremes.cell_min_mv, extremes.cell_min_no) == (3250, 5)
    status = latest(frames, module_id(1, 0x21))
    assert isinstance(status, protocol.PackStatus)
    assert status.avg_cell_mv == round(sum(cells) / 15)
    assert status.pack_v == pytest.approx(sum(cells) / 1000, abs=0.01)
    flags = latest(frames, module_id(3, 0x24))
    assert isinstance(flags, protocol.StatusFlags)
    assert flags.flags_raw == "0100831000000000"
    pack = latest(frames, module_id(2, 0x21))
    assert isinstance(pack, protocol.PackStatus)
    assert pack.current_raw_a == -10.0
    stack = latest(frames, 0x356)
    assert isinstance(stack, protocol.StackMeasurements)
    assert stack.current_a == pytest.approx(10.0 + 4.9 + 4.0)
    temps = latest(frames, module_id(1, 0x23))
    assert isinstance(temps, protocol.TempExtremes)
    assert temps.soc == 80
    soc = latest(frames, 0x355)
    assert isinstance(soc, protocol.StackSocSoh)
    assert soc.soc == round((80 + 9 + 8) / 3)

    start = clock()
    sim.schedule(start + 1.0, sim.silence_bus)
    sim.advance(2.0)
    late = [m for m in drain(listener) if m.arbitration_id == HEARTBEAT]
    assert 9 <= len(late) <= 11
    sim.resume_bus()


@pytest.mark.req("TEST-24")
def test_sim_defaults_weak_cells(sim: SimulatedStack) -> None:
    for module, weak in [(2, 8), (3, 9)]:
        cells = sim.module(module).cells_mv
        neighbours = (cells[weak - 2] + cells[weak]) / 2
        assert 20 <= neighbours - cells[weak - 1] <= 35
        assert cells[weak - 1] == min(cells)
    first = sim.module(1)
    assert len(first.cells_mv) == 15
    assert all(3200 <= mv <= 3400 for m in (1, 2, 3) for mv in sim.module(m).cells_mv)
    assert (first.current_a, first.soc, first.soh) == (4.9, 37, 97)
    assert sim.module(1).flags == bytes([0, 0, 0x03, 0, 0, 0, 0, 0])


@pytest.mark.req("TEST-25")
def test_sim_does_not_import_protocol() -> None:
    sources = sorted(SIM_DIR.glob("*.py"))
    assert {path.name for path in sources} >= {"__init__.py", "encode.py", "stack.py", "paced.py"}
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(alias.name for alias in node.names)]
            else:
                continue
            assert not any("dyness_b3_bms_mqtt" in n or "protocol" in n for n in names), (
                path.name,
                names,
            )


@pytest.mark.req("TEST-22", "CAN-03")
def test_paced_transport_runs_on_simulated_time(
    sim: SimulatedStack, clock: FakeClock, can_channel: str
) -> None:
    flaky = FlakyFactory(paced_factory(sim))
    settings = make_settings(CAN_CHANNEL=can_channel)
    with PythonCanTransport(settings, clock, clock.sleep, bus_factory=flaky) as transport:
        transport.open()
        assert isinstance(flaky.opened[0].inner, SimPacedBus)
        frame = transport.recv(0.2)
        assert isinstance(frame, RxFrame)
        assert frame.at == clock() <= 1000.2
        transport.send_poll(3)
        deadline = clock() + 2.0
        cells: list[RxFrame] = []
        while clock() < deadline:
            got = transport.recv(0.2)
            if isinstance(got, RxFrame) and got.arbitration_id == module_id(3, 0x11):
                cells.append(got)
        assert cells
        assert [r.arbitration_id for r in sim.received] == [0x18F21377]
        sim.silence_bus()
        while transport.recv(0.0) is not None:
            pass
        before = clock()
        assert transport.recv(0.2) is None
        assert clock() == pytest.approx(before + 0.2)
    assert flaky.all_shut_down()
