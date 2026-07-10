from __future__ import annotations

from pathlib import Path

from pf_core.guards.config import GuardsConfig
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


def _write_at(p: Path, n_lines: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    _write(p, n_lines)


class TestScanFileSizesLayerAware:
    def test_app_layer_hard_limit_applies(self, tmp_path: Path) -> None:
        _write_at(tmp_path / "app" / "cli" / "run.py", 120)  # cli hard = 100
        (v,) = scan_file_sizes(tmp_path, config=GuardsConfig())
        assert (v.severity, v.limit, v.lines) == ("hard", 100, 120)

    def test_app_layer_soft_is_08_of_hard(self, tmp_path: Path) -> None:
        _write_at(tmp_path / "app" / "orchestrators" / "flow.py", 330)  # 320 < 330 <= 400
        (v,) = scan_file_sizes(tmp_path, config=GuardsConfig())
        assert (v.severity, v.limit) == ("soft", 320)

    def test_non_app_files_keep_flat_limits_with_config(self, tmp_path: Path) -> None:
        _write_at(tmp_path / "pkg" / "big.py", 400)  # soft under flat 300/500
        (v,) = scan_file_sizes(tmp_path, config=GuardsConfig())
        assert (v.severity, v.limit) == ("soft", 300)

    def test_soft_fraction_override_widens_warn_band(self, tmp_path: Path) -> None:
        _write_at(tmp_path / "app" / "orchestrators" / "flow.py", 330)  # soft under 1.0×400
        assert scan_file_sizes(tmp_path, config=GuardsConfig(soft_fraction=1.0)) == []

    def test_no_config_behavior_unchanged(self, tmp_path: Path) -> None:
        _write_at(tmp_path / "app" / "cli" / "run.py", 120)  # would fail per-layer
        assert scan_file_sizes(tmp_path) == []  # flat 300/500: clean
