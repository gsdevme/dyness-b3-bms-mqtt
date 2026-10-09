"""Test doubles for the ``CanTransport`` protocol (CAN-03).

``FakeTransport`` lets a test script what ``recv`` returns (frames, error frames, timeouts
and ``TransportFault``s), fail the next N opens, set the link state, and read every poll
sent. ``send_poll`` goes through ``protocol.build_poll`` looked up as a module attribute, so
a test that instruments ``build_poll`` sees fake transmissions too (PROTO-31).

``RecordingTransport`` wraps any transport and records every call in order.
"""

from collections import deque
from collections.abc import Iterable
from typing import Self

from dyness_b3_bms_mqtt import protocol
from dyness_b3_bms_mqtt.canbus import (
    REOPEN_GAP_S,
    CanTransport,
    FaultKind,
    ListenOnlyError,
    RxErrorFrame,
    RxFrame,
    TransportFault,
)
from dyness_b3_bms_mqtt.linkstate import LinkState, unknown_link_state
from tests.fakes.clock import FakeClock

type Scripted = RxFrame | RxErrorFrame | TransportFault | None


class FakeTransport:
    """A scripted ``CanTransport``.

    ``recv`` returns the next scripted item: a frame as given, ``None`` as a timeout, and a
    ``TransportFault`` is raised. With an empty script it times out. Timeouts advance
    ``clock`` (when given) by the requested timeout, as the real transport would wait, and
    ``reopen`` advances it by ``REOPEN_GAP_S``.
    """

    def __init__(
        self, listen_only: bool = False, module_count: int = 3, *, clock: FakeClock | None = None
    ) -> None:
        self.listen_only = listen_only
        self.module_count = module_count
        self.clock = clock
        self.polls: list[int] = []
        self.frames: list[protocol.PollFrame] = []
        self.link: LinkState = unknown_link_state(clock() if clock else 0.0)
        self.is_open = False
        self.open_calls = 0
        self.close_calls = 0
        self.reopen_calls = 0
        self._script: deque[Scripted] = deque()
        self._open_failures: deque[TransportFault] = deque()
        self._send_failures: deque[TransportFault] = deque()

    def script(self, items: Iterable[Scripted]) -> None:
        """Append ``items`` to what ``recv`` returns."""
        self._script.extend(items)

    def fail_open(self, n: int, fault: TransportFault) -> None:
        """Make the next ``n`` opens raise ``fault``."""
        self._open_failures.extend([fault] * n)

    def fail_send(self, fault: TransportFault) -> None:
        """Make the next ``send_poll`` raise ``fault`` after the poll is built."""
        self._send_failures.append(fault)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        self.open_calls += 1
        if self._open_failures:
            raise self._open_failures.popleft()
        self.is_open = True

    def recv(self, timeout: float) -> RxFrame | RxErrorFrame | None:
        self._require_open("recv")
        item = self._script.popleft() if self._script else None
        if isinstance(item, TransportFault):
            raise item
        if item is None and self.clock is not None:
            self.clock.advance(timeout)
        return item

    def send_poll(self, module: int) -> None:
        if self.listen_only:
            raise ListenOnlyError("send_poll called with CAN_LISTEN_ONLY=true")
        self._require_open("send")
        frame = protocol.build_poll(module, self.module_count)
        if self._send_failures:
            raise self._send_failures.popleft()
        self.polls.append(module)
        self.frames.append(frame)

    def reopen(self) -> None:
        self.reopen_calls += 1
        self.close()
        if self.clock is not None:
            self.clock.advance(REOPEN_GAP_S)
        self.open()

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False

    def link_state(self) -> LinkState:
        return self.link

    def _require_open(self, op: str) -> None:
        if not self.is_open:
            raise TransportFault(op, FaultKind.NOT_OPEN, None, "can bus is not open")


class RecordingTransport:
    """Delegate to ``inner`` and record every call as ``(name, args)`` in ``calls``.

    ``polls`` lists the modules of the ``send_poll`` calls that returned normally.
    """

    def __init__(self, inner: CanTransport) -> None:
        self.inner = inner
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.polls: list[int] = []

    @property
    def listen_only(self) -> bool:
        return self.inner.listen_only

    @listen_only.setter
    def listen_only(self, value: bool) -> None:
        self.inner.listen_only = value

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        self.calls.append(("open", ()))
        self.inner.open()

    def recv(self, timeout: float) -> RxFrame | RxErrorFrame | None:
        self.calls.append(("recv", (timeout,)))
        return self.inner.recv(timeout)

    def send_poll(self, module: int) -> None:
        self.calls.append(("send_poll", (module,)))
        self.inner.send_poll(module)
        self.polls.append(module)

    def reopen(self) -> None:
        self.calls.append(("reopen", ()))
        self.inner.reopen()

    def close(self) -> None:
        self.calls.append(("close", ()))
        self.inner.close()

    def link_state(self) -> LinkState:
        self.calls.append(("link_state", ()))
        return self.inner.link_state()
