"""
Parallel execution helper for batch operations.

Used by service layers to grade/analyze/process items in parallel with
progress tracking and error resilience.

Usage::

    from pf_core.parallel import run_parallel

    def grade_one(item):
        ...

    run_parallel(
        items=submissions,
        fn=grade_one,
        workers=4,
        label="Graded",
    )
"""

from __future__ import annotations

import functools
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import TYPE_CHECKING, Any, Callable

from pf_core.exceptions import AppError, FlowException
from pf_core.log import get_logger, log_exception

_log = get_logger(__name__)

if TYPE_CHECKING:
    from pf_core.output import Reporter


def run_parallel(
    items: list,
    fn: Callable,
    workers: int = 1,
    label: str = "Processed",
    progress_callback: Callable[[int, int], Any] | None = None,
    reporter: Reporter | None = None,
    failures: list[tuple[str, str]] | None = None,
) -> None:
    """Run fn over items with optional parallelism and progress tracking.

    Args:
        items: List of work items to process.
        fn: Callable that takes one item and processes it.
        workers: Number of parallel workers (1 = sequential).
        label: Progress label (e.g. "Graded", "Analyzed").
        progress_callback: Optional (done, total) callback for job tracking.
        reporter: Optional Reporter for progress output. When provided,
            replaces the default ``print()`` progress line.
        failures: Optional caller-owned list of ``(label, reason)`` tuples
            (the same one passed to :func:`resilient`). When provided,
            an end-of-batch summary log is emitted at INFO level if the
            list is empty (event ``batch_complete_all_succeeded``) or
            WARNING level if any failures were recorded
            (``batch_complete_with_failures`` with ``succeeded`` /
            ``failed`` / ``failure_rate`` fields). Defaults to ``None``
            (no summary log — preserves pre-A3b behavior).
    """
    total = len(items)
    if total == 0:
        return

    progress_lock = threading.Lock()
    done_count = [0]

    def tracked_fn(item: Any) -> Any:
        result = fn(item)
        with progress_lock:
            done_count[0] += 1
            item_label = _item_label(item)
            if reporter is not None:
                reporter.step(
                    "{label} {done}/{total}: {item}",
                    label=label,
                    done=done_count[0],
                    total=total,
                    item=item_label,
                )
            else:
                print(
                    f"  {label} {done_count[0]}/{total}: {item_label}",
                    file=sys.stderr,
                )
            if progress_callback:
                progress_callback(done_count[0], total)
        return result

    if workers <= 1:
        for item in items:
            tracked_fn(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Each worker gets its own snapshot of the caller's contextvars
            # so that ContextVars set in the parent (e.g. ``Job()`` in
            # ``pf_core.jobs.runtime``, used to attribute LLM runs to the
            # active job) are visible inside the worker. ``copy_context()``
            # is called in the parent thread per submission — sharing one
            # ``Context`` across workers raises "already entered" under
            # concurrent execution.
            futures = {
                executor.submit(copy_context().run, tracked_fn, item): item
                for item in items
            }
            for future in as_completed(futures):
                future.result()

    # Optional end-of-batch summary log. Only emitted when the caller
    # opts in by passing a ``failures=`` list (the same one fed to
    # ``resilient``). Splits success/failure into two distinct events
    # so log filters and dashboards can alert on the warning case.
    if failures is not None:
        failed = len(failures)
        succeeded = total - failed
        if failed == 0:
            _log.info(
                "batch_complete_all_succeeded",
                label=label,
                total=total,
                succeeded=succeeded,
            )
        else:
            _log.warning(
                "batch_complete_with_failures",
                label=label,
                total=total,
                succeeded=succeeded,
                failed=failed,
                failure_rate=round(100.0 * failed / total, 2),
            )


def _item_label(item: Any) -> str:
    """Extract a short label from a work item for progress display."""
    if isinstance(item, tuple) and len(item) >= 2:
        label = str(item[1]) if isinstance(item[0], int) else str(item[0])
        return label[:60]
    return str(item)[:60]


# ─── Resilient worker wrapper ────────────────────────────────────────────────


def resilient(
    failures: list[tuple[str, str]],
    *,
    label_fn: Callable[[Any], str] | None = None,
    reporter: Reporter | None = None,
    log_label: str = "worker failed",
    catch: type[BaseException] | tuple[type[BaseException], ...] = Exception,
) -> Callable[[Callable], Callable]:
    """Wrap a per-item worker for batch resilience inside ``run_parallel``.

    The wrapped function runs normally until it raises. On exception
    (matching ``catch``) the wrapper:

    - logs via ``pf_core.log.log_exception`` with the item label as
      structured context
    - appends ``(label, reason)`` to ``failures`` (thread-safe)
    - reports the failure via ``reporter.error(...)`` if a reporter is given
    - returns the label so callers that consume ``fn``'s return value
      (outside ``run_parallel``, which discards it) see a stable identifier
      on both success and failure paths

    No re-raise — siblings in the batch keep running. Callers compute
    written-vs-failed counts from ``len(failures)`` after ``run_parallel``
    returns.

    ``reason`` is ``str(exc)`` for ``AppError``/``FlowException`` (whose
    messages are intentional) and ``f"{type(exc).__name__}: {exc}"`` for
    everything else (so unexpected types stay diagnosable in a CSV/log row).

    Args:
        failures: A list the wrapper appends ``(label, reason)`` tuples to.
            Caller-owned so multiple decorators in the same batch can share
            it; thread-safe via an internal lock.
        label_fn: Extracts a string label from one item. Defaults to
            ``str(item)``. Same item shape as ``run_parallel``'s ``fn``.
        reporter: Optional ``pf_core.output.Reporter`` for user-facing
            "✗ {item}: {reason}" lines. ``None`` (the default) silences
            per-failure output — failures still land in ``failures``.
        log_label: Prefix for ``log_exception``'s message. Defaults to
            ``"worker failed"``; pass e.g. ``"grader failed"`` so log
            grepping is per-service.
        catch: Exception types to absorb. Defaults to ``Exception``;
            narrow this if a specific subset of failures should propagate
            (e.g. ``catch=GradingError`` would let ``KeyboardInterrupt``
            and unrelated bugs still surface).

    Example::

        from pf_core.parallel import resilient, run_parallel

        failures: list[tuple[str, str]] = []

        @resilient(failures, label_fn=lambda i: i[0], reporter=reporter,
                   log_label="grader failed")
        def grade_one(item):
            stem, answer = item
            with job.step(f"grade_{stem}") as step:
                if step.skipped:
                    return stem
                # ... raise on errors; pf-core marks the step failed via
                # the step context manager's own except path ...
            return stem

        run_parallel(items, grade_one, workers=4, label="Graded")
        written = len(items) - len(failures)
    """
    lock = threading.Lock()
    resolved_label_fn = label_fn or str

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(item: Any) -> Any:
            label = resolved_label_fn(item)
            try:
                return fn(item)
            except catch as exc:
                # Domain exceptions carry intentional messages; everything
                # else gets type-prefixed for diagnosis.
                if isinstance(exc, (AppError, FlowException)):
                    reason = str(exc)
                else:
                    reason = f"{type(exc).__name__}: {exc}"
                log_exception(
                    exc,
                    message_prepend=log_label,
                    additional_context={"item": label},
                )
                if reporter is not None:
                    reporter.error(
                        "  ✗ {item}: {reason}", item=label, reason=reason,
                    )
                with lock:
                    failures.append((label, reason))
                return label

        return wrapper

    return decorator
