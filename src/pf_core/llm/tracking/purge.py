"""
Retention helper for ``llm_run_payloads``, the expensive cold sidecar of ``llm_runs``.

Retention is a deliberate operator decision — never called automatically.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import exists, select

from pf_core.db.connection import transaction
from pf_core.llm.tracking import schema as s


def purge_old_payloads(
    older_than_days: int = 90,
    *,
    keep_flagged: bool = True,
    now: dt.datetime | None = None,
) -> int:
    """Delete ``llm_run_payloads`` rows whose parent run is older than the cutoff.

    Golden-set members are always kept, regardless of ``keep_flagged``.

    Args:
        older_than_days: Age threshold in days. Payloads attached to runs
            created strictly before ``now - older_than_days`` are purged.
        keep_flagged: When ``True`` (default), exclude runs with non-success
            status OR a failed validation — those keep their forensic detail.
        now: Override "now" for tests. Defaults to ``datetime.utcnow()``.

    Returns:
        Number of payload rows deleted.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")

    reference_now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cutoff = reference_now - dt.timedelta(days=older_than_days)

    eligible = select(s.llm_runs.c.id).where(s.llm_runs.c.created_at < cutoff)
    # Golden members replay from their payload. GoldenSetRepo.add() writes both
    # rows; remove() drops only the tag, so the pair means "currently a member".
    golden_approved = (
        select(s.llm_run_outcomes.c.llm_run_id)
        .where(s.llm_run_outcomes.c.llm_run_id == s.llm_runs.c.id)
        .where(s.llm_run_outcomes.c.outcome_kind == "golden_approved")
    )
    eval_tagged = (
        select(s.llm_run_tags.c.llm_run_id)
        .where(s.llm_run_tags.c.llm_run_id == s.llm_runs.c.id)
        .where(s.llm_run_tags.c.tag.like("eval:%"))
    )
    eligible = eligible.where(~(exists(golden_approved) & exists(eval_tagged)))
    if keep_flagged:
        failed_validation = (
            select(s.llm_run_validations.c.llm_run_id)
            .where(s.llm_run_validations.c.llm_run_id == s.llm_runs.c.id)
            .where(s.llm_run_validations.c.passed.is_(False))
        )
        eligible = eligible.where(s.llm_runs.c.status == "success").where(
            ~exists(failed_validation)
        )

    stmt = s.llm_run_payloads.delete().where(s.llm_run_payloads.c.llm_run_id.in_(eligible))

    with transaction() as conn:
        result = conn.execute(stmt)
    return int(result.rowcount or 0)
