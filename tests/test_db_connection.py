"""Tests for pf_core.db.connection — database connection management."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from pf_core.db.connection import (
    DatabaseUnavailableError,
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
