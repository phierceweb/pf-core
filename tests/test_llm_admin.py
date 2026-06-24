"""Tests for pf_core.web.llm_admin — mountable admin sub-app."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pf_core.budget._schema import llm_budgets
from pf_core.budget.repo import BudgetSnapshotRepo
from pf_core.db.connection import transaction
from pf_core.jobs._schema import job_events, jobs
from pf_core.llm.cache import cache_store
from pf_core.llm.tracking import (
    LlmRunRepo,
    clear_resolver_caches,
    metadata,
)
from pf_core.web.llm_admin import make_admin_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    clear_resolver_caches()
    yield
    clear_resolver_caches()


@pytest.fixture()
def admin_db(pf_engine):
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


def _seed(with_jobs: bool = True, with_budgets: bool = True, with_cache: bool = True):
    """Populate a representative sample of rows across all admin domains."""
    run_repo = LlmRunRepo()

    # Happy path run
    r1 = run_repo.record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={"cost_usd": 0.50, "prompt_tokens": 1200, "completion_tokens": 800, "duration_ms": 2100},
        status="success",
        tags=["env:test"],
    )
    # Error run
    r2 = run_repo.record(
        agent_type="drafter",
        model="claude-opus-4-7",
        usage={"cost_usd": 0.05, "prompt_tokens": 500, "completion_tokens": 0, "duration_ms": 150},
        status="error",
        error="upstream timeout",
    )
    # Cache-hit run (zero-cost)
    r3 = run_repo.record(
        agent_type="classifier",
        model="openai/gpt-4o-mini",
        usage={"cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "duration_ms": 1},
        status="cache_hit",
    )

    if with_jobs:
        with transaction() as conn:
            res = conn.execute(
                jobs.insert().values(
                    kind="demo_draft",
                    status="running",
                    progress_total=10,
                    progress_current=3,
                    current_step="drafting_section_5",
                )
            )
            jid = res.inserted_primary_key[0]
            conn.execute(
                job_events.insert().values(
                    job_id=jid, event_type="started", message="job started"
                )
            )

    if with_budgets:
        with transaction() as conn:
            res = conn.execute(
                llm_budgets.insert().values(
                    scope_kind="agent",
                    scope_value="drafter",
                    period="daily",
                    limit_usd=20.0,
                    action="block",
                    enabled=True,
                )
            )
            bid = res.inserted_primary_key[0]
        BudgetSnapshotRepo().upsert(
            budget_id=bid,
            period_start=dt.date.today(),
            spent_usd=2.5,
            run_count=5,
        )

    if with_cache:
        cache_store(
            agent_type="classifier",
            input_hash="a" * 64,
            source_run_id=r1,
            model="openai/gpt-4o-mini",
            parsed_output={"ok": True},
            raw_response='{"ok":true}',
        )

    return {"runs": [r1, r2, r3]}


def _make_client(admin_db, **kwargs):
    app = FastAPI()
    # These route tests exercise the admin open; opt into that explicitly
    # (make_admin_router now refuses to mount unauthenticated by default).
    kwargs.setdefault("allow_unauthenticated", True)
    app.include_router(make_admin_router(**kwargs))
    return TestClient(app, raise_server_exceptions=True)


def test_refuses_unauthenticated_by_default():
    from pf_core.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        make_admin_router()


def test_auth_dep_satisfies_requirement():
    # Passing an auth dependency mounts without needing the opt-in flag.
    make_admin_router(auth_dep=lambda: None)


def test_allow_unauthenticated_opt_in():
    make_admin_router(allow_unauthenticated=True)


# ---------------------------------------------------------------------------
# Basic mounting / routes
# ---------------------------------------------------------------------------


def test_dashboard_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/")
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_runs_list_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/runs")
    assert r.status_code == 200
    assert "drafter" in r.text


def test_run_detail_renders(admin_db):
    seeded = _seed()
    client = _make_client(admin_db)
    r = client.get(f"/admin/llm/run/{seeded['runs'][0]}")
    assert r.status_code == 200
    assert "Sampling" in r.text


def test_run_detail_404_when_missing(admin_db):
    client = _make_client(admin_db)
    r = client.get("/admin/llm/run/99999")
    assert r.status_code == 404


def test_cost_by_model_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/cost-by-model")
    assert r.status_code == 200
    assert "claude-opus-4-7" in r.text


def test_cost_by_agent_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/cost-by-agent")
    assert r.status_code == 200
    assert "drafter" in r.text


def test_jobs_list_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/jobs")
    assert r.status_code == 200
    assert "demo_draft" in r.text


def test_job_detail_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/job/1")
    assert r.status_code == 200
    assert "demo_draft" in r.text
    assert "job started" in r.text  # event rendered


def test_job_detail_404(admin_db):
    client = _make_client(admin_db)
    r = client.get("/admin/llm/job/99999")
    assert r.status_code == 404


def test_cache_page_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/cache")
    assert r.status_code == 200
    assert "classifier" in r.text


def test_budgets_page_renders(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/budgets")
    assert r.status_code == 200
    assert "drafter" in r.text


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------


def test_dashboard_json_shape(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/api/dashboard.json")
    assert r.status_code == 200
    data = r.json()
    assert "data" in data and "meta" in data
    assert "kpis" in data["data"]
    assert data["data"]["kpis"]["total_runs"] >= 3


def test_runs_json_pagination(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/api/runs.json?limit=2&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data["data"]) <= 2
    assert "total" in data["meta"]


def test_cost_by_model_json(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/api/cost-by-model.json")
    assert r.status_code == 200
    payload = r.json()
    models = {row["model"] for row in payload["data"]}
    assert "claude-opus-4-7" in models


def test_budgets_json(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/api/budgets.json")
    assert r.status_code == 200
    payload = r.json()
    assert len(payload["data"]) >= 1
    assert payload["data"][0]["scope_value"] == "drafter"


def test_run_detail_json(admin_db):
    seeded = _seed()
    client = _make_client(admin_db)
    r = client.get(f"/admin/llm/api/run/{seeded['runs'][0]}.json")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["agent_type"] == "drafter"
    assert "payload" in data


# ---------------------------------------------------------------------------
# POST /api/job/{id}/cancel
# ---------------------------------------------------------------------------


def _seed_cancellable_job(*, status: str = "running") -> int:
    """Register a kind with the default state machine and insert one job
    in ``status`` (default ``running`` so cancel is allowed).
    """
    from pf_core.jobs import register_kind

    register_kind(kind="cancel_test_kind")
    with transaction() as conn:
        res = conn.execute(
            jobs.insert().values(
                kind="cancel_test_kind",
                status=status,
                progress_total=10,
                progress_current=3,
            )
        )
        return int(res.inserted_primary_key[0])


def test_cancel_job_succeeds_on_running(admin_db):
    job_id = _seed_cancellable_job()
    client = _make_client(admin_db)

    r = client.post(
        f"/admin/llm/api/job/{job_id}/cancel",
        json={"reason": "user clicked cancel"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["job_id"] == job_id
    assert body["data"]["status"] == "canceled"


def test_cancel_job_writes_canceled_event(admin_db):
    job_id = _seed_cancellable_job()
    client = _make_client(admin_db)

    client.post(
        f"/admin/llm/api/job/{job_id}/cancel",
        json={"reason": "user clicked cancel"},
    )

    # The detail bundle includes events; the canceled event should be there
    detail = client.get(f"/admin/llm/api/job/{job_id}.json").json()["data"]
    events = detail.get("events") or []
    canceled = [e for e in events if e.get("event_type") == "canceled"]
    assert len(canceled) == 1
    assert "user clicked cancel" in (canceled[0].get("message") or "")


def test_cancel_job_404_when_missing(admin_db):
    client = _make_client(admin_db)
    r = client.post("/admin/llm/api/job/99999/cancel", json={"reason": "x"})
    assert r.status_code == 404


def test_cancel_job_409_when_already_terminal(admin_db):
    # Seed a job that's already in a terminal state. ``DEFAULT_TRANSITIONS``
    # has no transition out of ``succeeded``, so the second cancel hits the
    # "cannot transition" path.
    job_id = _seed_cancellable_job(status="succeeded")
    client = _make_client(admin_db)

    r = client.post(f"/admin/llm/api/job/{job_id}/cancel")
    assert r.status_code == 409
    assert "cannot transition" in r.json()["detail"].lower()


def test_cancel_job_default_reason_when_body_omitted(admin_db):
    job_id = _seed_cancellable_job()
    client = _make_client(admin_db)

    r = client.post(f"/admin/llm/api/job/{job_id}/cancel")
    assert r.status_code == 200

    detail = client.get(f"/admin/llm/api/job/{job_id}.json").json()["data"]
    events = detail.get("events") or []
    canceled = [e for e in events if e.get("event_type") == "canceled"]
    assert len(canceled) == 1
    # Default message wired into the route — useful for audit trails
    assert "canceled via admin" in canceled[0]["message"]


# ---------------------------------------------------------------------------
# Auth hook
# ---------------------------------------------------------------------------


def test_auth_dep_protects_routes(admin_db):
    _seed()

    def deny():
        raise HTTPException(status_code=403)

    app = FastAPI()
    app.include_router(make_admin_router(auth_dep=deny))
    client = TestClient(app, raise_server_exceptions=True)

    r = client.get("/admin/llm/")
    assert r.status_code == 403
    r = client.get("/admin/llm/api/dashboard.json")
    assert r.status_code == 403


def test_auth_dep_allows_when_passes(admin_db):
    _seed()

    def allow():
        return {"user": "admin"}

    app = FastAPI()
    app.include_router(make_admin_router(auth_dep=allow))
    client = TestClient(app, raise_server_exceptions=True)

    r = client.get("/admin/llm/")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Empty state & prefix override
# ---------------------------------------------------------------------------


def test_empty_state_renders_friendly_message(admin_db):
    client = _make_client(admin_db)
    r = client.get("/admin/llm/runs")
    assert r.status_code == 200
    assert "No runs" in r.text


def test_custom_prefix(admin_db):
    _seed()
    app = FastAPI()
    app.include_router(make_admin_router(prefix="/ops/llm", allow_unauthenticated=True))
    client = TestClient(app, raise_server_exceptions=True)
    r = client.get("/ops/llm/")
    assert r.status_code == 200
    r = client.get("/admin/llm/")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Config resolver
# ---------------------------------------------------------------------------


def test_config_resolver_renders_readable_label(admin_db):
    run_repo = LlmRunRepo()
    run_id = run_repo.record(
        agent_type="drafter",
        model="claude-opus-4-7",
        configs={"report_config": 42},
    )

    resolvers = {"report_config": lambda cid: f"Report #{cid}"}
    client = _make_client(admin_db, config_resolvers=resolvers)

    r = client.get(f"/admin/llm/run/{run_id}")
    assert r.status_code == 200
    assert "Report #42" in r.text


def test_unresolved_config_falls_back_to_kind_colon_id(admin_db):
    run_repo = LlmRunRepo()
    run_id = run_repo.record(
        agent_type="drafter",
        model="claude-opus-4-7",
        configs={"unknown_kind": 7},
    )
    client = _make_client(admin_db)
    r = client.get(f"/admin/llm/run/{run_id}")
    assert r.status_code == 200
    assert "unknown_kind:7" in r.text


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_runs_filter_by_agent(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/api/runs.json?agent_type=classifier")
    assert r.status_code == 200
    data = r.json()["data"]
    assert all(row["agent_type"] == "classifier" for row in data)


def test_runs_filter_by_status(admin_db):
    _seed()
    client = _make_client(admin_db)
    r = client.get("/admin/llm/api/runs.json?status=error")
    assert r.status_code == 200
    data = r.json()["data"]
    assert all(row["status"] == "error" for row in data)
