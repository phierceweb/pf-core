"""
Budget configuration loader.

Reads ``BUDGET_CONFIG`` env var (default ``config/budgets.yaml``) and syncs
the defined scopes into ``llm_budgets``. In-process reload TTL controlled by
``BUDGET_CONFIG_RELOAD_SECONDS`` (default 300).

The YAML format and the read-failure policy are documented in
``docs/cost-budget.md``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from pf_core.exceptions import ConfigurationError
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


_SECTIONS: tuple[str, ...] = ("global", "agents", "job_kinds", "tags")


def _read(key: str) -> dict[str, Any]:
    # Absent file = "no budgets configured"; unreadable = config error, since {}
    # would disable every cap. Driving off open() rather than exists() keeps a
    # permission error, a symlink loop, and a broken symlink out of "absent".
    try:
        with open(key) as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        if Path(key).is_symlink():
            raise ConfigurationError(
                f"budget config {key} is a symlink to a missing target"
            ) from exc
        logger.debug("budget_config_absent", path=key)
        return {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise ConfigurationError(f"failed to read budget config {key}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"budget config {key} must be a mapping at the top level")
    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        logger.warning(
            "budget_config_unknown_sections",
            path=key,
            unknown=unknown,
            known=list(_SECTIONS),
            message=(
                "not a budget scope section — these caps are not enforced; "
                "check for a typo (the plural forms are 'agents', 'job_kinds', 'tags')"
            ),
        )
    logger.debug("budget_config_loaded", path=key)
    return raw


_cache: ReloadCache[str, dict[str, Any]] = ReloadCache(
    _read, ttl=_reload_seconds, stale_on=(ConfigurationError,)
)


def load_yaml() -> dict[str, Any]:
    """Return the parsed YAML config (with in-process TTL caching).

    Raises:
        ConfigurationError: The file exists but is unreadable or malformed,
            and no previously-loaded config is cached to serve instead.
    """
    return dict(_cache.get(str(_config_path())))


def clear_config_cache() -> None:
    """Reset the in-process config cache (useful for testing)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# YAML → DB sync
# ---------------------------------------------------------------------------


def _mapping(where: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"budget config '{where}' must be a mapping, got {type(value).__name__}"
        )
    return value


def _flatten_scopes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the YAML structure into (scope_kind, scope_value, period, ...) rows."""
    rows: list[dict[str, Any]] = []

    def _emit(kind: str, value: str | None, where: str, block: dict[str, Any]) -> None:
        defaults = {
            "soft_thresholds": block.get("soft_thresholds"),
            "action": block.get("action", "block"),
        }
        for period in ("daily", "monthly"):
            if period not in block:
                continue
            try:
                limit = float(block[period])
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"budget config '{where}.{period}' must be a number, "
                    f"got {block[period]!r}"
                ) from exc
            rows.append(
                {
                    "scope_kind": kind,
                    "scope_value": value,
                    "period": period,
                    "limit_usd": limit,
                    **defaults,
                }
            )

    if "global" in raw:
        _emit("global", None, "global", _mapping("global", raw["global"]))

    for section, kind in (("agents", "agent"), ("job_kinds", "job_kind"), ("tags", "tag")):
        for slug, block in _mapping(section, raw.get(section)).items():
            where = f"{section}.{slug}"
            _emit(kind, str(slug), where, _mapping(where, block))

    return rows


def sync_budgets_from_yaml() -> dict[str, int]:
    """Upsert YAML scopes into ``llm_budgets``; disable scopes no longer present.

    Returns a dict of counts: ``{"inserted": N, "updated": N, "disabled": N}``.

    Never raises on a bad config file — an absent one is a no-op, an unreadable
    one logs ``budget_config_unreadable`` at ERROR and leaves every existing row
    enforcing. Database errors from the write still propagate. Call
    :func:`load_yaml` first to fail fast on the config instead.
    """
    from pf_core.budget.repo import BudgetRepo

    try:
        desired = _flatten_scopes(load_yaml())
    except ConfigurationError as exc:
        logger.error(
            "budget_config_unreadable",
            path=str(_config_path()),
            error=str(exc),
            message=(
                "budgets.yaml exists but could not be read — existing llm_budgets "
                "rows left untouched; calls matching no row are uncapped"
            ),
        )
        return {"inserted": 0, "updated": 0, "disabled": 0}

    repo = BudgetRepo()
    counts = repo.sync_from_desired(desired)
    logger.info("budget_config_synced", **counts)
    return counts
