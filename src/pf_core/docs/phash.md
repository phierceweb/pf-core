# Perceptual-Hash Image Dedup

DCT-based image fingerprinting + clustering for detecting recurring images (header logos, watermarks, footer marks) and re-encoded duplicates that `sha256` would miss.

Strips repeated header/footer images before they reach downstream processing. Generic enough for any consumer that processes images extracted from documents.

## Install

Optional dependency — install the extra:

```bash
pip install 'pf-core[image-phash]'
```

Pulls in `ImageHash` and `Pillow`. The pure-stdlib helpers (`hamming_distance_hex`, `cluster_phashes`) work without the extra; only `compute_phash` and `detect_decoration_basenames` require it.

## High-level usage

```python
from pathlib import Path
from pf_core.utils.phash import detect_decoration_basenames

decoration_basenames = detect_decoration_basenames(
    list(Path("./images").iterdir()),
    threshold=10,         # ≥ N occurrences across the set → decoration
    hamming_distance=12,  # cluster phashes within 12 bits
)
# {"header_logo.png", "footer_mark.png", ...}
```

Walks the image set, computes a perceptual hash per image, clusters near-duplicates, and returns the basenames of any cluster large enough to be a recurring decoration. Missing files and per-image hash failures are logged and skipped — a single unreadable image won't kill a long dedup pass.

## Why DCT-based phash, not sha256

`sha256` flips entirely with a single re-encoded byte. The same source figure rasterized at two different resolutions, or with one pixel of margin variation, produces totally different sha256 digests but near-identical perceptual hashes (typically within a handful of bits). DCT-based phash captures the visual signal, not the byte signal — so "same logo, scanned twice" clusters together.

## Tuning the thresholds

| Knob | Sensible default | What it controls |
|---|---|---|
| `hamming_distance` | `12` | Bit-distance below which two phashes are considered the same image. Tighter values (4–8) cluster only near-pixel-perfect duplicates; looser values (16+) start grouping visually similar but distinct figures. |
| `threshold` | depends on N pages | Minimum cluster size to flag as decoration. For a 100-page document, `10` finds anything that appears on ~10% of pages. |

Both depend on the corpus. Start with the defaults; tighten `hamming_distance` if distinct figures are getting clustered together, loosen if a known recurring decoration is being missed.

## Lower-level helpers

```python
from pf_core.utils.phash import (
    compute_phash,
    hamming_distance_hex,
    cluster_phashes,
)

a = compute_phash(Path("logo_v1.png"))    # "8c3a7e1d04f29b56" — 16 hex chars (64 bits)
b = compute_phash(Path("logo_v2.png"))
hamming_distance_hex(a, b)                # int 0–64

clusters = cluster_phashes([a, b, ...], max_distance=12)
# list[set[str]] — each cluster is a set of phash strings within max_distance
```

`cluster_phashes` uses union-find — O(N²) in the size of the input. Fine for hundreds of images per document; do not use on millions of phashes without a more sophisticated index.

## Non-goals

- Not a content-based image search engine. The DCT-8x8 hash is too coarse to distinguish "different photos of the same subject" — that needs a proper embedding model.
- Not robust to heavy crops or rotation. A logo that appears top-left on one page and bottom-right on another with margin reflowing may not cluster.
- Not a substitute for `sha256` when you actually want byte-equality (e.g. cache keys for source files, integrity checks).
