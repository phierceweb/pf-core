# Web (FastAPI)

FastAPI application factory with built-in error handling, request logging, CORS, and Jinja2 template support.

## App factory

```python
from pf_core.web.app_factory import create_app

app = create_app(
    title="My App",
    cors_origins=["http://localhost:3000"],
    static_dir="app/static",
    template_dir="app/templates",
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | `str` | `"App"` | Application title |
| `version` | `str` | `"0.1.0"` | Application version |
| `cors_origins` | `list[str]` | `None` | Allowed CORS origins (empty = no CORS) |
| `static_dir` | `Path \| str` | `None` | Static files directory (mounted at `/static`) |
| `template_dir` | `Path \| str` | `None` | Jinja2 templates directory |
| `log_requests` | `bool` | `True` | Enable request logging middleware |
| `rate_limit` | `bool` | `True` | Enable rate limiting. Reads `API_RATE_LIMIT_PER_MINUTE` from env (default 60). Requires `pf-core[ratelimit]`. |

Additional kwargs are passed through to `FastAPI()`.

## What's included

### Request logging

Every request is logged with method, path, status code, and duration:

| Status range | Log level |
|-------------|-----------|
| 2xx | DEBUG |
| 4xx | WARNING |
| 5xx | ERROR |

### Error handling

Exceptions are mapped to HTTP responses automatically. Each `FlowException` subclass has a dedicated handler — no string matching:

| Exception type | HTTP status | Behavior |
|---------------|-------------|----------|
| `NotFoundError` | 404 | Clean message shown to user |
| `InvalidInputError` | 422 | Clean message shown to user |
| `PreconditionError` | 409 | Clean message shown to user |
| `ActionNotAllowedError` | 403 | Clean message shown to user |
| `ConfigurationError` | 500 | Logged with traceback; generic message shown |
| `FlowException` (catch-all) | 400 | Clean message shown to user |
| `AppError` | 500 | Logged with traceback; generic message shown to user |
| `HTTPException` | Status from exception | Standard FastAPI behavior |
| Unhandled `Exception` | 500 | Logged with traceback; generic message shown |

### Error pages

Built-in self-contained HTML error pages for: 400, 403, 404, 405, 422, 429, 500, 502, 503.

**Custom template override**: If your project has `shared/error.html` in the template directory, the framework uses it instead of the built-in page. The template receives: `title`, `code`, `heading`, `message`.

**API-aware**: Requests with `Accept: application/json` get JSON responses:

```json
{"detail": "The page you're looking for doesn't exist or has been moved."}
```

### CORS

Configured via `cors_origins`. When provided, adds `CORSMiddleware` with `allow_credentials=True` and all methods/headers allowed.

## Templates

Set up Jinja2 templates with custom globals and filters:

```python
from pf_core.web.templates import setup_templates

templates = setup_templates(
    app,
    template_dir="app/templates",
    extra_globals={"app_name": "My App", "version": "1.0"},
    extra_filters={"format_date": my_date_formatter},
)
```

The templates instance is stored on `app.state.templates` so error handlers can access it.

Use in route handlers:

```python
@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(request, "pages/home.html", {"sections": sections})
```

## JSON response helpers

Handles types the stdlib JSON encoder can't serialize: `date`, `datetime`, `Decimal`, `bytes`, SQLAlchemy `Row`.

```python
from pf_core.web.json import safe_json_response

@router.get("/api/events")
async def list_events():
    rows = repo.list_events()  # may contain dates, Decimals, Row objects
    return safe_json_response(rows)
```

Use `json_default` standalone if you need the encoder without a response:

```python
import json
from pf_core.web.json import json_default

json.dumps(data, default=json_default)
```

## Rate limiting

Rate limiting via `slowapi`. Requires `pf-core[ratelimit]`.

Enabled by default in `create_app`. Reads `API_RATE_LIMIT_PER_MINUTE` from env (default 60). Set the env var in `.env` to tune; no code changes needed.

To disable: `create_app(rate_limit=False)`.

**Per-route overrides:**

```python
from pf_core.web.rate_limit import setup_rate_limit

app = create_app(title="My App")
limiter = setup_rate_limit(app)

@app.get("/expensive")
@limiter.limit("5/minute")
async def expensive(request: Request):
    ...
```

Storage backend: Redis if `REDIS_URL` is set, otherwise in-memory. If `slowapi` is not installed, rate limiting is silently skipped.

## LLM admin sub-app

`pf_core.web.llm_admin` ships a mountable admin surface for the tracking, jobs, cache, and budget tables. One `app.include_router(make_admin_router(...))` call and the consumer has dashboards, runs list/detail, cost pages, jobs views, cache stats, and budget state — plus JSON siblings for every page.

See [llm-admin.md](llm-admin.md).

## Health check

Standard `GET /health` endpoint for deployment probes (Docker, ECS, k8s).

```python
from pf_core.web.health import health_router

app = create_app(title="My App")
app.include_router(health_router())
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `check_db` | `bool` | `True` | Include database connectivity check |
| `check_redis` | `bool` | `False` | Include Redis connectivity check |
| `prefix` | `str` | `""` | URL prefix (e.g. `"/api"` → `GET /api/health`) |

### Response

```json
{"status": "ok", "checks": {"db": "ok"}}
```

Returns 200 if all checks pass, 503 if any check fails (with `"status": "degraded"`).

### require_db dependency

For routes that need the database, use `require_db` as a FastAPI dependency. Returns 503 if the database is unreachable:

```python
from fastapi import Depends
from pf_core.web.health import require_db

@app.get("/data", dependencies=[Depends(require_db)])
async def get_data():
    ...
```

## Safe markdown rendering

Renders a safe markdown subset (bold, italic, links, lists, headings, code) using an escape-first approach. No sanitizer library needed.

```python
from pf_core.web.markdown import safe_markdown, setup_markdown_filter

# Direct usage
html = safe_markdown("**bold** and [link](https://example.com)")

# Jinja2 filter
setup_markdown_filter(templates)
# In template: {{ section.summary | markdown }}
```

For project-specific inline patterns, pass `extra_transforms` — see [markdown.md](markdown.md) for details.

## Web helpers

Shared utilities for route handlers.

### `resolve_or_404`

Returns the value if not None, raises `NotFoundError` (mapped to 404) otherwise:

```python
from pf_core.web.helpers import resolve_or_404

@router.get("/entries/{entry_id}")
async def get_entry(entry_id: str):
    entry = resolve_or_404(entry_repo.get_by_id(entry_id), "Entry")
    return entry
```

Works with any return type — dicts, ORM objects, strings, even falsy values like `0` or `""` (only `None` triggers the 404).

## Pagination

See [pagination.md](pagination.md) for `paginate_params` and `paginate_result` helpers — standardized parameter validation, offset/limit calculation, and result metadata for paginated list endpoints.

## Example: full project setup

```python
# app/__init__.py
from pathlib import Path
from pf_core.web.app_factory import create_app
from pf_core.web.health import health_router
from pf_core.web.templates import setup_templates
from pf_core.db import get_engine, db_url

from app.config import cfg

# Database
get_engine(db_url())

# App
app = create_app(
    title=cfg.APP_NAME,
    cors_origins=cfg.CORS_ORIGINS,
    static_dir=Path(__file__).parent / "static",
)

# Templates
templates = setup_templates(
    app,
    template_dir=Path(__file__).parent / "templates",
    extra_globals={"app_name": cfg.APP_NAME},
)

# Health + Routes
app.include_router(health_router())

from app.api import pages, entries, admin  # noqa: E402
app.include_router(pages.router)
app.include_router(entries.router, prefix="/api")
app.include_router(admin.router, prefix="/api/admin")
```
