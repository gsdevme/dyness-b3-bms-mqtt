"""A file system that can run out of space, for the local history (LOG-18..LOG-20).

``FullDisk.open`` stands in for the ``open`` that ``dyness_b3_bms_mqtt.localog`` calls; a
test installs it with ``monkeypatch.setattr(localog, "open", disk.open, raising=False)``.
"""

import builtins
import errno
from typing import IO, Any


class FullDisk:
    """Open files as ``builtins.open`` does, but let appends fail on demand.

    Every file opened in an append mode is recorded in ``appended``. While ``full`` is set,
    a write to such a file puts the first half of its text on disk, as a short write would,
    and then raises ``OSError(ENOSPC)``, leaving a partial line behind.
    """

    def __init__(self) -> None:
        """Start with free space and nothing opened."""
        self.full = False
        self.appended: list[IO[Any]] = []

    def open(self, file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        """Open ``file``; an append-mode file comes back wrapped so writes can fail."""
        handle = builtins.open(file, mode, *args, **kwargs)  # noqa: SIM115
        if "a" not in mode:
            return handle
        self.appended.append(handle)
        return _Appender(self, handle)


class _Appender:
    """An append-mode text file whose ``write`` obeys :attr:`FullDisk.full`."""

    def __init__(self, disk: FullDisk, inner: IO[Any]) -> None:
        self._disk = disk
        self._inner = inner

    def write(self, text: str) -> int:
        if self._disk.full:
            self._inner.write(text[: len(text) // 2])
            self._inner.flush()
            raise OSError(errno.ENOSPC, "No space left on device")
        return self._inner.write(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
