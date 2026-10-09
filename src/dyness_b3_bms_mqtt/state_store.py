"""Persistent recovery state in ``$STATE_DIRECTORY`` (HEALTH-24..26, SVC-03, SVC-04).

The service keeps two small JSON files in the directory systemd creates for it:

* ``recovery.json``, written here: the escalation counter that drives the recovery ladder
  in ``03-health-and-recovery.md`` and the ``restarts`` diagnostic. Writes are atomic
  (temporary file in the same directory, ``fsync``, rename). A file that is missing,
  unreadable or invalid reads as zero counts with a warning, so it can never stop start-up.
* ``last_exit.json``, written by the ``recover`` helper after each stop and only read here.

Without ``$STATE_DIRECTORY`` (development runs) the counts live in memory, start at zero,
and one warning says so. A failed write is logged and swallowed: losing a counter update is
better than crashing the recovery path that wanted to record it.
"""

import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self

from dyness_b3_bms_mqtt.timefmt import iso_utc

RECOVERY_FILE: Final = "recovery.json"
LAST_EXIT_FILE: Final = "last_exit.json"
STATE_DIRECTORY_ENV: Final = "STATE_DIRECTORY"

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """The persisted counters: ``escalation`` for the recovery ladder, ``restarts`` for show."""

    escalation: int = 0
    restarts: int = 0


@dataclass(frozen=True, slots=True)
class LastExit:
    """The previous stop as recorded by ``recover`` (SVC-15).

    ``code`` is the numeric exit status, a signal name such as ``"ABRT"``, or ``None`` when
    systemd did not provide one; ``result`` is systemd's ``SERVICE_RESULT`` or ``None``.
    """

    code: int | str | None
    result: str | None


class StateStore:
    """Reads and writes the state files in one directory, or keeps counts in memory.

    ``wall`` returns Unix seconds and is used only for the ``updated`` timestamp.
    """

    def __init__(self, directory: Path | None, wall: Callable[[], float]) -> None:
        """Use ``directory``, or memory only when it is ``None`` (warned about once)."""
        self._directory = directory
        self._wall = wall
        self._memory = RecoveryRecord()
        if directory is None:
            _log.warning(
                "%s is unset; the recovery counter is kept in memory only", STATE_DIRECTORY_ENV
            )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], wall: Callable[[], float]) -> Self:
        """Build a store for ``$STATE_DIRECTORY``; an unset or empty value means memory only.

        systemd joins several state directories with ``:``; the first one is used.
        """
        first = environ.get(STATE_DIRECTORY_ENV, "").split(":")[0]
        return cls(Path(first) if first else None, wall)

    @property
    def directory(self) -> Path | None:
        """The state directory, or ``None`` when counts are kept in memory."""
        return self._directory

    def load_recovery(self) -> RecoveryRecord:
        """Return the stored counters, or zero counts if the file is missing or invalid."""
        if self._directory is None:
            return self._memory
        path = self._directory / RECOVERY_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _log.warning("%s does not exist; starting with zero counts", path)
            return RecoveryRecord()
        except (OSError, ValueError) as exc:
            _log.warning("%s is unreadable (%s); starting with zero counts", path, exc)
            return RecoveryRecord()
        record = _recovery_record(data)
        if record is None:
            _log.warning("%s is invalid; starting with zero counts", path)
            return RecoveryRecord()
        return record

    def save_recovery(self, record: RecoveryRecord) -> None:
        """Store ``record`` atomically; a failed write is logged at ``WARNING`` and ignored."""
        if self._directory is None:
            self._memory = record
            return
        path = self._directory / RECOVERY_FILE
        payload = {
            "escalation": record.escalation,
            "restarts": record.restarts,
            "updated": iso_utc(self._wall()),
        }
        try:
            _write_atomic(path, json.dumps(payload) + "\n")
        except OSError as exc:
            _log.warning("could not write %s: %s", path, exc)

    def load_last_exit(self) -> LastExit | None:
        """Return the previous stop, or ``None`` if there is no valid record of one (SVC-04)."""
        if self._directory is None:
            return None
        path = self._directory / LAST_EXIT_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            _log.warning("%s is unreadable (%s); ignoring it", path, exc)
            return None
        last_exit = _last_exit(data)
        if last_exit is None:
            _log.warning("%s is invalid; ignoring it", path)
        return last_exit


def _count(value: object) -> int | None:
    """Return ``value`` if it is a non-negative JSON integer (not a boolean), else ``None``."""
    return value if type(value) is int and value >= 0 else None


def _recovery_record(data: object) -> RecoveryRecord | None:
    """Validate a decoded ``recovery.json``; ``None`` if either count is missing or bad."""
    if not isinstance(data, dict):
        return None
    escalation = _count(data.get("escalation"))
    restarts = _count(data.get("restarts"))
    if escalation is None or restarts is None:
        return None
    return RecoveryRecord(escalation=escalation, restarts=restarts)


def _last_exit(data: object) -> LastExit | None:
    """Validate a decoded ``last_exit.json``; ``code`` is required, ``result`` optional."""
    if not isinstance(data, dict) or "code" not in data:
        return None
    code, result = data["code"], data.get("result")
    if not (code is None or type(code) is int or isinstance(code, str)):
        return None
    if not (result is None or isinstance(result, str)):
        return None
    return LastExit(code=code, result=result)


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` via a synced temporary file in the same directory."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise
