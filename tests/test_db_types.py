"""Tests for pf_core.db.types — public cross-dialect column-type variants."""

from __future__ import annotations

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable


def _ddl(col_type, dialect_mod) -> str:
    md = MetaData()
    t = Table("t_probe", md, Column("c", col_type))
    return str(CreateTable(t).compile(dialect=dialect_mod.dialect()))


class TestTypeVariants:
    def test_pk_int_unsigned_on_mysql_plain_integer_on_sqlite(self):
        from pf_core.db import types as T

        assert "UNSIGNED" in _ddl(T.PK_INT, mysql)
        assert "INTEGER" in _ddl(T.PK_INT, sqlite)

    def test_timestamp_us_variants(self):
        from pf_core.db import types as T

        assert "TIMESTAMP(6)" in _ddl(T.TIMESTAMP_US, mysql)
        assert "TIMESTAMP WITH TIME ZONE" in _ddl(T.TIMESTAMP_US, postgresql)

    def test_large_text_is_mediumtext_on_mysql(self):
        from pf_core.db import types as T

        assert "MEDIUMTEXT" in _ddl(T.LARGE_TEXT, mysql)

    def test_json_is_jsonb_on_postgres(self):
        from pf_core.db import types as T

        assert "JSONB" in _ddl(T.JSON_, postgresql)

    def test_server_now_compiles_per_dialect(self):
        from pf_core.db.types import server_now

        assert (
            str(server_now().compile(dialect=mysql.dialect()))
            == "CURRENT_TIMESTAMP(6)"
        )
        assert (
            str(server_now().compile(dialect=sqlite.dialect()))
            == "CURRENT_TIMESTAMP"
        )


class TestSchemaSharesObjects:
    def test_tracking_schema_uses_the_public_objects(self):
        # The underscored names must stay importable (consumers pin them)
        # and be the same objects — one definition, public home.
        from pf_core.db import types as T
        from pf_core.llm.tracking import schema as s

        assert s._PK_INT is T.PK_INT
        assert s._PK_SMALL is T.PK_SMALL
        assert s._PK_BIG is T.PK_BIG
        assert s._FK_INT is T.FK_INT
        assert s._FK_SMALL is T.FK_SMALL
        assert s._FK_BIG is T.FK_BIG
        assert s._TIMESTAMP_US is T.TIMESTAMP_US
        assert s._LARGE_TEXT is T.LARGE_TEXT
        assert s._JSON is T.JSON_
        assert s._server_now is T.server_now
