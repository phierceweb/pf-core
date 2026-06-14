from __future__ import annotations

import json
from pathlib import Path

from pf_core.guards.structure import run_cli


def _write(p: Path, n: int) -> None:
    p.write_text("\n".join(f"x={i}" for i in range(n)) + "\n", encoding="utf-8")


class TestRunCli:
    def test_clean_tree_exits_zero(self, tmp_path: Path) -> None:
        _write(tmp_path / "ok.py", 100)
        assert run_cli(["--root", str(tmp_path)]) == 0

    def test_hard_violation_exits_one(self, tmp_path: Path, capsys) -> None:
        _write(tmp_path / "big.py", 600)
        code = run_cli(["--root", str(tmp_path)])
        assert code == 1
        assert "big.py" in capsys.readouterr().out

    def test_soft_violation_warns_but_exits_zero(self, tmp_path: Path, capsys) -> None:
        _write(tmp_path / "med.py", 400)
        code = run_cli(["--root", str(tmp_path)])
        assert code == 0
        assert "med.py" in capsys.readouterr().out

    def test_baselined_hard_exits_zero(self, tmp_path: Path) -> None:
        _write(tmp_path / "big.py", 600)
        bl = tmp_path / "baseline.json"
        bl.write_text(json.dumps({"big.py": 600}), encoding="utf-8")
        assert run_cli(["--root", str(tmp_path), "--baseline", str(bl)]) == 0
