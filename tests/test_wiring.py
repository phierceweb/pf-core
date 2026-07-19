"""Tests for pf_core.wiring — the pf-setup consumer wiring + doctor check rows."""

from __future__ import annotations

from pathlib import Path

from pf_core.wiring import check_wiring, ensure_wiring, installed_docs_dir, run_cli


def _consumer(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    return tmp_path


class TestEnsureWiring:
    def test_links_docs_into_fresh_project(self, tmp_path):
        actions, errors = ensure_wiring(tmp_path)
        assert errors == []
        link = tmp_path / "docs" / "pf-core"
        assert link.is_symlink()
        assert (link / "modules.md").is_file()

    def test_idempotent_second_run(self, tmp_path):
        ensure_wiring(tmp_path)
        actions, errors = ensure_wiring(tmp_path)
        assert errors == []
        assert (tmp_path / "docs" / "pf-core" / "modules.md").is_file()

    def test_replaces_wrong_target_symlink(self, tmp_path):
        stale = tmp_path / "stale"
        stale.mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "pf-core").symlink_to(stale)
        _, errors = ensure_wiring(tmp_path)
        assert errors == []
        assert (tmp_path / "docs" / "pf-core").resolve() == installed_docs_dir().resolve()

    def test_refuses_real_directory_at_docs_link(self, tmp_path):
        real = tmp_path / "docs" / "pf-core"
        real.mkdir(parents=True)
        (real / "keep.md").write_text("mine\n")
        _, errors = ensure_wiring(tmp_path)
        assert len(errors) == 1
        assert "docs/pf-core" in errors[0]
        assert (real / "keep.md").read_text() == "mine\n"


class TestRunCli:
    def test_exit_zero_on_success(self, tmp_path):
        assert run_cli(["--project-root", str(tmp_path)]) == 0

    def test_exit_one_on_refusal(self, tmp_path):
        (tmp_path / "docs" / "pf-core").mkdir(parents=True)
        assert run_cli(["--project-root", str(tmp_path)]) == 1


class TestCheckWiring:
    def test_missing_docs_link_warns_and_names_pf_setup(self, tmp_path):
        rows = check_wiring(_consumer(tmp_path))
        by_name = {name: (status, detail) for name, status, detail in rows}
        status, detail = by_name["docs_link"]
        assert status == "WARN"
        assert "pf-setup" in detail

    def test_docs_link_pass_when_wired_and_no_other_rows(self, tmp_path):
        root = _consumer(tmp_path)
        ensure_wiring(root)
        rows = check_wiring(root)
        assert [(name, status) for name, status, _ in rows] == [("docs_link", "PASS")]

    def test_broken_docs_symlink_fails(self, tmp_path):
        root = _consumer(tmp_path)
        (root / "docs" / "pf-core").symlink_to(root / "gone")
        by_name = {name: status for name, status, _ in check_wiring(root)}
        assert by_name["docs_link"] == "FAIL"

    def test_framework_checkout_skips(self, tmp_path):
        (tmp_path / "src" / "pf_core").mkdir(parents=True)
        rows = check_wiring(tmp_path)
        assert [status for _, status, _ in rows] == ["SKIP"]

    def test_non_consumer_root_skips(self, tmp_path):
        rows = check_wiring(tmp_path)
        assert [status for _, status, _ in rows] == ["SKIP"]


class TestDoctorIntegration:
    def test_doctor_includes_wiring_group(self, tmp_path, monkeypatch):
        from pf_core.doctor import run_checks

        monkeypatch.chdir(_consumer(tmp_path))
        wiring = [r for r in run_checks() if r.group == "wiring"]
        assert wiring
        assert any(r.name == "docs_link" and r.status == "WARN" for r in wiring)
