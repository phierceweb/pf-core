from __future__ import annotations

from pathlib import Path

from pf_core.guards.structure import LayeringViolation, check_layering


def _mk(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class TestCheckLayering:
    def test_repo_importing_service_is_violation(self, tmp_path: Path) -> None:
        _mk(tmp_path / "app/repo/entries.py", "from app.services.x import y\n")
        out = check_layering(tmp_path)
        assert out == [LayeringViolation(
            path="app/repo/entries.py", imported="app.services.x",
            reason="repo must not import services",
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
