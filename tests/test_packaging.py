"""Wheel-content guards: shipped data directories must be declared."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_template_dir_is_declared_in_package_data():
    """A templates/ dir without a package-data entry ships in editable
    installs but silently vanishes from the wheel (the 0.11.0 jobs_admin
    failure mode)."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pkg_data = pyproject["tool"]["setuptools"]["package-data"]
    declared = {
        pkg
        for pkg, globs in pkg_data.items()
        if any("templates" in g for g in globs)
    }
    src = ROOT / "src"
    for d in sorted(src.rglob("templates")):
        if not d.is_dir() or not any(d.glob("*.html")):
            continue
        pkg = ".".join(d.relative_to(src).parts[:-1])
        assert pkg in declared, (
            f"{pkg} ships templates/*.html but has no "
            f"[tool.setuptools.package-data] entry — the wheel would omit it"
        )
