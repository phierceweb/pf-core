"""
Exact-cache repository for ``llm_cache_entries``.

Looks up and stores cache entries keyed by ``input_hash`` (SHA256 of model +
rendered prompts + sampling + configs — computed by
:func:`pf_core.llm.tracking.compute_input_hash`).

Usage::

    from pf_core.llm.cache.exact import ExactCacheRepo

    repo = ExactCacheRepo()
    row = repo.lookup(input_hash="abc...", agent_type="classifier")
    if row:
        # row: {id, parsed_output, raw_response, source_run_id, model, created_at}
        ...

    repo.store(
        input_hash="abc...",
        agent_type="classifier",
        model="claude-opus-4-7",
        source_run_id=1042,
        parsed_output={...},
        raw_response="...",
        ttl_seconds=86400,
    )
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select, update

from pf_core.db.repository import Repository
from pf_core.llm.cache._schema import llm_cache_entries
from pf_core.llm.tracking._resolvers import (
    resolve_agent_type_id,
    resolve_llm_model_id,
)
from pf_core.llm.tracking.schema import llm_agent_types, llm_models


class ExactCacheRepo(Repository):
    """Reads and writes :data:`llm_cache_entries` rows."""

    def lookup(self, *, input_hash: str, agent_type: str) -> dict | None:
        """Return a valid cache entry for *input_hash*, or ``None``.

        An entry is valid if ``expires_at`` is NULL or in the future.

        Returns:
            Dict with keys: ``id``, ``parsed_output``, ``raw_response``,
            ``source_run_id``, ``model``, ``agent_type``, ``created_at``.
            ``None`` on miss or expiry.
        """
        with self._tx() as conn:
            now = dt.datetime.now(dt.timezone.utc)
            row = conn.execute(
                select(
                    llm_cache_entries.c.id,
                    llm_cache_entries.c.parsed_output,
                    llm_cache_entries.c.raw_response,
                    llm_cache_entries.c.source_run_id,
                    llm_cache_entries.c.created_at,
                    llm_models.c.name.label("model"),
                    llm_agent_types.c.slug.label("agent_type"),
                )
                .join(
                    llm_models,
                    llm_cache_entries.c.model_id == llm_models.c.id,
                )
                .join(
                    llm_agent_types,
                    llm_cache_entries.c.agent_type_id == llm_agent_types.c.id,
                )
                .where(llm_cache_entries.c.input_hash == input_hash)
                .where(
                    (llm_cache_entries.c.expires_at.is_(None))
                    | (llm_cache_entries.c.expires_at > now)
                )
            ).mappings().fetchone()

        return dict(row) if row else None

    def store(
        self,
        *,
        input_hash: str,
        agent_type: str,
        model: str,
        source_run_id: int,
        parsed_output: Any = None,
        raw_response: str | None = None,
        ttl_seconds: int = 0,
    ) -> int:
        """Insert a cache entry, ignoring conflicts (concurrent identical requests).

        Args:
            input_hash: SHA256 key (64 hex chars).
            agent_type: Agent slug for ID resolution.
            model: Model name for ID resolution.
            source_run_id: FK to the ``llm_runs`` row that produced this response.
            parsed_output: Parsed JSON value to cache.
            raw_response: Raw LLM response string.
            ttl_seconds: Seconds until expiry. ``0`` means no TTL (permanent).

        Returns:
            The ``llm_cache_entries.id`` of the stored (or pre-existing) row.
        """
        agent_type_id = resolve_agent_type_id(agent_type)
        model_id = resolve_llm_model_id(model)

        expires_at: dt.datetime | None = None
        if ttl_seconds and ttl_seconds > 0:
            expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=ttl_seconds
            )

        with self._tx() as conn:
            # Use INSERT OR IGNORE (SQLite) / INSERT IGNORE (MySQL) pattern:
            # attempt insert; if unique constraint fires, fetch the existing row.
            try:
                result = conn.execute(
                    llm_cache_entries.insert().values(
                        input_hash=input_hash,
                        agent_type_id=agent_type_id,
                        model_id=model_id,
                        source_run_id=source_run_id,
                        parsed_output=parsed_output,
                        raw_response=raw_response,
                        expires_at=expires_at,
                    )
                )
                return int(result.inserted_primary_key[0])
            except Exception:
                # Unique constraint on input_hash — fetch the existing entry id
                row = conn.execute(
                    select(llm_cache_entries.c.id).where(
                        llm_cache_entries.c.input_hash == input_hash
                    )
                ).fetchone()
                if row:
                    return int(row[0])
                raise

    def bump_hit(self, *, entry_id: int) -> None:
        """Increment ``hit_count`` and update ``last_hit_at`` for *entry_id*."""
        with self._tx() as conn:
            conn.execute(
                update(llm_cache_entries)
                .where(llm_cache_entries.c.id == entry_id)
                .values(
                    hit_count=llm_cache_entries.c.hit_count + 1,
                    last_hit_at=func.now(),
                )
            )
