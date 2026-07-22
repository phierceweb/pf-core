"""Tests for pf_core.export — incremental markdown export + YAML frontmatter."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pf_core.export import ExportResult, MarkdownExporter, yaml_frontmatter


class _FakeExporter(MarkdownExporter):
    """Test double: yields a caller-supplied list of (relpath, content)."""

    def __init__(self, artifacts: list[tuple[str, str]]) -> None:
        self._artifacts = artifacts

    def iter_artifacts(self) -> Iterator[tuple[str, str]]:
        yield from self._artifacts


# ---------------------------------------------------------------------------
# MarkdownExporter.export — write
# ---------------------------------------------------------------------------


class TestExportWrite:
    def test_writes_all_artifacts(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("a.md", "alpha"), ("b.md", "bravo")])
        result = exp.export(tmp_path)
        assert isinstance(result, ExportResult)
        assert (result.written, result.unchanged, result.pruned) == (2, 0, 0)
        assert (tmp_path / "a.md").read_text() == "alpha"
        assert (tmp_path / "b.md").read_text() == "bravo"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("deep/nested/x.md", "hi")])
        exp.export(tmp_path)
        assert (tmp_path / "deep" / "nested" / "x.md").read_text() == "hi"

    def test_result_paths_are_sorted_relpaths(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("b.md", "b"), ("a.md", "a"), ("c/d.md", "d")])
        result = exp.export(tmp_path)
        assert result.paths == ["a.md", "b.md", "c/d.md"]


class TestExportIncremental:
    def test_reexport_identical_is_unchanged_and_preserves_mtime(
        self, tmp_path: Path
    ) -> None:
        exp = _FakeExporter([("a.md", "alpha"), ("b.md", "bravo")])
        exp.export(tmp_path)
        mtimes = {
            p.name: (tmp_path / p.name).stat().st_mtime_ns
            for p in tmp_path.iterdir()
        }

        result = exp.export(tmp_path)

        assert (result.written, result.unchanged, result.pruned) == (0, 2, 0)
        assert (tmp_path / "a.md").stat().st_mtime_ns == mtimes["a.md"]
        assert (tmp_path / "b.md").stat().st_mtime_ns == mtimes["b.md"]

    def test_only_changed_artifact_is_rewritten(self, tmp_path: Path) -> None:
        _FakeExporter([("a.md", "alpha"), ("b.md", "bravo")]).export(tmp_path)

        result = _FakeExporter(
            [("a.md", "alpha"), ("b.md", "BRAVO-2")]
        ).export(tmp_path)

        assert (result.written, result.unchanged) == (1, 1)
        assert (tmp_path / "b.md").read_text() == "BRAVO-2"


class TestExportPrune:
    def test_removed_artifact_is_pruned(self, tmp_path: Path) -> None:
        _FakeExporter([("a.md", "a"), ("b.md", "b")]).export(tmp_path)

        result = _FakeExporter([("a.md", "a")]).export(tmp_path)

        assert result.pruned == 1
        assert not (tmp_path / "b.md").exists()
        assert (tmp_path / "a.md").exists()

    def test_prune_only_touches_managed_suffixes(self, tmp_path: Path) -> None:
        """A non-managed file (.txt) living in a produced directory survives."""
        _FakeExporter([("a.md", "a")]).export(tmp_path)
        (tmp_path / "keep.txt").write_text("hand-written, not ours")

        _FakeExporter([("a.md", "a")]).export(tmp_path)

        assert (tmp_path / "keep.txt").read_text() == "hand-written, not ours"

    def test_prune_does_not_touch_unproduced_directories(
        self, tmp_path: Path
    ) -> None:
        """A managed file in a directory the exporter never wrote to survives."""
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "keep.md").write_text("not in the export's subtree")

        _FakeExporter([("sub/a.md", "a")]).export(tmp_path)

        assert (tmp_path / "other" / "keep.md").exists()


class TestExportValidation:
    def test_rejects_absolute_relpath(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("/etc/passwd", "nope")])
        with pytest.raises(ValueError, match="relative"):
            exp.export(tmp_path)

    def test_rejects_parent_traversal(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("../escape.md", "nope")])
        with pytest.raises(ValueError, match=r"\.\.|escape|outside"):
            exp.export(tmp_path)

    def test_base_iter_artifacts_is_abstract(self, tmp_path: Path) -> None:
        with pytest.raises(NotImplementedError):
            MarkdownExporter().export(tmp_path)


# ---------------------------------------------------------------------------
# yaml_frontmatter
# ---------------------------------------------------------------------------


class TestYamlFrontmatter:
    def test_wraps_in_delimiters(self) -> None:
        out = yaml_frontmatter({"slug": "acme"})
        assert out.startswith("---\n")
        assert out.endswith("---\n")

    def test_safe_string_is_bare(self) -> None:
        assert "slug: acme" in yaml_frontmatter({"slug": "acme"})

    def test_colon_is_quoted(self) -> None:
        assert 'title: "a: b"' in yaml_frontmatter({"title": "a: b"})

    def test_double_quote_is_escaped(self) -> None:
        out = yaml_frontmatter({"name": 'say "hi"'})
        assert r'name: "say \"hi\""' in out

    def test_leading_dash_is_quoted(self) -> None:
        assert 'x: "- dash"' in yaml_frontmatter({"x": "- dash"})

    def test_unicode_is_preserved_bare(self) -> None:
        out = yaml_frontmatter({"city": "café"})
        assert "city: café" in out

    def test_none_value_is_omitted(self) -> None:
        out = yaml_frontmatter({"a": "x", "b": None})
        assert "b:" not in out
        assert "a: x" in out

    def test_list_is_block_sequence(self) -> None:
        out = yaml_frontmatter({"tags": ["punk", "dc"]})
        assert "tags:\n  - punk\n  - dc" in out

    def test_empty_list_is_omitted(self) -> None:
        assert "tags" not in yaml_frontmatter({"tags": []})

    def test_bool_is_unquoted(self) -> None:
        out = yaml_frontmatter({"loved": True, "hated": False})
        assert "loved: true" in out
        assert "hated: false" in out

    def test_int_is_unquoted(self) -> None:
        assert "plays: 892" in yaml_frontmatter({"plays": 892})

    def test_numeric_string_is_quoted(self) -> None:
        """A string that looks like a number stays a string."""
        assert 'zip: "90210"' in yaml_frontmatter({"zip": "90210"})


# ---------------------------------------------------------------------------
# MarkdownExporter.check — dry-run freshness gate
# ---------------------------------------------------------------------------


class TestCheck:
    def test_fresh_dir_reports_everything_stale(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("a.md", "alpha"), ("sub/b.md", "bravo")])
        assert exp.check(tmp_path) == ["a.md", "sub/b.md"]

    def test_clean_tree_reports_nothing(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("a.md", "alpha"), ("sub/b.md", "bravo")])
        exp.export(tmp_path)
        assert exp.check(tmp_path) == []

    def test_content_drift_and_missing_reported(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("a.md", "alpha"), ("b.md", "bravo")])
        exp.export(tmp_path)
        (tmp_path / "a.md").write_text("edited by hand")
        (tmp_path / "b.md").unlink()
        assert exp.check(tmp_path) == ["a.md", "b.md"]

    def test_orphan_reported(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("a.md", "alpha")])
        exp.export(tmp_path)
        (tmp_path / "zombie.md").write_text("orphan")
        assert exp.check(tmp_path) == ["zombie.md"]

    def test_check_writes_nothing(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("a.md", "alpha")])
        exp.export(tmp_path)
        (tmp_path / "zombie.md").write_text("orphan")
        before = sorted(p.name for p in tmp_path.iterdir())
        mtime = (tmp_path / "a.md").stat().st_mtime_ns
        exp.check(tmp_path)
        assert sorted(p.name for p in tmp_path.iterdir()) == before
        assert (tmp_path / "a.md").stat().st_mtime_ns == mtime

    def test_unmanaged_suffix_never_reported(self, tmp_path: Path) -> None:
        exp = _FakeExporter([("a.md", "alpha")])
        exp.export(tmp_path)
        (tmp_path / "notes.txt").write_text("hand-placed")
        assert exp.check(tmp_path) == []


# ---------------------------------------------------------------------------
# force_prune_dirs — stable subdirectories stay in prune scope
# ---------------------------------------------------------------------------


class _ForceExporter(_FakeExporter):
    force_prune_dirs = ("sections",)


class TestForcePruneDirs:
    def test_default_keeps_orphans_in_unproduced_dirs(self, tmp_path: Path) -> None:
        _FakeExporter([("sections/old.md", "x")]).export(tmp_path)
        result = _FakeExporter([("index.md", "i")]).export(tmp_path)
        assert result.pruned == 0
        assert (tmp_path / "sections" / "old.md").exists()

    def test_force_dir_pruned_when_it_yields_nothing(self, tmp_path: Path) -> None:
        _ForceExporter([("sections/old.md", "x")]).export(tmp_path)
        result = _ForceExporter([("index.md", "i")]).export(tmp_path)
        assert result.pruned == 1
        assert not (tmp_path / "sections" / "old.md").exists()

    def test_missing_force_dir_is_fine(self, tmp_path: Path) -> None:
        result = _ForceExporter([("index.md", "i")]).export(tmp_path)
        assert result.pruned == 0

    def test_check_honors_force_dirs(self, tmp_path: Path) -> None:
        _ForceExporter([("sections/old.md", "x")]).export(tmp_path)
        exp = _ForceExporter([("index.md", "i")])
        assert exp.check(tmp_path) == ["index.md", "sections/old.md"]
