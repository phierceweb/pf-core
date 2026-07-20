# LLM step

One call composing the per-item hot path of a batch LLM pass: input-hash → cache lookup → budget gate → tracked call → parse/validate → cache store. Not to be confused with a job step (`job.step(...)` from [jobs](jobs.md)) — `llm_step` is the *LLM part* that typically runs inside one; it opens no Job and persists nothing but the tracking/cache tables.

---

## Table of Contents

- [Quick usage](#quick-usage)
- [Semantics](#semantics)
- [What stays in the caller](#what-stays-in-the-caller)
- [Relationship to other modules](#relationship-to-other-modules)

## Quick usage

```python
from pf_core.llm.step import BudgetEstimate, llm_step

result = llm_step(
    client=client,
    agent_type="classifier",
    messages=messages,
    model=cfg.pop("model"),
    sampling=cfg,
    spec=spec,                     # prompt registration, as in tracked_messages_call
    provider="openrouter",
    cache=True,                    # lookup before, store after (only on valid)
    budget=BudgetEstimate(job_id=job_id, job_kind="grading_pass"),
    validate="object",             # parse_and_validate expect=
)
if result.validation and not result.validation.ok:
    ...record the item as failed; the batch continues
else:
    persist(item, result.value, result.run_id)     # persistence is yours
```

`StepResult` unpacks as `(value, content, run_id, cache_hit, validation)`. Every `tracked_messages_call` kwarg passes through under the same name.

## Semantics

- **Cache hit** (`cache=True`): records a `cache_hit` run row and returns without calling the client — and without consulting the budget. With `validate` set, the stored **raw** response is re-parsed and re-validated, so a validator change re-judges old cache entries instead of trusting stored output; with `validate=None`, the stored `parsed_output` (falling back to raw) is returned as `value`.
- **Budget** (`budget=BudgetEstimate(...)`): `project_cost` with the estimate's tokens → `check_budget` with its `job_id`/`job_kind`. A block records the blocked run (`status='budget_blocked'`) and **raises** `CostBudgetExceeded` — catch it per item to skip/collect, exactly as you would around `check_budget` itself.
- **Call**: `tracked_messages_call` — one `llm_runs` row, failed rows on client errors (then re-raise), prompt registration via `spec=`, ambient-Job attribution. The computed (or given) `input_hash` is stamped on the row.
- **Validate** (`validate="object" | "array" | "any"`): `parse_and_validate`; a failing result **returns** with `validation.ok False` and `value=None` — never raises. Validation is a per-item data outcome, not an exception.
- **Store**: only when `cache=True`, the call recorded a run, and validation passed (or was skipped). Raw is always stored; `parsed_output` only when the validated value is a plain dict/list (Pydantic instances are not coerced).

## What stays in the caller

Deliberately not part of this function: the Job/steps/progress shell, `run_parallel` fan-out and failure collection, persistence of `value` (locks, repos), retry policy, and batch splitting. See the [batch LLM service recipe](recipes/batch-llm-service.md) for the full shape around this call.

## Relationship to other modules

- [Tracked LLM call](llm-tracked.md) — the recording core this composes; use it directly when you need none of the legs.
- [LLM cache](llm-cache.md), [Cost & budget](cost-budget.md), [LLM schema validation](llm-schema-validation.md) — the legs, each usable standalone; `llm_step` adds only their ordering and short-circuits.
