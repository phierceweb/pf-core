# LLM Tracking

Records every LLM invocation a project makes — one row per call, plus optional sidecars for payloads, configs, validations, outcomes, run-to-run links, tags, and metrics.

This module replaces the ad-hoc per-project run-tracking tables a project tends to grow. The replacement is the `llm_runs` table plus seven sidecar tables, all prefixed `llm_` to signal framework ownership.

This doc is the implementation reference. The column-by-column schema definitions live in [`pf_core.llm.tracking.schema`](../llm/tracking/schema.py).

---

## Table of Contents

- [Concepts](#concepts)
- [Quick start](#quick-start)
- [`LlmRunRepo.record()` — atomic write](#llmrunreporecord--atomic-write)
- [`@track_run` decorator](#track_run-decorator)
- [Sidecar writers](#sidecar-writers)
- [Stats and read helpers](#stats-and-read-helpers)
- [Tags and metrics conventions](#tags-and-metrics-conventions)
- [Retention](#retention)
- [Schema overview](#schema-overview)
- [Query cookbook](#query-cookbook)
- [Three-dialect support](#three-dialect-support)
- [Adding a new project FK column](#adding-a-new-project-fk-column)
- [Adding a new validator or metric](#adding-a-new-validator-or-metric)
- [Adding a new agent type](#adding-a-new-agent-type)

---

## Concepts

- **run** — one row in `llm_runs` = one LLM invocation. The unit of accounting.
- **agent type / model / prompt** — reference rows (`llm_agent_types`, `llm_models`, `llm_prompts`). Prompts are versioned per `(agent_type, part)` where `part ∈ {'system', 'user', 'full'}`. Use `resolve_prompt_id()` to upsert a prompt row and get back the FK for `llm_runs.system_prompt_id` / `user_prompt_id` — see [prompts.md](prompts.md#registering-in-the-db).
- **payload** — cold sidecar holding rendered prompts, raw response, parsed output.
- **config** — parameter snapshot attached as `(config_kind, config_id)`. Soft FK into project tables.
- **validation** — quality signal at call time: `(validator, passed, severity, details)`.
- **outcome** — downstream business signal backfilled later (e.g. `summary_accepted`).
- **link** — run-to-run relation: `retry`, `critic`, `refine`, `fallback`, `subroutine`, `meta_analysis`.
- **tag** — colon-namespaced label (`eval:golden_v2`). Categorical signals.
- **metric** — numeric per-run signal (`coverage_ratio`, `n_items`). Indexed as a range.
- **job** — optional parent: one row in `jobs` can own many `llm_runs` rows via `llm_runs.job_id`. See [jobs.md](jobs.md).

**Do:** put numeric signals in metrics, categorical in tags.

**Do not:** add project-domain FKs (`task_id`, `item_id`) to base pf-core tables — projects ALTER `llm_runs` in their own migrations. Use `llm_run_configs` for parameter snapshots.

---

## Quick start

```python
from pf_core.llm.tracking import LlmRunRepo
from pf_core.clients.openrouter import get_client

content, usage = get_client().chat(
    model="anthropic/claude-sonnet-4.6",
    messages=[{"role": "user", "content": "Hello"}],
)

run_id = LlmRunRepo().record(
    agent_type="summarizer",
    model="anthropic/claude-sonnet-4.6",
    usage=usage,
    rendered_prompts=(None, "Hello"),
    raw_response=content,
)
```

Minimum-viable record is `agent_type + model`. Everything else is optional. Returns the new `llm_runs.id`.

---

## `LlmRunRepo.record()` — atomic write

One transaction, up to seven tables: `llm_runs` plus the six sidecars it writes at call time (`payloads`, `configs`, `validations`, `metrics`, `tags`, `links`). Outcomes arrive later, via `LlmRunOutcomeRepo`. The reference-table FKs are resolved in separate transactions first — see the behavior notes below.

```python
from pf_core.llm.tracking import LlmRunRepo

run_id = LlmRunRepo().record(
    agent_type="summarizer",
    model="claude-opus-4-7",
    system_prompt_id=ps_id,             # FK into llm_prompts
    user_prompt_id=pu_id,

    sampling={"temperature": 0.2, "top_p": 1.0, "max_tokens": 4096, "seed": 7},
    provider="openrouter",
    model_fingerprint=usage["system_fingerprint"],

    usage={
        "prompt_tokens": 1200, "completion_tokens": 800,
        "cache_read_tokens": 900, "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0052,
        "duration_ms": 3100,
    },

    items_out=7,
    status="success",                   # 'success' | 'failed' | 'partial' | 'filtered' | …

    # Sidecars — all optional
    configs={"prompt_variant": bv_id, "task_config": ec_id},
    validations=[
        ("url_hallucination", True,  "info",  None),
        ("coverage_ratio",     True,  "info",  {"ratio": 0.85}),
        ("json_schema",        False, "error", {"missing": ["fields"]}),
    ],
    metrics={"coverage_ratio": 0.85, "n_items": 12},
    tags=["env:prod", "agent:summarizer"],

    rendered_prompts=(rendered_sys, rendered_user),
    raw_response=response_text,
    parsed_output=parsed,                # any JSON-serializable value

    parent_run=(prior_run_id, "retry"),  # writes into llm_run_links

    extra_run_values={"thread_id": 12, "item_id": 87},  # project columns
)
```

**Behavior notes:**

- Reference-table FKs (`agent_type_id`, `model_id`) are resolved in their own short transactions *before* the write transaction opens — this avoids InnoDB lock cycles when parallel workers log the same agent/model pair simultaneously.
- `input_hash` is autocomputed as `SHA256(model + rendered prompts + sampling + configs)` if not provided. Pass an explicit value only to override the canonical algorithm.
- `validations` is a list of 4-tuples (positional, not kwargs): `(validator, passed, severity, details)`.
- `configs` is a flat `{kind: id}` dict — composite PK forbids two configs of the same kind on one run.
- Writing the same `(run_id, kind)` twice raises an integrity error. Use the sub-repos to overwrite.
- `extra_run_values` merges into the `llm_runs` INSERT after the framework-owned columns (last-write-wins on collision). Use it to persist **project-specific columns** you added to `llm_runs` via migration without subclassing — see [Adding a new project FK column](#adding-a-new-project-fk-column) below. Each key must name a column that exists on the `llm_runs` Table.

---

## `@track_run` decorator

Wraps any function whose return shape matches `(content, usage)` (the `OpenRouterClient.chat()` shape) or `{"content": ..., "usage": {...}}`.

```python
from pf_core.llm.tracking import track_run
from pf_core.llm.router import get_agent_config
from pf_core.clients.openrouter import get_client

@track_run(agent_type="summarizer", provider="openrouter")
def tracked_chat(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)

cfg = get_agent_config("summarizer")  # {"model": "...", "temperature": 0.2, ...}
content, usage = tracked_chat(messages=msgs, **cfg)
run_id = usage["_llm_run_id"]
```

Pass `provider=` explicitly — the backend name written to `llm_runs.provider`. With the router, use `resolve_agent(...).backend`. Pass `provider=None` to skip the label. Omitting `provider=` falls back to `"openrouter"` with a `DeprecationWarning`; the implicit default is removed in v1.0.

**Do:** pair `@track_run(agent_type="X")` with `get_agent_config("X")` so intent (router YAML) and actuals (tracking row) use the same slug.

**Do not:** hardcode `model="..."` in service code or read per-agent `*_MODEL` env vars — that's what the router replaces. See [model-router.md](model-router.md).

**Contract:**

- The wrapped function MUST be called with `model=` as a keyword argument.
- It SHOULD accept `messages=` if you want rendered prompts captured (system + user only; assistant/tool messages and multi-part content are skipped).
- Sampling kwargs (`temperature`, `top_p`, `max_tokens`, `seed`, `stop_sequences`) are captured opportunistically.

**On success:** records the run, stamps `_llm_run_id` onto the returned `usage` dict so the caller can attach configs/metrics/tags/outcomes after parsing.

**On failure:** records `status='failed'` with `error`, `error_class`, `http_status` (extracted from `ClientError.context["status_code"]` when available), preserves rendered prompts, then re-raises. The original exception always propagates unchanged.

**Attaching after the call:**

```python
content, usage = tracked_chat(model=..., messages=...)
run_id = usage["_llm_run_id"]

LlmRunValidationRepo().record(run_id, validator="json_schema", passed=True)
LlmRunOutcomeRepo().record(run_id, outcome_kind="summary_accepted", score=1.0)
```

---

## Sidecar writers

Outcomes, validations, and links arrive after the original call (reviewer actions, async checks, retries). Each sub-repo uses pf-core's portable `insert_ignore` / `upsert` helpers (keyed on the table's primary key) so re-recording the same composite key is idempotent across all three dialects — without the secondary-index gap locks a delete-then-insert would take, which deadlock under concurrent writers.

```python
from pf_core.llm.tracking import (
    LlmRunOutcomeRepo, LlmRunValidationRepo, LlmRunLinkRepo,
)

LlmRunOutcomeRepo().record(run_id, outcome_kind="summary_accepted", score=1.0)
LlmRunOutcomeRepo().record(run_id, outcome_kind="summary_edited",
                           score=0.7, notes="trimmed 2 sentences")

LlmRunValidationRepo().record(run_id, validator="post_hoc_check",
                              passed=False, severity="warn",
                              details={"flagged_items": 2})

LlmRunLinkRepo().link(parent_id=run_a, child_id=run_b, relation="critic")

# Reads
LlmRunOutcomeRepo().list_for_run(run_id)
LlmRunValidationRepo().list_for_run(run_id)
LlmRunLinkRepo().children(parent_id=run_a, relation="retry")
LlmRunLinkRepo().parents(child_id=run_b)
```

**Severity values** (`'info' | 'warn' | 'error'`) are conventional but stored as VARCHAR — projects may extend.

---

## Stats and read helpers

```python
from pf_core.llm.tracking import LlmRunRepo, LlmRunStatsRepo

repo = LlmRunRepo()
repo.get(run_id)                  # flat dict from llm_runs
repo.get_with_payload(run_id)     # joined with llm_run_payloads under "payload"
repo.find_by_hash(input_hash)     # all runs sharing this input_hash, newest first

stats = LlmRunStatsRepo()
stats.cost_by_model(since, until)
stats.halluc_rate_by_prompt("summarizer", since, until,
                            validator="url_hallucination")
stats.retry_success_rate(since, until)
stats.runs_with_all_tags(["eval:golden_v2", "experiment:opus47-a"])
```

Stats methods take `[since, until)` half-open ranges as required positional args — no hidden defaults. Pass `date` or `datetime`; dates are promoted to midnight UTC.

Returned rows are plain dicts. `Decimal` values (Postgres `NUMERIC`) are coerced to `float` so the rows round-trip through JSON.

---

## Tags and metrics conventions

**Tags** — colon-namespaced strings, `kind:value`. Conventional prefixes (not enforced): `env:*`, `experiment:*`, `cohort:*`, `eval:*`, `agent:*`. Stick to them so cohort queries stay tractable.

**Metrics** — numeric only. Composite PK is `(llm_run_id, metric_name)`; overwrite by deleting and re-inserting.

## Patterns that use this module

See `docs/recipes/`:
- [batch-llm-service.md](recipes/batch-llm-service.md) — the canonical shape for N-item LLM workers (job + run_parallel + cache + budget + validate + track)
- [job-refs-bridge.md](recipes/job-refs-bridge.md) — bridging pf-core jobs to a project-owned domain entity
- [critic.md](recipes/critic.md) — two-call critic pattern using `llm_run_links.relation="critic"`
- [self-consistency.md](recipes/self-consistency.md) — N-way sampling + majority vote + sibling links

---

## Retention

```python
from pf_core.llm.tracking import purge_old_payloads

purge_old_payloads(older_than_days=90)               # default: keep flagged runs
purge_old_payloads(older_than_days=30, keep_flagged=False)
```

Drops `llm_run_payloads` rows only — analytics columns on `llm_runs` are preserved forever. Returns the count of deleted payload rows.

`keep_flagged=True` (default) preserves payloads attached to:
- Any run whose `status != 'success'`, OR
- Any run with at least one validation row where `passed = false`.

**Do not** call this on a schedule from inside framework code. Wire it up as an admin command or weekly cron in the consumer project.

---

## Schema overview

Eleven tables, all prefixed `llm_`: three reference (`llm_models`, `llm_agent_types`, `llm_prompts`), one main (`llm_runs`), one 1:1 cold sidecar (`llm_run_payloads`), and six composite-PK sidecars (`llm_run_configs`, `llm_run_validations`, `llm_run_outcomes`, `llm_run_metrics`, `llm_run_tags`, `llm_run_links`). All sidecars cascade on parent delete.

**Hot vs cold.** Query `llm_runs` and the normalized sidecars (`tags`, `metrics`, `validations`) freely — they are indexed. JSON columns (`parsed_output`, `validations.details`) are cold-tier; inspect by `llm_run_id` only. **No JSON-path queries in hot paths** — that's what keeps the three dialects identical.

For the column-by-column definitions, read [`pf_core.llm.tracking.schema`](../llm/tracking/schema.py).

**`llm_runs.job_id`** — FK to `jobs.id`, `ON DELETE SET NULL`. Populated automatically by the tracking decorator / `LlmRunRepo.record()` when called inside a `with Job(...)` block. See [jobs.md](jobs.md) for the context-var plumbing and cost-attribution queries.

---

## Query cookbook

All queries run identically on SQLite, MySQL, and Postgres.

```sql
-- Worst (prompt, model) combo for URL hallucination, last 30 days
SELECT p.version, m.name,
       AVG(CASE WHEN v.passed THEN 0 ELSE 1 END) AS halluc_rate,
       COUNT(*)        AS runs,
       SUM(r.cost_usd) AS cost_attributable
FROM llm_runs r
JOIN llm_prompts p ON p.id = r.system_prompt_id
JOIN llm_models  m ON m.id = r.model_id
JOIN llm_run_validations v
  ON v.llm_run_id = r.id AND v.validator = 'url_hallucination'
WHERE r.created_at >= CURRENT_TIMESTAMP - INTERVAL '30' DAY
GROUP BY p.version, m.name
ORDER BY halluc_rate DESC;

-- Cached vs uncached input spend by model, last 7 days
SELECT m.name,
       SUM(r.prompt_tokens - COALESCE(r.cache_read_tokens, 0)) AS billable_input,
       SUM(COALESCE(r.cache_read_tokens, 0))                    AS cached_input,
       SUM(r.completion_tokens)                                 AS output,
       SUM(r.reasoning_tokens)                                  AS reasoning,
       SUM(r.cost_usd)                                          AS total
FROM llm_runs r
JOIN llm_models m ON m.id = r.model_id
WHERE r.created_at >= CURRENT_DATE - INTERVAL '7' DAY
GROUP BY m.name;

-- Self-correction: success rate and combined cost by relation
SELECT l.relation,
       COUNT(*) AS chains,
       AVG(CASE WHEN child.status = 'success' THEN 1.0 ELSE 0.0 END) AS child_success_rate,
       AVG(child.cost_usd + parent.cost_usd) AS avg_combined_cost
FROM llm_run_links l
JOIN llm_runs parent ON parent.id = l.parent_run_id
JOIN llm_runs child  ON child.id  = l.child_run_id
WHERE l.relation IN ('retry', 'critic', 'refine', 'fallback')
GROUP BY l.relation;

-- Tag intersection: runs carrying ALL given tags
SELECT llm_run_id
FROM llm_run_tags
WHERE tag IN ('eval:golden_v2', 'experiment:opus47-a')
GROUP BY llm_run_id
HAVING COUNT(DISTINCT tag) = 2;
```

`LlmRunStatsRepo` exposes the first three of these as Python methods. Hand-written SQL belongs in admin routes, not in services.

---

## Three-dialect support

The schema and query layer target MySQL 8+, PostgreSQL 14+, and SQLite 3.38+; the same queries run on all three.

- **Type variants** are declared in [`pf_core.llm.tracking.schema`](../llm/tracking/schema.py) using SQLAlchemy `with_variant()` — `MEDIUMTEXT`/`TEXT`, `JSON`/`JSONB`/`TEXT`, `DECIMAL`/`NUMERIC`/`REAL`, and `BIGINT`/`SMALLINT` only on MySQL + Postgres (see gotcha below).
- **JSON, NOW(), INSERT IGNORE, ON UPDATE** dialect helpers live in [`pf_core.db.json_compat`](../db/json_compat.py). Use these in custom migrations rather than hand-branching on `dialect.name`.
- **`ON DELETE CASCADE`** on the sidecars only fires on SQLite when `PRAGMA foreign_keys = ON` — already set in `pf_core.db`'s engine.

**Gotcha — SQLite autoincrement requires literal `INTEGER`.** SQLAlchemy's `BigInteger` base compiles to `BIGINT` on SQLite, which silently breaks the rowid alias — first INSERT raises `NOT NULL constraint failed: <table>.id`. Every PK/FK in `pf_core.llm.tracking.schema` uses `Integer()` as the base and declares `BIGINT`/`SMALLINT` only via `with_variant("mysql")` / `with_variant("postgresql")`. Follow the same pattern when adding project-side tables that join against `llm_runs`.

**Gotcha — MySQL `STRICT_TRANS_TABLES` rejects plain `CURRENT_TIMESTAMP` on `TIMESTAMP(6)`.** Use `pf_core.llm.tracking.schema._server_now()` (or the same compile-dispatched pattern) as the server default for µs-precision timestamp columns. Plain `func.now()` will fail at table-create time on strict MySQL.

See [database.md](database.md) for the broader dialect/transaction conventions.

---

## Adding a new project FK column

Project-specific FKs (`task_id`, `item_id`, …) must NOT live in pf-core. ALTER `llm_runs` in a per-project migration after pf-core's tables are created:

```python
op.add_column("llm_runs",
    sa.Column("task_id", sa.Integer,
              sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True))
op.create_index("idx_llm_runs_task", "llm_runs", ["task_id"])
```

Then teach the SQLAlchemy `llm_runs` Table about the column once at import time, and write it via `extra_run_values` on each call — no subclass, no `record()` copy:

```python
from sqlalchemy import Column, Integer
from pf_core.llm.tracking import schema as s

# At import time (idempotent against re-import):
if "task_id" not in s.llm_runs.c:
    s.llm_runs.append_column(Column("task_id", Integer, nullable=True))

# Per call — written inside the same INSERT as the rest of the run:
LlmRunRepo().record(
    agent_type="summarizer", model="...",
    extra_run_values={"task_id": task_id},
)
```

This keeps the write atomic (no follow-up UPDATE). Without `extra_run_values`, the only option would be to subclass `LlmRunRepo` and copy the entire `record()` body to add one column — a standing sync hazard this avoids.

---

## Adding a new validator or metric

No schema change needed. Just record it.

```python
LlmRunValidationRepo().record(run_id,
    validator="schema_v3", passed=False, severity="warn",
    details={"missing_fields": ["category"]})

LlmRunRepo().record(..., metrics={"toxicity": 0.02, "coverage_ratio": 0.83})
```

Stick to lowercase snake_case names. Reuse existing validator names across projects when the semantics match — that's what makes cross-project halluc/quality dashboards possible.

---

## Adding a new agent type

Pass any new slug to `record()` (or to the `@track_run(agent_type=...)` decorator) — `resolve_agent_type_id()` will INSERT IGNORE + SELECT the row and cache the id for the lifetime of the process.

```python
LlmRunRepo().record(agent_type="critic_v2", model="...", ...)
```

To pre-seed slugs (e.g. for an admin UI dropdown) write them in your project's data-migration step. Keep `description` short and operator-facing.
