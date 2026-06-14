"""
Centralized configuration loader.

Resolution order (highest priority wins):
    1. Explicit overrides passed to AppConfig()
    2. Environment variables (loaded from .env via python-dotenv)
    3. YAML config file (project-level domain config)
    4. Defaults

Usage in a project::

    from pf_core.config import AppConfig

    class MyConfig(AppConfig):
        # Declare project-specific settings with defaults
        SITE_NAME: str = "My App"
        CACHE_TTL_SECONDS: int = 300

    cfg = MyConfig(
        env_file=Path(".env"),
        yaml_file=Path("myapp.yaml"),
    )

    # Access settings as attributes
    cfg.DATABASE_URL        # from env or default
    cfg.SITE_NAME           # from env, yaml, or default
    cfg.yaml                # raw dict from yaml file
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _to_bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def _to_list(val: str, sep: str = ",") -> list[str]:
    return [s.strip() for s in val.split(sep) if s.strip()]


class AppConfig:
    """Base configuration class. Subclass in each project to add domain settings.

    Built-in settings (all overridable via env vars):
        DATABASE_URL, OPENROUTER_API_KEY, OPENROUTER_BASE_URL,
        OPENROUTER_PROVIDER_IGNORE, REDIS_URL, WEB_HOST, WEB_PORT,
        CORS_ORIGINS, LOG_LEVEL, LOG_FILE, APP_NAME, APP_URL
    """

    # --- Built-in defaults (env var name = attribute name) ---
    DATABASE_URL: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_PROVIDER_IGNORE: list[str] = []
    REDIS_URL: str = ""
    WEB_HOST: str = "127.0.0.1"
    WEB_PORT: int = 8000
    CORS_ORIGINS: list[str] = []
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = ""
    APP_NAME: str = "App"
    APP_URL: str = ""
    REQUEST_TIMEOUT: int = 120
    THREAD_MAX_WORKERS: int = 4
    API_RATE_LIMIT_PER_MINUTE: int = 60
    MAX_PER_PAGE: int = 200
    ID_LENGTH: int = 12

    # Populated by yaml_file loading
    yaml: dict[str, Any]

    def __init__(
        self,
        *,
        env_file: Path | str | None = None,
        yaml_file: Path | str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        # 1. Load .env
        if env_file:
            load_dotenv(Path(env_file))

        # 2. Load YAML
        self.yaml = {}
        if yaml_file:
            yp = Path(yaml_file)
            if yp.exists():
                try:
                    import yaml

                    with open(yp, encoding="utf-8") as f:
                        self.yaml = yaml.safe_load(f) or {}
                except Exception:
                    import warnings
                    warnings.warn(f"Failed to load {yp}", stacklevel=2)

        # 3. Resolve each declared setting
        self._resolve_all()

        # 4. Apply explicit overrides last
        if overrides:
            for k, v in overrides.items():
                setattr(self, k, v)

    def _resolve_all(self) -> None:
        """Walk class hierarchy, resolve each declared setting from env.

        Iterates base → subclass so that subclass defaults override parent
        defaults, and env vars always win over any default.
        """
        for cls in reversed(type(self).__mro__):
            for key, default in vars(cls).items():
                if key.startswith("_") or callable(default):
                    continue
                if key == "yaml":
                    continue
                env_val = os.environ.get(key)
                if env_val is not None:
                    setattr(self, key, self._coerce(key, env_val, default))
                else:
                    setattr(self, key, default)

    def _coerce(self, key: str, env_val: str, default: Any) -> Any:
        """Coerce an env string to the type implied by the default value."""
        if isinstance(default, bool):
            return _to_bool(env_val)
        if isinstance(default, int):
            try:
                return int(env_val)
            except ValueError:
                return default
        if isinstance(default, float):
            try:
                return float(env_val)
            except ValueError:
                return default
        if isinstance(default, list):
            return _to_list(env_val)
        return env_val.strip()

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style access for optional lookups."""
        return getattr(self, key, default)
