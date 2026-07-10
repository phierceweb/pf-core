from __future__ import annotations

import json
from pathlib import Path

from pf_core.guards.structure import run_cli


def _write(p: Path, n: int) -> None:
    p.write_text("\n".join(f"x={i}" for i in range(n)) + "\n", encoding="utf-8")


class TestRunCli:
    # chdir into the tmp tree so the repo's own pyproject/baseline can't leak in.
    def test_clean_tree_exits_zero(self, tmp_path: Path, monkeypatch) -> None:
        _write(tmp_path / "ok.py", 100)
        monkeypatch.chdir(tmp_path)
        assert run_cli(["--root", str(tmp_path)]) == 0

    def test_hard_violation_exits_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _write(tmp_path / "big.py", 600)
        monkeypatch.chdir(tmp_path)
        code = run_cli(["--root", str(tmp_path)])
        assert code == 1
        assert "big.py" in capsys.readouterr().out

    def test_soft_violation_warns_but_exits_zero(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _write(tmp_path / "med.py", 400)
        monkeypatch.chdir(tmp_path)
        code = run_cli(["--root", str(tmp_path)])
        assert code == 0
        assert "med.py" in capsys.readouterr().out

    def test_baselined_hard_exits_zero(self, tmp_path: Path, monkeypatch) -> None:
        _write(tmp_path / "big.py", 600)
        bl = tmp_path / "baseline.json"
        bl.write_text(json.dumps({"big.py": 600}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert run_cli(["--root", str(tmp_path), "--baseline", str(bl)]) == 0


class TestRunCliConfig:
    def test_bare_run_reads_pyproject(self, tmp_path: Path, monkeypatch, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pf_guards]\nroot = "src"\n', encoding="utf-8"
        )
        big = tmp_path / "src" / "big.py"
        big.parent.mkdir(parents=True)
        _write(big, 501)
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_config_hard_applies_without_flags(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pf_guards]\nhard = 100\nsoft = 80\n", encoding="utf-8"
        )
        f = tmp_path / "src" / "mid.py"
        f.parent.mkdir(parents=True)
        _write(f, 150)
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 1  # config's hard=100 applies; old hardcoded 500 would pass

    def test_flag_overrides_config(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pf_guards]\nhard = 100\n", encoding="utf-8"
        )
        f = tmp_path / "src" / "mid.py"
        f.parent.mkdir(parents=True)
        _write(f, 150)
        monkeypatch.chdir(tmp_path)
        assert run_cli(["--hard", "500", "--soft", "400"]) == 0  # flags beat config's 100

    def test_missing_root_exits_two(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)  # no pyproject, no src/ — default root doesn't exist
        assert run_cli([]) == 2
        assert "not found" in capsys.readouterr().out

    def test_malformed_pyproject_exits_two(self, tmp_path: Path, monkeypatch, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.pf_guards\nbroken", encoding="utf-8")
        (tmp_path / "src").mkdir()
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 2
        assert "malformed" in capsys.readouterr().out

    def test_nonsense_limits_exit_two(self, tmp_path: Path, monkeypatch, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pf_guards]\nhard = 0\n", encoding="utf-8"
        )
        (tmp_path / "src").mkdir()
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 2
        assert "hard" in capsys.readouterr().out

    def test_nonsense_soft_fraction_exits_two(self, tmp_path: Path, monkeypatch, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pf_guards]\nsoft_fraction = 5.0\n", encoding="utf-8"
        )
        (tmp_path / "src").mkdir()
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 2
        assert "soft_fraction" in capsys.readouterr().out

    def test_layering_violation_fails_gate(self, tmp_path: Path, monkeypatch, capsys) -> None:
        f = tmp_path / "src" / "app" / "orchestrators" / "flow.py"
        f.parent.mkdir(parents=True)
        f.write_text("import app.repo.q\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert run_cli(["--root", "src"]) == 1
        assert "LAYER" in capsys.readouterr().out

    def test_stale_baseline_entry_fails_gate(self, tmp_path: Path, monkeypatch, capsys) -> None:
        (tmp_path / "src").mkdir()
        _write(tmp_path / "src" / "small.py", 50)  # was baselined, now fine
        bl = tmp_path / "baseline.json"
        bl.write_text(json.dumps({"small.py": 600}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert run_cli(["--root", "src", "--baseline", str(bl)]) == 1
        out = capsys.readouterr().out
        assert "STALE" in out and "small.py" in out

    def test_stale_allowlist_entry_fails_gate(self, tmp_path: Path, monkeypatch, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pf_guards]\nroot = "src"\n'
            "[tool.pf_guards.layering_allowlist]\n"
            '"app/api/x.py" = ["app.repo.q"]\n',
            encoding="utf-8",
        )
        f = tmp_path / "src" / "app" / "api" / "x.py"
        f.parent.mkdir(parents=True)
        f.write_text("import os\n", encoding="utf-8")  # no violation → entry is stale
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 1
        out = capsys.readouterr().out
        assert "STALE" in out and "app.repo.q" in out

    def test_multi_root_scans_both_with_prefix_budgets(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pf_guards]\nroot = ["app", "tests"]\n'
            "[tool.pf_guards.limits]\ntests = 600\n",
            encoding="utf-8",
        )
        a = tmp_path / "app" / "cli" / "run.py"
        a.parent.mkdir(parents=True)
        _write(a, 120)  # over cli 100
        t = tmp_path / "tests" / "test_big.py"
        t.parent.mkdir(parents=True)
        _write(t, 650)  # over the tests prefix budget 600
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 1
        out = capsys.readouterr().out
        assert "app/cli/run.py" in out
        assert "tests/test_big.py" in out and "600" in out

    def test_multi_root_missing_one_exits_two(self, tmp_path: Path, monkeypatch, capsys) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pf_guards]\nroot = ["app", "nope"]\n', encoding="utf-8"
        )
        (tmp_path / "app").mkdir()
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 2
        assert "nope" in capsys.readouterr().out

    def test_inline_baseline_suppresses_and_stale_checks(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pf_guards]\nroot = "src"\n'
            "[tool.pf_guards.baseline]\n"
            '"big.py" = 600\n"gone.py" = 700\n',
            encoding="utf-8",
        )
        (tmp_path / "src").mkdir()
        _write(tmp_path / "src" / "big.py", 600)  # suppressed by baseline
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 1  # gone.py entry is stale
        out = capsys.readouterr().out
        assert "FAIL" not in out
        assert "STALE" in out and "gone.py" in out

    def test_string_baseline_in_config_exits_two(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pf_guards]\nbaseline = "x.json"\n', encoding="utf-8"
        )
        (tmp_path / "src").mkdir()
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 2
        assert "malformed" in capsys.readouterr().out

    def test_emit_baseline_prints_paste_ready_block(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        _write(tmp_path / "big.py", 600)
        monkeypatch.chdir(tmp_path)
        assert run_cli(["--root", str(tmp_path), "--emit-baseline"]) == 0
        out = capsys.readouterr().out
        assert "[tool.pf_guards.baseline]" in out
        assert '"big.py" = 600' in out

    def test_emit_allowlist_prints_paste_ready_block(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        f = tmp_path / "src" / "app" / "orchestrators" / "flow.py"
        f.parent.mkdir(parents=True)
        f.write_text("import app.repo.q\nimport app.clients.c\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert run_cli(["--root", "src", "--emit-allowlist"]) == 0
        out = capsys.readouterr().out
        assert "[tool.pf_guards.layering_allowlist]" in out
        assert '"app/orchestrators/flow.py" = ["app.clients.c", "app.repo.q"]' in out

    def test_layering_allowlist_plumbed_from_pyproject(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pf_guards]\nroot = "src"\n'
            "[tool.pf_guards.layering_allowlist]\n"
            '"app/orchestrators/flow.py" = ["app.repo.q"]\n',
            encoding="utf-8",
        )
        f = tmp_path / "src" / "app" / "orchestrators" / "flow.py"
        f.parent.mkdir(parents=True)
        f.write_text("import app.repo.q\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert run_cli([]) == 0  # allowlisted edge no longer fails the gate
