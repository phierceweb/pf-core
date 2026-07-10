"""Tests for pf_core.guards.config — [tool.pf_guards] loading + limit resolution."""
from __future__ import annotations

from pathlib import Path

from pf_core.guards.config import GuardsConfig, app_rel, hard_limit_for, load_guards_config


class TestLoadGuardsConfig:
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_guards_config(tmp_path / "pyproject.toml")
        assert cfg == GuardsConfig()
        assert (cfg.root, cfg.hard, cfg.soft, cfg.baseline) == ("src", 500, 300, {})

    def test_missing_section_returns_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "x"\nversion = "0.0.1"\n', encoding="utf-8")
        assert load_guards_config(p) == GuardsConfig()

    def test_full_section_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(
            "[tool.pf_guards]\n"
            'root = "src/pf_core"\n'
            "hard = 400\n"
            "soft = 250\n"
            "[tool.pf_guards.baseline]\n"
            '"jobs/repo.py" = 775\n'
            "[tool.pf_guards.layers]\n"
            "orchestrators = 450\n"
            "[tool.pf_guards.limits]\n"
            '"app/api/admin" = 600\n',
            encoding="utf-8",
        )
        cfg = load_guards_config(p)
        assert cfg.root == "src/pf_core"
        assert cfg.baseline == {"jobs/repo.py": 775}
        assert (cfg.hard, cfg.soft) == (400, 250)
        assert cfg.layers == {"orchestrators": 450}
        assert cfg.limits == {"app/api/admin": 600}

    def test_string_baseline_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(
            '[tool.pf_guards]\nbaseline = "some-file.json"\n', encoding="utf-8"
        )
        import pytest

        with pytest.raises(ValueError, match="baseline"):
            load_guards_config(p)

    def test_util_and_soft_fraction_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(
            "[tool.pf_guards]\nutil = 200\nsoft_fraction = 0.9\n", encoding="utf-8"
        )
        cfg = load_guards_config(p)
        assert (cfg.util, cfg.soft_fraction) == (200, 0.9)

    def test_layering_config_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(
            "[tool.pf_guards.allowed_imports]\n"
            'api = ["services", "orchestrators", "repo", "db"]\n'
            "[tool.pf_guards.layering_allowlist]\n"
            '"app/db/cache.py" = ["app.services.parsers.substack"]\n',
            encoding="utf-8",
        )
        cfg = load_guards_config(p)
        assert cfg.allowed_imports == {"api": ["services", "orchestrators", "repo", "db"]}
        assert cfg.layering_allowlist == {"app/db/cache.py": ["app.services.parsers.substack"]}


class TestAppRel:
    def test_app_segment_in_rel(self) -> None:
        assert app_rel(Path("proj"), "app/services/x.py") == "app/services/x.py"

    def test_root_named_app(self) -> None:
        assert app_rel(Path("proj/app"), "services/x.py") == "app/services/x.py"

    def test_outside_app_tree_is_none(self) -> None:
        assert app_rel(Path("src/pf_core"), "jobs/repo.py") is None


class TestHardLimitFor:
    def test_layer_defaults(self) -> None:
        cfg = GuardsConfig()
        assert hard_limit_for("app/cli/run.py", cfg) == 100
        assert hard_limit_for("app/services/catalog.py", cfg) == 300
        assert hard_limit_for("app/orchestrators/flow.py", cfg) == 400

    def test_util_beats_layer(self) -> None:
        assert hard_limit_for("app/api/_util.py", GuardsConfig()) == 150

    def test_util_override(self) -> None:
        assert hard_limit_for("app/api/_util.py", GuardsConfig(util=200)) == 200

    def test_unknown_layer_falls_back_to_flat_hard(self) -> None:
        assert hard_limit_for("app/templates/x.py", GuardsConfig()) == 500

    def test_layer_override_merges(self) -> None:
        cfg = GuardsConfig(layers={"orchestrators": 450})
        assert hard_limit_for("app/orchestrators/flow.py", cfg) == 450
        assert hard_limit_for("app/cli/run.py", cfg) == 100  # defaults survive

    def test_prefix_override_longest_wins_and_beats_util(self) -> None:
        cfg = GuardsConfig(limits={"app/api": 400, "app/api/admin": 600})
        assert hard_limit_for("app/api/admin/panel.py", cfg) == 600
        assert hard_limit_for("app/api/pages.py", cfg) == 400
        assert hard_limit_for("app/api/_util.py", cfg) == 400  # override beats _util rule
