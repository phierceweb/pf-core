# Changelog

Notable changes to pf-core, newest first. The project is pre-1.0 — pin to a tagged release; `main` is the development line.

## 0.1.0 — initial public release

The feature set, by area.

### Foundation (base install)
Structured logging (structlog — colored console, JSON-lines for production; handlers attach to the root logger by default, so a consumer's logs are covered whatever its package is named), a two-branch exception hierarchy (domain failures vs. errors) that the web layer maps to 4xx/5xx, env-driven `AppConfig` (override → env → YAML → default), `Service` / `Repository` / orchestrator base classes, thread-pool batch execution (`pf_core.parallel`) with per-item failure isolation and progress reporting, and date / id / JSON-recovery / canonical-JSON / content-hash / atomic-write utilities. Five dependencies; no httpx, no pydantic, no LLM code.

### LLM clients & routing (`[llm]`)
OpenRouter, Anthropic (official SDK), and Claude Code (`claude --print`, $0 against a Claude Max session) behind one `chat(messages, model) -> (content, usage)` interface. A YAML model router (`resolve_agent`) selects a backend per agent — nested per-backend model blocks, availability and call-failure fallback, and a registry for custom backends (Ollama, direct OpenAI, …). Combined with `pf_core.parallel`, a batch of calls can run concurrently and route across providers or onto a Claude Max session. Each client has a fail-fast preflight auth check and transient-failure retry.

### Output guards (`[validate]`)
`pf_core.llm.parse` recovers JSON from fenced, truncated, or prose-wrapped responses; `pf_core.llm.validate` checks the result against a schema with optional semantic and cross-field validators. Installs without the client stack, so output from any transport can be guarded.

### Cost & observability (`[tracking]`)
One database row per LLM call (prompt, tokens, cost, validations, job attribution), so spend and quality are queryable and runs replayable. Per-call cost comes from `pf_core.pricing` — a shared per-model rate table the clients use to populate `usage["cost_usd"]`, also callable directly for pre-call estimates (built-in Anthropic rates; register others with `register_rates`). A pre-call budget guard enforces daily/monthly caps with a kill-switch; an exact-match cache skips paying for identical calls; prompts are versioned and linked to the runs they produced; and an eval harness replays golden sets against a new model or prompt with structured-diff and LLM-judge comparators.

### App framework (`[db]` / `[web]` / `[jobs]` / `[admin]`)
A multi-dialect database layer (SQLite / MySQL / PostgreSQL, identical API) with a shared Alembic runner, dialect-agnostic `insert_ignore` / `upsert`, and an append-only versioned-config resolver; a FastAPI app factory with self-contained error pages and content negotiation; a job tracker with a state machine, idempotent step history, and worker leases; a mountable admin dashboard for runs, costs, and budgets; and pipeline helpers (run-records, baselines, baseline-diff, stage-cascade cache invalidation, resume, sequencer) plus incremental markdown export.

### Dev tooling
A structural build gate (`pf_core.guards`) that fails CI and pre-commit on oversized files, an auto-registered pytest fixture plugin (isolated per-test database, FastAPI test client), a Typer CLI scaffold, and client-side request throttling. Ships a PEP 561 `py.typed` marker. `bin/new-consumer <name> --layout {lib|app}` scaffolds a runnable, conformant consumer project (both layouts) with a day-1 vertical slice — see `docs/scaffold.md`.

### Packaging
A dependency-light base plus opt-in, orthogonally-composable extras (`[validate]`, `[llm]`, `[db]`, `[web]`, `[jobs]`, `[tracking]`, `[eval]`, `[admin]`, `[crawl]`, dialect drivers, and more). Importing a gated module without its extra raises an `ImportError` naming the extra and the pip command.
