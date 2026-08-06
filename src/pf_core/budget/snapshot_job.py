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
    db_now,
)
from pf_core.exceptions import InvalidInputError
from pf_core.log import get_logger, log_exception

logger = get_logger(__name__)


def refresh_snapshots(*, period: str | None = None) -> int:
    """Recompute current-period snapshots for all enabled budgets.

    Args:
        period: If given, only refresh budgets matching this period.

    Returns:
        The number of snapshot rows refreshed.
    """
    now = dt.datetime.now(dt.timezone.utc)
    # One cutoff for the pass: rows below it go in the snapshot, the live
    # delta resumes from it. Two clocks here would drop the runs in between.
    cutoff = db_now()
    budgets = BudgetRepo().list_enabled()
    snap_repo = BudgetSnapshotRepo()

    n = 0
    for budget in budgets:
        if period is not None and budget["period"] != period:
            continue
        period_start = compute_period_start(budget["period"], now)
        period_end = compute_period_end(budget["period"], period_start)
        try:
            spent, count = aggregate_spent(
                budget=budget,
                period_start=period_start,
                period_end=period_end,
                cutoff=cutoff,
            )
        except InvalidInputError as exc:
            log_exception(
                exc,
                message_prepend="budget snapshot skipped",
                additional_context={"budget_id": budget["id"]},
                log_level="warning",
            )
            continue
        snap_repo.upsert(
            budget_id=budget["id"],
            period_start=period_start,
            spent_usd=spent,
            run_count=count,
            last_updated=cutoff,
        )
        n += 1
    logger.info("budget_snapshots_refreshed", count=n, period=period or "all")
    return n
