"""
Thread-safe model name → ID resolver.

Many projects track which LLM model was used for each operation. This module
provides a thread-safe, cached resolver that does a dialect-agnostic
insert-or-ignore + SELECT to get or create a model ID. The insert goes through
:func:`pf_core.db.insert_ignore`, so SQLite, PostgreSQL, and MySQL/MariaDB all
work — never the MySQL-only ``INSERT IGNORE`` (a syntax error on PostgreSQL).

Usage::

    from pf_core.db.models import resolve_model_id

    model_id = resolve_model_id("anthropic/claude-sonnet-4.6")
"""

from __future__ import annotations

import threading

from sqlalchemy import Column, Integer, MetaData, Table, Text, text

from pf_core.db.connection import transaction
from pf_core.db.upsert import insert_ignore

_cache: dict[str, int] = {}
_lock = threading.Lock()

# Minimal metadata mirroring the application's ``models`` table — an
# auto-increment ``id`` PK and a unique ``name``. Used only to compile a
# dialect-correct INSERT via ``insert_ignore``; pf-core never emits DDL for it.
_models_md = MetaData()
_models = Table(
    "models",
    _models_md,
    Column("id", Integer, primary_key=True),
    Column("name", Text, unique=True),
)


def resolve_model_id(model_name: str) -> int | None:
    """Return the models.id for model_name, inserting a new row if needed.

    Thread-safe with a process-level cache so the INSERT fires at most once
    per model name per process.

    Requires a `models` table with columns: id (auto-increment), name (unique).
    """
    if not model_name:
        return None

    if model_name in _cache:
        return _cache[model_name]

    with _lock:
        # Double-check after acquiring lock
        if model_name in _cache:
            return _cache[model_name]

        with transaction() as conn:
            # Dialect-agnostic insert-or-ignore on the unique ``name`` — emits
            # ON CONFLICT DO NOTHING (SQLite/Postgres) or a no-op ON DUPLICATE
            # KEY UPDATE (MySQL/MariaDB), never the Postgres-invalid INSERT IGNORE.
            insert_ignore(conn, _models, {"name": model_name}, conflict=["name"])
            row = conn.execute(
                text("SELECT id FROM models WHERE name = :name"),
                {"name": model_name},
            ).fetchone()

        model_id = row[0] if row else None
        if model_id is not None:
            _cache[model_name] = model_id
        return model_id


def clear_cache() -> None:
    """Clear the model ID cache (useful for testing)."""
    with _lock:
        _cache.clear()
