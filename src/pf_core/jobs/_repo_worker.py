"""Worker-operations mixin for ``JobRepo`` — claim, lease reclaim, retention.

The distributed-worker half of the job repository: the portable
select-then-update claim, stale-lease recovery, and the purge that keeps
the table bounded. Composed into :class:`pf_core.jobs.repo.JobRepo`.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update

from pf_core.exceptions import InvalidInputError
from pf_core.jobs import _schema as s
from pf_core.jobs._repo_util import _coerce_row_utc, _default_lease_seconds
from pf_core.llm.tracking.schema import _server_now_minus_seconds


class WorkerOpsMixin:
    """Claim / reclaim / purge operations. Requires ``self._tx``."""

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
