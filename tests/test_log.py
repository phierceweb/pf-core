"""Tests for pf_core.log — structured logging."""

from __future__ import annotations

import logging
import warnings

import pytest

import pf_core.log as log_mod
from pf_core.exceptions import (
    AppError,
    ClientError,
    InvalidInputError,
    TaskError,
)
from pf_core.log import get_logger, log_exception, log_verbose, setup_logging


@pytest.fixture(autouse=True)
def _reset_logging():
    """Reset setup state and isolate handler changes (root + 'app')."""
    log_mod._setup_done = False
    log_mod._app_logger_name = ""
    root = logging.getLogger()
    app = logging.getLogger("app")
    saved = (root.handlers[:], root.level, app.handlers[:])
    root.handlers.clear()
    app.handlers.clear()
    yield
    log_mod._setup_done = False
    log_mod._app_logger_name = ""
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    app.handlers[:] = saved[2]


class TestSetupLogging:
    def test_idempotent(self):
        setup_logging()
        setup_logging()  # should not raise

    def test_respects_level_arg(self):
        setup_logging(level="DEBUG")
        # Verify it ran without error

    def test_reads_env_var(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        setup_logging()

    def test_file_handler_created(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        setup_logging(log_file=str(log_file))
        # Default attaches to the root logger: console + file handlers.
        assert len(logging.getLogger().handlers) >= 2

    def test_no_file_handler_when_empty(self):
        setup_logging(log_file="")


class TestLoggerNameAdoption:
    """The fix for after-the-fact adopters: handlers go on the root logger by
    default, so a consumer's logs reach them whatever its package is named, and
    log_exception logs under the same tree (not a dead "app.exceptions")."""

    def test_default_attaches_to_root_not_app(self):
        # Clear any handler pytest's logging plugin parked on root so the
        # idempotence guard in setup_logging doesn't short-circuit.
        logging.getLogger().handlers.clear()
        logging.getLogger("app").handlers.clear()
        log_mod._setup_done = False

        setup_logging()
        assert logging.getLogger().handlers              # root got the handlers
        assert logging.getLogger("app").handlers == []   # not the legacy "app" logger
        assert log_mod._app_logger_name == ""

    def test_arbitrary_package_logger_has_no_own_handler_but_is_reachable(self):
        # Before the fix, a non-"app" logger had no reachable handler (handlers
        # lived on "app"). Now they're on root, so any package is covered.
        logging.getLogger().handlers.clear()
        log_mod._setup_done = False
        setup_logging(level="INFO")

        consumer = logging.getLogger("ingester.services.fetch")
        assert consumer.handlers == []                       # relies on propagation
        assert consumer.getEffectiveLevel() <= logging.INFO  # reaches root handler

    def test_named_scoping_still_works(self):
        setup_logging(app_logger_name="myapp")
        assert logging.getLogger("myapp").handlers
        assert log_mod._app_logger_name == "myapp"

    def test_log_exception_logs_under_root_tree(self, caplog):
        setup_logging()
        with caplog.at_level(logging.ERROR):
            log_exception(AppError("boom", context={"k": 1}))
        # Logged under "exceptions" (a child of root), not "app.exceptions".
        assert any(r.name == "exceptions" for r in caplog.records)

    def test_log_exception_name_follows_named_config(self, caplog):
        setup_logging(app_logger_name="myapp")
        with caplog.at_level(logging.ERROR):
            log_exception(AppError("x"))
        assert any(r.name == "myapp.exceptions" for r in caplog.records)
        assert log_mod._app_logger_name == "myapp"


class TestGetLogger:
    def test_returns_bound_logger(self):
        logger = get_logger("test.module")
        assert logger is not None

    def test_triggers_setup(self):
        assert log_mod._setup_done is False
        get_logger("test")
        assert log_mod._setup_done is True


class TestLogVerbose:
    def test_info_when_verbose(self, capfd):
        logger = get_logger("test.verbose")
        log_verbose(logger, "hello", verbose=True, key="val")
        # Just verify no exception

    def test_debug_when_not_verbose(self):
        logger = get_logger("test.verbose")
        log_verbose(logger, "hello", verbose=False, key="val")


class TestLogException:
    def test_flow_exception_default_warning(self):
        exc = InvalidInputError("bad input")
        log_exception(exc)  # should not raise

    def test_app_error_default_error(self):
        exc = AppError("boom", context={"task_id": 7})
        log_exception(exc)

    def test_custom_log_level(self):
        exc = InvalidInputError("x")
        log_exception(exc, log_level="error")

    def test_message_prepend(self):
        exc = AppError("failed")
        log_exception(exc, message_prepend="search step")

    def test_additional_context_merged(self):
        exc = AppError("failed", context={"a": 1})
        log_exception(exc, additional_context={"b": 2})

    def test_additional_context_wins_over_exc_context(self):
        exc = AppError("failed", context={"key": "from_exc"})
        log_exception(exc, additional_context={"key": "from_additional"})

    def test_cause_chain_context_merged(self):
        inner = AppError("inner", context={"inner_key": "inner_val"})
        outer = ClientError("outer", context={"outer_key": "outer_val"}, cause=inner)
        log_exception(outer)

    def test_cause_chain_priority(self):
        # ancestor context < exc context < additional_context
        ancestor = AppError("a", context={"k": "ancestor"})
        exc = ClientError("b", context={"k": "exc"}, cause=ancestor)
        log_exception(exc, additional_context={"k": "additional"})

    def test_event_prefix(self):
        exc = InvalidInputError("x")
        log_exception(exc, event_prefix="COMP")

    def test_non_framework_exception(self):
        exc = ValueError("plain python error")
        log_exception(exc)

    def test_task_error_with_running_log(self):
        exc = TaskError("failed", context={"task_id": 1}, running_log="step1\nstep2")
        log_exception(exc)

    def test_circular_cause_chain_handled(self):
        """Circular __cause__ chains don't cause infinite loops."""
        exc_a = AppError("a", context={"from": "a"})
        exc_b = AppError("b", context={"from": "b"}, cause=exc_a)
        # Manually create a cycle
        exc_a.__cause__ = exc_b
        log_exception(exc_b)  # should not hang or raise


class TestExcInfoRendering:
    """ConsoleRenderer owns exception rendering; `format_exc_info` must not sit
    in the console processor chain — structlog 25.x emits a UserWarning when it
    does (a consumer hit it and filtered it, noting it belongs in
    pf-core). JSON/file output must still carry the traceback."""

    def test_no_format_exc_info_warning_when_logging_exception(self):
        logging.getLogger().handlers.clear()
        log_mod._setup_done = False
        setup_logging()
        try:
            raise AppError("boom")
        except AppError as e:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                log_exception(e)
        msgs = [str(w.message) for w in caught]
        assert not any("format_exc_info" in m for m in msgs), msgs

    def test_file_output_still_includes_traceback(self, tmp_path):
        log_file = tmp_path / "exc.jsonl"
        logging.getLogger().handlers.clear()
        log_mod._setup_done = False
        setup_logging(log_file=str(log_file))
        try:
            raise AppError("kaboom")
        except AppError as e:
            log_exception(e)
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_file.read_text()
        assert "kaboom" in content
        assert "AppError" in content
        assert "Traceback" in content
