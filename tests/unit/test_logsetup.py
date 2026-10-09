"""Tests of the stdout log format (``logsetup``): CFG-10 and SVC-22."""

import io
import logging
import re
import sys
from collections.abc import Iterator

import pytest

from dyness_b3_bms_mqtt.logsetup import configure_logging


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    """Yield the root logger and restore its handlers and level afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.req("CFG-10", "SVC-22")
def test_one_line_level_first_no_timestamp(
    root_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    log = logging.getLogger("dyness_b3_bms_mqtt.test")
    log.debug("hidden at INFO")
    log.info("bus open on %s", "can0")
    log.warning("first line\nsecond line\r\nthird line")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("crashed")

    lines = stream.getvalue().splitlines()
    assert lines[:2] == [
        "INFO bus open on can0",
        "WARNING first line | second line | third line",
    ]
    assert len(lines) == 3
    assert lines[2].startswith("ERROR crashed | Traceback (most recent call last): | ")
    assert lines[2].endswith("RuntimeError: boom")
    assert all(re.match(r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) ", line) for line in lines)
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", stream.getvalue())
    assert "\x1b" not in stream.getvalue()
    assert "bus open on can0" in caplog.text


@pytest.mark.req("CFG-10", "SVC-22")
def test_reconfiguring_replaces_only_its_own_handler(
    root_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    foreign = logging.NullHandler()
    root_logger.addHandler(foreign)
    first, second = io.StringIO(), io.StringIO()
    configure_logging("WARNING", stream=first)
    configure_logging("debug", stream=second)
    assert foreign in root_logger.handlers
    assert caplog.handler in root_logger.handlers
    assert root_logger.level == logging.DEBUG

    logging.getLogger("dyness_b3_bms_mqtt.test").debug("detail")
    assert first.getvalue() == ""
    assert second.getvalue() == "DEBUG detail\n"
    assert "detail" in caplog.text


@pytest.mark.req("SVC-22")
def test_logs_to_stdout_by_default(root_logger: logging.Logger) -> None:
    before = set(root_logger.handlers)
    configure_logging("INFO")
    added = [handler for handler in root_logger.handlers if handler not in before]
    assert len(added) == 1
    assert isinstance(added[0], logging.StreamHandler)
    assert added[0].stream is sys.stdout
