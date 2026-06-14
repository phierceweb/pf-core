"""Stable content hashing for change-detection and cache keys.

A small wrapper over :mod:`hashlib` that hashes either raw text / bytes or a
structured object (via :func:`pf_core.utils.json.canonical_json`, so dicts and
lists with reordered keys produce the same digest). Use for "did this change?"
comparisons and cache keys — not for security (the digest is plain and
unsalted).

Usage::

    from pf_core.utils.hashing import content_hash

    content_hash("some text")          # sha256 hex digest of the UTF-8 bytes
    content_hash({"a": 2, "b": 1})     # sha256 of the canonical JSON form
    content_hash(corpus, algo="md5")   # pick another hashlib algorithm
"""

from __future__ import annotations

import hashlib
from typing import Any

from pf_core.utils.json import canonical_json

__all__ = ["content_hash"]


def content_hash(content: Any, *, algo: str = "sha256") -> str:
    """Return a stable hex digest of *content*.

    ``bytes`` are hashed directly; ``str`` is hashed as its UTF-8 encoding; any
    other value is hashed via its :func:`canonical_json` form, so two equal
    dicts / lists with different key order produce the same digest.
    Deterministic across processes and machines — suitable for cache keys and
    "has this changed?" comparisons. Not a security primitive (no salt, no
    HMAC).

    Args:
        content: Text, bytes, or any JSON-serializable value.
        algo: Any algorithm name accepted by :func:`hashlib.new`
            (``"sha256"`` default; ``"md5"``, ``"sha1"``, … also work).

    Returns:
        The hex digest as a string.
    """
    if isinstance(content, bytes):
        data = content
    elif isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = canonical_json(content).encode("utf-8")
    return hashlib.new(algo, data).hexdigest()
