"""
Cache invalidation helpers for ``llm_cache_entries``.

All operations are explicit — there is no implicit auto-invalidation. Services
call these directly when they know entries should be flushed.

Usage::

    from pf_core.llm.cache.invalidate import (
        by_agent, by_model, by_run, purge_expired,
    )

    by_agent("classifier")                          # drop all classifier entries
    by_model("searcher", new_model="claude-opus-4-7")  # drop entries by model change
    by_run(run_id=1042)                             # drop one specific entry
    purge_expired()                                 # sweep rows past expires_at
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete

from pf_core.db import transaction
from pf_core.llm.cache._schema import llm_cache_entries
from pf_core.llm.tracking._resolvers import (
    resolve_agent_type_id,
    resolve_llm_model_id,
)
from pf_core.log import get_logger

logger = get_logger(__name__)


def by_agent(agent_type: str) -> int:
    """Delete all cache entries for *agent_type*.

    Returns:
        Number of rows deleted.
    """
    agent_type_id = resolve_agent_type_id(agent_type)
    with transaction() as conn:
        result = conn.execute(
            delete(llm_cache_entries).where(
                llm_cache_entries.c.agent_type_id == agent_type_id
            )
        )
    count = result.rowcount
    logger.info("cache_invalidated_by_agent", agent_type=agent_type, deleted=count)
    return count


def by_model(agent_type: str, *, new_model: str) -> int:
    """Delete cache entries for *agent_type* that used a different model.

    Call this after swapping the model for an agent in ``model_router.yaml``
    so stale entries from the old model are evicted.

    Args:
        agent_type: Agent slug.
        new_model: The new model name. Entries using *other* models are deleted.

    Returns:
        Number of rows deleted.
    """
    agent_type_id = resolve_agent_type_id(agent_type)
    new_model_id = resolve_llm_model_id(new_model)
    with transaction() as conn:
        result = conn.execute(
            delete(llm_cache_entries).where(
                (llm_cache_entries.c.agent_type_id == agent_type_id)
                & (llm_cache_entries.c.model_id != new_model_id)
            )
        )
    count = result.rowcount
    logger.info(
        "cache_invalidated_by_model",
        agent_type=agent_type,
        new_model=new_model,
        deleted=count,
    )
    return count


def by_run(run_id: int) -> int:
    """Delete the cache entry sourced from *run_id* (if any).

    Useful when a run is later flagged as producing a bad response.

    Returns:
        Number of rows deleted (0 or 1).
    """
    with transaction() as conn:
        result = conn.execute(
            delete(llm_cache_entries).where(
                llm_cache_entries.c.source_run_id == run_id
            )
        )
    count = result.rowcount
    logger.info("cache_invalidated_by_run", run_id=run_id, deleted=count)
    return count


def purge_expired() -> int:
    """Delete all rows where ``expires_at`` is in the past.

    Intended to be called from a periodic maintenance job.

    Returns:
        Number of rows deleted.
    """
    now = dt.datetime.now(dt.timezone.utc)
    with transaction() as conn:
        result = conn.execute(
            delete(llm_cache_entries).where(
                llm_cache_entries.c.expires_at.isnot(None)
                & (llm_cache_entries.c.expires_at <= now)
            )
        )
    count = result.rowcount
    logger.info("cache_purge_expired", deleted=count)
    return count
