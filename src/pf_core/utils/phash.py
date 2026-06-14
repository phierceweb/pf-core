"""Perceptual-hash helpers for image-dedup and decoration detection.

DCT-based 8x8 hash (64-bit, hex-encoded) via the ``ImageHash`` library.
Catches re-encoded duplicates that sha256 would miss — the same source
figure rasterized at different resolutions or with minor margin
variation still produces near-identical phashes.

The toolkit:

- :func:`compute_phash` — per-image hash.
- :func:`hamming_distance_hex` — bit-distance between two hex phashes.
- :func:`cluster_phashes` — union-find clustering by Hamming distance.
- :func:`detect_decoration_basenames` — high-level helper that walks a
  directory of images, clusters them, and returns the basenames of any
  cluster large enough to be a recurring page-decoration (header logo,
  watermark, footer mark).

Generic image-dedup machinery — e.g. stripping repeated page-header
images across paginated documents. Available via the optional
``[image-phash]`` extra.

Usage::

    from pf_core.utils.phash import detect_decoration_basenames

    decoration_basenames = detect_decoration_basenames(
        list(Path("./images").iterdir()),
        threshold=10,           # >= N occurrences → decoration
        hamming_distance=12,    # cluster phashes within 12 bits
    )
"""

from __future__ import annotations

from pathlib import Path

from pf_core.log import get_logger

logger = get_logger(__name__)


def compute_phash(image_path: Path) -> str:
    """Perceptual hash of an image as a 16-char hex string.

    Returns a DCT-based 8x8 hash (64-bit). Subtle render differences
    (margin variation, scan-quality drift) flip a few bits even for the
    same logo, so callers comparing hashes should pair this with
    :func:`hamming_distance_hex` and a small distance threshold rather
    than checking for equality.

    Raises:
        ImportError: If the ``ImageHash`` / ``Pillow`` extra isn't
            installed (``pip install 'pf-core[image-phash]'``).
    """
    try:
        import imagehash  # type: ignore[import-untyped]
        from PIL import Image
    except ImportError as e:
        raise ImportError(
            "pf_core.utils.phash requires the [image-phash] extra. "
            "Install with: pip install 'pf-core[image-phash]'"
        ) from e

    with Image.open(image_path) as img:
        return str(imagehash.phash(img))


def hamming_distance_hex(a: str, b: str) -> int:
    """Hamming distance (bits) between two hex-encoded perceptual hashes.

    Mismatched lengths return a worst-case distance equal to
    ``max(len(a), len(b)) * 4`` — treated as maximally-different rather
    than raising, so a malformed input doesn't crash a clustering pass.
    """
    if len(a) != len(b):
        return max(len(a), len(b)) * 4
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def cluster_phashes(phashes: list[str], *, max_distance: int) -> list[set[str]]:
    """Cluster phashes by Hamming distance via simple union-find.

    Phashes within ``max_distance`` bits of each other land in the same
    cluster. Returns a list of clusters, each a set of phash strings.
    O(N²) — fine at the typical hundreds-of-images scale; do not use on
    millions of phashes without a more sophisticated index.
    """
    parent: dict[str, str] = {p: p for p in phashes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i, a in enumerate(phashes):
        for b in phashes[i + 1 :]:
            if hamming_distance_hex(a, b) <= max_distance:
                union(a, b)

    clusters: dict[str, set[str]] = {}
    for p in phashes:
        clusters.setdefault(find(p), set()).add(p)
    return list(clusters.values())


def detect_decoration_basenames(
    image_paths: list[Path],
    *,
    threshold: int,
    hamming_distance: int,
) -> set[str]:
    """Identify image basenames that recur often enough to be decoration.

    Walks ``image_paths``, computes a perceptual hash per image, clusters
    near-duplicates by Hamming distance, and returns the basenames of
    any cluster whose total size meets ``threshold``.

    Missing files and per-image hash failures are logged and skipped
    rather than aborting the pass — a single unreadable image shouldn't
    kill a long dedup scan.

    Args:
        image_paths: Paths to candidate images. Missing paths are skipped.
        threshold: Minimum cluster size to flag as decoration. A cluster
            with N images at this size or larger means the image appears
            on at least N pages.
        hamming_distance: Maximum bit-distance between phashes in the
            same cluster. 12 is a sensible starting point for DCT-8x8
            hashes — close enough to catch re-encoding noise, loose
            enough that distinct figures stay separate.

    Returns:
        Set of basenames (image filename only, no directory) belonging
        to flagged decoration clusters.
    """
    phash_to_basenames: dict[str, set[str]] = {}
    for img in image_paths:
        if not img.exists():
            logger.warning("decoration_image_missing", path=str(img))
            continue
        try:
            ph = compute_phash(img)
        except Exception as e:
            logger.warning("decoration_phash_failed", path=str(img), error=str(e))
            continue
        phash_to_basenames.setdefault(ph, set()).add(img.name)

    clusters = cluster_phashes(list(phash_to_basenames), max_distance=hamming_distance)
    decorations: set[str] = set()
    for cluster in clusters:
        total = sum(len(phash_to_basenames[ph]) for ph in cluster)
        if total >= threshold:
            for ph in cluster:
                decorations |= phash_to_basenames[ph]
    return decorations
