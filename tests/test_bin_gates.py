"""Gate coverage for bin/: mypy and ruff both skip extensionless scripts unless
each is named, so this fails when a new one is missing from either list."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"


def _python_scripts_without_suffix() -> set[str]:
    """Paths (repo-relative) of bin/ files that are Python but not ``*.py``."""
    found = set()
    for path in sorted(BIN.iterdir()):
        if not path.is_file() or path.suffix == ".py":
            continue
        first = path.read_text(errors="replace").split("\n", 1)[0]
        if first.startswith("#!") and "python" in first:
            found.add(f"bin/{path.name}")
    return found


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_every_extensionless_bin_script_is_named_to_mypy():
    listed = set(_pyproject()["tool"]["mypy"]["files"])
    missing = _python_scripts_without_suffix() - listed
    assert not missing, (
        f"not type-checked — add to [tool.mypy] files in pyproject.toml: {sorted(missing)}"
    )


def test_every_extensionless_bin_script_is_named_to_ruff():
    listed = set(_pyproject()["tool"]["ruff"]["extend-include"])
    missing = _python_scripts_without_suffix() - listed
    assert not missing, (
        f"not linted — add to [tool.ruff] extend-include in pyproject.toml: {sorted(missing)}"
    )


def test_mypy_scripts_are_modules_is_on():
    """Without it, every named extensionless script resolves to ``__main__``."""
    assert _pyproject()["tool"]["mypy"]["scripts_are_modules"] is True


def test_the_gate_finds_the_known_scripts():
    """Guards the detector itself: a broken scan would make the gates vacuous."""
    assert _python_scripts_without_suffix() >= {"bin/pf-eval", "bin/pf-jobs"}
