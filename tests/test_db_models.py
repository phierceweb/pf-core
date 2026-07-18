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

    def test_cached_after_first_call(self, pf_tables, pf_connection):
        resolve_model_id("cached-model")
        # Second call should hit cache (no DB access needed)
        id2 = resolve_model_id("cached-model")
        assert id2 is not None


class TestClearCache:
    def test_clears_cache(self, pf_tables, pf_connection):
        resolve_model_id("to-clear")
        clear_cache()
        # After clearing, a new DB lookup should happen but still return same ID
        id2 = resolve_model_id("to-clear")
        assert id2 is not None


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
