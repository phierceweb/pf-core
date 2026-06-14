# Recipe: Critic pattern

Two-call pattern. The primary agent produces output; a second agent (`agent_type="critic"`) reads that output and scores or flags it. Links connect the two so the admin can trace the chain.

The example below uses a `summarizer` as the primary agent, but the pattern applies to any generative agent whose output is worth a second look.

## When to use

- You need a quality check that's harder to encode as a rule than to ask an LLM
- Primary agent's cost is high enough that catching bad outputs pre-delivery is worth a second call
- You want to track critic disagreement rates over time (drift signal)

## When NOT to use

- Cost-sensitive agents — critic doubles token spend per item
- Quick classifications where rule-based validation suffices
- Tight latency budgets (two sequential LLM calls)

## The pattern

```python
from pf_core.llm import get_agent_config
from pf_core.llm.tracking import track_run
from pf_core.llm.tracking.subrepos import LlmRunLinkRepo
from pf_core.clients.openrouter import get_client


@track_run(agent_type="summarizer", provider="openrouter")
def _summarize(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)


@track_run(agent_type="critic", provider="openrouter")
def _critic(*, model, messages, **sampling):
    return get_client().chat(model=model, messages=messages, **sampling)


def summarize_with_critic(prompt: str) -> dict:
    summarizer_cfg = get_agent_config("summarizer")
    critic_cfg = get_agent_config("critic")

    # 1. Primary call
    summary_content, summary_usage = _summarize(
        messages=[{"role": "user", "content": prompt}],
        **summarizer_cfg,
    )
    summary_run_id = summary_usage["_llm_run_id"]

    # 2. Critic reads the summary + original prompt
    critic_messages = [
        {"role": "system", "content": _critic_system_prompt()},
        {"role": "user", "content": f"Prompt: {prompt}\n\nSummary: {summary_content}"},
    ]
    critic_content, critic_usage = _critic(
        messages=critic_messages, **critic_cfg
    )
    critic_run_id = critic_usage["_llm_run_id"]

    # 3. Link the critic back to the summary
    LlmRunLinkRepo().link(
        parent_id=summary_run_id,
        child_id=critic_run_id,
        relation="critic",
    )

    return {
        "summary": summary_content,
        "critique": critic_content,
        "summary_run_id": summary_run_id,
        "critic_run_id": critic_run_id,
    }
```

## Deciding what to do with the critique

The critic's *output* is separate from what your service does with it. Common choices:

- **Advisory log** — write a `llm_run_outcomes` row with `outcome_kind="critic_flagged"` and the reason. Summary still ships. Aggregates over time show agents that accumulate flags.
- **Retry with feedback** — on flagged critique, re-run the summarizer with the critic's objection as added context. Link the retry via `llm_run_links.relation="retry_after_critic"`.
- **Hard reject** — raise a `FlowException` so the caller handles it.

## What the admin surfaces

- `/admin/llm/run/{summary_run_id}` — links section shows the critic as an outgoing link with `relation="critic"`
- `/admin/llm/run/{critic_run_id}` — links section shows the summary as an incoming link
- `/admin/llm/cost-by-agent` — `summarizer` vs `critic` side-by-side, so the 2x token cost is visible

## Good critic prompts

- Return structured output (JSON with `verdict`, `issues`, `severity`) — easier to parse than prose
- Be explicit about severity thresholds so the service can branch
- Include *why* in the output — the critic's reasoning is the interesting signal, not just the verdict
