# Configuration

`pf_core.config.AppConfig` is the base configuration class. Subclass it in each project to declare settings with defaults. Values resolve from env vars, YAML files, and defaults — in that priority order.

## Quick start

```python
from pf_core.config import AppConfig

class MyConfig(AppConfig):
    ENABLE_CACHE: bool = True
    MAX_WORKERS: int = 4
    CACHE_TTL_SECONDS: int = 300

cfg = MyConfig(env_file=".env", yaml_file="project.yaml")
```

**Do not** declare per-agent primary model or sampling fields here (e.g. `SUMMARIZER_MODEL`, `CLASSIFIER_TEMPERATURE`). Those live in `config/model_router.yaml` — see [model-router.md](model-router.md). `AppConfig` is for cross-cutting app settings and tunables that don't vary per agent.

## Resolution order

Each setting resolves in this order (highest priority wins):

1. **Explicit overrides** passed to the constructor
2. **Environment variables** (loaded from `.env` via python-dotenv)
3. **YAML config file** (project-level domain config)
4. **Class attribute defaults**

## Built-in settings

These are declared on `AppConfig` and available to every project:

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `DATABASE_URL` | `str` | `""` | SQLAlchemy database URL |
| `REDIS_URL` | `str` | `""` | Redis connection URL |
| `OPENROUTER_API_KEY` | `str` | `""` | OpenRouter API key |
| `OPENROUTER_BASE_URL` | `str` | `"https://openrouter.ai/api/v1"` | OpenRouter API base URL |
| `OPENROUTER_PROVIDER_IGNORE` | `list[str]` | `[]` | Providers to exclude from routing |
| `WEB_HOST` | `str` | `"127.0.0.1"` | Web server bind address |
| `WEB_PORT` | `int` | `8000` | Web server bind port |
| `CORS_ORIGINS` | `list[str]` | `[]` | Allowed CORS origins |
| `LOG_LEVEL` | `str` | `"INFO"` | Console log level |
| `LOG_FILE` | `str` | `""` | JSON-lines log file path (empty = disabled) |
| `APP_NAME` | `str` | `"App"` | Application name (sent in API headers) |
| `APP_URL` | `str` | `""` | Application URL (sent in API headers) |
| `REQUEST_TIMEOUT` | `int` | `120` | Per-request socket timeout (seconds) |
| `THREAD_MAX_WORKERS` | `int` | `4` | Thread pool size for parallel operations |
| `API_RATE_LIMIT_PER_MINUTE` | `int` | `60` | Default API rate limit |
| `MAX_PER_PAGE` | `int` | `200` | Upper bound for `per_page` in paginated endpoints |
| `ID_LENGTH` | `int` | `12` | Default nanoid length for `generate_id()` (clamped 8–36) |

## Framework env vars (not AppConfig fields)

These are read directly from the environment by framework modules — not declared on `AppConfig`:

| Variable | Default | Module | Description |
|----------|---------|--------|-------------|
| `CACHE_CONFIG` | `config/cache.yaml` | `pf_core.llm.cache` | Path to the LLM response cache config YAML |
| `CACHE_CONFIG_RELOAD_SECONDS` | `60` | `pf_core.llm.cache` | How often to reload the cache config |
| `BUDGET_CONFIG` | `config/budgets.yaml` | `pf_core.budget` | Path to the budget config YAML |
| `BUDGET_CONFIG_RELOAD_SECONDS` | `300` | `pf_core.budget` | How often to reload the budget config |
| `BUDGET_ENFORCEMENT_DISABLED` | *unset* | `pf_core.budget` | When `true`, `check_budget()` short-circuits to always-pass (incident kill-switch) |

## Type coercion

Env vars are strings. `AppConfig` coerces them based on the default value's type:

| Default type | Coercion |
|-------------|----------|
| `bool` | `"1"`, `"true"`, `"yes"`, `"on"` → `True`; everything else → `False` |
| `int` | `int(val)`, falls back to default on error |
| `float` | `float(val)`, falls back to default on error |
| `list[str]` | Comma-separated string → list (e.g. `"a, b, c"` → `["a", "b", "c"]`) |
| `str` | Stripped, no conversion |

## YAML access

The raw YAML dict is available as `cfg.yaml`:

```python
cfg = MyConfig(yaml_file="project.yaml")

# Access nested YAML values
section_order = cfg.yaml.get("section_order", [])
start_date = cfg.yaml.get("start_date")
```

YAML values are not auto-mapped to attributes — use `cfg.yaml` for domain-specific config that doesn't map to env vars.

## Dict-style access

```python
cfg.get("FEATURE_FLAG", False)  # like getattr with a default
```

## Example: a project config subclass

```python
class MyAppConfig(AppConfig):
    # In-call fallback knob — legit per-agent config. Primary per-agent
    # model + sampling live in config/model_router.yaml, not here.
    SUMMARIZE_MODEL_FALLBACK: str = "openai/gpt-4o-mini"
    CACHE_TTL_SECONDS: int = 300
    TASK_CACHE_TTL_SECONDS: int = 86400
    RESULT_CACHE: bool = True
    RESULT_CACHE_TTL_SECONDS: int = 90 * 86400

cfg = MyAppConfig(
    env_file=Path(__file__).parent.parent / ".env",
    yaml_file=Path(__file__).parent.parent / "project.yaml",
)
```
