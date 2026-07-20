"""The guarded, cached, tracked, validated LLM call — one composition.

``llm_step`` runs the per-item hot path of a batch LLM pass: input-hash →
cache lookup (hit: record and return, budget skipped) → budget gate →
:func:`~pf_core.llm.tracked.tracked_messages_call` → parse/validate →
cache store. The batch shell around it — Job/steps, ``run_parallel``,
persistence — stays in the caller (see ``docs/recipes/batch-llm-service.md``).

A failed validation *returns* (``result.validation.ok`` False); a blocked
budget records the blocked run and *raises* ``CostBudgetExceeded``; a client
error records a failed run and re-raises — each mirroring the underlying
primitive's contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from pf_core.budget import (
    CostBudgetExceeded,
    check_budget,
    project_cost,
    record_blocked_run,
)
from pf_core.llm.cache import cache_lookup, cache_store, record_cache_hit
from pf_core.llm.tracked import tracked_messages_call
from pf_core.llm.tracking import compute_input_hash
from pf_core.llm.validate import ValidationResult, parse_and_validate

__all__ = ["BudgetEstimate", "StepResult", "llm_step"]


@dataclass(frozen=True)
class BudgetEstimate:
    """Pre-call budget gate config; token defaults mirror ``project_cost``'s."""

    prompt_tokens: int = 1500
    completion_tokens: int = 1000
    job_id: int | None = None
    job_kind: str | None = None


class StepResult(NamedTuple):
    """Outcome of one :func:`llm_step` call.

    ``value`` is the validated object when ``validate`` was set and passed
    (``None`` when it failed); with ``validate=None`` it is the raw content —
    or, on a cache hit, the stored ``parsed_output`` when one exists.
    """

    value: Any
    content: str
    run_id: int | None
    cache_hit: bool
    validation: ValidationResult | None


def llm_step(
    *,
    client: Any,
    agent_type: str,
    messages: list[dict],
    model: str,
    sampling: dict[str, Any] | None = None,
    chat_kwargs: dict[str, Any] | None = None,
    spec: dict | None = None,
    spec_on_change: str = "keep_first",
    provider: str | None = None,
    configs: dict[str, int] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    metrics: dict[str, float] | None = None,
    items_out: int | None = None,
    job_id: int | None = None,
    on_record_error: str = "raise",
    cache: bool = False,
    input_hash: str | None = None,
    budget: BudgetEstimate | None = None,
    validate: str | None = None,
    validation_context: dict[str, Any] | None = None,
) -> StepResult:
    """Run one cache/budget/track/validate-composed LLM call.

    Args:
        client...on_record_error: Forwarded to
            :func:`~pf_core.llm.tracked.tracked_messages_call` under the same
            names and semantics.
        cache: Enable the cache legs — lookup before the call (a hit records a
            ``cache_hit`` run and returns without chat or budget), store after
            a successful, validation-passing call.
        input_hash: Cache/tracking key override. Default when ``cache`` is on:
            ``compute_input_hash(model, messages, sampling, configs)``.
        budget: When given, gate the live call: ``project_cost`` with these
            estimates → ``check_budget``; a block records the blocked run and
            re-raises ``CostBudgetExceeded`` (caller decides skip/collect).
        validate: ``expect=`` for :func:`parse_and_validate` (``"object"``,
            ``"array"``, ``"any"``); ``None`` skips parsing entirely. Cache
            hits re-validate the stored raw response, so a validator change
            re-judges old cache entries instead of trusting stored output.
        validation_context: Forwarded to :func:`parse_and_validate`.

    Returns:
        A :class:`StepResult`. Persistence of ``value`` is the caller's job.
    """
    resolved_hash = input_hash
    if cache and resolved_hash is None:
        resolved_hash = compute_input_hash(
            model=model, messages=messages, sampling=sampling, configs=configs
        )

    if cache:
        hit = cache_lookup(agent_type=agent_type, input_hash=resolved_hash)
        if hit is not None:
            run_id = record_cache_hit(hit=hit)
            raw = hit.raw_response or ""
            if validate is not None:
                validation = parse_and_validate(
                    raw,
                    agent_type=agent_type,
                    run_id=None,
                    validation_context=validation_context,
                    expect=validate,
                )
                value = validation.value if validation.ok else None
            else:
                validation = None
                value = hit.parsed_output if hit.parsed_output is not None else raw
            return StepResult(
                value=value,
                content=raw,
                run_id=run_id,
                cache_hit=True,
                validation=validation,
            )

    if budget is not None:
        projected = project_cost(
            agent_type=agent_type,
            model=model,
            estimated_prompt_tokens=budget.prompt_tokens,
            estimated_completion_tokens=budget.completion_tokens,
        )
        try:
            check_budget(
                agent_type=agent_type,
                projected_cost_usd=projected,
                job_id=budget.job_id,
                job_kind=budget.job_kind,
            )
        except CostBudgetExceeded as exc:
            record_blocked_run(
                agent_type=agent_type, model=model, exc=exc, job_id=budget.job_id
            )
            raise

    content, _usage, run_id = tracked_messages_call(
        client=client,
        agent_type=agent_type,
        messages=messages,
        model=model,
        sampling=sampling,
        chat_kwargs=chat_kwargs,
        spec=spec,
        spec_on_change=spec_on_change,
        provider=provider,
        input_hash=resolved_hash,
        configs=configs,
        metadata=metadata,
        tags=tags,
        metrics=metrics,
        items_out=items_out,
        job_id=job_id,
        on_record_error=on_record_error,
    )

    if validate is not None:
        validation = parse_and_validate(
            content,
            agent_type=agent_type,
            run_id=run_id,
            validation_context=validation_context,
            expect=validate,
        )
        if not validation.ok:
            return StepResult(
                value=None,
                content=content,
                run_id=run_id,
                cache_hit=False,
                validation=validation,
            )
        value: Any = validation.value
    else:
        validation = None
        value = content

    if cache and run_id is not None:
        parsed_for_cache = value if isinstance(value, (dict, list)) else None
        cache_store(
            agent_type=agent_type,
            input_hash=resolved_hash,
            source_run_id=run_id,
            model=model,
            parsed_output=parsed_for_cache,
            raw_response=content,
        )

    return StepResult(
        value=value, content=content, run_id=run_id, cache_hit=False, validation=validation
    )
