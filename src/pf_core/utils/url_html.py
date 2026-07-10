"""HTML metadata extraction for URL content sniffing — pure, no HTTP.

Public import path is ``pf_core.utils.urls`` (this module is its
HTML-parsing half); importing from here directly also works and needs
no ``[http]`` extra.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser


class _MetadataExtractor(HTMLParser):
    """Minimal HTML parser that collects <title>, selected <meta> tags, and
    the first paragraph of <article>/<main>/<body> for quick content sniffing.

    Deliberately simple — no dependency on beautifulsoup or readability. It
    handles well-formed HTML well enough for LLM content-match checks; edge
    cases (malformed markup, JS-rendered pages) degrade gracefully to empty.
    """

    _WANTED_META_NAMES = {
        "description": "description",
        "og:title": "og_title",
        "og:description": "og_description",
        "twitter:title": "twitter_title",
        "twitter:description": "twitter_description",
    }

    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.first_paragraph_parts: list[str] = []
        self._in_title = False
        self._capturing_p = False
        self._inside_main_region = False
        self._done_first_paragraph = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            name = (d.get("name") or d.get("property") or "").lower()
            key = self._WANTED_META_NAMES.get(name)
            if key and "content" in d and key not in self.meta:
                self.meta[key] = d["content"]
            return
        if tag in ("article", "main"):
            self._inside_main_region = True
            return
        if tag == "p" and self._inside_main_region and not self._done_first_paragraph:
            self._capturing_p = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "p" and self._capturing_p:
            self._capturing_p = False
            # Consider this paragraph done only if it has non-trivial content;
            # otherwise keep looking (e.g., skip caption-only paragraphs).
            text = "".join(self.first_paragraph_parts).strip()
            if len(text) >= 40:
                self._done_first_paragraph = True
            else:
                self.first_paragraph_parts = []
        elif tag in ("article", "main"):
            self._inside_main_region = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._capturing_p:
            self.first_paragraph_parts.append(data)


def extract_article_metadata(html: str) -> dict[str, str]:
    """Pull title, common meta tags, and the first paragraph from HTML.

    Used to get a lightweight snapshot of a page's topic for LLM
    content-match checks — the goal is to answer "is this article about X?"
    not to reconstruct the article's full prose.

    Args:
        html: Raw HTML as returned by ``fetch_url_content``. May be
            truncated; the parser handles partial input gracefully.

    Returns:
        Dict with keys ``title``, ``description``, ``og_title``,
        ``og_description``, ``twitter_title``, ``twitter_description``,
        ``first_paragraph``. Missing fields are empty strings rather than
        absent — callers can safely do ``metadata["title"]`` without
        checking membership.
    """
    out: dict[str, str] = {
        "title": "", "description": "", "og_title": "",
        "og_description": "", "twitter_title": "",
        "twitter_description": "", "first_paragraph": "",
    }
    if not html:
        return out
    parser = _MetadataExtractor()
    try:
        parser.feed(html)
    except Exception:
        return out
    out["title"] = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    for k, v in parser.meta.items():
        out[k] = re.sub(r"\s+", " ", v).strip()
    if parser.first_paragraph_parts:
        out["first_paragraph"] = re.sub(
            r"\s+", " ", "".join(parser.first_paragraph_parts)
        ).strip()
    return out
