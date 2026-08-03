"""Tests for pf_core.log — structured logging."""

from __future__ import annotations

import logging
import warnings

import pytest
import structlog

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
    log_mod._installed_handlers.clear()
    root = logging.getLogger()
    app = logging.getLogger("app")
    saved = (root.handlers[:], root.level, app.handlers[:])
    root.handlers.clear()
    app.handlers.clear()
    yield
    log_mod._setup_done = False
    log_mod._app_logger_name = ""
    log_mod._installed_handlers.clear()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    app.handlers[:] = saved[2]


def _console_handlers() -> list[logging.Handler]:
    """This module's console handlers on the root logger (excludes file/pytest)."""
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)
        and not isinstance(h, logging.FileHandler)
    ]


class TestSetupLogging:
    def test_repeated_calls_do_not_stack_handlers(self):
        # Clear pytest's own root handlers so setup_logging actually installs.
        logging.getLogger().handlers.clear()
        setup_logging()
        setup_logging()
        assert len(_console_handlers()) == 1

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


class TestExplicitReconfigure:
    """Explicit setup_logging() replaces prior config; implicit setup via
    get_logger never overrides an explicit configuration."""

    def test_explicit_call_reconfigures_after_implicit(self):
        # Clear pytest's own root handlers so setup_logging actually installs.
        logging.getLogger().handlers.clear()
        get_logger("boot.module")  # import-time implicit setup
        setup_logging(level="DEBUG")
        handlers = _console_handlers()
        assert len(handlers) == 1
        assert handlers[0].level == logging.DEBUG

    def test_two_explicit_calls_last_wins(self):
        logging.getLogger().handlers.clear()
        setup_logging(level="INFO")
        setup_logging(level="DEBUG")
        handlers = _console_handlers()
        assert len(handlers) == 1
        assert handlers[0].level == logging.DEBUG

    def test_implicit_does_not_reset_explicit_config(self):
        logging.getLogger().handlers.clear()
        setup_logging(level="WARNING")
        before = _console_handlers()
        get_logger("some.module")
        after = _console_handlers()
        assert after == before
        assert after[0].level == logging.WARNING

    def test_reconfigure_with_foreign_handler_present_reinstalls(self):
        # A consumer handler added between explicit calls must not make a
        # reconfigure remove pf's handlers and then install nothing.
        root = logging.getLogger()
        root.handlers.clear()
        setup_logging(level="INFO")
        foreign = logging.NullHandler()
        root.addHandler(foreign)
        try:
            setup_logging(level="DEBUG")
            handlers = _console_handlers()
            assert len(handlers) == 1
            assert handlers[0].level == logging.DEBUG
            assert foreign in root.handlers
        finally:
            root.removeHandler(foreign)

    def test_first_setup_respects_preexisting_foreign_handlers(self):
        root = logging.getLogger()
        root.handlers.clear()
        foreign = logging.NullHandler()
        root.addHandler(foreign)
        try:
            setup_logging(level="INFO")
            assert _console_handlers() == []
            assert foreign in root.handlers
        finally:
            root.removeHandler(foreign)


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


def _record(caplog) -> logging.LogRecord:
    """The single record log_exception emitted. ``.msg`` is the structlog event
    dict, so assert on keys — never on rendered text."""
    records = [r for r in caplog.records if r.name == "exceptions"]
    assert len(records) == 1, records
    return records[0]


class TestLogException:
    def test_flow_exception_default_warning(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(InvalidInputError("bad input"))
        rec = _record(caplog)
        assert rec.levelno == logging.WARNING
        assert rec.msg["event"] == "APP-InvalidInputError"
        assert rec.msg["message"] == "bad input"
        assert rec.msg["exc_info"] is False  # no traceback for a FlowException

    def test_app_error_default_error(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(AppError("boom", context={"task_id": 7}))
        rec = _record(caplog)
        assert rec.levelno == logging.ERROR
        assert rec.msg["event"] == "APP-AppError"
        assert rec.msg["task_id"] == 7
        assert isinstance(rec.msg["exc_info"], AppError)

    def test_custom_log_level(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(InvalidInputError("x"), log_level="error")
        assert _record(caplog).levelno == logging.ERROR

    def test_message_prepend(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(AppError("failed"), message_prepend="search step")
        assert _record(caplog).msg["message"] == "search step: failed"

    def test_additional_context_merged(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(AppError("failed", context={"a": 1}), additional_context={"b": 2})
        rec = _record(caplog)
        assert rec.msg["a"] == 1
        assert rec.msg["b"] == 2

    def test_additional_context_wins_over_exc_context(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(
                AppError("failed", context={"key": "from_exc"}),
                additional_context={"key": "from_additional"},
            )
        assert _record(caplog).msg["key"] == "from_additional"

    def test_cause_chain_context_merged(self, caplog):
        inner = AppError("inner", context={"inner_key": "inner_val"})
        outer = ClientError("outer", context={"outer_key": "outer_val"}, cause=inner)
        with caplog.at_level(logging.DEBUG):
            log_exception(outer)
        rec = _record(caplog)
        assert rec.msg["inner_key"] == "inner_val"
        assert rec.msg["outer_key"] == "outer_val"

    def test_exc_context_wins_over_ancestor(self, caplog):
        ancestor = AppError("a", context={"k": "ancestor"})
        exc = ClientError("b", context={"k": "exc"}, cause=ancestor)
        with caplog.at_level(logging.DEBUG):
            log_exception(exc)
        assert _record(caplog).msg["k"] == "exc"

    def test_cause_chain_priority(self, caplog):
        # ancestor context < exc context < additional_context
        ancestor = AppError("a", context={"k": "ancestor"})
        exc = ClientError("b", context={"k": "exc"}, cause=ancestor)
        with caplog.at_level(logging.DEBUG):
            log_exception(exc, additional_context={"k": "additional"})
        assert _record(caplog).msg["k"] == "additional"

    def test_furthest_ancestor_wins_among_ancestors(self, caplog):
        oldest = AppError("oldest", context={"k": "oldest"})
        middle = AppError("middle", context={"k": "middle"}, cause=oldest)
        exc = ClientError("top", cause=middle)
        with caplog.at_level(logging.DEBUG):
            log_exception(exc)
        assert _record(caplog).msg["k"] == "oldest"

    def test_event_prefix(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(InvalidInputError("x"), event_prefix="COMP")
        assert _record(caplog).msg["event"] == "COMP-InvalidInputError"

    def test_non_framework_exception(self, caplog):
        with caplog.at_level(logging.DEBUG):
            log_exception(ValueError("plain python error"))
        rec = _record(caplog)
        assert rec.levelno == logging.ERROR
        assert rec.msg["event"] == "APP-ValueError"
        assert rec.msg["exc_info"] is False

    def test_task_error_with_running_log(self, caplog):
        exc = TaskError("failed", context={"task_id": 1}, running_log="step1\nstep2")
        with caplog.at_level(logging.DEBUG):
            log_exception(exc)
        rec = _record(caplog)
        assert rec.msg["event"] == "APP-TaskError"
        assert rec.msg["task_id"] == 1

    def test_reserved_context_keys_renamed_not_colliding(self, caplog):
        exc = AppError("boom", context={"message": "collide", "event": "e", "exc_info": "x"})
        with caplog.at_level(logging.ERROR):
            log_exception(exc)  # must not raise TypeError
        rec = next(r for r in caplog.records if r.name == "exceptions")
        assert rec.msg["ctx_message"] == "collide"
        assert rec.msg["ctx_event"] == "e"
        assert rec.msg["ctx_exc_info"] == "x"
        assert rec.msg["message"] == "boom"

    def test_circular_cause_chain_handled(self, caplog):
        """Circular __cause__ chains don't cause infinite loops."""
        exc_a = AppError("a", context={"from_a": 1})
        exc_b = AppError("b", context={"from_b": 2}, cause=exc_a)
        # Manually create a cycle
        exc_a.__cause__ = exc_b
        with caplog.at_level(logging.DEBUG):
            log_exception(exc_b)
        rec = _record(caplog)
        assert rec.msg["from_a"] == 1
        assert rec.msg["from_b"] == 2


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
