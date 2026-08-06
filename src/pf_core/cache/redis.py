"""
Redis-backed caching via dogpile.cache.

Provides ``create_region()`` to build configured cache regions and a
backward-compatible ``RedisCache`` wrapper for projects that haven't
migrated to regions yet.

Usage::

    from pf_core.cache.redis import create_region

    # Create a region with Redis backend
    api_cache = create_region(url="redis://localhost:6379", expiration_time=300, key_prefix="myapp:api")

    # Use dogpile.cache API directly
    @api_cache.cache_on_arguments()
    def get_sections():
        return db.list_sections()

    # Manual get/set/delete
    api_cache.set("mykey", "myvalue")
    val = api_cache.get("mykey")
    api_cache.delete("mykey")

    # Invalidate all cached values in the region
    api_cache.invalidate()

Requires: pip install pf-core[redis]
"""

from __future__ import annotations

import os
from typing import Any

from pf_core.log import get_logger

logger = get_logger(__name__)

_REDIS_ERRORS: tuple[type[BaseException], ...]
try:
    from redis.exceptions import RedisError

    _REDIS_ERRORS = (RedisError, OSError)
except ImportError:  # pragma: no cover - redis extra not installed
    _REDIS_ERRORS = (OSError,)


class _ResilientBackend:
    """Backend proxy that turns an unreachable server into a cache miss.

    dogpile's Redis backend connects lazily, so ``configure()`` succeeds even
    when nothing is listening and every later operation raises instead.

    Absorbed failures are counted in ``degraded_ops`` so a caller that must
    report success or failure can tell a dropped operation from a real one.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._warned = False
        self.degraded_ops = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _degrade(self, op: str, exc: BaseException) -> None:
        self.degraded_ops += 1
        if self._warned:
            logger.debug("cache_backend_unavailable", op=op, error=str(exc))
            return
        self._warned = True
        logger.warning("cache_backend_unavailable", op=op, error=str(exc))

    def _call(self, op: str, fn: Any, default: Any) -> Any:
        try:
            return fn()
        except _REDIS_ERRORS as e:
            self._degrade(op, e)
            return default

    def get(self, key: Any) -> Any:
        return self._call("get", lambda: self._inner.get(key), _no_value())

    def get_multi(self, keys: Any) -> Any:
        keys = list(keys)
        return self._call("get_multi", lambda: self._inner.get_multi(keys),
                          [_no_value()] * len(keys))

    def get_serialized(self, key: Any) -> Any:
        return self._call("get_serialized", lambda: self._inner.get_serialized(key),
                          _no_value())

    def get_serialized_multi(self, keys: Any) -> Any:
        keys = list(keys)
        return self._call("get_serialized_multi",
                          lambda: self._inner.get_serialized_multi(keys),
                          [_no_value()] * len(keys))

    def set(self, key: Any, value: Any) -> None:
        self._call("set", lambda: self._inner.set(key, value), None)

    def set_multi(self, mapping: Any) -> None:
        self._call("set_multi", lambda: self._inner.set_multi(mapping), None)

    def set_serialized(self, key: Any, value: Any) -> None:
        self._call("set_serialized", lambda: self._inner.set_serialized(key, value), None)

    def set_serialized_multi(self, mapping: Any) -> None:
        self._call("set_serialized_multi",
                   lambda: self._inner.set_serialized_multi(mapping), None)

    def delete(self, key: Any) -> None:
        self._call("delete", lambda: self._inner.delete(key), None)

    def delete_multi(self, keys: Any) -> None:
        self._call("delete_multi", lambda: self._inner.delete_multi(keys), None)


def _no_value() -> Any:
    from dogpile.cache.api import NO_VALUE

    return NO_VALUE


def create_region(
    url: str = "",
    expiration_time: int = 300,
    key_prefix: str = "",
) -> Any:
    """Create a dogpile.cache region backed by Redis (or null when unavailable).

    Args:
        url: Redis URL (e.g. ``redis://localhost:6379``). Falls back to
             ``REDIS_URL`` env var. If empty/unset, uses the null backend
             (no caching — functions are called every time).
        expiration_time: Default TTL in seconds for cached values.
        key_prefix: Prefix prepended to all cache keys for namespace isolation.

    Returns:
        A configured ``dogpile.cache.CacheRegion``.
    """
    try:
        from dogpile.cache import make_region
    except ImportError:
        raise ImportError(
            "Cache support requires dogpile.cache + redis. "
            "Install with: pip install pf-core[redis]"
        )

    def _mangler(key: str) -> str:
        if key_prefix:
            return f"{key_prefix}:{key}"
        return key

    region = make_region(key_mangler=_mangler)

    resolved_url = url or os.environ.get("REDIS_URL", "")
    if resolved_url:
        try:
            region.configure(
                "dogpile.cache.redis",
                arguments={
                    "url": resolved_url,
                    "distributed_lock": False,
                },
                expiration_time=expiration_time,
            )
            region.backend = _ResilientBackend(region.backend)
            logger.debug("cache_region_configured", backend="redis", prefix=key_prefix)
        except Exception:
            region.configure("dogpile.cache.null")
            logger.debug("cache_region_fallback", backend="null", prefix=key_prefix)
    else:
        region.configure("dogpile.cache.null")
        logger.debug("cache_region_configured", backend="null", prefix=key_prefix)

    return region


# ---------------------------------------------------------------------------
# Backward-compatible RedisCache wrapper
# ---------------------------------------------------------------------------

class RedisCache:
    """Backward-compatible wrapper around a dogpile.cache region.

    New code should use ``create_region()`` directly. This class exists
    so projects that already import ``RedisCache`` keep working during
    migration.
    """

    def __init__(
        self,
        url: str | None = None,
        default_ttl: int = 300,
        key_prefix: str = "",
    ) -> None:
        self._url = url or os.environ.get("REDIS_URL", "")
        self._default_ttl = default_ttl
        self._key_prefix = key_prefix
        self._region = create_region(
            url=self._url,
            expiration_time=default_ttl,
            key_prefix=key_prefix,
        )

    @property
    def available(self) -> bool:
        from dogpile.cache.backends.null import NullBackend
        return not isinstance(self._region.backend, NullBackend)

    def _get_client(self) -> Any:
        """Return the underlying redis client (for legacy callers)."""
        if not self.available:
            return None
        return self._region.backend.reader_client  # type: ignore[attr-defined]

    def get(self, key: str) -> str | None:
        val = self._region.get(key)
        from dogpile.cache.api import NO_VALUE
        if val is NO_VALUE:
            return None
        return val  # type: ignore[return-value]

    def _write(self, op: Any) -> bool:
        backend = self._region.backend
        before = getattr(backend, "degraded_ops", 0)
        try:
            op()
        except Exception:
            return False
        return getattr(backend, "degraded_ops", 0) == before

    def set(self, key: str, value: str) -> bool:
        """Store ``value``; False when a configured backend dropped the write.

        The region TTL applies to every key — dogpile's ``CacheRegion.set``
        takes no per-key expiration.
        """
        return self._write(lambda: self._region.set(key, value))

    def delete(self, key: str) -> bool:
        """Delete ``key``; False when a configured backend dropped the delete."""
        return self._write(lambda: self._region.delete(key))

    def bump_generation(self) -> int:
        """Invalidate this region in **this process only**.

        dogpile's invalidation marker is in-memory, so other processes keep
        serving their cached values. Always returns 0 — there is no shared
        generation counter.
        """
        self._region.invalidate()
        return 0

    def _get_generation(self) -> int:
        return 0

    def cached_json(self, key_parts: tuple, variant: Any, fn: Any, ttl: int | None = None) -> Any:
        import hashlib
        import json
        parts = ":".join(str(p) for p in key_parts)
        variant_hash = ""
        if variant is not None:
            raw = json.dumps(variant, sort_keys=True, default=str)
            variant_hash = ":" + hashlib.md5(raw.encode()).hexdigest()[:10]
        cache_key = f"{parts}{variant_hash}"
        return self._region.get_or_create(cache_key, fn, expiration_time=ttl)


# ---------------------------------------------------------------------------
# Module-level singleton (backward compat)
# ---------------------------------------------------------------------------

_cache: RedisCache | None = None


def get_cache(
    url: str | None = None,
    default_ttl: int = 300,
    key_prefix: str = "",
) -> RedisCache:
    """Return the module-level RedisCache singleton."""
    global _cache
    if _cache is None:
        _cache = RedisCache(url=url, default_ttl=default_ttl, key_prefix=key_prefix)
    return _cache


def reset_cache() -> None:
    """Reset the singleton (useful for testing)."""
    global _cache
    _cache = None
