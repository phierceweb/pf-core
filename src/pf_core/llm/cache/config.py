"""
LLM cache configuration loader.

Reads ``CACHE_CONFIG`` env var (default ``config/cache.yaml``) and returns
per-agent policies merged with global defaults. The loaded config is cached
in-process with TTL reload controlled by ``CACHE_CONFIG_RELOAD_SECONDS``
(default 60).

Usage::

    from pf_core.llm.cache.config import get_agent_cache_config, AgentCacheConfig

    cfg: AgentCacheConfig = get_agent_cache_config("classifier")
    if cfg.exact:
        hit = cache_lookup(...)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pf_core.log import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCacheConfig:
    """Per-agent cache policy resolved from cache.yaml."""

    exact: bool = True
    semantic: bool = False
    ttl_seconds: int = 86400
    semantic_threshold: float = 0.93
    semantic_embedding_model: str = ""
    canonicalize: dict[str, bool] = field(default_factory=dict)
    max_entries_per_agent: int = 10_000
    on_miss: str = "proceed"  # 'proceed' | 'warn_log'


_DEFAULTS = AgentCacheConfig()


# ---------------------------------------------------------------------------
# Loader with in-process TTL cache
# ---------------------------------------------------------------------------

_loaded_at: float = 0.0
_raw_config: dict[str, Any] = {}


def _reload_if_stale() -> None:
    global _loaded_at, _raw_config

    reload_ttl = int(os.environ.get("CACHE_CONFIG_RELOAD_SECONDS", "60"))
    now = time.monotonic()
    if now - _loaded_at < reload_ttl and _raw_config:
        return

    config_path = os.environ.get("CACHE_CONFIG", "config/cache.yaml")
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
        logger.debug("cache_config_loaded", path=str(path))
    except Exception as exc:
        logger.warning("cache_config_load_failed", path=str(path), error=str(exc))
        _raw_config = {}
        _loaded_at = now


def _build_config(raw: dict[str, Any]) -> AgentCacheConfig:
    """Build an AgentCacheConfig from a dict, filling missing keys with defaults."""
    return AgentCacheConfig(
        exact=bool(raw.get("exact", _DEFAULTS.exact)),
        semantic=bool(raw.get("semantic", _DEFAULTS.semantic)),
        ttl_seconds=int(raw.get("ttl_seconds", _DEFAULTS.ttl_seconds)),
        semantic_threshold=float(
            raw.get("semantic_threshold", _DEFAULTS.semantic_threshold)
        ),
        semantic_embedding_model=str(
            raw.get("semantic_embedding_model", _DEFAULTS.semantic_embedding_model)
        ),
        canonicalize=dict(raw.get("canonicalize", {})),
        max_entries_per_agent=int(
            raw.get("max_entries_per_agent", _DEFAULTS.max_entries_per_agent)
        ),
        on_miss=str(raw.get("on_miss", _DEFAULTS.on_miss)),
    )


def get_agent_cache_config(agent_type: str) -> AgentCacheConfig:
    """Return resolved cache policy for *agent_type*.

    Merges per-agent overrides (from ``agents.<agent_type>`` in cache.yaml)
    on top of global ``defaults``. Falls back to framework defaults when the
    config file is absent or the agent has no entry.

    Args:
        agent_type: The agent slug (e.g. ``"classifier"``).

    Returns:
        A frozen :class:`AgentCacheConfig` with all fields populated.
    """
    _reload_if_stale()

    global_defaults = _raw_config.get("defaults", {})
    agent_overrides = (_raw_config.get("agents") or {}).get(agent_type, {})

    merged = {**global_defaults, **agent_overrides}
    return _build_config(merged)


def clear_config_cache() -> None:
    """Reset the in-process config cache (useful for testing)."""
    global _loaded_at, _raw_config
    _loaded_at = 0.0
    _raw_config = {}
