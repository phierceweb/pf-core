"""
After-the-fact writers for ``llm_runs`` sidecars.

These repos record signals that arrive *after* the original LLM call:

- :class:`LlmRunOutcomeRepo` — backfilled reviewer outcomes (draft accepted,
  grade matched professor, etc).
- :class:`LlmRunValidationRepo` — async or post-hoc quality checks.
- :class:`LlmRunLinkRepo` — run-to-run relations (retry, critic, refine,
  fallback, subroutine, meta_analysis).

All three use a delete-then-insert pattern so a re-record (same composite key)
overwrites cleanly across SQLite, MySQL, and PostgreSQL without needing
dialect-specific UPSERT syntax.
"""

from __future__ import annotations

from sqlalchemy.exc import OperationalError
try:
    from tenacity import (
        Retrying,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential_jitter,
    )
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("tracking", "tenacity", feature="pf_core.llm.tracking") from e

from pf_core.db.repository import Repository
from pf_core.llm.tracking import schema as s
from pf_core.log import get_logger

logger = get_logger(__name__)


def _is_mysql_deadlock(exc: BaseException) -> bool:
    """Return True for the MySQL 1213 ``Deadlock found`` OperationalError.

    Concurrent pipeline processes can race on the delete-then-insert path in
    ``LlmRunValidationRepo.record``; InnoDB resolves the conflict by killing
    one transaction with ``(1213, 'Deadlock found when trying to get lock;
    try restarting transaction')``. The losing transaction is safe to retry
    verbatim — the INSERT is idempotent (delete + insert keyed on
    ``(llm_run_id, validator)``).
    """
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc)
    return "1213" in msg or "Deadlock" in msg


class LlmRunOutcomeRepo(Repository):
    """Records downstream business outcomes (one row per outcome_kind per run)."""

    def record(
        self,
        run_id: int,
        *,
        outcome_kind: str,
        score: float | None = None,
        notes: str | None = None,
    ) -> None:
        """Insert (or replace) the outcome row for ``(run_id, outcome_kind)``."""
        with self._tx() as conn:
            conn.execute(
                s.llm_run_outcomes.delete().where(
                    (s.llm_run_outcomes.c.llm_run_id == run_id)
                    & (s.llm_run_outcomes.c.outcome_kind == outcome_kind)
                )
            )
            conn.execute(
                s.llm_run_outcomes.insert().values(
                    llm_run_id=run_id,
                    outcome_kind=outcome_kind,
                    score=score,
                    notes=notes,
                )
            )

    def list_for_run(self, run_id: int) -> list[dict]:
        """All outcomes attached to ``run_id`` (most recent first)."""
        with self._tx() as conn:
            rows = conn.execute(
                s.llm_run_outcomes.select()
                .where(s.llm_run_outcomes.c.llm_run_id == run_id)
                .order_by(s.llm_run_outcomes.c.recorded_at.desc())
            ).mappings().fetchall()
        return [dict(r) for r in rows]


class LlmRunValidationRepo(Repository):
    """Records quality-signal validations (one per validator per run)."""

    def record(
        self,
        run_id: int,
        *,
        validator: str,
        passed: bool,
        severity: str = "info",
        details: dict | None = None,
    ) -> None:
        """Insert (or replace) the validation row for ``(run_id, validator)``.

        ``severity`` is one of ``'info'``, ``'warn'``, ``'error'`` by convention
        but stored as VARCHAR to allow project-specific extensions.

        Retries up to 3 times on MySQL deadlock (error 1213). Concurrent
        pipeline processes (e.g. parallel ``curated-ingest`` runs) can race
        on the delete+insert transaction; InnoDB kills one transaction and
        the caller can safely retry since the row key is
        ``(llm_run_id, validator)``.
        """
        def _log_retry(retry_state) -> None:
            logger.warning(
                "llm_run_validation_deadlock_retry",
                attempt=retry_state.attempt_number,
                wait=retry_state.next_action.sleep,
                run_id=run_id,
                validator=validator,
            )

        retryer = Retrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=0.1, max=1.0),
            retry=retry_if_exception(_is_mysql_deadlock),
            reraise=True,
            before_sleep=_log_retry,
        )
        retryer(
            self._record_once,
            run_id,
            validator=validator,
            passed=passed,
            severity=severity,
            details=details,
        )

    def _record_once(
        self,
        run_id: int,
        *,
        validator: str,
        passed: bool,
        severity: str,
        details: dict | None,
    ) -> None:
        """Single transactional delete+insert — the unit the retryer replays."""
        with self._tx() as conn:
            conn.execute(
                s.llm_run_validations.delete().where(
                    (s.llm_run_validations.c.llm_run_id == run_id)
                    & (s.llm_run_validations.c.validator == validator)
                )
            )
            conn.execute(
                s.llm_run_validations.insert().values(
                    llm_run_id=run_id,
                    validator=validator,
                    passed=passed,
                    severity=severity,
                    details=details,
                )
            )

    def list_for_run(self, run_id: int) -> list[dict]:
        """All validations attached to ``run_id``."""
        with self._tx() as conn:
            rows = conn.execute(
                s.llm_run_validations.select()
                .where(s.llm_run_validations.c.llm_run_id == run_id)
                .order_by(s.llm_run_validations.c.validator)
            ).mappings().fetchall()
        return [dict(r) for r in rows]


class LlmRunLinkRepo(Repository):
    """Records run-to-run relations (parent → child with labeled relation)."""

    def link(
        self,
        *,
        parent_id: int,
        child_id: int,
        relation: str,
    ) -> None:
        """Insert (or replace) a single link row.

        Idempotent: re-linking the same triple is a no-op rewrite.
        """
        with self._tx() as conn:
            conn.execute(
                s.llm_run_links.delete().where(
                    (s.llm_run_links.c.parent_run_id == parent_id)
                    & (s.llm_run_links.c.child_run_id == child_id)
                    & (s.llm_run_links.c.relation == relation)
                )
            )
            conn.execute(
                s.llm_run_links.insert().values(
                    parent_run_id=parent_id,
                    child_run_id=child_id,
                    relation=relation,
                )
            )

    def children(self, parent_id: int, *, relation: str | None = None) -> list[dict]:
        """All child links of ``parent_id``, optionally filtered by relation."""
        stmt = s.llm_run_links.select().where(
            s.llm_run_links.c.parent_run_id == parent_id
        )
        if relation is not None:
            stmt = stmt.where(s.llm_run_links.c.relation == relation)
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [dict(r) for r in rows]

    def parents(self, child_id: int, *, relation: str | None = None) -> list[dict]:
        """All parent links of ``child_id``, optionally filtered by relation."""
        stmt = s.llm_run_links.select().where(
            s.llm_run_links.c.child_run_id == child_id
        )
        if relation is not None:
            stmt = stmt.where(s.llm_run_links.c.relation == relation)
        with self._tx() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [dict(r) for r in rows]
