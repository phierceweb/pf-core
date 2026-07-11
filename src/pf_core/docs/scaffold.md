# New-consumer scaffold

`bin/new-consumer` stamps a new pf-core consumer project from a template — a runnable, conformant skeleton, so a project adopts the framework's conventions from day one instead of rebuilding into them later.

It's a **from-checkout** dev tool: the generator reads `templates/` and `.ai/rules/` from the pf-core source tree (these aren't shipped in the wheel). The generated project then depends on pf-core however it likes — a git tag now, a PyPI version later.

## Usage

```bash
bin/new-consumer <name> --layout {lib|app} [--extras cli,llm] [--dest DIR]
```

- **`lib`** — a CLI / library in the src-layout (`src/<pkg>/`), no web or DB. Default extras: `cli`.
- **`app`** — a full-stack FastAPI + SQLAlchemy app (`app/` layout). Default extras: `full`.

```bash
bin/new-consumer my-tool --layout lib
cd ./my-tool        # created under --dest (default: cwd)
bin/setup
bin/run hello       # the day-1 slice
```

## What it generates

- `pyproject.toml` — the pf-core pin + chosen extras, a shared ruff config, build-system, and (lib) a console-script entry.
- `.pf-guards.toml` — the structural-gate config (correct scan root per layout).
- `bin/{setup,run,lint,test}` (plus `bin/web` for `app`) — **self-contained** wrappers that work whether pf-core is pip-installed or a sibling checkout.
- **Enforcement wired, not just documented**: `.pre-commit-config.yaml` (structural gate + ruff on every commit; `bin/setup` installs the hooks when the project is a git repo) and a `.github/workflows/guards.yml` CI backstop.
- The layout skeleton + a **day-1 vertical slice** that runs immediately: `bin/run hello` (lib) or a `GET /` route (app).
- `.ai/rules/` copied from pf-core, with `.claude/` and `.cursor/` symlinks wired by `bin/setup` — which also links the installed pf-core's module docs at `docs/pf-core`.
- `tests/` with a smoke test, plus `.env.example`, `.gitignore`, and a `CLAUDE.md` skeleton pointing at the docs link.

## Conventions baked in

The generated project follows the `project-structure` rule (in the repo's `.ai/rules/`) for its layout, is linted by `pf_core.guards` via `bin/lint` (reading the stamped `.pf-guards.toml` — `root = "src/<pkg>"` for lib, `root = "app"` for app), and pins pf-core from PyPI at the current compatible line (see the template `pyproject.toml`).
