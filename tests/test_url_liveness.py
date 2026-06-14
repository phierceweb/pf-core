"""Tests for ``pf_core.utils.url_liveness``.

Network calls (``check_url`` and ``_get_with_browser_ua``) are mocked. The
cache is exercised via a tiny in-memory backend so the production redis
contract is unit-tested here without a live redis dependency.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx

from pf_core.utils.url_liveness import (
    CacheBackend,
    DEFAULT_CACHE_TTL_SECONDS,
    _get_with_browser_ua,
    check_url_cached,
)


# ---------------------------------------------------------------------------
# Fake cache backend (mimics the redis-py contract)
# ---------------------------------------------------------------------------


class FakeCache:
    """Minimal in-memory backend that satisfies :class:`CacheBackend`."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self.last_ttl: int | None = None
        self.get_calls: list[str] = []
        self.setex_calls: list[tuple[str, int, str]] = []

    def get(self, key: str) -> bytes | None:
        self.get_calls.append(key)
        val = self._data.get(key)
        if val is None:
            return None
        return val.encode("utf-8")

    def setex(self, key: str, time: int, value: str) -> None:
        self._data[key] = value
        self.last_ttl = time
        self.setex_calls.append((key, time, value))


# ---------------------------------------------------------------------------
# check_url_cached
# ---------------------------------------------------------------------------


class TestCheckUrlCached:
    def test_empty_url_returns_error(self):
        assert check_url_cached("") == (0, "error")

    def test_disabled_short_circuits_without_network(self):
        # If disabled=True, neither check_url nor cache reads are attempted.
        cache = FakeCache()
        with patch("pf_core.utils.url_liveness.check_url") as mock_check:
            assert check_url_cached("https://example.com", disabled=True) == (0, "disabled")
            mock_check.assert_not_called()
        assert cache.get_calls == []

    def test_ok_passes_through(self):
        with patch(
            "pf_core.utils.url_liveness.check_url", return_value=(200, "ok")
        ):
            assert check_url_cached("https://example.com/ok") == (200, "ok")

    def test_404_passes_through(self):
        with patch(
            "pf_core.utils.url_liveness.check_url",
            return_value=(404, "not_found"),
        ):
            assert check_url_cached("https://example.com/fake") == (404, "not_found")

    def test_forbidden_triggers_get_fallback_success(self):
        """HEAD 403 → GET with browser UA → 200 = real content (e.g. NYT)."""
        with patch(
            "pf_core.utils.url_liveness.check_url",
            return_value=(403, "forbidden"),
        ), patch(
            "pf_core.utils.url_liveness._get_with_browser_ua",
            return_value=(200, "ok"),
        ) as mock_get:
            code, cat = check_url_cached("https://paywalled.example/real")
        assert (code, cat) == (200, "ok")
        mock_get.assert_called_once()

    def test_forbidden_get_fallback_still_forbidden(self):
        """HEAD 403 → GET 403 → stays forbidden (real bot-block)."""
        with patch(
            "pf_core.utils.url_liveness.check_url",
            return_value=(403, "forbidden"),
        ), patch(
            "pf_core.utils.url_liveness._get_with_browser_ua",
            return_value=(403, "forbidden"),
        ):
            assert check_url_cached("https://paywalled.example/x") == (403, "forbidden")

    def test_401_also_triggers_get_fallback(self):
        """HEAD 401 → GET fallback. 404 via GET indicates fabrication."""
        with patch(
            "pf_core.utils.url_liveness.check_url",
            return_value=(401, "http_401"),
        ), patch(
            "pf_core.utils.url_liveness._get_with_browser_ua",
            return_value=(404, "not_found"),
        ):
            assert check_url_cached("https://x.example/y") == (404, "not_found")


class TestCacheBehavior:
    def test_cache_none_skips_read_and_write(self):
        with patch(
            "pf_core.utils.url_liveness.check_url", return_value=(200, "ok")
        ):
            # No cache passed; nothing to inspect, but also nothing should error.
            assert check_url_cached("https://example.com/x", cache=None) == (200, "ok")

    def test_cache_hit_skips_network(self):
        cache = FakeCache()
        cache._data["url_liveness:https://example.com/cached"] = '[200, "ok"]'
        with patch("pf_core.utils.url_liveness.check_url") as mock_check:
            result = check_url_cached("https://example.com/cached", cache=cache)
        assert result == (200, "ok")
        mock_check.assert_not_called()

    def test_cache_miss_writes_through(self):
        cache = FakeCache()
        with patch(
            "pf_core.utils.url_liveness.check_url", return_value=(200, "ok")
        ):
            check_url_cached("https://example.com/new", cache=cache)
        assert cache.setex_calls
        key, ttl, value = cache.setex_calls[0]
        assert key == "url_liveness:https://example.com/new"
        assert ttl == DEFAULT_CACHE_TTL_SECONDS
        assert value == '[200, "ok"]'

    def test_custom_cache_key_prefix(self):
        cache = FakeCache()
        with patch(
            "pf_core.utils.url_liveness.check_url", return_value=(200, "ok")
        ):
            check_url_cached(
                "https://example.com/x",
                cache=cache,
                cache_key_prefix="myapp:liveness:",
            )
        assert cache.setex_calls[0][0] == "myapp:liveness:https://example.com/x"

    def test_custom_cache_ttl(self):
        cache = FakeCache()
        with patch(
            "pf_core.utils.url_liveness.check_url", return_value=(200, "ok")
        ):
            check_url_cached(
                "https://example.com/x",
                cache=cache,
                cache_ttl_seconds=3600,
            )
        assert cache.last_ttl == 3600

    def test_cache_corrupt_value_falls_through_to_network(self):
        """Garbage in cache shouldn't crash — silently re-check."""
        cache = FakeCache()
        cache._data["url_liveness:https://example.com/x"] = "not-json"
        with patch(
            "pf_core.utils.url_liveness.check_url", return_value=(200, "ok")
        ) as mock_check:
            assert check_url_cached("https://example.com/x", cache=cache) == (200, "ok")
            mock_check.assert_called_once()

    def test_cache_get_raises_falls_through(self):
        """Cache backend exception must not abort the liveness check."""

        class BrokenCache:
            def get(self, key):
                raise RuntimeError("redis down")

            def setex(self, key, time, value):
                raise RuntimeError("redis down")

        with patch(
            "pf_core.utils.url_liveness.check_url", return_value=(200, "ok")
        ):
            assert check_url_cached(
                "https://example.com/x", cache=BrokenCache()
            ) == (200, "ok")

    def test_cache_backend_protocol_runtime_compatibility(self):
        """``CacheBackend`` is a Protocol — duck-typing is what matters."""
        # Smoke test: FakeCache is not a subclass but satisfies the shape.
        assert hasattr(CacheBackend, "get")
        assert hasattr(CacheBackend, "setex")


# ---------------------------------------------------------------------------
# _get_with_browser_ua
# ---------------------------------------------------------------------------


class TestGetWithBrowserUa:
    def test_200_returns_ok(self):
        with patch("httpx.Client") as mock_client:
            resp = type("R", (), {"status_code": 200})()
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            assert _get_with_browser_ua("https://x.example") == (200, "ok")

    def test_404_returns_not_found(self):
        with patch("httpx.Client") as mock_client:
            resp = type("R", (), {"status_code": 404})()
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            assert _get_with_browser_ua("https://x.example") == (404, "not_found")

    def test_403_returns_forbidden(self):
        with patch("httpx.Client") as mock_client:
            resp = type("R", (), {"status_code": 403})()
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            assert _get_with_browser_ua("https://x.example") == (403, "forbidden")

    def test_410_returns_gone(self):
        with patch("httpx.Client") as mock_client:
            resp = type("R", (), {"status_code": 410})()
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            assert _get_with_browser_ua("https://x.example") == (410, "gone")

    def test_500_returns_http_code(self):
        with patch("httpx.Client") as mock_client:
            resp = type("R", (), {"status_code": 500})()
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            assert _get_with_browser_ua("https://x.example") == (500, "http_500")

    def test_timeout_returns_timeout(self):
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.side_effect = (
                httpx.TimeoutException("timeout")
            )
            assert _get_with_browser_ua("https://x.example") == (0, "timeout")

    def test_other_exception_returns_error(self):
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.side_effect = (
                RuntimeError("transport failure")
            )
            assert _get_with_browser_ua("https://x.example") == (0, "error")


class TestReExports:
    def test_check_url_cached_importable_from_pf_core_utils(self):
        from pf_core.utils import check_url_cached as exported
        from pf_core.utils.url_liveness import check_url_cached

        assert exported is check_url_cached

    def test_cache_backend_importable_from_pf_core_utils(self):
        from pf_core.utils import CacheBackend as exported
        from pf_core.utils.url_liveness import CacheBackend

        assert exported is CacheBackend
