"""Environment-variable resolver helpers.

Codify the resolution pattern that ``config-driven.md`` prescribes:
explicit argument → environment variable → default. Malformed env
values fall back to the default and emit a structured warning so
operators don't silently lose their intended override.

Usage::

    from pf_core.utils.env import resolve_int, resolve_str

    def paginate_params(page, per_page, max_per_page=None):
        max_per_page = resolve_int(max_per_page, "MAX_PER_PAGE", default=200)
        ...

    def get_model_name(model=None):
        return resolve_str(model, "DEFAULT_MODEL", default="haiku")
"""

from __future__ import annotations

import os

from pf_core.log import get_logger

_log = get_logger(__name__)


def resolve_int(arg: int | None, env_var: str, *, default: int) -> int:
    """Resolve an int from an explicit arg, then env var, then default.

    Resolution order (first non-None wins):

    1. ``arg`` — explicit value passed by the caller. ``0`` counts as
       a real value (not "unset"); only ``None`` falls through.
    2. ``$env_var`` — string env var, parsed as int. Whitespace is
       stripped before parsing. Malformed values (non-numeric, empty
       string) emit a ``env_var_malformed`` warning and fall through
       to the default rather than raising — operators don't silently
       lose their intended override, but a single malformed env var
       doesn't crash the program either.
    3. ``default`` — required.

    Args:
        arg: Explicit value, or ``None`` to defer to env / default.
        env_var: Name of the environment variable to consult.
        default: Value to return when neither ``arg`` nor ``$env_var``
            is set (or env value is malformed).

    Returns:
        The resolved int.
    """
    if arg is not None:
        return arg
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        _log.warning(
            "env_var_malformed",
            var=env_var,
            value=raw,
            expected="int",
            falling_back_to=default,
        )
        return default


def resolve_str(
    arg: str | None,
    env_var: str,
    *,
    default: str | None = None,
) -> str | None:
    """Resolve a str from an explicit arg, then env var, then default.

    Resolution order (first non-None wins):

    1. ``arg`` — explicit value. ``""`` (empty string) counts as a real
       value; only ``None`` falls through.
    2. ``$env_var`` — string env var. ``""`` (empty string set) counts
       as set per OS semantics; only an unset variable falls through.
    3. ``default`` — defaults to ``None`` so callers can distinguish
       "not configured anywhere" from "configured to empty string".

    Args:
        arg: Explicit value, or ``None`` to defer to env / default.
        env_var: Name of the environment variable to consult.
        default: Fallback value. Defaults to ``None``.

    Returns:
        The resolved str, or ``default`` (which may itself be ``None``).
    """
    if arg is not None:
        return arg
    raw = os.environ.get(env_var)
    if raw is not None:
        return raw
    return default


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def resolve_bool(arg: bool | None, env_var: str, *, default: bool) -> bool:
    """Resolve a bool from an explicit arg, then env var, then default.

    Resolution order (first non-None wins):

    1. ``arg`` — explicit value. ``False`` counts as a real value
       (not "unset"); only ``None`` falls through.
    2. ``$env_var`` — string env var, case-insensitive, whitespace
       stripped. Accepted truthy: ``1`` / ``true`` / ``yes`` / ``on``.
       Accepted falsy: ``0`` / ``false`` / ``no`` / ``off``. Anything
       else emits a structured ``env_var_malformed`` warning and falls
       through to the default rather than raising — operators don't
       silently lose their intended override, but a single malformed env
       var doesn't crash the program either.
    3. ``default`` — required.

    Args:
        arg: Explicit value, or ``None`` to defer to env / default.
            ``False`` is a real value and is not treated as "unset".
        env_var: Name of the environment variable to consult.
        default: Value to return when neither ``arg`` nor ``$env_var``
            is set (or env value is malformed).

    Returns:
        The resolved bool.
    """
    if arg is not None:
        return arg
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    norm = raw.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    _log.warning(
        "env_var_malformed",
        var=env_var,
        value=raw,
        expected="bool (1/true/yes/on or 0/false/no/off)",
        falling_back_to=default,
    )
    return default


def resolve_positive_int(
    arg: int | None, env_var: str, *, default: int, min_value: int = 1
) -> int:
    """Resolve an int that must be ``>= min_value`` (arg → env → default).

    A bounded form of :func:`resolve_int`. The asymmetry is deliberate:

    - An explicit ``arg`` below ``min_value`` is a **caller bug** — it
      raises :class:`ValueError` so the mistake surfaces at the call site.
    - An env value that parses but is below ``min_value`` is an
      **operator typo** — it emits a structured ``env_var_out_of_range``
      warning and falls back to ``default`` rather than crashing a
      long-running process. (Malformed / non-numeric env still routes
      through :func:`resolve_int`'s ``env_var_malformed`` path.)

    ``default`` itself is trusted (assumed ``>= min_value`` by the
    caller); it is the floor a bad env value lands on.

    Args:
        arg: Explicit value, or ``None`` to defer to env / default.
        env_var: Name of the environment variable to consult.
        default: Value to return when neither ``arg`` nor ``$env_var``
            resolves to an in-range int. Trusted as ``>= min_value``.
        min_value: Inclusive lower bound; defaults to ``1``.

    Returns:
        The resolved int, guaranteed ``>= min_value``.

    Raises:
        ValueError: If ``arg`` is not ``None`` and is below ``min_value``.
    """
    if arg is not None and arg < min_value:
        raise ValueError(f"{env_var} arg must be >= {min_value}, got {arg!r}")
    n = resolve_int(arg, env_var, default=default)
    if n < min_value:
        _log.warning(
            "env_var_out_of_range",
            var=env_var,
            value=n,
            expected=f">= {min_value}",
            falling_back_to=default,
        )
        return default
    return n
