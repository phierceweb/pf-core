"""
Reusable SQL queries for the LLM admin sub-app.

All queries use SQLAlchemy Core for dialect portability (SQLite, MySQL,
Postgres). Each function takes a date/time window where relevant and
returns plain ``list[dict]`` or ``dict`` suitable for template / JSON.

Covers:
- Dashboard KPIs (last 24h and last 7d)
- Runs list + filters + detail
- Cost by model / by agent
- Jobs list + detail
- Cache hit rate + top entries
- Budget state + history
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, desc, func, select

from pf_core.db.connection import transaction
from pf_core.jobs._schema import job_events, job_steps, jobs
from pf_core.budget._schema import (
    llm_budget_snapshots,
    llm_budgets,
)
from pf_core.llm.cache._schema import llm_cache_entries
from pf_core.llm.tracking.schema import (
    llm_agent_types,
    llm_models,
    llm_run_configs,
    llm_run_links,
    llm_run_metrics,
    llm_run_outcomes,
    llm_run_payloads,
    llm_run_tags,
    llm_run_validations,
    llm_runs,
)


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def default_window(days: int = 7) -> tuple[dt.datetime, dt.datetime]:
    """Return ``(since, until)`` for the default window."""
    now = dt.datetime.now(dt.timezone.utc)
    return now - dt.timedelta(days=days), now


def parse_window(
    since: str | None, until: str | None, *, default_days: int = 7
) -> tuple[dt.datetime, dt.datetime]:
    """Parse ISO strings into a ``(since, until)`` datetime pair."""
    default_since, default_until = default_window(default_days)
    s = dt.datetime.fromisoformat(since) if since else default_since
    u = dt.datetime.fromisoformat(until) if until else default_until
    return s, u


def _normalize(row: Any) -> dict:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _normalize_all(rows) -> list[dict]:
    return [_normalize(r) for r in rows]


# ---------------------------------------------------------------------------
# Dashboard KPIs
# ---------------------------------------------------------------------------


def dashboard_kpis(
    *, since: dt.datetime, until: dt.datetime
) -> dict:
    """Return KPI summary for the window: total runs, cost, error rate, cache rate."""
    error_case = case((llm_runs.c.status == "success", 0.0), else_=1.0)
    cache_case = case((llm_runs.c.status == "cache_hit", 1.0), else_=0.0)
    stmt = select(
        func.count().label("total_runs"),
        func.coalesce(func.sum(llm_runs.c.cost_usd), 0).label("total_cost"),
        func.coalesce(func.avg(error_case), 0).label("error_rate"),
        func.coalesce(func.avg(cache_case), 0).label("cache_hit_rate"),
    ).where(
        and_(llm_runs.c.created_at >= since, llm_runs.c.created_at < until)
    )
    with transaction() as conn:
        row = conn.execute(stmt).mappings().fetchone()
    return _normalize(row)


def top_agents_by_cost(
    *, since: dt.datetime, until: dt.datetime, limit: int = 5
) -> list[dict]:
    stmt = (
        select(
            llm_agent_types.c.slug.label("agent_type"),
            func.count().label("runs"),
            func.coalesce(func.sum(llm_runs.c.cost_usd), 0).label("total_cost"),
            func.coalesce(func.avg(llm_runs.c.cost_usd), 0).label("avg_cost"),
        )
        .select_from(
            llm_runs.join(
                llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
            )
        )
        .where(and_(llm_runs.c.created_at >= since, llm_runs.c.created_at < until))
        .group_by(llm_agent_types.c.slug)
        .order_by(func.coalesce(func.sum(llm_runs.c.cost_usd), 0).desc())
        .limit(limit)
    )
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


# ---------------------------------------------------------------------------
# Runs list + detail
# ---------------------------------------------------------------------------


def list_runs(
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    status: str | None = None,
    job_id: int | None = None,
    min_cost: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    stmt = (
        select(
            llm_runs.c.id,
            llm_runs.c.created_at,
            llm_runs.c.status,
            llm_runs.c.cost_usd,
            llm_runs.c.prompt_tokens,
            llm_runs.c.completion_tokens,
            llm_runs.c.duration_ms,
            llm_runs.c.items_out,
            llm_runs.c.job_id,
            llm_agent_types.c.slug.label("agent_type"),
            llm_models.c.name.label("model"),
        )
        .select_from(
            llm_runs.join(
                llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
            ).join(llm_models, llm_runs.c.model_id == llm_models.c.id)
        )
        .order_by(desc(llm_runs.c.created_at))
        .limit(limit)
        .offset(offset)
    )
    if since is not None:
        stmt = stmt.where(llm_runs.c.created_at >= since)
    if until is not None:
        stmt = stmt.where(llm_runs.c.created_at < until)
    if agent_type:
        stmt = stmt.where(llm_agent_types.c.slug == agent_type)
    if model:
        stmt = stmt.where(llm_models.c.name == model)
    if status:
        stmt = stmt.where(llm_runs.c.status == status)
    if job_id is not None:
        stmt = stmt.where(llm_runs.c.job_id == job_id)
    if min_cost is not None:
        stmt = stmt.where(llm_runs.c.cost_usd >= min_cost)

    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


def count_runs(
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    status: str | None = None,
    job_id: int | None = None,
    min_cost: float | None = None,
) -> int:
    stmt = (
        select(func.count())
        .select_from(
            llm_runs.join(
                llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
            ).join(llm_models, llm_runs.c.model_id == llm_models.c.id)
        )
    )
    if since is not None:
        stmt = stmt.where(llm_runs.c.created_at >= since)
    if until is not None:
        stmt = stmt.where(llm_runs.c.created_at < until)
    if agent_type:
        stmt = stmt.where(llm_agent_types.c.slug == agent_type)
    if model:
        stmt = stmt.where(llm_models.c.name == model)
    if status:
        stmt = stmt.where(llm_runs.c.status == status)
    if job_id is not None:
        stmt = stmt.where(llm_runs.c.job_id == job_id)
    if min_cost is not None:
        stmt = stmt.where(llm_runs.c.cost_usd >= min_cost)
    with transaction() as conn:
        return int(conn.execute(stmt).scalar() or 0)


def run_detail(run_id: int) -> dict | None:
    """Return all data about a single run: core row + payload + sidecars."""
    with transaction() as conn:
        row = conn.execute(
            select(
                llm_runs,
                llm_agent_types.c.slug.label("agent_type"),
                llm_models.c.name.label("model"),
            )
            .select_from(
                llm_runs.join(
                    llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
                ).join(llm_models, llm_runs.c.model_id == llm_models.c.id)
            )
            .where(llm_runs.c.id == run_id)
        ).mappings().fetchone()
        if row is None:
            return None
        out = _normalize(row)

        # Payload
        payload = conn.execute(
            select(llm_run_payloads).where(llm_run_payloads.c.llm_run_id == run_id)
        ).mappings().fetchone()
        out["payload"] = _normalize(payload) if payload else None

        # Configs
        out["configs"] = _normalize_all(
            conn.execute(
                select(llm_run_configs).where(llm_run_configs.c.llm_run_id == run_id)
            ).mappings().fetchall()
        )

        # Validations
        out["validations"] = _normalize_all(
            conn.execute(
                select(llm_run_validations).where(
                    llm_run_validations.c.llm_run_id == run_id
                )
            ).mappings().fetchall()
        )

        # Outcomes
        out["outcomes"] = _normalize_all(
            conn.execute(
                select(llm_run_outcomes).where(
                    llm_run_outcomes.c.llm_run_id == run_id
                )
            ).mappings().fetchall()
        )

        # Tags
        out["tags"] = [
            r.tag
            for r in conn.execute(
                select(llm_run_tags.c.tag).where(llm_run_tags.c.llm_run_id == run_id)
            ).fetchall()
        ]

        # Metrics
        out["metrics"] = _normalize_all(
            conn.execute(
                select(llm_run_metrics).where(
                    llm_run_metrics.c.llm_run_id == run_id
                )
            ).mappings().fetchall()
        )

        # Links — both directions
        out["links_out"] = _normalize_all(
            conn.execute(
                select(llm_run_links).where(llm_run_links.c.parent_run_id == run_id)
            ).mappings().fetchall()
        )
        out["links_in"] = _normalize_all(
            conn.execute(
                select(llm_run_links).where(llm_run_links.c.child_run_id == run_id)
            ).mappings().fetchall()
        )

    return out


# ---------------------------------------------------------------------------
# Cost pages
# ---------------------------------------------------------------------------


def cost_by_model(*, since: dt.datetime, until: dt.datetime) -> list[dict]:
    stmt = (
        select(
            llm_models.c.name.label("model"),
            func.count().label("runs"),
            func.coalesce(func.sum(llm_runs.c.cost_usd), 0).label("total_cost"),
            func.coalesce(func.avg(llm_runs.c.cost_usd), 0).label("avg_cost"),
            func.coalesce(func.sum(llm_runs.c.prompt_tokens), 0).label(
                "prompt_tokens"
            ),
            func.coalesce(func.sum(llm_runs.c.completion_tokens), 0).label(
                "completion_tokens"
            ),
        )
        .select_from(
            llm_runs.join(llm_models, llm_runs.c.model_id == llm_models.c.id)
        )
        .where(and_(llm_runs.c.created_at >= since, llm_runs.c.created_at < until))
        .group_by(llm_models.c.name)
        .order_by(func.coalesce(func.sum(llm_runs.c.cost_usd), 0).desc())
    )
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


def cost_by_agent(*, since: dt.datetime, until: dt.datetime) -> list[dict]:
    stmt = (
        select(
            llm_agent_types.c.slug.label("agent_type"),
            func.count().label("runs"),
            func.coalesce(func.sum(llm_runs.c.cost_usd), 0).label("total_cost"),
            func.coalesce(func.avg(llm_runs.c.cost_usd), 0).label("avg_cost"),
        )
        .select_from(
            llm_runs.join(
                llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
            )
        )
        .where(and_(llm_runs.c.created_at >= since, llm_runs.c.created_at < until))
        .group_by(llm_agent_types.c.slug)
        .order_by(func.coalesce(func.sum(llm_runs.c.cost_usd), 0).desc())
    )
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def cache_hit_rate_by_agent(
    *, since: dt.datetime, until: dt.datetime
) -> list[dict]:
    cache_case = case((llm_runs.c.status == "cache_hit", 1.0), else_=0.0)
    stmt = (
        select(
            llm_agent_types.c.slug.label("agent_type"),
            func.count().label("total_runs"),
            func.coalesce(func.avg(cache_case), 0).label("hit_rate"),
            func.coalesce(func.sum(llm_runs.c.cost_usd), 0).label("total_cost"),
        )
        .select_from(
            llm_runs.join(
                llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
            )
        )
        .where(and_(llm_runs.c.created_at >= since, llm_runs.c.created_at < until))
        .group_by(llm_agent_types.c.slug)
        .order_by(func.count().desc())
    )
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


def top_cache_entries(*, limit: int = 50) -> list[dict]:
    stmt = (
        select(
            llm_cache_entries.c.id,
            llm_cache_entries.c.hit_count,
            llm_cache_entries.c.last_hit_at,
            llm_cache_entries.c.created_at,
            llm_cache_entries.c.source_run_id,
            llm_agent_types.c.slug.label("agent_type"),
            llm_models.c.name.label("model"),
        )
        .select_from(
            llm_cache_entries.join(
                llm_agent_types,
                llm_cache_entries.c.agent_type_id == llm_agent_types.c.id,
            ).join(llm_models, llm_cache_entries.c.model_id == llm_models.c.id)
        )
        .order_by(desc(llm_cache_entries.c.hit_count))
        .limit(limit)
    )
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def list_budgets_with_spend() -> list[dict]:
    """Return every enabled budget with current-period spent + pct-of-limit."""
    today = dt.date.today()
    month_start = today.replace(day=1)

    with transaction() as conn:
        budgets = conn.execute(
            select(llm_budgets).where(llm_budgets.c.enabled.is_(True))
        ).mappings().fetchall()

        out = []
        for b in budgets:
            period_start = today if b["period"] == "daily" else month_start
            snap = conn.execute(
                select(llm_budget_snapshots).where(
                    and_(
                        llm_budget_snapshots.c.budget_id == b["id"],
                        llm_budget_snapshots.c.period_start == period_start,
                    )
                )
            ).mappings().fetchone()
            spent = float(snap["spent_usd"]) if snap else 0.0
            run_count = int(snap["run_count"]) if snap else 0
            limit = float(b["limit_usd"])
            pct = (spent / limit) if limit > 0 else 0.0
            row = _normalize(b)
            row["spent_usd"] = spent
            row["run_count"] = run_count
            row["pct_of_limit"] = pct
            row["period_start"] = period_start
            out.append(row)

    out.sort(key=lambda r: r["pct_of_limit"], reverse=True)
    return out


def blocked_runs_24h() -> int:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    stmt = select(func.count()).where(
        and_(
            llm_runs.c.status == "budget_blocked",
            llm_runs.c.created_at >= since,
        )
    )
    with transaction() as conn:
        return int(conn.execute(stmt).scalar() or 0)
