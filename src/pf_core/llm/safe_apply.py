"""Gather/apply with drift detection — safely run LLM-planned mutations.

The pattern: an LLM analyzes a snapshot of some data (the current
list of records, the current set of IDs, the current item ordering,
…) and produces a transform plan. By the time you APPLY the plan, the
underlying data may have drifted — other transforms ran between gather
and apply, the input was edited, etc. Blindly applying the plan against
the new state can mis-target — relabel the wrong record, mutate the
wrong row, drop the wrong item.

This module provides the safety net: gather captures the targets it
saw alongside its plan, apply re-extracts the current targets and
compares. If counts or texts drifted, the apply skips with a structured
warning rather than mis-applying.

Generalized from a production gather/apply pair so any LLM-driven
transform pipeline can use the same safety pattern.

Usage::

    from pf_core.llm.safe_apply import GatherResult, safe_apply

    # Phase 1 — gather: read current state, ask LLM, build plan
    def gather_renames(items: list[Item]) -> GatherResult[dict[int, str]]:
        plan = llm_propose_renames([i.text for i in items])
        return GatherResult(
            target_count=len(items),
            target_texts=tuple(i.text for i in items),
            data=plan,
        )

    # Phase 2 — apply (possibly much later, after other transforms have run)
    def apply_renames(doc: str, gathered: GatherResult[dict[int, str]]) -> str:
        current = extract_items(doc)  # re-extract NOW, not at gather time
        result = safe_apply(
            gathered,
            current_texts=[i.text for i in current],
            apply_fn=lambda plan: rewrite_items(doc, current, plan),
            label="rename_items",
        )
        return result if result is not None else doc  # drift fallback
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pf_core.log import get_logger

_log = get_logger(__name__)


T = TypeVar("T")  # consumer-defined transform plan
R = TypeVar("R")  # consumer-defined apply_fn return type


@dataclass(frozen=True)
class GatherResult(Generic[T]):
    """Frozen snapshot of an LLM-driven transform plan plus the targets
    it was generated against.

    Pass to :func:`safe_apply` together with the *current* targets
    (re-extracted at apply time) so drift can be detected before the
    transform runs.

    Attributes:
        target_count: Number of targets the LLM saw at gather time.
        target_texts: Per-target identifying text the LLM saw, in the
            same order it'll be re-extracted at apply time. Tuple
            because it must be hashable / immutable so the snapshot
            can't be mutated between gather and apply.
        data: The transform plan itself — generic over the consumer's
            domain. Could be a dict, dataclass, list, or anything else
            the consumer's apply_fn knows how to consume.
    """

    target_count: int
    target_texts: tuple[str, ...]
    data: T


@dataclass(frozen=True)
class DriftReport:
    """What changed between gather and apply.

    ``has_drift`` is the single boolean callers usually want; the other
    fields give per-component detail for richer logging or per-case
    fallback decisions.
    """

    count_changed: bool
    gathered_count: int
    current_count: int
    drifted_indices: tuple[int, ...] = field(default_factory=tuple)

    @property
    def has_drift(self) -> bool:
        return self.count_changed or bool(self.drifted_indices)


def detect_drift(
    gathered: GatherResult[T], current_texts: Sequence[str]
) -> DriftReport:
    """Compare a gathered snapshot to the current target texts.

    When counts differ, the per-index text comparison is short-circuited
    (the indices wouldn't line up meaningfully); the report has
    ``count_changed=True`` and an empty ``drifted_indices``.

    When counts match but some texts differ, ``drifted_indices`` lists
    the indices where ``gathered.target_texts[i] != current_texts[i]``.
    """
    current_count = len(current_texts)
    if current_count != gathered.target_count:
        return DriftReport(
            count_changed=True,
            gathered_count=gathered.target_count,
            current_count=current_count,
        )
    drifted = tuple(
        i
        for i, (g, c) in enumerate(
            zip(gathered.target_texts, current_texts, strict=False)
        )
        if g != c
    )
    return DriftReport(
        count_changed=False,
        gathered_count=gathered.target_count,
        current_count=current_count,
        drifted_indices=drifted,
    )


def safe_apply(
    gathered: GatherResult[T],
    current_texts: Sequence[str],
    apply_fn: Callable[[T], R],
    *,
    label: str = "transform",
) -> R | None:
    """Run ``apply_fn(gathered.data)`` iff no drift was detected.

    On drift: emits a structured warning (event names
    ``safe_apply_drift_count`` / ``safe_apply_drift_texts``) and returns
    ``None``. The transform is NOT called on drift — that's the whole
    safety property.

    The caller decides what to do with the ``None`` (typically: fall
    back to the un-transformed input). A common pattern::

        new_md = safe_apply(gathered, current, lambda data: rewrite(md, data))
        return new_md if new_md is not None else md  # drift → unchanged

    Args:
        gathered: Snapshot from gather time, including the transform plan.
        current_texts: Targets re-extracted from the current state. Must
            be in the same order as ``gathered.target_texts`` so per-index
            comparison is meaningful.
        apply_fn: Callable that receives ``gathered.data`` and returns
            the consumer-specific result.
        label: Identifying label for log events. Lets multiple
            ``safe_apply`` call sites be distinguished in the log stream
            (e.g., ``"normalize"``, ``"rename_items"``).
            Defaults to ``"transform"``.

    Returns:
        ``apply_fn(gathered.data)`` on the no-drift path; ``None`` on
        the drift path.
    """
    drift = detect_drift(gathered, current_texts)
    if drift.has_drift:
        if drift.count_changed:
            _log.warning(
                "safe_apply_drift_count",
                label=label,
                gathered_count=drift.gathered_count,
                current_count=drift.current_count,
            )
        else:
            _log.warning(
                "safe_apply_drift_texts",
                label=label,
                drifted_count=len(drift.drifted_indices),
                drifted_indices=drift.drifted_indices,
                total=drift.current_count,
            )
        return None
    return apply_fn(gathered.data)
