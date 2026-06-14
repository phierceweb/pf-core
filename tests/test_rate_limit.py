"""Tests for pf_core.web.rate_limit — rate limiting setup."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pf_core.web.rate_limit import setup_rate_limit


@pytest.fixture()
def app():
    """Bare FastAPI app for testing."""
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


class TestSetupRateLimit:
    def test_returns_limiter(self, app):
        limiter = setup_rate_limit(app)
        assert limiter is not None

    def test_attaches_to_app_state(self, app):
        limiter = setup_rate_limit(app)
        assert app.state.limiter is limiter

    def test_reads_env_var(self, app, monkeypatch):
        monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "42")
        limiter = setup_rate_limit(app)
        assert limiter is not None

    def test_memory_backend_when_no_redis(self, app):
        limiter = setup_rate_limit(app)
        assert limiter is not None


@pytest.mark.anyio
class TestRateLimitEnforcement:
    async def test_allows_requests_under_limit(self, app, monkeypatch):
        monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "10")
        setup_rate_limit(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ping")
            assert resp.status_code == 200

    async def test_blocks_requests_over_limit(self, app, monkeypatch):
        monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "3")
        setup_rate_limit(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(3):
                resp = await client.get("/ping")
                assert resp.status_code == 200

            resp = await client.get("/ping")
            assert resp.status_code == 429


class TestCreateAppIntegration:
    def test_rate_limit_enabled_by_default(self):
        from pf_core.web.app_factory import create_app

        app = create_app(title="Test")
        assert hasattr(app.state, "limiter")
        assert app.state.limiter is not None

    def test_rate_limit_disabled(self):
        from pf_core.web.app_factory import create_app

        app = create_app(title="Test", rate_limit=False)
        assert not hasattr(app.state, "limiter")


class TestRedisUnavailableFallback:
    """A configured-but-unreachable Redis must degrade to in-memory storage,
    not 500 every request.

    Regression: slowapi/limits raises the storage ConnectionError lazily on the
    first request; slowapi's middleware then misroutes it to the
    RateLimitExceeded handler, which crashes on ``exc.detail`` and returns a 500
    for every request. ``setup_rate_limit`` must probe the backend up front and
    fall back to ``memory://`` so the app stays up.
    """

    # Nothing listens on this port → connection refused fast.
    DEAD_REDIS = "redis://127.0.0.1:6399"

    def test_request_succeeds_when_configured_redis_is_down(self, app):
        from starlette.testclient import TestClient

        setup_rate_limit(app, redis_url=self.DEAD_REDIS)
        client = TestClient(app)
        resp = client.get("/ping")
        assert resp.status_code == 200

    def test_falls_back_to_memory_storage(self, app):
        from limits.storage import MemoryStorage

        limiter = setup_rate_limit(app, redis_url=self.DEAD_REDIS)
        assert isinstance(limiter._storage, MemoryStorage)
