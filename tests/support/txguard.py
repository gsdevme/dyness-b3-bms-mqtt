"""The transmit guard behind the autouse ``tx_guard`` fixture (TEST-28).

The fixture patches ``PythonCanTransport._construct_bus`` so that every bus the service
opens, in any test, is wrapped in a :class:`GuardedBus`. The wrapper fails the test on any
frame other than the extended poll ``0x18F21m77`` with data ``01 00 00 00 00 00 00 00`` for a
module in 1..``MODULE_COUNT``, explicitly on ``0x18F26677``, and on anything at all while
``CAN_LISTEN_ONLY=true``. Violations are also kept, so the fixture fails the test at teardown
even if the code under test swallowed the failure.

The pattern is written out here rather than taken from ``protocol``, so a bug there cannot
also blind the guard.
"""

from typing import Any

import can
import pytest

from dyness_b3_bms_mqtt.canbus import BusLike

POLL_DATA = bytes([1, 0, 0, 0, 0, 0, 0, 0])
POLL_ID_MASK = 0xFFFFF0FF
POLL_ID_PATTERN = 0x18F21077
PARAMETER_WRITE_ID = 0x18F26677


def violation(message: can.Message, *, listen_only: bool, module_count: int) -> str | None:
    """Return why ``message`` must not be transmitted, or ``None`` if it is a valid poll."""
    arbitration_id = message.arbitration_id
    if listen_only:
        return f"frame 0x{arbitration_id:08X} sent while CAN_LISTEN_ONLY=true"
    if arbitration_id == PARAMETER_WRITE_ID:
        return "parameter-write frame 0x18F26677 sent"
    module = (arbitration_id >> 8) & 0xF
    if (
        not message.is_extended_id
        or message.is_error_frame
        or message.is_remote_frame
        or arbitration_id & POLL_ID_MASK != POLL_ID_PATTERN
        or bytes(message.data) != POLL_DATA
        or not 1 <= module <= module_count
    ):
        return f"frame 0x{arbitration_id:08X} {bytes(message.data).hex()} is not a valid poll"
    return None


class GuardedBus:
    """Forward to ``inner``, refusing any transmission that ``violation`` rejects."""

    def __init__(
        self, guard: TxGuard, inner: BusLike, listen_only: bool, module_count: int
    ) -> None:
        self.guard = guard
        self.inner = inner
        self.listen_only = listen_only
        self.module_count = module_count
        self.sent = 0

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self.inner.recv(timeout)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        reason = violation(msg, listen_only=self.listen_only, module_count=self.module_count)
        if reason is not None:
            self.guard.violations.append(reason)
            pytest.fail(f"tx_guard: {reason}")
        self.sent += 1
        self.inner.send(msg, timeout)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class TxGuard:
    """The state of one test's guard: the buses it wrapped and the violations it saw."""

    def __init__(self) -> None:
        self.buses: list[GuardedBus] = []
        self.violations: list[str] = []

    def wrap(self, inner: BusLike, *, listen_only: bool, module_count: int) -> GuardedBus:
        """Return ``inner`` wrapped in a guard and remember it."""
        bus = GuardedBus(self, inner, listen_only, module_count)
        self.buses.append(bus)
        return bus

    def take_violations(self) -> list[str]:
        """Return and forget the violations seen so far (for tests of the guard itself)."""
        taken, self.violations = self.violations, []
        return taken
