"""Consumer wiring — link the installed docs where assistants look.

``pf-setup`` links ``docs/pf-core`` -> the installed package's bundled docs;
:func:`check_wiring` is the read-only counterpart surfaced by ``pf-doctor``.
A non-symlink at the link path is reported, never replaced.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def installed_docs_dir() -> Path:
    """The bundled docs directory of the imported pf_core."""
    import pf_core

    return Path(pf_core.__file__).resolve().parent / "docs"


def _ensure_symlink(
    link: Path, target: Path, actions: list[str], errors: list[str]
) -> None:
    if link.is_symlink():
        if link.resolve() == (link.parent / target).resolve():
            actions.append(f"ok: {link}")
            return
        link.unlink()
    elif link.exists():
        errors.append(f"refusing to replace non-symlink {link} — move it aside and re-run")
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    actions.append(f"linked: {link} -> {target}")


def ensure_wiring(root: Path) -> tuple[list[str], list[str]]:
    """Idempotently create the consumer links. Returns ``(actions, errors)``."""
    root = Path(root)
    actions: list[str] = []
    errors: list[str] = []
    _ensure_symlink(root / "docs" / "pf-core", installed_docs_dir(), actions, errors)
    return actions, errors


def check_wiring(root: Path | None = None) -> list[tuple[str, str, str]]:
    """Read-only wiring status rows ``(name, status, detail)`` for pf-doctor."""
    root = Path(root) if root is not None else Path.cwd()
    if (root / "src" / "pf_core").is_dir():
        return [("context", "SKIP", "framework checkout — consumer wiring n/a")]
    if not (root / "docs").is_dir():
        return [("context", "SKIP", "no docs/ here — not a consumer root?")]

    rows: list[tuple[str, str, str]] = []
    link = root / "docs" / "pf-core"
    if link.is_symlink():
        if (link / "modules.md").is_file():
            rows.append(("docs_link", "PASS", f"docs/pf-core -> {link.resolve()}"))
        else:
            rows.append(
                ("docs_link", "FAIL", "docs/pf-core is a broken symlink — re-run pf-setup")
            )
    elif link.exists():
        rows.append(("docs_link", "FAIL", "docs/pf-core exists but is not a symlink"))
    else:
        rows.append(
            ("docs_link", "WARN", "docs/pf-core missing — run pf-setup to link the installed docs")
        )

    return rows


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pf-setup",
        description="Link the installed pf-core's bundled docs at docs/pf-core "
        "so in-repo AI assistants can read them.",
    )
    parser.add_argument(
        "--project-root", default=".", help="consumer repo root (default: cwd)"
    )
    args = parser.parse_args(argv)
    actions, errors = ensure_wiring(Path(args.project_root))
    for line in actions:
        print(f"  ✓ {line}")
    for line in errors:
        print(f"  ! {line}")
    return 1 if errors else 0


__all__ = ["check_wiring", "ensure_wiring", "installed_docs_dir", "run_cli"]
