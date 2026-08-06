# Cache

Redis-backed caching via [dogpile.cache](https://dogpilecache.sqlalchemy.org/) (by the same author as SQLAlchemy). Provides region-based caching with automatic serialization, TTL management, and graceful degradation when Redis is unavailable.

## Installation

```bash
pip install pf-core[redis]
```

This installs `dogpile.cache` and `redis`.

## Quick start

```python
from pf_core.cache.redis import create_region

cache = create_region(
    url="redis://localhost:6379",
    expiration_time=300,       # TTL in seconds
    key_prefix="myapp",        # namespace isolation
)

# Cache a function result
result = cache.get_or_create("expensive_query", lambda: db.run_query())

# Manual get/set/delete
cache.set("mykey", {"data": [1, 2, 3]})
val = cache.get("mykey")       # returns the dict, or dogpile NO_VALUE
cache.delete("mykey")

# Invalidate all cached values in the region
cache.invalidate()
```

## create_region()

Factory function that returns a configured `dogpile.cache.CacheRegion`.

```python
create_region(
    url="redis://localhost:6379",  # Redis URL (or REDIS_URL env var)
    expiration_time=300,            # default TTL in seconds
    key_prefix="myapp",            # prepended to all keys
)
```

**Backend selection**:
- If `url` is provided (or `REDIS_URL` env var is set): uses the Redis backend
- If no URL is available: uses the **null backend** (no caching — functions are called every time)

This means your code works identically with or without Redis. No `if cache.available` checks needed.

## Using regions

A `CacheRegion` is the standard [dogpile.cache API](https://dogpilecache.sqlalchemy.org/en/latest/api.html#dogpile.cache.region.CacheRegion):

### get_or_create — cached function calls

```python
# Calls loader() on cache miss, returns cached value on hit
result = region.get_or_create("sections_list", lambda: db.list_sections())
```

### Decorator style

```python
@region.cache_on_arguments()
def get_sections():
    return db.list_sections()

sections = get_sections()  # cached after first call
```

### Manual operations

```python
from dogpile.cache.api import NO_VALUE

region.set("key", value)
val = region.get("key")
if val is NO_VALUE:
    # not in cache

region.delete("key")
```

### Invalidation

```python
region.invalidate()  # all cached values in this region are now stale
```

After invalidation, the next `get_or_create()` call will regenerate the value. Existing keys aren't deleted — they're just marked as expired. dogpile handles the "thundering herd" problem (only one thread regenerates the value; others wait or get the stale value).

**Invalidation is process-local.** The marker is held in memory by the `CacheRegion` instance, so `invalidate()` does not reach other workers or a CLI process — they keep serving their cached values until the TTL expires. Invalidating across processes needs a generation counter stored in Redis and folded into every cache key; bump that instead.

## Multiple regions

Use separate regions for different TTLs and invalidation scopes:

```python
from pf_core.cache.redis import create_region

# Short-lived API response cache — invalidated on data writes
api_cache = create_region(url=cfg.REDIS_URL, expiration_time=300, key_prefix="myapp:api")

# Long-lived blob storage for pipeline working data
blob_cache = create_region(url=cfg.REDIS_URL, expiration_time=86400, key_prefix="myapp:blob")

# Very long-lived LLM result cache
result_cache = create_region(url=cfg.REDIS_URL, expiration_time=90*86400, key_prefix="myapp:result")
```

Invalidating one region doesn't affect the others:

```python
api_cache.invalidate()  # only API cache is invalidated
# blob_cache and result_cache are untouched
```

## Example: wrapping regions in a project module

A consumer uses three regions wrapped in a `redis_cache.py` module:

```python
# app/redis_cache.py
from pf_core.cache.redis import create_region
from app.config import cfg

_api_region = None

def _get_api_region():
    global _api_region
    if _api_region is None:
        _api_region = create_region(
            url=cfg.REDIS_URL,
            expiration_time=cfg.CACHE_TTL_SECONDS,
            key_prefix="myapp:api",
        )
    return _api_region

def cached_json(key_parts, query, loader):
    """Cache an API response."""
    key = ":".join(str(p) for p in key_parts)
    return _get_api_region().get_or_create(key, loader)

def bump_cache_generation():
    """Invalidate all API caches (called after data writes)."""
    _get_api_region().invalidate()
```

Callers don't know or care about Redis:

```python
# In an API route
sections = redis_cache.cached_json(("sections",), None, db.list_sections)

# After a write
db.update_entry(entry_id, data)
redis_cache.bump_cache_generation()
```

## Backward compatibility

The module also exports `RedisCache` — a wrapper class that delegates to a dogpile region internally. This exists for projects that already import `RedisCache` and haven't migrated to regions yet. New code should use `create_region()` directly.

| Method | Behavior |
|---|---|
| `set(key, value)`, `delete(key)` | `False` when a configured backend dropped the write — degradation absorbs the error, so the return value is the only signal. `True` with the null backend, where caching is off by config. |
| `cached_json(key_parts, variant, fn, ttl=None)` | `get_or_create()` under a key built from the parts plus a hash of `variant`; `ttl` overrides the region TTL for that value. |
| `bump_generation()` | `invalidate()` on this process's region — process-local, as above. Always returns 0; there is no shared generation counter. |

`set()` takes no per-key TTL: `CacheRegion.set()` has no expiration argument, so the region TTL applies to every key it writes. Per-key expiry is available only on the read path, via `cached_json(..., ttl=...)` / `get_or_create(..., expiration_time=...)`.

## Graceful degradation

When Redis is unavailable (no URL configured, or Redis is down):

| Operation | Behavior |
|-----------|----------|
| `get_or_create(key, fn)` | Calls `fn()` every time (no caching) |
| `set(key, val)` | No-op |
| `get(key)` | Returns `NO_VALUE` |
| `delete(key)` | No-op |
| `invalidate()` | No-op |

Your code doesn't need any conditional logic for Redis availability.

The two cases reach that table by different routes. **No URL configured** selects the null backend at `create_region()`. **Redis down** cannot: dogpile connects lazily, so `configure()` succeeds against a dead server and the region holds a real Redis backend. That backend is wrapped, and each operation degrades — per operation rather than by a startup probe, so a server that recovers is used again without a restart.

**What degrades:** any `RedisError` or `OSError`. That is deliberately the whole Redis error surface, not just connection refusal — `ResponseError` (out-of-memory, writes to a read-only replica) and an authentication failure degrade too, because a cache that raises into a request is worse than a cache that misses. Nothing outside those two families is caught, so a bug in your key mangler or serializer still raises.

The cost is that an outage is quiet: the first failure in a process logs `cache_backend_unavailable` at WARNING and every later one at DEBUG. Do not treat the absence of warnings as health. Region operations return no signal either — only `RedisCache.set()` / `delete()` report a dropped write, by returning `False`.

**Availability cannot be inferred from the backend type.** `isinstance(region.backend, NullBackend)` distinguishes "no URL configured" from "URL configured", not "reachable" — a refused connection looks configured. For a health check, PING the client; note a null-backend region has no client, which is *configured-off* rather than unreachable:

```python
client = getattr(region.backend, "reader_client", None)
if client is None:
    ...  # caching disabled by config, not an outage
else:
    try:
        client.ping()
    except Exception:
        ...  # unreachable
```
