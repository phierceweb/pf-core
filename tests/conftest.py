"""Shared test fixtures — uses pf_core.testing plugins.

The pf_engine, pf_connection, and pf_tables fixtures come from
pf_core.testing.db_fixtures (opt-in plugin, requires the ``[db]`` extra).
The base plugin pf_core.testing.fixtures (auto-registered via pytest11
entry point) provides pf_app_client only.

This conftest defines the project-level schema that pf_tables will create,
compiled for whichever backend ``PF_TEST_DATABASE_URL`` selects.
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

# DB fixtures are opt-in. pf-core's own tests use them, so we explicitly
# load the DB plugin here. Consumers without the [db] extra don't need this.
pytest_plugins = ["pf_core.testing.db_fixtures", "pytester"]

_metadata = MetaData()

Table(
    "items",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False, unique=True),
    Column("data", Text),
    Column("status", String(32), server_default="active"),
    Column("created_at", DateTime, server_default=func.now()),
)
Table(
    "models",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False, unique=True),
)
Table(
    "agent_types",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("slug", String(255), nullable=False, unique=True),
)
Table(
    "prompt_versions",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_type_id", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("prompt", Text),
    Column("effective_date", String(32)),
    UniqueConstraint("agent_type_id", "version"),
)
Table(
    "llm_calls",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_type_id", Integer),
    Column("model_id", Integer),
    Column("prompt_version", Integer),
    Column("prompt_tokens", Integer, server_default="0"),
    Column("completion_tokens", Integer, server_default="0"),
    Column("cost_usd", Float, server_default="0"),
    Column("duration_ms", Integer, server_default="0"),
    Column("status", String(32), server_default="success"),
    Column("error", Text),
    Column("context_json", Text),
    Column("created_at", DateTime, server_default=func.now()),
)


@pytest.fixture(autouse=True)
def pf_schema():
    """Schema for pf-core's own tests.

    Consumer projects define their own pf_schema fixture with their tables.
    """
    from pf_core.testing.db_fixtures import metadata_ddl

    return metadata_ddl(_metadata)
