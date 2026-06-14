"""Cross-field validator decorator + registry.

Project code declares cross-field validators like this::

    @cross_field_validator("grade_within_rubric")
    def grade_within_rubric(parsed, *, context):
        ...
        return ValidationSignal(...)

The decorator registers the function under its given name; pipelines look it
up by name from the ``cross_field=[...]`` list in ``register()``.
"""

from __future__ import annotations

from typing import Any, Callable

_CROSS_FIELD_VALIDATORS: dict[str, Callable] = {}


def cross_field_validator(name: str) -> Callable[[Callable], Callable]:
    """Decorator that registers a cross-field validator by name.

    The decorated function takes ``(parsed, *, context)`` and returns a
    :class:`ValidationSignal` (or a list of them). The wrapper attaches
    ``.name`` to the function so :func:`parse_and_validate` can reference
    the registered name in error signals if the function raises.
    """
    def _decorator(fn: Callable) -> Callable:
        fn.name = name  # type: ignore[attr-defined]
        _CROSS_FIELD_VALIDATORS[name] = fn
        return fn
    return _decorator


def get_cross_field_validator(name: str) -> Callable:
    """Look up a registered cross-field validator. Raises ``KeyError`` if unknown."""
    if name not in _CROSS_FIELD_VALIDATORS:
        raise KeyError(
            f"cross_field validator '{name}' is not registered. "
            f"Known: {sorted(_CROSS_FIELD_VALIDATORS)}"
        )
    return _CROSS_FIELD_VALIDATORS[name]


def list_cross_field_validators() -> list[str]:
    """All registered cross-field validator names, sorted."""
    return sorted(_CROSS_FIELD_VALIDATORS)


def clear_cross_field_validators() -> None:
    """Drop all cross-field registrations. Test helper."""
    _CROSS_FIELD_VALIDATORS.clear()


# Make `Any` import silence linters that warn on unused generics in some tools.
_ = Any
