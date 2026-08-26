"""``pf-doctor --release`` group — read-only git introspection of the cwd project."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

from pf_core._doctor_types import CheckResult


def _git(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=Path.cwd())
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, proc.stdout.strip()


def _changelog_version() -> str | None:
    changelog = Path.cwd() / "CHANGELOG.md"
    if not changelog.is_file():
        return None
    match = re.search(r"^## v(\S+)", changelog.read_text(), re.MULTILINE)
    return match.group(1) if match else None


def _cwd_pyproject_version() -> str | None:
    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:
        return None


def release_checks() -> list[CheckResult]:
    rc, _ = _git("rev-parse", "--git-dir")
    if rc != 0:
        return [CheckResult("release", "repo", "SKIP", "not a git repo (or git absent)")]

    results: list[CheckResult] = []
    pkg_version = _cwd_pyproject_version()
    cl_version = _changelog_version()
    if pkg_version is None:
        results.append(
            CheckResult("release", "versions", "SKIP", "no pyproject.toml version in cwd")
        )
    elif cl_version is None:
        results.append(
            CheckResult(
                "release",
                "versions",
                "WARN",
                f"pyproject {pkg_version}; no v-heading found in CHANGELOG.md",
            )
        )
    elif pkg_version == cl_version:
        results.append(
            CheckResult(
                "release",
                "versions",
                "PASS",
                f"pyproject {pkg_version} == CHANGELOG v{cl_version}",
            )
        )
    else:
        results.append(
            CheckResult(
                "release",
                "versions",
                "FAIL",
                f"pyproject {pkg_version} != CHANGELOG v{cl_version} — sync before tagging",
            )
        )

    _, tags_out = _git("tag", "--points-at", "HEAD", "--list", "v*")
    tags = [t for t in tags_out.splitlines() if t]
    if not tags:
        results.append(
            CheckResult("release", "tag", "SKIP", "no v-tag at HEAD (nothing tagged yet)")
        )
    elif pkg_version is not None and f"v{pkg_version}" in tags:
        results.append(CheckResult("release", "tag", "PASS", f"HEAD tagged {', '.join(tags)}"))
    else:
        results.append(
            CheckResult(
                "release",
                "tag",
                "FAIL",
                f"HEAD tagged {', '.join(tags)} but pyproject says {pkg_version} — "
                "a build of this tag will not match",
            )
        )

    _, status_out = _git("status", "--porcelain")
    changes = [line for line in status_out.splitlines() if line]
    if changes:
        results.append(
            CheckResult(
                "release",
                "tree",
                "WARN",
                f"{len(changes)} uncommitted change(s) — NOT part of any build of HEAD",
            )
        )
    else:
        results.append(CheckResult("release", "tree", "PASS", "working tree clean"))
    return results
