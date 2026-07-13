# LLM Cache

`pf_core.llm.cache` — response cache for LLM calls. Answers "did we already ask the LLM something equivalent to this?" before making an expensive call.

Two layers (exact cache is shipped; semantic is planned):

1. **Exact cache** — keyed by `input_hash` (SHA256 of model + rendered prompts + sampling + configs). Reuses identical calls without a round-trip.
2. **Semantic cache** — keyed by embedding similarity. For queries that differ only in whitespace, phrasing, or order-irrelevant details. Opt-in per agent type. *(planned — not yet shipped; the schema ships today, the lookup doesn't)*

**Not** to be confused with `pf_core.cache.redis` (Redis KV cache for API response memoization). This module caches LLM call responses in the database.

## Quick start

```python
from pf_core.llm.cache import cache_lookup, cache_store, record_cache_hit
from pf_core.llm.tracking import compute_input_hash, track_run
from pf_core.llm.router import get_agent_config
from pf_core.clients.openrouter import get_client

@track_run(agent_type="classifier", provider="openrouter")
def _tracked_chat(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)

def classify(*, text: str) -> dict:
    cfg = get_agent_config("classifier")
    messages = [{"role": "user", "content": text}]

    input_hash = compute_input_hash(model=cfg["model"], messages=messages, sampling=cfg)

    # 1. Check cache first
    hit = cache_lookup(agent_type="classifier", input_hash=input_hash)
    if hit is not None:
        record_cache_hit(hit=hit)
        return hit.parsed_output

    # 2. Live call
    content, usage = _tracked_chat(messages=messages, **cfg)
    run_id = usage["_llm_run_id"]

    # 3. Store response
    parsed = parse_llm_json(content)
    cache_store(
        agent_type="classifier",
        input_hash=input_hash,
        source_run_id=run_id,
        model=cfg["model"],
        parsed_output=parsed,
        raw_response=content,
    )
    return parsed
```

## Cache config (cache.yaml)

Per-agent policy. Place in `config/cache.yaml` (or set `CACHE_CONFIG` env var).

To disable caching entirely with no config file, set `CACHE_CONFIG` to `off` (also accepted: `disabled`, `none`, `0`) — exact and semantic caching turn off for every agent. This is what test suites should use (`hermetic_test_env()` sets it — see [testing.md](testing.md)); a missing file, by contrast, means framework defaults, which have `exact: true`.

```yaml
# config/cache.yaml

defaults:
  exact: true
  semantic: false
  ttl_seconds: 86400        # 24 hours; 0 = never expire
  max_entries_per_agent: 10000
  on_miss: proceed           # 'proceed' | 'warn_log'

agents:
  classifier:
    exact: true
    semantic: false           # semantic layer not yet shipped
    ttl_seconds: 604800       # 7 days — classifications are stable

  extractor:
    exact: true
    ttl_seconds: 3600         # 1 hour — extracted values can go stale

  summarizer:
    exact: true
    ttl_seconds: 7200

  critic:
    exact: true
    ttl_seconds: 0            # no TTL — output is reproducible forever
```

### Config keys

| Key | Default | Description |
|-----|---------|-------------|
| `exact` | `true` | Check exact-hash cache |
| `semantic` | `false` | Check semantic-similarity cache (planned) |
| `ttl_seconds` | `86400` | Entry expiration; `0` = never |
| `semantic_threshold` | `0.93` | Cosine similarity threshold for a semantic hit |
| `semantic_embedding_model` | `""` | Model used for embeddings (planned) |
| `canonicalize.*` | all `false` | Pre-embedding normalizations (planned) |
| `max_entries_per_agent` | `10000` | LRU eviction trigger |
| `on_miss` | `proceed` | `warn_log` emits a WARNING on miss (useful during rollout) |

## compute_input_hash()

```python
from pf_core.llm.tracking import compute_input_hash

h = compute_input_hash(
    model="anthropic/claude-opus-4-7",
    messages=[{"role": "user", "content": "classify this text"}],
    sampling={"temperature": 0.0, "max_tokens": 512},
    configs={"category_set_id": 42},   # optional project-specific snapshot
)
# → 64-char hex string
```

Same hash that `LlmRunRepo.record()` writes to `llm_runs.input_hash`. Use it as the primary key for both the exact cache lookup and the tracking row.

**Accepts** either `messages` list or pre-rendered `rendered_system` / `rendered_user` strings. Non-sampling keys in the `sampling` dict (e.g. `model`, `agent_type`) are automatically stripped before hashing.

## cache_lookup()

```python
from pf_core.llm.cache import cache_lookup, CacheHit

hit: CacheHit | None = cache_lookup(
    agent_type="classifier",
    input_hash=h,
    canonical_text="...",   # pass now to avoid call-site changes when semantic lands
)
```

Returns `None` on miss or when caching is disabled for the agent. On hit, returns a `CacheHit`:

| Field | Type | Description |
|-------|------|-------------|
| `entry_id` | `int` | PK of `llm_cache_entries` |
| `parsed_output` | `Any` | Cached parsed JSON value |
| `raw_response` | `str \| None` | Cached raw LLM response |
| `source_run_id` | `int` | FK to original `llm_runs` row |
| `model` | `str` | Model name from the source run |
| `agent_type` | `str` | Agent slug |
| `input_hash` | `str` | The SHA256 key |
| `hit_type` | `str` | `"exact"` or `"semantic"` |
| `similarity` | `float` | `1.0` for exact hits |
| `created_at` | `datetime` | When the cache entry was created |

## cache_store()

```python
from pf_core.llm.cache import cache_store

cache_store(
    agent_type="classifier",
    input_hash=h,
    source_run_id=run_id,
    model="anthropic/claude-opus-4-7",
    parsed_output={"label": "tech"},
    raw_response='{"label": "tech"}',
)
```

No-op when exact caching is disabled for the agent. Silently skips duplicate entries (concurrent identical requests may both attempt to store; only one wins via the `UNIQUE` constraint).

## record_cache_hit()

Records a zero-cost `llm_runs` row so analytics see every "call that happened":

```python
from pf_core.llm.cache import record_cache_hit

hit_run_id = record_cache_hit(hit=hit, duration_ms=2)
```

Creates:
- `llm_runs` row: `status='cache_hit'`, zero tokens, zero cost
- `llm_run_links` row: `parent_run_id=source_run_id`, `relation='cache'`
- `llm_run_tags` rows: `cache:exact` and `cache_age:<bucket>`

## Invalidation

```python
from pf_core.llm.cache import by_agent, by_model, by_run, purge_expired

by_agent("classifier")                            # drop all classifier entries
by_model("extractor", new_model="claude-opus-4-7") # drop entries for other models
by_run(run_id=1042)                               # drop the entry from one run
purge_expired()                                   # sweep rows past expires_at
```

All functions return the count of deleted rows.

### When to invalidate

| Trigger | Invalidation |
|---------|-------------|
| Prompt version bump for an agent | `by_agent(agent_type)` — conservative; keeps cache warm if output is stable |
| Model swap for an agent | `by_model(agent_type, new_model=...)` — drops old-model entries only |
| Bad answer reported | `by_run(run_id)` — surgical, one entry |
| Maintenance sweep | `purge_expired()` — periodic, automated |

## Schema

### `llm_cache_entries`

Registered on the shared `pf_core.llm.tracking.schema.metadata`. Created by the consumer's Alembic migration.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INT PK | |
| `input_hash` | CHAR(64) UNIQUE | SHA256 hex — the cache key |
| `agent_type_id` | SMALLINT FK | → `llm_agent_types.id` |
| `model_id` | SMALLINT FK | → `llm_models.id` |
| `source_run_id` | BIGINT FK | → `llm_runs.id` ON DELETE CASCADE |
| `parsed_output` | JSON | Denormalized for fast hit reads |
| `raw_response` | MEDIUMTEXT | Denormalized for fast hit reads |
| `hit_count` | INT | LRU eviction input |
| `last_hit_at` | TIMESTAMP | |
| `expires_at` | TIMESTAMP NULL | NULL = never |
| `created_at` | TIMESTAMP | |

### `llm_embeddings` (semantic layer — schema only today)

Vector index for semantic cache. Not yet populated.

## Analytics queries

```sql
-- Cache hit rate last 7 days by agent type
SELECT at.slug,
       SUM(CASE WHEN r.status = 'cache_hit' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS hit_rate,
       COUNT(*) AS calls
FROM llm_runs r
JOIN llm_agent_types at ON at.id = r.agent_type_id
WHERE r.created_at >= CURRENT_DATE - INTERVAL '7' DAY
GROUP BY at.slug
ORDER BY hit_rate DESC;

-- Cost avoided by cache (what hits would have cost at source-run rates)
SELECT at.slug,
       COUNT(*) AS hits,
       SUM(source.cost_usd) AS cost_avoided_usd
FROM llm_runs hit
JOIN llm_run_links l    ON l.child_run_id  = hit.id AND l.relation = 'cache'
JOIN llm_runs source    ON source.id       = l.parent_run_id
JOIN llm_agent_types at ON at.id = hit.agent_type_id
WHERE hit.status = 'cache_hit'
  AND hit.created_at >= CURRENT_DATE - INTERVAL '30' DAY
GROUP BY at.slug;
```

## Env vars

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_CONFIG` | `config/cache.yaml` | Path to the cache config YAML file |
| `CACHE_CONFIG_RELOAD_SECONDS` | `60` | How often to reload config from disk |

## Consumer migration

Enabling the exact cache for a consumer project requires:

1. **Create the Alembic migration** — importing `pf_core.llm.tracking.metadata` in `env.py` is sufficient; `llm_cache_entries` and `llm_embeddings` will be detected by autogenerate.

2. **Add env vars** to `.env.example`:
   ```
   CACHE_CONFIG=config/cache.yaml
   ```

3. **Create `config/cache.yaml`** with per-agent policy (see example above).

4. **Update each service** to call `cache_lookup` before the LLM call and `cache_store` after (see Quick start above).

No `pyproject.toml` changes needed — the cache module uses only dependencies already declared in `pf-core`.

## See also

- [`docs/llm-tracking.md`](llm-tracking.md) — `input_hash` computation and the tracking backbone this cache sits on top of
- [`docs/model-router.md`](model-router.md) — per-agent model + sampling config (cache lookup happens before the router call)
- [`docs/cache.md`](cache.md) — Redis KV cache (different use case: API response memoization)
