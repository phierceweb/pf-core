"""
Reusable SQL queries for the LLM admin sub-app — the public facade.

All queries use SQLAlchemy Core for dialect portability (SQLite, MySQL,
Postgres). Each function takes a date/time window where relevant and
returns plain ``list[dict]`` or ``dict`` suitable for template / JSON.

Split by page domain; import from here (``queries as q``):

- ``_queries_runs`` — dashboard KPIs, runs list + filters + detail
- ``_queries_jobs`` — jobs list + detail
- ``_queries_spend`` — cost by model / agent, cache hit rate, budget state
- ``_queries_util`` — window parsing + row normalization
"""

from __future__ import annotations

from pf_core.web.llm_admin._queries_jobs import (
    count_jobs,
    job_detail,
    list_jobs,
)
from pf_core.web.llm_admin._queries_runs import (
    count_runs,
    dashboard_kpis,
    list_runs,
    run_detail,
    top_agents_by_cost,
)
from pf_core.web.llm_admin._queries_spend import (
    blocked_runs_24h,
    cache_hit_rate_by_agent,
    cost_by_agent,
    cost_by_model,
    list_budgets_with_spend,
    top_cache_entries,
)
from pf_core.web.llm_admin._queries_util import (
    default_window,
    parse_window,
)

__all__ = [
    "blocked_runs_24h",
    "cache_hit_rate_by_agent",
    "cost_by_agent",
    "cost_by_model",
    "count_jobs",
    "count_runs",
    "dashboard_kpis",
    "default_window",
    "job_detail",
    "list_budgets_with_spend",
    "list_jobs",
    "list_runs",
    "parse_window",
    "run_detail",
    "top_agents_by_cost",
    "top_cache_entries",
]
