"""
Repositories for budget, snapshot, and cost-rate tables.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from pf_core.db.repository import Repository
from pf_core.budget._schema import (
    llm_budget_snapshots,
    llm_budgets,
    llm_cost_rates,
)
from pf_core.llm.tracking._resolvers import resolve_llm_model_id


class BudgetRepo(Repository):
    """Reads and writes :data:`llm_budgets` rows."""

    def list_enabled(self) -> list[dict]:
        """Return all enabled budgets as dicts."""
        with self._tx() as conn:
            rows = conn.execute(
                select(llm_budgets).where(llm_budgets.c.enabled.is_(True))
            ).mappings().fetchall()
        return [dict(r) for r in rows]

    def find(
        self, *, scope_kind: str, scope_value: str | None, period: str
    ) -> dict | None:
        """Return the budget row matching (scope_kind, scope_value, period)."""
        with self._tx() as conn:
            where = [
                llm_budgets.c.scope_kind == scope_kind,
                llm_budgets.c.period == period,
            ]
            if scope_value is None:
                where.append(llm_budgets.c.scope_value.is_(None))
            else:
                where.append(llm_budgets.c.scope_value == scope_value)
            row = conn.execute(
                select(llm_budgets).where(and_(*where))
            ).mappings().fetchone()
        return dict(row) if row else None

    def list_for_scopes(
        self,
        *,
        agent_type: str | None = None,
        job_kind: str | None = None,
        job_id: int | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        """Return all enabled budgets matching any of the given scopes.

        Always includes ``scope_kind='global'`` budgets.
        """
        pairs: list[tuple[str, str | None]] = [("global", None)]
        if agent_type:
            pairs.append(("agent", agent_type))
        if job_kind:
            pairs.append(("job_kind", job_kind))
        if job_id is not None:
            pairs.append(("job_id", str(job_id)))
        for tag in tags or []:
            pairs.append(("tag", tag))

        clauses = []
        for kind, value in pairs:
            if value is None:
                clauses.append(
                    and_(
                        llm_budgets.c.scope_kind == kind,
                        llm_budgets.c.scope_value.is_(None),
                    )
                )
            else:
                clauses.append(
                    and_(
                        llm_budgets.c.scope_kind == kind,
                        llm_budgets.c.scope_value == value,
                    )
                )

        if not clauses:
            return []

        with self._tx() as conn:
            rows = conn.execute(
                select(llm_budgets)
                .where(llm_budgets.c.enabled.is_(True))
                .where(or_(*clauses))
            ).mappings().fetchall()
        return [dict(r) for r in rows]

    def sync_from_desired(self, desired: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert each desired row; disable any enabled row not in desired.

        A "desired" row is ``{scope_kind, scope_value, period, limit_usd,
        soft_thresholds, action}``.

        Returns counts: ``{"inserted": N, "updated": N, "disabled": N}``.
        """
        inserted = 0
        updated = 0
        disabled = 0
        desired_keys: set[tuple[str, str | None, str]] = set()

        with self._tx() as conn:
            for row in desired:
                key = (row["scope_kind"], row.get("scope_value"), row["period"])
                desired_keys.add(key)

                where = [
                    llm_budgets.c.scope_kind == row["scope_kind"],
                    llm_budgets.c.period == row["period"],
                ]
                if row.get("scope_value") is None:
                    where.append(llm_budgets.c.scope_value.is_(None))
                else:
                    where.append(llm_budgets.c.scope_value == row["scope_value"])

                existing = conn.execute(
                    select(llm_budgets.c.id).where(and_(*where))
                ).fetchone()

                values = {
                    "limit_usd": row["limit_usd"],
                    "soft_thresholds": row.get("soft_thresholds"),
                    "action": row.get("action", "block"),
                    "enabled": True,
                    "updated_at": func.now(),
                }
                if existing:
                    conn.execute(
                        update(llm_budgets)
                        .where(llm_budgets.c.id == existing[0])
                        .values(**values)
                    )
                    updated += 1
                else:
                    conn.execute(
                        llm_budgets.insert().values(
                            scope_kind=row["scope_kind"],
                            scope_value=row.get("scope_value"),
                            period=row["period"],
                            **values,
                        )
                    )
                    inserted += 1

            # Disable anything enabled but not in desired
            existing_rows = conn.execute(
                select(
                    llm_budgets.c.id,
                    llm_budgets.c.scope_kind,
                    llm_budgets.c.scope_value,
                    llm_budgets.c.period,
                ).where(llm_budgets.c.enabled.is_(True))
            ).fetchall()

            for r in existing_rows:
                key = (r.scope_kind, r.scope_value, r.period)
                if key not in desired_keys:
                    conn.execute(
                        update(llm_budgets)
                        .where(llm_budgets.c.id == r.id)
                        .values(enabled=False, updated_at=func.now())
                    )
                    disabled += 1

        return {"inserted": inserted, "updated": updated, "disabled": disabled}


class BudgetSnapshotRepo(Repository):
    """Reads and writes :data:`llm_budget_snapshots` rows."""

    def get(self, *, budget_id: int, period_start: dt.date) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                select(llm_budget_snapshots).where(
                    and_(
                        llm_budget_snapshots.c.budget_id == budget_id,
                        llm_budget_snapshots.c.period_start == period_start,
                    )
                )
            ).mappings().fetchone()
        return dict(row) if row else None

    def upsert(
        self,
        *,
        budget_id: int,
        period_start: dt.date,
        spent_usd: float,
        run_count: int,
    ) -> None:
        with self._tx() as conn:
            existing = conn.execute(
                select(llm_budget_snapshots.c.budget_id).where(
                    and_(
                        llm_budget_snapshots.c.budget_id == budget_id,
                        llm_budget_snapshots.c.period_start == period_start,
                    )
                )
            ).fetchone()
            if existing:
                conn.execute(
                    update(llm_budget_snapshots)
                    .where(
                        and_(
                            llm_budget_snapshots.c.budget_id == budget_id,
                            llm_budget_snapshots.c.period_start == period_start,
                        )
                    )
                    .values(
                        spent_usd=spent_usd,
                        run_count=run_count,
                        last_updated=func.now(),
                    )
                )
            else:
                conn.execute(
                    llm_budget_snapshots.insert().values(
                        budget_id=budget_id,
                        period_start=period_start,
                        spent_usd=spent_usd,
                        run_count=run_count,
                    )
                )


class CostRateRepo(Repository):
    """Reads and writes :data:`llm_cost_rates` rows."""

    def get_effective(
        self, *, model: str, on_date: dt.date | None = None
    ) -> dict | None:
        """Return the cost rate row in effect for *model* on *on_date*.

        Falls back to ``today`` when *on_date* omitted.
        """
        if on_date is None:
            on_date = dt.date.today()
        model_id = resolve_llm_model_id(model)
        with self._tx() as conn:
            row = conn.execute(
                select(llm_cost_rates)
                .where(llm_cost_rates.c.model_id == model_id)
                .where(llm_cost_rates.c.effective_from <= on_date)
                .where(
                    or_(
                        llm_cost_rates.c.effective_to.is_(None),
                        llm_cost_rates.c.effective_to >= on_date,
                    )
                )
                .order_by(llm_cost_rates.c.effective_from.desc())
            ).mappings().fetchone()
        return dict(row) if row else None

    def upsert(
        self,
        *,
        model: str,
        input_per_1k: float,
        output_per_1k: float,
        effective_from: dt.date | None = None,
        effective_to: dt.date | None = None,
        cache_read_per_1k: float | None = None,
        cache_write_per_1k: float | None = None,
        reasoning_per_1k: float | None = None,
    ) -> None:
        if effective_from is None:
            effective_from = dt.date.today()
        model_id = resolve_llm_model_id(model)

        with self._tx() as conn:
            existing = conn.execute(
                select(llm_cost_rates.c.model_id).where(
                    and_(
                        llm_cost_rates.c.model_id == model_id,
                        llm_cost_rates.c.effective_from == effective_from,
                    )
                )
            ).fetchone()

            values = {
                "input_per_1k": input_per_1k,
                "output_per_1k": output_per_1k,
                "cache_read_per_1k": cache_read_per_1k,
                "cache_write_per_1k": cache_write_per_1k,
                "reasoning_per_1k": reasoning_per_1k,
                "effective_to": effective_to,
            }
            if existing:
                conn.execute(
                    update(llm_cost_rates)
                    .where(
                        and_(
                            llm_cost_rates.c.model_id == model_id,
                            llm_cost_rates.c.effective_from == effective_from,
                        )
                    )
                    .values(**values)
                )
            else:
                conn.execute(
                    llm_cost_rates.insert().values(
                        model_id=model_id,
                        effective_from=effective_from,
                        **values,
                    )
                )


# ---------------------------------------------------------------------------
# Spent aggregation
# ---------------------------------------------------------------------------


def aggregate_spent(
    *,
    budget: dict,
    period_start: dt.date,
    period_end: dt.date,
    conn=None,
) -> tuple[float, int]:
    """Sum ``llm_runs.cost_usd`` for the rows matching a budget's scope in [period_start, period_end).

    Excludes rows with ``status IN ('cache_hit', 'budget_blocked')``.
    """
    from pf_core.llm.tracking.schema import (
        llm_agent_types,
        llm_run_tags,
        llm_runs,
    )

    def _run(c):
        q = select(
            func.coalesce(func.sum(llm_runs.c.cost_usd), 0),
            func.count(llm_runs.c.id),
        ).where(
            and_(
                llm_runs.c.created_at >= period_start,
                llm_runs.c.created_at < period_end,
                llm_runs.c.status.notin_(["cache_hit", "budget_blocked"]),
            )
        )

        scope_kind = budget["scope_kind"]
        scope_value = budget.get("scope_value")

        if scope_kind == "global":
            pass
        elif scope_kind == "agent":
            q = q.join(
                llm_agent_types, llm_runs.c.agent_type_id == llm_agent_types.c.id
            ).where(llm_agent_types.c.slug == scope_value)
        elif scope_kind == "job_kind":
            from pf_core.jobs._schema import jobs

            q = q.join(jobs, llm_runs.c.job_id == jobs.c.id).where(
                jobs.c.kind == scope_value
            )
        elif scope_kind == "job_id":
            q = q.where(llm_runs.c.job_id == int(scope_value))
        elif scope_kind == "tag":
            q = q.join(
                llm_run_tags, llm_runs.c.id == llm_run_tags.c.llm_run_id
            ).where(llm_run_tags.c.tag == scope_value)
        else:
            return (0.0, 0)

        total, n = c.execute(q).fetchone()
        return (float(total or 0), int(n or 0))

    if conn is not None:
        return _run(conn)
    from pf_core.db.connection import transaction

    with transaction() as c:
        return _run(c)
