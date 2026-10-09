"""Tests of the bus fakes and suite fixtures: ``FlakyBus`` (TEST-26), unique channels and state
directories (TEST-13) and the transmit guard (TEST-28)."""

import errno
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import can
import pytest

from dyness_b3_bms_mqtt.canbus import PythonCanTransport
from tests.fakes.clock import FakeClock
from tests.fakes.flaky import FlakyBus, FlakyFactory
from tests.support.settings import make_settings
from tests.support.txguard import GuardedBus, TxGuard

ROOT = Path(__file__).resolve().parents[2]
POLL_DATA = bytes([1, 0, 0, 0, 0, 0, 0, 0])


@dataclass
class ListBus:
    """A minimal bus that records what it is given."""

    sent: list[can.Message] = field(default_factory=list)
    shut: bool = False

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return None

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def shutdown(self) -> None:
        self.shut = True


@pytest.mark.req("TEST-26")
def test_flaky_bus(can_channel: str) -> None:
    timeline: list[str] = []
    factory = FlakyFactory(lambda **kw: can.Bus(**kw), timeline=timeline)
    factory.fail_next_opens(2, errno.ENETDOWN)
    kwargs = {"interface": "virtual", "channel": can_channel}
    for _ in range(2):
        with pytest.raises(OSError) as failed:
            factory(**kwargs)
        assert failed.value.errno == errno.ENETDOWN
    bus = factory(**kwargs)
    assert factory.kwargs == [kwargs, kwargs, kwargs]
    assert factory.opened == [bus]
    assert timeline == ["open-failed", "open-failed", "open"]

    with can.Bus(interface="virtual", channel=can_channel) as peer:
        bus.fail_recv(errno.ENODEV)
        with pytest.raises(OSError) as once:
            bus.recv(0)
        assert once.value.errno == errno.ENODEV
        peer.send(can.Message(arbitration_id=0x18F20103, data=bytes(8)))
        received = bus.recv(0)
        assert received is not None and received.arbitration_id == 0x18F20103

        bus.fail_send(errno.ENETDOWN, once=False)
        poll = can.Message(arbitration_id=0x18F21177, data=POLL_DATA)
        for _ in range(3):
            with pytest.raises(OSError) as repeated:
                bus.send(poll)
            assert repeated.value.errno == errno.ENETDOWN
        bus.fail_recv(errno.ENETDOWN, once=False)
        for _ in range(2):
            with pytest.raises(OSError):
                bus.recv(0)
        bus.clear()
        assert bus.recv(0) is None
        bus.send(poll)
        assert peer.recv(0) is not None

    assert bus.shutdown_called is False
    assert factory.all_shut_down() is False
    bus.shutdown()
    assert bus.shutdown_called is True
    assert factory.all_shut_down() is True
    assert timeline[-1] == "shutdown"

    default = FlakyFactory(lambda **kw: ListBus())
    default.fail_next_opens(1)
    with pytest.raises(OSError) as absent:
        default()
    assert absent.value.errno == errno.ENODEV
    assert isinstance(default(), FlakyBus)


@pytest.mark.req("TEST-13")
def test_unique_channels_and_state_dirs(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    out = pytester.path / "seen.jsonl"
    pytester.makeconftest("from tests.conftest import can_channel, state_dir  # noqa: F401\n")
    pytester.makepyfile(
        f"""
        import json
        import pytest

        @pytest.mark.parametrize("n", range(3))
        def test_record(n, can_channel, state_dir):
            assert state_dir.is_dir() and not any(state_dir.iterdir())
            (state_dir / "marker").write_text("x")
            with open({str(out)!r}, "a") as handle:
                handle.write(json.dumps([can_channel, str(state_dir)]) + "\\n")

        def test_again(can_channel, state_dir):
            test_record(9, can_channel, state_dir)
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=4)
    seen = [json.loads(line) for line in out.read_text().splitlines()]
    channels = [channel for channel, _ in seen]
    dirs = [directory for _, directory in seen]
    assert len(set(channels)) == len(set(dirs)) == 4
    assert all(os.path.isabs(d) for d in dirs)


@pytest.mark.req("TEST-28")
def test_tx_guard_rejects_bad_frames(tx_guard: TxGuard, can_channel: str) -> None:
    inner = ListBus()
    guarded = tx_guard.wrap(inner, listen_only=False, module_count=3)
    guarded.send(can.Message(arbitration_id=0x18F21177, data=POLL_DATA))
    bad = [
        can.Message(arbitration_id=0x18F26677, data=POLL_DATA),
        can.Message(arbitration_id=0x18F21477, data=POLL_DATA),
        can.Message(arbitration_id=0x18F21077, data=POLL_DATA),
        can.Message(arbitration_id=0x177, is_extended_id=False, data=POLL_DATA),
        can.Message(arbitration_id=0x18F21177, data=bytes(8)),
        can.Message(arbitration_id=0x18F21177, data=POLL_DATA[:7]),
        can.Message(arbitration_id=0x18F21177, is_remote_frame=True, dlc=8),
        can.Message(arbitration_id=0x18F21177, is_error_frame=True, data=POLL_DATA),
    ]
    for message in bad:
        with pytest.raises(pytest.fail.Exception, match="tx_guard"):
            guarded.send(message)
    listening = tx_guard.wrap(inner, listen_only=True, module_count=3)
    with pytest.raises(pytest.fail.Exception, match="CAN_LISTEN_ONLY"):
        listening.send(can.Message(arbitration_id=0x18F21177, data=POLL_DATA))
    assert [m.arbitration_id for m in inner.sent] == [0x18F21177]
    violations = tx_guard.take_violations()
    assert len(violations) == len(bad) + 1
    assert any("0x18F26677" in v for v in violations)
    assert tx_guard.violations == []

    clock = FakeClock(0.0)
    settings = make_settings(CAN_CHANNEL=can_channel, MODULE_COUNT="2")
    with (
        PythonCanTransport(settings, clock, clock.sleep) as transport,
        can.Bus(interface="virtual", channel=can_channel) as peer,
    ):
        transport.open()
        [opened] = tx_guard.buses[-1:]
        assert isinstance(opened, GuardedBus)
        assert (opened.listen_only, opened.module_count) == (False, 2)
        transport.send_poll(2)
        assert opened.sent == 1
        assert peer.recv(0) is not None
    listen_settings = make_settings(CAN_CHANNEL=can_channel, CAN_LISTEN_ONLY="true")
    with PythonCanTransport(listen_settings, clock, clock.sleep) as listener:
        listener.open()
        assert tx_guard.buses[-1].listen_only is True
