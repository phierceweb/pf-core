# __NAME__ — AI assistant context

A pf-core consumer: a full-stack **app-layout** project (FastAPI + SQLAlchemy),
built on the pf-core framework.

## Rules

Project rules live in `.ai/rules/` (copied from pf-core; `bin/setup` symlinks
`.claude/rules` and `.cursor/rules` to them). Read them — especially
`layering.md` (no layer imports from a layer above it) and
`project-structure.md`.

## Layout

- `app/__init__.py` — `create_app()` factory + router registration.
- `app/api/` — routes only; each handler calls one service/orchestrator.
- `app/config.py` — `Config(AppConfig)` subclass; the `cfg` singleton.
- Add `app/services/` (business logic), `app/repo/` (data access via
  `pf_core.db`), `app/orchestrators/` (workflows) as the app grows.

## Commands

`bin/setup`, `bin/web`, `bin/test`, `bin/lint`.

## pf-core

Declared in `pyproject.toml` as `pf-core[__EXTRAS__]`. Import from `pf_core.*`;
see pf-core's `docs/` (start with `docs/modules.md` and `docs/web.md`). Use the
framework's logging, config, exceptions, db, and web layers rather than
re-implementing them.
