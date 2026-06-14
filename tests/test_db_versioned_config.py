"""Tests for pf_core.db.versioned_config — append-only latest-version-wins config.

Runtime behaviour is exercised against the SQLite ``pf_engine`` fixture; a scratch
``section_config`` table stands in for a consumer's versioned-config table.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Text

from pf_core.db import transaction
from pf_core.db.versioned_config import (
    append_version,
    get_latest,
    get_latest_with_fallback,
    latest_version,
)
from pf_core.exceptions import InvalidInputError

pytest_plugins = ["pf_core.testing.db_fixtures"]

_md = MetaData()
_section_config = Table(
    "section_config", _md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("section_id", Integer),
    Column("version", Integer, nullable=False),
    Column("beat_query", Text),
    Column("research_context", Text),
)


@pytest.fixture()
def section_config(pf_engine):
    _md.create_all(pf_engine)
    return _section_config


class TestLatestVersion:
    def test_zero_when_empty(self, section_config):
        with transaction() as c:
            assert latest_version(c, "section_config", {"section_id": 1}) == 0

    def test_returns_max(self, section_config):
        with transaction() as c:
            append_version(c, "section_config", {"section_id": 1}, {"beat_query": "a"})
            append_version(c, "section_config", {"section_id": 1}, {"beat_query": "b"})
            assert latest_version(c, "section_config", {"section_id": 1}) == 2

    def test_is_scoped(self, section_config):
        with transaction() as c:
            append_version(c, "section_config", {"section_id": 1}, {"beat_query": "a"})
            append_version(c, "section_config", {"section_id": 2}, {"beat_query": "x"})
            assert latest_version(c, "section_config", {"section_id": 1}) == 1
            assert latest_version(c, "section_config", {"section_id": 2}) == 1


class TestGetLatest:
    def test_none_when_empty(self, section_config):
        with transaction() as c:
            assert get_latest(c, "section_config", {"section_id": 1}) is None

    def test_returns_highest_version(self, section_config):
        with transaction() as c:
            append_version(c, "section_config", {"section_id": 1}, {"beat_query": "old"})
            append_version(c, "section_config", {"section_id": 1}, {"beat_query": "new"})
            row = get_latest(c, "section_config", {"section_id": 1})
            assert row["version"] == 2
            assert row["beat_query"] == "new"


class TestAppendVersion:
    def test_first_version_is_one(self, section_config):
        with transaction() as c:
            assert append_version(c, "section_config", {"section_id": 1}, {"beat_query": "a"}) == 1

    def test_increments(self, section_config):
        with transaction() as c:
            assert append_version(c, "section_config", {"section_id": 1}, {"beat_query": "a"}) == 1
            assert append_version(c, "section_config", {"section_id": 1}, {"beat_query": "b"}) == 2

    def test_carry_forward_copies_unspecified(self, section_config):
        with transaction() as c:
            append_version(
                c, "section_config", {"section_id": 1},
                {"beat_query": "q1", "research_context": "ctx1"},
            )
            # v2 specifies only beat_query; research_context carries forward.
            append_version(
                c, "section_config", {"section_id": 1},
                {"beat_query": "q2"}, carry_forward=True,
            )
            row = get_latest(c, "section_config", {"section_id": 1})
            assert row["version"] == 2
            assert row["beat_query"] == "q2"
            assert row["research_context"] == "ctx1"

    def test_no_carry_forward_leaves_unspecified_null(self, section_config):
        with transaction() as c:
            append_version(
                c, "section_config", {"section_id": 1},
                {"beat_query": "q1", "research_context": "ctx1"},
            )
            append_version(c, "section_config", {"section_id": 1}, {"beat_query": "q2"})
            row = get_latest(c, "section_config", {"section_id": 1})
            assert row["research_context"] is None

    def test_carry_forward_gets_fresh_id(self, section_config):
        with transaction() as c:
            append_version(c, "section_config", {"section_id": 1}, {"beat_query": "q1"})
            v1 = get_latest(c, "section_config", {"section_id": 1})
            append_version(
                c, "section_config", {"section_id": 1}, {"beat_query": "q2"},
                carry_forward=True,
            )
            v2 = get_latest(c, "section_config", {"section_id": 1})
            assert v2["id"] != v1["id"]  # id excluded from carry-forward


class TestGetLatestWithFallback:
    def test_uses_specific_when_present(self, section_config):
        with transaction() as c:
            append_version(c, "section_config", {"section_id": 5}, {"beat_query": "specific"})
            append_version(c, "section_config", {"section_id": None}, {"beat_query": "default"})
            row = get_latest_with_fallback(
                c, "section_config", {"section_id": 5}, {"section_id": None},
            )
            assert row["beat_query"] == "specific"

    def test_falls_back_when_absent(self, section_config):
        with transaction() as c:
            append_version(c, "section_config", {"section_id": None}, {"beat_query": "default"})
            row = get_latest_with_fallback(
                c, "section_config", {"section_id": 99}, {"section_id": None},
            )
            assert row["beat_query"] == "default"

    def test_none_when_neither(self, section_config):
        with transaction() as c:
            assert get_latest_with_fallback(
                c, "section_config", {"section_id": 99}, {"section_id": None},
            ) is None


class TestIdentifierValidation:
    def test_bad_table_rejected(self, section_config):
        with transaction() as c:
            with pytest.raises(InvalidInputError):
                get_latest(c, "section_config; DROP TABLE x", {"section_id": 1})

    def test_bad_column_rejected(self, section_config):
        with transaction() as c:
            with pytest.raises(InvalidInputError):
                get_latest(c, "section_config", {"section_id) --": 1})

    def test_bad_version_col_rejected(self, section_config):
        with transaction() as c:
            with pytest.raises(InvalidInputError):
                latest_version(c, "section_config", {"section_id": 1}, version_col="v; DROP")
