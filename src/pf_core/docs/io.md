# Atomic File Writes

Crash-safe writes for caches, manifests, run-record sidecars, and any other file where a torn write (process killed mid-write, disk full, permissions error halfway through) would leave the consumer in a worse state than not writing at all.

The target file is either the old content or the new content — never a partial hybrid.

## Usage

```python
from pf_core.utils.io import atomic_write_text, atomic_write_json

# Write a markdown file. Crash partway through → original survives.
atomic_write_text(Path("./out.md"), "rendered markdown ...")

# Write a JSON sidecar with diff-friendly defaults (2-space indent,
# unicode preserved, insertion order preserved).
atomic_write_json(Path("./manifest.json"), {"step": "extract", "n": 42})
```

## How it works

The pattern: write to a sibling tempfile in the same directory, fsync it, chmod to the target permission, then `os.replace` onto the final path.

`os.replace` is atomic when source and target are on the same filesystem — that's why the tempfile lives next to the target rather than in `/tmp`.

If the write fails partway through, the temp file is unlinked and the original (if any) is untouched.

## Functions

### atomic_write_text

```python
atomic_write_text(
    path: Path | str,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o644,
) -> None
```

| Parameter | Default | Description |
|---|---|---|
| `path` | required | Target file path. Parent directory must exist. |
| `content` | required | String content to write. |
| `encoding` | `"utf-8"` | File encoding. |
| `mode` | `0o644` | Permission bits applied before the rename — readable by all, writable only by owner. tempfile's default is `0o600`, which surprises consumers expecting the cache file to be readable by other tools. |

### atomic_write_json

```python
atomic_write_json(
    path: Path | str,
    obj: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    ensure_ascii: bool = False,
    mode: int = 0o644,
) -> None
```

Thin wrapper over `atomic_write_text` + `json.dumps` with sidecar-friendly defaults.

| Parameter | Default | Description |
|---|---|---|
| `indent` | `2` | Pretty-printed for diff readability. Pass `None` for compact single-line. |
| `sort_keys` | `False` | Insertion order preserved. Set `True` for diff-stable output regardless of input dict ordering. |
| `ensure_ascii` | `False` | Non-ASCII strings stay readable (not escaped to `\uXXXX`). |
| `mode` | `0o644` | See above. |

A `TypeError` from `json.dumps` (non-serializable input) propagates without touching the target file — the existing file is safe even if the new payload is malformed.

## What this is not

- **Not a `mkdir` wrapper.** The parent directory must exist; the caller decides where things go. Atomic-write is only about atomicity.
- **Not a database transaction.** Atomicity here is per-file. Two related files written via two `atomic_write_*` calls are independently atomic; if you crash between them, you'll have one new + one old. For multi-file consistency, write a single manifest atomically and treat it as the source of truth.
- **Not lock-aware.** Two processes writing the same file simultaneously will both succeed via separate tempfiles; the last `os.replace` wins. If you need write coordination, layer file locks on top.

## See also

- [Markdown export](export.md) — `pf_core.export.MarkdownExporter` builds on `atomic_write_text` for incremental, crash-safe markdown tree exports (write-if-changed + scoped orphan prune).
- `pf_core.pipeline.run_record` — uses this for the `<output_dir>/run.json` sidecar; also exports `file_sha256` for bounded-memory file fingerprinting (typical companion when stamping a run record).
