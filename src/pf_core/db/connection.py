"""
Database connection management via SQLAlchemy.

Supports SQLite, MySQL/MariaDB, and PostgreSQL. Configured via DATABASE_URL env var.

Usage::

    from pf_core.db import transaction, get_engine

    with transaction() as conn:
        rows = conn.execute(text("SELECT * FROM users")).fetchall()

    with transaction() as conn:
        conn.execute(text("INSERT INTO users ..."), {"id": 1})
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ModuleNotFoundError:
    pass

from sqlalchemy import create_engine, event, pool, text
from sqlalchemy.engine import Connection, Engine

from pf_core.exceptions import ConfigurationError


class DatabaseUnavailableError(ConfigurationError):
    """DATABASE_URL is missing or the database is unreachable."""


def db_url(*, env_var: str = "DATABASE_URL", fallback_sqlite: str = "") -> str:
    """Resolve the SQLAlchemy database URL from environment.

    Resolution order:
        1. DATABASE_URL env var
        2. fallback_sqlite path (if provided, becomes sqlite:///path)
        3. Raises ConfigurationError

    Args:
        env_var: Environment variable name (default DATABASE_URL).
        fallback_sqlite: Path to SQLite file if DATABASE_URL is not set.
    """
    url = (os.environ.get(env_var) or "").strip()
    if url:
        return url

    if fallback_sqlite:
        p = Path(fallback_sqlite).resolve()
        return f"sqlite:///{p}"

    raise DatabaseUnavailableError(
        f"{env_var} is not set. Add it to .env, for example:\n"
        "  DATABASE_URL=mysql://user:pass@127.0.0.1:3306/mydb\n"
        "  DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/mydb\n"
        "  DATABASE_URL=sqlite:///data.db"
    )


def is_sqlite(url: str | None = None) -> bool:
    """Check if the given (or default) database URL is SQLite."""
    return (url or db_url()).startswith("sqlite")


def is_postgres(url: str | None = None) -> bool:
    """Check if the given (or default) database URL is PostgreSQL."""
    u = (url or db_url()).lower()
    return u.startswith("postgresql") or u.startswith("postgres://")


def dialect_of(url: str | None = None) -> str:
    """Return ``"sqlite"`` | ``"mysql"`` | ``"postgresql"`` for the given (or default) URL.

    Use this when you need a dialect string for ``pf_core.db.json_compat`` helpers.
    """
    u = (url or db_url()).lower()
    if u.startswith("sqlite"):
        return "sqlite"
    if u.startswith("postgresql") or u.startswith("postgres://"):
        return "postgresql"
    if u.startswith("mysql") or u.startswith("mariadb"):
        return "mysql"
    raise ConfigurationError(f"unsupported database dialect in URL: {url!r}")


_engine: Engine | None = None


def get_engine(url: str | None = None) -> Engine:
    """Return (and cache) the SQLAlchemy engine.

    Args:
        url: Database URL. If not provided, resolves from environment.
    """
    global _engine
    if _engine is not None:
        return _engine

    resolved_url = url or db_url()
    dialect = dialect_of(resolved_url)

    if dialect == "sqlite":
        db_file = resolved_url.removeprefix("sqlite:///")
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(resolved_url, poolclass=pool.NullPool)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            busy_timeout = int(os.environ.get("SQLITE_BUSY_TIMEOUT", "30000"))
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute(f"PRAGMA busy_timeout = {busy_timeout}")
            cur.close()

    elif dialect == "mysql":
        engine = create_engine(resolved_url)

        @event.listens_for(engine, "connect")
        def _mysql_session_setup(dbapi_conn, _record):
            # Pin the MySQL session to UTC so TIMESTAMP columns round-trip
            # as naive-UTC (matching SQLite's contract). Without this,
            # ``CURRENT_TIMESTAMP`` is evaluated in whatever ``time_zone``
            # the MySQL server defaults to; callers that wrap the returned
            # naive value as ``tzinfo=timezone.utc`` then silently add the
            # session offset, skewing every timestamp comparison.
            cur = dbapi_conn.cursor()
            cur.execute("SET time_zone = '+00:00'")
            cur.execute("SET foreign_key_checks = 1")
            cur.close()

    else:  # postgresql
        # No per-connection setup needed: psycopg already negotiates UTC for
        # TIMESTAMPTZ via the server's timezone setting (default UTC), and
        # FK constraints are always enforced by Postgres.
        engine = create_engine(resolved_url)

    _engine = engine
    return _engine


def reset_engine() -> None:
    """Dispose and reset the cached engine (useful for testing)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


@contextmanager
def transaction(url: str | None = None) -> Iterator[Connection]:
    """Context manager yielding a SQLAlchemy Connection with an open transaction.

    Commits on clean exit; rolls back on exception.

    Usage::

        from pf_core.db import transaction
        from sqlalchemy import text

        with transaction() as conn:
            conn.execute(text("INSERT INTO ..."), {...})
    """
    engine = get_engine(url)
    with engine.connect() as conn:
        with conn.begin():
            yield conn


def ping(url: str | None = None) -> None:
    """Verify database connectivity (no side effects)."""
    engine = get_engine(url)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
