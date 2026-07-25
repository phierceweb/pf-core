"""Tests for pf_core.db.connection — database connection management."""

from __future__ import annotations

import logging
import subprocess
import sys
import types

import pytest
from sqlalchemy import text

from pf_core.db.connection import (
    DatabaseUnavailableError,
    _install_mysqldb_shim,
    db_url,
    get_engine,
    is_sqlite,
    ping,
    reset_engine,
    transaction,
)
from pf_core.exceptions import ConfigurationError


class TestDbUrl:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
        assert db_url() == "sqlite:///test.db"

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "  sqlite:///test.db  ")
        assert db_url() == "sqlite:///test.db"

    def test_fallback_sqlite(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        result = db_url(fallback_sqlite="data.db")
        assert result.startswith("sqlite:///")
        assert "data.db" in result

    def test_raises_when_no_url_no_fallback(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(DatabaseUnavailableError, match="DATABASE_URL is not set"):
            db_url()

    def test_custom_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_DB", "sqlite:///custom.db")
        assert db_url(env_var="MY_DB") == "sqlite:///custom.db"

    def test_is_configuration_error(self):
        assert issubclass(DatabaseUnavailableError, ConfigurationError)

    def test_env_var_wins_over_fallback(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "mysql://real")
        result = db_url(fallback_sqlite="fallback.db")
        assert result == "mysql://real"


class TestIsSqlite:
    def test_sqlite_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
        assert is_sqlite("sqlite:///test.db") is True

    def test_mysql_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "mysql://localhost/db")
        assert is_sqlite("mysql://localhost/db") is False


class TestIsPostgres:
    def test_postgresql_url(self):
        from pf_core.db.connection import is_postgres
        assert is_postgres("postgresql+psycopg://localhost/db") is True
        assert is_postgres("postgres://localhost/db") is True

    def test_non_postgres_urls(self):
        from pf_core.db.connection import is_postgres
        assert is_postgres("sqlite:///x.db") is False
        assert is_postgres("mysql://localhost/db") is False


class TestDialectOf:
    def test_sqlite(self):
        from pf_core.db.connection import dialect_of
        assert dialect_of("sqlite:///x.db") == "sqlite"

    def test_mysql(self):
        from pf_core.db.connection import dialect_of
        assert dialect_of("mysql://localhost/db") == "mysql"
        assert dialect_of("mariadb://localhost/db") == "mysql"

    def test_postgresql(self):
        from pf_core.db.connection import dialect_of
        assert dialect_of("postgresql+psycopg://localhost/db") == "postgresql"
        assert dialect_of("postgres://localhost/db") == "postgresql"

    def test_unknown_raises(self):
        from pf_core.db.connection import dialect_of
        from pf_core.exceptions import ConfigurationError
        with pytest.raises(ConfigurationError):
            dialect_of("oracle://localhost/db")


class TestPostgresEngine:
    """Engine instantiation only — no real connection. Skipped when psycopg
    is not installed."""

    def test_engine_has_postgresql_dialect(self):
        psycopg = pytest.importorskip("psycopg")  # noqa: F841

        from pf_core.db.connection import get_engine, reset_engine
        reset_engine()
        try:
            engine = get_engine("postgresql+psycopg://demo:demo@127.0.0.1:5432/nope")
            assert engine.dialect.name == "postgresql"
        finally:
            reset_engine()


class TestPublicReExports:
    def test_is_postgres_re_exported(self):
        from pf_core.db import is_postgres as exported
        from pf_core.db.connection import is_postgres
        assert exported is is_postgres

    def test_dialect_of_re_exported(self):
        from pf_core.db import dialect_of as exported
        from pf_core.db.connection import dialect_of
        assert exported is dialect_of


class TestGetEngine:
    def test_returns_engine(self, pf_engine):
        # pf_engine fixture already sets up the engine
        engine = get_engine()
        assert engine is not None

    def test_caches_engine(self, pf_engine):
        assert get_engine() is get_engine()


class TestGetEngineUrlMismatchWarning:
    def test_differing_url_warns_and_returns_cached(self, pf_engine, caplog):
        with caplog.at_level(logging.WARNING, logger="pf_core.db.connection"):
            engine = get_engine("mysql://alice:s3cr3t@db.example.test:3306/other")
        assert engine is pf_engine
        messages = [r.getMessage() for r in caplog.records]
        assert any("get_engine_url_ignored" in m for m in messages)
        joined = " ".join(messages)
        assert "s3cr3t" not in joined
        assert "alice" not in joined
        assert "db.example.test" in joined

    def test_matching_url_no_warning(self, pf_engine, caplog):
        url = pf_engine.url.render_as_string(hide_password=False)
        with caplog.at_level(logging.WARNING, logger="pf_core.db.connection"):
            engine = get_engine(url)
        assert engine is pf_engine
        assert not [r for r in caplog.records if "get_engine_url_ignored" in r.getMessage()]

    def test_none_url_no_warning(self, pf_engine, caplog):
        with caplog.at_level(logging.WARNING, logger="pf_core.db.connection"):
            engine = get_engine()
        assert engine is pf_engine
        assert not [r for r in caplog.records if "get_engine_url_ignored" in r.getMessage()]


class TestMysqldbShim:
    """The PyMySQL install_as_MySQLdb shim must be an engine-creation concern,
    never an import side effect. Import-state assertions run in a subprocess
    because this process's sys.modules is already polluted."""

    def _run(self, code: str) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr

    def test_import_pf_core_db_does_not_install_shim(self):
        self._run(
            "import sys, pf_core.db; "
            "assert 'MySQLdb' not in sys.modules, 'shim installed at import time'"
        )

    def test_bare_mysql_url_engine_installs_shim(self):
        pytest.importorskip("pymysql")
        self._run(
            "import sys; "
            "from pf_core.db.connection import get_engine; "
            "engine = get_engine('mysql://demo:demo@127.0.0.1:3306/nope'); "
            "import pymysql; "
            "assert sys.modules.get('MySQLdb') is pymysql; "
            "assert engine.dialect.name == 'mysql'"
        )

    def test_explicit_pymysql_driver_skips_shim(self):
        pytest.importorskip("pymysql")
        self._run(
            "import sys; "
            "from pf_core.db.connection import get_engine; "
            "engine = get_engine('mysql+pymysql://demo:demo@127.0.0.1:3306/nope'); "
            "assert 'MySQLdb' not in sys.modules; "
            "assert engine.dialect.driver == 'pymysql'"
        )

    def test_shim_scheme_matrix(self, monkeypatch):
        calls: list[bool] = []
        fake = types.ModuleType("pymysql")
        fake.install_as_MySQLdb = lambda: calls.append(True)
        monkeypatch.setitem(sys.modules, "pymysql", fake)
        _install_mysqldb_shim("mysql://u:p@h/db")
        _install_mysqldb_shim("mariadb://u:p@h/db")
        assert len(calls) == 2
        _install_mysqldb_shim("mysql+pymysql://u:p@h/db")
        _install_mysqldb_shim("sqlite:///x.db")
        _install_mysqldb_shim("postgresql+psycopg://h/db")
        assert len(calls) == 2

    def test_shim_missing_pymysql_is_noop(self, monkeypatch):
        # None in sys.modules makes `import pymysql` raise ModuleNotFoundError.
        monkeypatch.setitem(sys.modules, "pymysql", None)
        monkeypatch.delitem(sys.modules, "MySQLdb", raising=False)
        _install_mysqldb_shim("mysql://u:p@h/db")  # must not raise
        assert "MySQLdb" not in sys.modules


class TestResetEngine:
    def test_allows_recreation(self, pf_engine):
        # Smoke: retrieving and resetting both succeed without raising.
        # A real new-engine assertion isn't possible here because the
        # pf_engine fixture has already patched _engine.
        get_engine()
        reset_engine()


class TestTransaction:
    def test_commits_on_success(self, pf_tables, pf_connection):
        # Use the test engine's transaction directly
        with transaction() as conn:
            conn.execute(text("INSERT INTO items (name) VALUES (:name)"), {"name": "tx_test"})

    def test_yields_connection(self, pf_tables, pf_connection):
        with transaction() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
            assert result == 1


class TestPing:
    def test_ping_succeeds(self, pf_engine):
        ping()  # should not raise
