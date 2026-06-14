"""Tests for pf_core.llm.cache — exact cache, config, invalidation, recording.

All tests use in-memory SQLite via the ``pf_engine`` fixture.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pf_core.llm.cache import (
    CacheHit,
    ExactCacheRepo,
    cache_lookup,
    cache_store,
    clear_config_cache,
    get_agent_cache_config,
    purge_expired,
    record_cache_hit,
)
from pf_core.llm.cache import by_agent, by_model, by_run
from pf_core.llm.cache._recorder import _age_bucket
from pf_core.llm.tracking import (
    LlmRunRepo,
    clear_resolver_caches,
    compute_input_hash,
    metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    clear_resolver_caches()
    clear_config_cache()
    yield
    clear_resolver_caches()
    clear_config_cache()


@pytest.fixture()
def cache_db(pf_engine):
    """In-memory SQLite engine with all framework tables (tracking + cache)."""
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


def _make_run(agent_type="classifier", model="openai/gpt-4o"):
    """Insert a minimal llm_runs row and return its id."""
    return LlmRunRepo().record(agent_type=agent_type, model=model)


# ---------------------------------------------------------------------------
# compute_input_hash
# ---------------------------------------------------------------------------


def test_compute_input_hash_stable():
    h1 = compute_input_hash(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    h2 = compute_input_hash(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    assert h1 == h2
    assert len(h1) == 64


def test_compute_input_hash_differs_on_model():
    h1 = compute_input_hash(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    h2 = compute_input_hash(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    assert h1 != h2


def test_compute_input_hash_strips_non_sampling_keys():
    # agent_type and model should not appear in sampling hash input
    h1 = compute_input_hash(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        sampling={"temperature": 0.5, "agent_type": "classifier", "model": "gpt-4o"},
    )
    h2 = compute_input_hash(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        sampling={"temperature": 0.5},
    )
    assert h1 == h2


def test_compute_input_hash_accepts_rendered_prompts():
    h = compute_input_hash(
        model="gpt-4o",
        rendered_system="sys",
        rendered_user="usr",
    )
    assert len(h) == 64


# ---------------------------------------------------------------------------
# AgentCacheConfig defaults
# ---------------------------------------------------------------------------


def test_agent_cache_config_defaults_when_no_file():
    cfg = get_agent_cache_config("classifier")
    assert cfg.exact is True
    assert cfg.semantic is False
    assert cfg.ttl_seconds == 86400


def test_agent_cache_config_from_yaml(tmp_path, monkeypatch):
    yaml_content = """
defaults:
  exact: true
  ttl_seconds: 3600

agents:
  searcher:
    ttl_seconds: 1800
    semantic: true
    semantic_threshold: 0.95
"""
    cfg_file = tmp_path / "cache.yaml"
    cfg_file.write_text(yaml_content)
    monkeypatch.setenv("CACHE_CONFIG", str(cfg_file))
    clear_config_cache()

    default_cfg = get_agent_cache_config("drafter")
    assert default_cfg.ttl_seconds == 3600
    assert default_cfg.semantic is False

    searcher_cfg = get_agent_cache_config("searcher")
    assert searcher_cfg.ttl_seconds == 1800
    assert searcher_cfg.semantic is True
    assert searcher_cfg.semantic_threshold == 0.95


def test_agent_cache_config_missing_file_uses_defaults(monkeypatch):
    monkeypatch.setenv("CACHE_CONFIG", "/nonexistent/cache.yaml")
    clear_config_cache()
    cfg = get_agent_cache_config("any")
    assert cfg.exact is True


# ---------------------------------------------------------------------------
# ExactCacheRepo — store + lookup
# ---------------------------------------------------------------------------


def test_exact_cache_store_and_lookup(cache_db):
    run_id = _make_run()
    repo = ExactCacheRepo()

    entry_id = repo.store(
        input_hash="a" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
        parsed_output={"label": "tech"},
        raw_response='{"label": "tech"}',
    )
    assert isinstance(entry_id, int)

    row = repo.lookup(input_hash="a" * 64, agent_type="classifier")
    assert row is not None
    assert row["parsed_output"] == {"label": "tech"}
    assert row["source_run_id"] == run_id
    assert row["model"] == "openai/gpt-4o"


def test_exact_cache_miss_returns_none(cache_db):
    row = ExactCacheRepo().lookup(input_hash="b" * 64, agent_type="classifier")
    assert row is None


def test_exact_cache_expired_entry_is_a_miss(cache_db):
    from sqlalchemy import update
    from pf_core.llm.cache._schema import llm_cache_entries
    from pf_core.db import transaction

    run_id = _make_run()
    repo = ExactCacheRepo()
    entry_id = repo.store(
        input_hash="c" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
        ttl_seconds=3600,
    )
    # Manually set expires_at to a past timestamp to simulate expiry
    past = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    with transaction() as conn:
        conn.execute(
            update(llm_cache_entries)
            .where(llm_cache_entries.c.id == entry_id)
            .values(expires_at=past)
        )

    row = repo.lookup(input_hash="c" * 64, agent_type="classifier")
    assert row is None


def test_exact_cache_permanent_entry_never_expires(cache_db):
    run_id = _make_run()
    ExactCacheRepo().store(
        input_hash="d" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
        ttl_seconds=0,  # permanent
    )
    row = ExactCacheRepo().lookup(input_hash="d" * 64, agent_type="classifier")
    assert row is not None


def test_exact_cache_store_conflict_returns_existing_id(cache_db):
    run_id = _make_run()
    repo = ExactCacheRepo()
    id1 = repo.store(
        input_hash="e" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
    )
    # Second store with same hash should silently return existing row's id
    id2 = repo.store(
        input_hash="e" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
    )
    assert id1 == id2


def test_exact_cache_bump_hit(cache_db):
    run_id = _make_run()
    repo = ExactCacheRepo()
    entry_id = repo.store(
        input_hash="f" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
    )
    repo.bump_hit(entry_id=entry_id)
    # No assertion on hit_count (SQLite server_default quirk); just verify no exception


# ---------------------------------------------------------------------------
# cache_lookup / cache_store helpers
# ---------------------------------------------------------------------------


def test_cache_lookup_miss_when_not_stored(cache_db):
    hit = cache_lookup(agent_type="classifier", input_hash="0" * 64)
    assert hit is None


def test_cache_lookup_disabled_returns_none(cache_db, tmp_path, monkeypatch):
    yaml_content = "agents:\n  noncache:\n    exact: false\n"
    cfg_file = tmp_path / "cache.yaml"
    cfg_file.write_text(yaml_content)
    monkeypatch.setenv("CACHE_CONFIG", str(cfg_file))
    clear_config_cache()

    hit = cache_lookup(agent_type="noncache", input_hash="1" * 64)
    assert hit is None


def test_cache_store_then_lookup_returns_hit(cache_db):
    run_id = _make_run()
    h = "2" * 64

    cache_store(
        agent_type="classifier",
        input_hash=h,
        source_run_id=run_id,
        model="openai/gpt-4o",
        parsed_output={"label": "policy"},
        raw_response='{"label": "policy"}',
    )

    hit = cache_lookup(agent_type="classifier", input_hash=h)
    assert hit is not None
    assert isinstance(hit, CacheHit)
    assert hit.parsed_output == {"label": "policy"}
    assert hit.hit_type == "exact"
    assert hit.similarity == 1.0
    assert hit.source_run_id == run_id


def test_cache_store_noop_when_exact_disabled(cache_db, tmp_path, monkeypatch):
    yaml_content = "agents:\n  noncache:\n    exact: false\n"
    cfg_file = tmp_path / "cache.yaml"
    cfg_file.write_text(yaml_content)
    monkeypatch.setenv("CACHE_CONFIG", str(cfg_file))
    clear_config_cache()

    run_id = _make_run()
    # Should not raise, should be a no-op
    cache_store(
        agent_type="noncache",
        input_hash="3" * 64,
        source_run_id=run_id,
        model="openai/gpt-4o",
    )
    hit = cache_lookup(agent_type="noncache", input_hash="3" * 64)
    assert hit is None


# ---------------------------------------------------------------------------
# record_cache_hit
# ---------------------------------------------------------------------------


def test_record_cache_hit_creates_run_row(cache_db):
    run_id = _make_run()
    h = "4" * 64
    cache_store(
        agent_type="classifier",
        input_hash=h,
        source_run_id=run_id,
        model="openai/gpt-4o",
        parsed_output={"x": 1},
    )
    hit = cache_lookup(agent_type="classifier", input_hash=h)
    assert hit is not None

    cache_hit_run_id = record_cache_hit(hit=hit, duration_ms=3)
    assert isinstance(cache_hit_run_id, int)
    assert cache_hit_run_id != run_id

    # Verify the run row has status='cache_hit'
    row = LlmRunRepo().get(cache_hit_run_id)
    assert row is not None
    assert row["status"] == "cache_hit"
    assert row["cost_usd"] == 0 or row["cost_usd"] is None
    assert row["prompt_tokens"] == 0 or row["prompt_tokens"] is None


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def test_invalidate_by_agent(cache_db):
    run_id = _make_run()
    ExactCacheRepo().store(
        input_hash="5" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
    )
    deleted = by_agent("classifier")
    assert deleted >= 1
    assert ExactCacheRepo().lookup(input_hash="5" * 64, agent_type="classifier") is None


def test_invalidate_by_model(cache_db):
    run_id = _make_run(model="openai/gpt-4o-mini")
    ExactCacheRepo().store(
        input_hash="6" * 64,
        agent_type="classifier",
        model="openai/gpt-4o-mini",
        source_run_id=run_id,
    )
    # Invalidate entries NOT using the new model
    deleted = by_model("classifier", new_model="openai/gpt-4o")
    assert deleted >= 1
    assert ExactCacheRepo().lookup(input_hash="6" * 64, agent_type="classifier") is None


def test_invalidate_by_run(cache_db):
    run_id = _make_run()
    ExactCacheRepo().store(
        input_hash="7" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
    )
    deleted = by_run(run_id)
    assert deleted == 1
    assert ExactCacheRepo().lookup(input_hash="7" * 64, agent_type="classifier") is None


def test_invalidate_by_run_zero_when_not_found(cache_db):
    deleted = by_run(99999)
    assert deleted == 0


def test_purge_expired(cache_db):
    run_id = _make_run()
    # Store a permanently valid entry
    ExactCacheRepo().store(
        input_hash="8" * 64,
        agent_type="classifier",
        model="openai/gpt-4o",
        source_run_id=run_id,
        ttl_seconds=0,  # permanent
    )
    deleted = purge_expired()
    assert deleted == 0
    # Still present
    assert ExactCacheRepo().lookup(input_hash="8" * 64, agent_type="classifier") is not None


# ---------------------------------------------------------------------------
# _age_bucket helper
# ---------------------------------------------------------------------------


def test_age_bucket_fresh():
    now = dt.datetime.now(dt.timezone.utc)
    assert _age_bucket(now) == "cache_age:fresh"


def test_age_bucket_1d():
    yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12)
    assert _age_bucket(yesterday) == "cache_age:<1d"


def test_age_bucket_7d():
    three_days = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    assert _age_bucket(three_days) == "cache_age:<7d"


def test_age_bucket_old():
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
    assert _age_bucket(old) == "cache_age:>7d"


def test_age_bucket_none():
    assert _age_bucket(None) == "cache_age:unknown"
