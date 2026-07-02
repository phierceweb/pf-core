"""Tests for pf_core.doctor — runtime ground-truth attestation.

All checks are exercised against controlled state (monkeypatched env,
tmp files, tmp sqlite); nothing here touches the network.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

from pf_core import doctor
from pf_core.doctor import (
    CheckResult,
    check_copy,
    check_deps,
    check_env,
    check_extras,
    check_python,
    check_router,
    db_checks,
    redact_value,
    run_checks,
    run_cli,
)
from pf_core.llm import _router_loader


@pytest.fixture(autouse=True)
def _router_cache_reset():
    _router_loader.clear_cache()
    yield
    _router_loader.clear_cache()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_url_credentials_masked(self):
        out = redact_value("DATABASE_URL", "mysql://user:s3cr3t@db.host:3306/app")
        assert "s3cr3t" not in out
        assert "user" not in out
        assert "db.host" in out

    def test_key_vars_presence_only(self):
        out = redact_value("ANTHROPIC_API_KEY", "sk-ant-abc123")
        assert "sk-ant-abc123" not in out
        assert "abc123" not in out
        assert out == "set (redacted)"

    def test_plain_value_passthrough(self):
        assert redact_value("LOG_LEVEL", "DEBUG") == "DEBUG"

    def test_url_without_credentials_untouched(self):
        assert redact_value("DATABASE_URL", "sqlite:///data.db") == "sqlite:///data.db"


# ---------------------------------------------------------------------------
# Core checks
# ---------------------------------------------------------------------------


class TestCheckPython:
    def test_passes_on_current_interpreter(self):
        (res,) = check_python()
        assert res.status == "PASS"
        assert f"{sys.version_info[0]}.{sys.version_info[1]}" in res.detail

    def test_fails_below_floor(self, monkeypatch):
        monkeypatch.setattr(doctor, "_MIN_PY", (99, 0))
        (res,) = check_python()
        assert res.status == "FAIL"


class TestCheckCopy:
    def test_reports_path_and_version(self):
        (res,) = check_copy()
        assert res.status in ("PASS", "WARN")
        assert "pf_core" in res.detail
        assert "version" in res.detail

    def test_warns_on_editable_metadata_mismatch(self, monkeypatch):
        monkeypatch.setattr(doctor, "_installed_version", lambda: "0.0.1")
        monkeypatch.setattr(doctor, "_adjacent_pyproject_version", lambda: "9.9.9")
        (res,) = check_copy()
        assert res.status == "WARN"
        assert "0.0.1" in res.detail
        assert "9.9.9" in res.detail

    def test_adjacent_pyproject_version_reads_file(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "1.2.3"\n'
        )
        monkeypatch.setattr(doctor, "_package_root", lambda: tmp_path / "src" / "pf_core")
        assert doctor._adjacent_pyproject_version() == "1.2.3"


class TestCheckExtras:
    def test_lists_installed_extras(self):
        (res,) = check_extras()
        assert res.status == "PASS"
        # dev env has pydantic + sqlalchemy installed
        assert "validate" in res.detail
        assert "db" in res.detail


class TestCheckEnv:
    def test_reports_set_and_unset_with_redaction(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # no .env here
        monkeypatch.setenv("DATABASE_URL", "mysql://u:pw@h:3306/d")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
        monkeypatch.delenv("REDIS_URL", raising=False)
        (res,) = check_env()
        assert res.status == "PASS"
        assert "pw" not in res.detail
        assert "sk-ant-xyz" not in res.detail
        assert "DATABASE_URL" in res.detail
        assert "REDIS_URL" in res.detail  # listed as unset
        assert "no .env" in res.detail

    def test_loads_cwd_dotenv_for_effective_env(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("LOG_LEVEL=TRACE_TEST\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        (res,) = check_env()
        assert "LOG_LEVEL=TRACE_TEST" in res.detail
        assert ".env loaded from" in res.detail

    def test_shell_value_wins_over_dotenv(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("LOG_LEVEL=FROM_FILE\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LOG_LEVEL", "FROM_SHELL")
        (res,) = check_env()
        assert "LOG_LEVEL=FROM_SHELL" in res.detail


class TestCheckRouter:
    def test_skip_when_config_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(tmp_path / "nope.yaml"))
        (res,) = check_router()
        assert res.status == "SKIP"

    def test_pass_lists_agents(self, tmp_path, monkeypatch):
        cfg = tmp_path / "router.yaml"
        cfg.write_text("agents:\n  summarizer:\n    model: m1\n  classifier:\n    model: m2\n")
        monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(cfg))
        (res,) = check_router()
        assert res.status == "PASS"
        assert "summarizer" in res.detail
        assert "classifier" in res.detail

    def test_fail_on_invalid_schema(self, tmp_path, monkeypatch):
        cfg = tmp_path / "router.yaml"
        cfg.write_text("agents:\n  broken: {}\n")
        monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(cfg))
        (res,) = check_router()
        assert res.status == "FAIL"


class TestCheckDeps:
    def test_reports_installed_versions(self):
        (res,) = check_deps()
        assert res.status == "PASS"
        assert "pydantic" in res.detail


# ---------------------------------------------------------------------------
# --db group
# ---------------------------------------------------------------------------


class TestDbChecks:
    def test_missing_url_fails(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        results = db_checks()
        assert results[0].status == "FAIL"
        assert "DATABASE_URL" in results[0].detail

    def test_sqlite_missing_file_fails_without_creating(self, tmp_path, monkeypatch):
        db_file = tmp_path / "absent.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        results = db_checks()
        connect = next(r for r in results if r.name == "connect")
        assert connect.status == "FAIL"
        assert not db_file.exists()  # read-only invariant: no file created

    def test_sqlite_existing_file_connects(self, tmp_path, monkeypatch):
        db_file = tmp_path / "real.db"
        sqlite3.connect(db_file).close()
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.chdir(tmp_path)  # no alembic/ here
        results = db_checks()
        by_name = {r.name: r for r in results}
        assert by_name["url"].status == "PASS"
        assert by_name["connect"].status == "PASS"
        assert by_name["migrations"].status == "SKIP"


# ---------------------------------------------------------------------------
# Runner + CLI
# ---------------------------------------------------------------------------


class TestRunner:
    def test_run_checks_core_only_by_default(self):
        results = run_checks(db=False)
        groups = {r.group for r in results}
        assert "db" not in groups
        assert {"copy", "python", "extras", "env", "router", "deps"} <= groups

    def test_run_checks_includes_db_when_asked(self, tmp_path, monkeypatch):
        db_file = tmp_path / "x.db"
        sqlite3.connect(db_file).close()
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.chdir(tmp_path)
        results = run_checks(db=True)
        assert any(r.group == "db" for r in results)


class TestRunCli:
    def test_exit_zero_when_no_fail(self, capsys, monkeypatch):
        monkeypatch.delenv("MODEL_ROUTER_CONFIG", raising=False)
        code = run_cli([])
        out = capsys.readouterr().out
        assert code == 0
        assert "pass" in out.lower()

    def test_exit_one_on_fail(self, capsys, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda db=False: [CheckResult("env", "forced", "FAIL", "boom")],
        )
        code = run_cli([])
        assert code == 1

    def test_warn_does_not_flip_exit(self, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda db=False: [CheckResult("copy", "forced", "WARN", "meh")],
        )
        assert run_cli([]) == 0

    def test_db_flag_parsed(self, tmp_path, monkeypatch, capsys):
        db_file = tmp_path / "x.db"
        sqlite3.connect(db_file).close()
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.chdir(tmp_path)
        code = run_cli(["--db"])
        out = capsys.readouterr().out
        assert code == 0
        assert "connect" in out
