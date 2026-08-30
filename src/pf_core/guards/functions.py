"""Function-length check: every function over its soft or hard line limit, named and measured.

Opt-in through ``max_function_lines`` in ``[tool.pf_guards]``. A function's length runs from
its ``def`` line to its last line, so blanks, comments and the docstring count and decorators do
not. A nested function is measured on its own and also counts toward its parent.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from pf_core.guards.config import FunctionLimits, app_rel, function_limits_for
from pf_core.guards.sources import python_files


@dataclass(frozen=True)
class FunctionLengthViolation:
    path: str  # POSIX, relative to the scan root
    line: int  # the def line
    name: str  # qualified: Class.method, outer.inner
    lines: int
    limit: int  # the limit that was exceeded
    severity: str  # "hard" or "soft"


def function_lengths(tree: ast.AST) -> list[tuple[str, int, int]]:
    """``(qualified name, def line, lines)`` for every function in ``tree``, nested ones included."""
    out: list[tuple[str, int, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                end = child.end_lineno or child.lineno
                out.append((name, child.lineno, end - child.lineno + 1))
                walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def scan_function_lengths(
    root: str | Path, limits: FunctionLimits, *, path_prefix: str = "", skip_unparsed: bool = False
) -> list[FunctionLengthViolation]:
    """Functions over their soft or hard limit under ``root`` (``*.py``, recursively).

    ``path_prefix`` (multi-root scans) is prepended to reported paths and takes part in prefix
    matching. A file that does not parse raises its ``SyntaxError``, unless ``skip_unparsed``.
    """
    root = Path(root)
    out: list[FunctionLengthViolation] = []
    for p, _, tree in python_files(root, skip_unparsed=skip_unparsed):
        rel = p.relative_to(root).as_posix()
        shown = f"{path_prefix}{rel}"
        hard, soft = function_limits_for(shown, app_rel(root, rel), limits)
        for name, line, n in function_lengths(tree):
            if n > hard:
                out.append(FunctionLengthViolation(shown, line, name, n, hard, "hard"))
            elif n > soft:
                out.append(FunctionLengthViolation(shown, line, name, n, soft, "soft"))
    return out


def report_function_lengths(roots: list[str], limits: FunctionLimits) -> bool:
    """Print every function over its limit; True when one is over its hard limit."""
    multi = len(roots) > 1
    found: list[FunctionLengthViolation] = []
    for r in roots:
        prefix = f"{r.rstrip('/')}/" if multi else ""
        found += scan_function_lengths(r, limits, path_prefix=prefix, skip_unparsed=True)
    for v in found:
        tag, what = ("FAIL ", "hard limit") if v.severity == "hard" else ("WARN ", "soft target")
        print(f"{tag} {v.path}:{v.line} {v.name}: {v.lines} lines (function {what} {v.limit})")
    over = sum(1 for v in found if v.severity == "hard")
    if over:
        print(f"\n{over} function(s) over the hard limit. Split them.")
    return bool(over)
