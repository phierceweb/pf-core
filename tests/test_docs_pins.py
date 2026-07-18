"""Docs drift gates: version examples in public prose must track released state."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PIN_RE = re.compile(r"~=\s*(\d+)\.(\d+)\.\d+")

# Version-ish tokens on the 0.x line: v-prefixed (v0.M), the pre-0.M idiom, or
# bare three-component (0.M.P). Bare two-component decimals (scores like 0.85,
# temperature 0.2) are deliberately not matched. A token offends when M exceeds
# the released minor from pyproject: the public line can never be referenced
# ahead of itself, and pre-publication internal numbering is exactly such
# out-of-line minors. The boundary is read at run time, so the gate self-heals
# as new minors ship.
_VERSIONISH_RE = re.compile(
    r"(?<![\w.])v0\.(\d{1,3})(?!\d)"
    r"|(?<![\w.])pre-0\.(\d{1,3})(?!\d)"
    r"|(?<![\d.])0\.(\d{1,3})\.\d"
)
# Real dependency versions share these shapes; a line is exempt when the match
# sits next to one of these package names.
_DEP_VERSIONS = (
    "ruff",
    "httpx",
    "uvicorn",
    "anthropic",
    "fastapi",
    "json-repair",
    "typer",
    "slowapi",
)


def _current_version() -> tuple[int, int]:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def _offending_minors(line: str, released_minor: int) -> list[int]:
    minors = [
        int(g)
        for m in _VERSIONISH_RE.finditer(line)
        for g in m.groups()
        if g is not None
    ]
    return [m for m in minors if m > released_minor]


def _pin_example_files() -> list[Path]:
    return [
        ROOT / "README.md",
        *sorted((ROOT / "src/pf_core/docs").rglob("*.md")),
        *sorted((ROOT / ".ai/rules").glob("*.md")),
        *sorted(ROOT.glob("templates/*/pyproject.toml")),
    ]


def test_pin_examples_track_current_minor():
    """Patch fixes land on the newest minor only, so a stale ``~=`` example
    points consumers at a frozen line."""
    major, minor = _current_version()
    offenders = []
    for path in _pin_example_files():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "pf-core" not in line:
                continue
            for m in PIN_RE.finditer(line):
                if (int(m.group(1)), int(m.group(2))) != (major, minor):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno}: ~={m.group(1)}.{m.group(2)}.x"
                    )
    assert not offenders, (
        f"pf-core pin examples not on the current minor ({major}.{minor}.x):\n"
        + "\n".join(offenders)
    )


def test_prepub_detector_semantics():
    """Gate logic pinned against synthetic tokens (built, not literal, so the
    fingerprint gate never matches this file)."""
    cur = 7
    high, low = cur + 35, cur - 2
    assert _offending_minors(f"see v0.{high} for details", cur) == [high]
    assert _offending_minors(f"moved out in 0.{high}.0", cur) == [high]
    assert _offending_minors(f"preserves pre-0.{cur + 6} behavior", cur) == [cur + 6]
    assert _offending_minors(f"v0.{cur + 1}", cur) == [cur + 1]
    assert _offending_minors(f"shipped in v0.{low}.3 and 0.{low}.1", cur) == []
    assert _offending_minors("score 0.85, temperature 0.2", cur) == []
    # The same token becomes legal the moment that minor is released.
    assert _offending_minors(f"released 0.{cur + 1}.0", cur + 1) == []


def test_no_prepublication_version_references():
    """The shipped tree must not reference versions ahead of the released
    line. Scans prose, source, scripts, workflows, and templates; skips this
    file (it manufactures version tokens) and dependency-version lines."""
    major, minor = _current_version()
    assert major == 0, "1.x reached: pin the final 0-line minor in this gate"
    scan = [
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CHANGELOG.md",
        ROOT / "pyproject.toml",
        *sorted((ROOT / "src/pf_core").rglob("*.md")),
        *sorted((ROOT / "src/pf_core").rglob("*.py")),
        *sorted((ROOT / "tests").rglob("*.py")),
        *sorted((ROOT / "bin").iterdir()),
        *sorted((ROOT / ".github").rglob("*")),
        *sorted((ROOT / ".ai/rules").glob("*.md")),
        *sorted((ROOT / "templates").rglob("*")),
    ]
    self_path = Path(__file__).resolve()
    offenders = []
    for path in scan:
        if not path.is_file() or path.resolve() == self_path:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _offending_minors(line, minor):
                continue
            if any(dep in line for dep in _DEP_VERSIONS):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "version references ahead of the released line (pre-publication/future numbering):\n"
        + "\n".join(offenders)
    )
