"""Shared test fixtures — uses pf_core.testing plugins.

The pf_engine, pf_connection, and pf_tables fixtures come from
pf_core.testing.db_fixtures (opt-in plugin, requires the ``[db]`` extra).
The base plugin pf_core.testing.fixtures (auto-registered via pytest11
entry point) provides pf_app_client only.

This conftest defines the project-level schema that pf_tables will create.
"""

from __future__ import annotations

import pytest

# DB fixtures are opt-in. pf-core's own tests use them, so we explicitly
# load the DB plugin here. Consumers without the [db] extra don't need this.
pytest_plugins = ["pf_core.testing.db_fixtures"]


@pytest.fixture(autouse=True)
def pf_schema():
    """Schema for pf-core's own tests.

    Consumer projects define their own pf_schema fixture with their tables.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            data TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_type_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            prompt TEXT,
            effective_date TEXT,
            UNIQUE(agent_type_id, version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_type_id INTEGER,
            model_id INTEGER,
            prompt_version INTEGER,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            duration_ms INTEGER DEFAULT 0,
            status TEXT DEFAULT 'success',
            error TEXT,
            context_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """,
    ]
