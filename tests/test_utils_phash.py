"""Tests for pf_core.utils.phash — perceptual-hash image dedup helpers.

These tests exercise the hex-string utilities (Hamming distance,
clustering, decoration detection) directly. The ``compute_phash``
function itself needs ``ImageHash`` + ``Pillow`` from the
``[image-phash]`` extra; we test it with real image files when the
deps are importable, and skip cleanly when they aren't.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from pf_core.utils.phash import (
    cluster_phashes,
    detect_decoration_basenames,
    hamming_distance_hex,
)


# --- hamming_distance_hex ----------------------------------------------------


def test_hamming_distance_hex_identical_is_zero() -> None:
    assert hamming_distance_hex("00ff", "00ff") == 0


def test_hamming_distance_hex_single_bit_difference() -> None:
    # 0x00 vs 0x01 = 1 bit flipped.
    assert hamming_distance_hex("00", "01") == 1


def test_hamming_distance_hex_all_bits_different() -> None:
    # 0xff vs 0x00 = 8 bits flipped.
    assert hamming_distance_hex("ff", "00") == 8


def test_hamming_distance_hex_mismatched_lengths_returns_worst_case() -> None:
    """Malformed input doesn't raise — it just reports max distance so
    a clustering pass continues."""
    # 4 hex chars * 4 bits/char = 16.
    assert hamming_distance_hex("00", "0000") == 16


# --- cluster_phashes ---------------------------------------------------------


def test_cluster_phashes_empty_returns_empty() -> None:
    assert cluster_phashes([], max_distance=0) == []


def test_cluster_phashes_identical_hashes_collapse() -> None:
    """Same hash listed twice collapses to one cluster of size 1
    (sets deduplicate)."""
    clusters = cluster_phashes(["00ff", "00ff"], max_distance=0)
    assert len(clusters) == 1
    assert clusters[0] == {"00ff"}


def test_cluster_phashes_distinct_hashes_dont_cluster() -> None:
    clusters = cluster_phashes(["00", "ff"], max_distance=2)
    # 0x00 vs 0xff = 8 bits, exceeds threshold 2.
    assert len(clusters) == 2


def test_cluster_phashes_near_duplicates_cluster() -> None:
    """Within-threshold pairs land in one cluster via union-find."""
    clusters = cluster_phashes(["00", "01", "03"], max_distance=2)
    # 00 vs 01: 1 bit. 01 vs 03: 1 bit. 00 vs 03: 2 bits. All linked.
    assert len(clusters) == 1
    assert clusters[0] == {"00", "01", "03"}


def test_cluster_phashes_transitive_link_via_intermediate() -> None:
    """A→B and B→C link even when A→C exceeds threshold (union-find)."""
    # 00→01 (1 bit), 01→03 (1 bit), 00→03 (2 bits)
    # If threshold is 1, 00 and 03 are NOT directly linked but become
    # linked through 01.
    clusters = cluster_phashes(["00", "01", "03"], max_distance=1)
    assert len(clusters) == 1


# --- detect_decoration_basenames --------------------------------------------


def test_detect_decoration_basenames_empty_returns_empty(tmp_path: Path) -> None:
    """No images → no decorations."""
    assert detect_decoration_basenames([], threshold=2, hamming_distance=4) == set()


def test_detect_decoration_basenames_missing_files_skipped(tmp_path: Path) -> None:
    """A missing path is logged and skipped, not raised."""
    nonexistent = tmp_path / "no-such-image.png"
    # Should NOT raise even though file is missing.
    result = detect_decoration_basenames([nonexistent], threshold=1, hamming_distance=4)
    assert result == set()


# --- compute_phash + integration (skip when extra not installed) ------------


def _have_phash_deps() -> bool:
    try:
        import imagehash  # noqa: F401
        from PIL import Image  # noqa: F401

        return True
    except ImportError:
        return False


def _make_test_png(path: Path, *, size: int = 16) -> None:
    """Write a tiny but phash-able PNG. ImageHash needs ≥ 8x8 to do
    the DCT — 1x1 transparent fixtures used elsewhere are too small."""
    from PIL import Image  # type: ignore[import-not-found]

    img = Image.new("RGB", (size, size), color=(128, 128, 128))
    img.save(path, format="PNG")


@pytest.mark.skipif(not _have_phash_deps(), reason="[image-phash] extra not installed")
def test_compute_phash_returns_hex_string(tmp_path: Path) -> None:
    """A 16x16 PNG hashes to a 16-char hex string (8x8 DCT, 64 bits)."""
    from pf_core.utils.phash import compute_phash

    target = tmp_path / "img.png"
    _make_test_png(target)
    digest = compute_phash(target)
    assert isinstance(digest, str)
    assert len(digest) == 16
    # Hex.
    int(digest, 16)


def _write_psd(path: Path) -> None:
    """8x8 grayscale PSD that Pillow's PSD plugin decodes: header, three empty sections, raw pixels."""
    header = b"8BPS" + struct.pack(">H6xHIIHH", 1, 1, 8, 8, 8, 1)
    path.write_bytes(header + bytes(12) + bytes(2) + bytes(range(0, 256, 4)))


@pytest.mark.skipif(not _have_phash_deps(), reason="[image-phash] extra not installed")
def test_compute_phash_refuses_psd_without_decoding(tmp_path: Path, monkeypatch) -> None:
    """Pillow picks the decoder by content, so a PSD named .png must not reach the PSD plugin."""
    from PIL import PsdImagePlugin, UnidentifiedImageError

    from pf_core.utils.phash import compute_phash

    target = tmp_path / "embedded.png"
    _write_psd(target)
    reached: list[object] = []
    real_open = PsdImagePlugin.PsdImageFile._open

    def spy(self: PsdImagePlugin.PsdImageFile) -> None:
        reached.append(self)
        real_open(self)

    monkeypatch.setattr(PsdImagePlugin.PsdImageFile, "_open", spy)

    with pytest.raises(UnidentifiedImageError):
        compute_phash(target)
    assert reached == []


@pytest.mark.skipif(not _have_phash_deps(), reason="[image-phash] extra not installed")
@pytest.mark.parametrize("fmt", ["JPEG2000", "AVIF", "ICO", "TGA", "PPM"])
def test_compute_phash_refuses_other_raster_formats(tmp_path: Path, fmt: str) -> None:
    """Formats outside the allowlist are refused even when Pillow can decode them, so widening the list fails here."""
    from PIL import Image, UnidentifiedImageError

    from pf_core.utils.phash import compute_phash

    target = tmp_path / "embedded.png"
    try:
        Image.linear_gradient("L").convert("RGB").resize((32, 32)).save(target, format=fmt)
    except (KeyError, OSError):
        pytest.skip(f"this Pillow cannot write {fmt}")
    with Image.open(target) as unrestricted:
        assert unrestricted.format == fmt

    with pytest.raises(UnidentifiedImageError):
        compute_phash(target)


@pytest.mark.skipif(not _have_phash_deps(), reason="[image-phash] extra not installed")
@pytest.mark.parametrize(
    ("fmt", "name"),
    [
        ("PNG", "img.png"),
        ("JPEG", "img.jpg"),
        ("MPO", "multi-picture.jpg"),
        ("GIF", "img.gif"),
        ("BMP", "img.bmp"),
        ("TIFF", "img.tif"),
        ("WEBP", "img.webp"),
    ],
)
def test_compute_phash_hashes_document_formats(tmp_path: Path, fmt: str, name: str) -> None:
    """Each format documents carry hashes exactly as an unrestricted open does, so cache keys hold."""
    import imagehash
    from PIL import Image

    from pf_core.utils.phash import compute_phash

    target = tmp_path / name
    img = Image.linear_gradient("L").convert("RGB").resize((32, 32))
    extra = {"save_all": True, "append_images": [img]} if fmt == "MPO" else {}
    img.save(target, format=fmt, **extra)
    with Image.open(target) as reference:
        assert reference.format == fmt
        expected = str(imagehash.phash(reference))

    assert compute_phash(target) == expected


@pytest.mark.skipif(not _have_phash_deps(), reason="[image-phash] extra not installed")
def test_detect_decoration_basenames_flags_repeated_image(tmp_path: Path) -> None:
    """Three copies of the same image with threshold=3 → all flagged."""
    image_paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        _make_test_png(p)
        image_paths.append(p)

    decorations = detect_decoration_basenames(image_paths, threshold=3, hamming_distance=4)
    assert decorations == {"img0.png", "img1.png", "img2.png"}


def test_compute_phash_missing_deps_raises_import_error(monkeypatch) -> None:
    """When the [image-phash] extra isn't installed, compute_phash
    raises a clear ImportError pointing at the install command. Simulate
    the no-extra state via ``sys.modules['imagehash'] = None`` —
    Python's import machinery treats that as "import already failed"
    and raises ``ImportError`` on the next ``import imagehash``. Lets
    the test exercise the error branch in any install matrix instead
    of skipping when the extra is present."""
    import sys

    monkeypatch.setitem(sys.modules, "imagehash", None)
    from pf_core.utils.phash import compute_phash

    with pytest.raises(ImportError, match=r"\[image-phash\] extra"):
        compute_phash(Path("/nonexistent.png"))
