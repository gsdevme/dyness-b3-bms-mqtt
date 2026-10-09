"""A fake systemd ``NOTIFY_SOCKET``: a bound ``AF_UNIX`` datagram socket (TEST-20).

``NotifySocket`` binds a filesystem socket in a short private temporary directory, or an
abstract-namespace socket when ``abstract=True``. ``path`` is the value to put in
``NOTIFY_SOCKET`` (``@name`` for the abstract form) and ``messages()`` returns every datagram
received so far, decoded, in arrival order. ``close()`` releases the socket and the directory.
"""

import shutil
import socket
import tempfile
import uuid
from pathlib import Path


class NotifySocket:
    """A bound datagram socket that records what a ``Notifier`` sends to it."""

    def __init__(self, *, abstract: bool = False) -> None:
        """Bind a fresh socket; filesystem by default, abstract namespace when asked."""
        self._dir: Path | None = None
        self._received: list[str] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if abstract:
            name = f"dyness-test-{uuid.uuid4().hex}"
            self._sock.bind(f"\0{name}")
            self.path = f"@{name}"
        else:
            self._dir = Path(tempfile.mkdtemp(prefix="notify-"))
            self.path = str(self._dir / "notify.sock")
            self._sock.bind(self.path)
        self._sock.setblocking(False)

    def messages(self) -> list[str]:
        """Return every datagram received since the socket was bound, oldest first."""
        while True:
            try:
                data = self._sock.recv(4096)
            except BlockingIOError:
                return list(self._received)
            self._received.append(data.decode("utf-8"))

    def close(self) -> None:
        """Close the socket and remove its temporary directory; safe to call twice."""
        self._sock.close()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
