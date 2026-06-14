"""
HTML page routes for the LLM admin sub-app.

Every page is a thin wrapper: parse window + filters → call :mod:`queries` →
pass to template. Business logic belongs in the repo layer, not here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from pf_core.web.llm_admin import queries as q


def register_page_routes(router: APIRouter, templates: Jinja2Templates) -> None:
    """Attach all HTML endpoints to *router*."""

    @router.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        since: str | None = None,
        until: str | None = None,
    ):
        s, u = q.parse_window(since, until, default_days=7)
        s24 = u - _hours(24)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "kpis_7d": q.dashboard_kpis(since=s, until=u),
                "kpis_24h": q.dashboard_kpis(since=s24, until=u),
                "top_agents": q.top_agents_by_cost(since=s, until=u),
                "since": s,
                "until": u,
                "blocked_24h": q.blocked_runs_24h(),
            },
        )

    @router.get("/runs", response_class=HTMLResponse)
    def runs_list(
        request: Request,
        since: str | None = None,
        until: str | None = None,
        agent_type: str | None = None,
        model: str | None = None,
        status: str | None = None,
        job_id: int | None = None,
        min_cost: float | None = None,
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=500),
    ):
        s, u = q.parse_window(since, until, default_days=7)
        offset = (page - 1) * per_page
        rows = q.list_runs(
            since=s, until=u, agent_type=agent_type, model=model, status=status,
            job_id=job_id, min_cost=min_cost, limit=per_page, offset=offset,
        )
        total = q.count_runs(
            since=s, until=u, agent_type=agent_type, model=model, status=status,
            job_id=job_id, min_cost=min_cost,
        )
        return templates.TemplateResponse(
            request,
            "runs_list.html",
            {
                "runs": rows,
                "total": total,
                "page": page,
                "per_page": per_page,
                "has_prev": page > 1,
                "has_next": offset + per_page < total,
                "filters": {
                    "since": since, "until": until,
                    "agent_type": agent_type, "model": model, "status": status,
                    "job_id": job_id, "min_cost": min_cost,
                },
            },
        )

    @router.get("/run/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        detail = q.run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return templates.TemplateResponse(request, "run_detail.html", {"run": detail})

    @router.get("/cost-by-model", response_class=HTMLResponse)
    def cost_by_model(
        request: Request, since: str | None = None, until: str | None = None
    ):
        s, u = q.parse_window(since, until, default_days=7)
        return templates.TemplateResponse(
            request,
            "cost_by_model.html",
            {"rows": q.cost_by_model(since=s, until=u), "since": s, "until": u},
        )

    @router.get("/cost-by-agent", response_class=HTMLResponse)
    def cost_by_agent(
        request: Request, since: str | None = None, until: str | None = None
    ):
        s, u = q.parse_window(since, until, default_days=7)
        return templates.TemplateResponse(
            request,
            "cost_by_agent.html",
            {"rows": q.cost_by_agent(since=s, until=u), "since": s, "until": u},
        )

    @router.get("/jobs", response_class=HTMLResponse)
    def jobs_list(
        request: Request,
        status: str | None = None,
        kind: str | None = None,
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=500),
    ):
        offset = (page - 1) * per_page
        rows = q.list_jobs(status=status, kind=kind, limit=per_page, offset=offset)
        total = q.count_jobs(status=status, kind=kind)
        return templates.TemplateResponse(
            request,
            "jobs_list.html",
            {
                "jobs": rows,
                "total": total,
                "page": page,
                "per_page": per_page,
                "has_prev": page > 1,
                "has_next": offset + per_page < total,
                "filters": {"status": status, "kind": kind},
            },
        )

    @router.get("/job/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int):
        detail = q.job_detail(job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="job not found")
        return templates.TemplateResponse(request, "job_detail.html", {"job": detail})

    @router.get("/cache", response_class=HTMLResponse)
    def cache_page(
        request: Request, since: str | None = None, until: str | None = None
    ):
        s, u = q.parse_window(since, until, default_days=7)
        return templates.TemplateResponse(
            request,
            "cache.html",
            {
                "hit_rate": q.cache_hit_rate_by_agent(since=s, until=u),
                "top_entries": q.top_cache_entries(limit=50),
                "since": s,
                "until": u,
            },
        )

    @router.get("/budgets", response_class=HTMLResponse)
    def budgets_page(request: Request):
        return templates.TemplateResponse(
            request,
            "budgets.html",
            {
                "budgets": q.list_budgets_with_spend(),
                "blocked_24h": q.blocked_runs_24h(),
            },
        )


def _hours(n: int):
    import datetime as dt

    return dt.timedelta(hours=n)
