# Modules

What each module in `src/pf_core/` is responsible for, grouped by concern. Every entry links to the module's own doc for usage details.

This file is the navigational index. It describes responsibilities, not APIs — for API-level detail (function signatures, parameters, examples) see the linked module doc.

---

## Table of Contents

- [LLM observability](#llm-observability)
- [AI-quality guards](#ai-quality-guards)
- [Cost & budget](#cost--budget)
- [LLM clients & content fetch](#llm-clients--content-fetch)
- [Evaluation](#evaluation)
- [Database](#database)
- [Jobs & layered architecture](#jobs--layered-architecture)
- [Web & API](#web--api)
- [Dev workflow](#dev-workflow)
- [Configuration & infrastructure](#configuration--infrastructure)
- [Pipeline ergonomics](#pipeline-ergonomics)
- [Utilities](#utilities)
- [Recipes](#recipes)

---

## LLM observability

Persist and inspect every LLM call so cost, prompts, validations, and outcomes are queryable and replayable.

| Module | Concern |
|---|---|
| [LLM tracking](llm-tracking.md) | One database row per LLM call with sidecar tables for prompts, parsed output, validations, links, tags, metrics, and job attribution. The single source of truth that the dashboard, eval harness, cache, and budget guards all read from. |
| [Tracked LLM call](llm-tracked.md) | `tracked_call` — render spec → invoke injected client → record one run → tracked JSON retry. The common "spec → invoke → record → maybe-parse" shape in one function. |
| [LLM cache](llm-cache.md) | Avoid paying for identical calls twice. Per-agent TTL policy with hit-rate analytics. |
| [Prompts](prompts.md) | Load prompts from YAML, version them, and register them in the database so every tracked run links back to the prompt that produced it. |
| [Model router](model-router.md) | Per-agent model, sampling, and backend configuration loaded from YAML with live reload. Swap an agent's model — or which client serves it (`resolve_agent`, nested per-backend blocks, opt-in availability fallback, custom backends via the client registry) — without code changes or restarts. |
| [LLM admin dashboard](llm-admin.md) | Mountable FastAPI sub-app showing runs, costs, jobs, cache stats, and budgets. |

## AI-quality guards

Mitigate the characteristic ways LLMs fail: hallucinated URLs, drifted output shapes, malformed JSON, near-duplicates, and free-text that should be a controlled vocabulary.

| Module | Concern |
|---|---|
| [Anti-hallucination pattern](anti-hallucination.md) | Architectural guidance: constrain LLM input from real data sources rather than try to detect fabrication after the fact. |
| [LLM schema validation](llm-schema-validation.md) | Three-tier validation pipeline (shape, semantic, cross-field) for parsed LLM output, with a registry of pluggable validators. |
| [LLM URL validation](llm-validation.md) | Pluggable URL hallucination dispatcher for catching invented citations. |
| [LLM parse](llm-parse.md) | High-level pipeline that recovers structured data from imperfect LLM responses — markdown fences, trailing prose, truncation, malformed JSON (`[validate]`). Composes the generic [JSON recovery](json-recovery.md) utilities. |
| [Gather/apply drift detection](llm-safe-apply.md) | Safely apply LLM-planned transforms after other code may have changed the data. Re-extracts current targets and skips with a warning if counts/texts drifted from gather time. |
| [Similarity](similarity.md) | Detect near-duplicate text outputs without exact matching. |
| [Vocab](vocab.md) | Map free-text LLM output to canonical slugs with explicit-reject vs unknown distinction. |

## Cost & budget

Pre-call spending guardrails so an LLM agent can never silently burn through a budget.

| Module | Concern |
|---|---|
| [Cost & budget](cost-budget.md) | Daily and monthly caps scoped by agent, job kind, job ID, or tag. Hard-block and warn modes, soft-threshold alerts, kill-switch, and cost projection from a rates table or rolling mean. |
| [Pricing](pricing.md) | Shared per-model cost estimation (`estimate_cost`) — the clients populate `usage["cost_usd"]` from it, and it's callable directly for pre-call estimates. Built-in Anthropic rates; register others with `register_rates`. |

## LLM clients & content fetch

Wrappers for the external services that LLM agents talk to. All clients integrate with the tracking and budget systems.

| Module | Concern |
|---|---|
| [OpenRouter client](openrouter.md) | Primary chat client. Timeouts, provider routing, usage tracking, citation handling. |
| [Claude Code client](claude-code.md) | Subprocess wrapper for Claude Max sessions. Drop-in alternative when you want to use a Claude Max plan instead of paying API credits. |
| [Anthropic client](anthropic.md) | Direct wrapper around the official `anthropic` SDK. Multimodal-capable; reports cache + input/output tokens directly. |
| [Brave search client](brave.md) | Web search for grounding LLM calls in real results (the backbone of the anti-hallucination pattern). |
| [Article fetch](article-fetch.md) | Title, body, and publish-date extraction with Wayback Machine fallback for paywalled or blocked URLs. |

## Evaluation

Replay historical LLM calls against new models, prompts, or comparators. Decide whether a change is an improvement before shipping it.

| Module | Concern |
|---|---|
| [Eval harness](eval-harness.md) | Tag-based golden sets, structured-diff and LLM-judge comparators, per-agent tolerances, parallel replay, and a `pf-eval` CLI. |

## Database

SQLAlchemy-based data layer that works identically across SQLite, MySQL/MariaDB, and PostgreSQL.

| Module | Concern |
|---|---|
| [Database](database.md) | Engine setup, transaction context manager, dialect detection, connection helpers, and a `Repository` base class for organizing query functions. |
| [Dialect-agnostic upserts](db-upsert.md) | `insert_ignore` / `upsert` — build the live dialect's insert-on-conflict (`ON CONFLICT` on SQLite/Postgres, `ON DUPLICATE KEY UPDATE` on MySQL/MariaDB) from `Table` metadata, so consumers never write SQLite-only `INSERT OR IGNORE`/`REPLACE`. |
| [Soft delete](soft-delete.md) | `deleted_at` timestamp pattern with helpers for soft-delete, restore, and not-deleted filtering. |
| [Versioned config](versioned-config.md) | `get_latest` / `append_version` / `get_latest_with_fallback` — the append-only, latest-version-wins config-table pattern (insert a new row at `version+1`; readers take the max per scope), with optional carry-forward of unspecified columns and default-scope fallback. |
| [Alembic migrations](alembic.md) | Shared migration runner — each project's `env.py` becomes a few lines. |

## Jobs & layered architecture

Coordinate multi-step work across services and persist progress so jobs survive restarts.

| Module | Concern |
|---|---|
| [Jobs](jobs.md) | Generic job tracker with state machine, idempotent step history, worker-claim leases, and automatic LLM-run attribution. Includes a `pf-jobs` CLI. |
| [Orchestrators](orchestrators.md) | Base class for multi-step workflows that coordinate several services. Enforces the layered architecture. |
| [Services](services.md) | Base class for single-domain business logic. Standard hooks for repos, logging, and configuration. |

## Web & API

FastAPI scaffolding so a new consumer doesn't re-implement error pages, content negotiation, and request logging.

| Module | Concern |
|---|---|
| [Web app factory](web.md) | `create_app()` that ships with structured request logging, self-contained HTML error pages with JSON content negotiation, CORS, and exception-to-status-code mapping. |
| [Markdown](markdown.md) | Escape-first safe markdown rendering for user-generated or LLM-generated content. |
| [Pagination](pagination.md) | Validated pagination params and result-metadata builders with has-next detection that doesn't require a count query. |

## Dev workflow

Make architecture violations and bloated files fail the build, not a code review.

| Module | Concern |
|---|---|
| [Linting](linting.md) | Layer import linter (enforces the call direction in [`.ai/rules/layering.md`](../../../.ai/rules/layering.md)) and file-size linter (per-layer line budgets with project overrides). |
| [CLI](cli.md) | Typer scaffold with consistent verbose flag and exception-to-exit-code mapping. |
| [Testing](testing.md) | Pytest fixtures auto-registered as a plugin: isolated per-test SQLite database (file-backed, concurrency-safe), savepoint-per-test, FastAPI test client. |
| [Output reporters](output.md) | Reporter protocol for batch progress, with console (Rich) and structlog implementations. |

## Configuration & infrastructure

| Module | Concern |
|---|---|
| [Config](config.md) | `AppConfig` base class. Resolution order: explicit override > environment variable > YAML > class default. Type coercion built in. |
| [Exceptions](exceptions.md) | Two-branch hierarchy that keeps expected domain failures separate from unexpected system errors. The web layer maps each branch to the right HTTP status; the logging layer treats each differently. |
| [Logging](logging.md) | structlog setup with colored console for development and JSON-lines for production ingestion. |
| [Cache](cache.md) | Redis-backed regions with graceful degradation when Redis is unavailable. |
| [Project portability](project-portability.md) | The rule that keeps pf-core consumable: zero hardcoded project names, organizations, jurisdictions, or domains in framework code. |

## Pipeline ergonomics

Generic patterns for multi-stage pipelines: stamp run records, snapshot output for later comparison, diff baselines, invalidate caches by stage, resume from upstream snapshots. Composable — adopt as much or as little as the consumer needs.

| Module | Concern |
|---|---|
| [Pipeline](pipeline.md) | The full set: `run_record` (stamp + sha256), `baseline` (snapshot), `baseline_diff` (compare), `cache` (stage-cascade invalidation), `resume` (snapshot validity check), `sequencer` (run a named slice of ordered phases). |
| [CLI subcommand factories](cli-subcommands.md) | Typer factories that wrap the pipeline modules so consumer CLIs don't repeat the same Typer command bodies (`baseline save/list/diff`, `invalidate`). |

## Utilities

Small, focused helpers used across the framework.

| Module | Concern |
|---|---|
| [Parallel](parallel.md) | Thread-pool batch execution with progress reporting, per-item failure isolation, and opt-in end-of-batch summary log. |
| [Throttle](throttle.md) | `Throttle` — thread-safe client-side request pacing (minimum interval / max-per-second) for staying under a rate-limited API's cap. The outbound counterpart to `pf_core.web.rate_limit`. |
| [Atomic file writes](io.md) | Crash-safe `atomic_write_text` / `atomic_write_json` for caches, manifests, and run-record sidecars. Target file is either old or new content, never a torn write. |
| [Markdown export](export.md) | `MarkdownExporter` — turn a system-of-record into a markdown tree for RAG/review, incrementally (write-if-changed + atomic + scoped orphan prune). Includes a `yaml_frontmatter` helper. |
| [Env-var resolver](env.md) | `resolve_int` / `resolve_str` / `resolve_bool` — explicit-arg → env var → default with warn-on-malformed. Codifies the `config-driven.md` rule. |
| [Structural guards](guards.md) | Build gate (`python -m pf_core.guards`): fails pre-commit + CI on files over the hard line limit, with a baseline ratchet. Also ships a layering checker (`check_layering`) for consumer four-layer apps. Turns `code-style.md` / `layering.md` rules into enforcement. |
| [Perceptual-hash image dedup](phash.md) | DCT-based image fingerprinting + Hamming-distance clustering for detecting recurring page decorations and re-encoded duplicates that sha256 misses. Optional `[image-phash]` extra. |
| [IDs](ids.md) | URL-safe nanoid generation with collision-safe allocation against a database table. |
| [Dates](dates.md) | Stable parsing, formatting, and range generation for ISO dates and YYYY-MM month labels. |
| [Relative dates](relative-dates.md) | Resolve LLM-emitted phrases like "yesterday" or "Tuesday" against a known reference date. |
| [URLs](urls.md) | Domain extraction, canonical URL forms, suspicious-archive-timestamp detection. |
| [JSON utils](json-utils.md) | Tolerant JSON parsing for database columns that may already be parsed, plus `canonical_json` — sorted-key serialization so equal objects compare and hash identically. |
| [Content hashing](hashing.md) | `content_hash` — stable hex digest of text or a structured object (via `canonical_json`) for change-detection and cache keys. Not a security primitive. |
| [JSON recovery](json-recovery.md) | Extract/recover JSON from messy text — markdown fences, trailing prose, mid-stream truncation. Generic, stdlib-only. |
| [Parsers](parsers.md) | HTML parsing primitives shared across content-extraction code. |

## Recipes

Patterns for common LLM-app shapes. Each recipe shows how the modules above compose end-to-end.

| Recipe | Pattern |
|---|---|
| [Batch LLM service](recipes/batch-llm-service.md) | N items processed in parallel with caching, budget guard, validation, tracking, and job-progress reporting. |
| [Critic](recipes/critic.md) | Primary agent plus a critic agent that checks the first result; runs are linked for traceability. |
| [Self-consistency](recipes/self-consistency.md) | Same prompt N times at non-zero temperature with majority vote — trades tokens for accuracy on high-stakes classifications. |
| [Job ↔ refs bridge](recipes/job-refs-bridge.md) | Connect generic job state to domain-specific reference tables. |
