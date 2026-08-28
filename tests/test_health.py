"""Tests for pf_core.web.health."""

from __future__ import annotations

import asyncio
import inspect
import time
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from pf_core.web.health import health_router, require_db, require_db_sync


@pytest.fixture()
def app_with_health():
    """FastAPI app with health endpoint (DB check enabled)."""
    app = FastAPI()
    app.include_router(health_router(check_db=True))
    return app


@pytest.fixture()
def app_no_checks():
    """FastAPI app with health endpoint (no checks)."""
    app = FastAPI()
    app.include_router(health_router(check_db=False, check_redis=False))
    return app


class TestHealthEndpoint:
    def test_healthy_no_checks(self, app_no_checks):
        client = TestClient(app_no_checks)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"] == {}

    def test_healthy_db_ok(self, app_with_health):
        client = TestClient(app_with_health)
        with patch("pf_core.web.health._check_db", return_value="ok"):
            resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"]["db"] == "ok"

    def test_unhealthy_db_down(self, app_with_health):
        client = TestClient(app_with_health)
        with patch("pf_core.web.health._check_db", return_value="error: connection refused"):
            resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert "error" in data["checks"]["db"]

    def test_check_db_does_not_leak_exception_detail(self):
        from pf_core.web import health

        secret_url = "postgresql://user:s3cr3t@db.internal/app"
        with patch("pf_core.db.ping", side_effect=Exception(f"cannot connect: {secret_url}")):
            result = health._check_db()
        assert result == "error"
        assert "s3cr3t" not in result
        assert "postgresql://" not in result

    def test_redis_check_included(self):
        app = FastAPI()
        app.include_router(health_router(check_db=False, check_redis=True))
        client = TestClient(app)
        with patch("pf_core.web.health._check_redis", return_value="ok"):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["checks"]["redis"] == "ok"

    def test_mixed_checks(self):
        app = FastAPI()
        app.include_router(health_router(check_db=True, check_redis=True))
        client = TestClient(app)
        with (
            patch("pf_core.web.health._check_db", return_value="ok"),
            patch("pf_core.web.health._check_redis", return_value="error: timeout"),
        ):
            resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["checks"]["db"] == "ok"
        assert "error" in data["checks"]["redis"]

    def test_prefix(self):
        app = FastAPI()
        app.include_router(health_router(check_db=False, prefix="/api"))
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200


class TestRequireDb:
    def test_db_available(self):
        app = FastAPI()

        @app.get("/data", dependencies=[Depends(require_db)])
        async def get_data():
            return {"ok": True}

        client = TestClient(app)
        with patch("pf_core.web.health._check_db", return_value="ok"):
            resp = client.get("/data")
        assert resp.status_code == 200

    def test_db_unavailable(self):
        app = FastAPI()

        @app.get("/data", dependencies=[Depends(require_db)])
        async def get_data():
            return {"ok": True}

        client = TestClient(app)
        with patch("pf_core.web.health._check_db", return_value="error: refused"):
            resp = client.get("/data")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestRequireDbSync:
    def test_db_available_returns_none(self):
        with patch("pf_core.web.health._check_db", return_value="ok"):
            assert require_db_sync() is None

    def test_db_unavailable_raises(self):
        with patch("pf_core.web.health._check_db", return_value="error: refused"):
            with pytest.raises(HTTPException) as exc_info:
                require_db_sync()
        assert exc_info.value.status_code == 503
        assert "unavailable" in exc_info.value.detail.lower()

    def test_depends_integration(self):
        app = FastAPI()

        @app.get("/data", dependencies=[Depends(require_db_sync)])
        async def get_data():
            return {"ok": True}

        client = TestClient(app)
        with patch("pf_core.web.health._check_db", return_value="ok"):
            resp = client.get("/data")
        assert resp.status_code == 200
        with patch("pf_core.web.health._check_db", return_value="error: refused"):
            resp = client.get("/data")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


class TestEventLoopSafety:
    """An ``async`` endpoint runs the blocking ping on the loop, stalling the worker."""

    def _health_endpoint(self):
        routes = [r for r in health_router().routes if getattr(r, "path", "") == "/health"]
        assert len(routes) == 1
        return routes[0].endpoint

    def test_health_endpoint_is_not_a_coroutine_function(self):
        assert not inspect.iscoroutinefunction(self._health_endpoint())

    def test_require_db_is_not_a_coroutine_function(self):
        assert not inspect.iscoroutinefunction(require_db)
        assert not inspect.iscoroutinefunction(require_db_sync)

    def test_slow_db_check_does_not_stall_the_event_loop(self):
        import httpx

        app = FastAPI()
        app.include_router(health_router(check_db=True))

        def slow_check() -> str:
            time.sleep(0.3)
            return "ok"

        async def scenario() -> float:
            gaps: list[float] = []

            async def ticker() -> None:
                last = time.perf_counter()
                while True:
                    await asyncio.sleep(0.01)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                tick = asyncio.create_task(ticker())
                await asyncio.sleep(0.05)
                resp = await client.get("/health")
                await asyncio.sleep(0.02)  # let the ticker record the gap it just sat through
                tick.cancel()
            assert resp.status_code == 200
            return max(gaps)

        with patch("pf_core.web.health._check_db", slow_check):
            max_gap = asyncio.run(scenario())
        assert max_gap < 0.2, f"event loop blocked for {max_gap:.2f}s by the blocking db check"
