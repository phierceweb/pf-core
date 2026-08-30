"""Per-test isolation on a server test backend named by ``PF_TEST_DATABASE_URL``."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool

from pf_core.exceptions import ConfigurationError
from pf_core.utils.env import resolve_int

URL_ENV_VAR = "PF_TEST_DATABASE_URL"

_LOCK_TIMEOUT_S_DEFAULT = 10
_LOCK_TIMEOUT_ENV_VAR = "PF_TEST_LOCK_TIMEOUT_S"

# Tables and views in public that no extension owns (PostGIS keeps spatial_ref_sys there).
_SHARED_RELATIONS = text(
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
    "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass "
    "AND d.objid = c.oid AND d.deptype = 'e') ORDER BY c.relname"
)
_SHARED_SHOWN = 5


def configured_url() -> str:
    """The operator's server test URL, or ``""`` for the per-test SQLite default."""
    return os.environ.get(URL_ENV_VAR, "").strip()


def backend_dialect(url: str) -> str:
    """Dialect family of *url* — ``sqlite`` for an empty URL, ``mysql`` for MariaDB."""
    if not url:
        return "sqlite"
    name = make_url(url).get_backend_name()
    return "mysql" if name == "mariadb" else name


def active_dialect() -> str:
    """Dialect the DB fixtures run against in this process."""
    return backend_dialect(configured_url())


def _lock_timeout_s() -> int:
    """Seconds a namespace statement may wait for a lock; 0 waits forever."""
    n: int = resolve_int(None, _LOCK_TIMEOUT_ENV_VAR, default=_LOCK_TIMEOUT_S_DEFAULT)
    return n if n >= 0 else _LOCK_TIMEOUT_S_DEFAULT


def _lock_timeout_sql(dialect: str, seconds: int) -> str:
    if not seconds:
        return ""
    if dialect == "postgresql":
        return f"SET lock_timeout = '{seconds}s'"
    return f"SET SESSION lock_wait_timeout = {seconds}"


def _scoped(url: URL, dialect: str, name: str) -> tuple[URL, str, str]:
    if dialect == "postgresql":
        options = url.query.get("options", "")
        if isinstance(options, tuple):
            options = " ".join(options)
        # public last so extension types and functions installed there still resolve, while
        # unqualified CREATEs land in the test schema.
        search_path = f"{options} -csearch_path={name},public".strip()
        return (
            url.update_query_dict({"options": search_path}),
            f'CREATE SCHEMA "{name}"',
            f'DROP SCHEMA IF EXISTS "{name}" CASCADE',
        )
    return url.set(database=name), f"CREATE DATABASE `{name}`", f"DROP DATABASE IF EXISTS `{name}`"


@contextmanager
def isolated_url(url: str) -> Iterator[str]:
    """Yield *url* scoped to a fresh Postgres schema or MySQL database, dropped on exit.

    Any other backend (SQLite included) is yielded unchanged.

    Raises:
        ConfigurationError: Postgres's ``public``, which stays on the test's search_path so
            extension types resolve, holds a table or view no extension owns.
    """
    dialect = backend_dialect(url)
    if dialect not in ("postgresql", "mysql"):
        yield url
        return

    base = make_url(url)
    scoped, create, drop = _scoped(base, dialect, f"pf_test_{uuid.uuid4().hex[:16]}")
    admin = create_engine(base, poolclass=NullPool)
    try:
        if dialect == "postgresql":
            _refuse_shared_relations(admin)
        _run(admin, dialect, create)
        try:
            yield scoped.render_as_string(hide_password=False)
        finally:
            _run(admin, dialect, drop)
    finally:
        admin.dispose()


def _refuse_shared_relations(admin: Engine) -> None:
    with admin.connect() as conn:
        names: list[str] = list(conn.execute(_SHARED_RELATIONS).scalars())
    if names:
        more = len(names) - _SHARED_SHOWN
        shown = ", ".join(names[:_SHARED_SHOWN]) + (f" and {more} more" if more > 0 else "")
        raise ConfigurationError(
            f"{URL_ENV_VAR}'s public schema holds tables or views every test would read and "
            f"write through its search_path: {shown}. Point it at an empty database, or drop them."
        )


def _run(admin: Engine, dialect: str, stmt: str) -> None:
    """Run one namespace statement under a lock timeout, so a connection a test leaked
    mid-transaction fails the teardown instead of hanging the run."""
    with admin.begin() as conn:
        timeout = _lock_timeout_sql(dialect, _lock_timeout_s())
        if timeout:
            conn.execute(text(timeout))
        conn.execute(text(stmt))
