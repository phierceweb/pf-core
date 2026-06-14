"""
Retention helper for ``llm_run_payloads``.

The hot ``llm_runs`` table keeps every analytics-relevant column forever;
the cold sidecar (rendered prompts, raw response, parsed output) is the
expensive one. ``purge_old_payloads`` drops payload rows past a given age
while preserving the parent run rows.

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
    if keep_flagged:
        failed_validation = (
            select(s.llm_run_validations.c.llm_run_id)
            .where(s.llm_run_validations.c.llm_run_id == s.llm_runs.c.id)
            .where(s.llm_run_validations.c.passed.is_(False))
        )
        eligible = eligible.where(s.llm_runs.c.status == "success").where(
            ~exists(failed_validation)
        )

    stmt = s.llm_run_payloads.delete().where(
        s.llm_run_payloads.c.llm_run_id.in_(eligible)
    )

    with transaction() as conn:
        result = conn.execute(stmt)
    return int(result.rowcount or 0)
