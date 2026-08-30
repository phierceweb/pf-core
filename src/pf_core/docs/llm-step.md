# LLM step

One call composing the per-item hot path of a batch LLM pass: input-hash → cache lookup → budget gate → tracked call → parse/validate → cache store. Not to be confused with a job step (`job.step(...)` from [jobs](jobs.md)) — `llm_step` is the *LLM part* that typically runs inside one; it opens no Job and persists nothing but the tracking/cache tables.

---

## Table of Contents

- [Quick usage](#quick-usage)
- [Semantics](#semantics)
- [Truncation](#truncation)
- [Dry run](#dry-run)
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
    budget=BudgetEstimate(job_id=job_id, job_kind="report_pass"),
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
- **Budget** (`budget=BudgetEstimate(...)`): `project_cost` with the estimate's tokens → `check_budget` with its `job_id`/`job_kind` and the call's `tags` (so tag-scoped budgets gate tagged runs), plus `projected_tokens` (prompt + completion estimates) and `projected_calls=1` — so token/call-limited budgets gate subscription backends whose projected USD is always $0. A block records the blocked run (`status='budget_blocked'`) and **raises** `CostBudgetExceeded` — catch it per item to skip/collect, exactly as you would around `check_budget` itself.
- **Call**: `tracked_messages_call` — one `llm_runs` row, failed rows on client errors (then re-raise), prompt registration via `spec=`, ambient-Job attribution. The computed (or given) `input_hash` is stamped on the row.
- **Validate** (`validate="object" | "array" | "any"`): `parse_and_validate`; a failing result **returns** with `validation.ok False` and `value=None` — never raises. Validation is a per-item data outcome, not an exception. The call's `usage["finish_reason"]` rides along as the [`truncated=` flag](llm-parse.md#detecting-truncation-truncated-not-the-text) — see [Truncation](#truncation).
- **Store**: only when `cache=True`, the call recorded a run, validation passed (or was skipped), **and the provider did not report truncation** — the call's `usage` dict is forwarded to [`cache_store`](llm-cache.md#cache_store), which is the single enforcement point for the truncation refusal. Raw is always stored; `parsed_output` only when the validated value is a plain dict/list (Pydantic instances are not coerced).

## Truncation

A cache hit replays stored text and carries no finish reason, so a truncated response written to the cache would lose its flag permanently: every later call would return `cache_hit=True` with a silently incomplete value. `llm_step` therefore **never stores a known-truncated response** — with or without `validate`, and regardless of `on_truncation`. The next call re-runs it. (The refusal is enforced inside `cache_store` itself, which receives the call's `usage` dict — direct `cache_store` callers get the same guard by passing `usage=`.)

`on_truncation` decides what the truncation means for the value you get back (values are `"warn"`/`"fail"` — not `tracked_call`'s `"warn"`/`"raise"`, because `llm_step` reports failure through the validation result instead of raising):

| Value | Effect |
|---|---|
| `"warn"` (default) | The pipeline runs as usual; `parse_llm_json` logs a WARNING and whatever parsed is returned. |
| `"fail"` | `validation.ok` is `False` with a single `<agent_type>_truncated` error signal and `value=None` — persisted to `llm_run_validations` like any other signal. |

`"fail"` needs `validate` set: with `validate=None` nothing inspects the response, so there is no `ValidationResult` to fail. A client that reports no finish reason is unaffected by either value — unknown is not truncated.

## Dry run

`llm_step(dry_run=True, ...)` answers **"would this call hit the exact cache?"** without spending anything. It resolves the input hash (computing it when `input_hash=` is not given, regardless of the `cache` flag), looks up the exact cache, and returns:

```python
res = llm_step(client=client, agent_type="classifier", messages=messages, model=model, dry_run=True)
if res.cache_hit:
    ...  # res.value is the stored parsed_output, res.content the stored raw
```

- **On a hit**: `value` is the entry's stored `parsed_output`, `content` its raw response, `cache_hit=True`.
- **On a miss**: `value=None`, `content=""`, `cache_hit=False`.
- **Always**: `run_id=None`, `validation=None`, and **no side effects** — no `cache_hit` run row, no hit-count bump, no budget check, no client call, no DB writes of any kind. (A live hit, by contrast, records a run row and bumps the entry's counters.)

For the per-agent aggregate form of the same question — "has this agent *ever* hit?" — see [`cache_stats`](llm-cache.md#cache_stats--the-key-volatility-diagnostic).

## What stays in the caller

Deliberately not part of this function: the Job/steps/progress shell, `run_parallel` fan-out and failure collection, persistence of `value` (locks, repos), retry policy, and batch splitting. See the [batch LLM service recipe](recipes/batch-llm-service.md) for the full shape around this call.

## Relationship to other modules

- [Tracked LLM call](llm-tracked.md) — the recording core this composes; use it directly when you need none of the legs.
- [LLM cache](llm-cache.md), [Cost & budget](cost-budget.md), [LLM schema validation](llm-schema-validation.md) — the legs, each usable standalone; `llm_step` adds only their ordering and short-circuits.
