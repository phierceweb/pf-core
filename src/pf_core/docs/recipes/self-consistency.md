# Recipe: Self-consistency via N-way sampling

Make the same call N times with non-zero temperature, then majority-vote the results. Trades token cost for accuracy on tasks with a well-defined correct answer.

## When to use

- High-stakes classifications (safety labels, category labels, binary flags)
- Tasks where being right most of the time at 5x cost beats being right most of the time at 1x cost
- Agents whose outputs you'd validate with `validate/_semantic.py` anyway — self-consistency front-runs the disagreement instead of discovering it post-hoc

## When NOT to use

- Generative output (drafts, summaries, prose) — there is no "majority" for novel text
- Cost-sensitive workflows — N=5 at temp=0.7 can be worse than N=1 at temp=0 after calibration
- Deterministic tasks where temp=0 already converges reliably

## The pattern

```python
from collections import Counter
from pf_core.parallel import run_parallel
from pf_core.llm import get_agent_config
from pf_core.llm.tracking import track_run
from pf_core.llm.tracking.subrepos import LlmRunLinkRepo
from pf_core.clients.openrouter import get_client


@track_run(agent_type="classifier", provider="openrouter")
def _classify_once(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)


def classify_consistent(text: str, *, n: int = 5) -> dict:
    cfg = get_agent_config("classifier")
    # Force non-zero temperature so samples diverge
    cfg = {**cfg, "temperature": 0.7, "seed": None}

    messages = [{"role": "user", "content": text}]
    results: list[tuple[str, int]] = []

    def _one(_idx: int) -> None:
        content, usage = _classify_once(messages=messages, **cfg)
        label = _parse_label(content)
        results.append((label, usage["_llm_run_id"]))

    run_parallel(items=list(range(n)), fn=_one, workers=n)

    # Majority vote
    labels = [r[0] for r in results]
    winner, winner_count = Counter(labels).most_common(1)[0]
    agreement = winner_count / n

    # Link sibling runs to the winning run for admin traceability
    winning_run_id = next(rid for lbl, rid in results if lbl == winner)
    link_repo = LlmRunLinkRepo()
    for lbl, rid in results:
        if rid != winning_run_id:
            link_repo.link(
                parent_id=winning_run_id,
                child_id=rid,
                relation="sibling",
            )

    return {
        "label": winner,
        "agreement": agreement,          # 0.6 = 3/5 agreed
        "all_labels": labels,
        "winning_run_id": winning_run_id,
    }
```

## Cost calculus

- At N=5 with temp=0.7, expect ~5x input tokens, ~5x output tokens, ~5x cost
- Before shipping, run an eval: which gives higher golden-set accuracy — `N=1 temp=0` or `N=5 temp=0.7`?
- If the answer is "about the same," stick with N=1 and save the cost. Self-consistency only pays off when the N=1 model genuinely disagrees with itself across samples.

## Using `agreement` as a confidence signal

The agreement fraction (1.0 = unanimous, 0.2 = 5-way split) is an output in its own right. Route low-agreement items to human review:

```python
result = classify_consistent(text, n=5)
if result["agreement"] < 0.6:
    _flag_for_review(text, result)
```

This is the payoff over a single call: you get a principled confidence estimate, not just a label.

## What the admin surfaces

- `/admin/llm/run/{winning_run_id}` — sibling links point at the other N-1 runs, and the run's tags
- `/admin/llm/cost-by-agent` — the full N calls all appear under the same `agent_type`, so the N-fold overhead lands in that agent's total

## Tag convention

When shipping a self-consistency variant alongside the base agent, tag its runs so the two cohorts stay separable. Tags are written when the row is recorded, and `@track_run` has no tag hook — record through [`tracked_messages_call`](../llm-tracked.md) instead of the decorator when you want them:

```python
from pf_core.llm.tracked import tracked_messages_call

content, usage, run_id = tracked_messages_call(
    client=client,
    agent_type="classifier",
    messages=messages,
    model=cfg["model"],
    sampling={"temperature": 0.7},
    metadata={"consistency": f"n={n}"},   # → tag "consistency:n=5"
)
```

`LlmRunRepo.record(tags=["consistency:n=5"])` is the lower-level equivalent.

There is no cost-by-tag page. To compare `consistency:n=5` against `consistency:n=1`, pull each cohort's run ids with `LlmRunStatsRepo.runs_with_all_tags([...])` (see [LLM tracking](../llm-tracking.md)) and aggregate cost over them.
