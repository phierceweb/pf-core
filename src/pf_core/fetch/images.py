"""Markdown/HTML remote-image localizer over the fetch core.

Finds remote image refs — markdown ``![alt](http…)`` and HTML
``<img src="http…">`` — downloads each into a local images dir, and retargets
the refs at the local copies. A failed download keeps its remote ref so the
doc still renders; naming is injectable, with a deterministic default.
"""

from __future__ import annotations

import http.client
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

from pf_core.exceptions import ClientError, InvalidInputError
from pf_core.fetch import Fetcher
from pf_core.log import get_logger
from pf_core.utils.io import atomic_write_bytes, atomic_write_text

logger = get_logger(__name__)

__all__ = [
    "LocalizeResult",
    "count_remote_images",
    "default_namer",
    "localize_file",
    "localize_images",
    "sniff_image_ext",
]

_IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".avif", ".heic", ".bmp", ".tiff", ".ico", ".jxl",
)

_DEFAULT_MAX_BYTES = 25 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 30.0

# How far into an XML body to look for the <svg> root. Generous enough to clear
# a DOCTYPE plus an embedded license header.
_SVG_SCAN_BYTES = 4096

# Remote refs only — local paths and other schemes (data:, file:) never match.
_MD_REMOTE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")

# Relative refs, resolved only against a caller-supplied base_url. The lookahead
# excludes already-localized ``images/`` refs, absolute paths, and data: URIs.
_MD_RELATIVE_RE = re.compile(r"!\[[^\]]*\]\((?!https?://|images/|/|data:)([^)\s]+)\)")

_HTML_REMOTE_RE = re.compile(r"""<img\b[^>]*?\bsrc=["'](https?://[^"']+)["']""", re.IGNORECASE)

# OSError covers urllib's HTTPError/URLError (subclasses) plus disk-write errors.
# The rest are what a malformed body raises through an *injected* fetcher —
# pf_core.fetch normalizes those to ClientError, a third-party one won't.
_PER_URL_FAILURES = (
    OSError,
    InvalidInputError,
    ClientError,
    zlib.error,
    EOFError,
    http.client.HTTPException,
)


class _BytesFetcher(Protocol):
    def get_bytes(self, url: str, *, timeout_s: float = ...) -> tuple[str, bytes]: ...


@dataclass
class LocalizeResult:
    """One localize pass: rewritten doc, local files (downloaded or reused), failure count."""

    markdown: str
    saved: list[Path]
    failed: int


def default_namer(url: str) -> str:
    """Deterministic local name: dash-join of the path segments after the first
    ``images`` component (else the last two), so figures sharing a basename in
    different sub-paths don't collide."""
    path = urlparse(url).path
    parts = Path(path).parts
    try:
        idx = next(i for i, p in enumerate(parts) if p == "images")
        sub = parts[idx + 1 :]
    except StopIteration:
        sub = parts[-2:] if len(parts) >= 2 else parts
    name = "-".join(sub) if sub else Path(path).name
    return name.replace("/", "-")


def sniff_image_ext(data: bytes) -> str | None:
    """Image extension from magic bytes; ``None`` when the body is not a
    recognized image — an HTML interstitial, a JSON error, a login page."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff"
    if data[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return ".heic"
    if data[:2] == b"\xff\x0a" or data[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return ".jxl"
    head = data[:_SVG_SCAN_BYTES].lstrip().lower()
    # A bare <?xml prolog is as likely an error document as an SVG, so the root
    # tag must actually appear — past a DOCTYPE, comments, or a license header.
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return ".svg"
    return None


def count_remote_images(text: str) -> int:
    """Distinct still-remote image refs the localizer would target (0 = fully localized)."""
    return len(_targets(text, None))


def localize_images(
    markdown: str,
    images_dir: Path | str,
    *,
    base_url: str | None = None,
    fetcher: _BytesFetcher | None = None,
    namer: Callable[[str], str] | None = None,
    reuse_existing: bool = True,
    link_prefix: str = "images/",
) -> LocalizeResult:
    """Download ``markdown``'s remote image refs into ``images_dir`` and retarget them.

    No-op input (nothing to localize) returns the markdown unchanged without
    creating ``images_dir`` or building a fetcher.

    Args:
        markdown: Document text (markdown and/or HTML image refs).
        images_dir: Directory for the local copies.
        base_url: Resolve relative refs against this URL; without it they're untouched.
        fetcher: Any object with ``get_bytes(url, *, timeout_s=...)``; defaults
            to a size-capped :class:`~pf_core.fetch.Fetcher`.
        namer: ``url -> base name`` (extension optional; sniffed from the bytes
            when absent). Defaults to :func:`default_namer`.
        reuse_existing: Probe ``images_dir`` for the name and reuse without
            fetching; ``False`` always fetches and collision-suffixes ``-2``, ``-3``…
            against the names already on disk.
        link_prefix: Prefix for rewritten refs.

    Returns:
        :class:`LocalizeResult`; failed downloads keep their remote ref.
    """
    targets = _targets(markdown, base_url)
    if not targets:
        return LocalizeResult(markdown=markdown, saved=[], failed=0)
    dir_path, fetch, name_for, used = _setup(
        images_dir, fetcher=fetcher, namer=namer, reuse_existing=reuse_existing
    )
    text = markdown
    saved: list[Path] = []
    failed = 0
    for ref, fetch_url in targets:
        local = _localize_one(
            fetch_url,
            dir_path,
            fetcher=fetch,
            namer=name_for,
            reuse_existing=reuse_existing,
            used=used,
        )
        if local is None:
            failed += 1
            continue
        text = _retarget(text, ref, f"{link_prefix}{local.name}")
        saved.append(local)
    logger.debug("images_localized", saved=len(saved), failed=failed, total=len(targets))
    return LocalizeResult(markdown=text, saved=saved, failed=failed)


def localize_file(
    doc_path: Path | str,
    images_dir: Path | str,
    *,
    checkpoint_every: int = 50,
    fetcher: _BytesFetcher | None = None,
    namer: Callable[[str], str] | None = None,
    reuse_existing: bool = False,
    link_prefix: str = "images/",
) -> int:
    """Resumable :func:`localize_images` over a file — the file is the ledger.

    Each ref is re-pointed in-memory the moment its image lands, and the doc is
    written (atomically) every ``checkpoint_every`` localized images plus once
    at the end, so a killed run keeps its progress: finished refs are local,
    pending ones stay remote. Returns the count localized this run — re-run
    until :func:`count_remote_images` reaches 0.
    """
    doc = Path(doc_path)
    text = doc.read_text(encoding="utf-8")
    targets = _targets(text, None)
    if not targets:
        return 0
    dir_path, fetch, name_for, used = _setup(
        images_dir, fetcher=fetcher, namer=namer, reuse_existing=reuse_existing
    )
    saved = 0
    since_checkpoint = 0
    for ref, fetch_url in targets:
        local = _localize_one(
            fetch_url,
            dir_path,
            fetcher=fetch,
            namer=name_for,
            reuse_existing=reuse_existing,
            used=used,
        )
        if local is None:
            continue
        text = _retarget(text, ref, f"{link_prefix}{local.name}")
        saved += 1
        since_checkpoint += 1
        if since_checkpoint >= checkpoint_every:
            atomic_write_text(doc, text)
            since_checkpoint = 0
    atomic_write_text(doc, text)
    logger.info("doc_images_localized", doc=str(doc), found=len(targets), saved=saved)
    return saved


def _targets(text: str, base_url: str | None) -> list[tuple[str, str]]:
    """Deduped ``(ref, fetch_url)`` pairs in first-seen order; non-image refs dropped."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for regex in (_MD_REMOTE_RE, _HTML_REMOTE_RE):
        for match in regex.finditer(text):
            url = match.group(1)
            if _is_image_url(url) and url not in seen:
                seen.add(url)
                out.append((url, url))
    if base_url:
        for match in _MD_RELATIVE_RE.finditer(text):
            ref = match.group(1)
            if _is_image_url(ref) and ref not in seen:
                seen.add(ref)
                out.append((ref, urljoin(base_url, ref)))
    return out


def _is_image_url(url: str) -> bool:
    """Image extension or none at all (opaque CDN URLs) qualifies; only a
    non-image extension (``.html``/``.css``/…) is rejected."""
    suffix = Path(urlparse(url).path.rstrip("/")).suffix.lower()
    return suffix == "" or suffix in _IMAGE_EXTENSIONS


def _setup(
    images_dir: Path | str,
    *,
    fetcher: _BytesFetcher | None,
    namer: Callable[[str], str] | None,
    reuse_existing: bool,
) -> tuple[Path, _BytesFetcher, Callable[[str], str], set[str]]:
    dir_path = Path(images_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    if fetcher is None:
        fetcher = Fetcher(max_bytes=_DEFAULT_MAX_BYTES)
    used = set() if reuse_existing else {p.name for p in dir_path.iterdir() if p.is_file()}
    return dir_path, fetcher, namer or default_namer, used


def _localize_one(
    fetch_url: str,
    images_dir: Path,
    *,
    fetcher: _BytesFetcher,
    namer: Callable[[str], str],
    reuse_existing: bool,
    used: set[str],
) -> Path | None:
    """Fetch (or reuse) one image; returns its local path, or None on failure."""
    base = namer(fetch_url)
    has_ext = Path(base).suffix.lower() in _IMAGE_EXTENSIONS
    if reuse_existing:
        existing = _existing_local(images_dir, base, has_ext)
        if existing is not None:
            return existing
    try:
        _final, data = fetcher.get_bytes(fetch_url, timeout_s=_DEFAULT_TIMEOUT_S)
        sniffed = sniff_image_ext(data)
        if sniffed is None:
            # Checked even when the URL declares an extension — a CDN path
            # ending .png can still 200 with a login page.
            raise ClientError(
                "response body is not a recognized image",
                context={"url": fetch_url, "bytes": len(data)},
            )
        name = base if has_ext else base + sniffed
        if not reuse_existing:
            name = _claim_name(name, used)
        atomic_write_bytes(images_dir / name, data)
    except _PER_URL_FAILURES as exc:
        logger.warning("image_localize_failed", url=fetch_url, error=str(exc))
        return None
    return images_dir / name


def _existing_local(images_dir: Path, base: str, has_ext: bool) -> Path | None:
    """Prior run's file for ``base`` (an extensionless base probes each known ext)."""
    if has_ext:
        candidate = images_dir / base
        return candidate if candidate.exists() else None
    for ext in _IMAGE_EXTENSIONS:
        candidate = images_dir / f"{base}{ext}"
        if candidate.exists():
            return candidate
    return None


def _claim_name(name: str, used: set[str]) -> str:
    """First free ``name`` / ``stem-2.ext`` / ``stem-3.ext``…, claimed in ``used``."""
    stem, ext = Path(name).stem, Path(name).suffix
    candidate = name
    i = 2
    while candidate in used:
        candidate = f"{stem}-{i}{ext}"
        i += 1
    used.add(candidate)
    return candidate


def _retarget(text: str, ref: str, local: str) -> str:
    """Anchor-safe rewrite of every occurrence in both syntaxes — never the bare URL."""
    text = text.replace(f"]({ref})", f"]({local})")
    text = text.replace(f'src="{ref}"', f'src="{local}"')
    return text.replace(f"src='{ref}'", f"src='{local}'")
