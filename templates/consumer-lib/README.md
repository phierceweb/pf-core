# __NAME__

A pf-core library / tool (src-layout — no web or database layers).

## Setup

```bash
bin/setup        # venv (3.11+) + editable install + .env
bin/run hello    # the day-1 vertical slice
```

## Commands

```bash
bin/run <cmd>    # the __NAME__ CLI (src/__PKG__/cli.py)
bin/test         # pytest
bin/lint         # ruff + pf-core's file-size gate
```

## Layout

`src/__PKG__/` is the importable package. Add a domain package under it as the
code grows; introduce `<domain>/services/`, `orchestrators/`, or `utils/`
directories only once each holds two or more files (see
`.ai/rules/project-structure.md`). Built on [pf-core](https://github.com/phierceweb/pf-core).
