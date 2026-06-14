"""Tests for pf_core.parallel — parallel execution helper."""

from __future__ import annotations

import threading

from pf_core.parallel import _item_label, resilient, run_parallel


class TestRunParallel:
    def test_empty_items(self):
        called = []
        run_parallel([], lambda x: called.append(x))
        assert called == []

    def test_sequential_processing(self):
        results = []
        run_parallel([1, 2, 3], lambda x: results.append(x), workers=1, label="Done")
        assert sorted(results) == [1, 2, 3]

    def test_parallel_processing(self):
        results = []
        run_parallel([1, 2, 3], lambda x: results.append(x), workers=3, label="Done")
        assert sorted(results) == [1, 2, 3]

    def test_progress_callback(self):
        calls = []
        run_parallel(
            ["a", "b"],
            lambda x: None,
            workers=1,
            progress_callback=lambda done, total: calls.append((done, total)),
        )
        assert calls == [(1, 2), (2, 2)]

    def test_exception_propagates(self):
        def fail(x):
            raise ValueError("boom")

        import pytest
        with pytest.raises(ValueError, match="boom"):
            run_parallel([1], fail)

    def test_parallel_exception_propagates(self):
        def fail(x):
            raise ValueError("boom")

        import pytest
        with pytest.raises(ValueError, match="boom"):
            run_parallel([1], fail, workers=2)

    def test_contextvars_propagate_to_parallel_workers(self):
        """ContextVars set in the calling thread must be visible inside
        worker threads. Without per-task ``copy_context()`` propagation,
        ``ThreadPoolExecutor.submit`` does not carry contextvars; the
        ``Job()`` ContextVar in ``pf_core.jobs.runtime`` would silently
        drop and downstream LLM run tracking would lose ``job_id``."""
        from contextvars import ContextVar

        cv: ContextVar[str] = ContextVar("test_cv", default="unset")
        cv.set("parent")

        seen: list[str] = []
        lock = threading.Lock()

        def record(item):
            with lock:
                seen.append(cv.get())

        run_parallel(list(range(8)), record, workers=4)
        assert seen == ["parent"] * 8

    def test_contextvars_isolate_between_workers(self):
        """Mutations to a ContextVar inside one worker must not leak into
        siblings. ``copy_context()`` per task gives each worker an
        independent snapshot."""
        from contextvars import ContextVar

        cv: ContextVar[int] = ContextVar("test_cv_isolate", default=0)
        cv.set(0)

        results: list[int] = []
        lock = threading.Lock()

        def mutate_and_read(item):
            cv.set(item)  # only visible to this worker
            with lock:
                results.append(cv.get())

        run_parallel([1, 2, 3, 4, 5, 6, 7, 8], mutate_and_read, workers=4)
        # Each worker sees its own value; parent's value (0) is unchanged.
        assert sorted(results) == [1, 2, 3, 4, 5, 6, 7, 8]
        assert cv.get() == 0


class TestResilient:
    def test_success_path_does_not_record_failure(self):
        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda x: f"item-{x}")
        def fn(x):
            return x * 2

        results = []
        run_parallel([1, 2, 3], lambda x: results.append(fn(x)), workers=2)
        assert sorted(results) == [2, 4, 6]
        assert failures == []

    def test_exception_records_failure_and_continues(self):
        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda x: f"item-{x}")
        def fn(x):
            if x == 2:
                raise RuntimeError("boom")
            return x

        results = []
        run_parallel([1, 2, 3], lambda x: results.append(fn(x)), workers=1)
        # Sibling items still ran
        assert 1 in results and 3 in results
        # The failed item's label is in failures with type-prefixed reason
        assert failures == [("item-2", "RuntimeError: boom")]

    def test_app_error_uses_clean_message(self):
        from pf_core.exceptions import AppError

        failures: list[tuple[str, str]] = []

        @resilient(failures)
        def fn(x):
            raise AppError("clean reason")

        run_parallel(["only"], fn, workers=1)
        assert failures == [("only", "clean reason")]

    def test_flow_exception_uses_clean_message(self):
        from pf_core.exceptions import FlowException

        failures: list[tuple[str, str]] = []

        @resilient(failures)
        def fn(x):
            raise FlowException("not a bug")

        run_parallel(["only"], fn, workers=1)
        assert failures == [("only", "not a bug")]

    def test_default_label_is_str_of_item(self):
        failures: list[tuple[str, str]] = []

        @resilient(failures)
        def fn(x):
            raise ValueError("nope")

        run_parallel([{"id": 7}], fn, workers=1)
        # str of the dict — exact form is stable for this assertion
        assert len(failures) == 1
        assert failures[0][0] == "{'id': 7}"
        assert failures[0][1] == "ValueError: nope"

    def test_concurrent_failures_are_thread_safe(self):
        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda x: f"item-{x}")
        def fn(x):
            raise RuntimeError(f"err-{x}")

        run_parallel(list(range(50)), fn, workers=8)
        assert len(failures) == 50
        # Every item is represented once — no race-condition drops
        labels = sorted(label for label, _ in failures)
        assert labels == sorted(f"item-{i}" for i in range(50))

    def test_reporter_receives_error_when_given(self):
        from pf_core.output import Reporter

        captured: list[str] = []

        class _CaptureReporter(Reporter):
            def info(self, msg, **kw):
                pass

            def step(self, msg, **kw):
                pass

            def warning(self, msg, **kw):
                pass

            def error(self, msg, **kw):
                captured.append(msg.format(**kw))

            def done(self, msg, **kw):
                pass

        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda x: x, reporter=_CaptureReporter())
        def fn(x):
            raise RuntimeError("nope")

        run_parallel(["a", "b"], fn, workers=1)
        assert any("✗ a: RuntimeError: nope" in line for line in captured)
        assert any("✗ b: RuntimeError: nope" in line for line in captured)

    def test_catch_narrowing_lets_other_exceptions_propagate(self):
        import pytest

        failures: list[tuple[str, str]] = []

        @resilient(failures, catch=ValueError)
        def fn(x):
            raise RuntimeError("not caught by ValueError filter")

        with pytest.raises(RuntimeError, match="not caught"):
            run_parallel([1], fn, workers=1)
        assert failures == []


class TestBatchSummary:
    """A3b: ``run_parallel(failures=...)`` — opt-in end-of-batch summary
    log. When the caller passes the same ``failures`` list it gave to
    ``resilient(...)``, run_parallel logs a summary at end-of-batch with
    succeeded / failed counts and percent failure rate. ``info`` if no
    failures, ``warning`` if any."""

    def test_no_summary_when_failures_arg_omitted(self, caplog):
        """Default behavior preserved: no summary log if the caller
        doesn't opt in by passing a ``failures=`` list."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="pf_core.parallel"):
            run_parallel([1, 2, 3], lambda x: None, workers=1, label="Done")
        for record in caplog.records:
            assert "batch_complete" not in record.getMessage()

    def test_summary_emitted_when_failures_arg_provided(self, caplog):
        """Empty failures list → info-level summary that all succeeded."""
        import logging

        failures: list[tuple[str, str]] = []

        with caplog.at_level(logging.INFO, logger="pf_core.parallel"):
            run_parallel(
                [1, 2, 3], lambda x: None, workers=1,
                label="Graded", failures=failures,
            )
        summary_records = [
            r for r in caplog.records if "batch_complete" in r.getMessage()
        ]
        assert len(summary_records) == 1
        msg = summary_records[0].getMessage()
        assert "succeeded" in msg or "all_succeeded" in msg

    def test_summary_warning_level_when_failures_present(self, caplog):
        import logging

        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda x: f"item-{x}")
        def fn(x):
            if x == 2:
                raise RuntimeError("boom")
            return x

        with caplog.at_level(logging.INFO, logger="pf_core.parallel"):
            run_parallel(
                [1, 2, 3], fn, workers=1, label="Graded", failures=failures,
            )
        summary_records = [
            r for r in caplog.records if "batch_complete" in r.getMessage()
        ]
        assert len(summary_records) == 1
        # When failures are present, summary is at WARNING level
        assert summary_records[0].levelname == "WARNING"

    def test_summary_includes_label(self, caplog):
        import logging

        failures: list[tuple[str, str]] = []

        with caplog.at_level(logging.INFO, logger="pf_core.parallel"):
            run_parallel(
                [1, 2], lambda x: None, workers=1,
                label="MyCustomLabel", failures=failures,
            )
        summary_records = [
            r for r in caplog.records if "batch_complete" in r.getMessage()
        ]
        assert len(summary_records) == 1
        # The label flows through as a structured field; rendered message
        # contains it one way or another (key=value or formatted)
        assert "MyCustomLabel" in summary_records[0].getMessage()

    def test_summary_includes_succeeded_failed_counts(self, caplog):
        import logging

        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda x: f"item-{x}")
        def fn(x):
            if x in (2, 4):
                raise RuntimeError("boom")
            return x

        with caplog.at_level(logging.INFO, logger="pf_core.parallel"):
            run_parallel(
                [1, 2, 3, 4, 5], fn, workers=1,
                label="Graded", failures=failures,
            )
        summary_records = [
            r for r in caplog.records if "batch_complete" in r.getMessage()
        ]
        assert len(summary_records) == 1
        msg = summary_records[0].getMessage()
        # 3 succeeded, 2 failed (40% failure rate)
        assert "3" in msg  # succeeded count
        assert "2" in msg  # failed count

    def test_summary_works_with_parallel_workers(self, caplog):
        """The summary log fires once after all parallel workers finish,
        not once per worker."""
        import logging

        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda x: str(x))
        def fn(x):
            if x % 3 == 0:
                raise RuntimeError("boom")
            return x

        with caplog.at_level(logging.INFO, logger="pf_core.parallel"):
            run_parallel(
                list(range(1, 13)), fn, workers=4,
                label="Done", failures=failures,
            )
        summary_records = [
            r for r in caplog.records if "batch_complete" in r.getMessage()
        ]
        assert len(summary_records) == 1
        # 4 of 12 fail (multiples of 3: 3, 6, 9, 12)
        assert len(failures) == 4

    def test_summary_skipped_when_items_empty(self, caplog):
        """Empty input: nothing to summarize — preserve the early-return
        from the run_parallel body."""
        import logging

        failures: list[tuple[str, str]] = []

        with caplog.at_level(logging.INFO, logger="pf_core.parallel"):
            run_parallel(
                [], lambda x: None, workers=1,
                label="Done", failures=failures,
            )
        summary_records = [
            r for r in caplog.records if "batch_complete" in r.getMessage()
        ]
        assert summary_records == []


class TestItemLabel:
    def test_string_item(self):
        assert _item_label("hello world") == "hello world"

    def test_long_string_truncated(self):
        long_str = "x" * 100
        assert len(_item_label(long_str)) == 60

    def test_tuple_with_int_first(self):
        assert _item_label((1, "my task")) == "my task"

    def test_tuple_with_string_first(self):
        assert _item_label(("task_name", "details")) == "task_name"

    def test_short_tuple(self):
        assert _item_label((42,)) == "(42,)"

    def test_integer_item(self):
        assert _item_label(42) == "42"
