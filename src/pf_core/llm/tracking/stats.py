"""
Aggregate analytics over ``llm_runs`` and its sidecars.

Each method takes a ``[since, until)`` half-open date range as required
positional args (no hidden defaults) and returns plain ``list[dict]`` rows
suitable for a CLI table or admin route.

Implemented in SQLAlchemy expressions so the same queries run on SQLite,
MySQL, and PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import case, distinct, func, select

from pf_core.db.repository import Repository
from pf_core.llm.tracking import schema as s


def _to_dt(value: dt.datetime | dt.date) -> dt.datetime:
    """Promote a ``date`` to a midnight ``datetime`` for comparison."""
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.combine(value, dt.time.min)


class LlmRunStatsRepo(Repository):
    """Aggregate read-side queries for cost, quality, and self-correction."""

    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------

    def cost_by_model(
        self,
        since: dt.datetime | dt.date,
        until: dt.datetime | dt.date,
    ) -> list[dict]:
        """Token + cost totals grouped by model name, over ``[since, until)``.

        Returns one row per model with: ``model``, ``runs``, ``billable_input``,
        ``cached_input``, ``output``, ``reasoning``, ``total_cost_usd``.
        """
        billable = func.coalesce(s.llm_runs.c.prompt_tokens, 0) - func.coalesce(
            s.llm_runs.c.cache_read_tokens, 0
        )
        stmt = (
            select(
                s.llm_models.c.name.label("model"),
                func.count().label("runs"),
                func.sum(billable).label("billable_input"),
                func.sum(func.coalesce(s.llm_runs.c.cache_read_tokens, 0)).label(
                    "cached_input"
                ),
                func.sum(func.coalesce(s.llm_runs.c.completion_tokens, 0)).label(
                    "output"
                ),
                func.sum(func.coalesce(s.llm_runs.c.reasoning_tokens, 0)).label(
                    "reasoning"
                ),
                func.sum(func.coalesce(s.llm_runs.c.cost_usd, 0)).label(
                    "total_cost_usd"
                ),
            )
            .select_from(s.llm_runs.join(s.llm_models, s.llm_runs.c.model_id == s.llm_models.c.id))
            .where(s.llm_runs.c.created_at >= _to_dt(since))
            .where(s.llm_runs.c.created_at < _to_dt(until))
            .group_by(s.llm_models.c.name)
            .order_by(func.sum(func.coalesce(s.llm_runs.c.cost_usd, 0)).desc())
        )
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [_normalize_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Quality
    # ------------------------------------------------------------------

    def halluc_rate_by_prompt(
        self,
        agent_type: str,
        since: dt.datetime | dt.date,
        until: dt.datetime | dt.date,
        *,
        validator: str = "url_hallucination",
    ) -> list[dict]:
        """Validator-failure rate grouped by ``(prompt_version, model)``.

        Filters runs to the given ``agent_type`` slug. Joins through the
        ``url_hallucination`` validator by default — override ``validator``
        to compute the failure rate of any other registered check.

        Returns: ``prompt_version``, ``model``, ``halluc_rate`` (0.0–1.0),
        ``runs``, ``cost_attributable``.
        """
        failed = case((s.llm_run_validations.c.passed.is_(False), 1.0), else_=0.0)
        stmt = (
            select(
                s.llm_prompts.c.version.label("prompt_version"),
                s.llm_models.c.name.label("model"),
                func.avg(failed).label("halluc_rate"),
                func.count().label("runs"),
                func.sum(func.coalesce(s.llm_runs.c.cost_usd, 0)).label(
                    "cost_attributable"
                ),
            )
            .select_from(
                s.llm_runs.join(
                    s.llm_prompts, s.llm_prompts.c.id == s.llm_runs.c.system_prompt_id
                )
                .join(s.llm_models, s.llm_models.c.id == s.llm_runs.c.model_id)
                .join(
                    s.llm_agent_types,
                    s.llm_agent_types.c.id == s.llm_runs.c.agent_type_id,
                )
                .join(
                    s.llm_run_validations,
                    (s.llm_run_validations.c.llm_run_id == s.llm_runs.c.id)
                    & (s.llm_run_validations.c.validator == validator),
                )
            )
            .where(s.llm_agent_types.c.slug == agent_type)
            .where(s.llm_runs.c.created_at >= _to_dt(since))
            .where(s.llm_runs.c.created_at < _to_dt(until))
            .group_by(s.llm_prompts.c.version, s.llm_models.c.name)
            .order_by(func.avg(failed).desc())
        )
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [_normalize_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Self-correction
    # ------------------------------------------------------------------

    def retry_success_rate(
        self,
        since: dt.datetime | dt.date,
        until: dt.datetime | dt.date,
    ) -> list[dict]:
        """For each correction relation, the child's success rate and combined cost.

        Considers ``retry``, ``critic``, ``refine``, ``fallback`` relations.
        Returns one row per relation with: ``relation``, ``chains``,
        ``child_success_rate``, ``avg_combined_cost``.
        """
        parent = s.llm_runs.alias("parent")
        child = s.llm_runs.alias("child")
        success = case((child.c.status == "success", 1.0), else_=0.0)
        combined = func.coalesce(child.c.cost_usd, 0) + func.coalesce(
            parent.c.cost_usd, 0
        )
        stmt = (
            select(
                s.llm_run_links.c.relation.label("relation"),
                func.count().label("chains"),
                func.avg(success).label("child_success_rate"),
                func.avg(combined).label("avg_combined_cost"),
            )
            .select_from(
                s.llm_run_links.join(
                    parent, parent.c.id == s.llm_run_links.c.parent_run_id
                ).join(child, child.c.id == s.llm_run_links.c.child_run_id)
            )
            .where(
                s.llm_run_links.c.relation.in_(
                    ["retry", "critic", "refine", "fallback"]
                )
            )
            .where(child.c.created_at >= _to_dt(since))
            .where(child.c.created_at < _to_dt(until))
            .group_by(s.llm_run_links.c.relation)
        )
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [_normalize_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Tag intersection — utility for cohort dashboards
    # ------------------------------------------------------------------

    def runs_with_all_tags(self, tags: list[str]) -> list[int]:
        """Return ``llm_run_id`` values that carry every tag in ``tags``."""
        if not tags:
            return []
        stmt = (
            select(s.llm_run_tags.c.llm_run_id)
            .where(s.llm_run_tags.c.tag.in_(tags))
            .group_by(s.llm_run_tags.c.llm_run_id)
            .having(func.count(distinct(s.llm_run_tags.c.tag)) == len(tags))
        )
        with self._tx() as conn:
            return [int(r[0]) for r in conn.execute(stmt).fetchall()]


def _normalize_row(row: Any) -> dict:
    """Convert SQLAlchemy ``RowMapping`` to plain dict; coerce ``Decimal`` to ``float``."""
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if hasattr(v, "as_tuple") and hasattr(v, "is_finite"):
            out[k] = float(v)
        else:
            out[k] = v
    return out
