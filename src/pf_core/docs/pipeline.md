# Pipeline Ergonomics

Generic patterns for multi-stage pipelines: stamp run records, snapshot output for later comparison, diff baselines, invalidate caches by stage, resume from upstream snapshots, run a named slice of ordered phases.

Promoted as a coherent group from a multi-stage pipeline's re-test ergonomics work. pf-core's API parameterizes the consumer-specific filenames / dir names / stage names so any consumer with a multi-stage pipeline can use the same machinery.

The six modules are independent and composable — a consumer can adopt just `run_record` for traceable runs, or layer on `baseline` + `baseline_diff` for change detection, or add `cache` + `resume` for incremental re-runs.

## Modules at a glance

| Module | Purpose |
|---|---|
| [run_record](#run_record) | Stamp `<output_dir>/<filename>` with resolved config + input SHA-256 + timestamps + counts |
| [baseline](#baseline) | Snapshot a pipeline's output dir for later comparison |
| [baseline_diff](#baseline_diff) | Structured diff between a saved baseline and the current live output |
| [cache](#cache) | Stage-cascade cache invalidation with structural-vs-content-keyed split |
| [resume](#resume) | Snapshot validity check + read for downstream-phase resume |
| [sequencer](#sequencer) | Run a contiguous slice of an ordered, named pipeline |

For consumers building a Typer CLI on top of these, see [`cli-subcommands.md`](cli-subcommands.md) for the `make_invalidate_subcommand` and `make_baseline_subcommand_group` factories.

---

## run_record

Stamp `<output_dir>/<filename>` with the resolved configuration and input fingerprint that produced the result. A one-line diff between two run records shows exactly what config differs between two outputs.

```python
from pf_core.pipeline.run_record import write_run_record, read_run_record, file_sha256

write_run_record(
    output_dir,
    input_path=src,
    version="1.0.0",
    preset="default",
    resolved_flags={"workers": 4, "model": "haiku"},
    started_at="2026-06-14T10:00:00Z",
    finished_at="2026-06-14T10:03:12Z",
    section_count=12,
    image_count=47,
    extra={"backend": "default"},
)

record = read_run_record(output_dir)
# {"version": "1.0.0", "preset": "default", "input": "...", "input_sha256": "...",
#  "started_at": "...", "finished_at": "...", "resolved_flags": {...},
#  "section_count": 12, "image_count": 47, "backend": "default"}
```

`started_at` / `finished_at` are required (ISO-8601 strings the caller supplies). `section_count` / `image_count` are standard optional kwargs. Filename is configurable (default `run.json`); each consumer can use its own (e.g., `.myproject-run.json`). Any further consumer-specific metadata goes in `extra={...}` and is flattened into the top-level JSON.

`file_sha256(path)` is also exported for use outside the run-record itself — bounded-memory streaming hash (1 MiB chunks) for large input files.

---

## baseline

Snapshot a pipeline's live output directory into `<output_dir>/<baselines_dir>/<label>/` for later comparison. Cache files are NOT copied — only the result artifacts (consolidated md, sections, INDEX.md, run record).

```python
from pf_core.pipeline.baseline import save_baseline, list_baselines

record = save_baseline(output_dir, label="pre-cleanup-rewrite")
# BaselineRecord(label="pre-cleanup-rewrite", path=..., version=..., ...)

records = list_baselines(output_dir)
# [BaselineRecord(...), BaselineRecord(...)]
```

The `auto_snapshot_on_version_change()` variant fires when `current_version` differs from the previous run's recorded version — useful as the first call in a pipeline dispatcher.

### BaselineConfig

Filename / directory conventions are parameterized via `BaselineConfig`:

```python
from pf_core.pipeline.baseline import BaselineConfig, save_baseline

cfg = BaselineConfig(
    run_record_filename=".myproject-run.json",
    sections_dir_name="sections",
    index_file_name="INDEX.md",
    baselines_dir_name=".baselines",
    consolidated_md_pattern="{stem}.md",  # {stem} = source input filename stem
)
save_baseline(output_dir, label="...", config=cfg)
```

Defaults match a generic pipeline (above). Override per-field for consumers that use different conventions.

---

## baseline_diff

Structured diff between a saved baseline and the current live output.

```python
from pf_core.pipeline.baseline_diff import diff_baseline

report = diff_baseline(output_dir, label="pre-cleanup-rewrite")
# DiffReport(
#     baseline_label="pre-cleanup-rewrite", baseline_path=..., current_path=...,
#     run_record=RunRecordDelta(changed_fields={...}),
#     sections=SectionSetDelta(added=[...], removed=[...], renamed=[...]),
#     body_changes=[LineCountDelta(path=..., plus=N, minus=M), ...],
# )
```

Three diff layers, all in the returned `DiffReport`:

1. **Run-record field-level diff** — dotted paths into the run record JSON (e.g. `resolved_flags.transform`).
2. **Section filename set diff** — added / removed / renamed.
3. **Per-section line-count rollup** — `+N -M` per file present on both sides.

### Rename detection

Conservative: exact body sha256 match (similarity 1.0) OR same-folder
+ Levenshtein basename ≤ 4 + body similarity ≥ 0.8. The bias is toward
"added + removed" over false "renamed" claims — easier to audit a clean add/remove pair than a misidentified rename.

---

## cache

Stage-cascade cache invalidation. Re-run from a specific stage, deleting that stage's cache plus every downstream stage's structural files — while preserving downstream content-keyed caches that self-invalidate via their own key.

### StageDefinition + StageRegistry

Describe your pipeline as an ordered list of stages, each with two classes of file template:

```python
from pf_core.pipeline.cache import StageDefinition, StageRegistry

REGISTRY = StageRegistry(stages=(
    StageDefinition(
        name="extract",
        structural_files=("{stem}.raw.md",),
        content_keyed_files=(),
    ),
    StageDefinition(
        name="enrich",
        structural_files=("{stem}.enrich.md",),
        content_keyed_files=(".enrich-cache",),  # content-hash-keyed, preserved across cascade
    ),
    StageDefinition(
        name="split",
        structural_files=("sections", "INDEX.md", "{stem}.md"),
        content_keyed_files=(),
    ),
))
```

| File class | Behavior |
|---|---|
| `structural_files` | Mtime-gated outputs. Cascade busts them on every downstream re-run. |
| `content_keyed_files` | Caches keyed by content hash. Self-invalidating, so PRESERVED across cascade. Only busted when the user explicitly targets the owning stage. |

### invalidate_caches

```python
from pf_core.pipeline.cache import invalidate_caches

deleted = invalidate_caches(
    output_dir,
    stage="enrich",
    registry=REGISTRY,
    source_stem="my-doc",
)
# Deleted: enrich-cache + every downstream stage's structural files
# Preserved: extract structural files, enrich structural file from extract
```

`files_to_invalidate(...)` is the dry-run variant — returns the path list without deleting.

File templates use `{stem}` as the only placeholder (the source input's filename stem). Other path components are literal — `images`, `sections`, `.enrich-cache/`, etc.

### Cascade rule, restated

Walking from the target stage forward in the registry:

- **Target stage's own** content-keyed files → busted (the user asked for it)
- **Every downstream stage's** structural files → busted (they depend on upstream)
- **Every downstream stage's** content-keyed files → preserved (self-invalidating)

---

## resume

Skip downstream work when an upstream snapshot is fresh enough to reuse.

```python
from pathlib import Path
from pf_core.pipeline.resume import SnapshotValidator, try_resume_from_snapshot

validator = SnapshotValidator(
    upstream_files=(input_path,),
    upstream_dirs_glob=((output_dir / ".enrich-cache", "*.json"),),
    run_record_path=output_dir / "run.json",
    flag_keys=("model", "transform_enabled"),
    current_flags={"model": "haiku", "transform_enabled": True},
)

snapshot_text = try_resume_from_snapshot(
    output_dir / f"{stem}.post-transform.md",
    validator,
)
if snapshot_text is not None:
    # Snapshot is valid — skip extraction + transform phases
    return hydrate_from_text(snapshot_text)
```

Validity rules (any failure → `is_snapshot_valid` returns False):

1. The snapshot file exists.
2. Its mtime is ≥ every `upstream_files` mtime.
3. Its mtime is ≥ the latest file matching any `upstream_dirs_glob` spec.
4. If `run_record_path` is set, the file exists, parses as JSON, and its `resolved_flags` match `current_flags` for every key in `flag_keys`.

`try_resume_from_snapshot` returns the snapshot's raw text content (or `None`); the consumer hydrates its own result type from that text. The lower-level `is_snapshot_valid(snapshot_path, validator)` is exposed separately for callers that just want the bool.

## sequencer

The executor for the other pipeline modules. `cache` owns stage **ordering**, `resume` owns **freshness**; `run_pipeline` runs a named slice of stages. It owns zero pipeline logic — it selects a contiguous slice and runs it.

A `Phase` is one stage — a `name` and a `run(ctx)`. The `ctx` is opaque to the sequencer; it is threaded verbatim to every phase. Phases communicate through that context or their own on-disk checkpoints, never through return values.

```python
from pf_core.pipeline.sequencer import run_pipeline

class Extract:
    name = "extract"
    def run(self, ctx): ...

class Transform:
    name = "transform"
    def run(self, ctx): ...

class Load:
    name = "load"
    def run(self, ctx): ...

phases = [Extract(), Transform(), Load()]

run_pipeline(phases, ctx=ctx)                                # ["extract","transform","load"]
run_pipeline(phases, ctx=ctx, start="transform")             # ["transform","load"]
run_pipeline(phases, ctx=ctx, stop_after="transform")        # ["extract","transform"]
run_pipeline(phases, ctx=ctx, start="transform",
             stop_after="transform")                         # ["transform"]  (single phase)
```

`run_pipeline` returns the names of the phases actually run, in order.

### Slice selection

Precedence, highest first:

1. **`start`** — begin at this phase. Single-phase run is `start == stop_after`.
2. **`rerun_from`** — begin here regardless of freshness. The caller must have invalidated this stage's caches first (use [`invalidate_caches`](#invalidate_caches)).
3. **`skip_fresh`** — resume: skip the *leading* run of phases the predicate reports fresh; begin at the first stale one. Only the leading contiguous fresh prefix is skipped — a fresh phase after the first stale one still runs.
4. Otherwise run every phase.

`stop_after` independently clamps the end (default: run to the last phase). `stop_after` resolving before the chosen start is a `ValueError`; an unknown `start` / `stop_after` / `rerun_from` name is `UnknownStageError` (a `ValueError` subclass).

### Resume is injected, not a protocol method

`Phase` deliberately has **no `is_fresh`**. Freshness already lives in [`resume`](#resume); the sequencer must not grow a second concept. When a consumer wants resume, it passes `skip_fresh` — a predicate that delegates to `is_snapshot_valid`:

```python
from pf_core.pipeline.resume import is_snapshot_valid

def fresh(phase):
    return is_snapshot_valid(checkpoint_for(phase.name), validator_for(phase.name))

run_pipeline(phases, ctx=ctx, skip_fresh=fresh)
```

Omit `skip_fresh` (the default) for explicit-slice-only pipelines whose phases handle their own internal resume inside `run()`.

### Stage identity

A phase's `name` is the slice handle *and* the key it shares with [`StageDefinition`](#stagedefinition--stageregistry) in `cache`. Use the same string in both so ordering, invalidation, and execution agree on what a stage is.

## See also

- [`cli-subcommands.md`](cli-subcommands.md) — Typer factories that wrap baseline / baseline_diff / cache for consumer CLIs
- [`io.md`](io.md) — atomic write helpers used internally by `run_record` and friends
- [`logging.md`](logging.md) — every pipeline module uses structured logging via `pf_core.log`
