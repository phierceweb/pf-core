"""The framework check as ``python -m pf_core.guards`` runs it: opt-in, report mode, exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest

from pf_core.guards.structure import run_cli

REASON = "the CLI entry point is where user-facing output belongs"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text("def f():\n    print('x')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _config(repo: Path, body: str = "", root: str = '"src"') -> None:
    (repo / ".pf-guards.toml").write_text(
        f"[tool.pf_guards]\nroot = {root}\n{body}", encoding="utf-8"
    )


def test_without_the_section_the_check_does_not_run(repo: Path, capsys) -> None:
    """Upgrading pf-core must not turn an existing consumer's gate red."""
    _config(repo)
    assert run_cli([]) == 0
    assert "FRAMEWORK" not in capsys.readouterr().out


def test_with_the_section_a_breach_fails_the_gate(repo: Path, capsys) -> None:
    _config(repo, "[tool.pf_guards.framework]\n")
    assert run_cli([]) == 1
    out = capsys.readouterr().out
    assert "FRAMEWORK core.py:2:" in out
    assert "use pf_core.log.get_logger(__name__)" in out
    assert "[print]" in out


def test_report_lists_every_breach_and_exits_zero(repo: Path, capsys) -> None:
    (repo / "src" / "env.py").write_text("import os\n\nX = os.getenv('X')\n", encoding="utf-8")
    _config(repo, "[tool.pf_guards.framework]\n")
    assert run_cli(["--report"]) == 0
    out = capsys.readouterr().out
    assert "core.py:2:" in out and "env.py:3:" in out
    assert "report only" in out


def test_report_counts_zero_on_a_clean_tree(repo: Path, capsys) -> None:
    """A clean tree and a check that never ran must not look alike."""
    _config(repo, '[tool.pf_guards.framework]\ndisable = ["print"]\n')
    assert run_cli(["--report"]) == 0
    assert "0 framework breaches" in capsys.readouterr().out
    assert run_cli(["--framework", "--report"]) == 0
    assert "0 framework breaches" in capsys.readouterr().out


def test_a_clean_gate_stays_quiet(repo: Path, capsys) -> None:
    _config(repo, '[tool.pf_guards.framework]\ndisable = ["print"]\n')
    assert run_cli([]) == 0
    assert "framework breach" not in capsys.readouterr().out


def test_report_runs_the_defaults_before_the_section_exists(repo: Path, capsys) -> None:
    _config(repo)
    assert run_cli(["--framework", "--report"]) == 0
    assert "core.py:2:" in capsys.readouterr().out


def test_framework_flag_runs_only_the_framework_check(repo: Path, capsys) -> None:
    (repo / "src" / "big.py").write_text("x = 1\n" * 600, encoding="utf-8")
    _config(repo, '[tool.pf_guards.framework]\ndisable = ["print"]\n')
    assert run_cli(["--framework"]) == 0
    assert "big.py" not in capsys.readouterr().out
    assert run_cli([]) == 1  # the size gate still runs without the flag


def test_an_exemption_clears_the_breach(repo: Path) -> None:
    _config(
        repo,
        "[tool.pf_guards.framework]\n"
        "[[tool.pf_guards.framework.exempt]]\n"
        f'rule = "print"\npath = "core.py"\nreason = "{REASON}"\n',
    )
    assert run_cli([]) == 0


def test_a_stale_exemption_fails_the_gate(repo: Path, capsys) -> None:
    _config(
        repo,
        "[tool.pf_guards.framework]\n"
        'disable = ["print"]\n'
        "[[tool.pf_guards.framework.exempt]]\n"
        f'rule = "env-read"\npath = "core.py"\nreason = "{REASON}"\n',
    )
    assert run_cli([]) == 1
    out = capsys.readouterr().out
    assert "STALE framework exemption" in out and "env-read" in out


def test_an_exemption_without_a_reason_exits_two(repo: Path, capsys) -> None:
    _config(
        repo,
        '[tool.pf_guards.framework]\n[[tool.pf_guards.framework.exempt]]\nrule = "print"\n'
        'path = "core.py"\n',
    )
    assert run_cli([]) == 2
    assert "reason" in capsys.readouterr().out


def test_the_section_root_narrows_a_multi_root_gate(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("print('fine in a test')\n", encoding="utf-8")
    _config(
        repo,
        '[tool.pf_guards.framework]\nroot = "src"\n[[tool.pf_guards.framework.exempt]]\n'
        f'rule = "print"\npath = "core.py"\nreason = "{REASON}"\n',
        root='["src", "tests"]',
    )
    assert run_cli([]) == 0


def test_a_missing_section_root_exits_two(repo: Path, capsys) -> None:
    _config(repo, '[tool.pf_guards.framework]\nroot = "nope"\n')
    assert run_cli([]) == 2
    assert "nope" in capsys.readouterr().out
