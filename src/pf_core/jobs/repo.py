"""
Job repositories.

``JobRepo`` is the atomic writer/reader for ``jobs`` and its sidecars
(``job_steps``, ``job_events``). It enforces state transitions via the
kind registry, serializes Pydantic-validated inputs/outputs as JSON, and
handles the distributed-worker claim pattern. Split by concern:
lifecycle here, worker claim/reclaim/purge in ``_repo_worker``, step and
event records in ``_repo_steps``, shared helpers in ``_repo_util``.

See ``docs/jobs.md`` for the implementation reference.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select

from pf_core.db.repository import Repository
from pf_core.exceptions import InvalidInputError, PreconditionError
from pf_core.jobs import _schema as s
from pf_core.jobs._repo_steps import StepEventsMixin
from pf_core.jobs._repo_util import (  # noqa: F401 — lock re-exported; consumers assert on it
    _coerce_row_utc,
    _dump_model,
    _normalize_input_dt,
    _step_creation_lock,
)
from pf_core.jobs._repo_worker import WorkerOpsMixin
from pf_core.jobs.registry import TERMINAL_STATES, get_kind


class JobRepo(WorkerOpsMixin, StepEventsMixin, Repository):
    """Atomic writes and reads for ``jobs`` + ``job_steps`` + ``job_events``."""

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        kind: str,
        inputs: Any | None = None,
        created_by: str | None = None,
        priority: int | None = None,
        parent_job_id: int | None = None,
        progress_total: int | None = None,
    ) -> int:
        """Insert a ``jobs`` row in ``pending`` state. Returns the new ``id``.

        Validates ``kind`` against the registry and ``inputs`` against the
        kind's registered Pydantic schema.
        """
        descriptor = get_kind(kind)
        validated = descriptor.validate_inputs(inputs) if inputs is not None else None
        resolved_priority = (
            priority if priority is not None else descriptor.default_priority
        )
        if not (0 <= resolved_priority <= 100):
            raise InvalidInputError(
                f"priority must be 0-100, got {resolved_priority}",
            )

        with self._tx() as conn:
            result = conn.execute(
                s.jobs.insert().values(
                    kind=kind,
                    status="pending",
                    parent_job_id=parent_job_id,
                    inputs=_dump_model(validated) if validated is not None else None,
                    priority=resolved_priority,
                    created_by=created_by,
                    progress_total=progress_total,
                    progress_current=0,
                )
            )
            return int(result.inserted_primary_key[0])

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, job_id: int) -> dict | None:
        """Return the ``jobs`` row as a dict, or ``None`` if not found.

        Datetime columns (``created_at``, ``updated_at``, ``started_at``,
        ``finished_at``, ``claimed_at``) are returned as aware UTC — safe
        to compare against ``datetime.now(timezone.utc)`` without
        wrapping.
        """
        with self._tx() as conn:
            row = (
                conn.execute(select(s.jobs).where(s.jobs.c.id == job_id))
                .mappings()
                .fetchone()
            )
        return _coerce_row_utc(dict(row)) if row else None

    def get_with_steps(self, job_id: int) -> dict | None:
        """Return the job joined with its ordered steps and recent events.

        All datetime columns — on the job, its steps, and its events —
        are returned as aware UTC.
        """
        with self._tx() as conn:
            row = (
                conn.execute(select(s.jobs).where(s.jobs.c.id == job_id))
                .mappings()
                .fetchone()
            )
            if row is None:
                return None
            steps = (
                conn.execute(
                    select(s.job_steps)
                    .where(s.job_steps.c.job_id == job_id)
                    .order_by(s.job_steps.c.step_index)
                )
                .mappings()
                .fetchall()
            )
            events = (
                conn.execute(
                    select(s.job_events)
                    .where(s.job_events.c.job_id == job_id)
                    .order_by(s.job_events.c.created_at)
                )
                .mappings()
                .fetchall()
            )
        out = _coerce_row_utc(dict(row))
        out["steps"] = [_coerce_row_utc(dict(r)) for r in steps]
        out["events"] = [_coerce_row_utc(dict(r)) for r in events]
        return out

    def find(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        parent_job_id: int | None = None,
        created_by: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List jobs matching the given filters, newest-first.

        ``since`` accepts aware or naive datetimes: aware values are
        converted to UTC, naive values are treated as UTC. Returned rows
        have datetime columns stamped as aware UTC.
        """
        stmt = select(s.jobs)
        conds = []
        if kind is not None:
            conds.append(s.jobs.c.kind == kind)
        if status is not None:
            conds.append(s.jobs.c.status == status)
        if since is not None:
            conds.append(s.jobs.c.created_at >= _normalize_input_dt(since))
        if parent_job_id is not None:
            conds.append(s.jobs.c.parent_job_id == parent_job_id)
        if created_by is not None:
            conds.append(s.jobs.c.created_by == created_by)
        if conds:
            stmt = stmt.where(and_(*conds))
        stmt = stmt.order_by(s.jobs.c.created_at.desc()).limit(limit)
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [_coerce_row_utc(dict(r)) for r in rows]

    #: Sort keys `find_page` accepts — a closed allowlist because the sort
    #: name typically arrives from a URL query parameter.
    _PAGE_SORTS = ("id", "kind", "status", "created_at")

    def find_page(
        self,
        *,
        sort: str = "created_at",
        direction: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """One sorted page of jobs plus the unfiltered total row count.

        Backs paginated jobs UIs without consumers touching the private
        schema. ``sort`` must be one of ``_PAGE_SORTS``; ``direction`` is
        ``"asc"`` or ``"desc"``. Ties break on ``id`` descending.
        """
        if sort not in self._PAGE_SORTS:
            raise InvalidInputError(
                f"sort must be one of {self._PAGE_SORTS}, got {sort!r}"
            )
        if direction not in ("asc", "desc"):
            raise InvalidInputError(f"direction must be asc|desc, got {direction!r}")
        col = s.jobs.c[sort]
        order = col.asc() if direction == "asc" else col.desc()
        stmt = (
            select(s.jobs)
            .order_by(order, s.jobs.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            total = conn.execute(
                select(func.count()).select_from(s.jobs)
            ).scalar_one()
        return [_coerce_row_utc(dict(r)) for r in rows], int(total)

    def descendants(self, parent_job_id: int) -> list[dict]:
        """Return all child jobs of ``parent_job_id`` (one level deep).

        Datetime columns are returned as aware UTC.
        """
        with self._tx() as conn:
            rows = (
                conn.execute(
                    select(s.jobs)
                    .where(s.jobs.c.parent_job_id == parent_job_id)
                    .order_by(s.jobs.c.created_at)
                )
                .mappings()
                .fetchall()
            )
        return [_coerce_row_utc(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def transition(
        self,
        job_id: int,
        to_status: str,
        *,
        error: str | None = None,
        error_class: str | None = None,
        outputs: Any | None = None,
    ) -> None:
        """Move a job to ``to_status`` after validating the transition.

        Side effects:
          - Sets ``started_at`` when entering the first non-pending state.
          - Sets ``finished_at`` when entering a terminal state.
          - Validates ``outputs`` against the kind's schema when entering
            ``succeeded``.

        Raises ``PreconditionError`` if the job doesn't exist or the
        transition is not allowed by the registered kind.
        """
        with self._tx() as conn:
            row = (
                conn.execute(
                    select(s.jobs.c.id, s.jobs.c.kind, s.jobs.c.status, s.jobs.c.started_at)
                    .where(s.jobs.c.id == job_id)
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                raise PreconditionError(f"Job {job_id} not found")

            descriptor = get_kind(row["kind"])
            current_status = row["status"]
            if not descriptor.can_transition(current_status, to_status):
                raise PreconditionError(
                    f"Job {job_id} ({descriptor.kind!r}): cannot transition "
                    f"{current_status!r} → {to_status!r}. Allowed from "
                    f"{current_status!r}: {list(descriptor.transitions.get(current_status, ()))!r}",
                )

            values: dict[str, Any] = {
                "status": to_status,
                "updated_at": func.now(),
            }
            if row["started_at"] is None and to_status != "pending":
                values["started_at"] = func.now()
            if to_status in TERMINAL_STATES:
                values["finished_at"] = func.now()
                # Clear worker claim on terminal states so a reclaim doesn't
                # try to take an already-done job.
                values["claimed_by"] = None
                values["claimed_at"] = None
            if error is not None:
                values["error"] = error
            if error_class is not None:
                values["error_class"] = error_class
            if outputs is not None:
                if to_status == "succeeded":
                    validated = descriptor.validate_outputs(outputs)
                    values["outputs"] = _dump_model(validated)
                else:
                    values["outputs"] = _dump_model(outputs)

            conn.execute(
                s.jobs.update().where(s.jobs.c.id == job_id).values(**values)
            )

    def set_progress(
        self,
        job_id: int,
        *,
        current: int | None = None,
        total: int | None = None,
        step: str | None = None,
    ) -> None:
        """Update one or more progress fields without changing status."""
        values: dict[str, Any] = {"updated_at": func.now()}
        if current is not None:
            if current < 0:
                raise InvalidInputError(f"progress_current must be >= 0, got {current}")
            values["progress_current"] = current
        if total is not None:
            if total < 0:
                raise InvalidInputError(f"progress_total must be >= 0, got {total}")
            values["progress_total"] = total
        if step is not None:
            values["current_step"] = step
        if len(values) == 1:  # only updated_at
            return
        with self._tx() as conn:
            conn.execute(
                s.jobs.update().where(s.jobs.c.id == job_id).values(**values)
            )

    # ------------------------------------------------------------------
    # Cancel / retry
    # ------------------------------------------------------------------

    def cancel(self, job_id: int, *, reason: str | None = None) -> None:
        """Cancel a pending or running job. Writes a ``canceled`` event."""
        self.transition(job_id, "canceled", error=reason)
        self.add_event(
            job_id,
            event_type="canceled",
            message=reason or "job canceled",
        )

    def retry(self, job_id: int) -> None:
        """Reset a failed/partial/canceled job back to ``pending``.

        Clears error fields and bumps priority by 10 (capped at 100) so the
        retry gets picked up quickly. Step history is preserved so idempotent
        orchestrators can skip already-succeeded steps.
        """
        with self._tx() as conn:
            row = (
                conn.execute(
                    select(s.jobs.c.status, s.jobs.c.priority).where(s.jobs.c.id == job_id)
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                raise PreconditionError(f"Job {job_id} not found")
            if row["status"] not in ("failed", "partial", "canceled"):
                raise PreconditionError(
                    f"Job {job_id}: retry only allowed from failed/partial/canceled, "
                    f"current status is {row['status']!r}",
                )
            conn.execute(
                s.jobs.update()
                .where(s.jobs.c.id == job_id)
                .values(
                    status="pending",
                    error=None,
                    error_class=None,
                    finished_at=None,
                    claimed_by=None,
                    claimed_at=None,
                    priority=min(100, int(row["priority"]) + 10),
                    updated_at=func.now(),
                )
            )
