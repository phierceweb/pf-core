"""
JSON API mirrors for the LLM admin sub-app.

Every HTML page has a matching ``/api/<name>.json`` endpoint returning the
same data in a stable ``{data, meta}`` shape.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from pf_core.exceptions import PreconditionError
from pf_core.jobs import JobRepo
from pf_core.web.llm_admin import queries as q


def register_api_routes(router: APIRouter) -> None:
    """Attach all JSON endpoints to *router*."""

    @router.get("/api/dashboard.json")
    def dashboard_json(since: str | None = None, until: str | None = None):
        s, u = q.parse_window(since, until, default_days=7)
        return {
            "data": {
                "kpis": q.dashboard_kpis(since=s, until=u),
                "top_agents": q.top_agents_by_cost(since=s, until=u),
                "blocked_24h": q.blocked_runs_24h(),
            },
            "meta": {"since": s.isoformat(), "until": u.isoformat()},
        }

    @router.get("/api/runs.json")
    def runs_json(
        since: str | None = None,
        until: str | None = None,
        agent_type: str | None = None,
        model: str | None = None,
        status: str | None = None,
        job_id: int | None = None,
        min_cost: float | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        s, u = q.parse_window(since, until, default_days=7)
        rows = q.list_runs(
            since=s, until=u, agent_type=agent_type, model=model, status=status,
            job_id=job_id, min_cost=min_cost, limit=limit, offset=offset,
        )
        total = q.count_runs(
            since=s, until=u, agent_type=agent_type, model=model, status=status,
            job_id=job_id, min_cost=min_cost,
        )
        return {
            "data": rows,
            "meta": {
                "since": s.isoformat(), "until": u.isoformat(),
                "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
            },
        }

    @router.get("/api/run/{run_id}.json")
    def run_detail_json(run_id: int):
        detail = q.run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"data": detail, "meta": {"run_id": run_id}}

    @router.get("/api/cost-by-model.json")
    def cost_by_model_json(since: str | None = None, until: str | None = None):
        s, u = q.parse_window(since, until, default_days=7)
        return {
            "data": q.cost_by_model(since=s, until=u),
            "meta": {"since": s.isoformat(), "until": u.isoformat()},
        }

    @router.get("/api/cost-by-agent.json")
    def cost_by_agent_json(since: str | None = None, until: str | None = None):
        s, u = q.parse_window(since, until, default_days=7)
        return {
            "data": q.cost_by_agent(since=s, until=u),
            "meta": {"since": s.isoformat(), "until": u.isoformat()},
        }

    @router.get("/api/jobs.json")
    def jobs_json(
        status: str | None = None,
        kind: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        rows = q.list_jobs(status=status, kind=kind, limit=limit, offset=offset)
        total = q.count_jobs(status=status, kind=kind)
        return {
            "data": rows,
            "meta": {
                "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
            },
        }

    @router.get("/api/job/{job_id}.json")
    def job_detail_json(job_id: int):
        detail = q.job_detail(job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"data": detail, "meta": {"job_id": job_id}}

    @router.post("/api/job/{job_id}/cancel")
    def cancel_job(
        job_id: int,
        body: dict | None = Body(default=None),
    ):
        """Soft-cancel a pending or running job.

        Transitions the job to ``canceled`` and writes a ``canceled``
        event. In-flight workers are not killed — they discover the
        cancellation on their next step transition. Project code that
        also tracks an in-memory job (e.g. a ``JobManager``) should
        layer that bookkeeping in its own wrapper around this route.

        Body (optional)::

            {"reason": "user clicked cancel"}

        Status codes:
            200 — canceled, returns the updated job detail
            404 — job not found
            409 — job already terminal (succeeded / failed / canceled)
        """
        repo = JobRepo()
        if repo.get(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        reason = (body or {}).get("reason") if body else None
        try:
            repo.cancel(job_id, reason=reason or "canceled via admin")
        except PreconditionError as exc:
            # cancel() bubbles up "cannot transition" when the job is
            # already terminal — surface as 409 so the UI can refresh
            # state without retrying.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        detail = q.job_detail(job_id)
        return {"data": detail, "meta": {"job_id": job_id}}

    @router.get("/api/cache.json")
    def cache_json(since: str | None = None, until: str | None = None):
        s, u = q.parse_window(since, until, default_days=7)
        return {
            "data": {
                "hit_rate": q.cache_hit_rate_by_agent(since=s, until=u),
                "top_entries": q.top_cache_entries(limit=50),
            },
            "meta": {"since": s.isoformat(), "until": u.isoformat()},
        }

    @router.get("/api/budgets.json")
    def budgets_json():
        return {
            "data": q.list_budgets_with_spend(),
            "meta": {"blocked_24h": q.blocked_runs_24h()},
        }
