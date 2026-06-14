# Env-Var Resolver Helpers

`resolve_int`, `resolve_str`, and `resolve_bool` codify the resolution pattern that the project's `config-driven` rule prescribes: **explicit argument → environment variable → default.** Malformed env values fall back to the default and emit a structured warning so operators don't silently lose their intended override.

## Usage

```python
from pf_core.utils.env import resolve_bool, resolve_int, resolve_str

def paginate_params(page, per_page, max_per_page=None):
    max_per_page = resolve_int(max_per_page, "MAX_PER_PAGE", default=200)
    ...

def get_model_name(model=None):
    return resolve_str(model, "DEFAULT_MODEL", default="haiku")
```

The pattern lets a framework function be tunable per-call (caller passes explicit value), per-deployment (env var sets a default), and falls back gracefully when neither is set.

## resolve_int

```python
resolve_int(arg: int | None, env_var: str, *, default: int) -> int
```

Resolution order, first non-`None` wins:

1. `arg` — explicit value. `0` counts as a real value (not "unset"); only `None` falls through.
2. `$env_var` — string env var, parsed as int. Whitespace stripped before parse. Malformed values (non-numeric, empty string) emit a warning event `env_var_malformed` and fall through to the default rather than raising.
3. `default` — required.

```python
resolve_int(7, "MAX_PER_PAGE", default=200)        # 7  (explicit wins)
resolve_int(None, "MAX_PER_PAGE", default=200)     # 200 (env unset, default)
# $MAX_PER_PAGE=50:
resolve_int(None, "MAX_PER_PAGE", default=200)     # 50  (env wins)
# $MAX_PER_PAGE=garbage:
resolve_int(None, "MAX_PER_PAGE", default=200)     # 200 (warn + default)
```

## resolve_str

```python
resolve_str(arg: str | None, env_var: str, *, default: str | None = None) -> str | None
```

Resolution order, first non-`None` wins:

1. `arg` — explicit value. `""` (empty string) counts as a real value; only `None` falls through.
2. `$env_var` — string env var. `""` (empty string set) counts as set per OS semantics; only an unset variable falls through.
3. `default` — defaults to `None` so callers can distinguish "not configured anywhere" from "configured to empty string".

```python
resolve_str("haiku", "DEFAULT_MODEL", default="opus")  # "haiku" (explicit)
resolve_str(None, "DEFAULT_MODEL", default="opus")     # "opus"  (env unset)
# $DEFAULT_MODEL=sonnet:
resolve_str(None, "DEFAULT_MODEL", default="opus")     # "sonnet" (env)
resolve_str(None, "DEFAULT_MODEL")                     # None    (no default)
```

## resolve_bool

```python
resolve_bool(arg: bool | None, env_var: str, *, default: bool) -> bool
```

Resolution order, first non-`None` wins:

1. `arg` — explicit value. `False` counts as a real value (not "unset"); only `None` falls through.
2. `$env_var` — string env var, case-insensitive, whitespace stripped. Truthy values: `1`, `true`, `yes`, `on`. Falsy: `0`, `false`, `no`, `off`. Malformed values emit `env_var_malformed` and fall through to the default rather than raising.
3. `default` — required.

```python
from pf_core.utils.env import resolve_bool

DEFAULT_ENABLE_CACHE = True

def cache_enabled(explicit: bool | None = None) -> bool:
    return resolve_bool(explicit, "MYPROJ_CACHE_ENABLED", default=DEFAULT_ENABLE_CACHE)
```

## Recipe: wrap a resolver in a tiny module helper

For values used in multiple places within a single module, wrap the resolver in a `_foo()` helper so the env-var name + default live in one place:

```python
from pf_core.utils.env import resolve_int

_FOO_TIMEOUT_S_DEFAULT = 600
_FOO_TIMEOUT_ENV_VAR = "MYPROJ_FOO_TIMEOUT_S"

def _foo_timeout_s() -> int:
    n: int = resolve_int(None, _FOO_TIMEOUT_ENV_VAR, default=_FOO_TIMEOUT_S_DEFAULT)
    return n
```

Then call `_foo_timeout_s()` at each call site. Reading at call time means a long-lived process picks up `.env` changes between calls. The `int:` annotation pins the return type for mypy strict (pf-core isn't `py.typed` yet, so the resolver's `int` return appears as `Any` to consumers).

See `.ai/rules/config-driven.md` for the full layering and carve-out rules.

## Why warn (don't raise) on malformed env

A typo in `MAX_PER_PAGE=20O` (letter O, not digit zero) shouldn't crash production. The framework warns loudly so the operator sees the issue in logs and falls back to the default — preserving the pre-misconfig behavior. If a malformed env should be fatal, the caller can re-validate.

## See also

- The project's `config-driven` rule — the framework rule that drives this helper.
- [`config.md`](config.md) — where to register a new tunable in `AppConfig`.
