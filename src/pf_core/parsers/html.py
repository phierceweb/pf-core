"""HTML body extractor for article / post content.

Pure-stdlib (``html.parser.HTMLParser``) walker that turns post body HTML
into:

  - A normalized plain-text rendering (paragraphs preserved, whitespace
    collapsed)
  - A list of inline links with surrounding-text context

Designed for the content ingestion shape: a post arrives as HTML, we hand
the LLM the plain text for record extraction and the link list with
context so the model can identify which record a candidate source
URL backs.

Public API::

    from pf_core.parsers.html import (
        BodyExtractor,            # the HTMLParser subclass
        parse_body_html,          # high-level helper: html -> (text, links)
        normalize_plain_text,     # blank-line collapser
        BLOCK_TAGS, SKIP_TAGS,    # tag-classification frozensets
    )
"""

from __future__ import annotations

from html.parser import HTMLParser

from pf_core.parsers.exceptions import ParseError
from pf_core.parsers.types import PostLink


# Block-level tags whose open/close should emit a paragraph break in the
# plain-text buffer. Not exhaustive — good enough for article prose,
# which is mostly <p>, <li>, and blockquotes.
BLOCK_TAGS: frozenset[str] = frozenset({
    "p", "div", "br", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "hr", "section", "article",
})

# Tags whose inner text we drop entirely (scripts, styles).
SKIP_TAGS: frozenset[str] = frozenset({"script", "style"})

# Default ± character window around each link for surrounding-text capture.
DEFAULT_CONTEXT_WINDOW_CHARS = 120


class BodyExtractor(HTMLParser):
    """Walks post body HTML, recording plain text + link offsets.

    Deliberately simple. The plain-text buffer is a list of chunks
    joined at the end; each ``<a href>`` start records its offset in
    the joined buffer so we can later slice surrounding context.

    Public state after :meth:`feed` + :meth:`close`:

    - ``text_parts``: list of text chunks; ``"".join(text_parts)`` is
      the raw extracted text (before :func:`normalize_plain_text`).
    - ``link_records``: list of ``(start_offset_in_joined_text, url,
      anchor_text)`` tuples. Empty-href and empty-anchor links are
      filtered (many sites sprinkle these for layout).
    """

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        # link_records: list[(start_offset_in_joined_text, url, anchor_text)]
        self.link_records: list[tuple[int, str, str]] = []
        # Stack-based state so nested <a> (uncommon but possible) works.
        self._link_stack: list[dict] = []
        self._skip_depth = 0

    # Offset in the eventual joined string at which the next chunk will land.
    @property
    def _buffer_offset(self) -> int:
        return sum(len(p) for p in self.text_parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "br":
            self.text_parts.append("\n")
            return
        if tag in BLOCK_TAGS:
            # Paragraph break at the START of a block — cheap way to
            # ensure adjacent blocks don't concatenate their text.
            self.text_parts.append("\n\n")
        if tag == "a":
            href = ""
            for k, v in attrs:
                if k == "href":
                    href = v or ""
                    break
            self._link_stack.append({
                "offset": self._buffer_offset,
                "url": href,
                "anchor_parts": [],
            })

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in BLOCK_TAGS:
            self.text_parts.append("\n\n")
        if tag == "a" and self._link_stack:
            rec = self._link_stack.pop()
            anchor = "".join(rec["anchor_parts"]).strip()
            url = (rec["url"] or "").strip()
            # Only record links that have a URL AND anchor text. Skip
            # empty shells (many sites sprinkle these for layout).
            if url and anchor:
                self.link_records.append((rec["offset"], url, anchor))

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self.text_parts.append(data)
        if self._link_stack:
            # Feed anchor-text to the most recent open <a>.
            self._link_stack[-1]["anchor_parts"].append(data)


def normalize_plain_text(text: str) -> str:
    """Trim and collapse excessive blank lines.

    Three or more consecutive blank lines collapse to one blank line
    (i.e. paragraph breaks survive, but giant gaps from nested empty
    blocks don't). Leading and trailing whitespace is stripped.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank_streak = 0
    for ln in lines:
        if not ln.strip():
            blank_streak += 1
            if blank_streak <= 1:
                out.append("")
        else:
            blank_streak = 0
            out.append(ln)
    return "\n".join(out).strip()


def parse_body_html(
    html: str,
    *,
    context_window_chars: int = DEFAULT_CONTEXT_WINDOW_CHARS,
) -> tuple[str, list[PostLink]]:
    """Extract plain text + inline links from a post's body HTML.

    Args:
        html: The post body as an HTML string.
        context_window_chars: Half-width of the surrounding-text window
            captured for each ``PostLink``. Default 120 chars on each side.

    Returns:
        ``(plain_text, links)``. ``plain_text`` is :func:`normalize_plain_text`
        output; each ``PostLink.surrounding_text`` is a whitespace-collapsed
        slice of the pre-normalized text centered on the link offset.

    Raises:
        :class:`pf_core.parsers.exceptions.ParseError`: if the HTML parser
            crashes mid-parse. Empty/whitespace-only input is NOT an error
            (returns ``("", [])``).
    """
    extractor = BodyExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception as e:
        raise ParseError("HTML parse failed", cause=e)

    text = "".join(extractor.text_parts)
    links: list[PostLink] = []
    for (start_offset, url, anchor_text) in extractor.link_records:
        before_start = max(0, start_offset - context_window_chars)
        after_end = min(len(text), start_offset + context_window_chars)
        context = text[before_start:after_end].strip()
        # Collapse runs of whitespace/newlines so the LLM sees clean prose.
        context = " ".join(context.split())
        links.append(PostLink(
            url=url.strip(),
            anchor_text=anchor_text.strip(),
            surrounding_text=context,
        ))
    out_text = normalize_plain_text(text)
    return out_text, links
