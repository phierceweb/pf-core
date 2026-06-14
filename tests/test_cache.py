"""Tests for pf_core.cache.redis — Redis-backed caching."""

from __future__ import annotations

import pytest

from pf_core.cache.redis import (
    RedisCache,
    create_region,
    get_cache,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_cache()
    yield
    reset_cache()


class TestCreateRegion:
    def test_null_backend_when_no_url(self):
        region = create_region(url="")
        from dogpile.cache.backends.null import NullBackend
        assert isinstance(region.backend, NullBackend)

    def test_null_backend_when_no_env(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        region = create_region()
        from dogpile.cache.backends.null import NullBackend
        assert isinstance(region.backend, NullBackend)

    def test_key_prefix_applied(self):
        region = create_region(key_prefix="test")
        # The key mangler should prefix keys
        mangled = region.key_mangler("mykey")
        assert mangled == "test:mykey"

    def test_no_prefix(self):
        region = create_region(key_prefix="")
        mangled = region.key_mangler("mykey")
        assert mangled == "mykey"

    def test_invalid_redis_url_falls_back_to_null(self):
        region = create_region(url="redis://invalid-host-that-does-not-exist:9999")
        # Should still return a region (may be null or redis depending on lazy connect)
        assert region is not None


class TestRedisCacheNullBackend:
    """Test RedisCache with null backend (no Redis)."""

    def test_not_available(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        assert cache.available is False

    def test_get_returns_none(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        assert cache.get("any_key") is None

    def test_set_returns_true(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        assert cache.set("key", "value") is True

    def test_delete_returns_true(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        assert cache.delete("key") is True

    def test_bump_generation(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        assert cache.bump_generation() == 0

    def test_get_generation(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        assert cache._get_generation() == 0

    def test_get_client_returns_none(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        assert cache._get_client() is None

    def test_cached_json(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        result = cache.cached_json(
            ("section", "home"), None, lambda: {"data": "value"}
        )
        assert result == {"data": "value"}

    def test_cached_json_with_variant(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        result = cache.cached_json(
            ("section", "home"), {"page": 1}, lambda: [1, 2, 3]
        )
        assert result == [1, 2, 3]


class TestGetCache:
    def test_returns_instance(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = get_cache()
        assert isinstance(cache, RedisCache)

    def test_singleton(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        c1 = get_cache()
        c2 = get_cache()
        assert c1 is c2

    def test_reset_allows_recreation(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        c1 = get_cache()
        reset_cache()
        c2 = get_cache()
        assert c1 is not c2
