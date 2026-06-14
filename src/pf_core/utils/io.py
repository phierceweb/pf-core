"""Atomic file-write helpers.

Crash-safe writes for caches, manifests, and run-record sidecars: the
target file is either the old content (if anything goes wrong during
the write) or the new content (after the write completes) — never a
truncated or partially-written hybrid.

Pattern: write to a sibling tempfile in the same directory, fsync it,
chmod to the target permission, then ``os.replace`` onto the final
path. ``os.replace`` is atomic when source and target are on the same
filesystem — that's why the tempfile lives next to the target rather
than in ``/tmp``.

Generalized from production pipeline use, where the same pattern was
duplicated across several caches, the pipeline manifest, and a
normalizer's checkpoint files.

Usage::

    from pf_core.utils.io import atomic_write_text, atomic_write_json

    atomic_write_json(Path("./manifest.json"), {"step": "extract", "n": 42})
    atomic_write_text(Path("./out.md"), "rendered markdown ...")
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# Default file permissions for newly-written files: rw-r--r-- (0o644).
# tempfile.mkstemp creates with 0o600 (owner-only), which surprises
# consumers who want their cache/manifest readable by other tools.
DEFAULT_MODE = 0o644


def atomic_write_text(
    path: Path | str,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int = DEFAULT_MODE,
) -> None:
    """Write ``content`` to ``path`` atomically.

    The target file's contents are either the old (pre-call) value or
    the new value — never a torn write. If the write fails partway
    through, the target file is left untouched and any temp file is
    cleaned up.

    Args:
        path: Target file path. Parent directory must exist (no silent
            ``mkdir`` — atomic-write isn't a "create my path for me"
            tool; that decision belongs to the caller).
        content: String content to write.
        encoding: File encoding for the write. Defaults to ``utf-8``.
        mode: File permission bits applied before the rename, so the
            final file has the right perms atomically. Defaults to
            ``0o644`` — readable by all, writable only by owner.
    """
    path = Path(path)
    parent = path.parent
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path | str,
    obj: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    ensure_ascii: bool = False,
    mode: int = DEFAULT_MODE,
) -> None:
    """Write ``obj`` to ``path`` as JSON, atomically.

    Thin wrapper over :func:`atomic_write_text` + :func:`json.dumps`
    with sidecar-friendly defaults: 2-space indent (readable in diff
    tools), insertion order preserved, unicode preserved (not escaped
    to ``\\uXXXX``).

    A :class:`TypeError` from ``json.dumps`` (non-serializable input)
    propagates without touching the target file — the existing file is
    safe even if the new payload is malformed.

    Args:
        path: Target file path. Parent must exist.
        obj: Anything ``json.dumps`` accepts.
        indent: Passed to ``json.dumps``. Defaults to 2 (pretty-printed,
            diffable). Pass ``None`` for compact single-line output.
        sort_keys: Passed to ``json.dumps``. Defaults to ``False`` —
            insertion order preserved. Set ``True`` for diff-stable
            output regardless of input dict ordering.
        ensure_ascii: Passed to ``json.dumps``. Defaults to ``False``
            so non-ASCII strings stay readable in the JSON file.
        mode: File permission bits. Defaults to ``0o644``.
    """
    text = json.dumps(
        obj, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii
    )
    atomic_write_text(path, text, mode=mode)
