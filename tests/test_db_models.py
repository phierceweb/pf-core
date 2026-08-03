"""Tests for pf_core.db.models — model name → ID resolver."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite

import pf_core.db.models as models_mod
from pf_core.db.models import clear_cache, resolve_model_id


@pytest.fixture(autouse=True)
def _clear_model_cache():
    clear_cache()
    yield
    clear_cache()


class TestResolveModelId:
    def test_creates_new_model(self, pf_tables, pf_connection):
        model_id = resolve_model_id("anthropic/claude-sonnet-4.6")
        assert model_id is not None
        assert isinstance(model_id, int)

    def test_returns_same_id_for_same_name(self, pf_tables, pf_connection):
        id1 = resolve_model_id("test/model-a")
        id2 = resolve_model_id("test/model-a")
        assert id1 == id2

    def test_different_models_get_different_ids(self, pf_tables, pf_connection):
        id1 = resolve_model_id("model-a")
        id2 = resolve_model_id("model-b")
        assert id1 != id2

    def test_empty_name_returns_none(self, pf_tables, pf_connection):
        assert resolve_model_id("") is None

    def test_cached_after_first_call(self, recording_conn):
        first = resolve_model_id("cached-model")
        after_first = len(recording_conn.statements)
        second = resolve_model_id("cached-model")
        assert second == first
        assert after_first == 2  # insert-or-ignore + SELECT
        assert len(recording_conn.statements) == 2  # second call never reached the DB

    def test_cached_id_survives_the_db_going_away(self, pf_tables, pf_connection, monkeypatch):
        model_id = resolve_model_id("real-model")

        @contextlib.contextmanager
        def _explode(*_args, **_kwargs):
            raise AssertionError("cache miss: resolve_model_id re-queried the DB")
            yield

        monkeypatch.setattr(models_mod, "transaction", _explode)
        assert resolve_model_id("real-model") == model_id

    def test_creates_the_row_in_the_db(self, pf_tables, pf_connection):
        from sqlalchemy import text

        from pf_core.db import transaction

        model_id = resolve_model_id("anthropic/claude-sonnet-4.6")
        with transaction() as conn:
            row = conn.execute(
                text("SELECT id FROM models WHERE name = :n"),
                {"n": "anthropic/claude-sonnet-4.6"},
            ).fetchone()
        assert row is not None
        assert row[0] == model_id


class TestClearCache:
    def test_clears_cache(self, recording_conn):
        first = resolve_model_id("to-clear")
        clear_cache()
        second = resolve_model_id("to-clear")
        assert second == first
        assert len(recording_conn.statements) == 4  # the DB was consulted again


# ---------------------------------------------------------------------------
# Dialect-correctness regression — no driver / server needed.
#
# Guards against ``resolve_model_id`` emitting raw ``INSERT IGNORE`` on a
# non-SQLite dialect — a hard syntax error on PostgreSQL. These tests capture
# the exact statement the resolver sends and compile it against each dialect,
# asserting it is that dialect's valid insert-or-ignore construct and never raw
# ``INSERT IGNORE``.
# ---------------------------------------------------------------------------


class _FakeResult:
    """Stand-in result: pretends a fresh row was inserted/selected.

    ``insert_ignore`` reads ``.first()`` (Postgres/SQLite) or ``.rowcount``
    (MySQL); ``resolve_model_id``'s SELECT reads ``.fetchone()``.
    """

    _row = (1,)

    def first(self):
        return self._row

    def fetchone(self):
        return self._row

    @property
    def rowcount(self) -> int:
        return 1


class _RecordingConn:
    """Captures every statement resolve_model_id executes for a chosen dialect."""

    def __init__(self, dialect_name: str):
        self.dialect = SimpleNamespace(name=dialect_name)
        self.statements: list = []

    def execute(self, stmt, params=None):
        self.statements.append(stmt)
        return _FakeResult()


@pytest.fixture()
def recording_conn(monkeypatch) -> _RecordingConn:
    """Swap the resolver's transaction for a statement recorder, so a cache hit
    is provable as "no DB access" rather than just "returned something"."""
    conn = _RecordingConn("sqlite")

    @contextlib.contextmanager
    def _fake_transaction(*_args, **_kwargs):
        yield conn

    monkeypatch.setattr(models_mod, "transaction", _fake_transaction)
    return conn


_DIALECTS = {
    "postgresql": postgresql.dialect(),
    "sqlite": sqlite.dialect(),
    "mysql": mysql.dialect(),
}


@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite", "mysql"])
def test_resolve_model_id_emits_dialect_valid_insert(monkeypatch, dialect_name):
    """The insert must be the dialect's on-conflict construct, never ``INSERT IGNORE``.

    Pins the fix for the Postgres ``INSERT IGNORE`` syntax-error bug and guards
    that the resolver keeps routing through the dialect-agnostic ``insert_ignore``
    helper for every supported dialect.
    """
    clear_cache()
    conn = _RecordingConn(dialect_name)

    @contextlib.contextmanager
    def _fake_transaction(*_args, **_kwargs):
        yield conn

    monkeypatch.setattr(models_mod, "transaction", _fake_transaction)

    model_id = resolve_model_id("anthropic/claude-sonnet-4.6")
    assert model_id == 1  # resolved from the SELECT that follows the insert

    # First statement executed is the insert; compile it for its dialect.
    insert_sql = str(conn.statements[0].compile(dialect=_DIALECTS[dialect_name])).upper()

    assert "INSERT IGNORE" not in insert_sql  # the bug: MySQL-only, invalid on Postgres
    if dialect_name in ("postgresql", "sqlite"):
        assert "ON CONFLICT (NAME) DO NOTHING" in insert_sql
    else:  # mysql / mariadb family — no-op ON DUPLICATE KEY UPDATE (not INSERT IGNORE)
        assert "ON DUPLICATE KEY UPDATE" in insert_sql
