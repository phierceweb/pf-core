# __NAME__ — AI assistant context

A pf-core consumer: a library/tool in the **src-layout** (no web/DB layers),
built on the pf-core framework.

## Rules

Project rules live in `.ai/rules/` (copied from pf-core; `bin/setup` symlinks
`.claude/rules` and `.cursor/rules` to them). Read them for layering, code
style, error handling, config-driven design, and structure conventions.

## Layout

- `src/__PKG__/cli.py` — CLI entry (thin; pf-core `create_cli` / `run_cli`).
- `src/__PKG__/config.py` — `Config(AppConfig)` subclass; the `cfg` singleton.
- Add one package per domain under `src/__PKG__/`. Grow a layer dir
  (`<domain>/services/`, `orchestrators/`, `utils/`) only when it has ≥2 files.

## Commands

`bin/setup`, `bin/run <cmd>`, `bin/test`, `bin/lint`.

## pf-core

Declared in `pyproject.toml` as `pf-core[__EXTRAS__]`. Import from `pf_core.*`.
**Module reference: [docs/pf-core/modules.md](docs/pf-core/modules.md)** —
`bin/setup` symlinks the installed pf-core's docs into the project docs dir.
If the link is missing, resolve directly:
`bin/run python -c "import pf_core, pathlib; print(pathlib.Path(pf_core.__file__).parent / 'docs')"`.
Never reach for a third-party library when pf-core already provides it
(logging, config, exceptions, parallel, LLM clients, etc.).
