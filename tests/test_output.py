"""Tests for pf_core.output."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

from rich.console import Console

from pf_core.output import (
    ConsoleReporter,
    LogReporter,
    NullReporter,
    Reporter,
    _fmt,
)


# ---------------------------------------------------------------------------
# _fmt helper
# ---------------------------------------------------------------------------


class TestFmt:
    def test_basic_format(self):
        assert _fmt("hello {name}", {"name": "world"}) == "hello world"

    def test_missing_key(self):
        assert _fmt("hello {name}", {}) == "hello {name}"

    def test_no_placeholders(self):
        assert _fmt("hello", {}) == "hello"


# ---------------------------------------------------------------------------
# NullReporter
# ---------------------------------------------------------------------------


class TestNullReporter:
    def test_all_methods_callable(self):
        r = NullReporter()
        r.info("hello")
        r.warning("warn")
        r.error("err")
        r.step("step")
        r.done("done")

    def test_isinstance_check(self):
        assert isinstance(NullReporter(), Reporter)


# ---------------------------------------------------------------------------
# ConsoleReporter
# ---------------------------------------------------------------------------


def _make_console_reporter() -> tuple[ConsoleReporter, StringIO]:
    """Create a ConsoleReporter backed by a StringIO for capture."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, highlight=False)
    return ConsoleReporter(console=console), buf


class TestConsoleReporter:
    def test_info_output(self):
        r, buf = _make_console_reporter()
        r.info("hello info")
        assert "hello info" in buf.getvalue()

    def test_warning_output(self):
        r, buf = _make_console_reporter()
        r.warning("be careful")
        assert "be careful" in buf.getvalue()

    def test_error_output(self):
        r, buf = _make_console_reporter()
        r.error("something broke")
        assert "something broke" in buf.getvalue()

    def test_step_output(self):
        r, buf = _make_console_reporter()
        r.step("doing a thing")
        assert "doing a thing" in buf.getvalue()

    def test_done_output(self):
        r, buf = _make_console_reporter()
        r.done("all finished")
        assert "all finished" in buf.getvalue()

    def test_format_placeholders(self):
        r, buf = _make_console_reporter()
        r.info("Count: {n}", n=5)
        assert "Count: 5" in buf.getvalue()

    def test_format_error_graceful(self):
        r, buf = _make_console_reporter()
        r.info("Missing {x}")
        assert "Missing {x}" in buf.getvalue()

    def test_isinstance_check(self):
        assert isinstance(ConsoleReporter(), Reporter)


# ---------------------------------------------------------------------------
# LogReporter
# ---------------------------------------------------------------------------


class TestLogReporter:
    def test_info_calls_logger_info(self):
        logger = MagicMock()
        r = LogReporter(logger)
        r.info("hello {name}", name="world")
        logger.info.assert_called_once_with("hello world", name="world")

    def test_error_calls_logger_error(self):
        logger = MagicMock()
        r = LogReporter(logger)
        r.error("boom {code}", code=500)
        logger.error.assert_called_once_with("boom 500", code=500)

    def test_step_calls_logger_debug(self):
        logger = MagicMock()
        r = LogReporter(logger)
        r.step("step {n}", n=1)
        logger.debug.assert_called_once_with("step 1", n=1)

    def test_done_calls_logger_info(self):
        logger = MagicMock()
        r = LogReporter(logger)
        r.done("finished {count}", count=10)
        logger.info.assert_called_once_with("finished 10", count=10)

    def test_isinstance_check(self):
        logger = MagicMock()
        assert isinstance(LogReporter(logger), Reporter)
