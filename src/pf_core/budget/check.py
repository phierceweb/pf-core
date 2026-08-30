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
from typing import Any

from pf_core.exceptions import FlowException
from pf_core.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CostBudgetExceeded(FlowException):
    """Raised when a planned call would push a ``block`` scope past its cap.

    A :class:`~pf_core.exceptions.FlowException` — an expected domain
    failure, not a bug; the web layer maps it to 429.

    Attributes:
        scope_kind: ``'global' | 'agent' | 'job_kind' | 'job_id' | 'tag'``
        scope_value: Scope identifier (e.g. agent slug); ``None`` for global
        period: ``'daily' | 'monthly'``
        limit_value: The cap that was exceeded — in the tripping dimension's
            unit (USD for ``dimension='usd'``, tokens/calls otherwise)
        spent_value: Recorded usage before the planned call (same unit)
        projected_value: Projected usage of the planned call (same unit)
        dimension: ``'usd' | 'tokens' | 'calls'`` — which limit tripped
        limit_usd: The scope's USD cap, or ``0.0`` when it sets none. Always
            USD whichever dimension tripped, so a handler can format it as
            currency (likewise ``spent_usd`` / ``projected_usd``).
    """

    def __init__(
        self,
        *,
        scope_kind: str,
        scope_value: str | None,
        period: str,
        limit_value: float,
        spent_value: float,
        projected_value: float,
        dimension: str = "usd",
        usd: tuple[float, float, float] | None = None,
    ) -> None:
        self.scope_kind = scope_kind
        self.scope_value = scope_value
        self.period = period
        self.limit_value = float(limit_value)
        self.spent_value = float(spent_value)
        self.projected_value = float(projected_value)
        self.dimension = dimension
        limit_usd, spent_usd, projected_usd = usd or (
            (limit_value, spent_value, projected_value) if dimension == "usd" else (0.0, 0.0, 0.0)
        )
        self.limit_usd = float(limit_usd)
        self.spent_usd = float(spent_usd)
        self.projected_usd = float(projected_usd)
        descriptor = f"{scope_kind}:{scope_value}" if scope_value else scope_kind

        def _fmt(v: float) -> str:
            return f"{v:.4f}" if dimension == "usd" else f"{v:.0f}"

        super().__init__(
            f"budget exceeded: {descriptor} ({period}) {dimension} "
            f"spent={_fmt(self.spent_value)} + projected={_fmt(self.projected_value)} "
            f"> limit={_fmt(self.limit_value)}"
        )


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _enforcement_disabled() -> bool:
    """BUDGET_ENFORCEMENT_DISABLED kill switch — disables the guard pair."""
    from pf_core.utils.env import resolve_bool

    return resolve_bool(None, "BUDGET_ENFORCEMENT_DISABLED", default=False)


def project_cost(
    *,
    agent_type: str,
    model: str,
    provider: str | None = None,
    estimated_prompt_tokens: int = 1500,
    estimated_completion_tokens: int = 1000,
) -> float:
    """Project the USD cost of a planned call.

    Priced from the ``llm_cost_rates`` row, else the shared
    :mod:`pf_core.pricing` table, else a 24h rolling mean of
    ``llm_runs.cost_usd`` for the (agent_type, model) pair. When nothing can
    price the call the projection is unknown rather than free: it logs
    ``budget_projection_unknown`` and returns ``0.0``, leaving the cap to
    fire on recorded spend alone. Returns ``0.0`` without touching the DB
    when ``BUDGET_ENFORCEMENT_DISABLED`` is set.
    """
    if _enforcement_disabled():
        return 0.0

    from pf_core.budget.repo import CostRateRepo

    rate = CostRateRepo().get_effective(model=model)
    if rate is not None:
        return estimated_prompt_tokens / 1000.0 * float(
            rate["input_per_1k"]
        ) + estimated_completion_tokens / 1000.0 * float(rate["output_per_1k"])

    from pf_core.pricing._resolver import price_call

    listed = price_call(
        provider or "",
        model,
        prompt_tokens=estimated_prompt_tokens,
        completion_tokens=estimated_completion_tokens,
    )
    if listed is not None:
        return listed

    mean = _recent_mean_cost(agent_type=agent_type, model=model)
    if mean is not None:
        return mean

    logger.warning(
        "budget_projection_unknown",
        agent_type=agent_type,
        model=model,
        message=(
            "no cost rate, price list entry, or recent runs — this call is "
            "invisible to the cap; add an llm_cost_rates row or call "
            "pf_core.pricing.register_rates"
        ),
    )
    return 0.0


def _recent_mean_cost(*, agent_type: str, model: str) -> float | None:
    """24h mean cost for the pair, or ``None`` when there is nothing to average."""
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
        return None

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
    if row is None or row[0] is None:
        return None
    return float(row[0])


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


def current_usage(budget: dict, *, conn=None) -> dict[str, float | int]:
    """Return current usage for *budget* = snapshot + live delta since snapshot.

    The one definition of "spent" — the guard, the admin pages, and anything
    else reporting budget state must agree. Returns ``{"usd": float,
    "tokens": int, "calls": int}``, where ``tokens`` sums all four token
    columns — uncached prompt, completion, cache read and cache write.
    """
    from pf_core.budget.repo import BudgetSnapshotRepo, aggregate_usage

    now = dt.datetime.now(dt.timezone.utc)
    period_start = compute_period_start(budget["period"], now)
    period_end = compute_period_end(budget["period"], period_start)

    snap = BudgetSnapshotRepo().get(budget_id=budget["id"], period_start=period_start)
    if snap is None:
        return aggregate_usage(
            budget=budget, period_start=period_start, period_end=period_end, conn=conn
        )

    # Snapshot exists — add runs from last_updated on; inclusive, because the
    # aggregate behind it stopped strictly below that same cutoff.
    from sqlalchemy import and_, select

    from pf_core.budget.repo import apply_scope_filter, usage_columns
    from pf_core.db.connection import transaction
    from pf_core.llm.tracking.schema import llm_runs

    def _delta(c):
        q = select(*usage_columns()).where(
            and_(
                llm_runs.c.created_at >= snap["last_updated"],
                llm_runs.c.created_at < period_end,
                llm_runs.c.status.notin_(["cache_hit", "budget_blocked"]),
            )
        )
        return c.execute(apply_scope_filter(q, budget)).fetchone()

    if conn is not None:
        usd, tokens, calls = _delta(conn)
    else:
        with transaction() as c:
            usd, tokens, calls = _delta(c)
    return {
        "usd": float(snap["spent_usd"]) + float(usd or 0),
        "tokens": int(snap.get("spent_tokens") or 0) + int(tokens or 0),
        "calls": int(snap.get("run_count") or 0) + int(calls or 0),
    }


def current_spent(budget: dict, *, conn=None) -> float:
    """USD-only view of :func:`current_usage`."""
    return float(current_usage(budget, conn=conn)["usd"])


_current_spent = current_spent


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
    # Soft thresholds are USD-only: the fractions anchor to limit_usd.
    thresholds = budget.get("soft_thresholds") or []
    if not thresholds or budget.get("limit_usd") is None:
        return
    limit = float(budget["limit_usd"])
    period_start = compute_period_start(budget["period"])
    for frac in thresholds:
        frac = float(frac)
        cross = limit * frac
        if spent_before < cross <= spent_after:
            key = (int(budget["id"]), str(period_start), frac)
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
# Unarmed-guard reporting
# ---------------------------------------------------------------------------

# job_id is unbounded so it stays out of the dedupe key; tags are caller-supplied
# and equally unbounded, so the set is capped rather than allowed to grow.
_NO_SCOPE_WARN_LIMIT = 512
_NO_SCOPE_WARNED: set[tuple[str | None, str | None, tuple[str, ...]]] = set()


def _clear_no_scope_state() -> None:
    """Testing helper — clears the in-process warned-scope set."""
    _NO_SCOPE_WARNED.clear()


def _log_no_scopes(
    *,
    agent_type: str | None,
    job_kind: str | None,
    job_id: int | None,
    tags: list[str] | None,
) -> None:
    """Report that the guard matched nothing, so an inert cap is visible in logs."""
    key = (agent_type, job_kind, tuple(sorted(tags or ())))
    first = key not in _NO_SCOPE_WARNED and len(_NO_SCOPE_WARNED) < _NO_SCOPE_WARN_LIMIT
    if first:
        _NO_SCOPE_WARNED.add(key)
    (logger.warning if first else logger.debug)(
        "budget_no_scopes_matched",
        agent_type=agent_type,
        job_kind=job_kind,
        job_id=job_id,
        tags=list(tags or []),
        message=(
            "no enabled llm_budgets row matched these scopes — this call is "
            "uncapped; run sync_budgets_from_yaml() if budgets.yaml defines one"
        ),
    )


# ---------------------------------------------------------------------------
# Main guard
# ---------------------------------------------------------------------------


def check_budget(
    *,
    agent_type: str | None = None,
    projected_cost_usd: float,
    projected_tokens: int | None = None,
    projected_calls: int | None = None,
    job_id: int | None = None,
    job_kind: str | None = None,
    tags: list[str] | None = None,
    override: dict[str, Any] | None = None,
) -> None:
    """Pre-call guard. Raises :class:`CostBudgetExceeded` if a block scope is over cap.

    Each budget is enforced on every dimension it configures a limit for —
    USD against ``projected_cost_usd``, tokens against ``projected_tokens``,
    calls against ``projected_calls``. Soft thresholds are USD-only.

    When no enabled scope matches, the call is uncapped and logged as
    ``budget_no_scopes_matched`` rather than passing silently.

    Args:
        agent_type: Agent slug (optional — skips agent scope when None).
        projected_cost_usd: Estimated cost of the planned call.
        projected_tokens: Estimated prompt + completion tokens (None = 0).
        projected_calls: Number of calls being planned. ``None`` counts as 1 —
            the guard fronts exactly one call by definition; pass an explicit
            ``0`` for a pure state read that plans no call.
        job_id: Current job id (checks job_id scope when given).
        job_kind: Current job kind (checks job_kind scope when given).
        tags: Tags attached to the call (checks tag scopes).
        override: When non-empty dict, short-circuits to pass. The caller is
            expected to attach a ``budget:override`` tag + outcome row.

    Raises:
        CostBudgetExceeded: First matching block scope with a dimension whose
            spent + projected exceeds its limit (``exc.dimension`` names it).
            Warn scopes log only.
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
        _log_no_scopes(agent_type=agent_type, job_kind=job_kind, job_id=job_id, tags=tags)
        return

    order = {"global": 0, "agent": 1, "job_kind": 2, "job_id": 3, "tag": 4}
    budgets.sort(key=lambda b: (order.get(b["scope_kind"], 99), b["period"]))

    for budget in budgets:
        usage = current_usage(budget)
        spent = float(usage["usd"])
        _maybe_log_threshold(
            budget=budget, spent_before=spent, spent_after=spent + projected_cost_usd
        )

        dimensions = (
            ("usd", budget.get("limit_usd"), spent, float(projected_cost_usd)),
            ("tokens", budget.get("limit_tokens"), usage["tokens"], projected_tokens or 0),
            (
                "calls",
                budget.get("limit_calls"),
                usage["calls"],
                1 if projected_calls is None else projected_calls,
            ),
        )
        for dimension, raw_limit, dim_spent, dim_projected in dimensions:
            if raw_limit is None:
                continue
            limit = float(raw_limit)
            if dim_spent + dim_projected <= limit:
                continue

            action = budget.get("action", "block")
            if action == "warn":
                logger.warning(
                    "budget_warn_exceeded",
                    scope_kind=budget["scope_kind"],
                    scope_value=budget.get("scope_value"),
                    period=budget["period"],
                    dimension=dimension,
                    spent=round(float(dim_spent), 4),
                    projected=round(float(dim_projected), 4),
                    limit=round(limit, 4),
                )
                continue

            raise CostBudgetExceeded(
                scope_kind=budget["scope_kind"],
                scope_value=budget.get("scope_value"),
                period=budget["period"],
                limit_value=limit,
                spent_value=dim_spent,
                projected_value=dim_projected,
                dimension=dimension,
                usd=(float(budget.get("limit_usd") or 0.0), spent, float(projected_cost_usd)),
            )
