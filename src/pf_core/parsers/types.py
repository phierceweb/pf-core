"""Shared data shapes for content-parser pipelines.

Currently scoped to the parser-side primitives — types that any consumer
extracting structured records from HTML or feeds will produce.
Orchestrator-side shapes (``PostRef``, ``Post``) stay in consumers
until a second consumer's orchestrator wants the same handoff.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PostLink:
    """One ``<a href>`` extracted from a post body, with surrounding context.

    Produced by :func:`pf_core.parsers.html.parse_body_html`.

    Attributes:
        url: The ``href`` attribute, stripped.
        anchor_text: The visible text inside the ``<a>`` tag, stripped.
        surrounding_text: A slice of the post's plain text centered on the
            link — the LLM uses this to disambiguate which record a candidate
            source URL is being cited for. Whitespace collapsed to
            single spaces; default window ±120 chars (configurable on
            :func:`parse_body_html`).
    """

    url: str
    anchor_text: str
    surrounding_text: str
