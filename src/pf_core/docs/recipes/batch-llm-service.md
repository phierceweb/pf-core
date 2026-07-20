# Recipe: Batch LLM service

Shape for services that process N items through an LLM with caching, budget enforcement, validation, tracking, and job lifecycle — the scaffolding pattern used by summarizer, classifier, and extractor services.

## When to use

- A batch or parallel operation where each item results in one LLM call
- Items are independent (can run in parallel)
- You want cost guardrails, cache reuse, per-call tracking, and job-level progress — all of which pf-core provides as separate building blocks

## When NOT to use

- Single interactive call (no job wrapper needed — call the client directly via `tracked_messages_call`)
- Streaming responses (this pattern assumes a full response arrives per call)
- Multi-turn agent loops where items depend on prior results

## The pattern

The per-item hot path — hash → cache → budget → tracked call → validate → store — is one framework call, [`llm_step`](../llm-step.md). The batch shell around it stays explicit in your service.

```python
from pf_core.jobs.runtime import Job
from pf_core.llm.router import get_agent_config
from pf_core.llm.step import BudgetEstimate, llm_step
from pf_core.output import NullReporter, Reporter
from pf_core.parallel import resilient, run_parallel
from pf_core.clients.openrouter import get_client

AGENT = "summarizer"  # or "classifier", "extractor", ...


def _process_one(item: dict, *, cfg: dict, job, job_id: int) -> None:
    """Single-item worker — the hot path. Called by run_parallel."""
    with job.step(f"{AGENT}_{item['id']}") as step:
        if step.skipped:
            return
        messages = _build_messages(item)
        result = llm_step(
            client=get_client(),
            agent_type=AGENT,
            messages=messages,
            model=cfg["model"],
            sampling={k: v for k, v in cfg.items() if k != "model"},
            provider="openrouter",
            cache=True,
            budget=BudgetEstimate(job_id=job_id, job_kind=f"{AGENT}_batch"),
            validate="object",
        )
        if result.validation and not result.validation.ok:
            step.error = "validation failed"
            return
        _persist(item, result.value, run_id=result.run_id)   # persistence is yours
        step.outputs = {"cache_hit": result.cache_hit}


def run_batch(
    items: list[dict],
    *,
    job_id: int,
    workers: int = 4,
    reporter: Reporter | None = None,
) -> None:
    """Entry point. The Job wrapper gives every tracked call job_id attribution."""
    cfg = get_agent_config(AGENT)
    reporter = reporter or NullReporter()
    failures: list[tuple[str, str]] = []

    with Job(job_id) as job:
        if job.status == "pending":
            job.transition("running")
        job.progress(total=len(items))

        @resilient(failures, label_fn=lambda i: str(i["id"]), reporter=reporter)
        def _one(item: dict) -> None:
            _process_one(item, cfg=cfg, job=job, job_id=job_id)

        run_parallel(items, _one, workers, f"{AGENT} items", None, failures=failures)

        job.outputs = {"n_done": len(items) - len(failures), "n_failed": len(failures)}
        job.transition("succeeded")
```

## What pf-core gives you automatically

- `llm_step` orders the legs and short-circuits correctly: a cache hit records a `cache_hit` run and skips both the client call and the budget; a budget block records the blocked run and raises `CostBudgetExceeded`; a client error records a failed run and re-raises; a validation failure returns as data
- `Job` context manager populates `llm_runs.job_id` via contextvar, so every call inside the `with Job(...)` block is attributed
- `cache_hit` status excluded from budget aggregation → no double-counting
- `parse_and_validate` emits `llm_run_validations` rows for dashboards
- `@resilient` + `run_parallel(..., failures=)` isolate per-item failures so the batch continues

## Variations

**Per-item fallback.** Catch `CostBudgetExceeded` around `llm_step`, retry with `get_agent_config(f"{AGENT}_cheap")`. Link the retry via `llm_run_links.relation="retry_after_budget_block"`.

**Skip on validation fail.** Shown above — `result.validation.ok` is data, not an exception; write an `llm_run_outcomes` row with `outcome_kind="skipped_invalid"` if you want it queryable.

**No cache / no validation.** Drop the kwarg — each leg is independent (`cache=False` passes, `validate=None` returns raw content as `result.value`).

## Don't abstract the batch shell

The hot path is a named call now; the shell deliberately is not. Job planning, step naming, item shapes, failure collection, and persistence differ per service — a `BatchLlmService` base class would hide exactly the parts you need to vary. Keep the shell explicit; when a new pf-core primitive lands, services update mechanically.
