"""Tests for pf_core.db.json_compat — three-dialect SQL fragment helpers."""

from __future__ import annotations

import pytest

from pf_core.db.json_compat import (
    SUPPORTED_DIALECTS,
    autoinc_pk,
    bool_type,
    decimal_type,
    fk_int_type,
    insert_ignore_prefix,
    json_col_type,
    json_extract_sql,
    mediumtext_type,
    now_expr,
    on_update_now_clause,
    small_autoinc_pk,
    timestamp_type,
    tiny_autoinc_pk,
)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_col_type_per_dialect():
    assert json_col_type("mysql") == "JSON"
    assert json_col_type("postgresql") == "JSONB"
    assert json_col_type("sqlite") == "TEXT"


def test_json_extract_sql_per_dialect():
    assert json_extract_sql("mysql", "details", "ratio") == "JSON_EXTRACT(details, '$.ratio')"
    assert json_extract_sql("postgresql", "details", "ratio") == "details->>'ratio'"
    assert json_extract_sql("sqlite", "details", "ratio") == "json_extract(details, '$.ratio')"


def test_json_extract_dotted_path():
    assert json_extract_sql("mysql", "d", "a.b") == "JSON_EXTRACT(d, '$.a.b')"


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_now_expr_per_dialect():
    assert now_expr("mysql") == "CURRENT_TIMESTAMP"
    assert now_expr("postgresql") == "CURRENT_TIMESTAMP"
    assert "strftime" in now_expr("sqlite")


def test_timestamp_type_default_fractional():
    assert timestamp_type("mysql") == "TIMESTAMP(6)"
    assert timestamp_type("postgresql") == "TIMESTAMPTZ"
    assert timestamp_type("sqlite") == "TEXT"


def test_timestamp_type_no_fractional():
    assert timestamp_type("mysql", fractional=False) == "TIMESTAMP"
    assert timestamp_type("postgresql", fractional=False) == "TIMESTAMPTZ"


# ---------------------------------------------------------------------------
# Numeric / scalar
# ---------------------------------------------------------------------------


def test_autoinc_pk_per_dialect():
    assert "AUTO_INCREMENT" in autoinc_pk("mysql")
    assert "IDENTITY" in autoinc_pk("postgresql")
    assert autoinc_pk("sqlite") == "INTEGER PRIMARY KEY"


def test_small_autoinc_pk_per_dialect():
    assert "SMALLINT" in small_autoinc_pk("mysql")
    assert "AUTO_INCREMENT" in small_autoinc_pk("mysql")
    assert "SMALLINT" in small_autoinc_pk("postgresql")


def test_tiny_autoinc_pk_mysql_uses_tinyint():
    assert "TINYINT" in tiny_autoinc_pk("mysql")
    # Postgres has no TINYINT — must fall back to SMALLINT
    assert "SMALLINT" in tiny_autoinc_pk("postgresql")


def test_fk_int_type_sizes():
    assert fk_int_type("mysql", size="int") == "INT"
    assert fk_int_type("mysql", size="small") == "SMALLINT UNSIGNED"
    assert fk_int_type("mysql", size="tiny") == "TINYINT UNSIGNED"
    assert fk_int_type("postgresql", size="small") == "SMALLINT"
    assert fk_int_type("sqlite", size="tiny") == "INTEGER"


def test_mediumtext_only_on_mysql():
    assert mediumtext_type("mysql") == "MEDIUMTEXT"
    assert mediumtext_type("postgresql") == "TEXT"
    assert mediumtext_type("sqlite") == "TEXT"


def test_decimal_type_per_dialect():
    assert decimal_type("mysql") == "DECIMAL(10,6)"
    assert decimal_type("postgresql") == "NUMERIC(10,6)"
    assert decimal_type("sqlite") == "REAL"


def test_decimal_type_custom_precision():
    assert decimal_type("mysql", precision=12, scale=4) == "DECIMAL(12,4)"
    assert decimal_type("postgresql", precision=12, scale=4) == "NUMERIC(12,4)"


def test_bool_type_per_dialect():
    assert bool_type("mysql") == "TINYINT(1)"
    assert bool_type("postgresql") == "BOOLEAN"
    assert bool_type("sqlite") == "INTEGER"


# ---------------------------------------------------------------------------
# DDL clauses
# ---------------------------------------------------------------------------


def test_on_update_now_clause_only_on_mysql():
    assert "ON UPDATE" in on_update_now_clause("mysql")
    assert on_update_now_clause("postgresql") == ""
    assert on_update_now_clause("sqlite") == ""


def test_insert_ignore_prefix_per_dialect():
    assert insert_ignore_prefix("mysql") == "INSERT IGNORE"
    assert insert_ignore_prefix("sqlite") == "INSERT OR IGNORE"
    assert insert_ignore_prefix("postgresql") == "INSERT"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn",
    [
        json_col_type,
        now_expr,
        timestamp_type,
        autoinc_pk,
        small_autoinc_pk,
        tiny_autoinc_pk,
        mediumtext_type,
        decimal_type,
        bool_type,
        on_update_now_clause,
        insert_ignore_prefix,
    ],
)
def test_unsupported_dialect_raises(fn):
    with pytest.raises(ValueError, match="unsupported dialect"):
        fn("oracle")


def test_supported_dialects_constant():
    assert set(SUPPORTED_DIALECTS) == {"mysql", "postgresql", "sqlite"}


def test_unsupported_dialect_in_extract():
    with pytest.raises(ValueError):
        json_extract_sql("oracle", "col", "path")
