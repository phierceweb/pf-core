"""Incremental markdown export — write-if-changed + orphan prune.

A :class:`MarkdownExporter` turns a system-of-record (a database, a parsed
corpus, …) into a tree of markdown files for RAG ingestion or portable
review. Subclasses implement :meth:`MarkdownExporter.iter_artifacts`, yielding
``(relative_path, content)`` pairs; the base handles the durable, incremental
write:

- **write-if-changed** — a file is rewritten only when its content actually
  differs, so unchanged files keep their mtime and a downstream index (a
  search index, git, an rsync) re-imports only the delta.
- **atomic** — each write goes through :func:`pf_core.utils.io.atomic_write_text`,
  so a crash mid-export never leaves a torn file.
- **prune** — files the exporter previously produced but no longer yields are
  deleted, but only within the directories it writes into and only for the
  managed suffixes (``.md`` by default). A hand-placed ``notes.txt`` or a file
  in an unrelated directory is never touched.

Incremental write + per-directory prune; any project subclasses it.

Usage::

    class RecordExporter(MarkdownExporter):
        def __init__(self, rows): self._rows = rows
        def iter_artifacts(self):
            for r in self._rows:
                yield f"records/{r['slug']}.md", render(r)

    result = RecordExporter(rows).export("./export")
    print(result.written, result.unchanged, result.pruned)
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pf_core.utils.io import atomic_write_text

# YAML indicator characters that, when they *start* a scalar, change how the
# value is parsed — a bare scalar leading with one of these must be quoted.
_YAML_INDICATORS = set("-?:,[]{}#&*!|>'\"%@`")

# Bare scalars equal to one of these (case-insensitively) would parse as a
# bool / null / empty rather than the intended string.
_YAML_RESERVED = {"", "true", "false", "null", "yes", "no", "on", "off", "~"}


@dataclass(frozen=True)
class ExportResult:
    """Outcome of an :meth:`MarkdownExporter.export` run.

    Attributes:
        written: Files created or rewritten because their content changed.
        unchanged: Files left untouched (content identical, mtime preserved).
        pruned: Orphaned files deleted (previously produced, no longer yielded).
        paths: The relative paths produced this run, sorted.
    """

    written: int
    unchanged: int
    pruned: int
    paths: list[str]


class MarkdownExporter:
    """Base class for incremental, atomic markdown tree exports.

    Subclass and implement :meth:`iter_artifacts`. Override
    :attr:`managed_suffixes` to own more than ``.md`` files for pruning, and
    :attr:`force_prune_dirs` for stable subdirectories whose orphans must be
    pruned even in a run that yields no artifacts into them.
    """

    #: File suffixes this exporter owns — only these are eligible for pruning.
    managed_suffixes: tuple[str, ...] = (".md",)

    #: Root-relative directories always in prune scope. By default a directory
    #: is pruned only when this run produced into it, so a section that goes
    #: from N artifacts to zero would keep its orphans forever.
    force_prune_dirs: tuple[str, ...] = ()

    def iter_artifacts(self) -> Iterator[tuple[str, str]]:
        """Yield ``(relative_path, content)`` for every artifact to write.

        Subclass responsibility. Paths are POSIX-style, relative to the export
        root; ``content`` is the full file body.
        """
        raise NotImplementedError(
            "Subclasses must implement iter_artifacts() -> "
            "Iterator[tuple[str, str]]"
        )

    def export(self, root: str | Path) -> ExportResult:
        """Write every artifact under ``root``, incrementally, then prune.

        Creates parent directories as needed. Rewrites a file only when its
        content differs (preserving mtimes otherwise). After writing, deletes
        managed-suffix files in the produced directories that were not yielded
        this run.

        Args:
            root: Destination directory (created if missing).

        Returns:
            An :class:`ExportResult` tallying written / unchanged / pruned.

        Raises:
            ValueError: If an artifact path is absolute or escapes ``root``
                via ``..``.
        """
        root = Path(root)
        artifacts = [(self._safe_relpath(rel), content)
                     for rel, content in self.iter_artifacts()]

        written = unchanged = 0
        for rel, content in artifacts:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if _write_if_changed(target, content):
                written += 1
            else:
                unchanged += 1

        produced = {rel for rel, _ in artifacts}
        pruned = 0
        for orphan in self._orphans(root, produced):
            orphan.unlink()
            pruned += 1

        return ExportResult(
            written=written,
            unchanged=unchanged,
            pruned=pruned,
            paths=sorted(produced),
        )

    def check(self, root: str | Path) -> list[str]:
        """Report what :meth:`export` would change, writing nothing.

        Returns the sorted root-relative paths that are out of date: missing
        files, files whose content differs from what this run would render,
        and managed-suffix orphans :meth:`export` would prune. An empty list
        means the tree on disk is exactly what :meth:`export` would produce —
        the freshness gate for committing generated trees.
        """
        root = Path(root)
        artifacts = [(self._safe_relpath(rel), content)
                     for rel, content in self.iter_artifacts()]

        stale: set[str] = set()
        for rel, content in artifacts:
            target = root / rel
            try:
                if target.read_text(encoding="utf-8") != content:
                    stale.add(rel)
            except OSError:
                stale.add(rel)

        produced = {rel for rel, _ in artifacts}
        for orphan in self._orphans(root, produced):
            stale.add(orphan.relative_to(root).as_posix())
        return sorted(stale)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _safe_relpath(rel: str) -> str:
        """Validate and normalize a yielded relative path.

        Returns the POSIX-normalized relative path. Rejects absolute paths and
        any path that would escape the export root via ``..``.
        """
        pure = PurePosixPath(rel)
        if pure.is_absolute() or rel.startswith("/"):
            raise ValueError(f"artifact path must be relative, got {rel!r}")
        if ".." in pure.parts:
            raise ValueError(f"artifact path must not contain '..', got {rel!r}")
        return pure.as_posix()

    def _orphans(self, root: Path, produced: Iterable[str]) -> list[Path]:
        """Managed-suffix orphans in prune scope, without touching them.

        Prune scope is deliberately narrow: only files whose suffix is in
        :attr:`managed_suffixes`, located directly in a directory that received
        at least one produced artifact (plus :attr:`force_prune_dirs`), and
        not themselves produced this run.
        """
        produced = set(produced)
        scope_dirs = {(root / rel).parent for rel in produced}
        scope_dirs.update(root / d for d in self.force_prune_dirs)
        keep = {(root / rel).resolve() for rel in produced}

        orphans: list[Path] = []
        for d in scope_dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix not in self.managed_suffixes:
                    continue
                if f.resolve() in keep:
                    continue
                orphans.append(f)
        return orphans


# ---------------------------------------------------------------------------
# YAML frontmatter
# ---------------------------------------------------------------------------


def yaml_frontmatter(fields: dict) -> str:
    """Render a dict as a YAML frontmatter block (with ``---`` delimiters).

    Emits a minimal, safe subset of YAML suited to RAG faceting:

    - ``None`` values and empty lists are omitted entirely.
    - ``bool`` renders as ``true`` / ``false``; ``int`` / ``float`` bare.
    - lists render as block sequences (``key:`` then ``  - item`` lines).
    - string scalars are emitted bare when unambiguous, and double-quoted
      (with ``"`` and ``\\`` escaped) when they contain YAML-significant
      characters, look numeric, or collide with a reserved word — so a value
      like ``"90210"`` or ``"- dash"`` round-trips as a string.

    Args:
        fields: Ordered mapping of frontmatter keys to values. Insertion order
            is preserved in the output.

    Returns:
        A string beginning with ``---\\n`` and ending with ``---\\n``.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            items = [v for v in value if v is not None]
            if not items:
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {_scalar(v)}" for v in items)
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _scalar(value: object) -> str:
    """Render a single YAML scalar (quoting strings only when needed)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    return _quote(s) if _needs_quote(s) else s


def _needs_quote(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if s[0] in _YAML_INDICATORS:
        return True
    if ":" in s or "#" in s:
        return True
    if '"' in s or "\\" in s or "\n" in s:
        return True
    if s.lower() in _YAML_RESERVED:
        return True
    return _looks_numeric(s)


def _looks_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _quote(s: str) -> str:
    body = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{body}"'


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` only when ``path`` is missing or differs.

    Returns True if the file was (re)written, False if left untouched (which
    preserves its mtime). The write itself is atomic.
    """
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    atomic_write_text(path, content)
    return True
