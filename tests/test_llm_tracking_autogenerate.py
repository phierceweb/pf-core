"""Smoke tests for Alembic autogenerate against ``pf_core.llm.tracking.metadata``.

Consumer projects write their own Alembic migrations that target our
metadata. These tests catch two failure modes before those migrations
are written:

1. **Empty DB → metadata** diff must contain a ``CREATE TABLE`` op for every
   table in ``ALL_TABLES``. If one is missing, a consumer autogenerate run
   would silently skip it.
2. **Populated DB (via ``metadata.create_all``) → metadata** diff must be
   empty. Any spurious op here means an autogenerate run against an already-
   migrated database would emit a no-op migration, which indicates drift
   between the metadata and whatever DDL SQLAlchemy actually emits.

The tests run on SQLite (in-memory). MySQL/Postgres drift gets caught during
the consumer migration work, where the real DB backends are exercised.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from pf_core.llm.tracking import ALL_TABLES, metadata


@pytest.fixture()
def engine() -> Engine:
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    return eng


def _diff_ops(engine: Engine) -> list:
    """Return the autogenerate diff between live DB and pf-core metadata."""
    with engine.connect() as conn:
        mc = MigrationContext.configure(conn)
        return compare_metadata(mc, metadata)


def _add_table_names(diff: list) -> set[str]:
    """Extract table names from ``('add_table', Table)`` diff entries."""
    names: set[str] = set()
    for op in diff:
        if isinstance(op, tuple) and op and op[0] == "add_table":
            names.add(op[1].name)
    return names


def test_autogenerate_against_empty_db_creates_every_llm_table(engine):
    """Every table in ``ALL_TABLES`` must appear as an ``add_table`` op."""
    diff = _diff_ops(engine)
    expected = {t.name for t in ALL_TABLES}
    assert expected.issubset(_add_table_names(diff)), (
        f"autogenerate would miss: {expected - _add_table_names(diff)}"
    )


def test_autogenerate_against_fully_migrated_db_is_empty(engine):
    """Once all tables exist, there must be no drift between DB and metadata."""
    metadata.create_all(engine)
    diff = _diff_ops(engine)
    # Filter to only our tables — a consumer project's conftest might add
    # unrelated tables we don't care about here, though this test uses a
    # clean engine so this is really just defense-in-depth.
    our_names = {t.name for t in ALL_TABLES}
    relevant = [
        op
        for op in diff
        if _op_touches_any(op, our_names)
    ]
    assert relevant == [], f"unexpected drift in pf-core metadata: {relevant}"


def _op_touches_any(op, names: set[str]) -> bool:
    """True if an autogenerate op touches any table in ``names``."""
    if isinstance(op, tuple) and op:
        kind = op[0]
        if kind in {"add_table", "remove_table"}:
            return op[1].name in names
        if kind in {
            "add_column",
            "remove_column",
            "modify_nullable",
            "modify_type",
            "modify_default",
            "add_index",
            "remove_index",
            "add_constraint",
            "remove_constraint",
        }:
            # Positional schema differs by op; scan args for a known name.
            return any(
                getattr(a, "name", None) in names
                or getattr(a, "table_name", None) in names
                for a in op
                if a is not None
            )
    elif isinstance(op, list):
        return any(_op_touches_any(sub, names) for sub in op)
    return False
