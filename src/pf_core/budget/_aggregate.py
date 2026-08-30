"""Scope filtering and usage aggregation over ``llm_runs``.

Re-exported from :mod:`pf_core.budget.repo` and ``pf_core.budget``.
See ``docs/cost-budget.md``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, select

from pf_core.db.types import server_now
from pf_core.exceptions import DataError, InvalidInputError


# ---------------------------------------------------------------------------
# Spent aggregation
# ---------------------------------------------------------------------------


def apply_scope_filter(q, budget: dict):
    """Restrict an ``llm_runs`` query to the runs a budget's scope covers.

    The single definition — snapshot aggregate and live delta must not
    disagree about what a budget counts.

    Raises:
        InvalidInputError: unrecognised ``scope_kind``.
    """
    from pf_core.llm.tracking.schema import (
        llm_agent_types,
        llm_run_tags,
        llm_runs,
    )

    scope_kind = budget["scope_kind"]
    scope_value = budget.get("scope_value")

    if scope_kind == "global":
        return q
    if scope_kind == "agent":
        return q.join(llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id).where(
            llm_agent_types.c.slug == scope_value
        )
    if scope_kind == "job_kind":
        from pf_core.jobs._schema import jobs

        return q.join(jobs, llm_runs.c.job_id == jobs.c.id).where(jobs.c.kind == scope_value)
    if scope_kind == "job_id":
        if scope_value is None:
            raise InvalidInputError("budget scope 'job_id' requires a scope_value")
        return q.where(llm_runs.c.job_id == int(scope_value))
    if scope_kind == "tag":
        return q.join(llm_run_tags, llm_runs.c.id == llm_run_tags.c.llm_run_id).where(
            llm_run_tags.c.tag == scope_value
        )
    raise InvalidInputError(f"unknown budget scope_kind: {scope_kind!r}")


def db_now(conn=None) -> dt.datetime:
    """Read the clock ``llm_runs.created_at`` is stamped from.

    Cutoffs compared against server-stamped rows must come from the same
    clock — a Python-side UTC value skews on MySQL TIMESTAMP columns.
    """
    if conn is not None:
        return _read_now(conn)
    from pf_core.db.connection import transaction

    with transaction() as c:
        return _read_now(c)


def _read_now(conn: Any) -> dt.datetime:
    value = conn.execute(select(server_now())).scalar()
    if value is None:  # pragma: no cover - a scalar clock select always yields a row
        raise DataError("database clock query returned no row")
    return value


def usage_columns() -> tuple[Any, Any, Any]:
    """The three usage aggregates in (usd, tokens, calls) order — one definition
    for the snapshot aggregate and the live delta.

    ``prompt_tokens`` excludes cached input, so the token total adds the cache
    columns back to count what the call actually consumed.
    """
    from pf_core.llm.tracking.schema import llm_runs

    return (
        func.coalesce(func.sum(llm_runs.c.cost_usd), 0),
        func.coalesce(
            func.sum(
                func.coalesce(llm_runs.c.prompt_tokens, 0)
                + func.coalesce(llm_runs.c.completion_tokens, 0)
                + func.coalesce(llm_runs.c.cache_read_tokens, 0)
                + func.coalesce(llm_runs.c.cache_write_tokens, 0)
            ),
            0,
        ),
        func.count(llm_runs.c.id),
    )


def aggregate_usage(
    *,
    budget: dict,
    period_start: dt.date,
    period_end: dt.date,
    cutoff: dt.datetime | None = None,
    conn=None,
) -> dict[str, float | int]:
    """Aggregate the runs matching a budget's scope in [period_start, period_end).

    Returns ``{"usd": float, "tokens": int, "calls": int}`` — cost sum, token
    sum over all four token columns (uncached prompt, completion, cache read,
    cache write), and run count. Excludes rows with
    ``status IN ('cache_hit', 'budget_blocked')``. ``cutoff`` bounds the
    aggregate to ``created_at < cutoff``; store it as the snapshot's
    ``last_updated`` so the live delta resumes exactly there.
    """
    from pf_core.llm.tracking.schema import llm_runs

    def _run(c):
        q = select(*usage_columns()).where(
            and_(
                llm_runs.c.created_at >= period_start,
                llm_runs.c.created_at < period_end,
                llm_runs.c.status.notin_(["cache_hit", "budget_blocked"]),
            )
        )
        if cutoff is not None:
            q = q.where(llm_runs.c.created_at < cutoff)

        usd, tokens, n = c.execute(apply_scope_filter(q, budget)).fetchone()
        return {"usd": float(usd or 0), "tokens": int(tokens or 0), "calls": int(n or 0)}

    if conn is not None:
        return _run(conn)
    from pf_core.db.connection import transaction

    with transaction() as c:
        return _run(c)


def aggregate_spent(
    *,
    budget: dict,
    period_start: dt.date,
    period_end: dt.date,
    cutoff: dt.datetime | None = None,
    conn=None,
) -> tuple[float, int]:
    """Backward-compatible ``(cost_usd sum, run count)`` view of :func:`aggregate_usage`."""
    usage = aggregate_usage(
        budget=budget, period_start=period_start, period_end=period_end, cutoff=cutoff, conn=conn
    )
    return (float(usage["usd"]), int(usage["calls"]))
