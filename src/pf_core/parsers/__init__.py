"""Parser-side primitives for content-ingest pipelines.

Framework-level pieces shared by any consumer building a "fetch from
external source → extract structured records" pipeline. Per-source
parser modules (Substack, WordPress RSS, sitemap walkers) and the
parser-to-orchestrator handoff classes (``PostRef``, ``Post``) stay
in consumers until a second consumer needs them. See
``pf_core/docs/parsers.md`` for the lift policy.

Public API::

    from pf_core.parsers import (
        ParseError, PaywalledPost,    # exception types
        PostLink,                     # link with surrounding-text context
        BodyExtractor,                # HTMLParser walker
        parse_body_html,              # high-level html -> (text, links)
        normalize_plain_text,         # blank-line collapser
        BLOCK_TAGS, SKIP_TAGS,        # tag-classification frozensets
    )
"""

from __future__ import annotations

from pf_core.parsers.exceptions import ParseError, PaywalledPost  # noqa: F401
from pf_core.parsers.html import (  # noqa: F401
    BLOCK_TAGS,
    DEFAULT_CONTEXT_WINDOW_CHARS,
    SKIP_TAGS,
    BodyExtractor,
    normalize_plain_text,
    parse_body_html,
)
from pf_core.parsers.types import PostLink  # noqa: F401

__all__ = [
    # Exception types
    "ParseError",
    "PaywalledPost",
    # Data shapes
    "PostLink",
    # HTML extraction
    "BodyExtractor",
    "parse_body_html",
    "normalize_plain_text",
    "BLOCK_TAGS",
    "SKIP_TAGS",
    "DEFAULT_CONTEXT_WINDOW_CHARS",
]
