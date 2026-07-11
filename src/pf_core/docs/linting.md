# Linting Tools

Two layers of lint:

1. **`ruff`** — code-quality lints (unused imports, ambiguous names, common bugs) for both pf-core itself and any consumer that adopts the same config. Configured in `pyproject.toml` under `[tool.ruff]`.
2. **`pf_core.guards`** — the structural gate: file-size budgets (flat for library code, per-layer for consumer `app/` trees) and layered-import discipline, configured in `.pf-guards.toml` and wired into pre-commit + CI. See [guards.md](guards.md).

```bash
python -m pf_core.guards    # structural gate — sizes + layering; reads .pf-guards.toml
```

*(The former standalone `bin/lint-size` / `bin/lint-layers` scripts and their `.lint-size.yaml` config are retired — the gate replaced them: `[tool.pf_guards.limits]` covers per-path budgets, and the baseline ratchet covers exemptions.)*

## Ruff (code quality)

pf-core ships a `[tool.ruff]` config in its own `pyproject.toml`. Run from the repo root:

```bash
ruff check src tests        # lint
ruff check --fix src tests  # auto-fix what's safe
```

Active rule sets: `E` (pycodestyle errors), `W` (pycodestyle warnings), `F` (pyflakes — unused imports, undefined names, etc.), `B` (flake8-bugbear — common bugs and antipatterns).

Ignored: `E501` (line length — defer until a separate formatting pass), `B008` (function call in default argument — common in FastAPI/Typer), `B904` (raise-from — pre-dates this lint).

Test files exempt from `B011` (bare assert in tests is fine).

Consumer projects can copy the same `[tool.ruff]` block into their own `pyproject.toml` to inherit the rule set.
