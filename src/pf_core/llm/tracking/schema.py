"""
SQLAlchemy schema for LLM run tracking.

Defines the eleven ``llm_*`` tables that form the tracking backbone:
``llm_models``, ``llm_agent_types``, ``llm_prompts``, ``llm_runs``,
``llm_run_payloads``, ``llm_run_configs``, ``llm_run_validations``,
``llm_run_outcomes``, ``llm_run_links``, ``llm_run_tags``,
``llm_run_metrics``.

The metadata is the source of truth for both Alembic autogeneration in
consumer projects and pf-core's repos. Type variants (MySQL ``UNSIGNED``,
``MEDIUMTEXT``; Postgres ``JSONB``) are declared via ``with_variant`` so
the same metadata creates correct DDL on MySQL, PostgreSQL, and SQLite.

Usage::

    from pf_core.llm.tracking.schema import metadata, llm_runs
    metadata.create_all(engine)

See ``docs/llm-tracking.md`` for the implementation reference.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression
from sqlalchemy.types import TIMESTAMP, Boolean

from pf_core.db.types import (
    FK_BIG,
    FK_INT,
    FK_SMALL,
    JSON_,
    LARGE_TEXT,
    PK_BIG,
    PK_INT,
    PK_SMALL,
    TIMESTAMP_US,
    server_now,
)

# ---------------------------------------------------------------------------
# Type variants — the public home is pf_core.db.types. The underscored
# aliases stay: framework siblings and consumer schema extensions import them.
# ---------------------------------------------------------------------------

_TIMESTAMP_US = TIMESTAMP_US
_LARGE_TEXT = LARGE_TEXT
_JSON = JSON_
_PK_INT = PK_INT
_PK_SMALL = PK_SMALL
_PK_BIG = PK_BIG
_FK_INT = FK_INT
_FK_SMALL = FK_SMALL
_FK_BIG = FK_BIG
_server_now = server_now

# Cost in USD. Six decimal places to capture sub-cent OpenRouter values.
_COST_USD = Numeric(10, 6)


class _server_now_minus_seconds(expression.FunctionElement):
    """Cross-dialect ``CURRENT_TIMESTAMP - INTERVAL N SECOND``.

    Use when a cutoff needs to be computed server-side — e.g. worker lease
    expiry, retention purge thresholds. Computing the cutoff in Python with
    ``datetime.now(timezone.utc) - timedelta(...)`` and binding it as a
    WHERE-clause value is unsafe on MySQL: TIMESTAMP columns are stored in
    the session time zone and aware-UTC bind values silently skew the
    comparison by the session offset.

    Holding the subtraction on the server guarantees left- and right-hand
    sides of the comparison share a time-zone frame whatever it is.

    Construct with a positive integer number of seconds; the expression
    evaluates to "that many seconds before now" in the DB's own clock.
    """

    type = TIMESTAMP()
    inherit_cache = True

    def __init__(self, seconds: int) -> None:
        super().__init__()
        if seconds < 0:
            raise ValueError(
                "_server_now_minus_seconds requires a non-negative int"
            )
        self._seconds = int(seconds)


@compiles(_server_now_minus_seconds, "mysql")
def _mysql_server_now_minus(element, compiler, **kw):  # noqa: ARG001
    return f"(CURRENT_TIMESTAMP(6) - INTERVAL {element._seconds} SECOND)"


@compiles(_server_now_minus_seconds, "postgresql")
def _pg_server_now_minus(element, compiler, **kw):  # noqa: ARG001
    return f"(CURRENT_TIMESTAMP - INTERVAL '{element._seconds} seconds')"


@compiles(_server_now_minus_seconds, "sqlite")
def _sqlite_server_now_minus(element, compiler, **kw):  # noqa: ARG001
    # SQLite's CURRENT_TIMESTAMP is naive UTC; datetime() with a modifier
    # returns a string of the same shape so comparisons against stored
    # TEXT timestamps work correctly.
    return f"datetime('now', '-{element._seconds} seconds')"


@compiles(_server_now_minus_seconds)
def _default_server_now_minus(element, compiler, **kw):  # noqa: ARG001
    # Fallback for any unknown dialect. SQL standard interval syntax.
    return f"(CURRENT_TIMESTAMP - INTERVAL '{element._seconds} seconds')"


# ---------------------------------------------------------------------------
# MetaData
# ---------------------------------------------------------------------------

metadata = MetaData()


# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------

llm_models = Table(
    "llm_models",
    metadata,
    Column("id", _PK_SMALL, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False, unique=True),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
)
"""Canonical model slug as sent to the provider (e.g. ``claude-opus-4-7``)."""


llm_agent_types = Table(
    "llm_agent_types",
    metadata,
    Column("id", _PK_SMALL, primary_key=True, autoincrement=True),
    Column("slug", String(64), nullable=False, unique=True),
    Column("description", Text, nullable=True),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
)
"""Pipeline-role classification for runs (``drafter``, ``reviewer``, ...)."""


llm_prompts = Table(
    "llm_prompts",
    metadata,
    Column("id", _PK_SMALL, primary_key=True, autoincrement=True),
    Column(
        "agent_type_id",
        _FK_SMALL,
        ForeignKey("llm_agent_types.id"),
        nullable=False,
    ),
    Column("part", String(16), nullable=False),
    Column("version", SmallInteger, nullable=False, server_default="1"),
    Column("content", _LARGE_TEXT, nullable=False),
    Column("effective_date", Date, nullable=False),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    UniqueConstraint(
        "agent_type_id", "part", "version", name="uq_llm_prompts_agent_part_version"
    ),
    Index("idx_llm_prompts_agent_part", "agent_type_id", "part"),
    CheckConstraint(
        "part IN ('system', 'user', 'full')", name="ck_llm_prompts_part"
    ),
)
"""Versioned prompt templates. ``part`` is one of system/user/full."""


# ---------------------------------------------------------------------------
# Hot table — one row per LLM invocation
# ---------------------------------------------------------------------------

llm_runs = Table(
    "llm_runs",
    metadata,
    Column("id", _PK_BIG, primary_key=True, autoincrement=True),
    Column(
        "agent_type_id",
        _FK_SMALL,
        ForeignKey("llm_agent_types.id"),
        nullable=False,
    ),
    Column(
        "model_id", _FK_SMALL, ForeignKey("llm_models.id"), nullable=False
    ),
    Column(
        "system_prompt_id",
        _FK_SMALL,
        ForeignKey("llm_prompts.id"),
        nullable=True,
    ),
    Column(
        "user_prompt_id",
        _FK_SMALL,
        ForeignKey("llm_prompts.id"),
        nullable=True,
    ),
    # Sampling parameters — captured for replay fidelity
    Column("temperature", Float, nullable=True),
    Column("top_p", Float, nullable=True),
    Column("max_tokens", Integer, nullable=True),
    Column("seed", Integer, nullable=True),
    Column("stop_sequences", _JSON, nullable=True),
    # Provider attribution
    Column("provider", String(32), nullable=True),
    Column("model_fingerprint", String(128), nullable=True),
    # Token accounting
    Column("prompt_tokens", Integer, nullable=True),
    Column("completion_tokens", Integer, nullable=True),
    Column("cache_read_tokens", Integer, nullable=True),
    Column("cache_write_tokens", Integer, nullable=True),
    Column("reasoning_tokens", Integer, nullable=True),
    # Cost + latency
    Column("cost_usd", _COST_USD, nullable=True),
    Column("duration_ms", Integer, nullable=True),
    # Outcome
    Column("items_out", SmallInteger, nullable=True),
    Column("status", String(32), nullable=False, server_default="success"),
    Column("error", Text, nullable=True),
    Column("error_class", String(64), nullable=True),
    Column("error_code", String(32), nullable=True),
    Column("http_status", SmallInteger, nullable=True),
    # Dedup / replay key
    Column("input_hash", String(64), nullable=True),
    # Attribution to a parent job (framework-to-framework FK; see
    # pf_core.jobs). SET NULL so deleting a job preserves cost history.
    Column(
        "job_id",
        _FK_INT,
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    Index("idx_llm_runs_created_at", "created_at"),
    Index("idx_llm_runs_agent_type_created", "agent_type_id", "created_at"),
    Index("idx_llm_runs_model_created", "model_id", "created_at"),
    Index("idx_llm_runs_status_created", "status", "created_at"),
    Index("idx_llm_runs_input_hash", "input_hash"),
    Index("idx_llm_runs_fingerprint", "model_fingerprint"),
    Index("idx_llm_runs_job_id", "job_id"),
)
"""Hot analytics table. One row = one LLM invocation."""


# ---------------------------------------------------------------------------
# Sidecar — cold storage for forensic payloads
# ---------------------------------------------------------------------------

llm_run_payloads = Table(
    "llm_run_payloads",
    metadata,
    Column(
        "llm_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("rendered_system", _LARGE_TEXT, nullable=True),
    Column("rendered_user", _LARGE_TEXT, nullable=True),
    Column("raw_response", _LARGE_TEXT, nullable=True),
    Column("parsed_output", _JSON, nullable=True),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
)
"""Cold-tier 1:1 sidecar for rendered prompts and raw response text."""


# ---------------------------------------------------------------------------
# Attachment tables — many-of per run
# ---------------------------------------------------------------------------

llm_run_configs = Table(
    "llm_run_configs",
    metadata,
    Column(
        "llm_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("config_kind", String(64), nullable=False),
    Column("config_id", Integer, nullable=False),
    PrimaryKeyConstraint("llm_run_id", "config_kind", name="pk_llm_run_configs"),
    Index("idx_llm_run_configs_kind_id", "config_kind", "config_id"),
)
"""Soft-FK snapshot of project-owned config rows used by the run."""


llm_run_validations = Table(
    "llm_run_validations",
    metadata,
    Column(
        "llm_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("validator", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("passed", Boolean, nullable=False),
    Column("details", _JSON, nullable=True),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    PrimaryKeyConstraint("llm_run_id", "validator", name="pk_llm_run_validations"),
    Index("idx_llm_run_validations_validator_passed", "validator", "passed"),
)
"""Quality signals captured at call time (one validator per run)."""


llm_run_outcomes = Table(
    "llm_run_outcomes",
    metadata,
    Column(
        "llm_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("outcome_kind", String(64), nullable=False),
    Column("score", Float, nullable=True),
    Column("notes", Text, nullable=True),
    Column(
        "recorded_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()
    ),
    PrimaryKeyConstraint("llm_run_id", "outcome_kind", name="pk_llm_run_outcomes"),
    Index("idx_llm_run_outcomes_kind_recorded", "outcome_kind", "recorded_at"),
)
"""Downstream outcomes backfilled by reviewer actions."""


llm_run_links = Table(
    "llm_run_links",
    metadata,
    Column(
        "parent_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "child_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("relation", String(32), nullable=False),
    Column("created_at", _TIMESTAMP_US, nullable=False, server_default=_server_now()),
    PrimaryKeyConstraint(
        "parent_run_id", "child_run_id", "relation", name="pk_llm_run_links"
    ),
    Index("idx_llm_run_links_child_relation", "child_run_id", "relation"),
)
"""Run-to-run relations: retry, critic, refine, fallback, subroutine, meta_analysis."""


llm_run_tags = Table(
    "llm_run_tags",
    metadata,
    Column(
        "llm_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("tag", String(64), nullable=False),
    PrimaryKeyConstraint("llm_run_id", "tag", name="pk_llm_run_tags"),
    Index("idx_llm_run_tags_tag", "tag"),
)
"""Free-form colon-namespaced labels: ``env:prod``, ``eval:golden_v2``."""


llm_run_metrics = Table(
    "llm_run_metrics",
    metadata,
    Column(
        "llm_run_id",
        _FK_BIG,
        ForeignKey("llm_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("metric_name", String(64), nullable=False),
    Column("metric_value", Float, nullable=False),
    PrimaryKeyConstraint("llm_run_id", "metric_name", name="pk_llm_run_metrics"),
    Index("idx_llm_run_metrics_name_val", "metric_name", "metric_value"),
)
"""Numeric per-run signals (categorical signals belong in tags)."""


# ---------------------------------------------------------------------------
# Public table list (in dependency order — safe for ``create_all`` and drops)
# ---------------------------------------------------------------------------

ALL_TABLES = (
    llm_models,
    llm_agent_types,
    llm_prompts,
    llm_runs,
    llm_run_payloads,
    llm_run_configs,
    llm_run_validations,
    llm_run_outcomes,
    llm_run_links,
    llm_run_tags,
    llm_run_metrics,
)
