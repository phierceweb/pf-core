"""llm_admin queries — dashboard KPIs and the runs list / count / detail."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, case, desc, func, select

from pf_core.db.connection import transaction
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
from pf_core.web.llm_admin._queries_util import _normalize, _normalize_all


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
