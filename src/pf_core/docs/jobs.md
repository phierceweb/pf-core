# Jobs

Generic job table for batches, multi-step workflows, and long-running operations — one row per user-triggered "thing" (summarize a text, classify a batch, import a CSV), with state-machine enforcement, step history, worker-claim semantics, and automatic attribution of LLM calls to the active job.

This sits one layer above [`llm_runs`](llm-tracking.md). A `jobs` row owns zero or more `llm_runs` rows; deleting a job sets `llm_runs.job_id` to NULL (historical cost data survives). Unlike `llm_runs`, jobs are orthogonal to LLM usage — a `csv_import` job may never call a model.

This doc is the implementation reference.

---

## Table of Contents

- [Concepts](#concepts)
- [Quick start](#quick-start)
- [Registering a job kind](#registering-a-job-kind)
- [The `Job` context manager](#the-job-context-manager)
- [Steps](#steps)
- [Events](#events)
- [LLM attribution](#llm-attribution)
- [Worker claim](#worker-claim)
- [Retry, cancel, purge](#retry-cancel-purge)
- [Schema overview](#schema-overview)
- [The `pf-jobs` CLI](#the-pf-jobs-cli)
- [Three-dialect support](#three-dialect-support)
- [Datetime handling](#datetime-handling)
- [Adding a new job kind](#adding-a-new-job-kind)

---

## Concepts

- **job** — one row in `jobs` = one user-triggered unit of work. Has a `kind`, declared state machine, validated inputs/outputs (JSON).
- **kind** — the discriminator (`'summarize_pass'`, `'classify_batch'`). Registered at import time, declares states, transitions, and optional Pydantic schemas.
- **step** — one row in `job_steps`. The ordered narrative: `'load_inputs'`, `'summarize_item_3'`. Idempotent: re-entering a step whose prior run succeeded short-circuits.
- **event** — one row in `job_events`. Cheaper than steps; used for retries, rate-limit warnings, budget events. Rendered as a timeline in admin UIs.
- **active job** — the job set on a ContextVar by `Job.__enter__`. The LLM tracking decorator reads this and populates `llm_runs.job_id` without services needing to know a job is active.

**Do:** make a job when a thing takes more than ~10 seconds or spans more than ~3 LLM calls.

**Do not:** wrap a one-off classification call in a `Job` — direct `LlmRunRepo.record()` is sufficient.

---

## Quick start

```python
from pf_core.jobs import JobRepo, Job, register_kind
from pydantic import BaseModel

class SummarizeInputs(BaseModel):
    item_ids: list[int]
    max_words: int

class SummarizeOutputs(BaseModel):
    n_summarized: int

register_kind(
    kind="summarize_pass",
    inputs_schema=SummarizeInputs,
    outputs_schema=SummarizeOutputs,
)

job_id = JobRepo().create(
    kind="summarize_pass",
    inputs={"item_ids": [1, 2, 3], "max_words": 200},
    created_by="cli:summarize",
)

with Job(job_id) as job:
    job.transition("running")
    job.progress(total=len(job.inputs["item_ids"]))

    for i, item_id in enumerate(job.inputs["item_ids"]):
        with job.step(f"summarize_{item_id}") as step:
            # Any LLM call inside this block auto-attributes to the job.
            step.outputs = {"item_id": item_id, "word_count": 180}
            job.progress(current=i + 1, step=f"summarized {item_id}")

    job.outputs = {"n_summarized": len(job.inputs["item_ids"])}
    job.transition("succeeded")
```

On an unhandled exception inside the block, the job transitions to `failed` with `error` and `error_class` populated, and a `job_events` row of type `'exception'` is written. The exception re-raises.

---

## Registering a job kind

Registration runs at import time. Define schemas and call `register_kind()` once per kind.

```python
from pf_core.jobs import register_kind, DEFAULT_STATES, DEFAULT_TRANSITIONS

register_kind(
    kind="summarize_pass",
    description="Summarize one item through a multi-stage pipeline",
    states=["pending", "fetching", "summarizing", "checking", "succeeded", "failed", "canceled"],
    transitions={
        "pending":     ["fetching", "canceled"],
        "fetching":    ["summarizing", "failed", "canceled"],
        "summarizing": ["checking", "failed", "canceled"],
        "checking":    ["succeeded", "failed"],
        "failed":      ["pending"],      # manual retry
    },
    inputs_schema=SummarizeInputs,
    outputs_schema=SummarizeOutputs,
    default_priority=60,
)
```

**Rules enforced:**
- `kind` must be non-empty.
- `0 <= default_priority <= 100`.
- Every source state in `transitions` must appear in `states`.
- Every target state must appear in `states`.
- Re-registering the same `kind` with a different signature raises `ConfigurationError`.
- Re-registering with the same signature is a no-op.

**Defaults:** If `states` and `transitions` are omitted, `DEFAULT_STATES` and `DEFAULT_TRANSITIONS` are used — they model the `pending → running → {succeeded, failed, canceled, partial}` flow. Omit `inputs_schema` to skip input validation.

**Do:** put registration calls in a module imported at app startup (e.g. `app/jobs/__init__.py`). The registry is process-local — call `clear_registry()` in test fixtures.

### Auto-tracking progress

Pass `auto_track_progress=True` to `register_kind` and pf-core will atomically increment `jobs.progress_current` by 1 every time a step transitions to `succeeded` or `failed` (skipped steps don't count — they represent resumed work that was already tallied). The increment happens in the same transaction as `finish_step`, so concurrent workers each contribute +1 without lost updates.

```python
register_kind(
    kind="summarize_pass",
    inputs_schema=SummarizePassInputs,
    auto_track_progress=True,
)
```

Without this flag (the default), callers must wire progress themselves via `job.progress(current=done, total=total)` from a per-item callback. Explicit `set_progress(current=N)` always wins over the auto-incremented value (last-write-wins), so callers who compute progress differently can still override.

---

## The `Job` context manager

`Job(job_id)` opens a DB-backed handle and sets the active-job ContextVar. State transitions go through the repo (which enforces the registered rules).

```python
from pf_core.jobs import Job

with Job(job_id) as job:
    job.transition("running")
    job.progress(total=100)
    job.progress(current=50, step="halfway")
    job.event("info", "checkpoint", context={"k": 1})
    job.outputs = {"result": "ok"}
    job.transition("succeeded")
```

**Attributes (post-`__enter__`):** `job.id`, `job.kind`, `job.status`, `job.inputs`.

**Deferred outputs:** Setting `job.outputs = {...}` stashes the value. `transition("succeeded")` and `transition("partial")` both pick it up automatically — no need to pass `outputs=...` explicitly. On clean exit with no terminal transition, the stashed outputs are written if the DB column is still NULL.

**Force-fail on exception:** `__exit__` bypasses the transition registry and writes `status='failed'` regardless of current state, because the service may have crashed before marking itself `running`. If the job is already terminal (e.g. the crash happened after `succeeded`), the terminal status is preserved but an `exception` event is still written for forensics.

**Nested jobs:** The ContextVar stacks correctly — inner `Job(...)` overrides, then restores on exit. Rarely needed outside parent/child batch patterns.

---

## Steps

`job.step(name)` is a context manager around one row of `job_steps`. Steps are **idempotent**:

```python
with job.step("summarize_1") as step:
    if step.skipped:
        # Prior run of this step succeeded — a resume. The body still
        # runs but can short-circuit expensive work.
        return
    step.outputs = {"word_count": 180}
```

**Idempotency rule:** If `job_steps` already has a row for `(job_id, name)` with `status='succeeded'`, the new step is not inserted and `skipped=True` is yielded. Any other prior status (including `failed`) causes a new row to be inserted.

**Completion states:**
- Clean exit with no `step.error` set → `'succeeded'`, `outputs` persisted.
- `step.error = "..."` before exit → `'failed'`, no exception raised.
- Exception inside the block → `'failed'` with the exception message, then re-raised (causing the parent `Job` to also force-fail).

**`duration_ms`** is computed automatically from `started_at` / `finished_at`.

**`step_index`** auto-increments via `SELECT COALESCE(MAX(step_index), -1) + 1` within the step's transaction — callers never coordinate indices.

---

## Events

Free-form diagnostic rows. Cheaper than steps; use for things that aren't a unit of work:

```python
job.event("retry", "backoff 2s", context={"attempt": 2})
job.event("budget_exceeded", "monthly cap hit", context={"cap_usd": 50})
```

Read them back:

```python
all_events = JobRepo().get_events(job_id)
retries = JobRepo().get_events(job_id, event_type="retry")
```

Events complement structured logging — they persist in the DB so an admin UI can reconstruct the timeline long after the log file rotated away.

---

## LLM attribution

Any call to `LlmRunRepo.record()` (or the `@track_run` decorator) made inside a `Job` block has `llm_runs.job_id` set to the active job id automatically — services stay ignorant of jobs.

```python
from pf_core.jobs import Job
from pf_core.llm.tracking import LlmRunRepo

with Job(job_id):
    run_id = LlmRunRepo().record(agent_type="summarizer", model="gpt-4o-mini")

# Afterwards:
LlmRunRepo().get(run_id)["job_id"] == job_id
```

**Explicit override:** Passing `job_id=...` to `record()` takes precedence over the ContextVar.

**Outside a job:** `job_id` is NULL. One-off CLI debug calls land unattributed, which is fine.

**Cost attribution query:**

```sql
SELECT j.kind, SUM(r.cost_usd) AS cost, COUNT(r.id) AS n_runs
FROM jobs j
LEFT JOIN llm_runs r ON r.job_id = j.id
WHERE j.created_at >= CURRENT_DATE - INTERVAL '7' DAY
GROUP BY j.kind
ORDER BY cost DESC;
```

---

## Worker claim

`claim_next()` supports distributed-worker setups (one DB, many processes pulling).

```python
from pf_core.jobs import JobRepo

claimed = JobRepo().claim_next(
    kinds=["summarize_pass", "classify_batch"],   # optional filter
    worker_id="worker-3",
    lease_seconds=300,
)

if claimed is not None:
    with Job(claimed["id"]) as job:
        # ... run it ...
```

**Ordering:** `status='pending' AND claimed_by IS NULL`, ordered by `priority DESC, created_at ASC`.

**Atomicity:** Implemented as `SELECT ... LIMIT 1 → UPDATE ... WHERE id=? AND claimed_by IS NULL`. If the UPDATE's rowcount is 0 (another worker won the race), the claim returns `None`. Portable across SQLite (no `FOR UPDATE SKIP LOCKED`), MySQL, and Postgres.

**Lease expiry:**

```python
n_reclaimed = JobRepo().reclaim_stale(lease_seconds=300)
```

Finds jobs still marked `running` whose `claimed_at` is older than the lease and resets them to `pending`. Run via `pf-jobs reclaim` or as a cron every minute.

**Default lease** comes from the `JOB_LEASE_SECONDS` env var (default 300). Override per-call with `lease_seconds=...`.

**Single-process orchestration** (CLI, cron) can ignore claims entirely and call `Job(job_id)` directly.

---

## Retry, cancel, purge

```python
JobRepo().retry(job_id)       # failed/partial/canceled → pending; priority += 10 (capped at 100)
JobRepo().cancel(job_id, reason="user aborted")  # → canceled + writes event
JobRepo().purge(older_than=timedelta(days=90), status="succeeded")
```

**Retry rules:** Allowed from `failed`, `partial`, or `canceled`. Clears `error`, `error_class`, `finished_at`, and claim fields. Preserves `job_steps` history — the idempotency rule causes the resumed run to skip already-succeeded steps.

**Purge rules:** Deletes jobs with `finished_at < now - older_than` matching the status filter. `job_steps` and `job_events` cascade via FK. `llm_runs.job_id` is set to NULL (not cascaded) — cost attribution survives.

---

## Schema overview

Three tables, all framework-owned (no project FKs). Column types are dialect-portable via the shared helpers in `pf_core.llm.tracking.schema`.

**`jobs`** — header row. `kind`, `status`, `priority`, `progress_current/total`, `current_step`, `inputs/outputs` JSON, `error/error_class`, lease fields (`claimed_by/at`), lifecycle timestamps (`started_at`, `finished_at`).

**`job_steps`** — ordered per-job log. `(job_id, step_index)` UNIQUE, `status` ∈ `{running, succeeded, failed, skipped}`, `duration_ms` computed at finish.

**`job_events`** — free-form diagnostic timeline. `event_type`, `message`, `context` JSON, indexed by `(job_id, created_at)` and `(event_type, created_at)`.

**`llm_runs.job_id`** — FK on `llm_runs`, `ON DELETE SET NULL`. Populated automatically by the tracking decorator when inside a `Job`.

All three tables live on the shared `pf_core.llm.tracking.metadata` — `metadata.create_all(engine)` emits jobs DDL alongside LLM tracking.

---

## The `pf-jobs` CLI

Admin CLI shipped in `bin/pf-jobs`. Reads from whatever DB the consumer project configures.

```
pf-jobs list [--kind X] [--status Y] [--since 24h] [--limit 20]
pf-jobs show <id>
pf-jobs retry <id>
pf-jobs cancel <id> [--reason "..."]
pf-jobs reclaim [--lease-seconds 300]
pf-jobs purge --older-than 90d [--status succeeded|any] [--yes]
```

### Mounting inside your own CLI

The same commands ship as a mountable Typer sub-app so consumer projects can expose them under their own namespace instead of asking operators to run a second binary:

```python
# my_project/app/cli/__init__.py
from pf_core.cli import create_cli, run_cli
from pf_core.cli.jobs import app as jobs_app

app = create_cli("myapp", help="My application CLI.")
app.add_typer(jobs_app, name="jobs")

def main() -> None:
    run_cli(app)
```

Now `myapp jobs list`, `myapp jobs retry 42`, `myapp jobs purge --older-than 90d --yes` behave identically to `pf-jobs <cmd>`. `bin/pf-jobs` is itself a thin shim over the same sub-app, so both entry points stay in sync forever — re-implementing these commands per project is not necessary.

**Safe by default:** `retry`, `cancel`, and `purge` require explicit arguments. `purge` prompts for confirmation unless `--yes` is passed.

**Duration syntax:** `30s`, `10m`, `24h`, `90d`, `2w`.

---

## Three-dialect support

Tested on SQLite, MySQL, and Postgres. Portability constraints:

- All timestamp columns use the `_TIMESTAMP_US` variant helper (microsecond precision on MySQL, fractional seconds on SQLite/PG).
- `priority` and `progress_*` columns use dialect-variant unsigned-integer helpers.
- No raw SQL dialect detection in `JobRepo` — every write uses SQLAlchemy expression constructs.
- Worker claim uses portable SELECT-then-UPDATE with rowcount check; no `FOR UPDATE SKIP LOCKED` dependency.

---

## Datetime handling

Every datetime column on `jobs`, `job_steps`, and `job_events` is UTC. `JobRepo` enforces this at both boundaries so callers never have to know the storage contract:

**Inputs** — methods that accept a datetime (`find(since=...)`) take either aware or naive values:

- Aware datetimes are converted to UTC before binding.
- Naive datetimes are assumed to already be UTC (the v0.7.0 contract).

**Outputs** — every datetime column returned from `get`, `get_with_steps`, `find`, `descendants`, `find_step`, `get_events`, and `claim_next` is stamped with `tzinfo=timezone.utc`. You can compare against `datetime.now(timezone.utc)` directly, no `.replace(tzinfo=...)` wrapping needed.

**Cutoffs** — worker-lease and retention thresholds (`claim_next`, `reclaim_stale`, `purge`) are computed server-side via `CURRENT_TIMESTAMP - INTERVAL N SECOND` so the comparison never crosses time-zone frames. Python-side `datetime.now(timezone.utc) - timedelta(...)` cutoffs silently skew by the session offset on MySQL; the helper `_server_now_minus_seconds` in `pf_core.llm.tracking.schema` keeps the subtraction in the DB's own clock.

**MySQL** — `pf_core.db.connection` pins every MySQL session to `SET time_zone = '+00:00'` on connect so naive `TIMESTAMP` reads come back as UTC (matching SQLite). If you manage the engine yourself, replicate this setting or the aware-UTC contract breaks on non-UTC servers.

---

## Adding a new job kind

1. Define a Pydantic model for `inputs` (and `outputs` if the kind produces structured results).
2. Call `register_kind(kind=..., inputs_schema=..., outputs_schema=..., states=..., transitions=...)` in a module imported at app startup. Omit `states` / `transitions` to use the defaults.
3. Create the job via `JobRepo().create(kind=..., inputs=...)`.
4. Wrap the worker body in `with Job(job_id) as job:` and `with job.step(name):`.
5. Transition the job through the registered states; the repo rejects invalid moves with `PreconditionError`.
6. If the kind will be picked up by distributed workers, ensure the process calls `JobRepo().claim_next(kinds=[...])` in its loop and runs `pf-jobs reclaim` (or `JobRepo().reclaim_stale()`) on a cadence.
