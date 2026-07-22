# Markdown Export

Turn a system-of-record (a database, a parsed corpus) into a tree of markdown files for RAG ingestion or portable review — **incrementally**, so a downstream index re-imports only what actually changed.

Foundation module, stdlib-only (`pf_core.export`), built on [atomic file writes](io.md). No extra required.

## Why

Exporting a corpus to per-file markdown on every run is cheap to write but expensive downstream: if every file is rewritten, every file looks "changed" to git, rsync, or a semantic-search index that re-embeds on mtime. `MarkdownExporter` does the necessary bookkeeping:

- **write-if-changed** — a file is rewritten only when its content differs, so unchanged files keep their mtime and the delta stays small.
- **atomic** — each write goes through `atomic_write_text`, so a crash mid-export never leaves a torn file.
- **prune** — files the exporter previously produced but no longer yields are deleted, scoped to the managed suffixes (`.md` by default) and only within the directories it writes into. Hand-placed files and unrelated directories are never touched.

A markdown-export orchestrator base any project can subclass.

## Usage

Subclass and implement `iter_artifacts`, yielding `(relative_path, content)` pairs:

```python
from pf_core.export import MarkdownExporter, yaml_frontmatter


class RecordExporter(MarkdownExporter):
    def __init__(self, records):
        self._records = records

    def iter_artifacts(self):
        for a in self._records:
            front = yaml_frontmatter({
                "slug": a["slug"],
                "tier": a["tier"],
                "count": a["count"],
                "active": a["active"],
                "tags": a["tags"],   # list -> block sequence
            })
            body = f"# {a['name']}\n\n{a['summary']}\n"
            yield f"records/{a['slug']}.md", front + body


result = RecordExporter(rows).export("./export")
print(result.written, result.unchanged, result.pruned)
# e.g. 3 written, 184 unchanged, 1 pruned
```

`export(root)` creates `root` and parent directories as needed, writes/skips each artifact, prunes orphans, and returns an `ExportResult`.

## API

### `MarkdownExporter`

| Member | Purpose |
|---|---|
| `managed_suffixes: tuple[str, ...] = (".md",)` | File suffixes this exporter owns. Only these are eligible for pruning — override to also manage `.json`, etc. |
| `force_prune_dirs: tuple[str, ...] = ()` | Root-relative directories always in prune scope. Use for stable subdirectories (`("sections",)`) whose orphans must go even when a run yields zero artifacts into them — by default such a directory keeps its orphans forever. |
| `iter_artifacts() -> Iterator[tuple[str, str]]` | **Subclass responsibility.** Yield `(relative_path, content)`. Paths are POSIX-style, relative to the export root. |
| `export(root) -> ExportResult` | Write all artifacts incrementally, then prune. Raises `ValueError` if a path is absolute or escapes `root` via `..`. |
| `check(root) -> list[str]` | Dry run: sorted relative paths `export` would touch — missing, content-stale, and prunable orphans. Empty list ⇒ the tree is exactly what `export` would produce. Writes nothing. |

### `ExportResult`

Frozen dataclass: `written`, `unchanged`, `pruned` (ints) and `paths` (the produced relative paths, sorted).

### `yaml_frontmatter(fields: dict) -> str`

Render a dict as a YAML frontmatter block delimited by `---` lines. A deliberately small, safe subset suited to RAG faceting:

- `None` values and empty lists are **omitted**.
- `bool` → `true` / `false`; `int` / `float` are bare.
- lists → block sequences (`key:` then `  - item` lines).
- string scalars are bare when unambiguous, and double-quoted (escaping `"` and `\`) when they contain YAML-significant characters, look numeric, or collide with a reserved word — so `"90210"` and `"- dash"` round-trip as strings.

```python
yaml_frontmatter({"slug": "widget-a", "name": "Widget A", "tier": "standard",
                  "count": 289, "tags": ["hardware"], "note": None})
# ---
# slug: widget-a
# name: Widget A
# tier: standard
# count: 289
# tags:
#   - hardware
# ---
```

## Prune scope (precise)

Pruning is intentionally narrow to avoid deleting files the exporter doesn't own. A file is pruned only if **all** hold:

1. its suffix is in `managed_suffixes`,
2. it lives directly in a directory that received at least one produced artifact this run, and
3. it was not itself produced this run.

So a `notes.txt` beside produced `.md` files survives (fails #1), and a `keep.md` in a directory the exporter never wrote into survives (fails #2) — unless that directory is listed in `force_prune_dirs`, which adds it to scope regardless of what this run produced.

## Committed generated trees

When the exported tree is committed (docs generated from data), wire `check` as the freshness gate — pre-commit and CI run it and fail on a non-empty list, so the tree can never drift from its source:

```python
stale = RecordExporter(rows).check("./export")
if stale:
    print("\n".join(stale))
    raise SystemExit(1)
```
