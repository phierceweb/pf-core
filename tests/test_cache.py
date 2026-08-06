"""Tests for pf_core.cache.redis — Redis-backed caching."""

from __future__ import annotations

from pathlib import Path

import pytest

from pf_core.cache.redis import (
    RedisCache,
    create_region,
    get_cache,
    reset_cache,
)

CACHE_DOC = Path(__file__).resolve().parents[1] / "src" / "pf_core" / "docs" / "cache.md"


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


def _closed_port() -> int:
    """A port nothing is listening on — bind, read it back, release."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestUnreachableRedis:
    """docs/cache.md promises degradation when "no URL configured, OR Redis
    is down". dogpile connects lazily, so a configured-but-down server still
    yields a live Redis backend — the half that was never implemented."""

    @pytest.fixture
    def region(self):
        return create_region(url=f"redis://127.0.0.1:{_closed_port()}/0",
                             key_prefix="down")

    def test_get_returns_no_value(self, region):
        from dogpile.cache.api import NO_VALUE

        assert region.get("k") is NO_VALUE

    def test_set_is_a_noop(self, region):
        region.set("k", "v")

    def test_delete_is_a_noop(self, region):
        region.delete("k")

    def test_invalidate_is_a_noop(self, region):
        region.invalidate()

    def test_get_or_create_calls_the_creator(self, region):
        calls = []

        def _creator():
            calls.append(1)
            return "computed"

        assert region.get_or_create("k", _creator) == "computed"
        # No caching is possible, so the creator runs every time.
        assert region.get_or_create("k", _creator) == "computed"
        assert len(calls) == 2

    def test_get_multi_returns_no_value_per_key(self, region):
        from dogpile.cache.api import NO_VALUE

        assert region.get_multi(["a", "b"]) == [NO_VALUE, NO_VALUE]

    def test_set_multi_is_a_noop(self, region):
        region.set_multi({"a": 1, "b": 2})

    def test_underlying_client_stays_reachable_for_probes(self, region):
        """Consumers that want a truthful health check still need the client
        (to PING it) — degradation must not hide the backend."""
        assert region.backend.reader_client is not None


class TestResilientBackendPassthrough:
    """Degrading on an outage must not change behaviour on a healthy server."""

    class _Inner:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def _record(*a):
                self.calls.append((name, *a))
                return f"{name}-result"
            return _record

    def _wrapped(self):
        from pf_core.cache.redis import _ResilientBackend

        inner = self._Inner()
        return _ResilientBackend(inner), inner

    def test_reads_return_the_inner_value(self):
        backend, inner = self._wrapped()
        assert backend.get("k") == "get-result"
        assert backend.get_serialized("k") == "get_serialized-result"
        assert inner.calls == [("get", "k"), ("get_serialized", "k")]

    def test_writes_reach_the_inner_backend(self):
        backend, inner = self._wrapped()
        backend.set("k", "v")
        backend.set_serialized("k", b"v")
        backend.delete("k")
        assert inner.calls == [
            ("set", "k", "v"), ("set_serialized", "k", b"v"), ("delete", "k"),
        ]

    def test_unknown_attributes_pass_through(self):
        backend, _ = self._wrapped()
        assert backend.reader_client("x") == "reader_client-result"

    def test_an_error_outside_the_redis_families_propagates(self):
        """Degradation covers RedisError and OSError; a bug in a mangler or
        serializer is neither, and must stay loud."""
        from pf_core.cache.redis import _ResilientBackend

        class _Broken:
            def get(self, key):
                raise TypeError("a real bug")

        with pytest.raises(TypeError):
            _ResilientBackend(_Broken()).get("k")

    def test_a_non_connection_redis_error_degrades(self):
        """The whole Redis error surface degrades, not just refused
        connections — an OOM or read-only replica must not reach the caller."""
        from dogpile.cache.api import NO_VALUE
        from redis.exceptions import ResponseError

        from pf_core.cache.redis import _ResilientBackend

        class _Oom:
            def get(self, key):
                raise ResponseError("OOM command not allowed when used memory > 'maxmemory'")

            def set(self, key, value):
                raise ResponseError("READONLY You can't write against a read only replica")

        backend = _ResilientBackend(_Oom())
        assert backend.get("k") is NO_VALUE
        backend.set("k", "v")


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


class TestDroppedWritesAreReported:
    """A configured backend that silently drops the write must report False —
    the degradation wrapper absorbs the exception, so the return value is the
    only channel left."""

    @pytest.fixture
    def cache(self):
        return RedisCache(url=f"redis://127.0.0.1:{_closed_port()}/0", key_prefix="down")

    def test_set_reports_the_dropped_write(self, cache):
        assert cache.set("k", "v") is False

    def test_delete_reports_the_dropped_write(self, cache):
        assert cache.delete("k") is False

    def test_a_reachable_backend_still_reports_success(self, monkeypatch):
        from pf_core.cache.redis import _ResilientBackend

        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        cache._region.backend = _ResilientBackend(TestResilientBackendPassthrough._Inner())
        assert cache.set("k", "v") is True
        assert cache.delete("k") is True


class TestPerKeyTtl:
    def test_set_takes_no_per_key_ttl(self, monkeypatch):
        """dogpile's ``CacheRegion.set`` has no expiration_time, so a ttl on
        ``set`` cannot be honoured and must not be silently accepted."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        with pytest.raises(TypeError):
            cache.set("k", "v", 60)

    def test_cached_json_passes_ttl_to_the_region(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        cache = RedisCache()
        seen = {}

        def _spy(key, creator, expiration_time=None, **kw):
            seen["expiration_time"] = expiration_time
            return creator()

        cache._region.get_or_create = _spy
        assert cache.cached_json(("a",), None, lambda: 1, ttl=42) == 1
        assert seen["expiration_time"] == 42

    def test_cached_json_ttl_reaches_the_real_region_api(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        assert RedisCache().cached_json(("a",), None, lambda: 1, ttl=42) == 1


class TestBumpGenerationScope:
    def test_invalidation_does_not_reach_another_region(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        bumped, other = RedisCache(), RedisCache()
        bumped.bump_generation()
        assert bumped._region.region_invalidator.was_hard_invalidated()
        assert not other._region.region_invalidator.was_hard_invalidated()

    def test_docstring_states_the_scope(self):
        assert "process" in (RedisCache.bump_generation.__doc__ or "").lower()

    def test_doc_states_the_scope(self):
        assert "process-local" in CACHE_DOC.read_text().lower()


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
