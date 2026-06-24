"""
GoldenSetRepo — curate and query the eval golden set.

The golden set is data, not code. Membership is a tag (``eval:golden_v2``).
Entering the set is a reviewer action (``llm_run_outcomes.outcome_kind='golden_approved'``).

Example::

    from pf_core.eval import GoldenSetRepo

    repo = GoldenSetRepo()
    repo.add(run_id=1042, version="golden_v2", notes="canonical high-quality draft")
    repo.add(
        run_id=8891,
        version="golden_v2",
        ground_truth={"expected_score": 85.0},
        notes="edge case: ambiguous input",
    )
    members = repo.list(version="golden_v2", agent_type="drafter")
    repo.remove(run_id=1042, version="golden_v2")
"""

from __future__ import annotations

from sqlalchemy import select

from pf_core.db.repository import Repository
from pf_core.llm.tracking import schema as s
from pf_core.llm.tracking.subrepos import LlmRunOutcomeRepo


class GoldenSetRepo(Repository):
    """CRUD for the golden set (backed by llm_run_tags, llm_run_outcomes, llm_run_metrics)."""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(
        self,
        run_id: int,
        *,
        version: str,
        ground_truth: dict[str, float] | None = None,
        notes: str | None = None,
    ) -> None:
        """Add a run to the golden set.

        Writes the ``eval:<version>`` tag, a ``golden_approved`` outcome (score 1.0),
        and optional ground-truth metrics. Idempotent: re-adding rewrites the tag
        and outcome but preserves existing metrics unless overridden.

        Args:
            run_id: ``llm_runs.id`` of the run to promote.
            version: Golden set version string (e.g. ``"golden_v2"``).
            ground_truth: Optional dict of ``metric_name → value`` stored as
                ``llm_run_metrics`` rows. Used by metric gates.
            notes: Free-text note written to the ``golden_approved`` outcome.
        """
        tag = f"eval:{version}"
        with self._tx() as conn:
            conn.execute(
                s.llm_run_tags.delete().where(
                    (s.llm_run_tags.c.llm_run_id == run_id)
                    & (s.llm_run_tags.c.tag == tag)
                )
            )
            conn.execute(s.llm_run_tags.insert().values(llm_run_id=run_id, tag=tag))

        LlmRunOutcomeRepo().record(
            run_id, outcome_kind="golden_approved", score=1.0, notes=notes
        )

        if ground_truth:
            with self._tx() as conn:
                for name in ground_truth:
                    conn.execute(
                        s.llm_run_metrics.delete().where(
                            (s.llm_run_metrics.c.llm_run_id == run_id)
                            & (s.llm_run_metrics.c.metric_name == name)
                        )
                    )
                conn.execute(
                    s.llm_run_metrics.insert(),
                    [
                        {
                            "llm_run_id": run_id,
                            "metric_name": name,
                            "metric_value": float(value),
                        }
                        for name, value in ground_truth.items()
                    ],
                )

    def remove(self, run_id: int, *, version: str) -> None:
        """Remove the golden tag. Outcomes + metrics remain as history."""
        tag = f"eval:{version}"
        with self._tx() as conn:
            conn.execute(
                s.llm_run_tags.delete().where(
                    (s.llm_run_tags.c.llm_run_id == run_id)
                    & (s.llm_run_tags.c.tag == tag)
                )
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list(
        self,
        *,
        version: str,
        agent_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """List all golden run rows for a version, optionally filtered by agent_type.

        Returns dicts with ``llm_runs`` columns plus ``agent_type_slug`` and
        ``model_name``, ordered by ``created_at DESC``.
        """
        tag = f"eval:{version}"
        stmt = (
            select(
                s.llm_runs,
                s.llm_agent_types.c.slug.label("agent_type_slug"),
                s.llm_models.c.name.label("model_name"),
            )
            .join(s.llm_run_tags, s.llm_runs.c.id == s.llm_run_tags.c.llm_run_id)
            .join(s.llm_agent_types, s.llm_runs.c.agent_type_id == s.llm_agent_types.c.id)
            .join(s.llm_models, s.llm_runs.c.model_id == s.llm_models.c.id)
            .where(s.llm_run_tags.c.tag == tag)
        )
        if agent_type is not None:
            stmt = stmt.where(s.llm_agent_types.c.slug == agent_type)
        stmt = stmt.order_by(s.llm_runs.c.created_at.desc()).limit(limit)
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [dict(r) for r in rows]

    def seed_from_outcomes(
        self,
        *,
        version: str,
        outcome_kind: str,
        agent_type: str | None = None,
        limit: int = 200,
        dry_run: bool = False,
    ) -> list[int]:
        """Bulk-seed the golden set from runs that have a given outcome_kind.

        Queries ``llm_run_outcomes`` for matching rows, orders by most recent,
        and calls :meth:`add` on each. Idempotent — re-seeding the same runs
        is safe.

        Args:
            version: Golden set version (e.g. ``"golden_v1"``).
            outcome_kind: Outcome to query (e.g. ``"draft_accepted"``).
            agent_type: Optional agent type slug to narrow the candidate pool.
            limit: Maximum number of runs to promote.
            dry_run: If ``True``, return candidate run_ids without promoting.

        Returns:
            List of run_ids promoted (or candidate ids when ``dry_run=True``).
        """
        stmt = (
            select(s.llm_run_outcomes.c.llm_run_id)
            .join(s.llm_runs, s.llm_runs.c.id == s.llm_run_outcomes.c.llm_run_id)
            .where(s.llm_run_outcomes.c.outcome_kind == outcome_kind)
        )
        if agent_type is not None:
            stmt = stmt.join(
                s.llm_agent_types,
                s.llm_runs.c.agent_type_id == s.llm_agent_types.c.id,
            ).where(s.llm_agent_types.c.slug == agent_type)
        stmt = stmt.order_by(s.llm_run_outcomes.c.recorded_at.desc()).limit(limit)

        with self._tx() as conn:
            run_ids = [row[0] for row in conn.execute(stmt).fetchall()]

        if not dry_run:
            for run_id in run_ids:
                self.add(run_id, version=version)

        return run_ids

    def get_payload(self, run_id: int) -> dict | None:
        """Return the ``llm_run_payloads`` sidecar for a run, or ``None``."""
        with self._tx() as conn:
            row = conn.execute(
                select(s.llm_run_payloads).where(
                    s.llm_run_payloads.c.llm_run_id == run_id
                )
            ).mappings().fetchone()
        return dict(row) if row else None

    def get_ground_truth(self, run_id: int) -> dict[str, float]:
        """Return ``metric_name → metric_value`` for a run's ground-truth annotations."""
        with self._tx() as conn:
            rows = conn.execute(
                select(s.llm_run_metrics).where(
                    s.llm_run_metrics.c.llm_run_id == run_id
                )
            ).mappings().fetchall()
        return {r["metric_name"]: float(r["metric_value"]) for r in rows}
