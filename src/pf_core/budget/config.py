"""
Budget configuration loader.

Reads ``BUDGET_CONFIG`` env var (default ``config/budgets.yaml``) and syncs
the defined scopes into ``llm_budgets``. In-process reload TTL controlled by
``BUDGET_CONFIG_RELOAD_SECONDS`` (default 300).

YAML format::

    global:
      daily: 50.00
      monthly: 1000.00
      soft_thresholds: [0.5, 0.8, 0.95]
      action: warn

    agents:
      drafter:
        daily: 20.00
        monthly: 400.00
        action: block
        soft_thresholds: [0.5, 0.8, 0.95]

    job_kinds:
      draft_batch:
        daily: 30.00

    tags:
      "experiment:opus47":
        monthly: 100.00

Usage::

    from pf_core.budget.config import sync_budgets_from_yaml, load_yaml

    sync_budgets_from_yaml()  # reads env var, upserts DB
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

from pf_core.log import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# YAML reader with TTL cache
# ---------------------------------------------------------------------------

_loaded_at: float = 0.0
_raw_config: dict[str, Any] = {}


def _reload_if_stale() -> None:
    global _loaded_at, _raw_config

    reload_ttl = int(os.environ.get("BUDGET_CONFIG_RELOAD_SECONDS", "300"))
    now = time.monotonic()
    if now - _loaded_at < reload_ttl and _raw_config:
        return

    config_path = os.environ.get("BUDGET_CONFIG", "config/budgets.yaml")
    path = Path(config_path)
    if not path.is_absolute():
        path = Path.cwd() / path

    if not path.exists():
        _raw_config = {}
        _loaded_at = now
        return

    try:
        with open(path) as fh:
            _raw_config = yaml.safe_load(fh) or {}
        _loaded_at = now
        logger.debug("budget_config_loaded", path=str(path))
    except Exception as exc:
        logger.warning("budget_config_load_failed", path=str(path), error=str(exc))
        _raw_config = {}
        _loaded_at = now


def load_yaml() -> dict[str, Any]:
    """Return the parsed YAML config (with in-process TTL caching)."""
    _reload_if_stale()
    return dict(_raw_config)


def clear_config_cache() -> None:
    """Reset the in-process config cache (useful for testing)."""
    global _loaded_at, _raw_config
    _loaded_at = 0.0
    _raw_config = {}


# ---------------------------------------------------------------------------
# YAML → DB sync
# ---------------------------------------------------------------------------


def _flatten_scopes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the YAML structure into (scope_kind, scope_value, period, ...) rows."""
    rows: list[dict[str, Any]] = []

    def _emit(kind: str, value: str | None, block: dict[str, Any]) -> None:
        defaults = {
            "soft_thresholds": block.get("soft_thresholds"),
            "action": block.get("action", "block"),
        }
        for period in ("daily", "monthly"):
            if period in block:
                rows.append(
                    {
                        "scope_kind": kind,
                        "scope_value": value,
                        "period": period,
                        "limit_usd": float(block[period]),
                        **defaults,
                    }
                )

    if "global" in raw:
        _emit("global", None, raw["global"] or {})

    for slug, block in (raw.get("agents") or {}).items():
        _emit("agent", str(slug), block or {})

    for kind, block in (raw.get("job_kinds") or {}).items():
        _emit("job_kind", str(kind), block or {})

    for tag, block in (raw.get("tags") or {}).items():
        _emit("tag", str(tag), block or {})

    return rows


def sync_budgets_from_yaml() -> dict[str, int]:
    """Upsert YAML scopes into ``llm_budgets``; disable scopes no longer present.

    Returns a dict of counts: ``{"inserted": N, "updated": N, "disabled": N}``.
    """
    from pf_core.budget.repo import BudgetRepo

    raw = load_yaml()
    desired = _flatten_scopes(raw)

    repo = BudgetRepo()
    counts = repo.sync_from_desired(desired)
    logger.info("budget_config_synced", **counts)
    return counts
