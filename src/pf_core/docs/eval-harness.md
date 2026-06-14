# Eval Harness (`pf_core.eval`)

Replay historical LLM calls against a new model or prompt version; compare results deterministically or via an LLM judge; surface regressions before they hit production.

**Version added:** v0.8.0

## Quick start

```python
from pf_core.eval import EvalRunner, GoldenSetRepo

# 1. Promote known-good runs to the golden set
repo = GoldenSetRepo()
repo.add(run_id=1042, version="golden_v1", notes="canonical high-quality summary")

# 2. Replay against a new model
runner = EvalRunner(config_path="config/eval.yaml")
report = runner.run(
    version="golden_v1",
    agent_type="summarizer",
    target={"model": "anthropic/claude-opus-4-7"},
    tag_as="experiment:opus47",
)

print(report.summary())     # pass/fail, mean score, distribution
report.write_html("out/summarizer_opus47.html")
```

## Concepts

### The golden set

A golden run is an `llm_runs` row that has been reviewed, found good, and tagged `eval:golden_<version>`. It stores:

- Rendered prompts (`llm_run_payloads.rendered_system` + `rendered_user`)
- Parsed output (`llm_run_payloads.parsed_output`)
- A reviewer outcome (`llm_run_outcomes.outcome_kind='golden_approved'`)
- Optional ground-truth annotations (`llm_run_metrics`)

Membership is tag-based. A run can belong to multiple versions (`golden_v1`, `golden_v2`).

### Replay

Given a golden run, the replay engine:

1. Loads the stored rendered prompts from `llm_run_payloads`
2. Overlays the target config (new model, eval sampling)
3. Calls `OpenRouterClient.chat()` with the stored prompts verbatim
4. Records the new `llm_runs` row, links it to the golden via `llm_run_links(relation='replay')`
5. Compares golden output vs replay output; writes score to `llm_run_outcomes(outcome_kind='eval_score')`

All replay runs are regular tracked runs — they appear in `pf-jobs`, `pf-stats`, cost reports, etc.

### Comparators

| Name | Description |
|---|---|
| `structured_diff` | Field-by-field comparison with per-field tolerances. Returns mean score across `diff_fields`. |
| `llm_judge` | Sends golden + replay output to a judge LLM; parses `{"score": 0-1, "rationale": "..."}`. The judge agent is routed via `resolve_agent(judge_agent_type)` and must be declared in `model_router.yaml` — an unconfigured or unresolvable judge agent raises `ConfigurationError` (the eval fails loudly rather than scoring against a default model). The judge call is itself a tracked `llm_runs` row, linked to the replay run via `llm_run_links(relation='critic')`. |
| `custom:<name>` | Project-registered comparator via `@register_comparator("name")`. |

### Job tracking

Each `EvalRunner.run()` call creates one `eval_replay` job that tracks progress (`N/M golden runs complete`). Inspect with `pf-jobs show <id>`.

---

## `GoldenSetRepo`

```python
from pf_core.eval import GoldenSetRepo

repo = GoldenSetRepo()

# Add (idempotent)
repo.add(run_id=1042, version="golden_v2", notes="canonical summary")

# Add with ground-truth annotations (stored as llm_run_metrics)
repo.add(
    run_id=8891,
    version="golden_v2",
    ground_truth={"expected_score": 85.0, "field_ratio": 0.9},
    notes="edge case: ambiguous input",
)

# List (returns llm_runs dicts, optionally filtered by agent_type)
members = repo.list(version="golden_v2", agent_type="summarizer", limit=200)

# Remove tag (outcomes + metrics kept as history)
repo.remove(run_id=1042, version="golden_v2")

# Bulk-seed from runs with a given outcome (e.g. human-accepted summaries)
seeded = repo.seed_from_outcomes(
    version="golden_v1",
    outcome_kind="summary_accepted",  # any outcome_kind your project writes
    agent_type="summarizer",          # optional: restrict to one agent
    limit=50,                         # cap at N most-recent matches
    dry_run=False,                    # True → list candidates without promoting
)
# returns list[int] of run_ids promoted

# Load stored prompts + output for a golden run
payload = repo.get_payload(run_id=1042)
# {"rendered_system": "...", "rendered_user": "...", "parsed_output": {...}}

# Load ground-truth annotations
gt = repo.get_ground_truth(run_id=8891)
# {"expected_score": 85.0, "field_ratio": 0.9}
```

---

## `EvalRunner`

```python
from pf_core.eval import EvalRunner

runner = EvalRunner(config_path="config/eval.yaml")

report = runner.run(
    version="golden_v2",
    agent_type="summarizer",
    target={"model": "anthropic/claude-opus-4-7"},
    tag_as="experiment:opus47-v5",   # optional experiment label
)
```

### `compare_experiments`

Compare two sets of replay runs against the same golden members:

```python
pairs = runner.compare_experiments(
    baseline="experiment:current-prod",
    candidate="experiment:opus47-v5",
    agent_type="summarizer",
)
# [{"golden_id": 1042, "baseline_score": 0.82, "candidate_score": 0.91, "delta": 0.09}, ...]
```

---

## `EvalReport`

```python
report.mean_score     # float
report.median_score   # float
report.pass_rate      # fraction of runs above threshold
report.passed         # bool — mean_score >= pass_threshold

report.summary()                   # human-readable text block
report.write_html("out/rep.html")  # self-contained HTML diff file
report.results                     # list[EvalResult]
```

---

## `EvalResult`

```python
@dataclass
class EvalResult:
    golden_id: int
    run_id: int       # -1 if the replay call failed before recording
    score: float      # 0.0-1.0
    passed: bool
    error: str | None  # short message if replay failed outright
```

---

## Eval config (`config/eval.yaml`)

```yaml
defaults:
  compare: structured_diff
  pass_threshold: 0.85
  parallelism: 4
  sampling:
    temperature: 0.0   # force deterministic for replay

agents:
  summarizer:
    compare: llm_judge
    judge_agent_type: summarizer_judge
    pass_threshold: 0.80

  classifier:
    compare: structured_diff
    diff_fields: [category, confidence]
    tolerances:
      confidence: 0.10
    pass_threshold: 0.95

  extractor:
    compare: structured_diff
    diff_fields: [amount, rationale]
    tolerances:
      amount: 3.0
    pass_threshold: 0.90
```

### Config keys

| Key | Default | Description |
|---|---|---|
| `compare` | `structured_diff` | Comparator name. One of `structured_diff`, `llm_judge`, `custom:<name>`. |
| `pass_threshold` | `0.85` | Mean score required for the overall eval to pass. |
| `parallelism` | `4` | Concurrent replay workers. |
| `sampling` | `{temperature: 0.0}` | Sampling overrides for replay calls (merged over base agent config). |
| `diff_fields` | all golden fields | Fields to compare in `structured_diff`. |
| `tolerances` | `{}` | Per-field abs tolerance for numeric fields. |
| `judge_agent_type` | `null` (falls back to `<agent>_judge`) | Slug of the judge agent type when `compare: llm_judge`. When unset, the runner uses `<agent_type>_judge`. The resolved slug must exist in `model_router.yaml`. |
| `metrics` | `[]` | Optional metric gates (see below). |

### Metric gates

```yaml
agents:
  summarizer:
    metrics:
      - name: field_ratio
        min: 0.70
      - name: n_items
        max: 50
```

Gates check `llm_run_metrics` on the replay run (if the service wrote them). If the metric is absent, the gate is skipped. If a gate fails, the run scores `0.0`.

### Environment variable

- `EVAL_CONFIG` — path to eval.yaml. Default: `config/eval.yaml`.

---

## `bin/pf-eval` CLI

```
pf-eval run --version golden_v1 --agent-type summarizer \
            --target model=anthropic/claude-opus-4-7 \
            [--tag-as experiment:opus47] \
            [--output out/report.html]

pf-eval compare --baseline experiment:current-prod \
                --candidate experiment:opus47 \
                --agent-type summarizer

pf-eval list-golden --version golden_v1 [--agent-type summarizer]

pf-eval seed --version golden_v1 --outcome-kind summary_accepted \
             [--agent-type summarizer] [--limit 50] [--dry-run]

pf-eval promote <run_id> --version golden_v1 [--notes "..."]
pf-eval demote  <run_id> --version golden_v1
```

Exit code: **0** = eval passed (mean score ≥ threshold); **1** = eval failed or error.

---

## Custom comparators

```python
from pf_core.eval import register_comparator

@register_comparator("amount_compare")
def amount_compare(golden: dict, replay: dict, *, context: dict) -> float:
    """Pass only when the extracted amount is within tolerance."""
    gold_amount = golden.get("amount", 0)
    repl_amount = replay.get("amount", 0)
    within_3 = abs(gold_amount - repl_amount) <= 3.0
    return 1.0 if within_3 else 0.0
```

Reference in `eval.yaml`:

```yaml
agents:
  extractor:
    compare: custom:amount_compare
```

---

## Seeding golden sets

### From production outcomes

```python
from pf_core.eval import GoldenSetRepo
from pf_core.llm.tracking import LlmRunRepo, LlmRunOutcomeRepo
from sqlalchemy import select
from pf_core.llm.tracking import schema as s

# Find human-accepted summary runs from the last 90 days
# (outcome_kind='summary_accepted' written by your review action)
with transaction() as conn:
    rows = conn.execute(
        select(s.llm_run_outcomes.c.llm_run_id)
        .where(s.llm_run_outcomes.c.outcome_kind == "summary_accepted")
        .limit(50)
    ).fetchall()

repo = GoldenSetRepo()
for (run_id,) in rows:
    repo.add(run_id, version="golden_v1")
```

### From a project table holding ground truth

```python
# Every input with a reviewed_values row is a candidate.
# Seed with the expected value as ground_truth.
for item in reviewed_items:
    run_id = get_run_id(item.id)   # your project lookup
    repo.add(
        run_id,
        version="golden_v1",
        ground_truth={"expected_value": float(item.reviewed_value)},
    )
```

---

## CI integration

```yaml
# .github/workflows/eval.yml
- name: Run summarizer eval
  run: |
    pf-eval run \
      --version golden_v1 \
      --agent-type summarizer \
      --target model=anthropic/claude-sonnet-4-6 \
      --tag-as ci:pr-${{ github.event.pull_request.number }}
  env:
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
```

Non-zero exit code from `pf-eval run` blocks the PR merge.

---

## Query reference

```sql
-- All eval scores for an experiment
SELECT AVG(o.score) AS mean_score, COUNT(*) AS n
FROM llm_run_outcomes o
JOIN llm_run_tags t ON t.llm_run_id = o.llm_run_id
WHERE t.tag = 'experiment:opus47'
  AND o.outcome_kind = 'eval_score';

-- Coverage: golden runs per agent type
SELECT at.slug, COUNT(*) AS n_golden
FROM llm_run_tags t
JOIN llm_runs r ON r.id = t.llm_run_id
JOIN llm_agent_types at ON at.id = r.agent_type_id
WHERE t.tag LIKE 'eval:golden_%'
GROUP BY at.slug;

-- Compare two experiments (paired by golden parent)
SELECT l1.parent_run_id AS golden_id,
       MAX(CASE WHEN t1.tag = 'experiment:current-prod' THEN o1.score END) AS baseline,
       MAX(CASE WHEN t2.tag = 'experiment:opus47'       THEN o2.score END) AS candidate
FROM llm_run_links l1
JOIN llm_run_links l2 ON l2.parent_run_id = l1.parent_run_id
JOIN llm_run_tags t1 ON t1.llm_run_id = l1.child_run_id
JOIN llm_run_tags t2 ON t2.llm_run_id = l2.child_run_id
JOIN llm_run_outcomes o1 ON o1.llm_run_id = l1.child_run_id AND o1.outcome_kind = 'eval_score'
JOIN llm_run_outcomes o2 ON o2.llm_run_id = l2.child_run_id AND o2.outcome_kind = 'eval_score'
WHERE l1.relation = 'replay' AND l2.relation = 'replay'
GROUP BY l1.parent_run_id;
```

---

## Consumer setup

### Step 1 — Create `config/eval.yaml`

Copy the relevant block from the examples below into your project's `config/eval.yaml`.

**An app with summarizer + classifier agents**:

```yaml
defaults:
  compare: structured_diff
  pass_threshold: 0.85
  parallelism: 4
  sampling:
    temperature: 0.0

agents:
  summarizer:
    compare: llm_judge
    judge_agent_type: summarizer_judge
    pass_threshold: 0.80
    metrics:
      - name: field_ratio
        min: 0.70

  classifier:
    compare: structured_diff
    diff_fields: [category, confidence]
    tolerances:
      confidence: 0.10
    pass_threshold: 0.95
```

**An app with an extractor agent**:

```yaml
defaults:
  compare: structured_diff
  pass_threshold: 0.90
  parallelism: 4
  sampling:
    temperature: 0.0

agents:
  extractor:
    compare: structured_diff
    diff_fields: [amount, rationale]
    tolerances:
      amount: 3.0
    pass_threshold: 0.90
```

### Step 2 — Seed the golden set

**From existing outcome records** (the summarizer and classifier agents):

```bash
# Dry-run first to see candidates
pf-eval seed --version golden_v1 --outcome-kind summary_accepted \
             --agent-type summarizer --limit 50 --dry-run

# Promote when happy with the list
pf-eval seed --version golden_v1 --outcome-kind summary_accepted \
             --agent-type summarizer --limit 50
```

Or in Python (e.g. a one-time migration script):

```python
from pf_core.eval import GoldenSetRepo

repo = GoldenSetRepo()
seeded = repo.seed_from_outcomes(
    version="golden_v1",
    outcome_kind="summary_accepted",
    agent_type="summarizer",
    limit=50,
)
print(f"Seeded {len(seeded)} summarizer runs")

seeded = repo.seed_from_outcomes(
    version="golden_v1",
    outcome_kind="classified_accepted",
    agent_type="classifier",
    limit=100,
)
print(f"Seeded {len(seeded)} classifier runs")
```

**From a project ground-truth table** (the extractor agent — project-specific because ground truth lives in a non-tracking table):

```python
from pf_core.eval import GoldenSetRepo
from sqlalchemy import select, text
from pf_core.db import transaction

# Find extractor runs paired with a reviewed value
with transaction() as conn:
    rows = conn.execute(text("""
        SELECT r.llm_run_id, g.amount
        FROM extractor_results r
        JOIN reviewed_values g ON g.input_id = r.input_id
        WHERE g.amount IS NOT NULL
        ORDER BY r.created_at DESC
        LIMIT 100
    """)).fetchall()

repo = GoldenSetRepo()
for run_id, reviewed_amount in rows:
    repo.add(
        run_id,
        version="golden_v1",
        ground_truth={"expected_amount": float(reviewed_amount)},
        notes="seeded from reviewed_values",
    )
print(f"Seeded {len(rows)} extractor runs with ground truth")
```

### Step 3 — Verify coverage

```bash
pf-eval list-golden --version golden_v1 --agent-type summarizer
```

Or via SQL:

```sql
SELECT at.slug, COUNT(*) AS n_golden
FROM llm_run_tags t
JOIN llm_runs r ON r.id = t.llm_run_id
JOIN llm_agent_types at ON at.id = r.agent_type_id
WHERE t.tag = 'eval:golden_v1'
GROUP BY at.slug;
```

Aim for 30–50 members per agent type. Prioritise diverse, hard cases over volume.

### Step 4 — Run the eval

```bash
# Test against the current production model
pf-eval run --version golden_v1 --agent-type summarizer \
            --tag-as experiment:baseline \
            --output out/summarizer_baseline.html

# Test a new model
pf-eval run --version golden_v1 --agent-type summarizer \
            --target model=anthropic/claude-opus-4-7 \
            --tag-as experiment:opus47 \
            --output out/summarizer_opus47.html

# Compare
pf-eval compare --baseline experiment:baseline \
                --candidate experiment:opus47 \
                --agent-type summarizer
```

### Step 5 — Wire CI

```yaml
# .github/workflows/eval.yml
- name: Summarizer eval gate
  run: |
    pf-eval run \
      --version golden_v1 \
      --agent-type summarizer \
      --target model=anthropic/claude-sonnet-4-6 \
      --tag-as ci:pr-${{ github.event.pull_request.number }}
  env:
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
```

Exit code 1 blocks the PR merge automatically.

---

## See also

- `docs/llm-tracking.md` — the tracking tables eval reads from and writes to
- `docs/jobs.md` — the `eval_replay` job kind
- `.ai/plans/EVAL_HARNESS.md` — design rationale
