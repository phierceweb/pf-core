"""Tests for the mountable jobs dashboard (web.jobs_admin)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pf_core.jobs import JobRepo
from pf_core.jobs.registry import register_kind
from pf_core.web.jobs_admin import make_jobs_router

KIND = "dash_probe"


@pytest.fixture
def pf_schema():
    from pf_core.testing.db_fixtures import framework_ddl

    return framework_ddl()


@pytest.fixture(autouse=True)
def _kind(pf_tables):
    register_kind(kind=KIND, description="dashboard test kind")


def _client(**router_kwargs) -> TestClient:
    app = FastAPI()
    app.include_router(make_jobs_router(**router_kwargs))
    return TestClient(app)


def _make_job(status: str = "pending") -> int:
    repo = JobRepo()
    job_id = repo.create(kind=KIND, created_by="test")
    if status in ("running", "succeeded"):
        repo.transition(job_id, "running")
    if status == "succeeded":
        repo.transition(job_id, "succeeded")
    return job_id


class TestListPage:
    def test_lists_jobs_with_labels_and_scope(self):
        job_id = _make_job()
        client = _client(
            kind_labels={KIND: "probe"},
            describe=lambda job: {"label": "scope-x", "href": "/x"},
        )
        r = client.get("/jobs")
        assert r.status_code == 200
        assert f"#{job_id}" in r.text
        assert "probe" in r.text
        assert "scope-x" in r.text

    def test_invalid_sort_falls_back(self):
        _make_job()
        r = _client().get("/jobs?sort=inputs")
        assert r.status_code == 200

    def test_pagination_math(self):
        for _ in range(3):
            _make_job()
        r = _client().get("/jobs?per_page=2&page=2")
        assert r.status_code == 200
        assert "page 2 of 2" in r.text


class TestDetailAndApi:
    def test_detail_page_renders(self):
        job_id = _make_job("running")
        r = _client().get(f"/jobs/{job_id}")
        assert r.status_code == 200
        assert f"#{job_id}" in r.text
        assert "running" in r.text

    def test_detail_404(self):
        assert _client().get("/jobs/999999").status_code == 404

    def test_api_bundle_shape(self):
        job_id = _make_job("running")
        r = _client().get(f"/jobs/api/{job_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["job"]["id"] == job_id
        assert isinstance(data["steps"], list)
        assert isinstance(data["events"], list)

    def test_api_404(self):
        assert _client().get("/jobs/api/999999").status_code == 404


class TestCancel:
    def test_cancel_pending_job(self):
        job_id = _make_job()
        r = _client().post(f"/jobs/api/{job_id}/cancel", json={"reason": "test"})
        assert r.status_code == 200
        assert JobRepo().get(job_id)["status"] == "canceled"

    def test_cancel_terminal_returns_409(self):
        job_id = _make_job("succeeded")
        r = _client().post(f"/jobs/api/{job_id}/cancel")
        assert r.status_code == 409

    def test_terminate_hook_called_first(self):
        killed: list[int] = []
        job_id = _make_job("running")
        client = _client(terminate_hook=lambda jid: killed.append(jid) or True)
        r = client.post(f"/jobs/api/{job_id}/cancel")
        assert r.status_code == 200
        assert killed == [job_id]
        assert JobRepo().get(job_id)["status"] == "canceled"


class TestFindPage:
    def test_rejects_unknown_sort_and_direction(self):
        from pf_core.exceptions import InvalidInputError

        repo = JobRepo()
        with pytest.raises(InvalidInputError):
            repo.find_page(sort="inputs")
        with pytest.raises(InvalidInputError):
            repo.find_page(direction="sideways")

    def test_returns_rows_and_total(self):
        ids = [_make_job() for _ in range(3)]
        rows, total = JobRepo().find_page(sort="id", direction="asc", limit=2)
        assert total == 3
        assert [r["id"] for r in rows] == sorted(ids)[:2]


class TestAuth:
    def test_auth_dep_guards_every_route(self):
        def deny():
            raise HTTPException(403, "no")

        job_id = _make_job()
        client = _client(auth_dep=deny)
        assert client.get("/jobs").status_code == 403
        assert client.get(f"/jobs/{job_id}").status_code == 403
        assert client.get(f"/jobs/api/{job_id}").status_code == 403
        assert client.post(f"/jobs/api/{job_id}/cancel").status_code == 403
