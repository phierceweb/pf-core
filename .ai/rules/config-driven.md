# Config-Driven Design

Framework features read configuration from environment variables — callers should not pass config values as parameters. Web apps centralize this via `AppConfig`; CLIs and libraries use the wrapper-function recipe in "How to wire a new env var" below.

## Principle

Tunable values belong in `.env`, not in function calls. This lets operators change behavior without code changes and ensures consistency across all call sites.

## The layering

User-tunable operational values are configuration; internal constants are code. The full layering for consumers of pf-core:

| Lives in | What |
|---|---|
| `.env.example` (copied to `.env`) | **Operational tunables**: timeouts, retry counts, thresholds whose right value depends on the run, concurrency knobs, host/port, log level, backend selection. Web apps also use `AppConfig` (see existing sections). |
| `config/model_router.yaml` (LLM consumers) | Per-task model + sampling kwargs (`model`, `max_tokens`, `temperature`, `max_input_tokens`). |
| `src/<project>/prompts/*.yaml` (LLM consumers) | Versioned LLM-facing prompts. |
| Code | Internal constants — regex patterns, schema versions, math, dataclass field defaults, cache key formats, loop guards. |

If you find yourself adding a `_FOO_TIMEOUT_S = 600`, `_BAR_THRESHOLD = 1000`, or `_RETRY_LIMIT = 3` to a `.py` file, **stop**: it belongs in `.env.example`. If you're tempted to bump the constant for a specific run, that's the litmus — it's operational.

## Pattern

```python
# WRONG — caller passes config string
app = create_app(title="My App", rate_limit="60/minute")

# RIGHT — framework reads from env internally
app = create_app(title="My App")  # reads API_RATE_LIMIT_PER_MINUTE from env
```

```python
# WRONG — caller computes and passes limit
def paginate_params(page, per_page, max_per_page=200):
    ...

# RIGHT — reads MAX_PER_PAGE from env, with per-call override
from pf_core.utils.env import resolve_int

def paginate_params(page, per_page, max_per_page=None):
    max_per_page = resolve_int(max_per_page, "MAX_PER_PAGE", default=200)
```

Use `pf_core.utils.env.resolve_int` / `resolve_str` / `resolve_bool` for this pattern — they handle malformed env values (warn + fall back to default rather than crash) and strip whitespace. See [`docs/env.md`](../../src/pf_core/docs/env.md).

## When to use

- The value is a tunable that operators might adjust per environment (dev vs prod)
- The value has a sensible default that works for most cases
- Multiple call sites would pass the same value

## When NOT to use

- The value is inherently per-call (e.g. `allowed_sorts` differs per endpoint)
- The value is a structural choice, not a tunable (e.g. `template_dir` path)

## What is NOT operational (stays in code)

The rule is for **operational knobs the operator might tune**, not every numeric literal. Keep these in code:

- Regex group indices (`match.group(1)`)
- Field defaults in `@dataclass`es (those are the public API)
- Cache key format strings, schema versions
- Page-pixel ratios, math constants, decoder field widths
- Internal loop guards (`if len(x) < 2`)
- Anything that's part of the type system or wire format

The opposite mistake — "make every numeric literal configurable" — produces a codebase where every change to internal logic requires an env-var migration, and downstream consumers can't read the source to understand behaviour without also reading their env.

## How to wire a new env var (non-AppConfig consumers)

For CLIs and libraries that don't have an `AppConfig` central object, the pattern is module-level constants + a tiny wrapper that calls one of the env resolvers:

```python
from pf_core.utils.env import resolve_int

_FOO_TIMEOUT_S_DEFAULT = 600
_FOO_TIMEOUT_ENV_VAR = "MYPROJ_FOO_TIMEOUT_S"

def _foo_timeout_s() -> int:
    """Read MYPROJ_FOO_TIMEOUT_S; fall back to default."""
    n: int = resolve_int(None, _FOO_TIMEOUT_ENV_VAR, default=_FOO_TIMEOUT_S_DEFAULT)
    return n
```

When the value is also a function kwarg, take `int | None` and pass the caller's value through as the first arg so the precedence chain stays **kwarg > env > default** (wrap `pf_core.utils.env.resolve_int` exactly as above, and add a positivity guard when zero/negative values would be nonsense for the knob).

Keep the in-code constant as the default fallback (suffix `_DEFAULT` to advertise that it's just the fallback). For CLIs, the CLI flag (if any) should override env; the Python kwarg (if exposed) overrides the CLI flag.

Read at call time (the helper above does, because `resolve_int` reads `os.environ` on each call), not at import, so a long-lived process picks up `.env` changes between calls.

**Project prefix.** Each project owns its own env-var prefix (`MYAPP_*`, `OTHERTOOL_*`, etc.) — except the API-key vars (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`) and `DATABASE_URL`, which follow upstream conventions.

## Checklist for new AppConfig settings (web apps)

1. Add the setting to `AppConfig` in `src/pf_core/config.py` with a default
2. Read it via `os.environ.get()` in the framework function (not via AppConfig instance — framework code shouldn't require instantiation)
3. Add to `docs/config.md` built-in settings table
4. Add to `.env.example` in each consumer project
5. Document in the relevant module's doc file

For non-AppConfig consumers (CLIs, libraries), follow the wrapper-function recipe above and the project's own docs-sync rule (typically: add to `.env.example`, sync the env-var table in `CLAUDE.md` / `docs/usage.md` or equivalent, add tests).

## Why this matters

A hardcoded timeout constant (`_FOO_TIMEOUT_S = 600`) is fine until the one run that needs more — then it fails at exactly the baked-in limit, and the only fix is a source edit + redeploy for what should have been a one-line `.env` change. The urge to bump the constant for a single run *is itself* the signal it should have been operational all along.
