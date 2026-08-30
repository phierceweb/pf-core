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

- **`llm_budgets`** — one row per `(scope_kind, scope_value, period)`. `scope_kind ∈ {global, agent, job_kind, job_id, tag}`. `period ∈ {daily, monthly}`. `action ∈ {block, warn}`. Limits per dimension: `limit_usd`, `limit_tokens`, `limit_calls` (each nullable — NULL = unenforced).
- **`llm_budget_snapshots`** — periodic aggregate cache (`spent_usd`, `spent_tokens`, `run_count`) per `(budget_id, period_start)`. Not source of truth; rebuilt from `llm_runs` by `refresh_snapshots()`.
- **`llm_cost_rates`** — per-model price list (`input_per_1k`, `output_per_1k`, …) for projecting call cost before the request.

A single `metadata.create_all()` creates tracking + jobs + cache + budget tables in one pass.

## Budget config (budgets.yaml)

Version-controlled source of truth. `sync_budgets_from_yaml()` upserts into `llm_budgets`; scopes removed from the YAML are **disabled** (not deleted) to preserve history. A config resolving to *zero* scopes is the exception — see [When the config cannot be read](#when-the-config-cannot-be-read).

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

## Token and call limits

A period key accepts either a bare number (a USD limit, as above) or a mapping with any of `usd` / `tokens` / `calls`:

```yaml
agents:
  vision:
    daily: {usd: 20.0, tokens: 2000000, calls: 500}   # any subset
  watcher:
    daily: {tokens: 1000000}                           # no USD limit at all
```

`sync_budgets_from_yaml()` writes these to `llm_budgets.limit_usd` / `limit_tokens` / `limit_calls` (NULL where absent). A mapping with none of the three keys, or a non-numeric value, raises `ConfigurationError` like any other malformed config; unknown keys inside the mapping log `budget_config_unknown_limit_keys` at WARNING.

**Why:** USD limits cannot constrain subscription-billed backends. The `claude_code` client reports `cost_usd = 0.0` on every call — a Claude Max session doesn't bill per call — so a USD budget **never trips** for it, no matter how much it consumes. Give any scope that runs over a subscription backend a `tokens` and/or `calls` limit; those count real, exhaustible usage. One caveat: the `claude_code` client's token counts come from its JSON envelope (the default mode) — a consumer that suppresses the envelope via `extra_args` (`--output-format` / `--verbose`) records zero tokens, so give such a scope a `calls` limit, which counts regardless (see [claude-code.md](claude-code.md)).

### Enforcement semantics

`check_budget()` enforces every dimension a budget configures, per budget, in the same scope order:

- `usd` against `projected_cost_usd`
- `tokens` against `projected_tokens` (prompt + completion estimate; `None` = 0)
- `calls` against `projected_calls` (`None` = 1 — the guard fronts exactly one call by definition, so a calls cap blocks the (limit+1)th call; pass an explicit `0` for a pure state read that plans no call)

Spent per dimension is the same snapshot-plus-live-delta figure (`current_usage(budget)` returns `{"usd", "tokens", "calls"}`; `current_spent` is its USD view). Tokens sum all four token columns — `prompt_tokens` (uncached input) plus `completion_tokens`, `cache_read_tokens` and `cache_write_tokens` — with NULL columns treated as 0, so a cap counts everything the call consumed. See [llm-tracking.md](llm-tracking.md) for the usage-dict contract. A `warn` budget logs `budget_warn_exceeded` once per over-limit dimension and continues.

**Soft thresholds are USD-only.** `soft_thresholds` fractions anchor to `limit_usd`; a budget with no USD limit logs no threshold crossings.

### Snapshot columns

`llm_budget_snapshots` carries `spent_usd`, `spent_tokens`, and `run_count` — `refresh_snapshots()` writes all three from one aggregate at one cutoff.

### Consumer migration

pf-core ships no migration mechanism; add an alembic revision in each consumer for:

- `llm_budgets.limit_tokens` — new, `BigInteger`, nullable
- `llm_budgets.limit_calls` — new, `Integer`, nullable
- `llm_budgets.limit_usd` — now **nullable** (was NOT NULL)
- `llm_budget_snapshots.spent_tokens` — new, `BigInteger`, NOT NULL, server default `0`

### When the config cannot be read

A budget guard that quietly disarms itself is worse than no guard, so the three states are kept distinct:

| State | `load_yaml()` | `sync_budgets_from_yaml()` |
|---|---|---|
| File absent | returns `{}` (`budget_config_absent`, DEBUG) — the guard is opt-in | no-op |
| File exists, unreadable or not a top-level mapping | raises `ConfigurationError` | no-op; logs `budget_config_unreadable` at ERROR |
| File readable but resolves to no scopes | returns the parsed document | disables nothing; logs `budget_sync_refused_mass_disable` at WARNING |

"Unreadable" covers a YAML syntax error, a permission error, a symlink loop, a symlink to a missing target (an unmounted volume, rather than an absent file), a non-UTF-8 body, and a section or limit of the wrong type (`agents: "nope"`, `daily: "20 USD"`). They all surface as `ConfigurationError` so one `except` clause catches the family.

**`sync_budgets_from_yaml()` never raises on a bad config file** — database errors from the write still propagate. Consumers call it at boot, where raising on an unreadable config costs availability without buying safety: the previously synced rows keep enforcing either way. Call `load_yaml()` first if you want boot to fail fast on a bad config:

```python
load_yaml()  # raises ConfigurationError on a bad file
sync_budgets_from_yaml()
```

Unrecognised top-level keys are ignored and logged as `budget_config_unknown_sections` (WARNING) — `agent:` for `agents:` parses cleanly and would otherwise silently define no caps at all.

Because the loader runs on a [`ReloadCache`](reload-cache.md) with `stale_on=(ConfigurationError,)`, a process that already loaded a good config keeps serving it when a later reload fails, logging `reload_cache_kept_stale` once per TTL. Only a first load with nothing cached raises.

## The pre-call guard

```python
check_budget(
    *,
    agent_type: str | None = None,
    projected_cost_usd: float,
    projected_tokens: int | None = None,
    projected_calls: int | None = None,
    job_id: int | None = None,
    job_kind: str | None = None,
    tags: list[str] | None = None,
    override: dict | None = None,
) -> None
```

Checks in order: **global → agent → job_kind → job_id → tag**, each budget on every dimension it configures (see [Token and call limits](#token-and-call-limits)). First failing `block` scope raises `CostBudgetExceeded`. `warn` scopes log and continue.

When *no* enabled row matches any of those scopes the call is uncapped, and that is reported rather than passed over: `budget_no_scopes_matched` logs at WARNING the first time a given `(agent_type, job_kind, tags)` combination goes unmatched and at DEBUG thereafter. `job_id` is excluded from the dedupe key as unbounded, and the set is capped (`_NO_SCOPE_WARN_LIMIT` in `budget/check.py`) so caller-supplied tags cannot grow it without limit; a process restart re-arms. Note that `list_for_scopes` always includes the `global` scope, so any deployment with a global budget never reaches this path.

`CostBudgetExceeded` attributes: `scope_kind`, `scope_value`, `period`, `limit_value`, `spent_value`, `projected_value`, and `dimension` (`'usd' | 'tokens' | 'calls'`, default `'usd'`). The `*_value` attributes are floats in the **tripping dimension's** unit — dollars when `dimension == 'usd'`, token or call counts otherwise — and the message names the dimension. A handler that formats dollars should read the `*_value` attributes and branch on `dimension`. The legacy `limit_usd` / `spent_usd` / `projected_usd` names remain and always carry USD, whichever dimension tripped, so an existing handler that renders them as currency needs no change. They are not aliases of the `*_value` attributes: on a token or call block they report the scope's USD figures, and `limit_usd` is `0.0` when the scope sets no USD cap.

`CostBudgetExceeded` is a `FlowException` subclass — an expected domain failure, not a bug. In a `create_app()` web app, a budget block that reaches the HTTP boundary renders as a **429** domain response (JSON or HTML per the `Accept` header — see [exceptions.md](exceptions.md)).

### Spent calculation

Per budget: snapshot value + live delta from `llm_runs`. Runs with `status IN ('cache_hit', 'budget_blocked')` are excluded. `pf_core.budget.current_usage(budget)` is that figure across all three dimensions (`current_spent(budget)` is its USD view) — the guard and every reporting surface (including the admin `/budgets` page) read it rather than the snapshot alone.

`current_usage()` is built on `pf_core.budget.aggregate_usage(budget=, period_start=, period_end=, cutoff=None, conn=None)`, which runs that aggregate directly against `llm_runs` and returns the same `{"usd", "tokens", "calls"}` dict; `refresh_snapshots()` calls it with a `cutoff` to write a snapshot. `aggregate_spent()` is its `(usd, calls)` tuple view, kept for existing callers.

The two halves meet at one cutoff. `refresh_snapshots()` reads the DB server clock once, aggregates `created_at < cutoff`, and stores that same value as the snapshot's `last_updated`; the live delta then sums `created_at >= last_updated`. Every run falls in exactly one half — nothing is lost to the window between aggregating and writing.

Both halves apply the same scope filter (`pf_core.budget.repo.apply_scope_filter`). A budget row with a `scope_kind` outside `{global, agent, job_kind, job_id, tag}` raises `InvalidInputError` rather than silently counting every scope or none; `refresh_snapshots()` logs and skips such a row so one bad budget cannot freeze the others' snapshots.

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
    provider="anthropic",  # optional — disambiguates the price list
    estimated_prompt_tokens=1500,
    estimated_completion_tokens=1000,
)
```

Priced in order:

1. The active `llm_cost_rates` row for the model.
2. The shared [`pf_core.pricing`](pricing.md) rate tables (built-ins plus anything a consumer passed to `register_rates`).
3. A 24h rolling mean of `llm_runs.cost_usd` for the (agent, model) pair.

When none of the three can price the call, the projection is **unknown, not free**: `project_cost()` logs `budget_projection_unknown` at WARNING and returns `0.0`, so the cap still fires on recorded spend but the gap is visible rather than silent. An unpriced model also records `cost_usd = 0.0` on every run, so its spend never accumulates — register rates (or add an `llm_cost_rates` row) before trusting a cap over it.

`pf_core.pricing.estimate_cost` keeps returning a plain `0.0` float for an unpriced model, so `cost_usd` is never NULL; `pf_core.pricing._resolver.price_call` returns `None` for the same call, which is how a genuinely zero-rate model (a Claude Max session) is told apart from one nobody has priced.

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

When a call pushes spend across a soft-threshold fraction (e.g. `0.8` of limit), `check_budget()` logs a structured `budget_threshold_crossed` event once per `(budget, period_start, threshold)` — the UTC period start, so a monthly budget alerts once per month, not once per calendar day. In-process dedupe set; a process restart re-arms alerts.

## Snapshot refresh

```python
from pf_core.budget import refresh_snapshots

refresh_snapshots()  # all budgets
refresh_snapshots(period="daily")  # just daily
```

Designed to run on a ~60s cron for daily budgets, ~5min for monthly. The snapshot query aggregates `llm_runs.cost_usd` per scope — O(rows in current period). On a well-indexed `llm_runs` with ~1M rows, a daily sum is <100ms.

### Background refresh loop

For long-running consumer processes (FastAPI app, worker daemon), call `start_budget_refresh_loop()` once at boot. It launches a daemon thread that calls `refresh_snapshots()` on an interval — no cron / systemd timer / APScheduler dependency required.

```python
from pf_core.budget import start_budget_refresh_loop

# In FastAPI startup hook, worker entry point, etc.
start_budget_refresh_loop()  # 60s default
start_budget_refresh_loop(interval_seconds=300)  # monthly-only consumer
```

Idempotent — only the first call wins. Refresh failures are logged at WARNING and swallowed; the loop continues so a transient DB hiccup does not freeze snapshots forever.

**Do not call this from short-lived CLI commands.** A one-shot command should rely on whatever the last cached snapshot was; starting a daemon thread inside it just adds shutdown noise.

## Consumer rollout pattern

Sequential, defensively:

1. Populate `llm_cost_rates` with current provider prices for each model in `llm_models`.
2. Start with `warn`-only global budget. Watch for a week to calibrate — verify projected ≈ actual.
3. Add agent budgets as `warn` first. Measure threshold crossings.
4. Promote to `block` one agent at a time, starting with the highest-volume loop (the highest-risk shape for runaway spend).
5. Route the structured `budget_threshold_crossed` / `budget_warn_exceeded` log events to Slack or similar via your log pipeline (pf-core does not ship a webhook integration). Route the unarmed-guard events (`budget_config_unreadable`, `budget_sync_refused_mass_disable`, `budget_no_scopes_matched`) with them — those say the cap is not enforcing at all, which matters more than a threshold crossing.
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
5. **Missing rate.** When a model has no row, projection falls back to the shared price list, then the 24h mean — slow to adapt but never fails closed on missing data. A model none of them knows projects at `0.0` and records `0.0`: the cap goes blind to it, loudly (`budget_projection_unknown`, `pricing_unknown_model`).
6. **Unarmed guard.** A deployment whose budgets were never synced — or whose `budgets.yaml` cannot be read — runs uncapped. Nothing raises; the state is reported instead (`budget_config_unreadable`, `budget_sync_refused_mass_disable`, `budget_no_scopes_matched`). Alert on those events; a silent log is an unenforced cap.
