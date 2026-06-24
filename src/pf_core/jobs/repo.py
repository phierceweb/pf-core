"""
Job repositories.

``JobRepo`` is the atomic writer/reader for ``jobs`` and its sidecars
(``job_steps``, ``job_events``). It enforces state transitions via the
kind registry, serializes Pydantic-validated inputs/outputs as JSON, and
handles the distributed-worker claim pattern.

See ``docs/jobs.md`` for the implementation reference.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from pf_core.db.repository import Repository
from pf_core.exceptions import InvalidInputError, PreconditionError
from pf_core.jobs import _schema as s
from pf_core.jobs.registry import TERMINAL_STATES, get_kind
from pf_core.llm.tracking.schema import _server_now_minus_seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_step_creation_lock = threading.Lock()

# Columns on jobs/job_steps/job_events that are stored as naive UTC (SQLite
# and MySQL) or aware UTC (Postgres TIMESTAMPTZ). See ``_coerce_row_utc``
# for why we stamp the naive variants as aware UTC on read.
_JOB_DT_COLS: tuple[str, ...] = (
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "claimed_at",
)


def _default_lease_seconds() -> int:
    try:
        return int(os.environ.get("JOB_LEASE_SECONDS", "300"))
    except (ValueError, TypeError):
        return 300


def _dump_model(value: Any) -> Any:
    """Coerce a Pydantic model to a plain JSON-serializable dict.

    Leaves dicts/lists/primitives untouched. Required because Pydantic
    models don't survive JSON serialization round-trip across all DB
    dialects.
    """
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return value


def _normalize_input_dt(value: datetime | None) -> datetime | None:
    """Normalize a caller-supplied datetime to naive UTC.

    ``JobRepo`` exposes two accepted inputs:

    - **Naive** datetimes — treated as already-UTC per the v0.7.0 contract.
      Returned unchanged.
    - **Aware** datetimes — converted to UTC, then stripped of tzinfo
      before binding so MySQL/SQLite (which compare against naive-UTC
      TIMESTAMP columns with the session pinned to UTC) see the right
      value. Postgres TIMESTAMPTZ handles aware binds natively, but
      stripping is safe there too.

    Returns ``None`` unchanged.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _stamp_utc(value: datetime | None) -> datetime | None:
    """Stamp a naive datetime with ``tzinfo=timezone.utc``.

    Naive values returned from ``JobRepo`` reads are guaranteed to be UTC
    by the schema contract (SQLite's ``CURRENT_TIMESTAMP`` is UTC; MySQL's
    session is pinned to UTC in ``pf_core.db.connection``). Aware values
    from Postgres TIMESTAMPTZ are returned unchanged.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _coerce_row_utc(row: dict | None) -> dict | None:
    """Stamp every known datetime column in a row dict as aware UTC.

    Mutates the dict in place and returns it for chaining. Safe to call
    on rows that lack some of the columns (``get_events`` only has
    ``created_at``, for instance).
    """
    if row is None:
        return None
    for col in _JOB_DT_COLS:
        if col in row:
            row[col] = _stamp_utc(row[col])
    return row


# ---------------------------------------------------------------------------
# JobRepo
# ---------------------------------------------------------------------------


class JobRepo(Repository):
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
    # Worker claim
    # ------------------------------------------------------------------

    def claim_next(
        self,
        *,
        kinds: list[str] | None = None,
        worker_id: str,
        lease_seconds: int | None = None,
    ) -> dict | None:
        """Atomically claim the highest-priority ``pending`` job.

        Uses a two-statement select-then-update pattern that is safe on
        SQLite, MySQL, and Postgres. On contention, one worker wins the
        UPDATE; the other sees zero rows matched and returns ``None`` —
        it should retry.

        Args:
            kinds: Restrict to these kinds. ``None`` means any kind.
            worker_id: Identifier written to ``claimed_by``.
            lease_seconds: Claim lifetime. Defaults to ``JOB_LEASE_SECONDS``
                env var or 300.

        Returns:
            The claimed job row as a dict, or ``None`` if none available.
        """
        lease = lease_seconds if lease_seconds is not None else _default_lease_seconds()
        # Compute the lease cutoff server-side so the comparison stays in
        # the DB's own clock frame. A Python-side aware-UTC cutoff silently
        # skews by the MySQL session offset — see _server_now_minus_seconds.
        cutoff_expr = _server_now_minus_seconds(lease)
        with self._tx() as conn:
            stmt = (
                select(s.jobs.c.id)
                .where(
                    and_(
                        s.jobs.c.status == "pending",
                        or_(
                            s.jobs.c.claimed_by.is_(None),
                            s.jobs.c.claimed_at < cutoff_expr,
                        ),
                    )
                )
                .order_by(s.jobs.c.priority.desc(), s.jobs.c.created_at.asc())
                .limit(1)
            )
            if kinds:
                stmt = stmt.where(s.jobs.c.kind.in_(kinds))
            row = conn.execute(stmt).mappings().fetchone()
            if row is None:
                return None
            job_id = row["id"]

            # Atomic take: this UPDATE returns 0 rows if another worker
            # claimed the same id between our SELECT and UPDATE.
            result = conn.execute(
                update(s.jobs)
                .where(
                    and_(
                        s.jobs.c.id == job_id,
                        s.jobs.c.status == "pending",
                        or_(
                            s.jobs.c.claimed_by.is_(None),
                            s.jobs.c.claimed_at < _server_now_minus_seconds(lease),
                        ),
                    )
                )
                .values(
                    claimed_by=worker_id,
                    claimed_at=func.now(),
                    updated_at=func.now(),
                )
            )
            if result.rowcount == 0:
                return None

            claimed = (
                conn.execute(select(s.jobs).where(s.jobs.c.id == job_id))
                .mappings()
                .fetchone()
            )
        return _coerce_row_utc(dict(claimed)) if claimed else None

    def reclaim_stale(self, *, lease_seconds: int | None = None) -> int:
        """Clear ``claimed_by`` on jobs whose lease expired and still show
        as ``running``. Returns number of rows reset.

        A follow-up worker's ``claim_next`` will pick them back up.
        """
        lease = lease_seconds if lease_seconds is not None else _default_lease_seconds()
        with self._tx() as conn:
            result = conn.execute(
                update(s.jobs)
                .where(
                    and_(
                        s.jobs.c.status == "running",
                        s.jobs.c.claimed_at.is_not(None),
                        s.jobs.c.claimed_at < _server_now_minus_seconds(lease),
                    )
                )
                .values(
                    claimed_by=None,
                    claimed_at=None,
                    status="pending",
                    updated_at=func.now(),
                )
            )
            return int(result.rowcount or 0)

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

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

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
                # Clamp to >=0. Sub-millisecond steps occasionally produce a
                # tiny negative when ``started_at`` (set by the INSERT's
                # statement-start timestamp) and ``server_now`` (this
                # SELECT's statement-start timestamp) are within the same
                # microsecond bucket but the SELECT started slightly before
                # MySQL's clock incremented past the INSERT's stamp. The
                # column is `Integer` (signed, but expected >=0) and
                # writing a negative crashes the UPDATE on some
                # configurations. A clamped 0 is the honest report:
                # "completed in <1 ms".
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

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def purge(
        self,
        *,
        older_than: timedelta,
        status: str | list[str] | None = "succeeded",
    ) -> int:
        """Delete jobs (and their cascaded steps/events) older than a cutoff.

        Args:
            older_than: ``timedelta`` — jobs with ``finished_at`` before
                ``now - older_than`` are eligible.
            status: Restrict to this status (default ``'succeeded'``). Pass
                ``None`` to purge regardless of status. Pass a list to match
                any of multiple statuses. ``llm_runs.job_id`` is set NULL
                via FK — historical LLM-run records survive.

        Returns:
            Number of jobs deleted.
        """
        # Compute the retention cutoff server-side to keep the comparison
        # in a single time-zone frame. See _server_now_minus_seconds.
        cutoff_seconds = int(older_than.total_seconds())
        if cutoff_seconds < 0:
            raise InvalidInputError("older_than must be non-negative")
        conds = [
            s.jobs.c.finished_at.is_not(None),
            s.jobs.c.finished_at < _server_now_minus_seconds(cutoff_seconds),
        ]
        if status is not None:
            if isinstance(status, str):
                conds.append(s.jobs.c.status == status)
            else:
                conds.append(s.jobs.c.status.in_(list(status)))
        with self._tx() as conn:
            result = conn.execute(s.jobs.delete().where(and_(*conds)))
            return int(result.rowcount or 0)
