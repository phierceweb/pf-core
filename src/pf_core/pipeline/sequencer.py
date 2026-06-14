"""Run a contiguous slice of an ordered, named pipeline.

``pipeline.cache`` owns stage ordering and cascade invalidation;
``pipeline.resume`` owns snapshot freshness. ``run_pipeline`` runs a
named slice of stages and nothing else — it owns zero pipeline logic,
only slice selection.

A :class:`Phase` is one stage: a ``name`` and a ``run(ctx)``. The
``ctx`` is opaque to the sequencer — it is threaded verbatim to each
phase, which reads and mutates whatever it needs. Phases communicate
through that context (or their own on-disk checkpoints), never through
return values.

Slice selection, in precedence order:

- ``start`` given      → begin there (single-phase / "from").
- else ``rerun_from``  → begin there regardless of freshness (the caller
  is responsible for having invalidated that stage's caches first, e.g.
  via :func:`pf_core.pipeline.cache.invalidate_caches`).
- else, if ``skip_fresh`` given → skip the *leading* run of phases the
  predicate reports fresh; begin at the first stale one (resume).
- ``stop_after`` given → halt after that phase (else run to the end).

Single phase == ``start == stop_after``.

Resume is opt-in and injected, not a protocol method. Pass
``skip_fresh`` — typically a closure over
:func:`pf_core.pipeline.resume.is_snapshot_valid` so freshness stays a
single concept across the pipeline family::

    from pf_core.pipeline.resume import is_snapshot_valid

    def fresh(phase):
        return is_snapshot_valid(checkpoint_for(phase.name), validator)

    run_pipeline(phases, ctx=ctx, skip_fresh=fresh)

Usage::

    class Extract:
        name = "extract"
        def run(self, ctx): ...

    ran = run_pipeline([Extract(), Transform(), Load()], ctx=ctx,
                        start="transform", stop_after="transform")
    # ran == ["transform"]
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class Phase(Protocol):
    """One pipeline stage.

    ``name`` is the slice handle — the value callers pass as ``start`` /
    ``stop_after`` / ``rerun_from`` and the key it shares with
    :class:`pf_core.pipeline.cache.StageDefinition` so ordering,
    invalidation, and execution all agree on stage identity.
    """

    name: str

    def run(self, ctx: object) -> None:
        """Do the stage's work against ``ctx``.

        Idempotent given the same upstream state — a phase may be re-run
        (``rerun_from``) or run in isolation (``start == stop_after``).
        """
        ...


class UnknownStageError(ValueError):
    """A ``start`` / ``stop_after`` / ``rerun_from`` name not in the phase list."""


def _index(names: list[str], stage: str | None, *, label: str) -> int | None:
    if stage is None:
        return None
    try:
        return names.index(stage)
    except ValueError:
        raise UnknownStageError(
            f"unknown {label} stage: {stage!r}. Valid: {tuple(names)}"
        ) from None


def run_pipeline(
    phases: list[Phase],
    *,
    ctx: object,
    start: str | None = None,
    stop_after: str | None = None,
    rerun_from: str | None = None,
    skip_fresh: Callable[[Phase], bool] | None = None,
) -> list[str]:
    """Run the selected contiguous slice of ``phases`` in order.

    Args:
        phases: Ordered pipeline phases. Names must be unique.
        ctx: Opaque context threaded verbatim to each ``phase.run``.
        start: Force the run to begin at this phase. Highest precedence.
        stop_after: Halt after this phase. Defaults to running to the end.
        rerun_from: Begin here regardless of freshness. Lower precedence
            than ``start``. The caller must have invalidated this stage's
            caches first (see :func:`pf_core.pipeline.cache.invalidate_caches`).
        skip_fresh: Optional resume predicate. When given (and neither
            ``start`` nor ``rerun_from`` is set), the leading run of
            phases for which ``skip_fresh(phase)`` is ``True`` is skipped
            and execution begins at the first phase it reports stale.
            Omit it (default) for explicit-slice-only behaviour. Wire it
            to :func:`pf_core.pipeline.resume.is_snapshot_valid` rather
            than inventing a parallel freshness concept.

    Returns:
        The names of the phases actually run, in order. Empty when the
        resume scan finds nothing stale.

    Raises:
        UnknownStageError: a stage name is not in ``phases``.
        ValueError: ``stop_after`` resolves before the start phase.
    """
    names = [p.name for p in phases]
    start_i = _index(names, start, label="start")
    stop_i = _index(names, stop_after, label="stop_after")
    rerun_i = _index(names, rerun_from, label="rerun_from")

    if start_i is not None:
        begin = start_i
    elif rerun_i is not None:
        begin = rerun_i
    elif skip_fresh is not None:
        # Resume: skip the leading run of fresh phases.
        begin = next(
            (i for i, p in enumerate(phases) if not skip_fresh(p)),
            len(phases),
        )
    else:
        begin = 0

    if begin >= len(phases):
        return []  # nothing stale / nothing to do

    end = stop_i if stop_i is not None else len(phases) - 1
    if end < begin:
        raise ValueError(
            f"stop_after={stop_after!r} resolves before start "
            f"{names[begin]!r} — empty run"
        )

    ran: list[str] = []
    for phase in phases[begin : end + 1]:
        phase.run(ctx)
        ran.append(phase.name)
    return ran


__all__ = ["Phase", "UnknownStageError", "run_pipeline"]
