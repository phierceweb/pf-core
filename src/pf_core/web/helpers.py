"""
Shared web-layer helpers for route handlers.

Small utilities that both projects duplicate across every route file.
"""

from __future__ import annotations

from typing import TypeVar

from pf_core.exceptions import NotFoundError

T = TypeVar("T")


def resolve_or_404(result: T | None, entity: str = "record") -> T:
    """Return *result* if not None, otherwise raise NotFoundError (-> 404).

    The app factory's exception handler maps NotFoundError to HTTP 404.

    Args:
        result: The value to check (typically a repo query result).
        entity: Name of the entity for the error message.

    Raises:
        NotFoundError: When result is None.
    """
    if result is None:
        raise NotFoundError(entity)
    return result
