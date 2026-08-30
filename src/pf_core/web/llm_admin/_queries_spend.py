"""llm_admin queries — cost breakdowns, cache efficiency, budget state."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, case, desc, func, select

from pf_core.budget._schema import llm_budgets
from pf_core.budget.check import (
    compute_period_start,
    current_usage,
)
from pf_core.db.connection import transaction
from pf_core.llm.cache import cache_stats as _cache_stats
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
            func.coalesce(func.sum(llm_runs.c.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(llm_runs.c.completion_tokens), 0).label("completion_tokens"),
        )
        .select_from(llm_runs.join(llm_models, llm_runs.c.model_id == llm_models.c.id))
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
            llm_runs.join(llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id)
        )
        .where(and_(llm_runs.c.created_at >= since, llm_runs.c.created_at < until))
        .group_by(llm_agent_types.c.slug)
        .order_by(func.coalesce(func.sum(llm_runs.c.cost_usd), 0).desc())
    )
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


def cache_hit_rate_by_agent(*, since: dt.datetime, until: dt.datetime) -> list[dict]:
    cache_case = case((llm_runs.c.status == "cache_hit", 1.0), else_=0.0)
    stmt = (
        select(
            llm_agent_types.c.slug.label("agent_type"),
            func.count().label("total_runs"),
            func.coalesce(func.avg(cache_case), 0).label("hit_rate"),
            func.coalesce(func.sum(llm_runs.c.cost_usd), 0).label("total_cost"),
        )
        .select_from(
            llm_runs.join(llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id)
        )
        .where(and_(llm_runs.c.created_at >= since, llm_runs.c.created_at < until))
        .group_by(llm_agent_types.c.slug)
        .order_by(func.count().desc())
    )
    with transaction() as conn:
        return _normalize_all(conn.execute(stmt).mappings().fetchall())


def cache_stats(*, agent_type: str | None = None, since=None) -> list[dict]:
    return _cache_stats(agent_type=agent_type, since=since)


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
    """Return every enabled budget with current-period usage + pct per dimension.

    Usage is the guard's own figure (``current_usage``) over the guard's own
    UTC period, so the page cannot read $0 while calls are being blocked.
    Each configured dimension gets a ``pct_usd`` / ``pct_tokens`` /
    ``pct_calls`` (``None`` when unconfigured); ``pct_of_limit`` — the sort
    key — is the max across configured dimensions.
    """
    with transaction() as conn:
        budgets = (
            conn.execute(select(llm_budgets).where(llm_budgets.c.enabled.is_(True)))
            .mappings()
            .fetchall()
        )

    out = []
    for b in budgets:
        row = _normalize(b)
        usage = current_usage(row)
        row["spent_usd"] = float(usage["usd"])
        row["spent_tokens"] = int(usage["tokens"])
        row["run_count"] = int(usage["calls"])
        pcts = []
        for dim, limit_key, spent in (
            ("usd", "limit_usd", row["spent_usd"]),
            ("tokens", "limit_tokens", row["spent_tokens"]),
            ("calls", "limit_calls", row["run_count"]),
        ):
            limit = row.get(limit_key)
            if limit is None:
                row[f"pct_{dim}"] = None
                continue
            # A zero limit admits nothing, so any usage against it is 100%.
            pct = (spent / float(limit)) if float(limit) > 0 else (1.0 if spent > 0 else 0.0)
            row[f"pct_{dim}"] = pct
            pcts.append(pct)
        row["pct_of_limit"] = max(pcts, default=0.0)
        row["pct_dimension"] = max(
            (d for d in ("usd", "tokens", "calls") if row.get(f"pct_{d}") is not None),
            key=lambda d: row[f"pct_{d}"],
            default="",
        )
        row["period_start"] = compute_period_start(row["period"])
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
