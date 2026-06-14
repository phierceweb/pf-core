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
- `bin/{setup,run,lint,test}` (plus `bin/web` for `app`) — **self-contained** wrappers that work whether pf-core is pip-installed or a sibling checkout.
- The layout skeleton + a **day-1 vertical slice** that runs immediately: `bin/run hello` (lib) or a `GET /` route (app).
- `.ai/rules/` copied from pf-core, with `.claude/` and `.cursor/` symlinks wired by `bin/setup`.
- `tests/` with a smoke test, plus `.env.example`, `.gitignore`, and a `CLAUDE.md` skeleton.

## Conventions baked in

The generated project follows [`project-structure.md`](../../../.ai/rules/project-structure.md) for its layout, is linted by `pf_core.guards` via `bin/lint` (`--root src/<pkg>` for lib, `--root app` for app — the layout-agnostic file-size + layering gate), and pins pf-core by git tag (swap to `pf-core[...]>=X` once it's on PyPI).
