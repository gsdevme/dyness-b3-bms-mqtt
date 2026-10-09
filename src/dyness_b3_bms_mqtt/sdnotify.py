"""A dependency-free systemd ``sd_notify`` client (09-service.md SVC-05, SVC-07, SVC-09).

Each message is one ``AF_UNIX`` ``SOCK_DGRAM`` datagram to ``$NOTIFY_SOCKET``; an address
starting with ``@`` is in the abstract namespace and the ``@`` becomes a NUL byte. Without
``NOTIFY_SOCKET`` every call does nothing, so development runs work unchanged. A failed send
is logged at ``DEBUG`` and never raised: losing a notification must not stop the service.

The watchdog cadence is driven by the caller's monotonic ``now`` so that only a completed
main-loop iteration can feed it (HEALTH-32); this module never reads a clock or starts a
thread.
"""

import logging
import socket
from collections.abc import Mapping

_log = logging.getLogger(__name__)


def _address(environ: Mapping[str, str]) -> str | None:
    """Return the socket address from ``NOTIFY_SOCKET``, or ``None`` when unset or empty."""
    path = environ.get("NOTIFY_SOCKET", "")
    if not path:
        return None
    return "\0" + path[1:] if path.startswith("@") else path


def _watchdog_interval(environ: Mapping[str, str], pid: int) -> float | None:
    """Return the watchdog period in seconds, ``WATCHDOG_USEC / 3``, or ``None`` when off.

    The watchdog is off when ``WATCHDOG_USEC`` is unset, not a positive integer, or when
    ``WATCHDOG_PID`` is set to anything other than ``pid``.
    """
    try:
        usec = int(environ.get("WATCHDOG_USEC", ""))
    except ValueError:
        return None
    if usec <= 0:
        return None
    owner = environ.get("WATCHDOG_PID", "")
    if owner and owner != str(pid):
        return None
    return usec / 3 / 1_000_000


class Notifier:
    """Send ``sd_notify`` messages for the process ``pid`` as configured by ``environ``.

    Every method returns ``True`` when a datagram was sent and ``False`` when nothing was sent
    (no socket, suppressed by ``ready``'s once rule or the watchdog cadence, or a failed send).
    The notifier is meant for the main thread only; it holds no lock.
    """

    def __init__(self, environ: Mapping[str, str], pid: int) -> None:
        """Read ``NOTIFY_SOCKET``, ``WATCHDOG_USEC`` and ``WATCHDOG_PID`` from ``environ``."""
        self._address = _address(environ)
        self._interval = _watchdog_interval(environ, pid)
        self._ready_sent = False
        self._last_watchdog: float | None = None

    def notify(self, message: str) -> bool:
        """Send ``message`` (newline-separated ``KEY=VALUE`` lines) as one datagram."""
        if self._address is None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.sendto(message.encode("utf-8"), self._address)
        except OSError as exc:
            _log.debug("sd_notify %r failed: %s", message, exc)
            return False
        return True

    def ready(self) -> bool:
        """Send ``READY=1`` the first time only (SVC-06); later calls do nothing."""
        if self._ready_sent:
            return False
        self._ready_sent = True
        return self.notify("READY=1")

    def status(self, text: str) -> bool:
        """Send ``STATUS=<text>`` (SVC-08)."""
        return self.notify(f"STATUS={text}")

    def stopping(self) -> bool:
        """Send ``STOPPING=1`` (SVC-09)."""
        return self.notify("STOPPING=1")

    def watchdog(self, now: float) -> bool:
        """Send ``WATCHDOG=1`` unless one was attempted less than ``WATCHDOG_USEC / 3`` ago.

        ``now`` is a monotonic reading taken by the caller at the end of a completed main-loop
        iteration. Nothing is sent when the watchdog is off for this process (SVC-07).
        """
        if self._interval is None or self._address is None:
            return False
        if self._last_watchdog is not None and now - self._last_watchdog < self._interval:
            return False
        self._last_watchdog = now
        return self.notify("WATCHDOG=1")
