"""Tests of the python-can transport: 02-can-transport.md sections 1-6 and 8.

python-can is replaced by recording buses wherever a real adapter would be needed; the
``virtual`` interface is used where real python-can behaviour matters (filtering, timing,
the contract tests).
"""

import errno
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import can
import pytest

from dyness_b3_bms_mqtt import protocol
from dyness_b3_bms_mqtt.canbus import (
    ACCEPT_FILTERS,
    CAN_ERR_MASK,
    CAN_RAW_ERR_FILTER,
    GS_CAN_MODE_HW_TIMESTAMP,
    GS_CAN_MODE_LISTEN_ONLY,
    RECV_TIMEOUT_S,
    REOPEN_GAP_S,
    SEND_TIMEOUT_S,
    SOL_CAN_RAW,
    CanTransport,
    FaultKind,
    ListenOnlyError,
    PythonCanTransport,
    RxErrorFrame,
    RxFrame,
    TransportFault,
    accepts,
    bus_kwargs,
    map_exception,
)
from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.linkstate import CanState, LinkState, unknown_link_state
from tests.fakes.clock import FakeClock
from tests.fakes.flaky import FlakyFactory
from tests.fakes.transport import FakeTransport, RecordingTransport
from tests.support.settings import make_settings

POLL_DATA = bytes([1, 0, 0, 0, 0, 0, 0, 0])


class FakeSocket:
    """Records ``setsockopt`` calls; optionally raises ``fail``."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.options: list[tuple[int, int, int]] = []
        self.fail = fail

    def setsockopt(self, level: int, option: int, value: int) -> None:
        if self.fail is not None:
            raise self.fail
        self.options.append((level, option, value))


@dataclass
class RecorderBus:
    """A python-can bus stand-in: scripted ``recv``, recorded ``send`` and ``shutdown``.

    ``recv`` pops the next scripted item: a message is returned after advancing ``clock`` by
    ``step``; an exception is raised; an empty script advances ``clock`` by the whole timeout
    and returns ``None``, as a real bus would after waiting.
    """

    kwargs: dict[str, Any]
    clock: FakeClock | None = None
    step: float = 0.0
    incoming: deque[can.Message | BaseException] = field(default_factory=deque)
    timeouts: list[float | None] = field(default_factory=list)
    sent: list[tuple[can.Message, float | None]] = field(default_factory=list)
    send_error: BaseException | None = None
    shutdown_error: BaseException | None = None
    shutdowns: int = 0
    socket: FakeSocket = field(default_factory=FakeSocket)
    gs_usb: Any = None
    timeline: list[object] | None = None

    def recv(self, timeout: float | None = None) -> can.Message | None:
        self.timeouts.append(timeout)
        if self.incoming:
            item = self.incoming.popleft()
            if isinstance(item, BaseException):
                raise item
            if self.clock is not None:
                self.clock.advance(self.step)
            return item
        if self.clock is not None and timeout is not None:
            self.clock.advance(timeout)
        return None

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((msg, timeout))

    def shutdown(self) -> None:
        self.shutdowns += 1
        if self.timeline is not None:
            self.timeline.append("shutdown")
        if self.shutdown_error is not None:
            raise self.shutdown_error


class Recorder:
    """A ``can.Bus`` replacement that records keyword arguments and returns ``RecorderBus``."""

    def __init__(
        self,
        clock: FakeClock | None = None,
        prepare: Callable[[RecorderBus], None] | None = None,
        error: BaseException | None = None,
        timeline: list[object] | None = None,
    ) -> None:
        self.clock = clock
        self.prepare = prepare
        self.error = error
        self.timeline = timeline
        self.calls: list[dict[str, Any]] = []
        self.buses: list[RecorderBus] = []

    def __call__(self, **kwargs: Any) -> RecorderBus:
        self.calls.append(kwargs)
        if self.timeline is not None:
            self.timeline.append("open")
        if self.error is not None:
            raise self.error
        bus = RecorderBus(kwargs, clock=self.clock, timeline=self.timeline)
        if self.prepare is not None:
            self.prepare(bus)
        self.buses.append(bus)
        return bus

    @property
    def bus(self) -> RecorderBus:
        return self.buses[-1]


class LinkQuery:
    """A ``link_query`` stand-in returning a fixed state and recording its calls."""

    def __init__(self, listen_only: bool | None = False, bitrate: int | None = 500000) -> None:
        self.listen_only = listen_only
        self.bitrate = bitrate
        self.calls: list[tuple[str, float]] = []

    def __call__(self, channel: str, at: float) -> LinkState:
        self.calls.append((channel, at))
        return LinkState(
            CanState.ERROR_ACTIVE, "UP", self.listen_only, self.bitrate, 0, 0, 0, 0, 0, at
        )


def transport(
    settings: Settings,
    factory: Callable[..., Any],
    clock: FakeClock | None = None,
    *,
    sleep: Callable[[float], None] | None = None,
    link_query: Callable[[str, float], LinkState] | None = None,
    platform: str = "linux",
) -> PythonCanTransport:
    fake_clock = clock or FakeClock(1000.0)
    return PythonCanTransport(
        settings,
        fake_clock,
        sleep or fake_clock.sleep,
        bus_factory=factory,
        link_query=link_query or LinkQuery(),
        platform=platform,
    )


class FakeGsUsb:
    """Stands in for ``gs_usb.gs_usb.GsUsb``: ``start`` stores the flags the adapter accepts."""

    def __init__(self, supported: int) -> None:
        self.supported = supported
        self.device_flags: int | None = None
        self.started: list[int] = []

    def start(self, flags: int = GS_CAN_MODE_HW_TIMESTAMP) -> None:
        self.started.append(flags)
        self.device_flags = flags & self.supported


@pytest.fixture
def gs_usb_module(monkeypatch: pytest.MonkeyPatch) -> type[FakeGsUsb]:
    """Install fake ``gs_usb`` and ``gs_usb.gs_usb`` modules and return the ``GsUsb`` class."""
    package = ModuleType("gs_usb")
    module = ModuleType("gs_usb.gs_usb")
    gs_usb_class = type("GsUsb", (FakeGsUsb,), {})
    module.GsUsb = gs_usb_class  # type: ignore[attr-defined]
    package.gs_usb = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gs_usb", package)
    monkeypatch.setitem(sys.modules, "gs_usb.gs_usb", module)
    return gs_usb_class


def gs_usb_recorder(gs_usb_class: type[FakeGsUsb], supported: int, **kwargs: Any) -> Recorder:
    """Return a recorder that mimics python-can's gs_usb: construct, then ``GsUsb.start()``."""

    def prepare(bus: RecorderBus) -> None:
        device = gs_usb_class(supported)
        gs_usb_class.start(device)
        bus.gs_usb = device

    return Recorder(prepare=prepare, **kwargs)


@pytest.fixture
def usb_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install fake ``usb`` and ``usb.core`` modules and return ``usb.core.Device``."""

    class Device:
        def is_kernel_driver_active(self, interface: int) -> bool:
            return True

    package = ModuleType("usb")
    core = ModuleType("usb.core")
    core.Device = Device  # type: ignore[attr-defined]
    package.core = core  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "usb", package)
    monkeypatch.setitem(sys.modules, "usb.core", core)
    return Device


def msg(arbitration_id: int, extended: bool = True, data: bytes = bytes(8)) -> can.Message:
    return can.Message(arbitration_id=arbitration_id, is_extended_id=extended, data=data)


def error_msg(arbitration_id: int, data: bytes = bytes(8)) -> can.Message:
    return can.Message(arbitration_id=arbitration_id, is_error_frame=True, data=data)


def records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


INTERFACE_SETTINGS: dict[str, dict[str, str]] = {
    "socketcan": {"CAN_INTERFACE": "socketcan", "CAN_CHANNEL": "can0"},
    "gs_usb": {"CAN_INTERFACE": "gs_usb", "CAN_CHANNEL": "1", "CAN_BITRATE": "250000"},
    "slcan": {"CAN_INTERFACE": "slcan", "CAN_CHANNEL": "/dev/ttyACM0"},
    "virtual": {"CAN_INTERFACE": "virtual", "CAN_CHANNEL": "sim0"},
}

EXPECTED_FILTERS = [
    {"can_id": 0x18F21000, "can_mask": 0x1FFFF000, "extended": True},
    {"can_id": 0x18F20103, "can_mask": 0x1FFFFFFF, "extended": True},
    *(
        {"can_id": can_id, "can_mask": 0x7FF, "extended": False}
        for can_id in (0x351, 0x355, 0x356, 0x359, 0x35C, 0x35F, 0x70D)
    ),
]

EXPECTED_KWARGS: dict[str, dict[str, Any]] = {
    "socketcan": {
        "interface": "socketcan",
        "channel": "can0",
        "can_filters": EXPECTED_FILTERS,
        "receive_own_messages": False,
    },
    "gs_usb": {"interface": "gs_usb", "channel": 1, "index": 1, "bitrate": 250000},
    "slcan": {"interface": "slcan", "channel": "/dev/ttyACM0", "bitrate": 500000},
    "virtual": {"interface": "virtual", "channel": "sim0", "receive_own_messages": False},
}


@pytest.mark.req("CAN-04", "CAN-08", "CAN-02", "CAN-13")
@pytest.mark.parametrize("interface", sorted(INTERFACE_SETTINGS))
def test_bus_kwargs_per_interface(interface: str, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(**INTERFACE_SETTINGS[interface])
    assert bus_kwargs(settings) == EXPECTED_KWARGS[interface]
    recorder = Recorder()
    monkeypatch.setattr(can, "Bus", recorder)
    fake_clock = FakeClock(5.0)
    real = PythonCanTransport(
        settings, fake_clock, fake_clock.sleep, link_query=LinkQuery(), platform="linux"
    )
    real.open()
    assert recorder.calls == [EXPECTED_KWARGS[interface]]
    real.close()
    assert list(ACCEPT_FILTERS) == EXPECTED_FILTERS


@pytest.mark.req("CAN-08", "CAN-09")
def test_accepts_matches_acceptance_set() -> None:
    accepted = [
        (0x18F21121, True),
        (0x18F21277, True),
        (0x18F21FFF, True),
        (0x18F20103, True),
        *((can_id, False) for can_id in (0x351, 0x355, 0x356, 0x359, 0x35C, 0x35F, 0x70D)),
    ]
    rejected = [
        (0x18F20104, True),
        (0x18F22121, True),
        (0x18F31121, True),
        (0x351, True),
        (0x18F21121 & 0x7FF, False),
        (0x305, False),
        (0x35E, False),
        (0x370, False),
        (0x123, False),
    ]
    assert all(accepts(can_id, ext) for can_id, ext in accepted)
    assert not any(accepts(can_id, ext) for can_id, ext in rejected)


@pytest.mark.req("CAN-05")
def test_gs_usb_darwin_stub_only_on_darwin(usb_module: Any, gs_usb_module: type) -> None:
    original = usb_module.is_kernel_driver_active
    seen: list[bool] = []

    def prepare(bus: RecorderBus) -> None:
        seen.append(usb_module().is_kernel_driver_active(0))

    settings = make_settings(**INTERFACE_SETTINGS["gs_usb"])
    transport(settings, Recorder(prepare=prepare), platform="linux").open()
    assert seen == [True]
    assert usb_module.is_kernel_driver_active is original
    slcan = make_settings(**INTERFACE_SETTINGS["slcan"])
    transport(slcan, Recorder(prepare=prepare), platform="darwin").open()
    assert seen == [True, True]
    transport(settings, Recorder(prepare=prepare), platform="darwin").open()
    assert seen == [True, True, False]


@pytest.mark.req("CAN-16")
def test_gs_usb_listen_only_wraps_start_and_restores(gs_usb_module: type[FakeGsUsb]) -> None:
    original = gs_usb_module.start
    settings = make_settings(**INTERFACE_SETTINGS["gs_usb"], CAN_LISTEN_ONLY="true")
    recorder = gs_usb_recorder(gs_usb_module, supported=0xFF)
    real = transport(settings, recorder)
    real.open()
    assert recorder.bus.gs_usb.started == [GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP]
    assert gs_usb_module.start is original
    real.close()
    assert recorder.bus.shutdowns == 1

    failing = Recorder(error=can.CanInitializationError("Cannot find device 1"))
    with pytest.raises(TransportFault) as caught:
        transport(settings, failing).open()
    assert (caught.value.op, caught.value.kind) == ("open", FaultKind.OPEN_FAILED)
    assert gs_usb_module.start is original

    normal = make_settings(**INTERFACE_SETTINGS["gs_usb"])
    plain = gs_usb_recorder(gs_usb_module, supported=0xFF)
    transport(normal, plain).open()
    assert plain.bus.gs_usb.started == [GS_CAN_MODE_HW_TIMESTAMP]


@pytest.mark.req("CAN-16", "CAN-06")
def test_gs_usb_listen_only_refused(gs_usb_module: type[FakeGsUsb]) -> None:
    settings = make_settings(**INTERFACE_SETTINGS["gs_usb"], CAN_LISTEN_ONLY="true")
    recorder = gs_usb_recorder(gs_usb_module, supported=GS_CAN_MODE_HW_TIMESTAMP)
    real = transport(settings, recorder)
    with pytest.raises(TransportFault) as caught:
        real.open()
    assert caught.value.op == "open"
    assert caught.value.kind is FaultKind.LISTEN_ONLY_REFUSED
    assert recorder.bus.shutdowns == 1
    with pytest.raises(TransportFault) as closed:
        real.recv(0.0)
    assert closed.value.kind is FaultKind.NOT_OPEN


@pytest.mark.req("CAN-06", "CAN-32", "CAN-10")
def test_open_failure_maps_and_shuts_partial_bus() -> None:
    virtual = make_settings()
    cases: list[tuple[BaseException, FaultKind, int | None]] = [
        (OSError(errno.ENODEV, "No such device"), FaultKind.DEVICE_GONE, errno.ENODEV),
        (can.CanInitializationError("Cannot find device 0"), FaultKind.OPEN_FAILED, None),
        (can.CanInterfaceNotImplementedError("unknown interface"), FaultKind.CAN_ERROR, None),
    ]
    for error, kind, code in cases:
        with pytest.raises(TransportFault) as caught:
            transport(virtual, Recorder(error=error)).open()
        assert (caught.value.op, caught.value.kind, caught.value.errno) == ("open", kind, code)
        assert caught.value.__cause__ is error

    try:
        raise OSError(errno.ENETDOWN, "Network is down")
    except OSError as cause:
        init_error = can.CanInitializationError("bind failed")
        init_error.__cause__ = cause
    with pytest.raises(TransportFault) as caught:
        transport(virtual, Recorder(error=init_error)).open()
    assert (caught.value.kind, caught.value.errno) == (FaultKind.LINK_DOWN, errno.ENETDOWN)

    socketcan = make_settings(**INTERFACE_SETTINGS["socketcan"])
    for failure, expected in [
        (OSError(errno.EPERM, "Operation not permitted"), FaultKind.OS_ERROR),
        (RuntimeError("unexpected"), None),
    ]:

        def prepare(bus: RecorderBus, failure: BaseException = failure) -> None:
            bus.socket = FakeSocket(fail=failure)

        recorder = Recorder(prepare=prepare)
        real = transport(socketcan, recorder)
        with pytest.raises(Exception) as raised:
            real.open()
        if expected is None:
            assert raised.value is failure
        else:
            assert isinstance(raised.value, TransportFault)
            assert raised.value.kind is expected
        assert recorder.bus.shutdowns == 1
        with pytest.raises(TransportFault) as closed:
            real.recv(0.0)
        assert closed.value.kind is FaultKind.NOT_OPEN


@pytest.mark.req("CAN-07")
def test_open_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    transport(make_settings(CAN_CHANNEL="sim0"), Recorder()).open()
    socketcan = make_settings(**INTERFACE_SETTINGS["socketcan"], CAN_LISTEN_ONLY="true")
    transport(socketcan, Recorder(), link_query=LinkQuery(True, 250000)).open()
    transport(socketcan, Recorder(), link_query=LinkQuery(True, None)).open()
    assert records(caplog, logging.INFO) == [
        "can bus open: interface=virtual channel=sim0 bitrate=500000 listen_only=false",
        "can bus open: interface=socketcan channel=can0 bitrate=250000 listen_only=true",
        "can bus open: interface=socketcan channel=can0 bitrate=unknown listen_only=true",
    ]


@pytest.mark.req("CAN-15", "CFG-13")
def test_socketcan_listen_only_mismatch_warns(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    listening = make_settings(**INTERFACE_SETTINGS["socketcan"], CAN_LISTEN_ONLY="true")
    normal = make_settings(**INTERFACE_SETTINGS["socketcan"])
    for settings, link_listen_only in [(listening, True), (normal, False)]:
        transport(settings, Recorder(), link_query=LinkQuery(link_listen_only)).open()
    assert records(caplog, logging.WARNING) == []
    real = transport(listening, Recorder(), link_query=LinkQuery(False))
    real.open()
    transport(normal, Recorder(), link_query=LinkQuery(True)).open()
    transport(listening, Recorder(), link_query=LinkQuery(None)).open()
    assert records(caplog, logging.WARNING) == [
        "CAN_LISTEN_ONLY=true but link can0 is not listen-only; transmission is still refused",
        "CAN_LISTEN_ONLY=false but link can0 is listen-only",
        "CAN_LISTEN_ONLY=true but the listen-only mode of link can0 cannot be determined; "
        "transmission is still refused",
    ]
    with pytest.raises(ListenOnlyError):
        real.send_poll(1)


@pytest.mark.req("CAN-18")
def test_slcan_listen_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    transport(make_settings(**INTERFACE_SETTINGS["slcan"]), Recorder()).open()
    assert records(caplog, logging.WARNING) == []
    settings = make_settings(**INTERFACE_SETTINGS["slcan"], CAN_LISTEN_ONLY="true")
    recorder = Recorder()
    real = transport(settings, recorder)
    real.open()
    assert records(caplog, logging.WARNING) == [
        "slcan has no hardware listen-only mode: the adapter is in normal mode and only "
        "software refusal to transmit applies"
    ]
    with pytest.raises(ListenOnlyError):
        real.send_poll(1)
    assert recorder.bus.sent == []


@pytest.mark.req("CAN-09", "CAN-12")
def test_recv_filters_without_ending_early_and_within_50ms(can_channel: str) -> None:
    fake_clock = FakeClock(100.0)
    recorder = Recorder(clock=fake_clock)
    real = transport(make_settings(), recorder, fake_clock)
    real.open()
    recorder.bus.step = 0.05
    recorder.bus.incoming.extend([msg(0x123, extended=False), msg(0x18F31121)])
    assert real.recv(0.2) is None
    assert fake_clock() == pytest.approx(100.2)
    assert recorder.bus.timeouts == pytest.approx([0.2, 0.15, 0.10])
    recorder.bus.incoming.extend([msg(0x305, extended=False), msg(0x18F21121, data=b"\x0c" * 8)])
    frame = real.recv(0.2)
    assert isinstance(frame, RxFrame)
    assert (frame.arbitration_id, frame.extended, frame.data) == (0x18F21121, True, b"\x0c" * 8)
    assert frame.at == pytest.approx(100.3)

    live = PythonCanTransport(
        make_settings(CAN_CHANNEL=can_channel),
        time.monotonic,
        fake_clock.sleep,
        link_query=LinkQuery(),
    )
    with live, can.Bus(interface="virtual", channel=can_channel) as peer:
        live.open()
        peer.send(msg(0x123, extended=False))
        start = time.monotonic()
        assert live.recv(0.2) is None
        elapsed = time.monotonic() - start
        assert 0.2 <= elapsed <= 0.25

        peer.send(msg(0x18F30000))
        timer = threading.Timer(0.05, peer.send, args=(msg(0x18F20103),))
        timer.start()
        start = time.monotonic()
        heartbeat = live.recv(0.2)
        elapsed = time.monotonic() - start
        timer.join()
        assert isinstance(heartbeat, RxFrame)
        assert (heartbeat.arbitration_id, heartbeat.extended) == (0x18F20103, True)
        assert 0.04 <= elapsed <= 0.2


@pytest.mark.req("CAN-11", "CAN-01")
def test_error_frames_returned_as_rx_error() -> None:
    fake_clock = FakeClock(50.0)
    recorder = Recorder(clock=fake_clock)
    real = transport(make_settings(), recorder, fake_clock)
    real.open()
    stamped = error_msg(0x004, bytes([0, 4, 0, 0, 0, 0, 0, 0]))
    stamped.timestamp = 1.0
    recorder.bus.incoming.extend([stamped, error_msg(0x20000040), msg(0x123, False)])
    first = real.recv(0.2)
    assert first == RxErrorFrame(0x004, bytes([0, 4, 0, 0, 0, 0, 0, 0]), 50.0)
    assert real.recv(0.2) == RxErrorFrame(0x040, bytes(8), 50.0)
    assert real.recv(0.2) is None
    with pytest.raises(AttributeError):
        first.error_class = 1  # type: ignore[misc]


@pytest.mark.req("CAN-10")
def test_socketcan_error_filter_set() -> None:
    assert (SOL_CAN_RAW, CAN_RAW_ERR_FILTER, CAN_ERR_MASK) == (101, 2, 0x1FFFFFFF)
    recorder = Recorder()
    transport(make_settings(**INTERFACE_SETTINGS["socketcan"]), recorder).open()
    assert recorder.bus.socket.options == [(101, 2, 0x1FFFFFFF)]
    virtual = Recorder()
    transport(make_settings(), virtual).open()
    assert virtual.bus.socket.options == []


@pytest.mark.req("CAN-12")
def test_recv_timeout_returns_none_quietly(caplog: pytest.LogCaptureFixture) -> None:
    assert RECV_TIMEOUT_S == 0.2
    fake_clock = FakeClock(0.0)
    recorder = Recorder(clock=fake_clock)
    real = transport(make_settings(), recorder, fake_clock)
    real.open()
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    assert real.recv(RECV_TIMEOUT_S) is None
    assert recorder.bus.timeouts == [RECV_TIMEOUT_S]
    assert [r for r in caplog.records if r.levelno > logging.DEBUG] == []


@pytest.mark.req("CAN-14", "CFG-13")
@pytest.mark.parametrize("interface", ["socketcan", "slcan", "virtual", "gs_usb"])
def test_send_poll_listen_only_raises_without_touching_bus(
    interface: str, monkeypatch: pytest.MonkeyPatch, gs_usb_module: type[FakeGsUsb]
) -> None:
    built: list[tuple[int, int]] = []
    monkeypatch.setattr(protocol, "build_poll", lambda m, n: built.append((m, n)))
    settings = make_settings(**INTERFACE_SETTINGS[interface], CAN_LISTEN_ONLY="true")
    recorder = gs_usb_recorder(gs_usb_module, supported=0xFF)
    real = transport(settings, recorder, link_query=LinkQuery(False))
    assert real.listen_only is True
    with pytest.raises(ListenOnlyError) as closed:
        real.send_poll(1)
    real.open()
    with pytest.raises(ListenOnlyError) as opened:
        real.send_poll(1)
    assert not isinstance(closed.value, TransportFault)
    assert not isinstance(opened.value, TransportFault)
    assert isinstance(opened.value, RuntimeError)
    assert recorder.bus.sent == []
    assert built == []


def forged_poll(arbitration_id: int, data: bytes = POLL_DATA) -> protocol.PollFrame:
    """Return a ``PollFrame`` that bypasses its own validation, as a buggy builder might."""
    frame = object.__new__(protocol.PollFrame)
    object.__setattr__(frame, "arbitration_id", arbitration_id)
    object.__setattr__(frame, "data", data)
    return frame


@pytest.mark.req("CAN-19", "CAN-20", "PROTO-30")
def test_send_poll_builds_via_build_poll_and_rechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    real = transport(make_settings(MODULE_COUNT="3"), recorder)
    real.open()
    calls: list[tuple[int, int]] = []
    original = protocol.build_poll

    def spy(module: int, module_count: int) -> protocol.PollFrame:
        calls.append((module, module_count))
        return original(module, module_count)

    monkeypatch.setattr(protocol, "build_poll", spy)
    real.send_poll(2)
    assert calls == [(2, 3)]
    [(sent, timeout)] = recorder.bus.sent
    assert (sent.arbitration_id, sent.is_extended_id, bytes(sent.data)) == (
        0x18F21277,
        True,
        POLL_DATA,
    )
    assert not sent.is_error_frame and not sent.is_remote_frame
    assert timeout == SEND_TIMEOUT_S
    for bad in (4, 0, True, 2.0):
        with pytest.raises(ValueError):
            real.send_poll(bad)  # type: ignore[arg-type]
    assert len(recorder.bus.sent) == 1

    for forged, module in [
        (forged_poll(0x18F26677), 1),
        (forged_poll(0x18F21377), 1),
        (forged_poll(0x18F21177, bytes(8)), 1),
        (forged_poll(0x18F21177 & 0x7FF), 1),
    ]:
        monkeypatch.setattr(protocol, "build_poll", lambda m, n, f=forged: f)
        with pytest.raises(ValueError):
            real.send_poll(module)
    assert len(recorder.bus.sent) == 1


@pytest.mark.req("CAN-20", "CAN-21")
def test_send_timeout_bounded() -> None:
    assert SEND_TIMEOUT_S == 0.5
    recorder = Recorder()
    real = transport(make_settings(), recorder)
    real.open()
    real.send_poll(1)
    assert [timeout for _, timeout in recorder.bus.sent] == [0.5]
    for error in (
        can.CanOperationError("Transmit buffer full"),
        can.CanOperationError("Transmit timed out"),
        can.CanTimeoutError("send timed out"),
    ):
        recorder.bus.send_error = error
        with pytest.raises(TransportFault) as caught:
            real.send_poll(1)
        assert (caught.value.op, caught.value.kind) == ("send", FaultKind.TX_BLOCKED)


class USBError(OSError):
    """Mimics ``usb.core.USBError`` (pyusb): an ``OSError`` that may lack an errno."""


def wrapped(error: OSError, as_code: bool = False) -> can.CanOperationError:
    """Return ``error`` wrapped as python-can 4 does, by cause or by ``error_code``."""
    if as_code:
        return can.CanOperationError("Failed to transmit", error.errno)
    try:
        raise error
    except OSError as cause:
        try:
            raise can.CanOperationError("operation failed") from cause
        except can.CanOperationError as outer:
            return outer


@pytest.mark.req("CAN-34", "CAN-21", "CAN-32")
@pytest.mark.parametrize("op", ["recv", "send"])
@pytest.mark.parametrize("wrap", ["bare", "cause", "error_code"])
@pytest.mark.parametrize("code", [errno.ENODEV, errno.ENETDOWN, errno.ENOBUFS])
def test_exception_mapping_matrix(op: str, wrap: str, code: int) -> None:
    error: BaseException = OSError(code, "boom")
    if wrap != "bare":
        error = wrapped(OSError(code, "boom"), as_code=wrap == "error_code")
    expected = {
        errno.ENODEV: FaultKind.DEVICE_GONE,
        errno.ENETDOWN: FaultKind.LINK_DOWN,
        errno.ENOBUFS: FaultKind.TX_BLOCKED if op == "send" else FaultKind.OS_ERROR,
    }[code]
    recorder = Recorder()
    real = transport(make_settings(), recorder)
    real.open()
    if op == "recv":
        recorder.bus.incoming.append(error)
        with pytest.raises(TransportFault) as caught:
            real.recv(0.2)
    else:
        recorder.bus.send_error = error
        with pytest.raises(TransportFault) as caught:
            real.send_poll(1)
    fault = caught.value
    assert (fault.op, fault.kind, fault.errno) == (op, expected, code)
    assert fault.__cause__ is error
    assert str(fault) == f"{op}: {expected}: {fault.message}"


@pytest.mark.req("CAN-32")
def test_map_exception_table() -> None:
    def kind(op: str, exc: BaseException) -> FaultKind | None:
        fault = map_exception(op, exc)
        return None if fault is None else fault.kind

    assert kind("recv", OSError(errno.ENXIO, "x")) is FaultKind.DEVICE_GONE
    assert kind("send", OSError(errno.EAGAIN, "x")) is FaultKind.TX_BLOCKED
    assert kind("recv", OSError(errno.EAGAIN, "x")) is FaultKind.OS_ERROR
    assert kind("recv", OSError(errno.EIO, "x")) is FaultKind.OS_ERROR
    assert kind("recv", OSError("no errno")) is FaultKind.OS_ERROR
    assert kind("recv", USBError(errno.EIO, "Input/Output Error")) is FaultKind.DEVICE_GONE
    assert kind("send", wrapped(USBError(errno.ENODEV, "x"))) is FaultKind.DEVICE_GONE
    assert kind("recv", USBError("No such device (it may have been disconnected)")) is (
        FaultKind.DEVICE_GONE
    )
    assert kind("recv", USBError(errno.EPIPE, "Pipe error")) is FaultKind.OS_ERROR
    assert kind("open", can.CanInitializationError("x")) is FaultKind.OPEN_FAILED
    assert kind("recv", can.CanOperationError("x")) is FaultKind.CAN_ERROR
    assert kind("recv", can.CanOperationError("Transmit timed out")) is FaultKind.CAN_ERROR
    assert kind("send", can.CanOperationError("Could not send")) is FaultKind.CAN_ERROR
    assert kind("recv", ValueError("x")) is None
    assert kind("send", ListenOnlyError("x")) is None

    precedence = can.CanOperationError("x", errno.ENODEV)
    precedence.__cause__ = OSError(errno.ENETDOWN, "x")
    assert kind("recv", precedence) is FaultKind.DEVICE_GONE
    try:
        try:
            raise OSError(errno.ENETDOWN, "down")
        except OSError:
            raise can.CanOperationError("implicit") from None
    except can.CanOperationError as implicit:
        assert implicit.__cause__ is None
        assert kind("recv", implicit) is FaultKind.LINK_DOWN

    fault = map_exception("open", OSError(errno.ENODEV, "No such device"))
    assert fault is not None
    assert (fault.op, fault.kind, fault.errno) == ("open", FaultKind.DEVICE_GONE, errno.ENODEV)
    assert "No such device" in fault.message


@pytest.mark.req("CAN-22", "CAN-23", "CAN-01")
def test_not_open_and_close_idempotent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    recorder = Recorder()
    real = transport(make_settings(), recorder)
    calls: list[tuple[Callable[[], object], str]] = [
        (lambda: real.recv(0.2), "recv"),
        (lambda: real.send_poll(1), "send"),
    ]
    for call, op in calls:
        with pytest.raises(TransportFault) as caught:
            call()
        assert (caught.value.op, caught.value.kind) == (op, FaultKind.NOT_OPEN)
    real.close()
    with real as entered:
        assert entered is real
        real.open()
    real.close()
    assert recorder.bus.shutdowns == 1
    assert records(caplog, logging.INFO).count("can bus shut down") == 1
    for call, op in calls:
        with pytest.raises(TransportFault) as caught:
            call()
        assert (caught.value.op, caught.value.kind) == (op, FaultKind.NOT_OPEN)
    real.open()
    real.open()
    assert len(recorder.buses) == 2
    with pytest.raises(RuntimeError), real:
        raise RuntimeError("main loop crashed")
    assert recorder.bus.shutdowns == 1


@pytest.mark.req("CAN-23")
def test_shutdown_exception_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    def prepare(bus: RecorderBus) -> None:
        bus.shutdown_error = can.CanOperationError("usb gone")

    recorder = Recorder(prepare=prepare)
    real = transport(make_settings(), recorder)
    real.open()
    real.close()
    real.close()
    assert recorder.bus.shutdowns == 1
    assert records(caplog, logging.WARNING) == ["can bus shutdown failed: usb gone"]
    with pytest.raises(TransportFault):
        real.recv(0.0)


@pytest.mark.req("CAN-33")
def test_usable_after_fault() -> None:
    recorder = Recorder()
    real = transport(make_settings(), recorder)
    real.open()
    recorder.bus.incoming.append(OSError(errno.ENODEV, "gone"))
    with pytest.raises(TransportFault):
        real.recv(0.2)
    assert recorder.bus.shutdowns == 0
    assert len(recorder.buses) == 1
    recorder.bus.send_error = OSError(errno.ENETDOWN, "down")
    with pytest.raises(TransportFault):
        real.send_poll(1)
    assert recorder.bus.shutdowns == 0
    first = recorder.bus
    real.reopen()
    assert first.shutdowns == 1
    assert len(recorder.buses) == 2
    assert real.recv(0.0) is None
    real.close()
    assert recorder.bus.shutdowns == 1


@pytest.mark.req("CAN-26")
def test_reopen_closes_sleeps_2s_opens() -> None:
    assert REOPEN_GAP_S == 2.0
    timeline: list[object] = []

    def sleep(seconds: float) -> None:
        timeline.append(("sleep", seconds))

    recorder = Recorder(timeline=timeline)
    flaky = FlakyFactory(recorder)
    real = transport(make_settings(), flaky, sleep=sleep)
    real.open()
    real.reopen()
    assert timeline == ["open", "shutdown", ("sleep", 2.0), "open"]
    timeline.clear()
    flaky.fail_next_opens(1, errno.ENODEV)
    with pytest.raises(TransportFault) as caught:
        real.reopen()
    assert (caught.value.op, caught.value.kind) == ("open", FaultKind.DEVICE_GONE)
    assert timeline == ["shutdown", ("sleep", 2.0)]
    with pytest.raises(TransportFault) as closed:
        real.recv(0.0)
    assert closed.value.kind is FaultKind.NOT_OPEN
    real.reopen()
    assert flaky.all_shut_down() is False
    real.close()
    assert flaky.all_shut_down() is True


@pytest.mark.req("CAN-30")
def test_non_socketcan_link_state_without_query() -> None:
    query = LinkQuery()
    for interface in ("gs_usb", "slcan", "virtual"):
        fake_clock = FakeClock(7.0)
        settings = make_settings(**INTERFACE_SETTINGS[interface])
        real = transport(settings, Recorder(), fake_clock, link_query=query)
        assert real.link_state() == unknown_link_state(7.0)
    assert query.calls == []
    fake_clock = FakeClock(9.0)
    socketcan = make_settings(**INTERFACE_SETTINGS["socketcan"])
    assert transport(socketcan, Recorder(), fake_clock, link_query=query).link_state().at == 9.0
    assert query.calls == [("can0", 9.0)]


@dataclass
class Contract:
    """How the contract test drives one ``CanTransport`` implementation."""

    transport: CanTransport
    deliver: Callable[[can.Message], None]
    polls: Callable[[], list[int]]
    timeout: float


@pytest.fixture(params=["virtual", "fake", "recording"])
def contract(request: pytest.FixtureRequest, can_channel: str) -> Iterator[Contract]:
    """Yield a ``CanTransport`` with hooks to inject frames and read the polls it sent."""
    if request.param == "virtual":
        peer = can.Bus(interface="virtual", channel=can_channel)
        sent: list[int] = []

        def peer_polls() -> list[int]:
            while (frame := peer.recv(0)) is not None:
                sent.append((frame.arbitration_id >> 8) & 0xF)
            return sent

        real = PythonCanTransport(
            make_settings(CAN_CHANNEL=can_channel),
            time.monotonic,
            FakeClock().sleep,
            link_query=LinkQuery(),
        )
        yield Contract(real, peer.send, peer_polls, 0.02)
        real.close()
        peer.shutdown()
        return

    fake = FakeTransport(module_count=3, clock=FakeClock(10.0))

    def deliver(message: can.Message) -> None:
        if message.is_error_frame:
            fake.script([RxErrorFrame(message.arbitration_id, bytes(message.data), 10.0)])
        else:
            data = bytes(message.data)
            fake.script([RxFrame(message.arbitration_id, message.is_extended_id, data, 10.0)])

    subject: CanTransport = fake if request.param == "fake" else RecordingTransport(fake)
    yield Contract(subject, deliver, lambda: fake.polls, 0.02)


@pytest.mark.req("CAN-01", "CAN-03")
def test_contract_virtual_and_fake(contract: Contract) -> None:
    real = contract.transport
    assert real.listen_only is False
    with pytest.raises(TransportFault) as caught:
        real.recv(contract.timeout)
    assert caught.value.kind is FaultKind.NOT_OPEN
    with real:
        real.open()
        contract.deliver(msg(0x18F21121, data=bytes(range(8))))
        frame = real.recv(contract.timeout)
        assert isinstance(frame, RxFrame)
        assert (frame.arbitration_id, frame.extended, frame.data) == (
            0x18F21121,
            True,
            bytes(range(8)),
        )
        contract.deliver(error_msg(0x020))
        error = real.recv(contract.timeout)
        assert isinstance(error, RxErrorFrame)
        assert error.error_class == 0x020
        assert real.recv(contract.timeout) is None
        real.send_poll(3)
        real.send_poll(1)
        assert contract.polls() == [3, 1]
        with pytest.raises(ValueError):
            real.send_poll(4)
        assert real.link_state().state is CanState.UNKNOWN
        real.reopen()
        assert real.recv(contract.timeout) is None
    real.close()
    closed_calls: list[Callable[[], object]] = [
        lambda: real.recv(contract.timeout),
        lambda: real.send_poll(1),
    ]
    for call in closed_calls:
        with pytest.raises(TransportFault) as closed:
            call()
        assert closed.value.kind is FaultKind.NOT_OPEN


@pytest.mark.req("CAN-14", "CAN-03")
def test_contract_listen_only_fake() -> None:
    fake = FakeTransport(listen_only=True)
    fake.open()
    with pytest.raises(ListenOnlyError):
        fake.send_poll(1)
    assert fake.polls == []
    fake.close()


@pytest.mark.req("CAN-03")
def test_fake_transport_scripting_hooks() -> None:
    fake_clock = FakeClock(0.0)
    fake = FakeTransport(clock=fake_clock)
    fault = TransportFault("open", FaultKind.DEVICE_GONE, errno.ENODEV, "absent")
    fake.fail_open(2, fault)
    for _ in range(2):
        with pytest.raises(TransportFault) as caught:
            fake.open()
        assert caught.value is fault
    fake.open()
    recv_fault = TransportFault("recv", FaultKind.LINK_DOWN, errno.ENETDOWN, "down")
    frame = RxFrame(0x18F20103, True, bytes(8), 0.0)
    fake.script([frame, None, recv_fault])
    assert fake.recv(0.2) is frame
    assert fake.recv(0.2) is None
    assert fake_clock() == pytest.approx(0.2)
    with pytest.raises(TransportFault) as raised:
        fake.recv(0.2)
    assert raised.value is recv_fault
    assert fake.recv(0.2) is None
    assert fake_clock() == pytest.approx(0.4)
    fake.link = LinkState(CanState.BUS_OFF, "UP", False, 500000, 0, 0, 1, 1, 0, 3.0)
    assert fake.link_state().state is CanState.BUS_OFF
    fake.reopen()
    assert fake_clock() == pytest.approx(0.4 + REOPEN_GAP_S)
    assert (fake.open_calls, fake.close_calls, fake.is_open) == (4, 1, True)

    recording = RecordingTransport(fake)
    recording.send_poll(2)
    recording.recv(0.2)
    recording.close()
    assert recording.polls == [2]
    assert [name for name, _ in recording.calls] == ["send_poll", "recv", "close"]
    assert recording.listen_only is False
