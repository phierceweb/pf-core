"""Cross-dialect column-type variants for table definitions.

The framework's own tables (tracking, jobs, cache, budget) are built from
these, and consumers that extend framework tables or define adjacent ones
(the ``framework_ddl(only=...)`` pattern — see docs/testing.md) need the
same variants so their DDL matches on MySQL, PostgreSQL, and SQLite.

Base types are ``Integer`` on every variant so SQLite emits literal
``INTEGER`` — required for the rowid alias that powers SQLite autoincrement
(a ``BigInteger`` base compiles to ``BIGINT`` and silently breaks it).
Autoincrement columns and their FKs are ``UNSIGNED`` on MySQL; Postgres has
no unsigned integer type; SQLite uses ``INTEGER`` regardless.
"""

from __future__ import annotations

from sqlalchemy import Integer, SmallInteger, Text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression
from sqlalchemy.types import JSON, TIMESTAMP

# Microsecond-precision UTC timestamp. MySQL stores TIMESTAMP(6); Postgres
# stores TIMESTAMPTZ; SQLite stores ISO 8601 in TEXT (handled by SQLAlchemy).
TIMESTAMP_US = (
    TIMESTAMP()
    .with_variant(mysql.TIMESTAMP(fsp=6), "mysql")
    .with_variant(postgresql.TIMESTAMP(timezone=True), "postgresql")
)

# Large variable text. MySQL gets MEDIUMTEXT (~16MB); Postgres and SQLite use
# native TEXT (effectively unlimited).
LARGE_TEXT = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

# JSON column. Postgres uses JSONB (indexable); MySQL uses native JSON;
# SQLite stores serialized JSON in TEXT (SQLAlchemy handles encoding).
# none_as_null: Python None stores as SQL NULL, not JSON 'null' — keeps
# IS NULL predicates truthful (use sqlalchemy.JSON.NULL to store JSON null).
JSON_ = JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)

# PK variants: SMALL for reference tables, BIG for hot tables where id
# exhaustion must never be a practical concern.
PK_INT = Integer().with_variant(mysql.INTEGER(unsigned=True), "mysql")
PK_SMALL = (
    Integer()
    .with_variant(mysql.SMALLINT(unsigned=True), "mysql")
    .with_variant(postgresql.SMALLINT(), "postgresql")
)
PK_BIG = (
    Integer()
    .with_variant(mysql.BIGINT(unsigned=True), "mysql")
    .with_variant(postgresql.BIGINT(), "postgresql")
)

FK_INT = Integer().with_variant(mysql.INTEGER(unsigned=True), "mysql")
FK_SMALL = (
    SmallInteger()
    .with_variant(mysql.SMALLINT(unsigned=True), "mysql")
    .with_variant(postgresql.SMALLINT(), "postgresql")
)
FK_BIG = (
    Integer()
    .with_variant(mysql.BIGINT(unsigned=True), "mysql")
    .with_variant(postgresql.BIGINT(), "postgresql")
)


class server_now(expression.FunctionElement):
    """Cross-dialect server-side timestamp default with microsecond precision.

    MySQL requires ``CURRENT_TIMESTAMP(6)`` to match ``TIMESTAMP(6)`` columns
    under STRICT_TRANS_TABLES; plain ``CURRENT_TIMESTAMP`` is rejected as an
    invalid default. Postgres and SQLite accept ``CURRENT_TIMESTAMP`` with no
    precision argument.
    """

    type = TIMESTAMP()
    inherit_cache = True


@compiles(server_now, "mysql")
def _mysql_server_now(element, compiler, **kw):  # noqa: ARG001
    return "CURRENT_TIMESTAMP(6)"


@compiles(server_now)
def _default_server_now(element, compiler, **kw):  # noqa: ARG001
    return "CURRENT_TIMESTAMP"


__all__ = [
    "FK_BIG",
    "FK_INT",
    "FK_SMALL",
    "JSON_",
    "LARGE_TEXT",
    "PK_BIG",
    "PK_INT",
    "PK_SMALL",
    "TIMESTAMP_US",
    "server_now",
]
