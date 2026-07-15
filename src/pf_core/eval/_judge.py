"""
LLM-judge comparator for free-form text outputs.

The judge is itself a tracked LLM call. It receives the task description
(user prompt from the golden run), the golden output, and the replay output,
and returns a 0.0–1.0 score with a rationale.

Judge output schema (parsed from the judge model's response)::

    {"score": 0.87, "rationale": "..."}

The judge run is recorded and linked to the replay run via
``llm_run_links(relation="critic")``.

Usage::

    from pf_core.eval._judge import run_judge

    score = run_judge(
        agent_type="drafter",
        judge_agent_type="drafter_judge",
        golden_payload={"rendered_user": "...", "parsed_output": {...}},
        replay_content="<raw LLM response from replay>",
        replay_run_id=50291,
    )
"""

from __future__ import annotations

import json

from pf_core.log import get_logger
from pf_core.llm.parse import parse_llm_json
from pf_core.llm.router import resolve_agent
from pf_core.llm.tracking.repo import LlmRunRepo
from pf_core.llm.tracking.subrepos import LlmRunLinkRepo

logger = get_logger(__name__)

_JUDGE_SYSTEM = (
    "You are an expert evaluator. Your task is to compare two LLM outputs "
    "for the same task and score the candidate output relative to the reference.\n\n"
    "Return ONLY valid JSON in this exact format:\n"
    "{\"score\": <float 0.0-1.0>, \"rationale\": \"<one sentence>\"}\n\n"
    "Score 1.0 = candidate matches or exceeds reference quality. "
    "Score 0.0 = candidate is completely wrong or empty."
)

_JUDGE_USER_TEMPLATE = """\
## Task description (from user prompt)
{task_description}

## Reference output (golden)
{golden_output}

## Candidate output (replay)
{replay_output}

Evaluate the candidate. Return JSON only.
"""


def run_judge(
    *,
    agent_type: str,
    judge_agent_type: str,
    golden_payload: dict,
    replay_content: str,
    replay_run_id: int,
) -> float:
    """Run an LLM judge and return a 0.0–1.0 score.

    The judge call is tracked as a new ``llm_runs`` row linked to
    ``replay_run_id`` via ``relation="critic"``.

    Args:
        agent_type: The slug of the agent type being evaluated.
        judge_agent_type: The slug of the judge agent (must be configured in
            model_router.yaml).
        golden_payload: Dict from ``GoldenSetRepo.get_payload()`` — contains
            ``rendered_user`` and ``parsed_output``.
        replay_content: Raw string response from the replay LLM call.
        replay_run_id: ID of the replay ``llm_runs`` row (for linking).

    Returns:
        Float score 0.0–1.0. Returns 0.0 on any judge failure.
    """
    task_desc = (golden_payload.get("rendered_user") or "")[:2000]
    golden_out = json.dumps(golden_payload.get("parsed_output") or {}, indent=2)[:3000]
    replay_out = replay_content[:3000]

    user_msg = _JUDGE_USER_TEMPLATE.format(
        task_description=task_desc,
        golden_output=golden_out,
        replay_output=replay_out,
    )
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    # No hardcoded judge model or backend: the judge agent must be declared
    # in model_router.yaml like any other agent. A missing slug or
    # unresolvable backend raises ConfigurationError here — config problems
    # fail the eval run loudly instead of silently scoring with a stale
    # framework-chosen model.
    client, cfg, _backend = resolve_agent(judge_agent_type)
    model = cfg.pop("model")
    # Deterministic defaults only — the judge agent's YAML sampling wins when
    # set (a reasoning judge may need max_tokens well past 512).
    cfg.setdefault("temperature", 0.0)
    cfg.setdefault("max_tokens", 512)

    try:
        raw_content, usage_raw = client.chat(messages=messages, model=model, **cfg)
    except Exception as exc:
        logger.warning("judge_call_failed", agent_type=agent_type, error=str(exc)[:200])
        return 0.0

    parsed = parse_llm_json(raw_content)
    score = 0.0
    if isinstance(parsed, dict) and "score" in parsed:
        try:
            score = max(0.0, min(1.0, float(parsed["score"])))
        except (TypeError, ValueError):
            score = 0.0

    # Record the judge run and link it to the replay
    judge_run_id = LlmRunRepo().record(
        agent_type=judge_agent_type,
        model=model,
        sampling=cfg,
        rendered_prompts=(_JUDGE_SYSTEM, user_msg),
        raw_response=raw_content,
        parsed_output=parsed if isinstance(parsed, dict) else None,
        tags=["eval:judge"],
        usage=usage_raw,
    )
    LlmRunLinkRepo().link(parent_id=replay_run_id, child_id=judge_run_id, relation="critic")

    logger.debug(
        "judge_scored",
        agent_type=agent_type,
        judge_agent_type=judge_agent_type,
        replay_run_id=replay_run_id,
        score=score,
    )
    return score
