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
    minors = [int(g) for m in _VERSIONISH_RE.finditer(line) for g in m.groups() if g is not None]
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


def test_no_backup_or_debris_files_are_tracked():
    """No editor backup or debris file is tracked.

    Enumerated from git, not globbed: ``*.md`` does not match ``.md.bak``.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    debris = [
        f
        for f in tracked
        if f.endswith((".bak", ".orig", ".rej", ".tmp", ".swp", "~")) or Path(f).name == ".DS_Store"
    ]
    assert not debris, f"debris committed: {debris}"


# ``0.M.x`` prose (e.g. "picks up 0.M.x fixes") sits next to a ``~=`` pin but is
# neither a pin nor a three-component version, so neither gate above sees it.
_LINE_X_RE = re.compile(r"(?<![\w.])0\.(\d{1,3})\.x(?![\w])")


def _requires_python() -> tuple[int, int]:
    spec = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    m = re.search(r">=\s*(\d+)\.(\d+)", spec)
    assert m, f"cannot parse requires-python: {spec!r}"
    return int(m.group(1)), int(m.group(2))


def test_pin_prose_tracks_current_minor():
    """A ``~=`` bump is a two-part edit: the pin token and the ``0.M.x`` line it
    is described by. Only the token was gated, so the prose drifted a minor
    behind in the README — which is also the PyPI long description."""
    _, minor = _current_version()
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}: 0.{m.group(1)}.x"
        for path in _pin_example_files()
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        for m in _LINE_X_RE.finditer(line)
        if int(m.group(1)) != minor
    ]
    assert not offenders, f"pin prose not on the current minor (0.{minor}.x):\n" + "\n".join(
        offenders
    )


def test_templates_track_requires_python():
    """A scaffolded project that claims a lower floor than the pf-core it pins
    is unresolvable on the interpreters it advertises."""
    ours = _requires_python()
    offenders = []
    for path in sorted(ROOT.glob("templates/*/pyproject.toml")):
        spec = tomllib.loads(path.read_text())["project"]["requires-python"]
        m = re.search(r">=\s*(\d+)\.(\d+)", spec)
        if not m or (int(m.group(1)), int(m.group(2))) != ours:
            offenders.append(f"{path.relative_to(ROOT)}: {spec}")
    assert not offenders, (
        f"template requires-python does not match pf-core's (>={ours[0]}.{ours[1]}):\n"
        + "\n".join(offenders)
    )


def test_doctor_floor_matches_requires_python():
    """``pf-doctor`` reports whether the interpreter can run pf-core, so a stale
    floor makes it PASS the exact environment the install rejects."""
    from pf_core.doctor import _MIN_PY

    assert _MIN_PY == _requires_python(), (
        f"pf_core.doctor._MIN_PY {_MIN_PY} != requires-python {_requires_python()}"
    )


def _setup_scripts() -> list[Path]:
    """Every script that picks an interpreter or validates a venv — pf-core's own
    plus the copies ``bin/new-consumer`` stamps into each scaffolded project."""
    return [
        ROOT / "bin" / "setup-common",
        ROOT / "bin" / "verify-bare-install",
        *sorted(ROOT.glob("templates/*/bin/setup")),
    ]


def test_setup_scripts_track_requires_python():
    """The setup helpers build the venv, so a stale floor there produces an
    interpreter that cannot install the package it was created for. Covers the
    template copies too: they are what a scaffolded project runs on day one."""
    major, minor = _requires_python()
    wanted = f"({major}, {minor})"
    stale_interpreters = re.compile(rf"python{major}\.(\d+)\b")
    offenders = []
    for path in _setup_scripts():
        rel = path.relative_to(ROOT)
        text = path.read_text()
        found = set(re.findall(r"sys\.version_info >= (\(\d+, \d+\))", text))
        if found != {wanted}:
            offenders.append(f"{rel}: version_info {sorted(found) or 'check missing'} != {wanted}")
        stale = sorted({int(m) for m in stale_interpreters.findall(text) if int(m) < minor})
        if stale:
            offenders.append(f"{rel}: offers {major}.{stale} below the floor")
    assert not offenders, f"setup scripts do not enforce the package floor {wanted}:\n" + "\n".join(
        offenders
    )


def test_ruff_target_version_tracks_requires_python():
    """``target-version`` tells ruff which syntax to accept. Below the floor it
    silently rejects language features the project is entitled to use."""
    major, minor = _requires_python()
    wanted = f"py{major}{minor}"
    offenders = []
    for path in [ROOT / "pyproject.toml", *sorted(ROOT.glob("templates/*/pyproject.toml"))]:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            m = re.match(r'\s*target-version\s*=\s*"([^"]+)"', line)
            if m and m.group(1) != wanted:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {m.group(1)} != {wanted}")
    assert not offenders, (
        f"ruff target-version does not match requires-python ({wanted}):\n" + "\n".join(offenders)
    )
