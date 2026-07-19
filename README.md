# pf-core

[![PyPI](https://img.shields.io/pypi/v/pf-core)](https://pypi.org/project/pf-core/)

A Python foundation for building LLM applications whose prompts and spend you can actually see: every prompt lives in versioned configuration, and every call is recorded — prompt version, model, provider, response, tokens, cost — and replayable, in your choice of SQLite, MySQL, or Postgres. The base install is dependency-light (structured logging, an exception hierarchy, config-from-env, a service/repo architecture); opt-in extras add the LLM clients, output validation, cost tracking and budgets, an eval harness, and a FastAPI + SQLAlchemy app framework. Capabilities compose orthogonally — the foundation alone, the LLM layer without a database, or the web layer without LLMs.

## Why pf-core

The LLM tooling landscape solves each concern separately: one product routes calls across providers, another traces and meters them, another versions prompts, another runs evals, another repairs malformed JSON, another lints repo structure. Stitch several together and the seams show — every tool brings its own config, its own storage, its own id space, and no shared key joins a prompt version to the tokens it burned or the validation it failed. pf-core is those concerns designed as one system; the integration is the feature.

It started as the shared core of several AI-assisted projects that fanned multithreaded LLM calls out to external providers. Each was built differently, with the important details — config, prompts, clients, methodology — buried in whichever file the AI found closest: working software, unreadable data, mounting tech debt. pf-core solves the five problems those projects kept hitting:

1. **Prompts buried in code.** Every prompt lives in versioned YAML configuration — seen, diffed, and tuned. Change one materially and you bump its version; old and new both stay in the registry.
2. **Calls unrecorded.** Every call lands as one database row: prompt version, model, provider, sampling, response, tokens, cost. Each run links to the exact prompt version it used.
3. **Spend ungoverned.** Budgets are checked before the call — daily/monthly caps scoped by agent, job, or tag, with a kill-switch and projection — and a cache refuses to pay twice for identical work.
4. **Upgrades are guesses.** Approved runs promote into a golden set; a new model or prompt replays against it and shows its deltas before it ships — in CI, if you want a failing eval to block the merge.
5. **Long batch work dies and forgets.** Jobs resume after a crash by skipping already-succeeded steps, and every LLM call inside a job is attributed to it automatically — no job id threaded through function signatures.

Deep dives on each: [prompts](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/prompts.md), [llm-tracking](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-tracking.md), [cost-budget](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/cost-budget.md), [llm-cache](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-cache.md), [eval-harness](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/eval-harness.md), [jobs](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/jobs.md).

All five land in the same place: one `llm_runs` row per call, plus sidecar tables for payloads, validations, tags, metrics, and run-to-run links. The dashboard, the cache, the budget checks, and the eval harness all read those same tables — which is what makes prompt-and-model combinations tunable in aggregate: in a dozen-step pipeline, each step with its own prompt and model, every combination is queryable side by side.

In a complex data pipeline the stakes are higher, because prompts feed prompts: step 3 consumes what steps 1 and 2 produced, so an upstream prompt or model change ripples through everything downstream. The same tables answer that too. Every call in a pipeline run shares a job id, and every run records the exact prompt version and model it used — so whole pipeline runs can be grouped by their upstream configuration and compared on their downstream results. Concretely: run the pipeline with steps 1 and 2 on prompt v2 and one model, run it again with the same prompts on a different model, leave every later step unchanged, and compare the later steps' validations and metrics between the two groups. The question being answered is not "which prompt is better in isolation" but "which upstream choice produced better results three steps later" — and it's answerable with a query, because everything landed in one schema.

The test suite runs against real Postgres and MySQL databases, not just SQLite, and live smoke tests exercise the real providers. Misconfiguration fails at deploy time: an unresolvable eval judge or an invalid router entry raises `ConfigurationError` rather than silently picking a default.

## One interface over multiple LLM backends — including Claude Code

OpenRouter (paid API), the Anthropic SDK, and Claude Code (a local Claude Max session, $0 per call) sit behind the same `chat(messages, model) -> (content, usage)` interface. A YAML model router assigns a backend per agent and falls back to the next available one; a registry accepts custom backends (Ollama, direct OpenAI, …). Because the clients are interchangeable and `pf_core.parallel` fans work across a thread pool, a batch of LLM calls can run concurrently and route anywhere — a large batch pushed onto a Claude Max subscription instead of spending API credits, or spread across providers — while every call is still tracked and budget-checked the same way.

## Output guards

LLMs return fenced, truncated, or not-quite-JSON output; pf-core recovers it (`pf_core.llm.parse`) and validates the result against a schema with optional semantic and cross-field checks (`pf_core.llm.validate`) — available without the client stack, so output from any transport can be guarded, and validation results are recorded on the run they judged.

## The database layer

One API over SQLite, MySQL, and PostgreSQL — develop on SQLite, deploy on a server database without query changes; a shared Alembic runner handles migrations. The tracking tables are written by parallel workers, and the write paths are shaped for that: reference-table lookups resolve in their own short transactions before the main write opens, sidecar writes are idempotent upserts rather than delete-then-insert, and worker claims use a portable SELECT-then-UPDATE that also runs on SQLite. MySQL connections are pinned to UTC and time cutoffs are computed server-side, so timestamps agree across dialects. Details: **[docs/llm-tracking.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-tracking.md)** and **[docs/database.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/database.md)**.

## The application framework

pf-core is a framework, not just a client library. Consumer apps follow a layered architecture — entry points → orchestrators → services → repositories, no layer importing from above — with a `Service` base class carrying config injection, logging, and repo access. Around that: a FastAPI app factory with self-contained error pages and content negotiation; a job tracker with a state machine, idempotent step history, and worker leases so multi-step work survives restarts; a mountable admin dashboard for runs, costs, and budgets; and pipeline helpers for run-records, baselines, and stage-cascade cache invalidation. See **[docs/modules.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/modules.md)** for the full index.

## Built for AI coding agents

pf-core is built to be worked on by AI coding agents as much as by people — and that part pays off even when a project has no LLM calls of its own. The conventions that keep a codebase legible to an agent are enforced, not suggested: a build gate fails CI when a file grows past its line budget (small files stay within a model's working context and edit cleanly), a companion checker flags imports that cross the layered architecture the wrong way, and logging, errors, config, and data access each have one obvious way to do them — so generated code lands in the same shapes as hand-written code instead of drifting. The testing bootstrap ships in the framework itself as an auto-registered pytest plugin (fixtures, framework-table DDL, hermetic test env), and the consumer scaffold stamps a runnable smoke test into every new project.

## Install

```bash
pip install pf-core                  # foundation only — no LLM, no DB, no web
pip install pf-core[validate]        # + output guards (no clients/HTTP)
pip install pf-core[llm]             # + LLM clients (includes [validate])
pip install pf-core[full,postgres]   # the whole app framework
```

Pin a **compatible release** for stability — e.g. `pip install "pf-core[llm]~=0.8.0"` (picks up `0.8.x` fixes, holds below the next minor; substitute the current release from the [changelog](https://github.com/phierceweb/pf-core/blob/main/CHANGELOG.md)). To track unreleased work, install from git instead — `main` is the development line and may contain work between releases:

```bash
pip install "pf-core[llm] @ git+https://github.com/phierceweb/pf-core.git@main"
```

Extras compose orthogonally (`[db]` without LLM, `[web]` without `[db]`, `[llm]` standalone); importing a gated module without its extra raises an `ImportError` naming the extra and the pip command. Full matrix and release/update flow: **[docs/INSTALLATION.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/INSTALLATION.md)**.

## Documentation

- **[docs/INSTALLATION.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/INSTALLATION.md)** — extras matrix, install/release/update flows, verification
- **[docs/modules.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/modules.md)** — one-line-per-module index, grouped by concern
- **[docs/](https://github.com/phierceweb/pf-core/tree/main/src/pf_core/docs)** — per-module reference with usage and parameter detail
- **[CHANGELOG.md](https://github.com/phierceweb/pf-core/blob/main/CHANGELOG.md)** — release history

The docs are written to be read by AI assistants and ship inside the package (`pf_core/docs/` under site-packages), so an installed copy always matches its version — the links above render `main`. One command puts them where an in-repo assistant looks: `pf-setup`, installed with pf-core, links `docs/pf-core` to the bundled docs from any consumer's repo root — idempotent, and it refuses to touch a real file — while `pf-doctor` reports the link read-only (the `wiring.docs_link` row). New projects get the link from their first `bin/setup` after scaffolding (`bin/new-consumer <name> --layout {lib|app}` from a pf-core checkout). To just locate the installed docs:

```bash
python -c "import pf_core, pathlib; print(pathlib.Path(pf_core.__file__).parent / 'docs')"
```

<details>
<summary><b>All docs</b> — every file in <code>src/pf_core/docs/</code>, one link each</summary>

- [INSTALLATION](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/INSTALLATION.md)
- [alembic](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/alembic.md)
- [anthropic](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/anthropic.md)
- [article-fetch](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/article-fetch.md)
- [brave](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/brave.md)
- [cache](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/cache.md)
- [claude-code](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/claude-code.md)
- [cli-subcommands](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/cli-subcommands.md)
- [cli](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/cli.md)
- [config-path](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/config-path.md)
- [config](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/config.md)
- [cost-budget](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/cost-budget.md)
- [database](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/database.md)
- [dates](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/dates.md)
- [db-upsert](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/db-upsert.md)
- [doctor](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/doctor.md)
- [env](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/env.md)
- [eval-harness](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/eval-harness.md)
- [exceptions](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/exceptions.md)
- [export](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/export.md)
- [guards](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/guards.md)
- [hashing](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/hashing.md)
- [ids](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/ids.md)
- [io](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/io.md)
- [jobs](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/jobs.md)
- [json-recovery](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/json-recovery.md)
- [json-utils](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/json-utils.md)
- [linting](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/linting.md)
- [llm-admin](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-admin.md)
- [llm-cache](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-cache.md)
- [llm-parse](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-parse.md)
- [llm-recording](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-recording.md)
- [llm-safe-apply](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-safe-apply.md)
- [llm-schema-validation](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-schema-validation.md)
- [llm-tracked](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-tracked.md)
- [llm-tracking](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-tracking.md)
- [llm-validation](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/llm-validation.md)
- [logging](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/logging.md)
- [markdown](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/markdown.md)
- [model-router](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/model-router.md)
- [modules](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/modules.md)
- [openrouter](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/openrouter.md)
- [orchestrators](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/orchestrators.md)
- [output](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/output.md)
- [pagination](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/pagination.md)
- [parallel](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/parallel.md)
- [parsers](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/parsers.md)
- [periods](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/periods.md)
- [phash](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/phash.md)
- [pipeline](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/pipeline.md)
- [pricing](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/pricing.md)
- [project-portability](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/project-portability.md)
- [prompts](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/prompts.md)
- [relative-dates](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/relative-dates.md)
- [scaffold](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/scaffold.md)
- [services](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/services.md)
- [similarity](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/similarity.md)
- [soft-delete](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/soft-delete.md)
- [test-migration](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/test-migration.md)
- [testing](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/testing.md)
- [throttle](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/throttle.md)
- [urls](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/urls.md)
- [versioned-config](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/versioned-config.md)
- [vocab](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/vocab.md)
- [web](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/web.md)
- Recipes: [batch-llm-service](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/recipes/batch-llm-service.md) · [critic](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/recipes/critic.md) · [job-refs-bridge](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/recipes/job-refs-bridge.md) · [self-consistency](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/recipes/self-consistency.md)

</details>

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[full,articles,anthropic,image-phash,dev]"   # everything, so the full suite runs
pre-commit install
pytest
python bin/verify-bare-install                                # confirm the base install stays dependency-light
```

Pytest fixtures auto-register as a plugin via the `pf_core` entry point — no `conftest.py` import needed in consumers. Contribution guidelines: **[CONTRIBUTING.md](https://github.com/phierceweb/pf-core/blob/main/CONTRIBUTING.md)**.

## Project history

pf-core was developed privately from early April 2026 and first published on June 14, 2026, with the pre-publication history squashed. Public releases are tagged (`v*`) and published to PyPI by CI via OIDC trusted publishing ([publish.yml](https://github.com/phierceweb/pf-core/blob/main/.github/workflows/publish.yml)); `main` is the development line and is pushed with each release.
