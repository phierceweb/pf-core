# Recipe: Batch LLM service

Shape for services that process N items through an LLM with caching, budget enforcement, validation, tracking, and job lifecycle — the scaffolding pattern used by summarizer, classifier, and extractor services.

## When to use

- A batch or parallel operation where each item results in one LLM call
- Items are independent (can run in parallel)
- You want cost guardrails, cache reuse, per-call tracking, and job-level progress — all of which pf-core provides as separate building blocks

## When NOT to use

- Single interactive call (no job wrapper needed — call the client directly via `@track_run`)
- Streaming responses (this pattern assumes a full response arrives per call)
- Multi-turn agent loops where items depend on prior results

## The pattern

```python
from pf_core.jobs.runtime import Job
from pf_core.jobs.repo import JobRepo
from pf_core.parallel import run_parallel
from pf_core.output import Reporter, NullReporter
from pf_core.llm import get_agent_config, parse_and_validate
from pf_core.llm.tracking import compute_input_hash, track_run
from pf_core.llm.cache import cache_lookup, cache_store, record_cache_hit
from pf_core.budget import check_budget, project_cost, CostBudgetExceeded, record_blocked_run
from pf_core.clients.openrouter import get_client

AGENT = "summarizer"  # or "classifier", "extractor", ...


@track_run(agent_type=AGENT, provider="openrouter")
def _tracked_chat(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)


def _process_one(item: dict, *, cfg: dict) -> dict:
    """Single-item worker — the hot path. Called by run_parallel."""
    messages = _build_messages(item)
    input_hash = compute_input_hash(
        model=cfg["model"], messages=messages, sampling=cfg
    )

    # 1. Cache check — zero-cost return if we've seen this input
    hit = cache_lookup(agent_type=AGENT, input_hash=input_hash)
    if hit is not None:
        record_cache_hit(hit=hit)
        return _finalize(item, hit.parsed_output)

    # 2. Budget guard — raises CostBudgetExceeded on block
    projected = project_cost(
        agent_type=AGENT,
        model=cfg["model"],
        estimated_prompt_tokens=1500,
        estimated_completion_tokens=800,
    )
    try:
        check_budget(agent_type=AGENT, projected_cost_usd=projected)
    except CostBudgetExceeded as exc:
        record_blocked_run(agent_type=AGENT, model=cfg["model"], exc=exc)
        raise

    # 3. LLM call — tracked, cost + tokens recorded automatically
    content, usage = _tracked_chat(messages=messages, **cfg)
    run_id = usage["_llm_run_id"]

    # 4. Parse + validate — signals written to llm_run_validations.
    # parse_and_validate returns a ValidationResult: `.ok` is the pass/fail
    # flag, `.value` is the parsed object (a dict, or a Pydantic instance
    # when the shape validator is a PydanticValidator).
    result = parse_and_validate(content, agent_type=AGENT, run_id=run_id)
    parsed = result.value

    # 5. Cache store — next identical call returns early
    cache_store(
        agent_type=AGENT,
        input_hash=input_hash,
        source_run_id=run_id,
        model=cfg["model"],
        parsed_output=parsed,
        raw_response=content,
    )

    return _finalize(item, parsed)


def run_batch(
    items: list[dict],
    *,
    job_id: int,
    workers: int = 4,
    reporter: Reporter | None = None,
) -> None:
    """Entry point. Wrap the run_parallel worker in a Job so every
    tracked LLM call gets job_id attribution."""
    cfg = get_agent_config(AGENT)
    reporter = reporter or NullReporter()

    with Job(job_id) as job:
        job.event("batch_started", f"{len(items)} items")

        run_parallel(
            items=items,
            fn=lambda item: _process_one(item, cfg=cfg),
            workers=workers,
            label=f"{AGENT} items processed",
            reporter=reporter,
        )

        job.event("batch_complete", f"{len(items)} items")
```

## What pf-core gives you automatically

- `@track_run` writes an `llm_runs` row per call with tokens, cost, duration, status, error
- `Job` context manager populates `llm_runs.job_id` via contextvar, so every call inside the `with Job(...)` block is attributed
- `cache_hit` status excluded from budget aggregation → no double-counting
- `CostBudgetExceeded` can be caught and handled per item (swap to a cheaper agent, skip, requeue) — service decides
- `record_blocked_run` writes a zero-cost `llm_runs` row tagged `budget:blocked` so the admin surfaces it
- `parse_and_validate` emits `llm_run_validations` rows for dashboards

## Variations

**Per-item fallback.** Catch `CostBudgetExceeded` inside `_process_one`, call `get_agent_config(f"{AGENT}_cheap")`, retry with the cheaper config. Link the retry via `llm_run_links.relation="retry_after_budget_block"`.

**Skip on validation fail.** Check the `parse_and_validate` result's `ok` attribute; if false, write an `llm_run_outcomes` row with `outcome_kind="skipped_invalid"` and return early.

**Per-step granularity.** Wrap sub-operations in `with job.step("step_name"):` — `job_steps` rows power resumable workers.

## Don't abstract this into a base class

Three services following this shape explicitly is clearer than a `BatchLlmService` scaffold. The pieces — cache, budget, track, validate — are named and visible. A base class hides the integration and makes it harder to vary any single step.

If the shape changes (e.g. new pf-core primitive lands), all three services update mechanically; this is easier than evolving a scaffold's API.
