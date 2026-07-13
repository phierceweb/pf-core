# LLM Admin Router

`pf_core.web.llm_admin` — a mountable FastAPI sub-app for reading the LLM tracking, jobs, cache, and budget tables. One import, one line to mount, and the consumer app has an observability surface.

## Quick start

```python
# app/__init__.py
from fastapi import FastAPI, Depends, HTTPException
from pf_core.web.llm_admin import make_admin_router

def require_admin(user=Depends(current_user)):
    if not user or not user.is_admin:
        raise HTTPException(403)
    return user

app = FastAPI()
app.include_router(
    make_admin_router(
        auth_dep=require_admin,
        prefix="/admin/llm",
        config_resolvers={
            "task_config": lambda cid: f"Task #{cid}",
            "prompt_variant": lambda cid: variant_repo.label(cid),
        },
    )
)
```

That's it. The admin is live at `/admin/llm`.

## `make_admin_router`

```python
def make_admin_router(
    *,
    auth_dep: Callable | None = None,
    prefix: str = "/admin/llm",
    config_resolvers: dict[str, Callable[[int], str]] | None = None,
    templates: Jinja2Templates | None = None,
    allow_unauthenticated: bool = False,
) -> APIRouter
```

| Parameter | Purpose |
|---|---|
| `auth_dep` | FastAPI dependency that runs on every route. **Required** — the admin exposes prompts, raw responses, and a job-cancel POST. |
| `prefix` | Mount path. Defaults to `/admin/llm`. |
| `config_resolvers` | Per-`config_kind` callback mapping an integer config id to a human-readable label. Unregistered kinds render as `kind:id`. |
| `templates` | Override the packaged Jinja2 templates (e.g. to inject your own `base.html` for skinning). When omitted, the packaged templates are used. |
| `allow_unauthenticated` | Explicit opt-in to mount with no `auth_dep` (local dev only). Default `False`. |

## Pages

| URL | What it shows |
|---|---|
| `/` | Dashboard — 24h + 7d KPIs, top agents by cost, budget-blocked count |
| `/runs` | Paginated list of `llm_runs` with filters for agent, model, status, job, cost |
| `/run/{id}` | One page with everything about a single run: sampling, payloads, configs, validations, outcomes, links, tags, metrics |
| `/cost-by-model` | Cost + run count + tokens per model |
| `/cost-by-agent` | Cost + run count per agent type |
| `/jobs` | Paginated list of `jobs` with filters for status + kind |
| `/job/{id}` | Job header, steps, events, LLM-run summary |
| `/cache` | Hit rate by agent + top cache entries by hit count |
| `/budgets` | Every active budget with current-period spent, %-of-limit, action |

Each HTML page has a matching `/api/*.json` endpoint returning `{data, meta}`. Drives shell scripts, Grafana (JSON datasource), external dashboards.

## Window parameters

Every aggregate page accepts `?since=&until=` (ISO-8601). Default window is the last 7 days.

```
/admin/llm/cost-by-model?since=2026-04-01&until=2026-04-15
```

JSON variants accept the same params.

## Config resolvers

`llm_run_configs` stores soft FK references as `(config_kind, config_id)` — pf-core doesn't own the referenced tables. The run-detail page renders them by calling the consumer-supplied resolver. Example:

```python
make_admin_router(
    auth_dep=require_admin,
    config_resolvers={
        "task_config":     lambda cid: task_repo.label(cid),   # "Task #42 · batch-7"
        "prompt_variant":  lambda cid: f"v{cid}",
    },
)
```

Unregistered kinds render as `task_config:42`. Resolver exceptions also fall back to that form (no page failure).

## Skinning

Two ways to customize appearance:

1. **Pass your own `templates` instance** built against a template directory that shadows specific files (Jinja2 looks up by name; the first directory wins).
2. **Use `pf_core.web.templates.setup_templates`** with a loader that searches your project templates first, then pf-core's packaged ones.

The templates that most projects will want to shadow: `base.html` (chrome/nav) and `macros.html` (badge colors).

## Auth

pf-core does not bundle authentication. Supply a FastAPI dependency via `auth_dep`:

```python
def require_admin(user=Depends(current_user)):
    if not user or not user.is_admin:
        raise HTTPException(status_code=403)
    return user
```

`auth_dep` is **required**: `make_admin_router()` raises `ConfigurationError` if called with neither `auth_dep` nor `allow_unauthenticated=True`, so an unauthenticated admin can never be mounted by accident. To run it open for local development, opt in explicitly with `allow_unauthenticated=True` — never in production.

## JSON API

Stable contract: `{data, meta}`. List endpoints paginate with `limit`/`offset` and return `meta.total` + `meta.next_offset` (or `None` if end reached).

```bash
curl 'https://app.example.com/admin/llm/api/cost-by-model.json?since=2026-04-01' \
     -H 'Cookie: session=...'
```

Example response:

```json
{
  "data": [
    {"model": "claude-opus-4-7", "runs": 4820, "total_cost": 248.13, "avg_cost": 0.0515,
     "prompt_tokens": 5_800_000, "completion_tokens": 2_100_000}
  ],
  "meta": {"since": "2026-04-01T00:00:00+00:00", "until": "2026-04-15T00:00:00+00:00"}
}
```

### Job actions

`POST /admin/llm/api/job/{job_id}/cancel` — soft-cancels a pending or running job by transitioning it to `canceled` and writing a `canceled` event. In-flight workers are not killed; they discover the cancel on their next step transition.

```bash
curl -X POST 'https://app.example.com/admin/llm/api/job/74/cancel' \
     -H 'Cookie: session=...' \
     -H 'Content-Type: application/json' \
     -d '{"reason": "user clicked cancel"}'
```

Status codes:

| Code | Meaning |
|------|---------|
| 200 | Canceled. Body is `{data: <job_detail>, meta: {job_id}}` matching the GET shape. |
| 404 | Job not found. |
| 409 | Job already terminal (`succeeded` / `failed` / `canceled`). |

The `reason` body field is optional; default is `"canceled via admin"`. Project code that also tracks an in-memory job (e.g. a `JobManager` for live progress) should layer that bookkeeping in its own wrapper around this route — pf-core can't know about project-specific in-memory state.

## What this doesn't do (v1)

- **No other write actions.** No "retry this run", no "re-validate" buttons. Operators use the `pf-jobs` CLI, the eval Python API, or direct SQL for surgical actions. Cancel is the one exception — interrupting a runaway job is a common admin need.
- **No SVG charts.** Tables only for v1. Add charts in a follow-up.
- **No validator heatmaps, router drift, eval pages, cost-over-time time series.** Deferred to follow-ups — the foundation is in place to add them incrementally.
- **No WebSocket live updates.** Pages refresh on load.

## Layering note

`pf_core.web.llm_admin` is pf-core's first web sub-app. It counts as an entry point in the layering rules:
- Reads through domain repos (`pf_core.llm.tracking`, `pf_core.jobs`, `pf_core.llm.cache`, `pf_core.budget`) — via `queries.py` helpers, no direct `transaction()` in pages.
- Contains no business logic — shape queries, pass to templates / JSON.
