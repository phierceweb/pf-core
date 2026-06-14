"""Tests for pf_core.utils.io — atomic write helpers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from pf_core.utils.io import atomic_write_json, atomic_write_text


# ---------------------------------------------------------------------------
# atomic_write_text
# ---------------------------------------------------------------------------


class TestAtomicWriteText:
    def test_writes_content_to_target(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello world")
        assert target.read_text() == "hello world"

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        target.write_text("old")
        atomic_write_text(target, "new")
        assert target.read_text() == "new"

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        """Path-like inputs (str or pathlib.Path) both work."""
        target = tmp_path / "out.txt"
        atomic_write_text(str(target), "hello")
        assert target.read_text() == "hello"

    def test_default_encoding_is_utf8(self, tmp_path: Path) -> None:
        """Unicode content round-trips via the default encoding."""
        target = tmp_path / "out.txt"
        atomic_write_text(target, "naïve café — résumé")
        assert target.read_text(encoding="utf-8") == "naïve café — résumé"

    def test_custom_encoding(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        atomic_write_text(target, "naïve", encoding="latin-1")
        assert target.read_bytes() == "naïve".encode("latin-1")

    def test_no_temp_file_left_behind_on_success(self, tmp_path: Path) -> None:
        """After a successful write, the only file in the dir is the target —
        the tempfile cleaned itself up via os.replace."""
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hi")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]

    def test_existing_file_unchanged_when_write_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If the write step blows up after the existing file is on disk,
        the original is untouched — no torn write, no partial update.
        Simulate the failure by monkeypatching os.replace to raise."""
        target = tmp_path / "out.txt"
        target.write_text("original")

        def boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("pf_core.utils.io.os.replace", boom)
        with pytest.raises(OSError, match="simulated rename failure"):
            atomic_write_text(target, "new content")
        assert target.read_text() == "original"

    def test_temp_file_cleaned_up_when_write_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failing write must not leave the dot-tempfile behind cluttering
        the directory."""
        target = tmp_path / "out.txt"

        def boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("pf_core.utils.io.os.replace", boom)
        with pytest.raises(OSError):
            atomic_write_text(target, "new content")
        # No leftover .out.txt.* tempfiles
        leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
        assert leftovers == []

    def test_missing_parent_dir_raises(self, tmp_path: Path) -> None:
        """No silent mkdir — caller is responsible for ensuring the parent
        directory exists. Atomic-write isn't a "create my path for me" tool."""
        target = tmp_path / "nonexistent" / "out.txt"
        with pytest.raises((FileNotFoundError, OSError)):
            atomic_write_text(target, "hi")

    def test_default_mode_is_readable_by_world(self, tmp_path: Path) -> None:
        """Default file mode is 0o644 (rw-r--r--) — readable by all, writable
        only by owner. tempfile.mkstemp's 0o600 default would surprise
        consumers who want their cache/manifest readable by other tools."""
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hi")
        # Mask off the file-type bits, leave just the perm bits.
        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode == 0o644

    def test_custom_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hi", mode=0o600)
        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# atomic_write_json
# ---------------------------------------------------------------------------


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        atomic_write_json(target, {"a": 1, "b": [2, 3]})
        loaded = json.loads(target.read_text())
        assert loaded == {"a": 1, "b": [2, 3]}

    def test_default_indent_is_two(self, tmp_path: Path) -> None:
        """2-space indent makes sidecar JSON files readable in diff tools."""
        target = tmp_path / "out.json"
        atomic_write_json(target, {"a": 1})
        assert "  " in target.read_text()  # at least one 2-space indent

    def test_unicode_preserved_by_default(self, tmp_path: Path) -> None:
        """Default ensure_ascii=False so non-ASCII strings stay readable
        in the JSON file (not escaped to \\uXXXX)."""
        target = tmp_path / "out.json"
        atomic_write_json(target, {"name": "naïve café"})
        text = target.read_text()
        assert "naïve café" in text

    def test_sort_keys_off_by_default(self, tmp_path: Path) -> None:
        """Insertion order preserved by default — caller decides whether
        sorted output matters (matters for diff stability, doesn't matter
        for pure machine-reads)."""
        target = tmp_path / "out.json"
        atomic_write_json(target, {"z": 1, "a": 2})
        text = target.read_text()
        # 'z' should come before 'a' since we didn't ask for sorting
        assert text.index('"z"') < text.index('"a"')

    def test_sort_keys_when_requested(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        atomic_write_json(target, {"z": 1, "a": 2}, sort_keys=True)
        text = target.read_text()
        assert text.index('"a"') < text.index('"z"')

    def test_overwrites_atomically(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        target.write_text('{"old": true}')
        atomic_write_json(target, {"new": True})
        assert json.loads(target.read_text()) == {"new": True}

    def test_non_serializable_raises_target_unchanged(
        self, tmp_path: Path
    ) -> None:
        """A TypeError from json.dumps must not corrupt an existing file."""
        target = tmp_path / "out.json"
        target.write_text('{"original": true}')
        with pytest.raises(TypeError):
            atomic_write_json(target, {"bad": object()})
        assert json.loads(target.read_text()) == {"original": True}
