# Gather/Apply with Drift Detection

Safety net for LLM-driven transforms that run gather and apply as separate phases.

The pattern: an LLM analyzes a snapshot of some data (the current list of records, the current set of item IDs, the current row order, …) and produces a transform plan. By the time you APPLY the plan, the underlying data may have drifted — other transforms ran between gather and apply, the input was edited, etc. Blindly applying the plan against the new state can mis-target — relabel the wrong record, mutate the wrong row, drop the wrong item.

`safe_apply` is the safety net: gather captures the targets it saw alongside its plan; apply re-extracts the current targets and compares. If counts or texts drifted, the apply skips with a structured warning rather than mis-applying.

## Usage

```python
from pf_core.llm.safe_apply import GatherResult, safe_apply

# Phase 1 — gather: read current state, ask LLM, build plan
def gather_relabels(records: list[Record]) -> GatherResult[dict[int, str]]:
    plan = llm_propose_relabels([r.text for r in records])
    return GatherResult(
        target_count=len(records),
        target_texts=tuple(r.text for r in records),
        data=plan,
    )

# Phase 2 — apply (possibly much later, after other transforms have run)
def apply_relabels(text: str, gathered: GatherResult[dict[int, str]]) -> str:
    current = extract_records(text)  # re-extract NOW, not at gather time
    result = safe_apply(
        gathered,
        current_texts=[r.text for r in current],
        apply_fn=lambda plan: rewrite_labels(text, current, plan),
        label="relabel_records",
    )
    return result if result is not None else text  # drift fallback
```

The transform is NOT called when drift is detected — that's the whole safety property. The caller decides what to do with the `None` (typically: fall back to the un-transformed input).

## How drift is detected

Two checks, in order:

1. **Count.** `len(current_texts) != gathered.target_count` → drift. Per-index text comparison is short-circuited (the indices wouldn't line up meaningfully).
2. **Per-index text.** When counts match, compare each `gathered.target_texts[i]` to `current_texts[i]`. Any mismatch counts as drift; `drifted_indices` lists the affected indices.

Positional comparison only — if a target was inserted at index 0, everything else "drifts" by one index. That's correct behavior; the inserted target invalidates the snapshot.

## API

### GatherResult

```python
@dataclass(frozen=True)
class GatherResult(Generic[T]):
    target_count: int
    target_texts: tuple[str, ...]
    data: T
```

| Attribute | Description |
|---|---|
| `target_count` | How many targets the LLM saw at gather time |
| `target_texts` | Per-target identifying text the LLM saw, in extraction order. Tuple so it's immutable — the snapshot can't be mutated between gather and apply |
| `data` | The transform plan itself, generic over consumer's domain (dict, dataclass, list, anything) |

### safe_apply

```python
safe_apply(
    gathered: GatherResult[T],
    current_texts: Sequence[str],
    apply_fn: Callable[[T], R],
    *,
    label: str = "transform",
) -> R | None
```

Returns `apply_fn(gathered.data)` on the no-drift path, `None` on the drift path. On drift, emits one of two structured warning events:

| Event | When |
|---|---|
| `safe_apply_drift_count` | `gathered.target_count != len(current_texts)` |
| `safe_apply_drift_texts` | counts match, but some text-at-index differs |

Both events include the `label` kwarg so multiple call sites in the same log stream are distinguishable (e.g. `"relabel_records"`, `"reorder_rows"`).

### detect_drift / DriftReport

For callers who want richer drift handling than "skip on any drift," `detect_drift` is exposed separately:

```python
report = detect_drift(gathered, current_texts)
if report.has_drift:
    if report.count_changed:
        # decide: fall back? attempt partial apply? bail?
        ...
    else:
        # decide based on which indices drifted
        recoverable = all(i > 5 for i in report.drifted_indices)
        ...
```

`DriftReport` carries `count_changed`, `gathered_count`, `current_count`, `drifted_indices`, and the convenience `has_drift` property.

## When NOT to use this

- **Single-phase transforms** where gather and apply happen in the same function with no chance of drift. The overhead of building a `GatherResult` only pays off when there's real time / state separation between the phases.
- **Idempotent transforms** where mis-application is harmless. `safe_apply` is for transforms whose meaning depends on alignment with the targets the LLM saw.
- **Transforms over data the LLM didn't analyze.** If the targets at apply time aren't a re-extraction of what the LLM saw, drift detection is meaningless.
