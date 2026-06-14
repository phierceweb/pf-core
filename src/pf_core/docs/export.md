# Markdown Export

Turn a system-of-record (a database, a parsed corpus) into a tree of markdown files for RAG ingestion or portable review — **incrementally**, so a downstream index re-imports only what actually changed.

Foundation module, stdlib-only (`pf_core.export`), built on [atomic file writes](io.md). No extra required.

## Why

Exporting a corpus to per-file markdown on every run is cheap to write but expensive downstream: if every file is rewritten, every file looks "changed" to git, rsync, or a semantic-search index that re-embeds on mtime. `MarkdownExporter` does the necessary bookkeeping:

- **write-if-changed** — a file is rewritten only when its content differs, so unchanged files keep their mtime and the delta stays small.
- **atomic** — each write goes through `atomic_write_text`, so a crash mid-export never leaves a torn file.
- **prune** — files the exporter previously produced but no longer yields are deleted, scoped to the managed suffixes (`.md` by default) and only within the directories it writes into. Hand-placed files and unrelated directories are never touched.

Generalized from a production markdown-export orchestrator so any consumer can subclass it.

## Usage

Subclass and implement `iter_artifacts`, yielding `(relative_path, content)` pairs:

```python
from pf_core.export import MarkdownExporter, yaml_frontmatter


class CanonExporter(MarkdownExporter):
    def __init__(self, artists):
        self._artists = artists

    def iter_artifacts(self):
        for a in self._artists:
            front = yaml_frontmatter({
                "slug": a["slug"],
                "tier": a["tier"],
                "plays": a["plays"],
                "loved": a["loved"],
                "aliases": a["aliases"],   # list -> block sequence
            })
            body = f"# {a['name']}\n\n{a['why']}\n"
            yield f"artists/{a['slug']}.md", front + body


result = CanonExporter(rows).export("./export")
print(result.written, result.unchanged, result.pruned)
# e.g. 3 written, 184 unchanged, 1 pruned
```

`export(root)` creates `root` and parent directories as needed, writes/skips each artifact, prunes orphans, and returns an `ExportResult`.

## API

### `MarkdownExporter`

| Member | Purpose |
|---|---|
| `managed_suffixes: tuple[str, ...] = (".md",)` | File suffixes this exporter owns. Only these are eligible for pruning — override to also manage `.json`, etc. |
| `iter_artifacts() -> Iterator[tuple[str, str]]` | **Subclass responsibility.** Yield `(relative_path, content)`. Paths are POSIX-style, relative to the export root. |
| `export(root) -> ExportResult` | Write all artifacts incrementally, then prune. Raises `ValueError` if a path is absolute or escapes `root` via `..`. |

### `ExportResult`

Frozen dataclass: `written`, `unchanged`, `pruned` (ints) and `paths` (the produced relative paths, sorted).

### `yaml_frontmatter(fields: dict) -> str`

Render a dict as a YAML frontmatter block delimited by `---` lines. A deliberately small, safe subset suited to RAG faceting:

- `None` values and empty lists are **omitted**.
- `bool` → `true` / `false`; `int` / `float` are bare.
- lists → block sequences (`key:` then `  - item` lines).
- string scalars are bare when unambiguous, and double-quoted (escaping `"` and `\`) when they contain YAML-significant characters, look numeric, or collide with a reserved word — so `"90210"` and `"- dash"` round-trip as strings.

```python
yaml_frontmatter({"slug": "fugazi", "tier": "foundational", "plays": 289,
                  "aliases": ["Fugazi"], "note": None})
# ---
# slug: fugazi
# tier: foundational
# plays: 289
# aliases:
#   - Fugazi
# ---
```

## Prune scope (precise)

Pruning is intentionally narrow to avoid deleting files the exporter doesn't own. A file is pruned only if **all** hold:

1. its suffix is in `managed_suffixes`,
2. it lives directly in a directory that received at least one produced artifact this run, and
3. it was not itself produced this run.

So a `notes.txt` beside produced `.md` files survives (fails #1), and a `keep.md` in a directory the exporter never wrote into survives (fails #2).
