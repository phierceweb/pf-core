"""TTL-cached loader for ``model_router.yaml``.

Internal helper for :mod:`pf_core.llm.router` — owns file location, parse,
schema validation (via :mod:`pf_core.llm._router_schema`), and the
hot-reload cache. Operators edit the YAML and changes land within
``MODEL_ROUTER_RELOAD_SECONDS`` (default 60; ``0`` re-reads every call)
without a restart. A reload that fails to parse keeps serving the last
good config with a warning rather than taking the app down.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from pf_core.exceptions import ConfigurationError
from pf_core.llm._router_schema import validate_router_doc
from pf_core.log import get_logger


logger = get_logger(__name__)

_DEFAULT_CONFIG_PATH = "config/model_router.yaml"
_DEFAULT_RELOAD_SECONDS = 60

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_path: str | None = None
_cache_loaded_at: float = 0.0


def config_path() -> Path:
    return Path(os.environ.get("MODEL_ROUTER_CONFIG", _DEFAULT_CONFIG_PATH))


def _reload_seconds() -> int:
    raw = os.environ.get("MODEL_ROUTER_RELOAD_SECONDS")
    if raw is None:
        return _DEFAULT_RELOAD_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_RELOAD_SECONDS


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"model_router.yaml not found at {path}")

    try:
        import yaml
    except ImportError as e:
        raise ConfigurationError("pyyaml is required to load model_router.yaml") from e

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigurationError(f"failed to parse {path}: {e}") from e

    return validate_router_doc(raw, path)


def load(force: bool = False) -> dict[str, Any]:
    """Validated router document — ``{agents, default_client, env_prefix, non_chat_keys}``."""
    global _cache, _cache_path, _cache_loaded_at

    path = str(config_path())
    ttl = _reload_seconds()
    now = time.monotonic()

    if (
        not force
        and _cache is not None
        and _cache_path == path
        and ttl > 0
        and (now - _cache_loaded_at) < ttl
    ):
        return _cache

    with _lock:
        now = time.monotonic()
        if (
            not force
            and _cache is not None
            and _cache_path == path
            and ttl > 0
            and (now - _cache_loaded_at) < ttl
        ):
            return _cache

        try:
            parsed = _read_yaml(Path(path))
        except ConfigurationError:
            if _cache is not None and _cache_path == path:
                logger.warning(
                    "model_router_reload_failed_keeping_cache", path=path
                )
                _cache_loaded_at = now
                return _cache
            raise

        _cache = parsed
        _cache_path = path
        _cache_loaded_at = now
        return _cache


def clear_cache() -> None:
    """Drop the in-memory cache. For tests and manual reloads."""
    global _cache, _cache_path, _cache_loaded_at
    with _lock:
        _cache = None
        _cache_path = None
        _cache_loaded_at = 0.0
