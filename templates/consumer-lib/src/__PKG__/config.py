"""__NAME__ configuration.

Subclass ``pf_core.config.AppConfig`` and add project settings as class
attributes (env var name == attribute name). See pf-core ``docs/config.md``.
"""

from __future__ import annotations

from pathlib import Path

from pf_core.config import AppConfig


class Config(AppConfig):
    """Project settings. Add your own as typed class attributes, e.g.::

    GREETING: str = "hello"
    """


_env = Path(".env")
cfg = Config(env_file=_env if _env.exists() else None)
