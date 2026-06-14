"""Tests for pf_core.db.soft_delete."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from pf_core.db.soft_delete import not_deleted, restore, soft_delete


_PRODUCTS_TABLE = """
CREATE TABLE products (
    id TEXT PRIMARY KEY,
    name TEXT,
    deleted_at TEXT,
    deleted_reason TEXT
)
"""

_NOTES_TABLE = """
CREATE TABLE notes (
    id TEXT PRIMARY KEY,
    name TEXT,
    deleted_at TEXT
)
"""


class TestSoftDelete:
    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_soft_delete_active_row(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO products (id, name) VALUES (:id, :name)"),
            {"id": "abc", "name": "thing"},
        )
        result = soft_delete(pf_connection, "products", "id", "abc", reason="bad data")
        assert result is True
        row = pf_connection.execute(
            text("SELECT deleted_at, deleted_reason FROM products WHERE id = :id"),
            {"id": "abc"},
        ).mappings().fetchone()
        assert row["deleted_at"] is not None
        assert row["deleted_reason"] == "bad data"

    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_soft_delete_already_deleted(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO products (id, name, deleted_at) VALUES (:id, :name, :ts)"),
            {"id": "abc", "name": "thing", "ts": "2026-01-01T00:00:00Z"},
        )
        result = soft_delete(pf_connection, "products", "id", "abc")
        assert result is False

    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_soft_delete_nonexistent_row(self, pf_tables, pf_connection):
        result = soft_delete(pf_connection, "products", "id", "missing")
        assert result is False

    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_soft_delete_without_reason(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO products (id, name) VALUES (:id, :name)"),
            {"id": "abc", "name": "thing"},
        )
        result = soft_delete(pf_connection, "products", "id", "abc")
        assert result is True
        row = pf_connection.execute(
            text("SELECT deleted_at, deleted_reason FROM products WHERE id = :id"),
            {"id": "abc"},
        ).mappings().fetchone()
        assert row["deleted_at"] is not None
        assert row["deleted_reason"] is None

    @pytest.mark.pf_tables(_NOTES_TABLE)
    def test_soft_delete_no_reason_column(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO notes (id, name) VALUES (:id, :name)"),
            {"id": "abc", "name": "thing"},
        )
        result = soft_delete(pf_connection, "notes", "id", "abc", reason_column=None)
        assert result is True
        row = pf_connection.execute(
            text("SELECT deleted_at FROM notes WHERE id = :id"),
            {"id": "abc"},
        ).mappings().fetchone()
        assert row["deleted_at"] is not None


class TestRestore:
    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_restore_deleted_row(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO products (id, name, deleted_at, deleted_reason) VALUES (:id, :name, :ts, :reason)"),
            {"id": "abc", "name": "thing", "ts": "2026-01-01T00:00:00Z", "reason": "oops"},
        )
        result = restore(pf_connection, "products", "id", "abc")
        assert result is True
        row = pf_connection.execute(
            text("SELECT deleted_at, deleted_reason FROM products WHERE id = :id"),
            {"id": "abc"},
        ).mappings().fetchone()
        assert row["deleted_at"] is None
        assert row["deleted_reason"] is None

    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_restore_active_row(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO products (id, name) VALUES (:id, :name)"),
            {"id": "abc", "name": "thing"},
        )
        result = restore(pf_connection, "products", "id", "abc")
        assert result is False

    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_restore_nonexistent_row(self, pf_tables, pf_connection):
        result = restore(pf_connection, "products", "id", "missing")
        assert result is False

    @pytest.mark.pf_tables(_NOTES_TABLE)
    def test_restore_no_reason_column(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO notes (id, name, deleted_at) VALUES (:id, :name, :ts)"),
            {"id": "abc", "name": "thing", "ts": "2026-01-01T00:00:00Z"},
        )
        result = restore(pf_connection, "notes", "id", "abc", reason_column=None)
        assert result is True
        row = pf_connection.execute(
            text("SELECT deleted_at FROM notes WHERE id = :id"),
            {"id": "abc"},
        ).mappings().fetchone()
        assert row["deleted_at"] is None


class TestRoundTrip:
    @pytest.mark.pf_tables(_PRODUCTS_TABLE)
    def test_delete_then_restore(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO products (id, name) VALUES (:id, :name)"),
            {"id": "abc", "name": "thing"},
        )
        assert soft_delete(pf_connection, "products", "id", "abc", reason="test") is True
        assert restore(pf_connection, "products", "id", "abc") is True
        row = pf_connection.execute(
            text("SELECT deleted_at, deleted_reason FROM products WHERE id = :id"),
            {"id": "abc"},
        ).mappings().fetchone()
        assert row["deleted_at"] is None
        assert row["deleted_reason"] is None


class TestNotDeleted:
    def test_default(self):
        assert not_deleted() == "AND deleted_at IS NULL"

    def test_custom_column(self):
        assert not_deleted(column="removed_at") == "AND removed_at IS NULL"

    def test_where_prefix(self):
        assert not_deleted(prefix="WHERE ") == "WHERE deleted_at IS NULL"

    def test_empty_prefix(self):
        assert not_deleted(prefix="") == "deleted_at IS NULL"
