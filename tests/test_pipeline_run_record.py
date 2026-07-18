"""Tests for pf_core.pipeline.run_record."""

from __future__ import annotations

import json
from pathlib import Path

from pf_core.pipeline.run_record import (
    DEFAULT_FILENAME,
    RunRecord,
    file_sha256,
    read_run_record,
    write_run_record,
)


def test_file_sha256_known_input(tmp_path: Path) -> None:
    """Hash a known byte string."""
    f = tmp_path / "input"
    f.write_bytes(b"hello world")
    assert file_sha256(f) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_file_sha256_streams_large_file(tmp_path: Path) -> None:
    """Chunked-streaming path doesn't alter the digest."""
    f = tmp_path / "big"
    f.write_bytes(b"a" * (2 * 1024 * 1024 + 7))
    digest = file_sha256(f)
    assert len(digest) == 64
    assert file_sha256(f) == digest


def test_write_run_record_default_filename(tmp_path: Path) -> None:
    """Default filename is `run.json`."""
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    written = write_run_record(
        out,
        version="1.1.0",
        preset=None,
        resolved_flags={},
        input_path=src,
        started_at="2026-05-10T00:00:00Z",
        finished_at="2026-05-10T00:00:30Z",
    )
    assert written == out / DEFAULT_FILENAME
    assert written.exists()


def test_write_run_record_custom_filename(tmp_path: Path) -> None:
    """`filename=` overrides the default — consumers can use their own."""
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    written = write_run_record(
        out,
        version="1.1.0",
        preset=None,
        resolved_flags={},
        input_path=src,
        started_at="2026-05-10T00:00:00Z",
        finished_at="2026-05-10T00:00:30Z",
        filename=".myapp-run.json",
    )
    assert written == out / ".myapp-run.json"


def test_write_run_record_schema(tmp_path: Path) -> None:
    """Pin the on-disk schema keys."""
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"fake pdf bytes")
    out = tmp_path / "out"
    out.mkdir()
    write_run_record(
        out,
        version="1.1.0",
        preset="rag-default",
        resolved_flags={"cleanup": "basic", "split_sections": True},
        input_path=src,
        started_at="2026-05-10T00:00:00Z",
        finished_at="2026-05-10T00:00:30Z",
        section_count=42,
        image_count=7,
    )
    record = json.loads((out / DEFAULT_FILENAME).read_text(encoding="utf-8"))
    assert record["version"] == "1.1.0"
    assert record["preset"] == "rag-default"
    assert record["resolved_flags"] == {"cleanup": "basic", "split_sections": True}
    assert record["input"] == "doc.pdf"
    assert record["input_sha256"] == file_sha256(src)
    assert record["started_at"] == "2026-05-10T00:00:00Z"
    assert record["finished_at"] == "2026-05-10T00:00:30Z"
    assert record["section_count"] == 42
    assert record["image_count"] == 7


def test_write_run_record_extra_fields_flattened(tmp_path: Path) -> None:
    """`extra=` fields go into the top-level JSON dict (flattened)."""
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    write_run_record(
        out,
        version="1.1.0",
        preset=None,
        resolved_flags={},
        input_path=src,
        started_at="2026-05-10T00:00:00Z",
        finished_at="2026-05-10T00:00:30Z",
        extra={"custom_field": "custom_value", "another": 123},
    )
    record = json.loads((out / DEFAULT_FILENAME).read_text(encoding="utf-8"))
    assert record["custom_field"] == "custom_value"
    assert record["another"] == 123
    # Standard fields still present.
    assert record["version"] == "1.1.0"


def test_write_run_record_input_sha256_stable(tmp_path: Path) -> None:
    """Same input bytes → same sha256 across runs."""
    src1 = tmp_path / "a.pdf"
    src2 = tmp_path / "b.pdf"
    src1.write_bytes(b"identical bytes")
    src2.write_bytes(b"identical bytes")
    out1 = tmp_path / "o1"
    out1.mkdir()
    out2 = tmp_path / "o2"
    out2.mkdir()
    common = {
        "version": "1.1.0",
        "preset": None,
        "resolved_flags": {},
        "started_at": "2026-05-10T00:00:00Z",
        "finished_at": "2026-05-10T00:00:30Z",
    }
    write_run_record(out1, input_path=src1, **common)  # type: ignore[arg-type]
    write_run_record(out2, input_path=src2, **common)  # type: ignore[arg-type]
    r1 = json.loads((out1 / DEFAULT_FILENAME).read_text(encoding="utf-8"))
    r2 = json.loads((out2 / DEFAULT_FILENAME).read_text(encoding="utf-8"))
    assert r1["input_sha256"] == r2["input_sha256"]


def test_read_run_record_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_run_record(tmp_path) is None


def test_read_run_record_returns_dict_when_present(tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    write_run_record(
        out,
        version="1.1.0",
        preset="test",
        resolved_flags={"a": 1},
        input_path=src,
        started_at="2026-05-10T00:00:00Z",
        finished_at="2026-05-10T00:00:30Z",
    )
    record = read_run_record(out)
    assert record is not None
    assert record["version"] == "1.1.0"
    assert record["preset"] == "test"


def test_read_run_record_custom_filename(tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    write_run_record(
        out,
        version="1.1.0",
        preset=None,
        resolved_flags={},
        input_path=src,
        started_at="2026-05-10T00:00:00Z",
        finished_at="2026-05-10T00:00:30Z",
        filename="custom.json",
    )
    assert read_run_record(out, filename="custom.json") is not None
    assert read_run_record(out, filename="other.json") is None


def test_run_record_dataclass_fields() -> None:
    """The `RunRecord` dataclass mirrors the JSON schema."""
    rec = RunRecord(
        version="1.1.0",
        preset=None,
        resolved_flags={},
        input="doc.pdf",
        input_sha256="abc",
        started_at="t1",
        finished_at="t2",
    )
    assert rec.section_count is None
    assert rec.image_count is None
    assert rec.extra == {}
