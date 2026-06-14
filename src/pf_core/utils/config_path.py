"""Resolve a config file via an override chain with a packaged default.

The path half of the config-driven convention (the value half is
:mod:`pf_core.utils.env`). Walk: an env-named override directory → the
CWD ``config/`` subtree → the package-bundled default. Return the first
existing path, absolute, so callers don't trip over a later CWD change.
Because the consumer's bundled file is a packaging invariant, this never
raises ``FileNotFoundError`` — the bundled default is the guaranteed floor.

Two integration styles:

1. Load directly — ``load_prompt_spec(resolve_config_path(...))``.
2. Export into a downstream loader's env var when that loader resolves
   its own path (e.g. the model router reads ``MODEL_ROUTER_CONFIG``)::

       import os
       from pathlib import Path
       from pf_core.utils.config_path import resolve_config_path

       _PKG = Path(__file__).parent          # the consumer's package dir
       os.environ.setdefault(
           "MODEL_ROUTER_CONFIG",
           str(resolve_config_path(
               "model_router.yaml",
               env_dir_var="MYAPP_ROUTER_DIR",
               bundled_dir=_PKG / "config",
           )),
       )
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_config_path(
    filename: str,
    *,
    env_dir_var: str | None,
    bundled_dir: Path,
    cwd_subdir: str = "config",
) -> Path:
    """Resolve a config file via override-dir → CWD subdir → bundled default.

    Returns the first existing of, in order:

    1. ``$env_dir_var/filename`` — an operator override directory (skipped
       when ``env_dir_var`` is ``None``, the var is unset, or the file is
       absent there).
    2. ``./cwd_subdir/filename`` — a project-local config dir.
    3. ``bundled_dir/filename`` — the package-bundled default.

    The result is always absolute (resolved eagerly so a later CWD change
    can't invalidate it). The bundled file is the guaranteed floor — a
    packaging invariant for the caller — so this never raises
    ``FileNotFoundError`` (it returns the bundled path even if that file
    happens to be missing; shipping it is the caller's responsibility).

    Args:
        filename: Bare config filename, e.g. ``"model_router.yaml"``.
        env_dir_var: Name of an env var holding an override *directory*,
            or ``None`` to skip the env step entirely.
        bundled_dir: The consumer's packaged config directory (the floor).
        cwd_subdir: Subdir under the CWD to check; defaults to ``"config"``.
            Pass e.g. ``"config/prompts"`` for nested layouts.

    Returns:
        The resolved absolute :class:`~pathlib.Path`.
    """
    if env_dir_var:
        env_dir = os.environ.get(env_dir_var)
        if env_dir:
            cand = Path(env_dir) / filename
            if cand.exists():
                return cand.resolve()
    cwd_cand = (Path(cwd_subdir) / filename).resolve()
    if cwd_cand.exists():
        return cwd_cand
    return (bundled_dir / filename).resolve()


__all__ = ["resolve_config_path"]
