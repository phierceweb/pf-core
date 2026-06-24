"""Tests for pf_core.db.versioned_config — append-only latest-version-wins config.

Runtime behaviour is exercised against the SQLite ``pf_engine`` fixture; a scratch
``report_config`` table stands in for a consumer's versioned-config table.
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
_report_config = Table(
    "report_config", _md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("report_id", Integer),
    Column("version", Integer, nullable=False),
    Column("query", Text),
    Column("extra_context", Text),
)


@pytest.fixture()
def report_config(pf_engine):
    _md.create_all(pf_engine)
    return _report_config


class TestLatestVersion:
    def test_zero_when_empty(self, report_config):
        with transaction() as c:
            assert latest_version(c, "report_config", {"report_id": 1}) == 0

    def test_returns_max(self, report_config):
        with transaction() as c:
            append_version(c, "report_config", {"report_id": 1}, {"query": "a"})
            append_version(c, "report_config", {"report_id": 1}, {"query": "b"})
            assert latest_version(c, "report_config", {"report_id": 1}) == 2

    def test_is_scoped(self, report_config):
        with transaction() as c:
            append_version(c, "report_config", {"report_id": 1}, {"query": "a"})
            append_version(c, "report_config", {"report_id": 2}, {"query": "x"})
            assert latest_version(c, "report_config", {"report_id": 1}) == 1
            assert latest_version(c, "report_config", {"report_id": 2}) == 1


class TestGetLatest:
    def test_none_when_empty(self, report_config):
        with transaction() as c:
            assert get_latest(c, "report_config", {"report_id": 1}) is None

    def test_returns_highest_version(self, report_config):
        with transaction() as c:
            append_version(c, "report_config", {"report_id": 1}, {"query": "old"})
            append_version(c, "report_config", {"report_id": 1}, {"query": "new"})
            row = get_latest(c, "report_config", {"report_id": 1})
            assert row["version"] == 2
            assert row["query"] == "new"


class TestAppendVersion:
    def test_first_version_is_one(self, report_config):
        with transaction() as c:
            assert append_version(c, "report_config", {"report_id": 1}, {"query": "a"}) == 1

    def test_increments(self, report_config):
        with transaction() as c:
            assert append_version(c, "report_config", {"report_id": 1}, {"query": "a"}) == 1
            assert append_version(c, "report_config", {"report_id": 1}, {"query": "b"}) == 2

    def test_carry_forward_copies_unspecified(self, report_config):
        with transaction() as c:
            append_version(
                c, "report_config", {"report_id": 1},
                {"query": "q1", "extra_context": "ctx1"},
            )
            # v2 specifies only query; extra_context carries forward.
            append_version(
                c, "report_config", {"report_id": 1},
                {"query": "q2"}, carry_forward=True,
            )
            row = get_latest(c, "report_config", {"report_id": 1})
            assert row["version"] == 2
            assert row["query"] == "q2"
            assert row["extra_context"] == "ctx1"

    def test_no_carry_forward_leaves_unspecified_null(self, report_config):
        with transaction() as c:
            append_version(
                c, "report_config", {"report_id": 1},
                {"query": "q1", "extra_context": "ctx1"},
            )
            append_version(c, "report_config", {"report_id": 1}, {"query": "q2"})
            row = get_latest(c, "report_config", {"report_id": 1})
            assert row["extra_context"] is None

    def test_carry_forward_gets_fresh_id(self, report_config):
        with transaction() as c:
            append_version(c, "report_config", {"report_id": 1}, {"query": "q1"})
            v1 = get_latest(c, "report_config", {"report_id": 1})
            append_version(
                c, "report_config", {"report_id": 1}, {"query": "q2"},
                carry_forward=True,
            )
            v2 = get_latest(c, "report_config", {"report_id": 1})
            assert v2["id"] != v1["id"]  # id excluded from carry-forward


class TestGetLatestWithFallback:
    def test_uses_specific_when_present(self, report_config):
        with transaction() as c:
            append_version(c, "report_config", {"report_id": 5}, {"query": "specific"})
            append_version(c, "report_config", {"report_id": None}, {"query": "default"})
            row = get_latest_with_fallback(
                c, "report_config", {"report_id": 5}, {"report_id": None},
            )
            assert row["query"] == "specific"

    def test_falls_back_when_absent(self, report_config):
        with transaction() as c:
            append_version(c, "report_config", {"report_id": None}, {"query": "default"})
            row = get_latest_with_fallback(
                c, "report_config", {"report_id": 99}, {"report_id": None},
            )
            assert row["query"] == "default"

    def test_none_when_neither(self, report_config):
        with transaction() as c:
            assert get_latest_with_fallback(
                c, "report_config", {"report_id": 99}, {"report_id": None},
            ) is None


class TestIdentifierValidation:
    def test_bad_table_rejected(self, report_config):
        with transaction() as c:
            with pytest.raises(InvalidInputError):
                get_latest(c, "report_config; DROP TABLE x", {"report_id": 1})

    def test_bad_column_rejected(self, report_config):
        with transaction() as c:
            with pytest.raises(InvalidInputError):
                get_latest(c, "report_config", {"report_id) --": 1})

    def test_bad_version_col_rejected(self, report_config):
        with transaction() as c:
            with pytest.raises(InvalidInputError):
                latest_version(c, "report_config", {"report_id": 1}, version_col="v; DROP")
