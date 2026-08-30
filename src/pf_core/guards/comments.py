"""Comment budget: a ceiling on comment and docstring prose per module and across the tree.

Opt-in through ``[tool.pf_guards.comment_budget]``. A line is prose when it holds a comment or
falls inside a module, class or function docstring; code is every other non-blank line. The
per-module ratio skips modules under ``min_code_lines``, where one honest docstring dominates.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pf_core.exceptions import ConfigurationError
from pf_core.guards.sources import python_files, source_lines

_SECTION = "[tool.pf_guards.comment_budget]"
_KEYS = frozenset({"file", "total", "min_code_lines", "total_root"})
_DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class CommentBudget:
    """Ceilings on prose lines per line of code."""

    file: float = 0.48  # per module
    total: float = 0.22  # across total_root
    min_code_lines: int = 25  # below this a module's own ratio is not judged
    total_root: tuple[str, ...] = ()  # the scanned roots the total counts; () = every one


@dataclass(frozen=True)
class ProseViolation:
    scope: str  # "module" or "total"
    path: str  # relative to the scan root; for the total, its total_root ("" = every root)
    prose: int
    code: int
    limit: float


def parse_comment_budget(raw: object) -> CommentBudget:
    """Validate ``[tool.pf_guards.comment_budget]``; an empty table takes every default.

    Raises:
        ConfigurationError: not a table, an unknown key, a ratio that is not a positive
            number, ``min_code_lines`` that is not a positive whole number, or ``total_root``
            that is not a path or a non-empty list of paths.
    """
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{_SECTION} must be a table")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ConfigurationError(f"{_SECTION}: unknown key(s) {unknown} (allowed: {sorted(_KEYS)})")
    default = CommentBudget()
    floor = raw.get("min_code_lines", default.min_code_lines)
    if isinstance(floor, bool) or not isinstance(floor, int) or floor <= 0:
        raise ConfigurationError(f"{_SECTION} min_code_lines must be a positive line count")
    return CommentBudget(
        file=_ratio(raw.get("file", default.file), "file"),
        total=_ratio(raw.get("total", default.total), "total"),
        min_code_lines=floor,
        total_root=_roots(raw["total_root"]) if "total_root" in raw else (),
    )


def _roots(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and value and all(isinstance(r, str) for r in value):
        return tuple(value)
    raise ConfigurationError(f"{_SECTION} total_root must be a path or a list of paths")


def _ratio(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigurationError(f"{_SECTION} {key} must be a positive ratio (got {value!r})")
    return float(value)


def prose_and_code(text: str, tree: ast.Module | None = None) -> tuple[int, int]:
    """Lines of comment or docstring, and the other non-blank lines; ``tree`` is ``text`` parsed.

    Raises:
        SyntaxError: ``text`` does not parse.
    """
    tree = tree or ast.parse(text)
    prose = {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, _DOCSTRING_HOLDERS)
            and ast.get_docstring(node, clean=False) is not None
        ):
            doc = node.body[0]
            prose.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    code = sum(1 for n, line in enumerate(source_lines(text), 1) if line.strip() and n not in prose)
    return len(prose), code


def total_root_problems(roots: Sequence[str | Path], budget: CommentBudget) -> list[str]:
    """Each ``total_root`` entry that is not one of the scanned ``roots``."""
    scanned = {Path(r) for r in roots}
    return [
        f"{_SECTION} total_root {r!r} is not a scanned root ({', '.join(map(str, roots))})"
        for r in budget.total_root
        if Path(r) not in scanned
    ]


def scan_comment_budget(
    roots: Sequence[str | Path], budget: CommentBudget, *, skip_unparsed: bool = False
) -> list[ProseViolation]:
    """Modules over ``budget.file`` under every root, then the total over ``budget.total_root``
    (every root when empty) if it is over ``budget.total``.

    With several roots, module paths carry their root. ``skip_unparsed`` passes over a file that
    does not parse, leaving it out of the total too.

    Raises:
        ConfigurationError: a ``total_root`` entry is not one of ``roots``.
        SyntaxError: a file does not parse, unless ``skip_unparsed``.
    """
    problems = total_root_problems(roots, budget)
    if problems:
        raise ConfigurationError("; ".join(problems))
    counted = {Path(r) for r in budget.total_root or roots}
    multi = len(roots) > 1
    out: list[ProseViolation] = []
    prose_sum = code_sum = 0
    for r in roots:
        root = Path(r)
        prefix = f"{str(r).rstrip('/')}/" if multi else ""
        for p, text, tree in python_files(root, skip_unparsed=skip_unparsed):
            prose, code = prose_and_code(text, tree)
            if root in counted:
                prose_sum, code_sum = prose_sum + prose, code_sum + code
            if code >= budget.min_code_lines and prose / code > budget.file:
                rel = f"{prefix}{p.relative_to(root).as_posix()}"
                out.append(ProseViolation("module", rel, prose, code, budget.file))
    if code_sum and prose_sum / code_sum > budget.total:
        where = ", ".join(budget.total_root)
        out.append(ProseViolation("total", where, prose_sum, code_sum, budget.total))
    return out


def report_comment_budget(roots: list[str], budget: CommentBudget) -> bool:
    """Print every module over budget and the total if it is over; True when anything is."""
    found = scan_comment_budget(roots, budget, skip_unparsed=True)
    for v in found:
        where = v.path if v.scope == "module" else f"total ({v.path})" if v.path else "total"
        print(
            f"PROSE {where}: {v.prose} prose lines to {v.code} code "
            f"({v.prose / v.code:.0%}, limit {v.limit:.0%})"
        )
    if found:
        print("\nOver the comment budget: cut history, alternatives not taken, and restated code.")
    return bool(found)
