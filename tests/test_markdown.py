"""Tests for pf_core.web.markdown."""

from __future__ import annotations

from markupsafe import Markup

from pf_core.web.markdown import safe_markdown, setup_markdown_filter


class TestSafeMarkdown:
    def test_empty_input(self):
        assert safe_markdown(None) == Markup("")
        assert safe_markdown("") == Markup("")

    def test_plain_text_wrapped_in_paragraph(self):
        result = safe_markdown("hello world")
        assert result == Markup("<p>hello world</p>")

    def test_bold(self):
        result = safe_markdown("some **bold** text")
        assert "<strong>bold</strong>" in result

    def test_italic(self):
        result = safe_markdown("some *italic* text")
        assert "<em>italic</em>" in result

    def test_bold_and_italic(self):
        result = safe_markdown("**bold** and *italic*")
        assert "<strong>bold</strong>" in result
        assert "<em>italic</em>" in result

    def test_inline_code(self):
        result = safe_markdown("use `print()` here")
        assert "<code>print()</code>" in result

    def test_link(self):
        result = safe_markdown("[click](https://example.com)")
        assert 'href="https://example.com"' in result
        assert 'rel="nofollow noopener"' in result
        assert 'target="_blank"' in result
        assert ">click</a>" in result

    def test_link_with_nested_parens(self):
        result = safe_markdown("[wiki](https://en.wikipedia.org/wiki/Test_(thing))")
        assert "Test_(thing)" in result

    def test_javascript_scheme_link_dropped(self):
        result = safe_markdown("[click](javascript:alert(1))")
        assert "javascript:" not in result
        assert "<a " not in result  # link dropped
        assert "click" in result    # label kept as inert text

    def test_data_scheme_link_dropped(self):
        result = safe_markdown("[x](data:text/html,hi)")
        assert "data:" not in result
        assert "<a " not in result

    def test_relative_link_preserved(self):
        result = safe_markdown("[home](/dashboard)")
        assert 'href="/dashboard"' in result

    def test_unordered_list(self):
        result = safe_markdown("- item one\n- item two")
        assert "<ul>" in result
        assert "<li>item one</li>" in result
        assert "<li>item two</li>" in result
        assert "</ul>" in result

    def test_ordered_list(self):
        result = safe_markdown("1. first\n2. second")
        assert "<ol>" in result
        assert "<li>first</li>" in result
        assert "<li>second</li>" in result
        assert "</ol>" in result

    def test_heading_default_offset(self):
        result = safe_markdown("# Title")
        assert "<h3>" in result
        assert "Title" in result

    def test_heading_offset_zero(self):
        result = safe_markdown("# Title", heading_offset=0)
        assert "<h1>" in result

    def test_heading_clamped_to_h6(self):
        result = safe_markdown("#### Deep", heading_offset=4)
        assert "<h6>" in result

    def test_multiple_paragraphs(self):
        result = safe_markdown("first paragraph\n\nsecond paragraph")
        assert "<p>first paragraph</p>" in result
        assert "<p>second paragraph</p>" in result

    def test_lines_within_paragraph_joined_with_br(self):
        result = safe_markdown("line one\nline two")
        assert "<br>" in result

    def test_html_escaped(self):
        result = safe_markdown("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_mixed_content(self):
        text = "# Heading\n\nA **bold** paragraph.\n\n- list item\n- another"
        result = safe_markdown(text)
        assert "<h3>" in result
        assert "<strong>bold</strong>" in result
        assert "<ul>" in result
        assert "<li>list item</li>" in result

    def test_extra_transforms(self):
        def upcase_ids(text: str) -> str:
            return text.replace("abc", "ABC")

        result = safe_markdown("test abc here", extra_transforms=[upcase_ids])
        assert "ABC" in result
        assert "abc" not in result

    def test_returns_markup_type(self):
        result = safe_markdown("hello")
        assert isinstance(result, Markup)

    def test_list_followed_by_paragraph(self):
        result = safe_markdown("- item\n\nparagraph after")
        assert "</ul>" in result
        assert "<p>paragraph after</p>" in result

    def test_asterisk_list_items(self):
        result = safe_markdown("* one\n* two")
        assert "<ul>" in result
        assert "<li>one</li>" in result


class TestSetupMarkdownFilter:
    def test_registers_filter(self):
        class FakeEnv:
            filters: dict = {}

        class FakeTemplates:
            env = FakeEnv()

        templates = FakeTemplates()
        setup_markdown_filter(templates)
        assert "markdown" in templates.env.filters
        result = templates.env.filters["markdown"]("**bold**")
        assert "<strong>bold</strong>" in result

    def test_custom_filter_name(self):
        class FakeEnv:
            filters: dict = {}

        class FakeTemplates:
            env = FakeEnv()

        templates = FakeTemplates()
        setup_markdown_filter(templates, filter_name="md")
        assert "md" in templates.env.filters

    def test_filter_with_extra_transforms(self):
        class FakeEnv:
            filters: dict = {}

        class FakeTemplates:
            env = FakeEnv()

        templates = FakeTemplates()

        def add_prefix(text: str) -> str:
            return "PREFIX:" + text

        setup_markdown_filter(templates, extra_transforms=[add_prefix])
        result = templates.env.filters["markdown"]("hello")
        assert "PREFIX:" in result
