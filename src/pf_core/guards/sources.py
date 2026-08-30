"""The Python source the opt-in checks read, and the files they cannot.

A file that does not parse is a failure, never a skip: a check that passes over it has vouched
for code it never read.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Iterable, Iterator
from pathlib import Path


def source_lines(text: str) -> list[str]:
    """``text`` split only at ``\\n``: ``str.splitlines`` also ends a row at a form feed, NEL or
    U+2028, which Python does not, and every row after one would be misnumbered."""
    return text.split("\n")


def parse_file(path: Path) -> tuple[str, ast.Module]:
    """The file's text and tree, read as Python reads it: a BOM, a coding cookie, and a bare
    ``\r`` ending a row. A ``SyntaxError``, undecodable bytes included, names the path."""
    data = path.read_bytes()
    try:
        tree = ast.parse(data, filename=path.as_posix())
    except SyntaxError as exc:
        exc.filename = exc.filename or path.as_posix()  # a null byte is refused without one
        raise
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return io.TextIOWrapper(io.BytesIO(data), encoding=encoding).read(), tree


def python_files(
    root: Path, *, skip_unparsed: bool = False
) -> Iterator[tuple[Path, str, ast.Module]]:
    """Each ``*.py`` under ``root``, sorted, with its text and tree.

    A file that does not parse raises its ``SyntaxError``; ``skip_unparsed`` passes over it
    instead, for a caller that names such files itself (the gate, through ``report_unparsed``).
    """
    for path in sorted(root.rglob("*.py")):
        try:
            text, tree = parse_file(path)
        except SyntaxError:
            if skip_unparsed:
                continue
            raise
        yield path, text, tree


def unparsed(roots: Iterable[str | Path]) -> list[SyntaxError]:
    """One ``SyntaxError`` per ``*.py`` under ``roots`` that does not parse, each file once."""
    out: list[SyntaxError] = []
    for path in sorted({p for r in roots for p in Path(r).rglob("*.py")}):
        try:
            parse_file(path)
        except SyntaxError as exc:
            out.append(exc)
    return out


def report_unparsed(roots: Iterable[str | Path]) -> bool:
    """Print each file under ``roots`` that does not parse; True when there is one."""
    found = unparsed(roots)
    for exc in found:
        where = f"{exc.filename}:{exc.lineno}" if exc.lineno else exc.filename
        print(f"FAIL  {where}: does not parse ({exc.msg})")
    if found:
        print(f"\n{len(found)} file(s) do not parse; no opt-in check can vouch for them.")
    return bool(found)
