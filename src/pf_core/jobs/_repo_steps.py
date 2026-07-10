"""Step and event mixin for ``JobRepo`` — per-job activity records.

Step history (with auto-assigned indices and duration computation) and
the diagnostic event log. Composed into :class:`pf_core.jobs.repo.JobRepo`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select

from pf_core.exceptions import InvalidInputError, PreconditionError
from pf_core.jobs import _schema as s
from pf_core.jobs._repo_util import (
    _coerce_row_utc,
    _dump_model,
    _step_creation_lock,
)


class StepEventsMixin:
    """Step + event records. Requires ``self._tx``."""

    def find_step(self, job_id: int, *, name: str) -> dict | None:
        """Return the most recent step matching ``name`` for ``job_id``.

        Datetime columns are returned as aware UTC.
        """
        with self._tx() as conn:
            row = (
                conn.execute(
                    select(s.job_steps)
                    .where(
                        and_(
                            s.job_steps.c.job_id == job_id,
                            s.job_steps.c.name == name,
                        )
                    )
                    .order_by(s.job_steps.c.step_index.desc())
                    .limit(1)
                )
                .mappings()
                .fetchone()
            )
        return _coerce_row_utc(dict(row)) if row else None

    def start_step(
        self,
        job_id: int,
        *,
        name: str,
        inputs: Any | None = None,
    ) -> int:
        """Insert a new ``job_steps`` row in ``running`` state. Returns id.

        ``step_index`` is auto-assigned to ``max(existing)+1`` so callers
        don't coordinate indices.

        Holds a process-level lock around the SELECT MAX / INSERT pair so
        parallel workers can't race to claim the same step_index.
        """
        with _step_creation_lock:
            with self._tx() as conn:
                # coalesce handles the "no prior steps" case; the `or` trick
                # would incorrectly overwrite a legitimate 0.
                max_idx = conn.execute(
                    select(func.coalesce(func.max(s.job_steps.c.step_index), -1))
                    .where(s.job_steps.c.job_id == job_id)
                ).scalar()
                if max_idx is None:
                    max_idx = -1
                result = conn.execute(
                    s.job_steps.insert().values(
                        job_id=job_id,
                        step_index=int(max_idx) + 1,
                        name=name,
                        status="running",
                        inputs=_dump_model(inputs),
                    )
                )
                return int(result.inserted_primary_key[0])

    def finish_step(
        self,
        step_id: int,
        *,
        status: str = "succeeded",
        outputs: Any | None = None,
        error: str | None = None,
    ) -> None:
        """Mark a step terminal. Computes ``duration_ms`` from ``started_at``.

        When the parent job's kind was registered with
        ``auto_track_progress=True``, atomically increments
        ``jobs.progress_current`` by 1 for ``succeeded`` and ``failed``
        transitions in the same transaction. ``skipped`` steps do not
        count — they represent resumed work that was already tallied.
        """
        if status not in ("succeeded", "failed", "skipped"):
            raise InvalidInputError(
                f"step status must be succeeded/failed/skipped, got {status!r}",
            )
        with self._tx() as conn:
            # Read started_at, the server's current time, and the parent
            # job's kind in the same statement. started_at + server_now
            # share a time-zone frame (see duration_ms note below); kind
            # drives the auto_track_progress check.
            row = (
                conn.execute(
                    select(
                        s.job_steps.c.started_at,
                        s.job_steps.c.job_id,
                        s.jobs.c.kind,
                        func.now().label("server_now"),
                    )
                    .select_from(
                        s.job_steps.join(
                            s.jobs, s.jobs.c.id == s.job_steps.c.job_id
                        )
                    )
                    .where(s.job_steps.c.id == step_id)
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                raise PreconditionError(f"job_step {step_id} not found")
            started = row["started_at"]
            server_now = row["server_now"]
            duration_ms: int | None = None
            if started is not None and server_now is not None:
                # Clamp to >=0: the INSERT's and this SELECT's statement
                # timestamps can share a microsecond bucket, yielding a tiny
                # negative that some configs reject. 0 = "completed in <1 ms".
                duration_ms = max(0, int((server_now - started).total_seconds() * 1000))
            conn.execute(
                s.job_steps.update()
                .where(s.job_steps.c.id == step_id)
                .values(
                    status=status,
                    finished_at=func.now(),
                    duration_ms=duration_ms,
                    outputs=_dump_model(outputs),
                    error=error,
                )
            )

            # Auto-track progress on succeeded/failed if the kind opted in.
            # `UPDATE ... SET progress_current = progress_current + 1` is
            # atomic at the row level under DB locking, so concurrent
            # workers each contribute +1 without lost updates.
            if status in ("succeeded", "failed"):
                from pf_core.jobs.registry import _REGISTRY

                kind_descriptor = _REGISTRY.get(row["kind"])
                if kind_descriptor is not None and kind_descriptor.auto_track_progress:
                    conn.execute(
                        s.jobs.update()
                        .where(s.jobs.c.id == row["job_id"])
                        .values(
                            progress_current=s.jobs.c.progress_current + 1,
                        )
                    )

    def add_event(
        self,
        job_id: int,
        *,
        event_type: str,
        message: str,
        context: dict | None = None,
    ) -> int:
        """Insert a diagnostic row into ``job_events``."""
        with self._tx() as conn:
            result = conn.execute(
                s.job_events.insert().values(
                    job_id=job_id,
                    event_type=event_type,
                    message=message,
                    context=context,
                )
            )
            return int(result.inserted_primary_key[0])

    def get_events(
        self,
        job_id: int,
        *,
        event_type: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Return events for a job, oldest-first.

        ``created_at`` is returned as aware UTC.
        """
        stmt = select(s.job_events).where(s.job_events.c.job_id == job_id)
        if event_type is not None:
            stmt = stmt.where(s.job_events.c.event_type == event_type)
        stmt = stmt.order_by(s.job_events.c.created_at).limit(limit)
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [_coerce_row_utc(dict(r)) for r in rows]
