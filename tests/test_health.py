"""Tests for pf_core.web.health."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pf_core.web.health import health_router, require_db


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
