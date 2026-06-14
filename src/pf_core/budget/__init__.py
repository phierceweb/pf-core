"""
Pre-call cost guardrails for LLM spending.

The kernel-safe surface (importable without ``pf-core[db]``) covers the
budget guard itself and YAML config loading:

    from pf_core.budget import (
        check_budget, project_cost, CostBudgetExceeded,
        compute_period_start, compute_period_end,
        load_yaml, clear_config_cache,
    )

The DB-backed surface (requires ``pf-core[db]``) is loaded lazily — the
import only triggers SQLAlchemy when an attribute is first accessed:

    from pf_core.budget import (
        BudgetRepo, BudgetSnapshotRepo, CostRateRepo, aggregate_spent,
        sync_budgets_from_yaml,
        refresh_snapshots, start_budget_refresh_loop,
        record_blocked_run, record_override,
        ALL_BUDGET_TABLES, llm_budgets, llm_budget_snapshots, llm_cost_rates,
    )

In a kernel-only install (no ``[db]`` extra), ``check_budget()`` and
``project_cost()`` still import and run, but ``project_cost()`` falls back
to ``0.0`` when no DB-backed cost rates are reachable, and any code path
that calls into the repos will surface ``ModuleNotFoundError: sqlalchemy``.

See ``docs/cost-budget.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Eager imports — kernel-safe modules with no top-level sqlalchemy coupling.
# These remain importable in a base ``pip install pf-core`` (no extras).
from pf_core.budget.check import (  # noqa: F401
    CostBudgetExceeded,
    check_budget,
    compute_period_end,
    compute_period_start,
    project_cost,
)
from pf_core.budget.config import (  # noqa: F401
    clear_config_cache,
    load_yaml,
)


# Lazy imports — these submodules pull SQLAlchemy at module top, so we
# defer them via PEP 562 ``__getattr__`` to keep the kernel install clean.
# The first attribute access triggers the import; subsequent accesses are
# free because we cache into ``globals()``.
_LAZY: dict[str, str] = {
    # Repos (DB-required)
    "BudgetRepo":           "pf_core.budget.repo",
    "BudgetSnapshotRepo":   "pf_core.budget.repo",
    "CostRateRepo":         "pf_core.budget.repo",
    "aggregate_spent":      "pf_core.budget.repo",
    # Audit logging (DB-required)
    "record_blocked_run":   "pf_core.budget.audit",
    "record_override":      "pf_core.budget.audit",
    # Snapshot / scheduler jobs (DB-required)
    "refresh_snapshots":    "pf_core.budget.snapshot_job",
    "start_budget_refresh_loop": "pf_core.budget.scheduler",
    # YAML→DB sync (DB-required path within the otherwise-kernel config module)
    "sync_budgets_from_yaml": "pf_core.budget.config",
    # Schema tables (DB-required — registers on shared MetaData on first access)
    "ALL_BUDGET_TABLES":    "pf_core.budget._schema",
    "llm_budgets":          "pf_core.budget._schema",
    "llm_budget_snapshots": "pf_core.budget._schema",
    "llm_cost_rates":       "pf_core.budget._schema",
}


def __getattr__(name: str):
    """Lazy import for DB-required attributes (PEP 562)."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    mod = importlib.import_module(target)
    value = getattr(mod, name)
    globals()[name] = value  # cache so subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    """Expose lazy attributes to ``dir()`` and IDE autocomplete."""
    return sorted(set(globals()) | set(_LAZY))


# Type-checker view of the lazy attributes — keeps ``mypy`` / IDEs happy
# without paying the import cost at runtime.
if TYPE_CHECKING:
    from pf_core.budget._schema import (  # noqa: F401
        ALL_BUDGET_TABLES,
        llm_budget_snapshots,
        llm_budgets,
        llm_cost_rates,
    )
    from pf_core.budget.audit import (  # noqa: F401
        record_blocked_run,
        record_override,
    )
    from pf_core.budget.config import sync_budgets_from_yaml  # noqa: F401
    from pf_core.budget.repo import (  # noqa: F401
        BudgetRepo,
        BudgetSnapshotRepo,
        CostRateRepo,
        aggregate_spent,
    )
    from pf_core.budget.scheduler import start_budget_refresh_loop  # noqa: F401
    from pf_core.budget.snapshot_job import refresh_snapshots  # noqa: F401


__all__ = [
    # Guard (eager)
    "check_budget",
    "project_cost",
    "CostBudgetExceeded",
    "compute_period_start",
    "compute_period_end",
    # Config — kernel-safe (eager)
    "load_yaml",
    "clear_config_cache",
    # Config — DB-backed (lazy)
    "sync_budgets_from_yaml",
    # Repos (lazy)
    "BudgetRepo",
    "BudgetSnapshotRepo",
    "CostRateRepo",
    "aggregate_spent",
    # Snapshot / scheduler jobs (lazy)
    "refresh_snapshots",
    "start_budget_refresh_loop",
    # Audit (lazy)
    "record_blocked_run",
    "record_override",
    # Schema (lazy)
    "ALL_BUDGET_TABLES",
    "llm_budgets",
    "llm_budget_snapshots",
    "llm_cost_rates",
]
