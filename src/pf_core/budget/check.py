"""
Pre-call budget guard.

``check_budget()`` raises :class:`CostBudgetExceeded` when a planned LLM call
would push any ``block``-action scope past its hard cap. Soft threshold
crossings log but do not halt.

Usage::

    from pf_core.budget import check_budget, project_cost, CostBudgetExceeded

    projected = project_cost(
        agent_type="drafter",
        model="claude-opus-4-7",
        estimated_prompt_tokens=1500,
        estimated_completion_tokens=1000,
    )
    try:
        check_budget(agent_type="drafter", projected_cost_usd=projected)
    except CostBudgetExceeded as e:
        # Service decides: skip, fall back to cheaper agent, or requeue
        ...
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any

from pf_core.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CostBudgetExceeded(Exception):
    """Raised when a planned call would push a ``block`` scope past its cap.

    Attributes:
        scope_kind: ``'global' | 'agent' | 'job_kind' | 'job_id' | 'tag'``
        scope_value: Scope identifier (e.g. agent slug); ``None`` for global
        period: ``'daily' | 'monthly'``
        limit_usd: The cap that was exceeded
        spent_usd: Recorded spend before the planned call
        projected_usd: Projected cost of the planned call
    """

    def __init__(
        self,
        *,
        scope_kind: str,
        scope_value: str | None,
        period: str,
        limit_usd: float,
        spent_usd: float,
        projected_usd: float,
    ) -> None:
        self.scope_kind = scope_kind
        self.scope_value = scope_value
        self.period = period
        self.limit_usd = float(limit_usd)
        self.spent_usd = float(spent_usd)
        self.projected_usd = float(projected_usd)
        descriptor = f"{scope_kind}:{scope_value}" if scope_value else scope_kind
        super().__init__(
            f"budget exceeded: {descriptor} ({period}) "
            f"spent={self.spent_usd:.4f} + projected={self.projected_usd:.4f} "
            f"> limit={self.limit_usd:.4f}"
        )


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScopeSummary:
    """Per-scope spent / limit / threshold view used internally."""

    scope_kind: str
    scope_value: str | None
    period: str
    limit_usd: float
    spent_usd: float
    action: str
    soft_thresholds: list[float]
    budget_id: int


def _enforcement_disabled() -> bool:
    """BUDGET_ENFORCEMENT_DISABLED kill switch — disables the guard pair."""
    return os.environ.get("BUDGET_ENFORCEMENT_DISABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


def project_cost(
    *,
    agent_type: str,
    model: str,
    estimated_prompt_tokens: int = 1500,
    estimated_completion_tokens: int = 1000,
) -> float:
    """Project the USD cost of a planned call using ``llm_cost_rates``.

    Falls back to a 24h rolling mean of ``llm_runs.cost_usd`` for the
    (agent_type, model) pair when no cost-rate row exists. Returns ``0.0``
    without touching the DB when ``BUDGET_ENFORCEMENT_DISABLED`` is set.
    """
    if _enforcement_disabled():
        return 0.0

    from pf_core.budget.repo import CostRateRepo

    rate = CostRateRepo().get_effective(model=model)
    if rate is not None:
        return (
            estimated_prompt_tokens / 1000.0 * float(rate["input_per_1k"])
            + estimated_completion_tokens / 1000.0 * float(rate["output_per_1k"])
        )

    return _recent_mean_cost(agent_type=agent_type, model=model)


def _recent_mean_cost(*, agent_type: str, model: str) -> float:
    from sqlalchemy import and_, func, select

    from pf_core.db.connection import transaction
    from pf_core.llm.tracking._resolvers import (
        resolve_agent_type_id,
        resolve_llm_model_id,
    )
    from pf_core.llm.tracking.schema import llm_runs

    try:
        agent_id = resolve_agent_type_id(agent_type)
        model_id = resolve_llm_model_id(model)
    except Exception:
        return 0.0

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    with transaction() as conn:
        row = conn.execute(
            select(func.avg(llm_runs.c.cost_usd)).where(
                and_(
                    llm_runs.c.agent_type_id == agent_id,
                    llm_runs.c.model_id == model_id,
                    llm_runs.c.status == "success",
                    llm_runs.c.created_at >= since,
                )
            )
        ).fetchone()
    return float(row[0] or 0.0)


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def compute_period_start(period: str, now: dt.datetime | None = None) -> dt.date:
    """Return the UTC period start date for ``daily`` or ``monthly``."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if period == "daily":
        return now.date()
    if period == "monthly":
        return now.date().replace(day=1)
    raise ValueError(f"unknown period: {period!r}")


def compute_period_end(period: str, start: dt.date) -> dt.date:
    if period == "daily":
        return start + dt.timedelta(days=1)
    if period == "monthly":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    raise ValueError(f"unknown period: {period!r}")


# ---------------------------------------------------------------------------
# Spent lookup (snapshot + live delta)
# ---------------------------------------------------------------------------


def _current_spent(budget: dict, *, conn=None) -> float:
    """Return current spent for *budget* = snapshot + live delta since snapshot."""
    from pf_core.budget.repo import BudgetSnapshotRepo, aggregate_spent

    now = dt.datetime.now(dt.timezone.utc)
    period_start = compute_period_start(budget["period"], now)
    period_end = compute_period_end(budget["period"], period_start)

    snap = BudgetSnapshotRepo().get(budget_id=budget["id"], period_start=period_start)
    if snap is None:
        spent, _ = aggregate_spent(
            budget=budget, period_start=period_start, period_end=period_end, conn=conn
        )
        return spent

    # Snapshot exists — add in runs recorded after snapshot.last_updated
    from sqlalchemy import and_, func, select

    from pf_core.db.connection import transaction
    from pf_core.llm.tracking.schema import (
        llm_agent_types,
        llm_run_tags,
        llm_runs,
    )

    def _delta(c):
        q = select(func.coalesce(func.sum(llm_runs.c.cost_usd), 0)).where(
            and_(
                llm_runs.c.created_at > snap["last_updated"],
                llm_runs.c.created_at < period_end,
                llm_runs.c.status.notin_(["cache_hit", "budget_blocked"]),
            )
        )
        scope_kind = budget["scope_kind"]
        scope_value = budget.get("scope_value")
        if scope_kind == "agent":
            q = q.join(
                llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
            ).where(llm_agent_types.c.slug == scope_value)
        elif scope_kind == "job_kind":
            from pf_core.jobs._schema import jobs

            q = q.join(jobs, llm_runs.c.job_id == jobs.c.id).where(
                jobs.c.kind == scope_value
            )
        elif scope_kind == "job_id":
            q = q.where(llm_runs.c.job_id == int(scope_value))
        elif scope_kind == "tag":
            q = q.join(
                llm_run_tags, llm_runs.c.id == llm_run_tags.c.llm_run_id
            ).where(llm_run_tags.c.tag == scope_value)
        return float(c.execute(q).scalar() or 0.0)

    if conn is not None:
        delta = _delta(conn)
    else:
        with transaction() as c:
            delta = _delta(c)
    return float(snap["spent_usd"]) + delta


# ---------------------------------------------------------------------------
# Soft threshold crossing dedupe (in-process set)
# ---------------------------------------------------------------------------

_THRESHOLD_FIRED: set[tuple[int, str, float]] = set()


def _maybe_log_threshold(
    *,
    budget: dict,
    spent_before: float,
    spent_after: float,
) -> None:
    thresholds = budget.get("soft_thresholds") or []
    if not thresholds:
        return
    limit = float(budget["limit_usd"])
    for frac in thresholds:
        frac = float(frac)
        cross = limit * frac
        if spent_before < cross <= spent_after:
            key = (int(budget["id"]), str(dt.date.today()), frac)
            if key in _THRESHOLD_FIRED:
                continue
            _THRESHOLD_FIRED.add(key)
            logger.warning(
                "budget_threshold_crossed",
                scope_kind=budget["scope_kind"],
                scope_value=budget.get("scope_value"),
                period=budget["period"],
                threshold=frac,
                spent=round(spent_after, 4),
                limit=round(limit, 4),
            )


def _clear_threshold_state() -> None:
    """Testing helper — clears the in-process fired-threshold set."""
    _THRESHOLD_FIRED.clear()


# ---------------------------------------------------------------------------
# Main guard
# ---------------------------------------------------------------------------


def check_budget(
    *,
    agent_type: str | None = None,
    projected_cost_usd: float,
    job_id: int | None = None,
    job_kind: str | None = None,
    tags: list[str] | None = None,
    override: dict[str, Any] | None = None,
) -> None:
    """Pre-call guard. Raises :class:`CostBudgetExceeded` if a block scope is over cap.

    Args:
        agent_type: Agent slug (optional — skips agent scope when None).
        projected_cost_usd: Estimated cost of the planned call.
        job_id: Current job id (checks job_id scope when given).
        job_kind: Current job kind (checks job_kind scope when given).
        tags: Tags attached to the call (checks tag scopes).
        override: When non-empty dict, short-circuits to pass. The caller is
            expected to attach a ``budget:override`` tag + outcome row.

    Raises:
        CostBudgetExceeded: First matching block scope whose spent + projected
            exceeds the limit. Warn scopes log only.
    """
    if override:
        logger.info(
            "budget_override_invoked",
            agent_type=agent_type,
            reason=override.get("reason"),
            operator=override.get("operator"),
        )
        return

    if _enforcement_disabled():
        logger.debug("budget_enforcement_disabled")
        return

    from pf_core.budget.repo import BudgetRepo

    budgets = BudgetRepo().list_for_scopes(
        agent_type=agent_type, job_kind=job_kind, job_id=job_id, tags=tags
    )
    if not budgets:
        return

    # Check in order: global → agent → job_kind → job_id → tag
    order = {"global": 0, "agent": 1, "job_kind": 2, "job_id": 3, "tag": 4}
    budgets.sort(key=lambda b: (order.get(b["scope_kind"], 99), b["period"]))

    for budget in budgets:
        spent = _current_spent(budget)
        after = spent + projected_cost_usd
        _maybe_log_threshold(budget=budget, spent_before=spent, spent_after=after)

        limit = float(budget["limit_usd"])
        if after <= limit:
            continue

        action = budget.get("action", "block")
        if action == "warn":
            logger.warning(
                "budget_warn_exceeded",
                scope_kind=budget["scope_kind"],
                scope_value=budget.get("scope_value"),
                period=budget["period"],
                spent=round(spent, 4),
                projected=round(projected_cost_usd, 4),
                limit=round(limit, 4),
            )
            continue

        # action == 'block' — raise immediately; first failing block wins
        raise CostBudgetExceeded(
            scope_kind=budget["scope_kind"],
            scope_value=budget.get("scope_value"),
            period=budget["period"],
            limit_usd=limit,
            spent_usd=spent,
            projected_usd=projected_cost_usd,
        )
