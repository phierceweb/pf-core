"""Day-1 smoke test: the scaffolded app boots and serves the index route."""

from __future__ import annotations

from starlette.testclient import TestClient

from app import app


def test_index_ok():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
