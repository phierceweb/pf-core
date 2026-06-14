"""
Snapshot refresh job.

``refresh_snapshots()`` iterates enabled budgets and recomputes the current
period's ``llm_budget_snapshots`` row from ``llm_runs``. Designed to run on a
60s-ish cron for daily budgets; 5min cron for monthly is acceptable.
"""

from __future__ import annotations

import datetime as dt

from pf_core.budget.check import compute_period_end, compute_period_start
from pf_core.budget.repo import (
    BudgetRepo,
    BudgetSnapshotRepo,
    aggregate_spent,
)
from pf_core.log import get_logger

logger = get_logger(__name__)


def refresh_snapshots(*, period: str | None = None) -> int:
    """Recompute current-period snapshots for all enabled budgets.

    Args:
        period: If given, only refresh budgets matching this period.

    Returns:
        The number of snapshot rows refreshed.
    """
    now = dt.datetime.now(dt.timezone.utc)
    budgets = BudgetRepo().list_enabled()
    snap_repo = BudgetSnapshotRepo()

    n = 0
    for budget in budgets:
        if period is not None and budget["period"] != period:
            continue
        period_start = compute_period_start(budget["period"], now)
        period_end = compute_period_end(budget["period"], period_start)
        spent, count = aggregate_spent(
            budget=budget, period_start=period_start, period_end=period_end
        )
        snap_repo.upsert(
            budget_id=budget["id"],
            period_start=period_start,
            spent_usd=spent,
            run_count=count,
        )
        n += 1
    logger.info("budget_snapshots_refreshed", count=n, period=period or "all")
    return n
