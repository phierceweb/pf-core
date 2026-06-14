# Linting Tools

Two layers of lint:

1. **`ruff`** — code-quality lints (unused imports, ambiguous names, common bugs) for both pf-core itself and any consumer that adopts the same config. Configured in `pyproject.toml` under `[tool.ruff]`.
2. **`bin/lint-layers` and `bin/lint-size`** — consumer-targeted architecture lints (layered import discipline, file-size budgets). These walk a consumer project's `app/` tree and don't apply to pf-core itself.

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

## Layer Import Linter

Checks that imports follow the layered architecture: entry points → orchestrators → services → repo/clients.

```bash
cd ~/projects/my_project
python ../pf-core/bin/lint-layers
```

### Rules enforced

| Layer | Can import from | Cannot import from |
|-------|----------------|-------------------|
| api, cli | services, orchestrators | repo, clients |
| orchestrators | services | repo, clients, api, cli |
| services | repo, clients | orchestrators, api, cli |
| repo | *(no app layers)* | services, orchestrators, api, cli |
| clients | *(no app layers)* | services, orchestrators, api, cli |

### Output

```
LAYER VIOLATION: app/api/_util.py
  line 12: import app.repo.catalog (api → repo, should go through services)

Found 1 violation(s) in 1 file(s).
```

### Skipping files

Add `# lint-layers: skip` in the first 5 lines of a file to exempt it.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | No violations |
| 1 | Violations found |
| 2 | No `app/` directory |

---

## File Size Linter

Checks Python file sizes against per-layer limits from `project-structure.md`.

```bash
cd ~/projects/my_project
python ../pf-core/bin/lint-size
```

### Default limits

| Path pattern | Limit |
|-------------|-------|
| `app/cli/` | 100 lines |
| `app/services/` | 300 lines |
| `app/repo/` | 300 lines |
| `app/api/` | 300 lines |
| `app/orchestrators/` | 400 lines |
| `_util*.py` (any directory) | 150 lines |
| Everything else | 500 lines |

### Per-project overrides (`.lint-size.yaml`)

Create a `.lint-size.yaml` in the project root to override defaults and/or exempt specific files:

```yaml
limits:
  app/cli: 400             # raise the cli budget for this project
  app/services: 500
  app/api: 400
  app/api/admin: 600       # longer prefix wins over app/api
exempt:
  - app/db/sections.py     # pending split — temporary exemption
```

**How matching works:**

- `limits:` keys are path prefixes under the project root. A file matches when its path equals the prefix or starts with `prefix/`.
- When multiple prefixes match, the **longest wins** (so `app/api/admin` can override `app/api`).
- A limit set via `limits:` beats the built-in default for that file, including the `_util*.py` special case.
- `exempt:` is still available for genuinely-over-budget files that need a temporary pass while you split them.

The recommended pattern: keep `exempt:` short and justified. Use `limits:` to encode a project-wide budget that differs from pf-core's defaults, so new over-budget files show up as real regressions instead of being drowned in exemptions.

### Output

Files are sorted by most-over-limit first:

```
OVER LIMIT: app/repo/records.py
  610 lines (limit: 300, over by 310)

Found 1 file(s) over their size limit.
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | All files within limits |
| 1 | Files over limit |
| 2 | No `app/` directory |
