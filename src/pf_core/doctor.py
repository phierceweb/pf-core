"""Runtime ground-truth attestation for pf-core installs.

``pf-doctor`` (or ``python -m pf_core.doctor``) prints verified facts about
the current environment: which pf-core copy is loaded, interpreter and venv,
installed extras, resolved env vars (redacted), model-router config validity,
and key dependency versions. ``--db`` adds a read-only database attestation
(URL, connectivity, alembic revision vs script head).

Invariants: doctor never writes, never touches the network except the opt-in
``--db`` connect (``--release`` runs read-only local git commands), and never
imports consumer application code. Exit code is ``0`` when no check FAILs
(WARNs don't flip it), ``1`` otherwise.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_MIN_PY = (3, 11)

# Env vars pf-core features read, reported by the `env` check.
_ENV_VARS = (
    "DATABASE_URL",
    "MODEL_ROUTER_CONFIG",
    "LOG_LEVEL",
    "LOG_FILE",
    "REDIS_URL",
    "CACHE_CONFIG",
    "WEB_HOST",
    "WEB_PORT",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "BRAVE_API_KEY",
    "REQUEST_TIMEOUT",
)

# Marker import -> extra it indicates (find_spec only; nothing is imported).
_EXTRA_MARKERS = (
    ("httpx", "http"),
    ("pydantic", "validate"),
    ("json_repair", "validate"),
    ("typer", "cli"),
    ("tenacity", "llm"),
    ("sqlalchemy", "db"),
    ("alembic", "db"),
    ("fastapi", "web"),
    ("anthropic", "anthropic"),
    ("redis", "redis"),
    ("slowapi", "ratelimit"),
    ("trafilatura", "articles"),
    ("jsonschema", "jsonschema"),
    ("PIL", "image-phash"),
)

_DEP_VERSIONS = (
    "anthropic",
    "httpx",
    "pydantic",
    "sqlalchemy",
    "fastapi",
    "structlog",
    "tenacity",
    "typer",
)

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
_URL_CREDS = re.compile(r"^([a-z0-9+.-]+://)[^@/]+@", re.IGNORECASE)


@dataclass(frozen=True)
class CheckResult:
    group: str
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str


def redact_value(name: str, value: str) -> str:
    """Redact secrets: key-like vars to presence-only, URL credentials masked."""
    if _SECRET_NAME.search(name):
        return "set (redacted)"
    return _URL_CREDS.sub(r"\1***@", value)


# ---------------------------------------------------------------------------
# Core checks — local, read-only, no network.
# ---------------------------------------------------------------------------


def _package_root() -> Path:
    import pf_core

    return Path(pf_core.__file__).resolve().parent


def _installed_version() -> str:
    try:
        return importlib.metadata.version("pf-core")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _adjacent_pyproject_version() -> str | None:
    """Version from the pyproject.toml two levels above the package (src layout).

    Present only for editable/source installs; site-packages installs return
    ``None``.
    """
    pyproject = _package_root().parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:
        return None


def check_copy() -> list[CheckResult]:
    root = _package_root()
    version = _installed_version()
    kind = "site-packages" if "site-packages" in str(root) else "editable/source"
    detail = f"{root} ({kind}), version {version}"
    src_version = _adjacent_pyproject_version()
    if src_version is not None and src_version != version:
        return [
            CheckResult(
                "copy",
                "loaded",
                "WARN",
                f"{detail} — adjacent pyproject says {src_version}; "
                "stale editable install? reinstall with pip install -e .",
            )
        ]
    return [CheckResult("copy", "loaded", "PASS", detail)]


def check_python() -> list[CheckResult]:
    cur = sys.version_info[:2]
    detail = f"python {cur[0]}.{cur[1]}, venv {sys.prefix}"
    if cur < _MIN_PY:
        return [
            CheckResult(
                "python", "interpreter", "FAIL",
                f"{detail} — pf-core requires >= {_MIN_PY[0]}.{_MIN_PY[1]}",
            )
        ]
    return [CheckResult("python", "interpreter", "PASS", detail)]


def check_extras() -> list[CheckResult]:
    installed = sorted(
        {extra for module, extra in _EXTRA_MARKERS if importlib.util.find_spec(module)}
    )
    detail = ", ".join(installed) if installed else "none (foundation only)"
    return [CheckResult("extras", "available", "PASS", detail)]


def check_env() -> list[CheckResult]:
    # Load the consumer's .env first so the report shows the EFFECTIVE env
    # the app would boot with, not just the bare shell. Existing shell vars
    # win (dotenv default), matching consumer boot behavior.
    dotenv_path = Path.cwd() / ".env"
    source = "shell only (no .env in cwd)"
    if dotenv_path.is_file():
        from dotenv import load_dotenv

        load_dotenv(dotenv_path)
        source = f".env loaded from {dotenv_path}"
    lines = [source]
    for var in _ENV_VARS:
        value = os.environ.get(var)
        lines.append(
            f"{var}={redact_value(var, value)}" if value else f"{var} unset"
        )
    return [CheckResult("env", "resolution", "PASS", "; ".join(lines))]


def check_router() -> list[CheckResult]:
    from pf_core.llm import _router_loader

    path = _router_loader.config_path()
    if not Path(path).is_file():
        return [CheckResult("router", "config", "SKIP", f"no router config at {path}")]
    try:
        doc = _router_loader.load(force=True)
    except Exception as e:
        return [CheckResult("router", "config", "FAIL", f"{path}: {e}")]
    agents = sorted((doc.get("agents") or {}).keys())
    default_client = doc.get("default_client") or "(unset)"
    return [
        CheckResult(
            "router", "config", "PASS",
            f"{path}: {len(agents)} agent(s) [{', '.join(agents)}], "
            f"default_client={default_client}",
        )
    ]


def check_deps() -> list[CheckResult]:
    parts = []
    for dist in _DEP_VERSIONS:
        try:
            parts.append(f"{dist} {importlib.metadata.version(dist)}")
        except importlib.metadata.PackageNotFoundError:
            continue
    return [CheckResult("deps", "versions", "PASS", ", ".join(parts) or "none")]


# ---------------------------------------------------------------------------
# --db group — opt-in; read-only (no writes, no file creation).
# ---------------------------------------------------------------------------


def db_checks() -> list[CheckResult]:
    if importlib.util.find_spec("sqlalchemy") is None:
        from pf_core._extras import install_target

        return [
            CheckResult(
                "db", "extra", "SKIP",
                f"sqlalchemy not installed — pip install {install_target('db')}",
            )
        ]

    from sqlalchemy import create_engine, text

    from pf_core.db import db_url

    try:
        url = db_url()
    except Exception as e:
        return [CheckResult("db", "url", "FAIL", str(e))]
    results = [CheckResult("db", "url", "PASS", redact_value("DATABASE_URL", url))]

    # SQLite connects CREATE the file when absent — stat first so doctor
    # stays read-only.
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path not in ("", ":memory:") and not Path(db_path).is_file():
            results.append(
                CheckResult("db", "connect", "FAIL", f"sqlite file not found: {db_path}")
            )
            results.append(CheckResult("db", "migrations", "SKIP", "no connection"))
            return results

    current_rev: str | None = None
    try:
        engine = create_engine(url, connect_args=_connect_args(url))
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            current_rev = _alembic_current(conn)
        engine.dispose()
        results.append(CheckResult("db", "connect", "PASS", "SELECT 1 ok"))
    except Exception as e:
        results.append(CheckResult("db", "connect", "FAIL", str(e)))
        results.append(CheckResult("db", "migrations", "SKIP", "no connection"))
        return results

    results.append(_migration_result(current_rev))
    return results


def _connect_args(url: str) -> dict:
    if url.startswith("sqlite"):
        return {}
    return {"connect_timeout": 5}


def _alembic_current(conn) -> str | None:
    from sqlalchemy import text

    try:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _migration_result(current_rev: str | None) -> CheckResult:
    alembic_dir = Path.cwd() / "alembic"
    if not (alembic_dir / "env.py").is_file():
        return CheckResult("db", "migrations", "SKIP", "no alembic/ in cwd")
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(alembic_dir))
        head = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception as e:
        return CheckResult("db", "migrations", "WARN", f"cannot read script head: {e}")
    if current_rev is None:
        return CheckResult(
            "db", "migrations", "WARN",
            f"db has no alembic_version table; script head is {head}",
        )
    if current_rev != head:
        return CheckResult(
            "db", "migrations", "WARN",
            f"db at {current_rev}, script head {head} — run migrations",
        )
    return CheckResult("db", "migrations", "PASS", f"at head {head}")


# ---------------------------------------------------------------------------
# --release group — opt-in; read-only git introspection of the cwd project.
# ---------------------------------------------------------------------------


def _git(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=Path.cwd()
        )
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
                "release", "versions", "WARN",
                f"pyproject {pkg_version}; no v-heading found in CHANGELOG.md",
            )
        )
    elif pkg_version == cl_version:
        results.append(
            CheckResult(
                "release", "versions", "PASS",
                f"pyproject {pkg_version} == CHANGELOG v{cl_version}",
            )
        )
    else:
        results.append(
            CheckResult(
                "release", "versions", "FAIL",
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
        results.append(
            CheckResult("release", "tag", "PASS", f"HEAD tagged {', '.join(tags)}")
        )
    else:
        results.append(
            CheckResult(
                "release", "tag", "FAIL",
                f"HEAD tagged {', '.join(tags)} but pyproject says {pkg_version} — "
                "a build of this tag will not match",
            )
        )

    _, status_out = _git("status", "--porcelain")
    changes = [line for line in status_out.splitlines() if line]
    if changes:
        results.append(
            CheckResult(
                "release", "tree", "WARN",
                f"{len(changes)} uncommitted change(s) — NOT part of any build of HEAD",
            )
        )
    else:
        results.append(CheckResult("release", "tree", "PASS", "working tree clean"))
    return results


# ---------------------------------------------------------------------------
# Runner + CLI
# ---------------------------------------------------------------------------

_CORE_CHECKS = (check_copy, check_python, check_extras, check_env, check_router, check_deps)


def run_checks(*, db: bool = False, release: bool = False) -> list[CheckResult]:
    results: list[CheckResult] = []
    for fn in _CORE_CHECKS:
        results.extend(fn())
    if db:
        results.extend(db_checks())
    if release:
        results.extend(release_checks())
    return results


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pf-doctor", description="pf-core runtime ground-truth attestation"
    )
    parser.add_argument(
        "--db", action="store_true",
        help="include read-only database checks (connect + migration state)",
    )
    parser.add_argument(
        "--release", action="store_true",
        help="include release-state checks (tag vs pyproject vs CHANGELOG, dirty tree)",
    )
    args = parser.parse_args(argv)

    from rich.console import Console
    from rich.table import Table

    results = run_checks(db=args.db, release=args.release)
    table = Table(title="pf-doctor", show_lines=False)
    table.add_column("status", no_wrap=True)
    table.add_column("check", no_wrap=True)
    table.add_column("detail", overflow="fold")
    styles = {"PASS": "green", "WARN": "yellow", "FAIL": "red", "SKIP": "dim"}
    for r in results:
        table.add_row(
            f"[{styles[r.status]}]{r.status}[/]", f"{r.group}.{r.name}", r.detail
        )
    console = Console()
    console.print(table)

    counts = {s: sum(1 for r in results if r.status == s) for s in styles}
    console.print(
        f"{counts['PASS']} pass, {counts['WARN']} warn, "
        f"{counts['FAIL']} fail, {counts['SKIP']} skip"
    )
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
