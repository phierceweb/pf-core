"""
LLM run tracking — schema, repos, stats, decorator, and retention.

This package exposes the tracking backbone used to record every LLM call
made by a consumer project.

Public surface::

    from pf_core.llm.tracking import (
        # Schema
        metadata, ALL_TABLES,
        llm_runs, llm_models, llm_agent_types, llm_prompts,
        llm_run_payloads, llm_run_configs, llm_run_validations,
        llm_run_outcomes, llm_run_links, llm_run_tags, llm_run_metrics,

        # Resolvers
        resolve_llm_model_id, resolve_agent_type_id, resolve_prompt_id,
        clear_resolver_caches,

        # Hash
        compute_input_hash,

        # Repos
        LlmRunRepo,
        LlmRunOutcomeRepo, LlmRunValidationRepo, LlmRunLinkRepo,
        LlmRunStatsRepo,

        # Decorator
        track_run,

        # Retention
        purge_old_payloads,
    )

See ``docs/llm-tracking.md`` for the implementation reference.
"""

from pf_core.llm.tracking.schema import (  # noqa: F401
    ALL_TABLES,
    llm_agent_types,
    llm_models,
    llm_prompts,
    llm_run_configs,
    llm_run_links,
    llm_run_metrics,
    llm_run_outcomes,
    llm_run_payloads,
    llm_run_tags,
    llm_run_validations,
    llm_runs,
    metadata,
)

# Register jobs tables on the shared metadata. llm_runs.job_id FK-references
# jobs.id, so metadata.create_all() needs jobs loaded to emit correct DDL.
from pf_core.jobs import _schema as _job_schema  # noqa: F401, E402

# Register cache tables on the shared metadata. llm_cache_entries.source_run_id
# FK-references llm_runs.id, so metadata.create_all() needs cache loaded too.
from pf_core.llm.cache import _schema as _cache_schema  # noqa: F401, E402

# Register budget tables on the shared metadata. llm_cost_rates.model_id
# FK-references llm_models.id, so the tables need to share metadata too.
from pf_core.budget import _schema as _budget_schema  # noqa: F401, E402

from pf_core.llm.tracking._resolvers import (  # noqa: F401
    clear_caches as clear_resolver_caches,
    resolve_agent_type_id,
    resolve_llm_model_id,
    resolve_prompt_id,
)
from pf_core.llm.tracking.decorator import track_run  # noqa: F401
from pf_core.llm.tracking.purge import purge_old_payloads  # noqa: F401
from pf_core.llm.tracking.repo import LlmRunRepo, compute_input_hash  # noqa: F401
from pf_core.llm.tracking.stats import LlmRunStatsRepo  # noqa: F401
from pf_core.llm.tracking.subrepos import (  # noqa: F401
    LlmRunLinkRepo,
    LlmRunOutcomeRepo,
    LlmRunValidationRepo,
)

__all__ = [
    # Schema
    "ALL_TABLES",
    "llm_agent_types",
    "llm_models",
    "llm_prompts",
    "llm_run_configs",
    "llm_run_links",
    "llm_run_metrics",
    "llm_run_outcomes",
    "llm_run_payloads",
    "llm_run_tags",
    "llm_run_validations",
    "llm_runs",
    "metadata",
    # Resolvers
    "clear_resolver_caches",
    "resolve_agent_type_id",
    "resolve_llm_model_id",
    "resolve_prompt_id",
    # Hash
    "compute_input_hash",
    # Repos
    "LlmRunLinkRepo",
    "LlmRunOutcomeRepo",
    "LlmRunRepo",
    "LlmRunStatsRepo",
    "LlmRunValidationRepo",
    # Decorator
    "track_run",
    # Retention
    "purge_old_payloads",
]
