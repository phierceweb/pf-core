# __NAME__

A pf-core full-stack app (FastAPI + SQLAlchemy, app-layout).

## Setup

```bash
bin/setup        # venv (3.11+) + editable install + .env
bin/web          # start the server → http://127.0.0.1:8000/
```

## Commands

```bash
bin/web          # run the server (uvicorn)
bin/test         # pytest
bin/lint         # ruff + pf-core's file-size + layering gate
```

## Layout

`app/` holds the application: `api/` (routes only), and — as the app grows —
`services/` (business logic), `repo/` (data access), `orchestrators/`
(multi-step workflows). No layer imports from a layer above it; see
`.ai/rules/layering.md` and `.ai/rules/project-structure.md`. Built on
[pf-core](https://github.com/phierceweb/pf-core).
