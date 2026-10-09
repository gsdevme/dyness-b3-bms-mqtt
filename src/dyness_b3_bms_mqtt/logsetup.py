"""Stdout logging in the journald-friendly format of CFG-10 and SVC-22.

Every record becomes exactly one line, ``LEVEL message``, with no timestamp (journald adds
one) and no colour. Line breaks inside a message or a traceback are joined with ``" | "``
so a multi-line record cannot be split into several journal entries.
"""

import logging
import sys
from typing import TextIO

FORMAT = "%(levelname)s %(message)s"
SEPARATOR = " | "
HANDLER_NAME = "dyness-b3-bms-mqtt"


class OneLineFormatter(logging.Formatter):
    """Format a record, traceback included, as a single line."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the formatted record with every line break replaced by ``SEPARATOR``."""
        return SEPARATOR.join(super().format(record).splitlines())


def configure_logging(level: str | int, stream: TextIO | None = None) -> None:
    """Send log records at ``level`` and above to ``stream`` (stdout by default).

    The handler is installed on the root logger and tagged with ``HANDLER_NAME``. Calling
    this again replaces only that tagged handler, so handlers installed by others (such as
    pytest's ``caplog``) keep working. A string ``level`` is case-insensitive.
    """
    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == HANDLER_NAME]:
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(OneLineFormatter(FORMAT))
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
