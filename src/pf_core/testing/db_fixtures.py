"""
Pytest plugin providing pf-core's DB testing fixtures (opt-in).

This module requires the ``[db]`` extra (sqlalchemy). Consumers who want
these fixtures must explicitly opt in by adding to their conftest.py::

    pytest_plugins = ["pf_core.testing.db_fixtures"]

Unlike ``pf_core.testing.fixtures`` (which auto-loads via the pytest11
entry point), this plugin is NOT auto-loaded. The split exists so that
no-DB consumers can use pf-core without their test runs trying to
import sqlalchemy.

Fixtures
--------
pf_engine
    File-backed SQLite engine (per-test temp file, ``NullPool``), fresh per
    test. Patches ``pf_core.db.connection`` so ``get_engine()`` /
    ``transaction()`` use the test engine. Set ``PF_TEST_DATABASE_URL`` to
    point every test at a disposable Postgres/MySQL database instead.
    Clears the tracking resolver caches at setup.

pf_engine_teardown
    No-op hook. Override in a consumer conftest to return a zero-arg
    callable run before ``engine.dispose()`` (e.g. drain worker threads).

pf_connection
    A SQLAlchemy ``Connection`` inside a SAVEPOINT. Rolled back after each
    test so tests never see each other's data.

pf_tables
    DDL runner. Use the ``@pytest.mark.pf_tables("CREATE TABLE ...")`` marker
    or define a ``pf_schema`` fixture returning a list of SQL strings.

Helpers
-------
``metadata_ddl(metadata)`` compiles any SQLAlchemy ``MetaData`` into DDL
strings for ``pf_schema``; ``framework_ddl()`` does it for every
pf-core-owned table (tracking, jobs, cache, budget)::

    @pytest.fixture
    def pf_schema():
        return framework_ddl() + PROJECT_DDL
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy.schema import CreateIndex, CreateTable


def _create_test_engine(url: str = "sqlite://") -> Engine:
    """Create a test engine for *url* (SQLite pragmas applied only to SQLite).

    In-memory SQLite (``sqlite://``) uses ``StaticPool`` — one shared DBAPI
    connection, visible across connects but **thread-unsafe**: concurrent use
    (e.g. ``pf_core.parallel.run_parallel`` through the repos) raises
    SQLITE_MISUSE, and ``check_same_thread=False`` only silences the guard.
    File URLs use ``NullPool`` + WAL + ``busy_timeout`` — a connection per
    worker thread, mirroring ``pf_core.db``'s production SQLite path — which
    is why ``pf_engine`` passes a per-test temp file.
    """
    if not url.startswith("sqlite"):
        # Operator-managed backend (PF_TEST_DATABASE_URL) — e.g. a throwaway
        # Postgres. NullPool as in production; SQLite pragmas don't apply.
        return create_engine(url, poolclass=NullPool)

    in_memory = url in ("sqlite://", "sqlite:///:memory:")
    engine = create_engine(
        url,
        poolclass=StaticPool if in_memory else NullPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        if not in_memory:
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA busy_timeout = 30000")
        cur.close()

    return engine


# ---------------------------------------------------------------------------
# DDL helpers — consumers splice these into their pf_schema fixture
# ---------------------------------------------------------------------------


def _dialect(name: str):
    from sqlalchemy.dialects import mysql, postgresql, sqlite

    mods = {"sqlite": sqlite, "postgresql": postgresql, "mysql": mysql}
    if name not in mods:
        raise ValueError(
            f"unknown dialect {name!r}; expected one of {sorted(mods)}"
        )
    return mods[name].dialect()


def metadata_ddl(
    metadata: MetaData,
    *,
    dialect: str = "sqlite",
    if_not_exists: bool = True,
    only: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Compile *metadata* into DDL strings for the ``pf_schema`` fixture.

    Tables come first in dependency order, then their indexes. ``only``
    restricts output to the named tables (and their indexes).
    """
    d = _dialect(dialect)
    tables = [
        t for t in metadata.sorted_tables if only is None or t.name in only
    ]
    stmts = [
        str(CreateTable(t, if_not_exists=if_not_exists).compile(dialect=d))
        for t in tables
    ]
    for t in tables:
        for idx in t.indexes:
            stmts.append(
                str(CreateIndex(idx, if_not_exists=if_not_exists).compile(dialect=d))
            )
    return stmts


def framework_ddl(
    *,
    dialect: str = "sqlite",
    if_not_exists: bool = True,
    only: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """DDL for every pf-core-owned table (tracking, jobs, cache, budget).

    Importing :mod:`pf_core.llm.tracking` registers all four table groups on
    the shared metadata. Never hand-copy these CREATE TABLE statements —
    copies drift when the framework schema changes. Requires the
    ``[tracking]`` closure; a jobs-only consumer can pass
    ``pf_core.jobs._schema``'s ``metadata`` to :func:`metadata_ddl` instead.

    ``only`` restricts output to the named tables (for projects whose own
    migrations extend the others).
    """
    import pf_core.llm.tracking as tracking

    return metadata_ddl(
        tracking.metadata, dialect=dialect, if_not_exists=if_not_exists, only=only
    )


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pf_engine_teardown():
    """Hook run by ``pf_engine`` before ``engine.dispose()``.

    Override in a consumer conftest to return a zero-arg callable when
    something must finish with the engine still alive — e.g. draining
    background worker threads (disposing under them can segfault SQLite).
    """
    return None


@pytest.fixture()
def pf_engine(tmp_path, monkeypatch, pf_engine_teardown) -> Iterator[Engine]:
    """Per-test file-backed SQLite engine, patched into pf_core.db.connection.

    Uses a temp file (not shared in-memory) with ``NullPool`` so each
    worker thread gets its own connection — safe for tests that exercise
    ``pf_core.parallel.run_parallel`` against the repos, and faithful to
    ``pf_core.db``'s production SQLite path. Each test gets a fresh file,
    so tests never see each other's data.

    Set ``PF_TEST_DATABASE_URL`` to run the same suite against a disposable
    Postgres/MySQL database instead; tests create and drop tables in it, so
    never point it at real data.

    After this fixture runs, ``get_engine()`` and ``transaction()`` from
    ``pf_core.db`` use this test engine instead of the real one. Tracking
    resolver caches are cleared at setup so ids cached against a previous
    test's database can't leak into this one.
    """
    url = os.environ.get("PF_TEST_DATABASE_URL", "").strip()
    if not url:
        db_path = tmp_path / "pf_core_test.sqlite"
        url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    engine = _create_test_engine(url)

    # Patch get_engine() to return our test engine
    import pf_core.db.connection as conn_mod

    original_engine = conn_mod._engine
    conn_mod._engine = engine

    try:
        from pf_core.llm.tracking import clear_resolver_caches
    except ImportError:
        pass  # [db]-only install — no tracking caches to clear
    else:
        clear_resolver_caches()

    yield engine

    # Cleanup — hook first, while the engine is still alive and patched in.
    if pf_engine_teardown is not None:
        pf_engine_teardown()

    engine.dispose()
    conn_mod._engine = original_engine


@pytest.fixture()
def pf_connection(pf_engine: Engine) -> Iterator[Connection]:
    """A connection with an active transaction, rolled back after each test.

    This gives each test a clean slate without the cost of recreating tables.
    Tests can commit within savepoints, but the outer transaction is always
    rolled back.
    """
    with pf_engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()


@pytest.fixture()
def pf_tables(request, pf_engine: Engine) -> Engine:
    """Create tables from marker DDL or from a ``pf_schema`` fixture.

    Usage with marker::

        @pytest.mark.pf_tables(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        def test_something(pf_tables, pf_connection):
            pf_connection.execute(text("INSERT INTO items ..."), {...})

    Usage with fixture (for project-wide schemas)::

        # In conftest.py:
        @pytest.fixture
        def pf_schema():
            return [
                "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)",
                "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)",
            ]

        def test_something(pf_tables, pf_connection):
            ...

    Both approaches can be combined — marker DDL runs after pf_schema.
    """
    ddl_statements: list[str] = []

    # Collect from pf_schema fixture if available
    if "pf_schema" in request.fixturenames:
        schema_fixture = request.getfixturevalue("pf_schema")
        if isinstance(schema_fixture, list):
            ddl_statements.extend(schema_fixture)
        elif isinstance(schema_fixture, str):
            ddl_statements.append(schema_fixture)

    # Collect from @pytest.mark.pf_tables(...) marker
    marker = request.node.get_closest_marker("pf_tables")
    if marker:
        for arg in marker.args:
            if isinstance(arg, str):
                ddl_statements.append(arg)
            elif isinstance(arg, (list, tuple)):
                ddl_statements.extend(arg)

    # Execute DDL
    if ddl_statements:
        with pf_engine.connect() as conn, conn.begin():
            for stmt in ddl_statements:
                conn.execute(text(stmt))

    return pf_engine


# ---------------------------------------------------------------------------
# Marker registration (prevents pytest "unknown marker" warnings)
# ---------------------------------------------------------------------------


def pytest_configure(config):
    """Register the pf_tables marker. Lives with the fixture it belongs to."""
    config.addinivalue_line(
        "markers",
        "pf_tables(*ddl_sql): Create tables from SQL strings before the test runs.",
    )
