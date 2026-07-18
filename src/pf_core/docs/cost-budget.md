# Cost Budget

`pf_core.budget` — pre-call cost guardrails for LLM spending. Answers "would this call push us over the daily/monthly cap?" *before* the request leaves the process.

Budgets bound the runaway-cost failure mode: a broken or looping prompt that would otherwise burn unbounded spend before anyone notices.

## Quick start

```python
from pf_core.budget import check_budget, project_cost, CostBudgetExceeded, record_blocked_run
from pf_core.llm import get_agent_config
from pf_core.llm.tracking import track_run
from pf_core.clients.openrouter import get_client

@track_run(agent_type="summarizer")
def _tracked_chat(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)

def summarize_item(*, item_id: int, job_id: int | None = None) -> dict:
    cfg = get_agent_config("summarizer")

    projected = project_cost(
        agent_type="summarizer",
        model=cfg["model"],
        estimated_prompt_tokens=1500,
        estimated_completion_tokens=1000,
    )
    try:
        check_budget(
            agent_type="summarizer",
            projected_cost_usd=projected,
            job_id=job_id,
            tags=["experiment:opus47"],
        )
    except CostBudgetExceeded as exc:
        record_blocked_run(agent_type="summarizer", model=cfg["model"], exc=exc, job_id=job_id)
        raise  # or fall back to a cheaper agent

    content, usage = _tracked_chat(messages=[...], **cfg)
    ...
```

## Schema

Three tables register on the shared tracking metadata:

- **`llm_budgets`** — one row per `(scope_kind, scope_value, period)`. `scope_kind ∈ {global, agent, job_kind, job_id, tag}`. `period ∈ {daily, monthly}`. `action ∈ {block, warn}`.
- **`llm_budget_snapshots`** — periodic aggregate cache per `(budget_id, period_start)`. Not source of truth; rebuilt from `llm_runs` by `refresh_snapshots()`.
- **`llm_cost_rates`** — per-model price list (`input_per_1k`, `output_per_1k`, …) for projecting call cost before the request.

A single `metadata.create_all()` creates tracking + jobs + cache + budget tables in one pass.

## Budget config (budgets.yaml)

Version-controlled source of truth. `sync_budgets_from_yaml()` upserts into `llm_budgets`; scopes removed from the YAML are **disabled** (not deleted) to preserve history.

```yaml
# config/budgets.yaml

global:
  daily: 50.00
  monthly: 1000.00
  soft_thresholds: [0.5, 0.8, 0.95]
  action: warn            # global is a warn-only tripwire

agents:
  summarizer:
    daily: 20.00
    monthly: 400.00
    action: block
    soft_thresholds: [0.5, 0.8, 0.95]
  classifier:
    daily: 10.00
    action: block
  backfill:
    daily: 5.00
    action: block

job_kinds:
  item_summary:
    daily: 30.00

tags:
  "experiment:opus47":
    monthly: 100.00
    action: block
```

Load path resolved from `BUDGET_CONFIG` (default `config/budgets.yaml`). Reload cadence: `BUDGET_CONFIG_RELOAD_SECONDS` (default 300).

## The pre-call guard

```python
check_budget(
    *,
    agent_type: str | None = None,
    projected_cost_usd: float,
    job_id: int | None = None,
    job_kind: str | None = None,
    tags: list[str] | None = None,
    override: dict | None = None,
) -> None
```

Checks in order: **global → agent → job_kind → job_id → tag**. First failing `block` scope raises `CostBudgetExceeded`. `warn` scopes log and continue.

`CostBudgetExceeded` attributes: `scope_kind`, `scope_value`, `period`, `limit_usd`, `spent_usd`, `projected_usd`.

### Spent calculation

Per budget: snapshot value + live delta from `llm_runs` recorded after the snapshot. Runs with `status IN ('cache_hit', 'budget_blocked')` are excluded.

> **`cost_usd` is not homogeneous across backends — know the mix before you sum.** Each client populates the field with what it can actually know: **OpenRouter** reports the provider's billed cost (an actual); **Anthropic** computes it locally from the bundled rate table (an estimate); **Claude Code** records `0.0` (a Claude Max session doesn't bill per call). A budget scope, a `cost_by_model` total, or any `SUM(cost_usd)` therefore blends billed-actuals, local-estimates, and structural zeros into one number. This is correct per-call and usually fine within a single-backend scope; it becomes misleading only when one agent's runs span backends. `llm_runs.provider` records which backend produced each row — group or filter by it when the blend would distort the figure (the shipped `stats` aggregates group by model, not provider).

### Period boundaries

Calendar-anchored, UTC:
- `daily` resets at 00:00 UTC.
- `monthly` resets at the 1st at 00:00 UTC.

### Projection

```python
cost = project_cost(
    agent_type="summarizer",
    model="claude-opus-4-7",
    estimated_prompt_tokens=1500,
    estimated_completion_tokens=1000,
)
```

Uses the active `llm_cost_rates` row for the model. Falls back to a 24h rolling mean of `llm_runs.cost_usd` for the (agent, model) pair when no rate row exists.

For a DB-free estimate (no `llm_cost_rates` table), [`pf_core.pricing.estimate_cost`](pricing.md) computes the same input+output math from the shared per-model rate tables — useful for pre-call gating in a consumer that doesn't run `[tracking]`.

## Blocked-call audit trail

```python
from pf_core.budget import record_blocked_run

record_blocked_run(agent_type="summarizer", model=cfg["model"], exc=exc, job_id=job_id)
```

Writes a zero-cost `llm_runs` row with `status='budget_blocked'` and tags `budget:blocked`, `budget:scope=agent:summarizer:daily`. Keeps blocked calls visible in analytics — *"how many calls did the budget save us from?"* is a measurable question.

## Override path

```python
check_budget(
    agent_type="summarizer",
    projected_cost_usd=projected,
    override={"reason": "manual backfill", "operator": "ops@example.com"},
)
```

- Short-circuits to pass, regardless of budget state.
- After the call, attach the tag + outcome row with `record_override(run_id=..., reason=..., operator=...)`.
- Writes `llm_run_outcomes.outcome_kind='budget_override'` with reason + operator.

## Emergency kill-switch

Set `BUDGET_ENFORCEMENT_DISABLED=true` in the environment. `check_budget()` short-circuits to always-pass and `project_cost()` returns `0.0` without touching the DB — the whole guard pair goes inert without config changes. Useful during incident response when false positives are blocking real work, and in test suites (the `pf_budget_disabled` fixture sets it — see [testing.md](testing.md)).

## Soft-threshold alerts

When a call pushes spend across a soft-threshold fraction (e.g. `0.8` of limit), `check_budget()` logs a structured `budget_threshold_crossed` event once per `(budget, period_start, threshold)`. In-process dedupe set; a process restart re-arms alerts.

## Snapshot refresh

```python
from pf_core.budget import refresh_snapshots

refresh_snapshots()              # all budgets
refresh_snapshots(period="daily")  # just daily
```

Designed to run on a ~60s cron for daily budgets, ~5min for monthly. The snapshot query aggregates `llm_runs.cost_usd` per scope — O(rows in current period). On a well-indexed `llm_runs` with ~1M rows, a daily sum is <100ms.

### Background refresh loop

For long-running consumer processes (FastAPI app, worker daemon), call `start_budget_refresh_loop()` once at boot. It launches a daemon thread that calls `refresh_snapshots()` on an interval — no cron / systemd timer / APScheduler dependency required.

```python
from pf_core.budget import start_budget_refresh_loop

# In FastAPI startup hook, worker entry point, etc.
start_budget_refresh_loop()                       # 60s default
start_budget_refresh_loop(interval_seconds=300)   # monthly-only consumer
```

Idempotent — only the first call wins. Refresh failures are logged at WARNING and swallowed; the loop continues so a transient DB hiccup does not freeze snapshots forever.

**Do not call this from short-lived CLI commands.** A one-shot command should rely on whatever the last cached snapshot was; starting a daemon thread inside it just adds shutdown noise.

## Consumer rollout pattern

Sequential, defensively:

1. Populate `llm_cost_rates` with current provider prices for each model in `llm_models`.
2. Start with `warn`-only global budget. Watch for a week to calibrate — verify projected ≈ actual.
3. Add agent budgets as `warn` first. Measure threshold crossings.
4. Promote to `block` one agent at a time, starting with the highest-volume loop (the highest-risk shape for runaway spend).
5. Route the structured `budget_threshold_crossed` / `budget_warn_exceeded` log events to Slack or similar via your log pipeline (pf-core does not ship a webhook integration).
6. For agents that should fall back rather than fail: service-side catch `CostBudgetExceeded`, call `get_agent_config(cheaper_slug)`, retry.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BUDGET_CONFIG` | `config/budgets.yaml` | YAML config path |
| `BUDGET_CONFIG_RELOAD_SECONDS` | `300` | In-process reload TTL |
| `BUDGET_ENFORCEMENT_DISABLED` | unset | When `true`, `check_budget()` always passes and `project_cost()` returns `0.0` (no DB access) |

## Caveats

1. **Concurrent race.** Two calls both passing the check at 99.5% can land at 100.3%. Acceptable slop — defending against orders of magnitude overruns, not $0.01 precision.
2. **Cold start.** First call of a new period has no snapshot and `spent_usd=0`. Fine by design.
3. **Time zones.** All boundaries are UTC. Document this to human operators loudly.
4. **Stale projection.** If `llm_cost_rates` drifts from actual costs, run a projection-accuracy query weekly (compare projected vs actual `cost_usd` on recent `llm_runs`) and update rates when `|mean_delta| > 5%`.
5. **Missing rate.** When a model has no row, projection falls back to 24h mean — slow to adapt but never fails closed on missing data.
