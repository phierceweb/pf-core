"""
Comparator registry and built-in comparators.

A comparator takes a golden output dict, a replay output dict, and a context
dict (sourced from AgentEvalConfig) and returns a float 0.0–1.0 score.

Built-in comparators:
- ``structured_diff`` — field-by-field comparison with per-field tolerances.

Projects register custom comparators::

    from pf_core.eval import register_comparator

    @register_comparator("my_compare")
    def my_compare(golden: dict, replay: dict, *, context: dict) -> float:
        ...

Then reference in eval.yaml::

    agents:
      drafter:
        compare: custom:my_compare

The ``custom:`` prefix is optional: bare names like ``my_compare`` also resolve.
"""

from __future__ import annotations

from typing import Any, Callable

from pf_core.exceptions import ConfigurationError

_registry: dict[str, Callable[..., float]] = {}


def register_comparator(name: str) -> Callable:
    """Decorator that registers a named comparator.

    Args:
        name: Registry key. Can be referenced in eval.yaml as-is or prefixed
            with ``custom:``.

    Returns:
        The original function unchanged.
    """

    def decorator(fn: Callable[..., float]) -> Callable[..., float]:
        _registry[name] = fn
        return fn

    return decorator


def get_comparator(name: str) -> Callable[..., float]:
    """Retrieve a comparator by name.

    Handles both bare names (``structured_diff``) and the ``custom:`` prefix.

    Raises:
        ConfigurationError: If no comparator is registered under ``name``.
    """
    key = name.removeprefix("custom:")
    if key not in _registry:
        raise ConfigurationError(
            f"Unknown comparator {name!r}. Registered: {sorted(_registry)}"
        )
    return _registry[key]


def list_comparators() -> list[str]:
    """Return all registered comparator names."""
    return sorted(_registry)


# ---------------------------------------------------------------------------
# Field scoring helpers
# ---------------------------------------------------------------------------


def _field_score(golden: Any, replay: Any, *, tolerance: float | None) -> float:
    """Score one field comparison. Returns 0.0–1.0."""
    if golden is None and replay is None:
        return 1.0
    if golden is None or replay is None:
        return 0.0

    # Allow int ↔ float coercion for numeric fields
    g: Any = golden
    r: Any = replay
    if isinstance(g, int) and isinstance(r, float):
        g = float(g)
    elif isinstance(r, int) and isinstance(g, float):
        r = float(r)

    if type(g) is not type(r):
        return 0.0

    if isinstance(g, float):
        diff = abs(g - r)
        if tolerance is not None:
            if diff <= tolerance:
                return 1.0
            # Linear decay beyond tolerance
            return max(0.0, 1.0 - (diff - tolerance) / (abs(g) + 1e-9))
        return 1.0 if g == r else 0.0

    if isinstance(g, list):
        g_set = {str(x) for x in g}
        r_set = {str(x) for x in r}
        if not g_set and not r_set:
            return 1.0
        union = g_set | r_set
        if not union:
            return 1.0
        return len(g_set & r_set) / len(union)

    return 1.0 if g == r else 0.0


# ---------------------------------------------------------------------------
# Built-in: structured_diff
# ---------------------------------------------------------------------------


@register_comparator("structured_diff")
def structured_diff(golden: dict, replay: dict, *, context: dict) -> float:
    """Field-by-field comparison with optional per-field numeric tolerances.

    Context keys (from ``AgentEvalConfig``):
        diff_fields: Fields to compare. Defaults to all keys in the golden dict.
        tolerances: ``{field_name: abs_tolerance}`` for numeric fields.

    Returns:
        Mean per-field score across all ``diff_fields``. 1.0 = exact match.
    """
    diff_fields: list[str] = context.get("diff_fields") or list(golden.keys())
    tolerances: dict[str, float] = context.get("tolerances") or {}

    if not diff_fields:
        return 1.0

    scores = [
        _field_score(golden.get(f), replay.get(f), tolerance=tolerances.get(f))
        for f in diff_fields
    ]
    return sum(scores) / len(scores)
