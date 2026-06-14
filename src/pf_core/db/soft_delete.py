"""
Soft delete helpers for database tables.

Convention: soft-deleted rows have a non-NULL ``deleted_at`` column
(ISO timestamp). Active rows have ``deleted_at IS NULL``.

Usage::

    from pf_core.db import transaction
    from pf_core.db.soft_delete import soft_delete, restore, not_deleted

    with transaction() as conn:
        soft_delete(conn, "entries", "id", entry_id, reason="duplicate")
        restore(conn, "entries", "id", entry_id)

    # In SQL queries:
    sql = f"SELECT * FROM entries WHERE section_id = :sid {not_deleted()}"
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import column, func, table


def soft_delete(
    conn: Any,
    table_name: str,
    id_column: str,
    id_value: Any,
    *,
    reason: str | None = None,
    deleted_at_column: str = "deleted_at",
    reason_column: str | None = "deleted_reason",
) -> bool:
    """Mark a row as soft-deleted by setting deleted_at.

    Uses SQLAlchemy's ``func.now()`` for database-independent timestamps.

    Args:
        conn: SQLAlchemy connection (inside a transaction).
        table_name: Table name.
        id_column: Primary key column name.
        id_value: Primary key value to delete.
        reason: Optional deletion reason.
        deleted_at_column: Name of the timestamp column (default ``"deleted_at"``).
        reason_column: Name of the reason column (default ``"deleted_reason"``).
            Set to ``None`` if the table has no reason column.

    Returns:
        True if a row was updated, False if no matching active row found.
    """
    t = table(table_name, column(id_column), column(deleted_at_column))
    stmt = (
        t.update()
        .where(t.c[id_column] == id_value)
        .where(t.c[deleted_at_column].is_(None))
        .values({deleted_at_column: func.now()})
    )
    if reason_column and reason is not None:
        t = table(table_name, column(id_column), column(deleted_at_column), column(reason_column))
        stmt = (
            t.update()
            .where(t.c[id_column] == id_value)
            .where(t.c[deleted_at_column].is_(None))
            .values({deleted_at_column: func.now(), reason_column: reason})
        )
    result = conn.execute(stmt)
    return result.rowcount > 0


def restore(
    conn: Any,
    table_name: str,
    id_column: str,
    id_value: Any,
    *,
    deleted_at_column: str = "deleted_at",
    reason_column: str | None = "deleted_reason",
) -> bool:
    """Restore a soft-deleted row by clearing deleted_at.

    Args:
        conn: SQLAlchemy connection (inside a transaction).
        table_name: Table name.
        id_column: Primary key column name.
        id_value: Primary key value to restore.
        deleted_at_column: Name of the timestamp column (default ``"deleted_at"``).
        reason_column: Name of the reason column (default ``"deleted_reason"``).
            Set to ``None`` if the table has no reason column.

    Returns:
        True if a row was restored, False if no matching deleted row found.
    """
    t = table(table_name, column(id_column), column(deleted_at_column))
    values: dict[str, Any] = {deleted_at_column: None}
    if reason_column:
        t = table(table_name, column(id_column), column(deleted_at_column), column(reason_column))
        values[reason_column] = None
    stmt = (
        t.update()
        .where(t.c[id_column] == id_value)
        .where(t.c[deleted_at_column].is_not(None))
        .values(values)
    )
    result = conn.execute(stmt)
    return result.rowcount > 0


def not_deleted(*, column: str = "deleted_at", prefix: str = "AND ") -> str:
    """Return a SQL fragment for filtering active (non-deleted) rows.

    Args:
        column: Name of the deleted_at column (default ``"deleted_at"``).
        prefix: SQL prefix (default ``"AND "``). Use ``"WHERE "`` if this
            is the first condition, or ``""`` for bare fragment.

    Returns:
        SQL fragment string, e.g. ``"AND deleted_at IS NULL"``.
    """
    return f"{prefix}{column} IS NULL"
