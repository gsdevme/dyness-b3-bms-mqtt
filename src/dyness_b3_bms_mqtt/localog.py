"""The local history of the stack on the Pi (``docs/specs/12-local-logging.md``).

:class:`LocalLog` appends, whenever the main loop asks it to, one CSV row per module and one
for the stack to daily files in ``$STATE_DIRECTORY/logs``, and one line per event to a daily
events file. Rows are built by the MQTT payload builders and cut down to
:data:`MODULE_COLUMNS` and :data:`STACK_COLUMNS`, so a CSV value and the published value of
the same key never differ (LOG-11). Each file is named from the UTC date of what is written
to it, so the files roll over at the first write of a new UTC day (LOG-04).

:meth:`LocalLog.housekeep` gzips and deletes old files by the date in their names, as
planned by the pure :func:`housekeeping_plan`. While the status is ``error`` it also closes
and reopens today's files, which is the only way back to ``ok`` (LOG-20).

Every ``OSError`` stays inside this module: it turns the status to ``error``, is logged at
``ERROR`` on that change and only at ``DEBUG`` after it, and rows and events are dropped until
housekeeping recovers (LOG-18, LOG-19). Files are flushed after every write and never forced
to disk, to spare the SD card (LOG-12).
"""

import csv
import gzip
import io
import json
import logging
import math
import re
import shutil
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, TextIO

from dyness_b3_bms_mqtt.alerts import AlertState
from dyness_b3_bms_mqtt.config import Settings
from dyness_b3_bms_mqtt.payloads import module_payload, stack_payload
from dyness_b3_bms_mqtt.protocol import CELLS_PER_MODULE
from dyness_b3_bms_mqtt.status import LocalLogStatus as LocalLogStatus
from dyness_b3_bms_mqtt.telemetry import TEMPERATURE_SENSORS, Snapshot
from dyness_b3_bms_mqtt.timefmt import iso_utc

LOG_DIRECTORY: Final = "logs"
"""The subdirectory of ``$STATE_DIRECTORY`` that holds the history (LOG-01)."""

HOUSEKEEPING_INTERVAL_S: Final = 3600.0
"""Seconds between housekeeping runs on the monotonic clock (LOG-13)."""

MAX_COMPRESS_PER_RUN: Final = 8
"""The most files one housekeeping run gzips (LOG-15).

Housekeeping runs on the main loop, so a backlog (after lowering
``LOCAL_LOG_COMPRESS_AFTER_DAYS``, say) must not hold it past the systemd watchdog. Files
left over are gzipped by the following hourly runs.
"""

GZIP_COMPRESSLEVEL: Final = 6
"""The gzip level for old files: several times faster than 9, for slightly larger files."""

MODULE_COLUMNS: Final[tuple[str, ...]] = (
    "ts",
    "module",
    "online",
    "cells_fresh",
    *(f"cell_{n:02d}_mv" for n in range(1, CELLS_PER_MODULE + 1)),
    "cell_min_mv",
    "cell_min_no",
    "cell_max_mv",
    "cell_max_no",
    "cell_avg_mv",
    "spread_mv",
    "pack_v",
    "current_a",
    "soc",
    "soh",
    "cycles",
    "energy_kwh",
    "chg_limit_a",
    "dis_limit_a",
    *(f"temp_{n}_c" for n in range(1, TEMPERATURE_SENSORS + 1)),
    "temp_mos_c",
    "temp_board_c",
    "flags_raw",
    "bms_alarm",
    "bms_protection",
)
"""The columns of ``modules-YYYY-MM-DD.csv``, named after the MQTT-16 keys (LOG-07)."""

STACK_COLUMNS: Final[tuple[str, ...]] = (
    "ts",
    "modules_online",
    "module_count_bms",
    "lowest_mv",
    "lowest_module",
    "lowest_cell",
    "highest_mv",
    "highest_module",
    "highest_cell",
    "spread_mv",
    "total_current_a",
    "bms_current_a",
    "avg_soc",
    "bms_soc",
    "bms_soh",
    "voltage_v",
    "temp_c",
    "chg_limit_v",
    "chg_limit_a",
    "dis_limit_a",
    "dis_limit_v",
    "any_alarm",
    "any_protection",
    "imbalance_pct",
)
"""The columns of ``stack-YYYY-MM-DD.csv``, named after the MQTT-17 keys (LOG-08)."""

_SUFFIXES: Final[Mapping[str, str]] = {"events": ".log", "modules": ".csv", "stack": ".csv"}
"""The file name suffix of each daily file, in the order the files are reopened."""

_HEADERS: Final[Mapping[str, tuple[str, ...]]] = {
    "modules": MODULE_COLUMNS,
    "stack": STACK_COLUMNS,
}

_NAMES: Final = (
    re.compile(r"(?:modules|stack)-(?P<day>\d{4}-\d{2}-\d{2})\.csv(?:\.gz)?", re.ASCII),
    re.compile(r"events-(?P<day>\d{4}-\d{2}-\d{2})\.log(?:\.gz)?", re.ASCII),
)
"""The only file names housekeeping considers (LOG-14)."""

_GZ: Final = ".gz"

_log = logging.getLogger(__name__)


class GzipSink(Protocol):
    """What :class:`LocalLog` needs from the file that ``gzip_open`` returns."""

    def write(self, data: bytes, /) -> object:
        """Compress and write ``data``."""

    def close(self) -> None:
        """Finish the gzip stream and close the file."""


class GzipOpen(Protocol):
    """How :class:`LocalLog` opens a compressed file; ``gzip.open`` by default."""

    def __call__(self, path: Path, mode: Literal["wb"], /, *, compresslevel: int) -> GzipSink:
        """Open ``path`` for writing gzip data at ``compresslevel``."""
        ...


@dataclass(frozen=True, slots=True)
class _File:
    """One open daily file."""

    path: Path
    handle: TextIO


class _Failure(Exception):
    """An ``OSError`` from one file operation, with the operation and the path it was on."""

    def __init__(self, operation: str, path: Path, error: OSError) -> None:
        """Remember what failed where, and how."""
        super().__init__(operation, path, error)
        self.operation = operation
        self.path = path
        self.error = error


class LocalLog:
    """The daily CSV and events files under ``<directory>/logs``; see the module docstring.

    ``directory`` is the state directory, or ``None`` when ``$STATE_DIRECTORY`` is unset.
    ``wall`` is Unix seconds and stamps the exit event written by :meth:`close`.
    ``gzip_open(path, "wb", compresslevel=…)`` opens a compressed file for housekeeping. No
    method raises ``OSError``: :attr:`status` reports the outcome instead.
    """

    def __init__(
        self,
        settings: Settings,
        directory: Path | None,
        wall: Callable[[], float],
        *,
        gzip_open: GzipOpen = gzip.open,
    ) -> None:
        """Touch nothing yet; logging is disabled, with one warning, without ``directory``."""
        self._settings = settings
        self._wall = wall
        self._gzip_open = gzip_open
        self._directory = Path() if directory is None else directory / LOG_DIRECTORY
        self._files: dict[str, _File] = {}
        self._status = LocalLogStatus.OK
        if not settings.local_log:
            self._status = LocalLogStatus.DISABLED
        elif directory is None:
            self._status = LocalLogStatus.DISABLED
            _log.warning("STATE_DIRECTORY is unset; local logging is disabled")

    @property
    def status(self) -> LocalLogStatus:
        """``disabled``, ``error`` or ``ok`` (LOG-21)."""
        return self._status

    def write(self, snapshot: Snapshot, alerts: AlertState, ts: float) -> None:
        """Append one row per module and one stack row for wall time ``ts`` (LOG-06..08).

        Each file is flushed after its rows. Nothing is written unless the status is ``ok``.
        """
        if self._status is not LocalLogStatus.OK:
            return
        modules = (
            module_payload(ms, alerts.module(ms.module), self._settings.module_name(ms.module), ts)
            for ms in snapshot.modules
        )
        self._append("modules", ts, _csv(_row(p, MODULE_COLUMNS) for p in modules))
        stack = stack_payload(snapshot.stack, alerts.stack, ts)
        self._append("stack", ts, _csv([_row(stack, STACK_COLUMNS)]))

    def event(self, ts: float, line: str) -> None:
        """Append ``<ts> <line>`` to the events file of ``ts`` and flush it (LOG-09)."""
        self._append("events", ts, f"{iso_utc(ts)} {line}\n")

    def close(self, line: str) -> None:
        """Append the exit event ``line`` stamped now, then close every file (LOG-10)."""
        if self._status is LocalLogStatus.DISABLED:
            return
        self.event(self._wall(), line)
        try:
            self._close_all()
        except _Failure as failure:
            self._fail(failure)

    def housekeep(self, now_wall: float) -> None:
        """Create the directory, compress and delete old files, and recover from ``error``.

        While the status is ``error`` the open files are closed first and today's files are
        reopened afterwards; if all of it succeeds the status returns to ``ok`` (LOG-20).
        """
        if self._status is LocalLogStatus.DISABLED:
            return
        recovering = self._status is LocalLogStatus.ERROR
        try:
            if recovering:
                self._close_all()
            self._tidy(now_wall)
            if recovering:
                for prefix in _SUFFIXES:
                    self._file(prefix, day_name(prefix, now_wall))
        except _Failure as failure:
            self._fail(failure)
            return
        if recovering:
            self._status = LocalLogStatus.OK
            _log.info("local log recovered")

    def _append(self, prefix: str, ts: float, text: str) -> None:
        """Write ``text`` to the ``prefix`` file of the UTC day of ``ts`` and flush it."""
        if self._status is not LocalLogStatus.OK:
            return
        try:
            file = self._file(prefix, day_name(prefix, ts))
            with _step("write", file.path):
                file.handle.write(text)
            with _step("flush", file.path):
                file.handle.flush()
        except _Failure as failure:
            self._fail(failure)

    def _file(self, prefix: str, name: str) -> _File:
        """Return the open ``prefix`` file called ``name``, closing another day's first."""
        current = self._files.get(prefix)
        if current is not None and current.path.name == name:
            return current
        if current is not None:
            del self._files[prefix]
            with _step("close", current.path):
                current.handle.close()
        return self._open(prefix, self._directory / name)

    def _open(self, prefix: str, path: Path) -> _File:
        """Open ``path`` for appending (LOG-05).

        A new or empty CSV file gets its header; a file left with a partial last line gets
        a newline first, so the next row or line starts on a line of its own (LOG-20).
        The file stays open until the day rolls over or :meth:`close`.
        """
        with _step("open", path):
            partial = _ends_without_newline(path)
            handle = open(path, "a", encoding="utf-8", newline="")  # noqa: SIM115
        file = self._files[prefix] = _File(path, handle)
        with _step("write", path):
            if handle.tell() == 0 and prefix in _HEADERS:
                handle.write(_csv([_HEADERS[prefix]]))
            elif partial:
                handle.write("\n")
        return file

    def _close_all(self) -> None:
        """Close every open file; raise the first failure after trying them all."""
        files, self._files = self._files, {}
        first: _Failure | None = None
        for file in files.values():
            try:
                with _step("close", file.path):
                    file.handle.close()
            except _Failure as failure:
                first = first or failure
        if first is not None:
            raise first

    def _tidy(self, now_wall: float) -> None:
        """Create the log directory, then delete and compress as planned (LOG-14..16).

        At most :data:`MAX_COMPRESS_PER_RUN` files are compressed, in the plan's order.
        """
        settings = self._settings
        with _step("create", self._directory):
            self._directory.mkdir(exist_ok=True)
        with _step("list", self._directory):
            to_gzip, to_delete = housekeeping_plan(
                self._directory,
                now_wall,
                settings.local_log_compress_after_days,
                settings.local_log_retention_days,
            )
        for path in to_delete:
            with _step("delete", path):
                path.unlink(missing_ok=True)
        for path in to_gzip[:MAX_COMPRESS_PER_RUN]:
            self._compress(path)

    def _compress(self, path: Path) -> None:
        """Gzip ``path`` to ``<name>.gz``; delete it only once that is written and closed."""
        with _step("compress", path), open(path, "rb") as source:
            sink = self._gzip_open(
                path.with_name(path.name + _GZ), "wb", compresslevel=GZIP_COMPRESSLEVEL
            )
            try:
                shutil.copyfileobj(source, sink)
            finally:
                sink.close()
        with _step("delete", path):
            path.unlink()

    def _fail(self, failure: _Failure) -> None:
        """Turn the status to ``error``; log ``ERROR`` only on that change (LOG-19)."""
        level = logging.DEBUG if self._status is LocalLogStatus.ERROR else logging.ERROR
        self._status = LocalLogStatus.ERROR
        _log.log(
            level,
            "local log %s failed: %s: %s",
            failure.operation,
            failure.path,
            failure.error,
        )


def day_name(prefix: str, wall: float) -> str:
    """Return the daily file name of ``prefix`` for Unix time ``wall``.

    ``prefix`` is ``modules``, ``stack`` or ``events``; for example
    ``modules-2026-10-09.csv`` or ``events-2026-10-09.log`` (LOG-04).

    Raises:
        ValueError: ``prefix`` is not one of the three.

    """
    suffix = _SUFFIXES.get(prefix)
    if suffix is None:
        raise ValueError(f"unknown local log file prefix {prefix!r}")
    return f"{prefix}-{_utc_date(wall).isoformat()}{suffix}"


def housekeeping_plan(
    directory: Path, now_wall: float, compress_after_days: int, retention_days: int
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return ``(to_gzip, to_delete)`` for the history files in ``directory`` (LOG-14..16).

    Only regular files with a LOG-14 name and a valid date count. A file's age is the UTC
    date of ``now_wall`` minus the date in its name; files aged 0 or less are left alone.
    Older than ``retention_days`` means delete; otherwise an uncompressed file older than
    ``compress_after_days`` is to be gzipped. The function only lists ``directory``.

    Raises:
        OSError: ``directory`` cannot be listed.

    """
    today = _utc_date(now_wall)
    to_gzip: list[Path] = []
    to_delete: list[Path] = []
    for path in sorted(directory.iterdir()):
        day = _name_date(path.name)
        if day is None or not path.is_file():
            continue
        age = (today - day).days
        if age <= 0:
            continue
        if age > retention_days:
            to_delete.append(path)
        elif age > compress_after_days and not path.name.endswith(_GZ):
            to_gzip.append(path)
    return tuple(to_gzip), tuple(to_delete)


def _name_date(name: str) -> date | None:
    """Return the date in a LOG-14 file name, or ``None`` for any other name."""
    for pattern in _NAMES:
        match = pattern.fullmatch(name)
        if match is not None:
            try:
                return date.fromisoformat(match["day"])
            except ValueError:
                return None
    return None


def _utc_date(wall: float) -> date:
    """Return the UTC date of Unix time ``wall``, truncated like ``iso_utc``."""
    return datetime.fromtimestamp(math.floor(wall), UTC).date()


def _ends_without_newline(path: Path) -> bool:
    """Return whether ``path`` exists, is not empty and does not end in a newline."""
    try:
        with open(path, "rb") as handle:
            if handle.seek(0, io.SEEK_END) == 0:
                return False
            handle.seek(-1, io.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


@contextmanager
def _step(operation: str, path: Path) -> Iterator[None]:
    """Turn an ``OSError`` raised inside into a :class:`_Failure` naming ``operation``."""
    try:
        yield
    except OSError as error:
        raise _Failure(operation, path, error) from error


def _csv(rows: Iterable[Sequence[str]]) -> str:
    """Return ``rows`` as CSV: ``,`` separators, minimal quoting, newline endings (LOG-05)."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def _row(payload: Mapping[str, object], columns: Sequence[str]) -> list[str]:
    """Return the CSV fields of ``columns`` from a state payload (LOG-06)."""
    return [_field(payload[column]) for column in columns]


def _field(value: object) -> str:
    """Encode one payload value: empty for null, JSON text for booleans and numbers.

    Strings are written as they are; a float is normalised so ``-0.0`` becomes ``0.0``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        value += 0.0
    return json.dumps(value)
