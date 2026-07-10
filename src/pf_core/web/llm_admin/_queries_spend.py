"""llm_admin queries — cost breakdowns, cache efficiency, budget state."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, case, desc, func, select

from pf_core.budget._schema import (
    llm_budget_snapshots,
    llm_budgets,
)
from pf_core.db.connection import transaction
from pf_core.llm.cache._schema import llm_cache_entries
from pf_core.llm.tracking.schema import (
    llm_agent_types,
    llm_models,
    llm_runs,
)
from pf_core.web.llm_admin._queries_util import _normalize, _normalize_all


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
