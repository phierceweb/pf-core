"""
SQLAlchemy schema for the LLM response cache.

Defines two tables registered on the shared ``pf_core.llm.tracking.schema``
metadata so ``metadata.create_all()`` creates them in the same pass:

- ``llm_cache_entries`` — exact-cache index (one row per canonical input_hash)
- ``llm_embeddings`` — semantic vector index (opt-in, added in v0.9.1)

See ``docs/llm-cache.md`` for design rationale and usage.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql

# Reuse shared metadata and helpers so a single create_all() builds everything.
from pf_core.llm.tracking.schema import (
    _FK_BIG,
    _FK_INT,
    _FK_SMALL,
    _JSON,
    _LARGE_TEXT,
    _PK_INT,
    _TIMESTAMP_US,
    _server_now,
    metadata,
)

# Unsigned int variants for hit_count and embedding_dim
_UINT = Integer().with_variant(mysql.INTEGER(unsigned=True), "mysql")
_UINT_SMALL = Integer().with_variant(mysql.SMALLINT(unsigned=True), "mysql")


llm_cache_entries = Table(
    "llm_cache_entries",
    metadata,
    Column("id", _PK_INT, primary_key=True, autoincrement=True),
    # input_hash is a SHA256 hex digest — always 64 chars
    Column("input_hash", String(64), nullable=False),
    Column(
        "agent_type_id",
        _FK_SMALL,
        ForeignKey("llm_agent_types.id"),
        nullable=False,
    ),
    Column(
        "model_id",
        _FK_SMALL,
        ForeignKey("llm_models.id"),
        nullable=False,
    ),
    Column(
        "source_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Denormalized response — avoids payload join on every hit
    Column("parsed_output", _JSON, nullable=True),
    Column("raw_response", _LARGE_TEXT, nullable=True),
    # Eviction / analytics
    Column("hit_count", _UINT, nullable=False, server_default="0"),
    Column("last_hit_at", _TIMESTAMP_US, nullable=True),
    Column("expires_at", _TIMESTAMP_US, nullable=True),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    UniqueConstraint("input_hash", name="uq_llm_cache_input_hash"),
    Index("idx_llm_cache_agent_expires", "agent_type_id", "expires_at"),
    Index("idx_llm_cache_source_run", "source_run_id"),
)
"""Exact-cache index. One row per canonical (input_hash) → response mapping."""


llm_embeddings = Table(
    "llm_embeddings",
    metadata,
    Column("id", _PK_INT, primary_key=True, autoincrement=True),
    Column(
        "cache_entry_id",
        _FK_INT,
        ForeignKey("llm_cache_entries.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "agent_type_id",
        _FK_SMALL,
        ForeignKey("llm_agent_types.id"),
        nullable=False,
    ),
    Column("embedding_model", String(128), nullable=False),
    Column("embedding_dim", _UINT_SMALL, nullable=False),
    Column("embedding_text", _LARGE_TEXT, nullable=False),
    # Packed float32 vector bytes — dialect-specific interpretation at query time
    Column("embedding_vector", LargeBinary, nullable=True),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    Index("idx_llm_embeddings_agent_model", "agent_type_id", "embedding_model"),
)
"""Semantic embedding index. Opt-in; populated only when semantic=true for an agent."""


ALL_CACHE_TABLES = (llm_cache_entries, llm_embeddings)
