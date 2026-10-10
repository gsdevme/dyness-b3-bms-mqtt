"""How long the application spends carrying out each health action (HEALTH-34).

The run uses the same rig as ``test_app.py``: the real :class:`App` and python-can
``virtual`` transport on a ``FakeClock`` that only the injected sleep and the simulated
stack advance. Every action is timed on that clock as the application carries it out.
"""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from dyness_b3_bms_mqtt.app import App
from dyness_b3_bms_mqtt.health import (
    Action,
    Exit,
    Poll,
    ReopenBus,
    SetAvailability,
    SetEscalation,
    Transition,
)
from tests.fakes.clock import FakeClock
from tests.unit.test_app import Rig, make_rig

pytestmark = pytest.mark.timeout(60)

EXEMPT = (ReopenBus,)
MAX_BLOCK_S = 1.0


@pytest.fixture
def rig(can_channel: str, clock: FakeClock, state_dir: Path) -> Iterator[Rig]:
    made = make_rig(can_channel, clock, state_dir)
    yield made
    made.sim.close()


@pytest.mark.req("HEALTH-34")
def test_no_action_but_reopen_blocks_over_one_second(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    timings: list[tuple[Action, float]] = []
    original = App._carry_out

    def timed(app: App, actions: Sequence[Action]) -> Exit | None:
        for action in actions:
            before = rig.clock()
            result = original(app, [action])
            timings.append((action, rig.clock() - before))
            if result is not None:
                return result
        return None

    monkeypatch.setattr(App, "_carry_out", timed)
    rig.publisher.set_connected(True)
    rig.at(30.0, rig.sim.silence_bus)

    assert rig.run(seconds=300.0) == 75

    kinds = {type(action) for action, _ in timings}
    assert {Transition, SetAvailability, SetEscalation, Poll, ReopenBus, Exit} <= kinds
    slow = [(a, t) for a, t in timings if not isinstance(a, EXEMPT) and t > MAX_BLOCK_S]
    assert slow == []
    assert max(t for a, t in timings if isinstance(a, ReopenBus)) >= 2.0
