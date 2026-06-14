"""Tests for pf_core.utils.ids."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from pf_core.exceptions import InvalidInputError, PreconditionError
from pf_core.utils.ids import allocate_id, generate_id


class TestAllocateIdIdentifierValidation:
    """table/column are interpolated into SQL, so they must be plain identifiers."""

    def test_rejects_bad_table(self):
        with pytest.raises(InvalidInputError):
            allocate_id(None, table="users; DROP TABLE x")

    def test_rejects_bad_column(self):
        with pytest.raises(InvalidInputError):
            allocate_id(None, table="widgets", column="id) OR 1=1--")

    def test_rejects_empty_table(self):
        with pytest.raises(InvalidInputError):
            allocate_id(None, table="")


class TestGenerateId:
    def test_default_length(self):
        result = generate_id()
        assert len(result) == 12

    def test_custom_length(self):
        result = generate_id(size=8)
        assert len(result) == 8

    def test_url_safe_characters(self):
        for _ in range(50):
            result = generate_id()
            assert re.match(r"^[A-Za-z0-9_-]+$", result)

    def test_uniqueness(self):
        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100

    def test_respects_env_var(self, monkeypatch):
        monkeypatch.setenv("ID_LENGTH", "20")
        result = generate_id()
        assert len(result) == 20

    def test_env_var_clamped_low(self, monkeypatch):
        monkeypatch.setenv("ID_LENGTH", "3")
        result = generate_id()
        assert len(result) == 8  # clamped to minimum

    def test_env_var_clamped_high(self, monkeypatch):
        monkeypatch.setenv("ID_LENGTH", "100")
        result = generate_id()
        assert len(result) == 36  # clamped to maximum

    def test_explicit_size_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ID_LENGTH", "20")
        result = generate_id(size=8)
        assert len(result) == 8


_WIDGETS_TABLE = "CREATE TABLE widgets (id TEXT PRIMARY KEY, name TEXT)"


class TestAllocateId:
    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_generates_unique_id(self, pf_tables, pf_connection):
        result = allocate_id(pf_connection, table="widgets")
        assert len(result) == 12
        assert re.match(r"^[A-Za-z0-9_-]+$", result)

    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_preferred_id_used_when_available(self, pf_tables, pf_connection):
        result = allocate_id(pf_connection, table="widgets", preferred="my-custom-id")
        assert result == "my-custom-id"

    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_preferred_id_skipped_when_taken(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO widgets (id, name) VALUES (:id, :name)"),
            {"id": "taken-id", "name": "existing"},
        )
        result = allocate_id(pf_connection, table="widgets", preferred="taken-id")
        assert result != "taken-id"
        assert len(result) == 12

    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_preferred_none_generates(self, pf_tables, pf_connection):
        result = allocate_id(pf_connection, table="widgets", preferred=None)
        assert len(result) == 12

    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_preferred_empty_generates(self, pf_tables, pf_connection):
        result = allocate_id(pf_connection, table="widgets", preferred="  ")
        assert len(result) == 12

    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_custom_column(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO widgets (id, name) VALUES (:id, :name)"),
            {"id": "x", "name": "test-name"},
        )
        result = allocate_id(pf_connection, table="widgets", column="name", preferred="test-name")
        assert result != "test-name"

    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_custom_size(self, pf_tables, pf_connection):
        result = allocate_id(pf_connection, table="widgets", size=20)
        assert len(result) == 20

    @pytest.mark.pf_tables(_WIDGETS_TABLE)
    def test_raises_after_max_attempts(self, pf_tables, pf_connection, monkeypatch):
        # Insert an item, then make generate_id always return that same ID
        pf_connection.execute(
            text("INSERT INTO widgets (id, name) VALUES (:id, :name)"),
            {"id": "collision", "name": "x"},
        )
        monkeypatch.setattr("pf_core.utils.ids.generate_id", lambda size=None: "collision")
        with pytest.raises(PreconditionError, match="Could not allocate"):
            allocate_id(pf_connection, table="widgets", max_attempts=3)
