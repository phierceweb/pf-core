"""llm_admin queries — jobs list / count / detail."""

from __future__ import annotations

from sqlalchemy import desc, func, select

from pf_core.db.connection import transaction
from pf_core.jobs._schema import job_events, job_steps, jobs
from pf_core.llm.tracking.schema import llm_runs
from pf_core.web.llm_admin._queries_util import _normalize, _normalize_all


def list_jobs(
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    stmt = select(jobs).order_by(desc(jobs.c.created_at)).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(jobs.c.status == status)
    if kind:
        stmt = stmt.where(jobs.c.kind == kind)
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


def count_jobs(*, status: str | None = None, kind: str | None = None) -> int:
    stmt = select(func.count()).select_from(jobs)
    if status:
        stmt = stmt.where(jobs.c.status == status)
    if kind:
        stmt = stmt.where(jobs.c.kind == kind)
    with transaction() as conn:
        return int(conn.execute(stmt).scalar() or 0)


def job_detail(job_id: int) -> dict | None:
    with transaction() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().fetchone()
        if row is None:
            return None
        out = _normalize(row)

        out["steps"] = _normalize_all(
            conn.execute(
                select(job_steps)
                .where(job_steps.c.job_id == job_id)
                .order_by(job_steps.c.step_index)
            ).mappings().fetchall()
        )
        out["events"] = _normalize_all(
            conn.execute(
                select(job_events)
                .where(job_events.c.job_id == job_id)
                .order_by(desc(job_events.c.created_at))
                .limit(200)
            ).mappings().fetchall()
        )
        # Child runs summary
        child_cost = conn.execute(
            select(
                func.count().label("runs"),
                func.coalesce(func.sum(llm_runs.c.cost_usd), 0).label("total_cost"),
            ).where(llm_runs.c.job_id == job_id)
        ).mappings().fetchone()
        out["runs_summary"] = _normalize(child_cost) if child_cost else {}

    return out
