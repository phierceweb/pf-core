# Config-File Path Resolver

`resolve_config_path` finds a config file via an override chain — **env-named directory → CWD `config/` → package-bundled default** — and returns an absolute path. It is the file-path half of the project's `config-driven` convention; the env-*value* half is [`env.md`](env.md).

Because the consumer's bundled file is a packaging invariant, the resolver never raises `FileNotFoundError`: the bundled default is the guaranteed floor.

## Usage

```python
from pathlib import Path
from pf_core.utils.config_path import resolve_config_path

_PKG = Path(__file__).parent   # the consumer's package dir (ships its config/)

spec_path = resolve_config_path(
    "drafter.yaml",
    env_dir_var="MYAPP_PROMPTS_DIR",
    bundled_dir=_PKG / "config" / "prompts",
    cwd_subdir="config/prompts",
)
```

```python
resolve_config_path(
    filename: str, *, env_dir_var: str | None, bundled_dir: Path, cwd_subdir: str = "config"
) -> Path
```

Resolution order, first existing wins:

1. `$env_dir_var/filename` — an operator override directory. Skipped when `env_dir_var` is `None`, the var is unset, or the file is absent there.
2. `./cwd_subdir/filename` — a project-local config dir (default subdir `config`; pass e.g. `config/prompts` for nested layouts).
3. `bundled_dir/filename` — the package-bundled default (the floor).

The returned path is always absolute — resolved eagerly, so a later CWD change can't invalidate it.

## Integration styles

**1. Load directly** — pass the resolved path straight to a loader:

```python
from pf_core.llm.prompts import load_prompt_spec

spec = load_prompt_spec(
    resolve_config_path("drafter.yaml", env_dir_var="MYAPP_PROMPTS_DIR",
                        bundled_dir=_PKG / "config" / "prompts", cwd_subdir="config/prompts")
)
```

**2. Export into a downstream loader's env var** — when that loader resolves its own path (e.g. the model router reads `MODEL_ROUTER_CONFIG`):

```python
import os

os.environ.setdefault(
    "MODEL_ROUTER_CONFIG",
    str(resolve_config_path("model_router.yaml", env_dir_var="MYAPP_ROUTER_DIR",
                           bundled_dir=_PKG / "config")),
)
```

`setdefault` respects an operator-set value, so a full-path override still wins.

## Why a directory env var (not a full file path)

The override env names a *directory*, matching the common "ship a `config/` the operator can shadow" layout. A loader that wants a full-file-path override (like the router's `MODEL_ROUTER_CONFIG`) is served by integration style 2 — export the resolved path into it — so the generic helper stays one shape. (If a consumer ever needs a full-path env override, add an optional `env_file_var` param then — not before.)

## See also

- The project's `config-driven` rule — the framework rule this helper serves.
- [`env.md`](env.md) — the env-*value* half of the configuration convention (`resolve_int` / `resolve_positive_int` / …).
- [`model-router.md`](model-router.md) — the router's own env→CWD path chain (`MODEL_ROUTER_CONFIG`).
