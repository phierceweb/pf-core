"""Tests for ``pf_core.parsers.html`` and ``pf_core.parsers.types``.

Behavior coverage of the HTML body extractor: paragraphs preserved,
script/style stripped, link offsets correct, surrounding-text window
configurable, normalization rules.
"""

from __future__ import annotations

import pytest

from pf_core.parsers import (
    BLOCK_TAGS,
    DEFAULT_CONTEXT_WINDOW_CHARS,
    SKIP_TAGS,
    BodyExtractor,
    ParseError,
    PostLink,
    normalize_plain_text,
    parse_body_html,
)


# ---------------------------------------------------------------------------
# parse_body_html — high-level happy path + edge cases
# ---------------------------------------------------------------------------


class TestParseBodyHtml:
    def test_empty_html_returns_empty(self):
        text, links = parse_body_html("")
        assert text == ""
        assert links == []

    def test_plain_paragraph_extracts_text(self):
        text, links = parse_body_html("<p>Hello world</p>")
        assert "Hello world" in text
        assert links == []

    def test_two_paragraphs_separated_by_blank_line(self):
        text, _ = parse_body_html("<p>First</p><p>Second</p>")
        # Paragraph break preserved between blocks
        assert "First" in text and "Second" in text
        # First paragraph ends before Second starts
        assert text.index("First") < text.index("Second")
        # And there's a blank line (or paragraph break) between them
        assert "\n" in text[text.index("First"):text.index("Second")]

    def test_single_link_extracted(self):
        text, links = parse_body_html(
            '<p>See <a href="https://example.com">this report</a> for details.</p>'
        )
        assert "this report" in text
        assert len(links) == 1
        assert links[0].url == "https://example.com"
        assert links[0].anchor_text == "this report"
        assert "this report" in links[0].surrounding_text

    def test_link_surrounding_text_windowed(self):
        # Force a long context so the window matters
        before = "x" * 200
        after = "y" * 200
        html = f'<p>{before}<a href="https://example.com">link</a>{after}</p>'
        _, links = parse_body_html(html, context_window_chars=50)
        # Window is 50 chars on each side, so we should see ~50 x's and ~50 y's
        assert "link" in links[0].surrounding_text
        # Surrounding text is bounded — not the full 400 chars
        assert len(links[0].surrounding_text) < 200

    def test_default_context_window_is_120(self):
        assert DEFAULT_CONTEXT_WINDOW_CHARS == 120

    def test_empty_anchor_link_dropped(self):
        """Many sites sprinkle empty <a> shells for layout."""
        _, links = parse_body_html('<p><a href="https://example.com"></a></p>')
        assert links == []

    def test_empty_href_link_dropped(self):
        _, links = parse_body_html('<p><a href="">anchor</a></p>')
        assert links == []

    def test_script_content_dropped(self):
        text, _ = parse_body_html(
            "<p>Real text</p><script>alert('xss')</script><p>More text</p>"
        )
        assert "Real text" in text
        assert "More text" in text
        assert "alert" not in text
        assert "xss" not in text

    def test_style_content_dropped(self):
        text, _ = parse_body_html(
            "<p>Visible</p><style>.x { color: red }</style>"
        )
        assert "Visible" in text
        assert "color: red" not in text

    def test_br_emits_newline(self):
        text, _ = parse_body_html("<p>line one<br>line two</p>")
        assert "line one" in text and "line two" in text
        assert "\n" in text[text.index("line one"):text.index("line two")]

    def test_nested_link_in_block(self):
        text, links = parse_body_html(
            '<blockquote>Quote with <a href="https://x.example">cite</a></blockquote>'
        )
        assert "Quote with cite" in text or ("Quote with" in text and "cite" in text)
        assert len(links) == 1
        assert links[0].url == "https://x.example"

    def test_multiple_links_each_get_own_record(self):
        _, links = parse_body_html(
            '<p>See <a href="https://a.example">one</a> and '
            '<a href="https://b.example">two</a>.</p>'
        )
        assert len(links) == 2
        assert {link.url for link in links} == {"https://a.example", "https://b.example"}

    def test_url_and_anchor_stripped(self):
        _, links = parse_body_html(
            '<p><a href="  https://example.com  ">  spaced  </a></p>'
        )
        assert links[0].url == "https://example.com"
        assert links[0].anchor_text == "spaced"

    def test_surrounding_text_collapses_whitespace(self):
        _, links = parse_body_html(
            '<p>before\n\n\n  text  here\t<a href="https://x">L</a>'
            "  after\n\n\n</p>"
        )
        # Internal runs of whitespace become single spaces
        assert "  " not in links[0].surrounding_text
        assert "\n" not in links[0].surrounding_text


class TestParseBodyHtmlErrors:
    def test_parse_failure_raises_parse_error(self):
        """If the parser crashes, wrap in ParseError so consumers can catch it."""
        from unittest.mock import patch

        # Make BodyExtractor.feed raise to simulate a parser crash.
        with patch.object(BodyExtractor, "feed", side_effect=RuntimeError("boom")):
            with pytest.raises(ParseError) as ei:
                parse_body_html("<p>x</p>")
        assert "HTML parse failed" in str(ei.value)
        assert isinstance(ei.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# normalize_plain_text
# ---------------------------------------------------------------------------


class TestNormalizePlainText:
    def test_strips_leading_trailing(self):
        assert normalize_plain_text("\n\n  hi  \n\n") == "hi"

    def test_collapses_three_newlines_to_two(self):
        # Three blank lines (4 \n) → one blank line between (2 \n)
        out = normalize_plain_text("a\n\n\n\nb")
        assert out == "a\n\nb"

    def test_collapses_many_newlines_to_two(self):
        out = normalize_plain_text("a\n\n\n\n\n\n\n\nb")
        assert out == "a\n\nb"

    def test_preserves_single_blank_line(self):
        out = normalize_plain_text("a\n\nb")
        assert out == "a\n\nb"

    def test_preserves_no_blank_lines(self):
        out = normalize_plain_text("a\nb\nc")
        assert out == "a\nb\nc"

    def test_strips_trailing_whitespace_per_line(self):
        out = normalize_plain_text("a   \nb\t\t")
        assert out == "a\nb"

    def test_empty_input(self):
        assert normalize_plain_text("") == ""

    def test_whitespace_only_input(self):
        assert normalize_plain_text("\n\n  \n  \n") == ""


# ---------------------------------------------------------------------------
# BodyExtractor — direct state-machine sanity
# ---------------------------------------------------------------------------


class TestBodyExtractorState:
    def test_initial_state_empty(self):
        ex = BodyExtractor()
        assert ex.text_parts == []
        assert ex.link_records == []

    def test_buffer_offset_grows_with_data(self):
        ex = BodyExtractor()
        ex.handle_data("hello")
        assert ex._buffer_offset == 5
        ex.handle_data(" world")
        assert ex._buffer_offset == 11

    def test_link_recorded_with_offset(self):
        ex = BodyExtractor()
        ex.feed('<p>before <a href="https://x.example">link</a></p>')
        ex.close()
        assert len(ex.link_records) == 1
        offset, url, anchor = ex.link_records[0]
        assert url == "https://x.example"
        assert anchor == "link"
        # Offset points into the joined text where the link begins
        joined = "".join(ex.text_parts)
        assert "link" in joined[offset : offset + 10]

    def test_skip_tags_constant(self):
        assert "script" in SKIP_TAGS
        assert "style" in SKIP_TAGS

    def test_block_tags_constant(self):
        for tag in ("p", "div", "li", "h1", "blockquote"):
            assert tag in BLOCK_TAGS


# ---------------------------------------------------------------------------
# Re-export surface
# ---------------------------------------------------------------------------


class TestReExports:
    def test_post_link_is_dataclass(self):
        link = PostLink(url="https://x", anchor_text="anchor", surrounding_text="ctx")
        assert link.url == "https://x"

    def test_all_symbols_at_package_root(self):
        from pf_core.parsers import (
            BLOCK_TAGS as r1,
            BodyExtractor as r2,
            PostLink as r5,
            SKIP_TAGS as r6,
            normalize_plain_text as r7,
            parse_body_html as r8,
        )

        from pf_core.parsers.html import (
            BLOCK_TAGS as h1,
            BodyExtractor as h2,
            SKIP_TAGS as h6,
            normalize_plain_text as h7,
            parse_body_html as h8,
        )
        from pf_core.parsers.types import PostLink as t5

        assert r1 is h1
        assert r2 is h2
        assert r5 is t5
        assert r6 is h6
        assert r7 is h7
        assert r8 is h8
