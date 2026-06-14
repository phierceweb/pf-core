from __future__ import annotations

from pathlib import Path

from pf_core.guards.structure import FileSizeViolation, filter_baselined, scan_file_sizes


def _write(p: Path, n_lines: int) -> None:
    p.write_text("\n".join(f"x = {i}" for i in range(n_lines)) + "\n", encoding="utf-8")


class TestScanFileSizes:
    def test_hard_violation_flagged(self, tmp_path: Path) -> None:
        _write(tmp_path / "big.py", 600)
        out = scan_file_sizes(tmp_path, hard=500, soft=300)
        assert out == [FileSizeViolation(path="big.py", lines=600, limit=500, severity="hard")]

    def test_soft_violation_flagged(self, tmp_path: Path) -> None:
        _write(tmp_path / "med.py", 400)
        out = scan_file_sizes(tmp_path, hard=500, soft=300)
        assert out == [FileSizeViolation(path="med.py", lines=400, limit=300, severity="soft")]

    def test_under_soft_is_clean(self, tmp_path: Path) -> None:
        _write(tmp_path / "ok.py", 200)
        assert scan_file_sizes(tmp_path, hard=500, soft=300) == []

    def test_non_python_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("\n" * 600, encoding="utf-8")
        assert scan_file_sizes(tmp_path, hard=500, soft=300) == []

    def test_paths_are_relative_posix(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        _write(tmp_path / "pkg" / "big.py", 600)
        out = scan_file_sizes(tmp_path, hard=500, soft=300)
        assert out[0].path == "pkg/big.py"


class TestBaseline:
    def test_baselined_hard_violation_is_suppressed(self) -> None:
        v = [FileSizeViolation("big.py", 600, 500, "hard")]
        assert filter_baselined(v, baseline={"big.py": 600}) == []

    def test_growth_beyond_baseline_is_reported(self) -> None:
        v = [FileSizeViolation("big.py", 650, 500, "hard")]
        out = filter_baselined(v, baseline={"big.py": 600})
        assert out == [FileSizeViolation("big.py", 650, 500, "hard")]

    def test_shrink_below_baseline_still_suppressed(self) -> None:
        v = [FileSizeViolation("big.py", 540, 500, "hard")]
        assert filter_baselined(v, baseline={"big.py": 600}) == []

    def test_new_file_not_in_baseline_is_reported(self) -> None:
        v = [FileSizeViolation("new.py", 600, 500, "hard")]
        assert filter_baselined(v, baseline={"big.py": 600}) == [v[0]]

    def test_soft_violations_never_baselined(self) -> None:
        v = [FileSizeViolation("med.py", 400, 300, "soft")]
        assert filter_baselined(v, baseline={"med.py": 400}) == v
