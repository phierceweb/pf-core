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
from pathlib import Path
from typing import Any

import yaml

from pf_core.log import get_logger
from pf_core.utils.env import resolve_int
from pf_core.utils.reload_cache import ReloadCache

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# YAML reader with TTL cache
# ---------------------------------------------------------------------------


def _reload_seconds() -> int:
    return resolve_int(None, "BUDGET_CONFIG_RELOAD_SECONDS", default=300)


def _config_path() -> Path:
    path = Path(os.environ.get("BUDGET_CONFIG", "config/budgets.yaml"))
    return path if path.is_absolute() else Path.cwd() / path


def _read(key: str) -> dict[str, Any]:
    # Fail-empty: budget sync treats a missing or broken config as "no scopes".
    path = Path(key)
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        logger.debug("budget_config_loaded", path=key)
        return raw
    except Exception as exc:
        logger.warning("budget_config_load_failed", path=key, error=str(exc))
        return {}


_cache: ReloadCache[str, dict[str, Any]] = ReloadCache(_read, ttl=_reload_seconds)


def load_yaml() -> dict[str, Any]:
    """Return the parsed YAML config (with in-process TTL caching)."""
    return dict(_cache.get(str(_config_path())))


def clear_config_cache() -> None:
    """Reset the in-process config cache (useful for testing)."""
    _cache.clear()


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
