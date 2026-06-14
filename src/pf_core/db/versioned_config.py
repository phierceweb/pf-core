"""Append-only "latest-version-wins" config resolution.

A reusable helper for the versioned-config pattern: instead of ``UPDATE``-ing a
config row in place, you ``INSERT`` a new row at ``version + 1``; readers always
take the highest ``version`` for a given scope. This keeps a full, auditable
history of every config a pipeline ran under, and makes "did the config change
since I last read it?" a cheap version comparison.

Two independent consumers built this by hand (per-section research config; a
versioned grading config with a project-default fallback), so the shared core
lives here. Policy that differs between them is parameterized: ``carry_forward``
(copy unspecified columns from the prior version) and ``get_latest_with_fallback``
(fall back to a default scope).

The table is referenced by name with caller-supplied column identifiers,
validated against a strict identifier pattern — **never interpolate user input
as a table or column name here.** Scope filters and inserted *values* are always
passed as bound parameters. The generated SQL is dialect-portable (SQLite /
MySQL / PostgreSQL).

Usage::

    from pf_core.db import transaction
    from pf_core.db.versioned_config import get_latest, append_version

    with transaction() as conn:
        current = get_latest(conn, "section_config", {"section_id": 5})
        new_version = append_version(
            conn, "section_config", {"section_id": 5},
            {"beat_query": "…"}, carry_forward=True,
        )
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Connection, text

from pf_core.exceptions import InvalidInputError

__all__ = [
    "latest_version",
    "get_latest",
    "get_latest_with_fallback",
    "append_version",
]

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Columns never carried forward by ``append_version(carry_forward=True)``: the
# DB owns the surrogate key and timestamps, so a new version row should get
# fresh ones rather than inheriting the prior row's.
_DEFAULT_CARRY_FORWARD_EXCLUDE = frozenset({"id", "created_at", "updated_at"})


def _check_ident(name: str) -> None:
    """Reject anything that isn't a bare SQL identifier (defense-in-depth)."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise InvalidInputError(f"invalid SQL identifier: {name!r}")


def _where(scope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build an AND-joined equality WHERE clause + bound params for *scope*.

    ``None`` values become ``IS NULL`` (so a NULL-keyed default scope matches);
    an empty scope yields ``1=1`` (the whole table is one scope — e.g. a
    singleton config).
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for col, val in scope.items():
        _check_ident(col)
        if val is None:
            clauses.append(f"{col} IS NULL")
        else:
            clauses.append(f"{col} = :{col}")
            params[col] = val
    return (" AND ".join(clauses) if clauses else "1=1"), params


def latest_version(
    conn: Connection, table: str, scope: dict[str, Any], *, version_col: str = "version"
) -> int:
    """Return the highest ``version_col`` for *scope*, or ``0`` if no rows."""
    _check_ident(table)
    _check_ident(version_col)
    where, params = _where(scope)
    row = (
        conn.execute(
            text(f"SELECT MAX({version_col}) AS v FROM {table} WHERE {where}"), params
        )
        .mappings()
        .fetchone()
    )
    v = row["v"] if row is not None else None
    return int(v) if v is not None else 0


def get_latest(
    conn: Connection, table: str, scope: dict[str, Any], *, version_col: str = "version"
) -> dict[str, Any] | None:
    """Return the highest-``version`` row matching *scope* as a dict, or ``None``."""
    _check_ident(table)
    _check_ident(version_col)
    where, params = _where(scope)
    row = (
        conn.execute(
            text(
                f"SELECT * FROM {table} WHERE {where} "
                f"ORDER BY {version_col} DESC LIMIT 1"
            ),
            params,
        )
        .mappings()
        .fetchone()
    )
    return dict(row) if row is not None else None


def get_latest_with_fallback(
    conn: Connection,
    table: str,
    scope: dict[str, Any],
    fallback_scope: dict[str, Any],
    *,
    version_col: str = "version",
) -> dict[str, Any] | None:
    """``get_latest(scope)``; if no row, ``get_latest(fallback_scope)``.

    Models the "specific config, else a shared default" lookup — e.g. an
    essay-specific config falling back to the project default.
    """
    row = get_latest(conn, table, scope, version_col=version_col)
    if row is not None:
        return row
    return get_latest(conn, table, fallback_scope, version_col=version_col)


def append_version(
    conn: Connection,
    table: str,
    scope: dict[str, Any],
    values: dict[str, Any],
    *,
    version_col: str = "version",
    carry_forward: bool = False,
    carry_forward_exclude: frozenset[str] = _DEFAULT_CARRY_FORWARD_EXCLUDE,
) -> int:
    """Insert a new version row for *scope* and return its version number.

    The new row is ``scope`` + ``values`` + ``{version_col: latest + 1}``. With
    ``carry_forward=True``, any column on the prior latest row but absent from
    ``scope`` / ``values`` is copied forward — except ``carry_forward_exclude``
    (by default the DB-owned ``id`` / ``created_at`` / ``updated_at``), so the new
    row gets fresh identity and timestamps.

    Args:
        conn: An open connection (e.g. from ``pf_core.db.transaction()``).
        table: Target table name (a validated SQL identifier).
        scope: Column→value identifying the config scope — the filter for
            ``latest_version`` and also written onto the new row.
        values: Column→value for the new version's payload.
        version_col: Name of the integer version column.
        carry_forward: Copy unspecified columns from the prior latest row.
        carry_forward_exclude: Columns never carried forward.

    Returns:
        The new version integer (prior max + 1; ``1`` for a brand-new scope).
    """
    _check_ident(table)
    _check_ident(version_col)
    for col in scope:
        _check_ident(col)
    for col in values:
        _check_ident(col)

    next_version = latest_version(conn, table, scope, version_col=version_col) + 1
    row: dict[str, Any] = {**scope, **values, version_col: next_version}

    if carry_forward:
        prior = get_latest(conn, table, scope, version_col=version_col)
        if prior is not None:
            skip = set(row) | set(carry_forward_exclude)
            for col, val in prior.items():
                if col not in skip:
                    _check_ident(col)
                    row[col] = val

    cols = list(row.keys())
    col_sql = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    conn.execute(text(f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"), row)
    return next_version
