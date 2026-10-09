"""A python-can bus wrapper that fails on demand, and a factory that fails opens (TEST-26).

``FlakyFactory`` stands in for ``can.Bus`` as a transport's ``bus_factory``: it records the
keyword arguments of every open, fails the next N opens with an ``OSError``, and wraps every
bus it does open in a ``FlakyBus``. ``FlakyBus`` raises ``OSError(errno)`` from ``recv`` or
``send`` once or until cleared, and records whether ``shutdown()`` ran. Both can append to a
shared ``timeline`` so a test can assert the order of opens and shutdowns.
"""

import errno as errno_codes
import os
from collections.abc import Callable
from typing import Any

import can

from dyness_b3_bms_mqtt.canbus import BusLike


def _os_error(code: int) -> OSError:
    return OSError(code, os.strerror(code))


class FlakyBus:
    """Wrap ``inner`` and raise ``OSError`` from ``recv`` / ``send`` when told to.

    Attributes not defined here (``socket``, ``gs_usb``, ...) are read from ``inner``.
    """

    def __init__(self, inner: BusLike, timeline: list[str] | None = None) -> None:
        self.inner = inner
        self.timeline = timeline
        self.shutdown_called = False
        self.shutdown_count = 0
        self._recv_fault: tuple[int, bool] | None = None
        self._send_fault: tuple[int, bool] | None = None

    def fail_recv(self, errno: int, once: bool = True) -> None:
        """Make ``recv`` raise ``OSError(errno)`` on the next call, or until ``clear()``."""
        self._recv_fault = (errno, once)

    def fail_send(self, errno: int, once: bool = True) -> None:
        """Make ``send`` raise ``OSError(errno)`` on the next call, or until ``clear()``."""
        self._send_fault = (errno, once)

    def clear(self) -> None:
        """Stop failing."""
        self._recv_fault = None
        self._send_fault = None

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if self._recv_fault is not None:
            code, once = self._recv_fault
            if once:
                self._recv_fault = None
            raise _os_error(code)
        return self.inner.recv(timeout)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        if self._send_fault is not None:
            code, once = self._send_fault
            if once:
                self._send_fault = None
            raise _os_error(code)
        self.inner.send(msg, timeout)

    def shutdown(self) -> None:
        self.shutdown_called = True
        self.shutdown_count += 1
        if self.timeline is not None:
            self.timeline.append("shutdown")
        self.inner.shutdown()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class FlakyFactory:
    """A ``bus_factory`` that builds ``FlakyBus(make_inner(**kwargs))`` or fails on demand."""

    def __init__(
        self, make_inner: Callable[..., BusLike], timeline: list[str] | None = None
    ) -> None:
        self.make_inner = make_inner
        self.timeline = timeline
        self.opened: list[FlakyBus] = []
        self.kwargs: list[dict[str, Any]] = []
        self._failures: list[int] = []

    def fail_next_opens(self, n: int, errno: int = errno_codes.ENODEV) -> None:
        """Make the next ``n`` opens raise ``OSError(errno)``."""
        self._failures.extend([errno] * n)

    def __call__(self, **kwargs: Any) -> FlakyBus:
        self.kwargs.append(kwargs)
        if self._failures:
            code = self._failures.pop(0)
            if self.timeline is not None:
                self.timeline.append("open-failed")
            raise _os_error(code)
        bus = FlakyBus(self.make_inner(**kwargs), self.timeline)
        self.opened.append(bus)
        if self.timeline is not None:
            self.timeline.append("open")
        return bus

    @property
    def latest(self) -> FlakyBus:
        """Return the most recently opened bus."""
        return self.opened[-1]

    def all_shut_down(self) -> bool:
        """Return whether ``shutdown()`` ran on every bus this factory opened."""
        return all(bus.shutdown_called for bus in self.opened)
