"""Background snapshot refresh loop for ``pf_core.budget``.

A daemon thread that calls :func:`pf_core.budget.refresh_snapshots` on an
interval. Designed for long-running consumer processes (FastAPI app boot,
worker daemons) so ``check_budget()`` reads recent snapshot data instead of
stale rows.

Short-lived CLI commands should NOT start this loop — they rely on whatever
the last cached snapshot was. Starting a daemon thread inside a one-shot
command is wasted work and adds shutdown noise.

Usage::

    from pf_core.budget import start_budget_refresh_loop

    # Inside FastAPI startup:
    start_budget_refresh_loop()                     # default 60s cadence
    start_budget_refresh_loop(interval_seconds=30)  # custom cadence

The loop is idempotent — only the first :func:`start_budget_refresh_loop`
call wins. Subsequent calls in the same process are no-ops.

Refresh failures are logged at WARNING and swallowed; the loop continues
running so that a transient DB hiccup does not silently leave snapshots
stale forever.
"""

from __future__ import annotations

import threading

from pf_core.budget.snapshot_job import refresh_snapshots
from pf_core.log import get_logger

logger = get_logger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60
_started = False
_lock = threading.Lock()


def _tick(interval_seconds: int) -> None:
    """One iteration of the refresh loop. Reschedules itself on completion."""
    try:
        refresh_snapshots()
    except Exception as exc:
        # Snapshot refresh failure must not kill the loop — budgets remain
        # readable from the previous snapshot row.
        logger.warning("budget_snapshot_refresh_failed", error=str(exc))
    finally:
        timer = threading.Timer(interval_seconds, _tick, args=(interval_seconds,))
        timer.daemon = True
        timer.start()


def start_budget_refresh_loop(interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> None:
    """Start the snapshot refresh loop. Idempotent — only the first call wins.

    Args:
        interval_seconds: Seconds between refresh ticks. Default 60s — fine
            for daily budgets; consumers with monthly-only budgets can pass a
            larger value (e.g. 300).
    """
    global _started
    with _lock:
        if _started:
            return
        _started = True
    timer = threading.Timer(interval_seconds, _tick, args=(interval_seconds,))
    timer.daemon = True
    timer.start()
    logger.info("budget_scheduler_started", interval_seconds=interval_seconds)
