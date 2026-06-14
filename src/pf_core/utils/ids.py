"""
ID generation and allocation utilities.

Provides URL-safe nanoid generation and collision-safe allocation against
a database table.

Usage::

    from pf_core.utils.ids import generate_id, allocate_id

    # Simple generation (no DB check)
    new_id = generate_id()            # "V1StGXR8_Z5j"
    new_id = generate_id(size=8)      # "k3J9xQ2m"

    # Allocate with collision check
    from pf_core.db import transaction

    with transaction() as conn:
        entry_id = allocate_id(conn, table="entries")
"""

from __future__ import annotations

from typing import Any

from nanoid import generate

from pf_core.exceptions import PreconditionError

# URL-safe alphabet: alphanumeric + underscore + hyphen
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-"

_DEFAULT_SIZE = 12
_MAX_ATTEMPTS = 24


def generate_id(*, size: int | None = None) -> str:
    """Generate a URL-safe nanoid string.

    Args:
        size: Character length. Defaults to the ``ID_LENGTH`` environment
            variable (clamped to 8–36), or 12 if it is unset.

    Returns:
        A random URL-safe string.
    """
    length = size if size is not None else _default_size()
    return generate(_ALPHABET, length)


def allocate_id(
    conn: Any,
    *,
    table: str,
    column: str = "id",
    preferred: str | None = None,
    size: int | None = None,
    max_attempts: int = _MAX_ATTEMPTS,
) -> str:
    """Generate a unique ID, checking for collisions against a database table.

    Tries the preferred ID first (if provided and not taken), then generates
    nanoids in a bounded retry loop.

    Args:
        conn: A SQLAlchemy ``Connection`` (inside a transaction).
        table: Table name to check for collisions.
        column: Column name for the ID (default ``"id"``).
        preferred: Try this ID first before generating.
        size: Character length for generated IDs. Defaults to the
            ``ID_LENGTH`` environment variable (clamped to 8–36), or 12.
        max_attempts: Maximum generation attempts before raising.

    Returns:
        A unique ID string (not yet inserted — caller must insert it).

    Raises:
        PreconditionError: If a unique ID could not be allocated after
            *max_attempts* tries.
    """
    from sqlalchemy import text

    check_sql = text(f"SELECT 1 FROM {table} WHERE {column} = :id LIMIT 1")

    if preferred and str(preferred).strip():
        p = str(preferred).strip()
        row = conn.execute(check_sql, {"id": p}).fetchone()
        if not row:
            return p

    for _ in range(max_attempts):
        candidate = generate_id(size=size)
        row = conn.execute(check_sql, {"id": candidate}).fetchone()
        if not row:
            return candidate

    raise PreconditionError(
        f"Could not allocate unique ID in {table}.{column} after {max_attempts} attempts"
    )


def _default_size() -> int:
    """Read the ``ID_LENGTH`` env var (clamped to 8–36), else fall back to 12."""
    try:
        import os
        val = os.environ.get("ID_LENGTH", "")
        if val.strip():
            n = int(val)
            return max(8, min(36, n))
    except (ValueError, TypeError):
        pass
    return _DEFAULT_SIZE
