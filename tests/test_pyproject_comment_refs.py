"""Drift gate: module names cited in pyproject.toml comments must still exist.

The extras comments are the primary "which extra do I need?" reference, so a
rename that leaves them behind ships wrong install advice to consumers.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = ROOT / "pyproject.toml"
_SRC = ROOT / "src"

_DOTTED_RE = re.compile(r"pf_core(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_MEMBERS_RE = re.compile(r"members of (pf_core(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(([^)]*)\)")


def _comment_text() -> str:
    lines = [
        line.strip().lstrip("#").strip()
        for line in _PYPROJECT.read_text().splitlines()
        if line.strip().startswith("#")
    ]
    return " ".join(lines)


def _resolves(dotted: str) -> bool:
    base = _SRC.joinpath(*dotted.split("."))
    return base.is_dir() or base.with_suffix(".py").exists()


def test_dotted_module_refs_in_comments_exist() -> None:
    missing = [
        tok
        for tok in sorted(set(_DOTTED_RE.findall(_comment_text())))
        # A trailing attribute (e.g. pf_core.budget.check_budget) is allowed.
        if not _resolves(tok) and not _resolves(tok.rsplit(".", 1)[0])
    ]
    assert not missing, f"pyproject.toml comments cite non-existent modules: {missing}"


def test_member_lists_in_comments_exist() -> None:
    bad: list[str] = []
    for package, listing in _MEMBERS_RE.findall(_comment_text()):
        for member in (m.strip() for m in listing.split(",")):
            if member and not _resolves(f"{package}.{member}"):
                bad.append(f"{package}.{member}")
    assert not bad, f"pyproject.toml comments list non-existent members: {bad}"
