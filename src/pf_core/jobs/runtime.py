"""
Job runtime — ``Job`` and ``Step`` context managers.

Wrap orchestration code so that state transitions, progress, step history,
and LLM-run attribution all flow through the DB automatically.

Usage::

    from pf_core.jobs import Job, JobRepo

    job_id = JobRepo().create(kind="grading_pass", inputs={...}, created_by="cli:grade")

    with Job(job_id) as job:
        job.transition("running")
        submissions = load_submissions(job.inputs["submission_ids"])
        job.progress(total=len(submissions))

        for i, sub in enumerate(submissions):
            with job.step(f"grade_{sub.id}") as step:
                # @track_run reads the active-job contextvar and sets
                # llm_runs.job_id automatically.
                ...
                step.outputs = {"grade": 28}
                job.progress(current=i + 1, step=f"graded {sub.id}")

        job.outputs = {"n_graded": len(submissions)}
        job.transition("succeeded")

On an unhandled exception inside the block, the job transitions to
``failed`` with ``error`` populated. The exception is re-raised.

See ``docs/jobs.md`` for the implementation reference.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from pf_core.exceptions import PreconditionError
from pf_core.jobs.repo import JobRepo


# ---------------------------------------------------------------------------
# Active-job context var (read by the tracking decorator)
# ---------------------------------------------------------------------------

#: The currently active job id. Set by ``Job.__enter__`` and read by
#: ``pf_core.llm.tracking.track_run`` so that ``llm_runs.job_id`` is
#: populated without the service knowing a job is active.
current_job_id: ContextVar[int | None] = ContextVar(
    "pf_core_current_job_id", default=None
)


def get_current_job_id() -> int | None:
    """Return the active job id, or ``None`` if no ``Job`` is open."""
    return current_job_id.get()


# ---------------------------------------------------------------------------
# Step handle
# ---------------------------------------------------------------------------


class _StepHandle:
    """Lightweight mutable handle yielded by ``Job.step(...)``.

    Services set ``outputs`` before the step exits and they get written to
    ``job_steps.outputs``. Setting ``error`` marks the step ``failed``.
    """

    __slots__ = ("id", "name", "outputs", "error", "skipped")

    def __init__(self, *, step_id: int | None, name: str, skipped: bool = False):
        self.id = step_id
        self.name = name
        self.outputs: Any | None = None
        self.error: str | None = None
        self.skipped = skipped


# ---------------------------------------------------------------------------
# Job context manager
# ---------------------------------------------------------------------------


class Job:
    """Open a job for orchestration. DB-backed state manager.

    Usage::

        with Job(job_id) as job:
            job.transition("running")
            ...

    On clean exit, the job is left in whatever status the block set. If the
    block raises, the job transitions to ``failed`` and the exception
    re-propagates.

    Args:
        job_id: An existing ``jobs.id`` (created via ``JobRepo().create``).
        repo: Optional pre-built ``JobRepo``; defaults to a fresh instance.
    """

    def __init__(self, job_id: int, *, repo: JobRepo | None = None):
        self._job_id = job_id
        self._repo = repo if repo is not None else JobRepo()
        self._token = None
        self._row: dict | None = None
        # Settable by the caller before transition("succeeded") — the
        # transition picks it up.
        self.outputs: Any | None = None

    # -- state ---------------------------------------------------------

    @property
    def id(self) -> int:
        return self._job_id

    @property
    def kind(self) -> str:
        return self._row["kind"] if self._row else ""

    @property
    def status(self) -> str:
        return self._row["status"] if self._row else ""

    @property
    def inputs(self) -> Any:
        return self._row["inputs"] if self._row else None

    # -- lifecycle -----------------------------------------------------

    def __enter__(self) -> Job:
        self._row = self._repo.get(self._job_id)
        if self._row is None:
            raise PreconditionError(f"Job {self._job_id} not found")
        self._token = current_job_id.set(self._job_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is not None:
                # Force-fail the job. Bypass the transition registry check
                # because the current status might not allow → failed (e.g.
                # still 'pending'): the service blew up before it could mark
                # itself 'running'. We mark it failed regardless and stash
                # the error.
                self._force_fail(
                    error=str(exc_val)[:10_000],
                    error_class=exc_type.__name__,
                )
            else:
                # Apply deferred outputs if the service set `job.outputs`
                # but hadn't called transition("succeeded") yet. Don't force
                # a transition — the service owns the final status.
                if self.outputs is not None:
                    fresh = self._repo.get(self._job_id)
                    if fresh is not None and fresh.get("outputs") is None:
                        from pf_core.jobs import _schema as s

                        with self._repo._tx() as conn:
                            conn.execute(
                                s.jobs.update()
                                .where(s.jobs.c.id == self._job_id)
                                .values(outputs=_maybe_dump(self.outputs))
                            )
        finally:
            if self._token is not None:
                current_job_id.reset(self._token)
                self._token = None

    # -- operations ----------------------------------------------------

    def transition(self, to_status: str, **kwargs) -> None:
        """Delegate to ``JobRepo.transition``. Picks up ``self.outputs`` when
        entering a terminal state.
        """
        if "outputs" not in kwargs and self.outputs is not None and to_status in (
            "succeeded",
            "partial",
        ):
            kwargs["outputs"] = self.outputs
        self._repo.transition(self._job_id, to_status, **kwargs)
        # Refresh cached row so `status` reflects the write.
        self._row = self._repo.get(self._job_id)

    def progress(
        self,
        *,
        current: int | None = None,
        total: int | None = None,
        step: str | None = None,
    ) -> None:
        """Update progress without changing status."""
        self._repo.set_progress(
            self._job_id, current=current, total=total, step=step
        )

    def event(
        self,
        event_type: str,
        message: str,
        *,
        context: dict | None = None,
    ) -> int:
        """Append a diagnostic row to ``job_events``."""
        return self._repo.add_event(
            self._job_id,
            event_type=event_type,
            message=message,
            context=context,
        )

    @contextmanager
    def step(self, name: str, *, inputs: Any | None = None) -> Iterator[_StepHandle]:
        """Context manager for a single step.

        Idempotent: if a step with the same ``name`` already completed
        ``succeeded``, the block short-circuits and yields a handle with
        ``skipped=True``. Services that peek at ``handle.skipped`` can
        skip expensive work.

        On exception, marks the step ``failed`` with the exception message;
        re-raises so the job also fails.
        """
        prior = self._repo.find_step(self._job_id, name=name)
        if prior is not None and prior.get("status") == "succeeded":
            yield _StepHandle(step_id=prior["id"], name=name, skipped=True)
            return

        step_id = self._repo.start_step(self._job_id, name=name, inputs=inputs)
        handle = _StepHandle(step_id=step_id, name=name)
        try:
            yield handle
        except Exception as exc:
            self._repo.finish_step(
                step_id, status="failed", error=str(exc)[:10_000],
            )
            raise
        else:
            if handle.error is not None:
                self._repo.finish_step(
                    step_id, status="failed", error=handle.error,
                )
            else:
                self._repo.finish_step(
                    step_id, status="succeeded", outputs=handle.outputs,
                )

    # -- internals -----------------------------------------------------

    def _force_fail(self, *, error: str, error_class: str) -> None:
        """Transition to 'failed' regardless of current status.

        Bypasses the registry transition check because an unhandled
        exception can happen from any non-terminal state. Writes a
        ``job_events`` row with the exception details.
        """
        from sqlalchemy import func

        from pf_core.jobs import _schema as s
        from pf_core.jobs.registry import TERMINAL_STATES

        with self._repo._tx() as conn:
            row = (
                conn.execute(
                    s.jobs.select().where(s.jobs.c.id == self._job_id)
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                return
            if row["status"] in TERMINAL_STATES:
                # Already terminal — don't overwrite. Still log event.
                pass
            else:
                conn.execute(
                    s.jobs.update()
                    .where(s.jobs.c.id == self._job_id)
                    .values(
                        status="failed",
                        error=error,
                        error_class=error_class,
                        finished_at=func.now(),
                        claimed_by=None,
                        claimed_at=None,
                        updated_at=func.now(),
                    )
                )
            conn.execute(
                s.job_events.insert().values(
                    job_id=self._job_id,
                    event_type="exception",
                    message=error,
                    context={"error_class": error_class},
                )
            )


def _maybe_dump(value: Any) -> Any:
    """Coerce a Pydantic model to a dict; pass-through otherwise."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return value
