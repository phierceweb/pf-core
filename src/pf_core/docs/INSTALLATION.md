# Installation

How to install `pf-core` for local development, deploy from PyPI, and pick the right combination of extras for your project's shape.

`pf-core` is a dependency-light Python foundation. The default install is the **architectural foundation only** — structured logging, an exception hierarchy, config + env resolvers, utils, and the `Service` base class, on just five small dependencies (structlog, python-dotenv, pyyaml, nanoid, rich). Everything else — LLM clients + anti-slop guards, HTTP utils, CLI scaffolding, the database layer, the FastAPI web layer, the job tracker, eval harness, and admin dashboard — ships as **opt-in extras** that compose orthogonally. A project can adopt pf-core's discipline without installing httpx, pydantic, or the LLM stack at all; an LLM script can `pip install pf-core[llm]` without dragging in a database or web server.

## Quick reference

| Scenario | Command |
|----------|---------|
| Foundation only — logging/exceptions/config/utils, no LLM/HTTP | `pip install pf-core` |
| Validate LLM output only — anti-slop guards, no clients | `pip install pf-core[validate]` |
| Lightweight LLM tool — clients + anti-slop guards | `pip install pf-core[llm]` |
| Crawl/fetch web pages — title, body, publish date, liveness | `pip install pf-core[crawl]` |
| Active pf-core development on this machine | `pip install -e ~/projects/pf-core` |
| New consumer project, full app framework | Pin `pf-core[full,<dialect>]~=0.12.0` in `pyproject.toml`, then `pip install -e .` |
| Fresh machine, consumer only | `git clone <project>` → `pip install -e .` (pulls pf-core from PyPI automatically) |
| Fresh machine, also editing pf-core | Clone pf-core → `pip install -e ~/projects/pf-core` (overrides the PyPI pin) |

**Starting a new consumer from scratch?** From a pf-core checkout, `bin/new-consumer <name> --layout {lib|app}` stamps a runnable, conformant project with the pf-core dep, `bin/` wrappers, and a day-1 slice — see [scaffold.md](scaffold.md).

**Wiring an existing consumer?** From the project root, `pf-setup` (installed with pf-core) links the installed package's bundled docs at `docs/pf-core/`. Idempotent; it never replaces a real file or directory (it reports and exits 1 instead). `pf-doctor` reports the link read-only (the `wiring.docs_link` row).

## Local development (recommended while actively working on pf-core)

Editable install means changes to pf-core are picked up immediately with no reinstall:

```bash
cd ~/projects/my-project
pip install -e ~/projects/pf-core
```

Edit any file in `~/projects/pf-core/`, and every project using pf-core sees the change on the next import.

## From PyPI (deploy or fresh machine)

Pin a compatible release in your project's `pyproject.toml`:

```toml
dependencies = [
    "pf-core[full,postgres]~=0.12.0",
]
```

Then `pip install -e .` pulls pf-core from PyPI automatically. To track unreleased work on `main`, pin `"pf-core[full,postgres] @ git+https://github.com/phierceweb/pf-core.git@main"` instead.

## Choosing extras

Pick the lowest tier that covers what you actually use. Extras compose — you can add more later without restructuring.

### Tier 0 — base install (no extras)

```bash
pip install pf-core
```

Foundation install — five small deps (structlog, python-dotenv, pyyaml, nanoid, rich), no httpx/pydantic/LLM stack. Includes:

- `pf_core.config`, `pf_core.exceptions`, `pf_core.log`, `pf_core.parallel`, `pf_core.output`
- `pf_core.services.Service` base class
- `pf_core.utils.*` pure-Python helpers (dates, ids, json utils, similarity, vocab, relative dates, env resolvers)
- `pf_core.parsers` (HTML primitives)
- `pf_core.budget.check_budget`, `pf_core.budget.project_cost` (in-memory mode — falls back to 0.0 projection without `[db]` cost rates)
- `pf_core.utils.json_recovery` — extract/recover JSON from messy text (markdown fences, trailing prose, mid-stream truncation); generic and stdlib-only

Use this for: any well-structured Python project that wants pf-core's logging, exception hierarchy, config/env discipline, and `Service` base — CLIs, scripts, libraries — without LLM or web dependencies.

Not included at this tier — `pf_core.llm.*` is the **LLM surface**, not foundation: `pf_core.llm.parse` / `pf_core.llm.validate` (the anti-slop output guards) need `[validate]` (raise a friendly `ImportError`); `pf_core.clients.*` need `[llm]`; `pf_core.llm.tracked` needs `[tracking]`. The remaining `pf_core.llm` members (`prompts`, `router`, `url_check`, `safe_apply`) are pyyaml/stdlib-only so they happen to import without an extra, but they belong to the LLM tier — install `[validate]` (or any `[llm]`+ tier) when you use them. `pf_core.utils.urls` / `pf_core.utils.url_liveness` need `[http]`; `pf_core.cli.create_cli` needs `[cli]`.

### Tier 1 — capability extras

Each unlocks one tier of pf-core's framework surface. They compose orthogonally — `[validate]` (anti-slop guards) without httpx/clients, `[db]` without LLM, `[web]` without `[db]`, `[llm]` standalone. Mix and match as needed.

| Extra | Adds | Unlocks |
|---|---|---|
| `[validate]` | `json-repair`, `pydantic` | The anti-slop **output guards** with no client/HTTP stack: `pf_core.llm.parse` (+ json-repair recovery) and `pf_core.llm.validate` (pydantic schema/semantic validation). For validating LLM output, or bringing your own transport. |
| `[llm]` | `[validate]` + `httpx` (via `[http]`) + `tenacity` | `pf_core.clients.*` (OpenRouter / Brave / Claude Code) on top of `[validate]`. `[llm]` ⊇ `[validate]`. (`pf_core.llm.tracked` additionally needs `[tracking]` — it records to the DB.) |
| `[http]` | `httpx` | `pf_core.utils.urls`, `pf_core.utils.url_liveness` (URL liveness + Wayback checks) |
| `[cli]` | `typer`, `click` | `pf_core.cli.create_cli` — Typer command scaffolding |
| `[db]` | `sqlalchemy`, `alembic` | `pf_core.db.*`, `pf_core.alembic`, fully-functional DB-backed cost guards (`BudgetRepo`, `CostRateRepo`) |
| `[web]` | `fastapi`, `jinja2`, `uvicorn[standard]` | `pf_core.web.app_factory`, error pages, markdown, pagination, templates |
| `[jobs]` | (depends on `[db,cli]` + `pydantic`) | `pf_core.jobs.*` — state machine, step history, worker leases, `pf-jobs` CLI |
| `[tracking]` | (depends on `[db,llm]`) | `pf_core.llm.tracking.*`, `pf_core.llm.cache.*` — one DB row per LLM call, cache hit-rate analytics |
| `[admin]` | (depends on `[web,tracking]`) | `pf_core.web.llm_admin` — mountable admin dashboard |
| `[eval]` | (depends on `[tracking,jobs]`) | `pf_core.eval.*` — golden-set replay, comparators (Python API) |

### Tier 2 — driver and feature extras

| Extra | Adds | When to use |
|---|---|---|
| `[mysql]` | `pymysql` | MySQL/MariaDB consumers |
| `[postgres]` | `psycopg[binary]` | PostgreSQL consumers |
| `[redis]` | `dogpile.cache`, `redis` | Caching layer (graceful no-op without it) |
| `[ratelimit]` | `slowapi` (depends on `[web]`) | Per-IP rate limiting on FastAPI routes |
| `[jsonschema]` | `jsonschema` | Strict JSON Schema path in `pf_core.llm.validate` |
| `[articles]` | `trafilatura`, `htmldate`, `tenacity` | Article fetch + extract (`pf_core.utils.article_fetch`) |
| `[crawl]` | = `[http,articles]` | **Intent extra: "crawl/fetch web pages."** Title, body, publish date (`article_fetch`) + URL liveness/Wayback checks (`utils.urls`) in one named line — serving a front-end is a different intent (`[web]`) |
| `[anthropic]` | `anthropic` | Direct Anthropic SDK client (`pf_core.clients.anthropic`) — multimodal/vision, direct cache + input/output token reporting |
| `[image-phash]` | `ImageHash`, `Pillow` | Perceptual-hash image dedup (`pf_core.utils.phash`) |

SQLite needs no driver extra — it's part of stdlib.

### Tier 3 — meta-extra for the maximalist

```bash
pip install pf-core[full,postgres]
```

`[full]` is shorthand for `[db,web,llm,cli,jobs,tracking,admin,eval,redis,ratelimit,jsonschema]`. Add the dialect driver (`[mysql]` or `[postgres]`) and any optional capability extras (`[articles]`) separately.

### Tier 4 — dev / test

| Extra | Adds | When to use |
|---|---|---|
| `[dev]` | `pytest`, `httpx`, `factory-boy`, `ruff`, `pre-commit` | Tests, linting, and the build-gate hooks |
| `[test-containers]` | `testcontainers[mysql]` | Integration tests against a real MySQL container |

## Combining extras

```bash
pip install -e ".[full,postgres,articles]"
```

Comma-separated, no spaces. Quote the spec when using zsh.

## Example install profiles

Real-world shapes, by project type. Anything importing `pf_core.clients` / `pf_core.llm` resolves to `[llm]` — `[full]` profiles transitively, the batch-pipeline profile via `[tracking]` (which is `[db,llm]`). Declaring `[llm]` explicitly is still good practice so each pyproject self-documents what it uses.

| Project shape | Install line |
|---|---|
| Full-stack web app, Postgres | `pf-core[full,llm,postgres]~=0.12.0` |
| Full-stack web app, MySQL + article ingest | `pf-core[full,llm,mysql,articles]~=0.12.0` (+ `[redis,ratelimit]` if caching/limits are used) |
| Full-stack web app, SQLite | `pf-core[full,llm]~=0.12.0` (SQLite driver is stdlib) |
| Batch document pipeline (no web/db) | `pf-core[image-phash,tracking,llm]~=0.12.0` |
| Foundation-only CLI (no LLM at all) | `pf-core[cli]~=0.12.0` |

## Updating the dependency

### Day-to-day (local editable install)

If pf-core is installed as editable (`pip install -e`), there is nothing to do. Edit a file in `pf-core/`, and every project using it sees the change immediately.

### Releasing a new version

When a change is stable and you want it on PyPI for other machines:

```bash
# 1. Bump the version in pyproject.toml (__version__ derives from it), commit
cd ~/projects/pf-core
git add -A && git commit -m "what changed"

# 2. Tag the release and push — a v* tag triggers .github/workflows/publish.yml,
#    which runs the full suite, builds the sdist/wheel, and uploads to PyPI
git tag vX.Y.Z
git push origin main --tags

# 3. Bump the compatible-release pin in each consumer's pyproject.toml
#    "pf-core[full,postgres]~=0.12.0"

# 4. Reinstall in each consumer
cd ~/projects/my-project
pip install -U -e .
```

PyPI versions are immutable — a published version can never be replaced, so bump the version for every release (never re-tag an existing one). A `~=0.12.0` consumer picks up `0.12.x` patches on the next reinstall with no pin change; a new minor (`0.13.x`) requires a deliberate pin bump. Patch fixes land on the newest minor only — once a new minor ships, older lines are frozen, so bumping the pin is how a consumer keeps receiving fixes. If OIDC trusted publishing isn't active for the run, publish manually from the pf-core checkout: `python -m build && twine upload -u __token__ dist/*`.

### On a fresh machine

```bash
git clone <project-repo>
cd <project>
python -m venv .venv && source .venv/bin/activate
pip install -e ".[full,postgres,redis]"   # whichever extras the project needs
```

pf-core is pulled from PyPI automatically via the version pin in `pyproject.toml`.

If you also need to develop pf-core locally on that machine:

```bash
git clone https://github.com/phierceweb/pf-core.git ~/projects/pf-core
pip install -e ~/projects/pf-core[full]   # overrides the PyPI install with editable
```

## Verifying which copy is loaded

When patching pf-core, always verify the consumer is loading your edits and not a stale wheel from site-packages:

```bash
python -c "import pf_core; print(pf_core.__file__)"
# /Users/you/projects/pf-core/src/pf_core/__init__.py   ← editable, good
# /Users/you/.venv/lib/python3.11/site-packages/pf_core/...  ← installed copy, NOT being edited
```

If the path points into `site-packages`, you're editing files that aren't being imported. Reinstall with `pip install -e ~/projects/pf-core` to fix.

## Testing

```bash
cd ~/projects/pf-core
python3.12 -m venv .venv                          # 3.11+; skip if .venv already exists
.venv/bin/python -m pip install -e ".[full,dev,anthropic]"
.venv/bin/pytest
```

The `[dev]` extra installs pytest. The base fixture `pf_app_client` is auto-registered as a pytest plugin via the `pf_core` entry point in `pyproject.toml` — no `conftest.py` import needed. The DB fixtures (`pf_engine`, `pf_connection`, `pf_tables`) require the `[db]` extra and are opt-in: add `pytest_plugins = ["pf_core.testing.db_fixtures"]` to your `conftest.py`. See [testing.md](testing.md).

The full test suite requires `[full,dev,anthropic]` — most fixtures and integration tests exercise the DB and web layers, and the Anthropic client tests patch the `anthropic` SDK, which is its own extra (not part of `full`). Without `[anthropic]`, those tests error instead of skipping.

### Verifying the bare install stays lean

The in-suite `tests/test_pyproject_tiers.py` guards the dependency *metadata* (base stays free of httpx/pydantic/typer/json-repair/tenacity; `[llm]`/`[tracking]`/`[full]` compose correctly). To verify the *actual* install behaviour — that a bare `pip install pf-core` resolves to only the five foundation deps and that gated modules raise a friendly `ImportError` — run:

```bash
python bin/verify-bare-install
```

It builds a throwaway venv, installs pf-core with no extras, and runs `bin/bare_install_smoke.py` inside it. Run this after any change to the dependency tiers; CI runs the same smoke on every push (the `bare` job in `.github/workflows/test.yml`).
