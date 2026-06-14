"""Tests for pf_core.db.insert_ignore / upsert — dialect-agnostic upserts.

SQLite behaviour is exercised at runtime via the ``pf_engine`` fixture. Every supported dialect's
generated SQL is pinned by compiling against that dialect — which needs no database driver, so the
suite stays dependency-light (no pymysql / psycopg). The MySQL *runtime* path is exercised by a
real roundtrip against either ``PF_CORE_TEST_MYSQL_URL`` or an ephemeral ``testcontainers`` MySQL
(needs Docker); it skips cleanly when neither is available.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.exc import DBAPIError

from pf_core.db import insert_ignore, transaction, upsert
from pf_core.db.upsert import _insert_ignore_stmt, _upsert_stmt
from pf_core.exceptions import InvalidInputError

pytest_plugins = ["pf_core.testing.db_fixtures"]

_md = MetaData()
_widgets = Table(
    "widgets", _md,
    Column("slug", Text, primary_key=True),
    Column("name", Text),
    Column("n", Integer),
)


@pytest.fixture()
def widgets(pf_engine):
    _md.create_all(pf_engine)
    return _widgets


# ---------------------------------------------------------------------------
# SQLite — runtime behaviour
# ---------------------------------------------------------------------------


def test_insert_ignore_inserts_then_skips(widgets):
    with transaction() as c:
        assert insert_ignore(c, widgets, {"slug": "a", "name": "A", "n": 1}, conflict=["slug"]) == 1
        # conflict on the same slug → not inserted, original preserved
        assert insert_ignore(c, widgets, {"slug": "a", "name": "A2", "n": 2}, conflict=["slug"]) == 0
    with transaction() as c:
        row = c.execute(text("SELECT name, n FROM widgets WHERE slug = 'a'")).fetchone()
    assert tuple(row) == ("A", 1)


def test_upsert_inserts_then_updates(widgets):
    with transaction() as c:
        upsert(c, widgets, {"slug": "b", "name": "B", "n": 1}, conflict=["slug"], update=["name", "n"])
        upsert(c, widgets, {"slug": "b", "name": "B2", "n": 2}, conflict=["slug"], update=["name", "n"])
    with transaction() as c:
        row = c.execute(text("SELECT name, n FROM widgets WHERE slug = 'b'")).fetchone()
    assert tuple(row) == ("B2", 2)


def test_upsert_rejects_empty_conflict_or_update(widgets):
    with transaction() as c:
        with pytest.raises(InvalidInputError):
            upsert(c, widgets, {"slug": "z"}, conflict=[], update=["name"])
        with pytest.raises(InvalidInputError):
            upsert(c, widgets, {"slug": "z"}, conflict=["slug"], update=[])


# ---------------------------------------------------------------------------
# Every supported dialect — generated SQL (compiled, no driver / server)
# ---------------------------------------------------------------------------


def _sql(stmt, dialect) -> str:
    return str(stmt.compile(dialect=dialect)).upper()


def _ii(name):
    return _insert_ignore_stmt(name, _widgets, {"slug": "a", "name": "A", "n": 1}, ["slug"])


def _up(name):
    return _upsert_stmt(name, _widgets, {"slug": "b", "name": "B", "n": 1}, ["slug"], ["name", "n"])


@pytest.mark.parametrize("name,dialect", [("postgresql", postgresql.dialect()), ("sqlite", sqlite.dialect())])
def test_on_conflict_dialects(name, dialect):
    ii = _sql(_ii(name), dialect)
    assert "ON CONFLICT (SLUG) DO NOTHING" in ii
    assert "RETURNING" in ii  # reliable inserted-count on psycopg
    up = _sql(_up(name), dialect)
    assert "ON CONFLICT (SLUG) DO UPDATE SET" in up
    assert "NAME = EXCLUDED.NAME" in up and "N = EXCLUDED.N" in up


@pytest.mark.parametrize("name", ["mysql", "mariadb"])
def test_mysql_family_dialects(name):
    dialect = mysql.dialect()
    ii = _sql(_ii(name), dialect)
    # No-op ON DUPLICATE KEY UPDATE — NOT INSERT IGNORE (which would swallow non-duplicate errors).
    assert "INSERT IGNORE" not in ii
    assert "ON DUPLICATE KEY UPDATE SLUG = WIDGETS.SLUG" in ii
    assert "RETURNING" not in ii  # MySQL has none here; the count comes from rowcount
    up = _sql(_up(name), dialect)
    assert "ON DUPLICATE KEY UPDATE" in up
    set_clause = up.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert "NAME = VALUES(NAME)" in set_clause and "N = VALUES(N)" in set_clause


def test_unsupported_dialect_raises():
    with pytest.raises(NotImplementedError):
        _insert_ignore_stmt("oracle", _widgets, {"slug": "a"}, ["slug"])


# ---------------------------------------------------------------------------
# MySQL / MariaDB — real runtime roundtrip
#
# Source order: PF_CORE_TEST_MYSQL_URL (e.g. a CI service container), else an
# ephemeral testcontainers MySQL (needs Docker). Skips cleanly if neither is
# available, so the suite stays green without Docker.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mysql_engine():
    url = os.environ.get("PF_CORE_TEST_MYSQL_URL")
    if url:
        engine = create_engine(url)
        try:
            with engine.connect():
                pass
        except Exception as e:  # set but unreachable → skip, don't error the module
            engine.dispose()
            pytest.skip(f"PF_CORE_TEST_MYSQL_URL set but not reachable: {e}")
        yield engine
        engine.dispose()
        return

    mysql_mod = pytest.importorskip(
        "testcontainers.mysql", reason="testcontainers[mysql] not installed"
    )
    pytest.importorskip("pymysql", reason="MySQL driver ([mysql] extra) not installed")
    image = os.environ.get("PF_CORE_TEST_MYSQL_IMAGE", "mysql:8.4")
    try:
        container = mysql_mod.MySqlContainer(image)
        container.start()
    except Exception as e:  # Docker not running / image unpullable → skip
        pytest.skip(f"testcontainers MySQL unavailable (is Docker running?): {e}")
    try:
        engine = create_engine(container.get_connection_url())
        yield engine
        engine.dispose()
    finally:
        container.stop()


def test_mysql_roundtrip(mysql_engine):
    assert mysql_engine.dialect.name in ("mysql", "mariadb")  # really a MySQL family server
    md = MetaData()
    t = Table(
        "pf_core_upsert_test", md,
        Column("slug", String(64), primary_key=True),  # MySQL PKs need a bounded length
        Column("name", Text),
        Column("n", Integer),
    )
    md.drop_all(mysql_engine)
    md.create_all(mysql_engine)
    try:
        with mysql_engine.begin() as c:
            assert insert_ignore(c, t, {"slug": "a", "name": "A", "n": 1}, conflict=["slug"]) == 1
            # second insert conflicts → skipped (rowcount 0) AND the original row is untouched
            assert insert_ignore(c, t, {"slug": "a", "name": "A2", "n": 2}, conflict=["slug"]) == 0
            row = c.execute(text("SELECT name, n FROM pf_core_upsert_test WHERE slug = 'a'")).fetchone()
            assert tuple(row) == ("A", 1)
            upsert(c, t, {"slug": "a", "name": "A3", "n": 3}, conflict=["slug"], update=["name", "n"])
            row = c.execute(text("SELECT name, n FROM pf_core_upsert_test WHERE slug = 'a'")).fetchone()
            assert tuple(row) == ("A3", 3)
    finally:
        md.drop_all(mysql_engine)


def test_mysql_insert_ignore_does_not_swallow_non_duplicate_errors(mysql_engine):
    """The no-op ``ON DUPLICATE KEY UPDATE`` must NOT behave like ``INSERT IGNORE``.

    A non-duplicate error — here a too-long value under strict mode — must raise, not be silently
    truncated. The roundtrip above can't catch a regression to ``INSERT IGNORE`` (that also
    preserves the original on a dup); this is the test that actually pins the fix.
    """
    md = MetaData()
    t = Table("pf_core_trunc_test", md, Column("code", String(4), primary_key=True))
    md.drop_all(mysql_engine)
    md.create_all(mysql_engine)
    try:
        with mysql_engine.connect() as c:
            c.execute(text("SET SESSION sql_mode = 'STRICT_ALL_TABLES'"))
            with pytest.raises(DBAPIError):
                insert_ignore(c, t, {"code": "way-too-long"}, conflict=["code"])
            c.rollback()
    finally:
        md.drop_all(mysql_engine)
