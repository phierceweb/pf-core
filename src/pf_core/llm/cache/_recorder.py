"""
Cache-hit run recorder.

Writes an ``llm_runs`` row with ``status='cache_hit'`` plus the required
``llm_run_links`` and ``llm_run_tags`` rows, so analytics can measure hit rate,
cost avoided, and staleness.

Usage::

    from pf_core.llm.cache._recorder import record_cache_hit
    from pf_core.llm.cache import cache_lookup

    hit = cache_lookup(agent_type="classifier", input_hash=h)
    if hit is not None:
        run_id = record_cache_hit(hit=hit, duration_ms=2)
        return hit.parsed_output
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from pf_core.llm.cache._schema import llm_cache_entries
from pf_core.llm.tracking.repo import LlmRunRepo
from pf_core.llm.tracking.schema import llm_run_links, llm_run_tags
from pf_core.db import transaction

if TYPE_CHECKING:
    # Forward-ref guard. CacheHit lives in pf_core.llm.cache.__init__
    # which imports from this module — runtime import would cycle.
    from pf_core.llm.cache import CacheHit


def record_cache_hit(
    *,
    hit: CacheHit,
    duration_ms: int = 1,
) -> int:
    """Record a cache hit as a zero-cost ``llm_runs`` row.

    Creates:
    - ``llm_runs`` row: ``status='cache_hit'``, zero tokens / cost
    - ``llm_run_links``: ``parent_run_id=source_run_id``, ``relation='cache'``
    - ``llm_run_tags``: ``cache:exact`` or ``cache:semantic``, ``cache_age:<bucket>``

    Args:
        hit: The :class:`CacheHit` returned by :func:`cache_lookup`.
        duration_ms: Observed cache lookup latency in milliseconds.

    Returns:
        The new ``llm_runs.id`` for the cache-hit row.
    """
    repo = LlmRunRepo()
    run_id = repo.record(
        agent_type=hit.agent_type,
        model=hit.model,
        usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "duration_ms": duration_ms,
        },
        status="cache_hit",
        input_hash=hit.input_hash,
    )

    tag_type = f"cache:{hit.hit_type}"
    age_tag = _age_bucket(hit.created_at)

    with transaction() as conn:
        conn.execute(
            llm_run_links.insert().values(
                parent_run_id=hit.source_run_id,
                child_run_id=run_id,
                relation="cache",
            )
        )
        conn.execute(
            llm_run_tags.insert(),
            [
                {"llm_run_id": run_id, "tag": tag_type},
                {"llm_run_id": run_id, "tag": age_tag},
            ],
        )
        # Increment hit counter on the cache entry
        from sqlalchemy import update
        from sqlalchemy.sql import func

        conn.execute(
            update(llm_cache_entries)
            .where(llm_cache_entries.c.id == hit.entry_id)
            .values(
                hit_count=llm_cache_entries.c.hit_count + 1,
                last_hit_at=func.now(),
            )
        )

    return run_id


def _age_bucket(created_at: dt.datetime | None) -> str:
    """Return a cache-age tag string: ``cache_age:fresh`` | ``<1d`` | ``<7d`` | ``>7d``."""
    if created_at is None:
        return "cache_age:unknown"

    # Ensure timezone-aware for comparison
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)

    age = dt.datetime.now(dt.timezone.utc) - created_at
    hours = age.total_seconds() / 3600

    if hours < 1:
        return "cache_age:fresh"
    elif hours < 24:
        return "cache_age:<1d"
    elif hours < 168:  # 7 days
        return "cache_age:<7d"
    else:
        return "cache_age:>7d"
