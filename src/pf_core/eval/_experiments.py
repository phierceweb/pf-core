"""Paired baseline-vs-candidate comparison over scored replay runs.

Backs :meth:`pf_core.eval.EvalRunner.compare_experiments`. See
``docs/eval-harness.md``.
"""

from __future__ import annotations

from pf_core.db.repository import Repository
from pf_core.exceptions import InvalidInputError, PreconditionError


def _exact_tag_rows(rows: list, tag: str) -> list:
    """Drop rows whose stored tag is not *tag* byte-for-byte.

    MySQL's default collation makes SQL ``=`` case-insensitive; tag matching is
    exact on every dialect.
    """
    return [r for r in rows if r["tag"] == tag]


def _fetch_tag_rows(repo: Repository, tag: str, agent_type: str) -> list:
    """Scored replay rows the server matches for *tag*.

    Ordered by child run id ascending so the dict build keeps the latest
    replay's score when a golden has duplicates. Callers must filter through
    :func:`_exact_tag_rows`.
    """
    from sqlalchemy import and_, select

    import pf_core.llm.tracking.schema as s

    stmt = (
        select(
            s.llm_run_links.c.parent_run_id.label("golden_id"),
            s.llm_run_outcomes.c.score,
            s.llm_run_tags.c.tag,
        )
        .join(
            s.llm_run_tags,
            s.llm_run_tags.c.llm_run_id == s.llm_run_links.c.child_run_id,
        )
        .join(
            s.llm_run_outcomes,
            and_(
                s.llm_run_outcomes.c.llm_run_id == s.llm_run_links.c.child_run_id,
                s.llm_run_outcomes.c.outcome_kind == "eval_score",
            ),
        )
        .join(
            s.llm_runs,
            s.llm_runs.c.id == s.llm_run_links.c.child_run_id,
        )
        .join(
            s.llm_agent_types,
            s.llm_agent_types.c.id == s.llm_runs.c.agent_type_id,
        )
        .where(s.llm_run_tags.c.tag == tag)
        .where(s.llm_run_links.c.relation == "replay")
        .where(s.llm_agent_types.c.slug == agent_type)
        .where(s.llm_run_outcomes.c.score.is_not(None))
        .order_by(s.llm_run_links.c.child_run_id)
    )
    with repo._tx() as conn:
        return list(conn.execute(stmt).mappings().fetchall())


def _scores_for_tag(repo: Repository, tag: str, agent_type: str) -> tuple[dict[int, float], int]:
    """Return (golden_id -> eval_score, matched row count) for one tag."""
    rows = _exact_tag_rows(_fetch_tag_rows(repo, tag, agent_type), tag)
    return {int(r["golden_id"]): float(r["score"]) for r in rows}, len(rows)


def compare_experiments(*, baseline: str, candidate: str, agent_type: str) -> dict:
    """Pair baseline and candidate replay runs by shared golden parent.

    Args:
        baseline: Experiment tag for the baseline runs.
        candidate: Experiment tag for the candidate runs.
        agent_type: Slug to filter both sets by.

    Raises:
        InvalidInputError: If both tags are the same string.
        PreconditionError: If either tag matches zero scored replay runs, or if
            the two tags share no golden parent.

    Returns:
        ``{"pairs", "baseline_runs", "candidate_runs"}`` — ``pairs`` holds
        ``golden_id``, ``baseline_score``, ``candidate_score`` and ``delta``
        per shared golden, sorted by ``golden_id``; the counts are matched
        scored-replay rows per tag. ``docs/eval-harness.md`` reads them.
    """
    if baseline == candidate:
        raise InvalidInputError(
            f"baseline and candidate are the same tag ({baseline!r}); "
            "a self-comparison reports every delta as 0.0"
        )

    repo = Repository()
    base_scores, base_rows = _scores_for_tag(repo, baseline, agent_type)
    cand_scores, cand_rows = _scores_for_tag(repo, candidate, agent_type)

    empty_tags = [
        tag for tag, scores in ((baseline, base_scores), (candidate, cand_scores)) if not scores
    ]
    if empty_tags:
        names = ", ".join(repr(t) for t in empty_tags)
        raise PreconditionError(
            f"No scored replay runs found for tag(s) {names} with "
            f"agent_type={agent_type!r}. Check the tag spelling and that the "
            "replays recorded eval_score outcomes."
        )

    shared_golden_ids = sorted(set(base_scores) & set(cand_scores))
    if not shared_golden_ids:
        raise PreconditionError(
            f"Tags {baseline!r} ({base_rows} scored runs) and {candidate!r} "
            f"({cand_rows} scored runs) share no golden parent, so there is "
            "nothing to compare. The two experiments replayed different "
            "golden sets."
        )

    return {
        "pairs": [
            {
                "golden_id": gid,
                "baseline_score": base_scores[gid],
                "candidate_score": cand_scores[gid],
                "delta": cand_scores[gid] - base_scores[gid],
            }
            for gid in shared_golden_ids
        ],
        "baseline_runs": base_rows,
        "candidate_runs": cand_rows,
    }
