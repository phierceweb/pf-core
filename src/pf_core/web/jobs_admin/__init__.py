"""Mountable jobs dashboard + JSON API.

The jobs sibling of :mod:`pf_core.web.llm_admin`: list and detail pages
over the ``jobs`` tables plus a polling JSON bundle and a cancel endpoint,
mounted into a consumer app with one call::

    from pf_core.web.jobs_admin import make_jobs_router

    app.include_router(make_jobs_router(
        auth_dep=require_admin,
        kind_labels={"grading_pass": "grade"},
        describe=lambda job: {"label": short_scope(job), "href": section_url(job)},
        terminate_hook=terminate_job,      # subprocess-mode consumers only
    ))

Templates are self-contained (no consumer base template or CSS assumed);
pass ``templates=`` to reskin. Cancel is soft — transition the row, 409 on
an already-terminal job — with ``terminate_hook`` invoked first when the
consumer's jobs run as killable subprocesses.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from pf_core.exceptions import PreconditionError
from pf_core.jobs.repo import JobRepo
from pf_core.web.json import safe_json_response
from pf_core.web.pagination import paginate_params

__all__ = ["make_jobs_router"]

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_SORTS = {"id", "kind", "status", "created_at"}


class _CancelBody(BaseModel):
    reason: str | None = None


def _split_bundle(bundle: dict) -> dict:
    steps = bundle.get("steps") or []
    events = bundle.get("events") or []
    job = {k: v for k, v in bundle.items() if k not in ("steps", "events")}
    return {"job": job, "steps": steps, "events": events}


def make_jobs_router(
    *,
    auth_dep: Callable | None = None,
    kind_labels: dict[str, str] | None = None,
    describe: Callable[[dict], dict | None] | None = None,
    terminate_hook: Callable[[int], bool] | None = None,
    templates: Jinja2Templates | None = None,
    prefix: str = "/jobs",
) -> APIRouter:
    """Build the jobs dashboard router.

    Args:
        auth_dep: FastAPI dependency guarding every route (``None`` = open).
        kind_labels: kind → human action label for the list/detail pages.
        describe: ``job_row -> {"label": str, "href": str} | None`` — the
            consumer's scope link (a section, a document, …).
        terminate_hook: ``job_id -> bool`` invoked before the soft cancel;
            pass :func:`pf_core.jobs.workers.terminate_job` when jobs run
            as subprocesses. ``None`` for thread-mode consumers.
        templates: Override the self-contained default templates.
        prefix: Mount prefix for pages (``{prefix}``, ``{prefix}/{id}``) and
            API (``{prefix}/api/...``).
    """
    labels = kind_labels or {}
    if templates is None:
        templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.globals["jobs_prefix"] = prefix

    deps = [Depends(auth_dep)] if auth_dep is not None else []
    router = APIRouter(prefix=prefix, dependencies=deps, tags=["jobs-admin"])

    def _display(job: dict) -> dict:
        scope = describe(job) if describe is not None else None
        return {
            **job,
            "action": labels.get(job.get("kind"), job.get("kind")),
            "scope": scope or {},
        }

    @router.get("", response_model=None)
    def jobs_list(
        request: Request,
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=200),
        sort: str = Query("created_at"),
        dir: str = Query("desc"),
    ):
        pg = paginate_params(
            page, per_page, sort, dir,
            allowed_sorts=_SORTS, default_sort="created_at", default_dir="desc",
        )
        rows, total = JobRepo().find_page(
            sort=pg["sort"], direction=pg["dir"],
            limit=pg["per_page"], offset=pg["offset"],
        )
        total_pages = max(1, math.ceil(total / pg["per_page"]))
        return templates.TemplateResponse(
            request,
            "jobs_list.html",
            {
                "jobs": [_display(r) for r in rows],
                "total": total,
                "total_pages": total_pages,
                "page": pg["page"],
                "per_page": pg["per_page"],
                "sort": pg["sort"],
                "dir": pg["dir"],
                "has_prev": pg["page"] > 1,
                "has_next": pg["page"] < total_pages,
            },
        )

    @router.get("/api/{job_id}")
    def job_bundle(job_id: int):
        bundle = JobRepo().get_with_steps(job_id)
        if bundle is None:
            raise HTTPException(404, f"job {job_id} not found")
        return safe_json_response(_split_bundle(bundle))

    @router.post("/api/{job_id}/cancel")
    def cancel_job(job_id: int, body: _CancelBody | None = None):
        reason = (body.reason if body else None) or "canceled from web"
        if terminate_hook is not None:
            terminate_hook(job_id)
        repo = JobRepo()
        try:
            repo.cancel(job_id, reason=reason)
        except PreconditionError as exc:
            # Already terminal (or unknown) — 409 tells the UI to refresh,
            # not retry.
            raise HTTPException(409, str(exc)) from exc
        bundle = repo.get_with_steps(job_id)
        if bundle is None:
            return safe_json_response({"ok": True})
        return safe_json_response(_split_bundle(bundle))

    @router.get("/{job_id}", response_model=None)
    def job_detail(request: Request, job_id: int):
        bundle = JobRepo().get_with_steps(job_id)
        if bundle is None:
            raise HTTPException(404, f"job {job_id} not found")
        parts = _split_bundle(bundle)
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "job": _display(parts["job"]),
                "steps": parts["steps"],
                "events": parts["events"],
            },
        )

    return router
