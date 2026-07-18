# pf-core

[![PyPI](https://img.shields.io/pypi/v/pf-core)](https://pypi.org/project/pf-core/)

A dependency-light Python foundation for building LLM applications — and one built to be worked on by AI coding agents as much as by people. The base install provides structured logging, an exception hierarchy, config-from-env, and a service/repo architecture; opt-in extras add LLM clients, output validation, cost tracking and budgets, an eval harness, and a FastAPI + SQLAlchemy app framework. Capabilities compose orthogonally — the foundation alone, the LLM layer without a database, or the web layer without LLMs.

## Built for AI-assisted development

The conventions that keep a codebase legible to an AI agent are enforced, not suggested. A build gate fails CI when a file grows past its line budget — small files stay within a model's working context and edit cleanly — and a companion checker flags imports that cross a consumer app's layered architecture the wrong way. Logging, errors, config, and data access each have one obvious way to do them, documented one-module-per-file for retrieval, so generated code lands in the same shapes as hand-written code instead of drifting. The result is a substrate where an agent can do real work and the guardrails hold.

## One interface over every LLM backend — including Claude Code

OpenRouter (paid API), the Anthropic SDK, and Claude Code (a local Claude Max session, $0 per call) sit behind the same `chat(messages, model) -> (content, usage)` interface. A YAML model router assigns a backend per agent and falls back to the next available one; a registry accepts custom backends (Ollama, direct OpenAI, …). Because the clients are interchangeable and `pf_core.parallel` fans work across a thread pool, a batch of LLM calls can run concurrently and route anywhere — a large batch pushed onto a Claude Max subscription instead of spending API credits, or spread across providers — while every call is still tracked and budget-checked the same way.

## Output guards and observability

LLMs return fenced, truncated, or not-quite-JSON output; pf-core recovers it (`pf_core.llm.parse`) and validates the result against a schema with optional semantic and cross-field checks (`pf_core.llm.validate`) — available without the client stack, so output from any transport can be guarded. Every call can record one database row (prompt, tokens, cost, validations, and the job it belongs to), making spend and quality queryable and runs replayable. Pre-call budget checks enforce daily/monthly caps with a kill-switch, a cache skips paying for identical calls, prompts are versioned and linked to the runs they produced, and an eval harness replays golden sets against a new model or prompt to show whether a change is an improvement before it ships.

## The rest of the framework

A multi-dialect database layer (SQLite / MySQL / PostgreSQL, identical API) with a shared Alembic runner; a FastAPI app factory with self-contained error pages and content negotiation; a job tracker with a state machine, idempotent step history, and worker leases so multi-step work survives restarts; a mountable admin dashboard for runs, costs, and budgets; and pipeline helpers for run-records, baselines, and stage-cascade cache invalidation. See **[docs/modules.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/modules.md)** for the full index.

## Install

```bash
pip install pf-core                  # foundation only — no LLM, no DB, no web
pip install pf-core[validate]        # + output guards (no clients/HTTP)
pip install pf-core[llm]             # + LLM clients (includes [validate])
pip install pf-core[full,postgres]   # the whole app framework
```

Pin a **compatible release** for stability — e.g. `pip install "pf-core[llm]~=0.7.0"` (picks up `0.7.x` fixes, holds below the next minor; substitute the current release from the [changelog](https://github.com/phierceweb/pf-core/blob/main/CHANGELOG.md)). To track unreleased work, install from git instead — `main` is the development line and may contain work between releases:

```bash
pip install "pf-core[llm] @ git+https://github.com/phierceweb/pf-core.git@main"
```

Extras compose orthogonally (`[db]` without LLM, `[web]` without `[db]`, `[llm]` standalone); importing a gated module without its extra raises an `ImportError` naming the extra and the pip command. Full matrix and release/update flow: **[docs/INSTALLATION.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/INSTALLATION.md)**.

## Documentation

- **[docs/INSTALLATION.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/INSTALLATION.md)** — extras matrix, install/release/update flows, verification
- **[docs/modules.md](https://github.com/phierceweb/pf-core/blob/main/src/pf_core/docs/modules.md)** — one-line-per-module index, grouped by concern
- **[docs/](https://github.com/phierceweb/pf-core/tree/main/src/pf_core/docs)** — per-module reference with usage and parameter detail
- **[CHANGELOG.md](https://github.com/phierceweb/pf-core/blob/main/CHANGELOG.md)** — release history

Docs ship inside the package (`pf_core/docs/` under site-packages), so an installed copy always matches its version — the links above render `main`, which may be ahead of the latest release.

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
