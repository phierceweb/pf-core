from __future__ import annotations

from pathlib import Path

from pf_core.guards import GuardsConfig, LayeringViolation, check_layering


def _mk(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class TestCheckLayering:
    def test_repo_importing_service_is_violation(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/repo/entries.py", "from app.services.x import y\n")
        out = check_layering(tmp_path)
        assert out == [LayeringViolation(
            path="app/repo/entries.py", imported="app.services.x",
            reason="repo → services, repo must not import from upper layers", line=1,
        )]

    def test_allowed_downward_import_is_clean(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/services/x.py", "from app.repo.entries import y\n")
        assert check_layering(tmp_path) == []

    def test_orchestrator_direct_transaction_is_violation(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/orchestrators/run.py", "from pf_core.db import transaction\n")
        out = check_layering(tmp_path)
        assert out and out[0].reason == "orchestrator must not open transaction() directly"

    def test_non_app_file_ignored(self, tmp_path: Path) -> None:
        _mk(tmp_path / "scripts/tool.py", "from app.services.x import y\n")
        assert check_layering(tmp_path) == []


class TestLayeringParity:
    def test_orchestrator_importing_repo_is_violation(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/orchestrators/flow.py", "import app.repo.catalog\n")
        (v,) = check_layering(tmp_path)
        assert "orchestrators" in v.reason and "repo" in v.reason
        assert "through services" in v.reason
        assert v.line == 1

    def test_line_number_reported(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/api/pages.py", "import os\n\nfrom app.repo import q\n")
        (v,) = check_layering(tmp_path)
        assert v.line == 3

    def test_skip_comment_honored(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/api/x.py", "# lint-layers: skip\nimport app.repo.q\n")
        assert check_layering(tmp_path) == []

    def test_tests_and_conftest_skipped(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/services/tests/test_s.py", "import app.api.pages\n")
        _mk(tmp_path / "app/services/conftest.py", "import app.api.pages\n")
        assert check_layering(tmp_path) == []

    def test_root_is_app_dir(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/repo/q.py", "from app.services import s\n")
        (v,) = check_layering(tmp_path / "app")
        assert "repo" in v.reason

    def test_orchestrator_pf_core_db_rule_kept(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/orchestrators/flow.py", "from pf_core.db import transaction\n")
        (v,) = check_layering(tmp_path)
        assert "transaction()" in v.reason

    def test_unparseable_file_skipped(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/api/broken.py", "def oops(:\n")
        assert check_layering(tmp_path) == []


class TestLayeringConfig:
    def test_allowed_imports_override_replaces_per_key(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/api/pages.py", "from app.repo import q\n")
        _mk(tmp_path / "app/cli/run.py", "from app.repo import q\n")
        cfg = GuardsConfig(allowed_imports={"api": ["services", "orchestrators", "repo", "db"]})
        out = check_layering(tmp_path, config=cfg)
        # api → repo now allowed; cli keeps the default rules and still violates
        assert [v.path for v in out] == ["app/cli/run.py"]

    def test_custom_layer_becomes_checked(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/workers/w.py", "from app.api import pages\n")
        cfg = GuardsConfig(allowed_imports={"workers": ["services", "db"]})
        (v,) = check_layering(tmp_path, config=cfg)
        assert "workers" in v.reason and "api" in v.reason

    def test_allowlist_exempts_exact_edge(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/db/cache.py", "import app.services.parsers.substack\n")
        cfg = GuardsConfig(
            layering_allowlist={"app/db/cache.py": ["app.services.parsers.substack"]}
        )
        assert check_layering(tmp_path, config=cfg) == []

    def test_allowlist_other_edges_still_violate(self, tmp_path: Path) -> None:
        _mk(
            tmp_path / "app/db/cache.py",
            "import app.services.parsers.substack\nimport app.repo.entries\n",
        )
        cfg = GuardsConfig(
            layering_allowlist={"app/db/cache.py": ["app.services.parsers.substack"]}
        )
        (v,) = check_layering(tmp_path, config=cfg)
        assert v.imported == "app.repo.entries"

    def test_allowlist_covers_db_transaction_rule(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/orchestrators/flow.py", "from pf_core.db import transaction\n")
        cfg = GuardsConfig(layering_allowlist={"app/orchestrators/flow.py": ["pf_core.db"]})
        assert check_layering(tmp_path, config=cfg) == []

    def test_allowlist_keys_app_relative_when_root_is_app(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/repo/q.py", "from app.services import s\n")
        cfg = GuardsConfig(layering_allowlist={"app/repo/q.py": ["app.services"]})
        assert check_layering(tmp_path / "app", config=cfg) == []

    def test_no_config_behavior_unchanged(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/repo/q.py", "from app.services import s\n")
        assert len(check_layering(tmp_path)) == 1


class TestRelativeImports:
    def test_relative_upward_import_is_violation(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/orchestrators/flow.py", "from ..repo import entries\n")
        (v,) = check_layering(tmp_path)
        assert "orchestrators" in v.reason and "repo" in v.reason
        assert v.imported == "app.repo"

    def test_relative_module_path_resolved(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/api/pages.py", "from ..clients.routing import route\n")
        (v,) = check_layering(tmp_path)
        assert v.imported == "app.clients.routing"

    def test_from_dot_import_name_resolved(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/orchestrators/flow.py", "from .. import repo\n")
        (v,) = check_layering(tmp_path)
        assert "repo" in v.reason

    def test_same_package_relative_is_clean(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/services/x.py", "from . import helpers\nfrom .helpers import f\n")
        assert check_layering(tmp_path) == []

    def test_relative_downward_is_clean(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/services/x.py", "from ..repo import entries\n")
        assert check_layering(tmp_path) == []

    def test_init_relative_resolution(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/api/__init__.py", "from ..clients import routing\n")
        (v,) = check_layering(tmp_path)
        assert v.imported == "app.clients"


class TestDbLayer:
    def test_db_importing_services_is_violation(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/db/cache.py", "from app.services.parsers import x\n")
        (v,) = check_layering(tmp_path)
        assert "db" in v.reason and "services" in v.reason
        assert v.line == 1

    def test_db_importing_repo_is_violation(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/db/cache.py", "import app.repo.entries\n")
        (v,) = check_layering(tmp_path)
        assert "db" in v.reason

    def test_db_as_target_allowed_from_any_layer(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/api/pages.py", "from app.db.cache import y\n")
        _mk(tmp_path / "app/services/s.py", "from app.db.cache import y\n")
        _mk(tmp_path / "app/repo/r.py", "from app.db.cache import y\n")
        assert check_layering(tmp_path) == []
