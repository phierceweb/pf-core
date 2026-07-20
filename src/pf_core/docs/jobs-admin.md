# Jobs admin dashboard

Mountable jobs list/detail pages plus a polling JSON API and cancel endpoint — the jobs sibling of [llm-admin](llm-admin.md). Not to be confused with the `pf-jobs` CLI ([jobs](jobs.md)), which is the same operations surface for terminals.

---

## Table of Contents

- [Mounting](#mounting)
- [Routes](#routes)
- [Cancel semantics](#cancel-semantics)
- [Customization](#customization)

## Mounting

```python
from pf_core.web.jobs_admin import make_jobs_router
from pf_core.jobs.workers import terminate_job

app.include_router(make_jobs_router(
    auth_dep=require_admin,                       # None = open (dev only)
    kind_labels={"grading_pass": "grade"},
    describe=lambda job: {"label": section_label(job), "href": section_url(job)},
    terminate_hook=terminate_job,                 # only when jobs run as subprocesses
    prefix="/jobs",
))
```

`describe` receives the job row and returns the consumer's scope link (`{"label", "href"}`) or `None`; `kind_labels` maps kinds to human action names. Both default to raw values.

## Routes

| Route | What |
|---|---|
| `GET {prefix}` | Sortable, paginated list (`sort` ∈ id/kind/status/created_at — backed by `JobRepo.find_page`, a fixed allowlist because the sort key arrives from the URL). |
| `GET {prefix}/{id}` | Detail page: status, progress bar, steps, events; polls the JSON bundle every 2s while pending/running, reloads on terminal. |
| `GET {prefix}/api/{id}` | JSON bundle `{job, steps, events}` (404 unknown). |
| `POST {prefix}/api/{id}/cancel` | Body `{"reason": "..."}` optional. See below. |

## Cancel semantics

Cancel is soft: the row transitions to `canceled` (a `canceled` event is recorded); an already-terminal job returns **409** — the UI treats that as "refresh state", not an error. When `terminate_hook` is provided it is invoked *first*, so subprocess-mode consumers (see [jobs-runtime](jobs-runtime.md)) also kill the process; the runtime's canceled-check keeps the two paths from fighting over the terminal state. Thread-mode consumers pass no hook — their in-flight step finishes and the worker exits on its next transition.

## Customization

Templates are self-contained (no consumer base template or CSS assumed) so the router works in any app unstyled-by-design; pass `templates=Jinja2Templates(...)` pointing at your own `jobs_list.html` / `job_detail.html` to reskin. `auth_dep` guards every route via a standard FastAPI dependency — the same pattern as `llm_admin.make_admin_router`.
