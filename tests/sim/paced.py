"""Run a transport against the simulated stack on simulated time (TEST-22).

``SimPacedBus`` wraps the service's python-can ``virtual`` bus. Its ``recv`` never really
waits: when no frame is queued it advances the ``SimulatedStack`` (and with it the
``FakeClock``) until the stack sends something or the timeout has passed in simulated time.
``paced_factory`` returns a ``bus_factory`` for ``PythonCanTransport`` that builds such buses.
"""

from collections.abc import Callable
from typing import Any, Protocol

import can

from tests.sim.stack import SimulatedStack

UNBOUNDED_WAIT_S = 3600.0
"""Simulated time a ``recv(timeout=None)`` gives up after, so a silent bus cannot hang a test."""


class BusLike(Protocol):
    """The python-can bus calls the transport makes (mirrors ``canbus.BusLike``)."""

    def recv(self, timeout: float | None = None) -> can.Message | None: ...

    def send(self, msg: can.Message, timeout: float | None = None) -> None: ...

    def shutdown(self) -> None: ...


class SimPacedBus:
    """A bus whose ``recv`` advances ``sim`` instead of blocking; other calls go to ``inner``."""

    def __init__(self, inner: BusLike, sim: SimulatedStack) -> None:
        self.inner = inner
        self.sim = sim

    def recv(self, timeout: float | None = None) -> can.Message | None:
        message = self.inner.recv(0)
        if message is not None:
            return message
        deadline = self.sim.clock() + (UNBOUNDED_WAIT_S if timeout is None else timeout)
        while (remaining := deadline - self.sim.clock()) > 0:
            if not self.sim.advance_until_frame(remaining):
                return None
            message = self.inner.recv(0)
            if message is not None:
                return message
        return None

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.inner.send(msg, timeout)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _identity(bus: BusLike) -> BusLike:
    return bus


def paced_factory(
    sim: SimulatedStack, wrap: Callable[[BusLike], BusLike] = _identity
) -> Callable[..., BusLike]:
    """Return a ``bus_factory`` building ``SimPacedBus(wrap(can.Bus(**kwargs)), sim)``.

    Raises ``ValueError`` at open time unless the transport asks for the ``virtual`` channel
    the simulator is on.
    """

    def factory(**kwargs: Any) -> BusLike:
        if kwargs.get("interface") != "virtual" or kwargs.get("channel") != sim.channel:
            raise ValueError(f"expected the virtual channel {sim.channel!r}, got {kwargs!r}")
        return SimPacedBus(wrap(can.Bus(**kwargs)), sim)

    return factory
