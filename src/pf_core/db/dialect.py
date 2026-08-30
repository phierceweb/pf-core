"""Runtime SQL fragments resolved from a live connection's dialect.

See ``docs/db-dialect.md``.

Usage::

    from pf_core.db.dialect import now_sql, utc_cutoff

    with transaction() as conn:
        conn.execute(
            text(f"UPDATE jobs SET updated_at={now_sql(conn)} WHERE id=:i"),
            {"i": job_id},
        )
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pf_core.db.json_compat import insert_ignore_prefix as _insert_ignore_prefix
from pf_core.db.json_compat import now_expr as _now_expr
from pf_core.exceptions import InvalidInputError

__all__ = [
    "now_sql",
    "insert_ignore_prefix",
    "insert_ignore_suffix",
    "row_lock_suffix",
    "utc_cutoff",
]


def _dialect_name(conn: Any) -> str:
    """Normalized dialect name for a connection, engine, or plain string.

    ``mariadb`` (and any ``mysql+driver`` spelling) normalizes to ``mysql``;
    ``postgres`` spellings to ``postgresql`` — mirroring
    :func:`pf_core.db.connection.dialect_of`.

    Raises:
        InvalidInputError: for anything that is neither a dialect string nor
            an object with ``.dialect.name``, or for an unsupported dialect.
    """
    raw = conn if isinstance(conn, str) else getattr(getattr(conn, "dialect", None), "name", None)
    if not isinstance(raw, str) or not raw:
        raise InvalidInputError(f"expected a Connection/Engine or dialect string, got {conn!r}")
    name = raw.lower()
    if name.startswith("postgres"):
        return "postgresql"
    if name.startswith(("mysql", "mariadb")):
        return "mysql"
    if name.startswith("sqlite"):
        return "sqlite"
    raise InvalidInputError(f"unsupported dialect: {name!r}")


def now_sql(conn: Any) -> str:
    """:func:`~pf_core.db.json_compat.now_expr` for a connection's dialect."""
    return _now_expr(_dialect_name(conn))


def insert_ignore_prefix(conn: Any) -> str:
    """``INSERT``-or-skip-on-duplicate prefix for the connection's dialect.

    Accepts a connection, an engine, or a ``dialect`` string.

    On Postgres this returns a bare ``"INSERT"``: skip-on-duplicate there is a
    trailing clause, so it must be paired with :func:`insert_ignore_suffix`.

    Only skip-on-*duplicate* is portable: MySQL and SQLite also swallow
    NOT NULL, CHECK and truncation failures where Postgres raises. Where
    anything but a duplicate key can fail, use
    :func:`pf_core.db.upsert.insert_ignore` (see ``docs/db-dialect.md``).
    """
    return _insert_ignore_prefix(_dialect_name(conn))


def insert_ignore_suffix(conn: Any) -> str:
    """Trailing clause completing skip-on-duplicate: ``" ON CONFLICT DO
    NOTHING"`` on Postgres, ``""`` elsewhere.

    Append it after the VALUES clause whenever you use
    :func:`insert_ignore_prefix`, or Postgres inserts will raise on conflict.
    """
    return " ON CONFLICT DO NOTHING" if _dialect_name(conn) == "postgresql" else ""


def row_lock_suffix(conn: Any) -> str:
    """``SELECT`` suffix locking matched rows for a write in the same
    transaction, or ``""`` where the dialect has no row locks.

    SQLite rejects the clause and gets ``""``: its single-writer model is not
    a substitute for ``FOR UPDATE``.
    """
    return "" if _dialect_name(conn) == "sqlite" else " FOR UPDATE"


def utc_cutoff(
    *,
    seconds: int | None = None,
    minutes: int | None = None,
    days: int | None = None,
) -> datetime:
    """``now(UTC)`` minus the given delta, to bind as a query parameter.

    Bind it through a typed ``bindparam`` so SQLAlchemy renders the value;
    an untyped bind hands the raw datetime to the driver, which on SQLite
    means the adapter deprecated in Python 3.12::

        stmt = text("... WHERE updated_at < :cutoff").bindparams(
            bindparam("cutoff", type_=DateTime())
        )
        conn.execute(stmt, {"cutoff": utc_cutoff(minutes=30)})

    Returns a *naive* UTC datetime: ``get_engine`` pins the session to UTC on
    MySQL and Postgres, and an aware value renders an offset that SQLite — which
    compares as text — mismatches.

    ``docs/db-dialect.md`` covers when to compute the cutoff server-side
    instead.

    Raises:
        InvalidInputError: if no delta is given.
    """
    if seconds is None and minutes is None and days is None:
        raise InvalidInputError("utc_cutoff requires seconds=, minutes= or days=")
    delta = timedelta(
        seconds=seconds or 0,
        minutes=minutes or 0,
        days=days or 0,
    )
    return (datetime.now(timezone.utc) - delta).replace(tzinfo=None)
