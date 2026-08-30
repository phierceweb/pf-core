"""Per-test isolation on a server test backend (``PF_TEST_DATABASE_URL``)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from pf_core.exceptions import ConfigurationError
from pf_core.testing._server_db import (
    _lock_timeout_s,
    _lock_timeout_sql,
    _scoped,
    active_dialect,
    backend_dialect,
    configured_url,
    isolated_url,
)
from pf_core.testing.db_fixtures import metadata_ddl

_PG = "postgresql+psycopg://u:p@h:5432/db"
_MY = "mysql+pymysql://u:p@h:3306/db"


class TestBackendDialect:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("", "sqlite"),
            ("sqlite:///x.db", "sqlite"),
            (_PG, "postgresql"),
            (_MY, "mysql"),
            ("mariadb+pymysql://u@h/db", "mysql"),
        ],
    )
    def test_families(self, url, expected):
        assert backend_dialect(url) == expected

    def test_active_dialect_follows_env(self, monkeypatch):
        monkeypatch.setenv("PF_TEST_DATABASE_URL", f"  {_PG}  ")
        assert configured_url() == _PG
        assert active_dialect() == "postgresql"
        monkeypatch.delenv("PF_TEST_DATABASE_URL")
        assert active_dialect() == "sqlite"


class TestScopedUrl:
    def test_postgres_sets_search_path(self):
        url, create, drop = _scoped(make_url(_PG), "postgresql", "pf_test_abc")
        assert url.query["options"] == "-csearch_path=pf_test_abc,public"
        assert url.database == "db"
        assert create == 'CREATE SCHEMA "pf_test_abc"'
        assert drop == 'DROP SCHEMA IF EXISTS "pf_test_abc" CASCADE'

    def test_postgres_keeps_existing_options(self):
        url, _, _ = _scoped(
            make_url(f"{_PG}?options=-cstatement_timeout%3D5000"), "postgresql", "pf_test_abc"
        )
        assert url.query["options"] == "-cstatement_timeout=5000 -csearch_path=pf_test_abc,public"

    def test_mysql_swaps_database(self):
        url, create, drop = _scoped(make_url(_MY), "mysql", "pf_test_abc")
        assert url.database == "pf_test_abc"
        assert create == "CREATE DATABASE `pf_test_abc`"
        assert drop == "DROP DATABASE IF EXISTS `pf_test_abc`"


class TestLockTimeout:
    @pytest.mark.parametrize(
        "dialect,expected",
        [
            ("postgresql", "SET lock_timeout = '10s'"),
            ("mysql", "SET SESSION lock_wait_timeout = 10"),
        ],
    )
    def test_sql_per_dialect(self, dialect, expected):
        assert _lock_timeout_sql(dialect, 10) == expected

    def test_zero_waits_forever(self):
        assert _lock_timeout_sql("postgresql", 0) == ""

    def test_env_overrides_default(self, monkeypatch):
        assert _lock_timeout_s() == 10
        monkeypatch.setenv("PF_TEST_LOCK_TIMEOUT_S", "3")
        assert _lock_timeout_s() == 3

    def test_negative_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PF_TEST_LOCK_TIMEOUT_S", "-5")
        assert _lock_timeout_s() == 10


class TestDdlDefaultsToTestBackend:
    @staticmethod
    def _metadata():
        from sqlalchemy import Column, Index, Integer, MetaData, String, Table

        md = MetaData()
        Table(
            "t",
            md,
            Column("id", Integer, primary_key=True),
            Column("name", String(32)),
            Index("idx_t_name", "name"),
        )
        return md

    def test_postgres_env_compiles_postgres(self, monkeypatch):
        monkeypatch.setenv("PF_TEST_DATABASE_URL", _PG)
        assert "SERIAL" in "\n".join(metadata_ddl(self._metadata()))

    def test_mysql_env_drops_index_if_not_exists(self, monkeypatch):
        monkeypatch.setenv("PF_TEST_DATABASE_URL", _MY)
        stmts = metadata_ddl(self._metadata())
        assert "CREATE TABLE IF NOT EXISTS" in stmts[0]
        assert "CREATE INDEX idx_t_name" in stmts[1] and "IF NOT EXISTS" not in stmts[1]

    def test_explicit_dialect_wins(self, monkeypatch):
        monkeypatch.setenv("PF_TEST_DATABASE_URL", _PG)
        assert "SERIAL" not in "\n".join(metadata_ddl(self._metadata(), dialect="sqlite"))


_server = pytest.mark.skipif(
    active_dialect() not in ("postgresql", "mysql"),
    reason="needs a Postgres or MySQL PF_TEST_DATABASE_URL",
)
_SEEN: list[str] = []


def _namespaces() -> set[str]:
    admin = create_engine(configured_url(), poolclass=NullPool)
    try:
        return set(inspect(admin).get_schema_names())
    finally:
        admin.dispose()


@_server
class TestServerIsolation:
    """Each test gets its own schema/database, gone after its teardown."""

    def test_engine_is_scoped_and_seeds_a_row(self, pf_engine):
        dialect = pf_engine.dialect.name
        name = (
            make_url(os.environ["DATABASE_URL"])
            .query["options"]
            .split("search_path=")[1]
            .split(",")[0]
            if dialect == "postgresql"
            else pf_engine.url.database
        )
        assert name.startswith("pf_test_")
        assert os.environ["DATABASE_URL"] == pf_engine.url.render_as_string(hide_password=False)
        assert name in _namespaces()
        with pf_engine.begin() as conn:
            conn.execute(text("CREATE TABLE leak_probe (v INTEGER)"))
            conn.execute(text("INSERT INTO leak_probe (v) VALUES (1)"))
        _SEEN.append(name)

    def test_previous_namespace_was_dropped(self, pf_engine):
        assert len(_SEEN) == 1
        assert _SEEN[0] not in _namespaces()
        assert "leak_probe" not in inspect(pf_engine).get_table_names()


@_server
class TestTeardownDoesNotHang:
    def test_a_leaked_transaction_fails_the_drop_until_it_closes(self, monkeypatch):
        """A test that leaves a connection mid-transaction must not stall the run."""
        monkeypatch.setenv("PF_TEST_LOCK_TIMEOUT_S", "1")
        url = configured_url()
        leaked = create_engine(url, poolclass=NullPool)
        conn = leaked.connect()
        try:
            with pytest.raises(OperationalError):
                with isolated_url(url) as scoped:
                    scoped_engine = create_engine(scoped, poolclass=NullPool)
                    with scoped_engine.begin() as held:
                        held.execute(text("CREATE TABLE held (v INTEGER)"))
                    scoped_engine.dispose()
                    conn.begin()
                    conn.execute(text(f"INSERT INTO {_quoted(scoped)}.held (v) VALUES (1)"))
        finally:
            conn.close()
        name = _namespace(scoped)
        assert name in _namespaces()
        with leaked.begin() as admin:
            admin.execute(text(_scoped(make_url(url), backend_dialect(url), name)[2]))
        leaked.dispose()
        assert name not in _namespaces()


def _namespace(scoped: str) -> str:
    url = make_url(scoped)
    if url.get_backend_name() == "postgresql":
        return str(url.query["options"]).split("search_path=")[1].split(",")[0]
    return str(url.database)


def _quoted(scoped: str) -> str:
    """The namespace as a connection on the base URL must address it."""
    q = '"' if make_url(scoped).get_backend_name() == "postgresql" else "`"
    return f"{q}{_namespace(scoped)}{q}"


_postgres = pytest.mark.skipif(
    active_dialect() != "postgresql", reason="needs a Postgres PF_TEST_DATABASE_URL"
)


@contextmanager
def _public_table(name: str, *, owned_by: str | None = None) -> Iterator[None]:
    """``public.<name>`` for the block, made a member of extension ``owned_by`` when given."""
    admin = create_engine(configured_url(), poolclass=NullPool)
    steps = [(f"CREATE TABLE public.{name} (v INTEGER)", f"DROP TABLE public.{name}")]
    if owned_by:
        member = f"EXTENSION {owned_by} %s TABLE public.{name}"
        steps.append((f"ALTER {member % 'ADD'}", f"ALTER {member % 'DROP'}"))
        with admin.connect() as conn:
            installed = conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = :e"), {"e": owned_by}
            ).first()
        if installed is None:
            steps.insert(0, (f"CREATE EXTENSION {owned_by}", f"DROP EXTENSION {owned_by}"))
    undo: list[str] = []
    try:
        for do, undone in steps:
            with admin.begin() as conn:
                conn.execute(text(do))
            undo.append(undone)
        yield
    finally:
        for stmt in reversed(undo):
            with admin.begin() as conn:
                conn.execute(text(stmt))
        admin.dispose()


@_postgres
class TestPublicSchema:
    """``public`` stays on the search_path for extension types, so a table there would resolve."""

    def test_a_table_in_public_is_refused_before_anything_is_created(self):
        before = _namespaces()
        with _public_table("pf_probe_shared"):
            with pytest.raises(ConfigurationError, match="pf_probe_shared"):
                with isolated_url(configured_url()):
                    pass
        assert _namespaces() == before

    def test_a_table_an_extension_owns_is_not_refused(self):
        with _public_table("pf_probe_owned", owned_by="citext"):
            with isolated_url(configured_url()) as scoped:
                assert _namespace(scoped).startswith("pf_test_")
