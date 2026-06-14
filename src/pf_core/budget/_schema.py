"""
SQLAlchemy schema for pf-core cost budgets.

Defines three framework-owned tables that enforce per-agent, per-job, and
per-tag cost caps on LLM calls:

- ``llm_budgets`` — authoritative budget definitions (one row per scope+period)
- ``llm_budget_snapshots`` — periodic aggregate cache for fast pre-call checks
- ``llm_cost_rates`` — per-model price list for projecting call cost

Shares the ``metadata`` object from ``pf_core.llm.tracking.schema`` so a
single ``metadata.create_all()`` creates tracking, jobs, cache, and budget
tables in one pass.

See ``docs/cost-budget.md`` for the full reference.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
)

from pf_core.llm.tracking.schema import (
    _JSON,
    _PK_SMALL,
    _FK_SMALL,
    _TIMESTAMP_US,
    _server_now,
    metadata,
)

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

llm_budgets = Table(
    "llm_budgets",
    metadata,
    Column("id", _PK_SMALL, primary_key=True, autoincrement=True),
    Column("scope_kind", String(32), nullable=False),
    Column("scope_value", String(128), nullable=True),
    Column("period", String(16), nullable=False),
    Column("limit_usd", Numeric(12, 4), nullable=False),
    Column("soft_thresholds", _JSON, nullable=True),
    Column("hard_cap", Boolean, nullable=False, server_default="1"),
    Column("action", String(32), nullable=False, server_default="block"),
    Column("enabled", Boolean, nullable=False, server_default="1"),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    Column("updated_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    UniqueConstraint(
        "scope_kind", "scope_value", "period", name="uq_llm_budgets_scope_period"
    ),
    Index("idx_llm_budgets_enabled", "enabled"),
)
"""Budget definitions. scope_kind ∈ {global, agent, job_kind, job_id, tag}."""


llm_budget_snapshots = Table(
    "llm_budget_snapshots",
    metadata,
    Column(
        "budget_id",
        _FK_SMALL,
        ForeignKey("llm_budgets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("period_start", Date, nullable=False),
    Column("spent_usd", Numeric(12, 4), nullable=False, server_default="0"),
    Column("run_count", Integer, nullable=False, server_default="0"),
    Column(
        "last_updated", _TIMESTAMP_US, nullable=False, server_default=_server_now()
    ),
    PrimaryKeyConstraint("budget_id", "period_start", name="pk_llm_budget_snapshots"),
    Index("idx_llm_budget_snapshots_period", "period_start"),
)
"""Periodic aggregate cache. Not source of truth — rebuilt from llm_runs."""


llm_cost_rates = Table(
    "llm_cost_rates",
    metadata,
    Column(
        "model_id",
        _FK_SMALL,
        ForeignKey("llm_models.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("input_per_1k", Numeric(8, 6), nullable=False),
    Column("output_per_1k", Numeric(8, 6), nullable=False),
    Column("cache_read_per_1k", Numeric(8, 6), nullable=True),
    Column("cache_write_per_1k", Numeric(8, 6), nullable=True),
    Column("reasoning_per_1k", Numeric(8, 6), nullable=True),
    Column("effective_from", Date, nullable=False),
    Column("effective_to", Date, nullable=True),
    PrimaryKeyConstraint(
        "model_id", "effective_from", name="pk_llm_cost_rates"
    ),
    Index("idx_llm_cost_rates_model_eff", "model_id", "effective_from"),
)
"""Per-model price list. Multiple rows per model allowed (versioned pricing)."""


# ---------------------------------------------------------------------------
# Public table list (in dependency order)
# ---------------------------------------------------------------------------

ALL_BUDGET_TABLES = (
    llm_budgets,
    llm_budget_snapshots,
    llm_cost_rates,
)
