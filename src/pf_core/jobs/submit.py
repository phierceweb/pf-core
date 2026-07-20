"""Background thread submitter for web-triggered jobs.

``submit_tracked`` creates the job row, then runs the work on a daemon
thread inside a :class:`~pf_core.jobs.runtime.Job` window — the HTTP
request returns the job id immediately and the UI polls the row.
``submit_detached`` runs a service that creates and manages its *own* job
and resolves that job's id for the caller. ``wait_all`` joins outstanding
worker threads — return it from the ``pf_engine_teardown`` fixture so a
mid-query worker can't outlive the test database.

Dedup is scope-blind: pass ``dedup_key`` — a predicate over a candidate
job's parsed ``inputs`` — to define what "already running" means for your
domain (same section, same tenant, …). Without it, no dedup is applied.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from pf_core.jobs.registry import TERMINAL_STATES
from pf_core.jobs.repo import JobRepo
from pf_core.jobs.runtime import Job
from pf_core.log import get_logger
from pf_core.utils.json import safe_json_loads

logger = get_logger(__name__)

__all__ = ["JobAlreadyRunning", "submit_detached", "submit_tracked", "wait_all"]

_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()

_DEDUP_SCAN_LIMIT = 50


class JobAlreadyRunning(ValueError):
    """A non-terminal job of the same kind already matches the dedup scope."""


def _spawn(target: Callable[[], None]) -> threading.Thread:
    t = threading.Thread(target=target, daemon=True)
    t.start()
    with _threads_lock:
        _threads.append(t)
        _threads[:] = [x for x in _threads if x.is_alive()]
    return t


def wait_all(timeout: float | None = 10.0) -> None:
    """Join every outstanding worker thread (the test-teardown drain hook)."""
    with _threads_lock:
        snapshot = list(_threads)
    for t in snapshot:
        t.join(timeout=timeout)
    with _threads_lock:
        _threads[:] = [x for x in _threads if x.is_alive()]


def _inputs_dict(inputs: Any) -> dict:
    if isinstance(inputs, str):
        return safe_json_loads(inputs, fallback={}) or {}
    return inputs or {}


def _running_job_for(kind: str, match: Callable[[dict], bool]) -> int | None:
    for row in JobRepo().find(kind=kind, limit=_DEDUP_SCAN_LIMIT):
        if row.get("status") in TERMINAL_STATES:
            continue
        if match(_inputs_dict(row.get("inputs"))):
            return int(row["id"])
    return None


def _latest_job_id(kind: str, match: Callable[[dict], bool]) -> int:
    # find() is newest-first, so the first match has the highest id.
    for row in JobRepo().find(kind=kind, limit=_DEDUP_SCAN_LIMIT):
        if match(_inputs_dict(row.get("inputs"))):
            return int(row["id"])
    return 0


def _await_new_job(
    kind: str,
    match: Callable[[dict], bool],
    *,
    after_id: int,
    thread: threading.Thread,
    timeout: float,
) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = _latest_job_id(kind, match)
        if latest > after_id:
            return latest
        if not thread.is_alive():
            # A no-op service returns without creating a job — check once
            # more, then report None rather than waiting out the deadline.
            latest = _latest_job_id(kind, match)
            return latest if latest > after_id else None
        time.sleep(0.15)
    return None


def submit_tracked(
    *,
    kind: str,
    inputs: dict,
    created_by: str,
    run: Callable[[Callable], None],
    dedup_key: Callable[[dict], bool] | None = None,
) -> int:
    """Create a job and run *run(progress_callback)* on a background thread.

    Returns the new job id immediately. The thread transitions the job to
    ``running``, invokes ``run`` with a ``(done, total, message=None)``
    progress callback, and marks it ``succeeded`` unless the work already
    drove it terminal. An exception is recorded as ``failed`` by the
    :class:`Job` context manager; the submitter only logs it.

    Raises:
        JobAlreadyRunning: ``dedup_key`` matched a non-terminal same-kind job.
    """
    if dedup_key is not None:
        existing = _running_job_for(kind, dedup_key)
        if existing is not None:
            raise JobAlreadyRunning(
                f"{kind} already running for this scope (job #{existing})"
            )

    job_id = JobRepo().create(kind=kind, inputs=inputs, created_by=created_by)

    def body() -> None:
        try:
            with Job(job_id) as job:
                job.transition("running")

                def progress_callback(done, total, message=None):
                    job.progress(current=done, total=total, step=message)

                run(progress_callback)
                if job.status not in TERMINAL_STATES:
                    job.transition("succeeded")
        except Exception:
            # Job.__exit__ already marked the row failed; just log.
            logger.error("submitted job failed", job_id=job_id, kind=kind, exc_info=True)

    _spawn(body)
    return job_id


def submit_detached(
    *,
    kind: str,
    run: Callable[[], None],
    dedup_key: Callable[[dict], bool] | None = None,
    await_timeout: float = 12.0,
) -> int | None:
    """Run a service that creates its own job; resolve that job's id.

    Returns the created job's id, or ``None`` when the service decided
    there was nothing to do. Failures are the service's to record on its
    own job row; the submitter only logs.

    Raises:
        JobAlreadyRunning: ``dedup_key`` matched a non-terminal same-kind job.
    """
    match = dedup_key or (lambda _inputs: True)
    if dedup_key is not None:
        existing = _running_job_for(kind, dedup_key)
        if existing is not None:
            raise JobAlreadyRunning(
                f"{kind} already running for this scope (job #{existing})"
            )
    before = _latest_job_id(kind, match)

    def body() -> None:
        try:
            run()
        except Exception:
            logger.error("detached job failed", kind=kind, exc_info=True)

    t = _spawn(body)
    return _await_new_job(kind, match, after_id=before, thread=t, timeout=await_timeout)
