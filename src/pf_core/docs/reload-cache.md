# Reload cache

TTL-based hot-reload cache for config loaders: operators edit a config file and the change lands within the TTL, without a process restart and without every call re-reading disk. One primitive replaces the hand-rolled double-checked-lock caches that config loaders otherwise grow. Not to be confused with [`llm-cache`](llm-cache.md) (DB-backed LLM response caching) or [`cache`](cache.md) (Redis regions) — `ReloadCache` is in-process, single-value-per-key, and about *freshness*, not sharing.

---

## Table of Contents

- [Quick usage](#quick-usage)
- [Semantics](#semantics)
- [Error policy](#error-policy)
- [Testing](#testing)

## Quick usage

```python
from pf_core.utils.reload_cache import ReloadCache
from pf_core.utils.env import resolve_int

def _ttl() -> int:
    return resolve_int(None, "MYTOOL_CONFIG_RELOAD_SECONDS", default=60)

_cache = ReloadCache(lambda path: _parse(Path(path)), ttl=_ttl)

def load(force: bool = False) -> dict:
    return _cache.get(str(_config_path()), force=force)

def clear_cache() -> None:   # test seam
    _cache.clear()
```

Keep one module-level `ReloadCache` per cached value and expose `load(force=)` / `clear_cache()` wrappers — callers should never see the cache object.

## Semantics

- **Double-checked lock**: the hit path takes no lock; misses serialize the reload so concurrent callers trigger one load.
- **`ttl` is a zero-arg callable, read on every call** — env-var changes land without a restart. Pass your own reader (kwarg > env > default per the config-driven rule); the primitive hardcodes no env name.
- **`ttl() <= 0` reloads on every call.** `pf_core.testing` pins reload TTLs to `0` so test suites always see fresh config — preserve that semantic in anything built on this.
- **Key change invalidates**: `get(key)` with a different key than the cached one reloads. Fold *everything* that selects the config into the key (path, profile name, root directory) — a knob left out of the key is a staleness bug.
- `force=True` bypasses the window; `clear()` drops the cached value.

## Error policy

Reload failure policy is per-site, expressed in two places:

- **`stale_on=(ExcType, …)`** — when the loader raises a matching exception *and a prior good value exists for the same key*, the cache logs `reload_cache_kept_stale`, refreshes the window (throttling retries to once per TTL), and serves the stale value. A config file made momentarily invalid mid-edit doesn't take the app down. With no prior value, the exception raises.
- **Fail-loud** (the default, `stale_on=()`): loader exceptions always propagate.
- **Fail-empty**: catch inside your loader and return the empty value — the policy belongs to the loader, not the cache.

pf-core's own model-router and LLM-cache config loaders run on this primitive (stale-serving and fail-empty respectively); read `pf_core/llm/_router_loader.py` for the canonical adoption shape.

## Testing

Call the module's `clear_cache()` seam in fixtures rather than poking cache internals, and pin the TTL env to `0` (or rely on `pf_core.testing`'s bootstrap, which already does) so tests never race the freshness window.
