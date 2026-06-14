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
    ``transaction()`` use the test engine.

pf_connection
    A SQLAlchemy ``Connection`` inside a SAVEPOINT. Rolled back after each
    test so tests never see each other's data.

pf_tables
    DDL runner. Use the ``@pytest.mark.pf_tables("CREATE TABLE ...")`` marker
    or define a ``pf_schema`` fixture returning a list of SQL strings.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool, StaticPool


def _create_test_engine(url: str = "sqlite://") -> Engine:
    """Create a SQLite engine suitable for testing.

    A bare ``sqlite://`` (in-memory) uses ``StaticPool`` — one shared
    connection — so the same in-memory database is visible across
    connections within a test. Convenient, but **not safe for tests
    that exercise multiple threads**: anything driving
    ``pf_core.parallel.run_parallel`` through the repos hits that single
    pooled connection from several threads at once and raises
    ``sqlite3.InterfaceError: bad parameter or other API misuse``
    (SQLITE_MISUSE). ``check_same_thread=False`` only silences the
    same-thread guard; it does not make concurrent use safe.

    A file URL uses ``NullPool`` + WAL + ``busy_timeout`` — each
    connection (hence each worker thread) gets its own DBAPI connection
    to the same file, exactly mirroring ``pf_core.db``'s production
    SQLite path, so concurrent writers serialize instead of corrupting a
    shared handle. The ``pf_engine`` fixture passes a per-test temp file
    for this reason.
    """
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
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pf_engine(tmp_path, monkeypatch) -> Iterator[Engine]:
    """Per-test file-backed SQLite engine, patched into pf_core.db.connection.

    Uses a temp file (not shared in-memory) with ``NullPool`` so each
    worker thread gets its own connection — safe for tests that exercise
    ``pf_core.parallel.run_parallel`` against the repos, and faithful to
    ``pf_core.db``'s production SQLite path. Each test gets a fresh file,
    so tests never see each other's data.

    After this fixture runs, ``get_engine()`` and ``transaction()`` from
    ``pf_core.db`` use this test engine instead of the real one.
    """
    db_path = tmp_path / "pf_core_test.sqlite"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    engine = _create_test_engine(url)

    # Patch get_engine() to return our test engine
    import pf_core.db.connection as conn_mod

    original_engine = conn_mod._engine
    conn_mod._engine = engine

    yield engine

    # Cleanup
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
