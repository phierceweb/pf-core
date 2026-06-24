"""
Safe markdown subset renderer for Jinja2 templates.

Renders a controlled subset of markdown to HTML using an escape-first
approach: all text is HTML-escaped, then safe markup (bold, italic, links,
lists, headings, code) is selectively applied. This avoids the need for
a sanitizer library — there is never untrusted HTML to sanitize.

Usage::

    from pf_core.web.markdown import safe_markdown, setup_markdown_filter

    # Render directly
    html = safe_markdown("**bold** and *italic*")

    # Register as Jinja2 filter
    setup_markdown_filter(templates)
    # In template: {{ text | markdown }}
"""

from __future__ import annotations

import re
from collections.abc import Callable

from markupsafe import Markup, escape

# --- Inline patterns ---

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")

# --- Block patterns ---

_UL_ITEM = re.compile(r"^\s*[-*]\s+(.+)$")
_OL_ITEM = re.compile(r"^\s*\d+\.\s+(.+)$")
_MD_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")


def _is_safe_href(href: str) -> bool:
    """Reject dangerous URL schemes (``javascript:``, ``data:``, …) in links.

    Allows http(s), mailto, and relative/anchor/protocol-relative hrefs. Anything
    carrying an explicit scheme before the first ``/`` that isn't allow-listed is
    rejected, so ``[x](javascript:alert(1))`` renders as inert text, not a link.
    """
    h = href.strip()
    if h.startswith(("http://", "https://", "mailto:", "/", "#", "?", "//")):
        return True
    return ":" not in h.split("/", 1)[0]


def _apply_links(text: str) -> str:
    """Convert [label](url) links, supporting nested parens in URLs."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "[":
            out.append(text[i])
            i += 1
            continue
        close_bracket = text.find("]", i + 1)
        if close_bracket == -1 or close_bracket + 1 >= n or text[close_bracket + 1] != "(":
            out.append(text[i])
            i += 1
            continue
        j = close_bracket + 2
        depth = 1
        while j < n and depth > 0:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        if depth != 0:
            out.append(text[i])
            i += 1
            continue
        label = text[i + 1 : close_bracket]
        href = text[close_bracket + 2 : j - 1]
        if _is_safe_href(href):
            out.append(
                f'<a href="{href}" rel="nofollow noopener" target="_blank">{label}</a>'
            )
        else:
            # Unsafe scheme — drop the link, keep the (already-escaped) label text.
            out.append(label)
        i = j
    return "".join(out)


def _inline_md(
    text: str,
    *,
    extra_transforms: list[Callable[[str], str]] | None = None,
) -> str:
    """Apply inline markdown transforms to already-escaped text."""
    result = str(escape(text))
    if extra_transforms:
        for fn in extra_transforms:
            result = fn(result)
    result = _apply_links(result)
    result = _MD_CODE.sub(r"<code>\1</code>", result)
    result = _MD_BOLD.sub(r"<strong>\1</strong>", result)
    result = _MD_ITALIC.sub(r"<em>\1</em>", result)
    return result


def safe_markdown(
    text: str | None,
    *,
    extra_transforms: list[Callable[[str], str]] | None = None,
    heading_offset: int = 2,
) -> Markup:
    """Render a safe markdown subset to HTML.

    Supports: paragraphs, headings (# through ####), bold, italic,
    inline code, links, unordered lists, and ordered lists.

    Args:
        text: Raw markdown text. Returns empty Markup if falsy.
        extra_transforms: Optional list of callables that receive
            HTML-escaped text and return transformed text. Applied
            before standard inline transforms. Useful for
            project-specific patterns (e.g. entry ID links).
        heading_offset: Added to heading level (default 2, so
            ``#`` becomes ``<h3>``, ``##`` becomes ``<h4>``).

    Returns:
        Sanitized HTML wrapped in ``Markup``.
    """
    if not text:
        return Markup("")

    out: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{'<br>'.join(paragraph)}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    for line in str(text).splitlines():
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_list()
            continue

        hm = _MD_HEADING.match(line)
        if hm:
            flush_paragraph()
            close_list()
            level = min(len(hm.group(1)) + heading_offset, 6)
            out.append(
                f"<h{level}>{_inline_md(hm.group(2).strip(), extra_transforms=extra_transforms)}</h{level}>"
            )
            continue

        ul = _UL_ITEM.match(line)
        if ul:
            flush_paragraph()
            if list_kind != "ul":
                close_list()
                out.append("<ul>")
                list_kind = "ul"
            out.append(
                f"<li>{_inline_md(ul.group(1).strip(), extra_transforms=extra_transforms)}</li>"
            )
            continue

        ol = _OL_ITEM.match(line)
        if ol:
            flush_paragraph()
            if list_kind != "ol":
                close_list()
                out.append("<ol>")
                list_kind = "ol"
            out.append(
                f"<li>{_inline_md(ol.group(1).strip(), extra_transforms=extra_transforms)}</li>"
            )
            continue

        close_list()
        paragraph.append(_inline_md(stripped, extra_transforms=extra_transforms))

    flush_paragraph()
    close_list()
    return Markup("\n".join(out))


def setup_markdown_filter(
    templates: object,
    *,
    filter_name: str = "markdown",
    extra_transforms: list[Callable[[str], str]] | None = None,
    heading_offset: int = 2,
) -> None:
    """Register a safe markdown Jinja2 filter on a templates instance.

    Args:
        templates: A ``Jinja2Templates`` instance (or anything with ``.env``).
        filter_name: Filter name in templates (default ``"markdown"``).
        extra_transforms: Passed through to ``safe_markdown()``.
        heading_offset: Passed through to ``safe_markdown()``.
    """

    def _filter(text: str | None) -> Markup:
        return safe_markdown(
            text,
            extra_transforms=extra_transforms,
            heading_offset=heading_offset,
        )

    templates.env.filters[filter_name] = _filter  # type: ignore[union-attr]
