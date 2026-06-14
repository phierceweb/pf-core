"""Tests for pf_core.llm.prompts."""

from __future__ import annotations

import pytest

from pf_core.exceptions import ConfigurationError, InvalidInputError
from pf_core.llm.prompts import (
    load_prompt_spec,
    load_prompts,
    render,
    render_spec,
)


class TestLoadPrompts:
    def test_loads_yaml_file(self, tmp_path):
        f = tmp_path / "prompts.yaml"
        f.write_text("summarizing:\n  system: Summarize this.\n  user: |\n    Answer: {answer}\n")
        result = load_prompts(f)
        assert result["summarizing"]["system"] == "Summarize this."
        assert "Answer: {answer}" in result["summarizing"]["user"]

    def test_file_not_found(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not found"):
            load_prompts(tmp_path / "missing.yaml")

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text(":\n  - [\n")
        with pytest.raises(ConfigurationError, match="Failed to parse"):
            load_prompts(f)

    def test_non_dict_yaml(self, tmp_path):
        f = tmp_path / "list.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(ConfigurationError, match="must be a YAML mapping"):
            load_prompts(f)

    def test_empty_yaml(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        with pytest.raises(ConfigurationError, match="must be a YAML mapping"):
            load_prompts(f)

    def test_nested_structure(self, tmp_path):
        f = tmp_path / "prompts.yaml"
        f.write_text(
            "summarizing:\n"
            "  system: You are a summarizer.\n"
            "  user: Summarize this\n"
            "feedback:\n"
            "  system: You write feedback.\n"
            "  user: Score this\n"
        )
        result = load_prompts(f)
        assert "summarizing" in result
        assert "feedback" in result
        assert result["feedback"]["system"] == "You write feedback."


# ---- Brace style (default) ----

class TestRenderBrace:
    def test_simple_substitution(self):
        assert render("Hello {name}", name="world") == "Hello world"

    def test_multiple_variables(self):
        assert render("{a} and {b}", a="one", b="two") == "one and two"

    def test_repeated_variable(self):
        assert render("{x} then {x}", x="same") == "same then same"

    def test_escaped_braces_preserved(self):
        result = render("JSON: {{\"key\": \"{val}\"}}", val="hello")
        assert result == 'JSON: {"key": "hello"}'

    def test_missing_variable_raises(self):
        with pytest.raises(InvalidInputError, match="undefined variables.*name"):
            render("Hello {name}")

    def test_extra_variables_ignored(self):
        assert render("Hello {name}", name="world", extra="ignored") == "Hello world"

    def test_empty_template(self):
        assert render("") == ""

    def test_no_placeholders(self):
        assert render("No variables here.") == "No variables here."

    def test_multiline_template(self):
        result = render("Line 1: {a}\nLine 2: {b}\n", a="one", b="two")
        assert result == "Line 1: one\nLine 2: two\n"

    def test_integer_value(self):
        assert render("Score: {score}/{total}", score=85, total=100) == "Score: 85/100"

    def test_explicit_brace_style(self):
        assert render("Hello {name}", style="brace", name="world") == "Hello world"


# ---- Token style (@@VARIABLE@@) ----

class TestRenderToken:
    def test_simple_substitution(self):
        assert render("Hello @@NAME@@", style="@@", NAME="world") == "Hello world"

    def test_multiple_variables(self):
        result = render("@@A@@ and @@B@@", style="@@", A="one", B="two")
        assert result == "one and two"

    def test_repeated_variable(self):
        result = render("@@X@@ then @@X@@", style="@@", X="same")
        assert result == "same then same"

    def test_curly_braces_not_affected(self):
        result = render('{"role": "@@ROLE@@"}', style="@@", ROLE="summarizer")
        assert result == '{"role": "summarizer"}'

    def test_json_heavy_template(self):
        template = (
            'Output JSON: {"score": @@SCORE@@, "items": [{"name": "@@NAME@@"}]}'
        )
        result = render(template, style="@@", SCORE="95", NAME="test")
        assert result == 'Output JSON: {"score": 95, "items": [{"name": "test"}]}'

    def test_missing_variable_raises(self):
        with pytest.raises(InvalidInputError, match="undefined variables.*NAME"):
            render("Hello @@NAME@@", style="@@")

    def test_extra_variables_ignored(self):
        result = render("Hello @@NAME@@", style="@@", NAME="world", EXTRA="ignored")
        assert result == "Hello world"

    def test_empty_template(self):
        assert render("", style="@@") == ""

    def test_no_placeholders(self):
        assert render("No variables here.", style="@@") == "No variables here."

    def test_multiline_template(self):
        result = render("Line 1: @@A@@\nLine 2: @@B@@\n", style="@@", A="one", B="two")
        assert result == "Line 1: one\nLine 2: two\n"

    def test_integer_value(self):
        result = render("Score: @@SCORE@@/@@TOTAL@@", style="@@", SCORE=85, TOTAL=100)
        assert result == "Score: 85/100"

    def test_preserves_single_at_signs(self):
        result = render("email@example.com @@NAME@@", style="@@", NAME="test")
        assert result == "email@example.com test"


# ---- Invalid style ----

class TestRenderInvalidStyle:
    def test_unknown_style_raises(self):
        with pytest.raises(InvalidInputError, match="Unknown render style"):
            render("Hello", style="%%")


# ---------------------------------------------------------------------------
# load_prompt_spec — per-agent YAML file with agent/version/system schema
# ---------------------------------------------------------------------------


def _write_spec(tmp_path, filename="searcher.yaml", **fields):
    """Helper: write a minimal spec file and return its path."""
    import yaml as _yaml
    full = {"agent": "searcher", "version": 1, "system": "You are a searcher."}
    full.update(fields)
    p = tmp_path / filename
    p.write_text(_yaml.safe_dump(full))
    return p


class TestLoadPromptSpec:
    def test_loads_valid_spec(self, tmp_path):
        p = _write_spec(tmp_path)
        spec = load_prompt_spec(p)
        assert spec["agent"] == "searcher"
        assert spec["version"] == 1
        assert "searcher" in spec["system"]

    def test_validates_expected_agent(self, tmp_path):
        p = _write_spec(tmp_path, agent="searcher")
        # Mismatch → raises
        with pytest.raises(ConfigurationError, match="does not match"):
            load_prompt_spec(p, expected_agent="drafter")
        # Match → ok
        spec = load_prompt_spec(p, expected_agent="searcher")
        assert spec["agent"] == "searcher"

    def test_missing_required_keys_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("just_a_key: value\n")
        with pytest.raises(ConfigurationError, match="missing required keys"):
            load_prompt_spec(p)

    def test_non_int_version_raises(self, tmp_path):
        p = _write_spec(tmp_path, version="seven")
        with pytest.raises(ConfigurationError, match="version must be"):
            load_prompt_spec(p)

    def test_zero_version_raises(self, tmp_path):
        p = _write_spec(tmp_path, version=0)
        with pytest.raises(ConfigurationError, match="version must be"):
            load_prompt_spec(p)

    def test_empty_system_raises(self, tmp_path):
        p = _write_spec(tmp_path, system="   ")
        with pytest.raises(ConfigurationError, match="system must be"):
            load_prompt_spec(p)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not found"):
            load_prompt_spec(tmp_path / "missing.yaml")

    def test_optional_keys_preserved(self, tmp_path):
        p = _write_spec(
            tmp_path,
            changelog=["v1:Apr21 initial"],
            placeholders=["today", "block"],
            user="User: {query}",
        )
        spec = load_prompt_spec(p)
        assert spec["changelog"] == ["v1:Apr21 initial"]
        assert spec["placeholders"] == ["today", "block"]
        assert spec["user"] == "User: {query}"


# ---------------------------------------------------------------------------
# render_spec — (text, version) tuple
# ---------------------------------------------------------------------------


class TestRenderSpec:
    def test_renders_system_part_with_variables(self):
        spec = {
            "agent": "x", "version": 3,
            "system": "Hello {name}. Today is {today}.",
        }
        text, version = render_spec(spec, name="world", today="2026-04-21")
        assert text == "Hello world. Today is 2026-04-21."
        assert version == 3

    def test_returns_template_verbatim_without_variables(self):
        """No placeholders → no formatting, no KeyError risk."""
        spec = {"agent": "x", "version": 1, "system": "Static text {literal}"}
        # No kwargs → render_spec returns the raw template without .format().
        text, v = render_spec(spec)
        assert text == "Static text {literal}"
        assert v == 1

    def test_selects_user_part(self):
        spec = {
            "agent": "x", "version": 5,
            "system": "sys text",
            "user": "User: {q}",
        }
        text, version = render_spec(spec, part="user", q="query")
        assert text == "User: query"
        assert version == 5

    def test_missing_part_raises(self):
        spec = {"agent": "x", "version": 1, "system": "sys"}
        with pytest.raises(InvalidInputError, match="no 'user' section"):
            render_spec(spec, part="user")

    def test_token_style_for_json_heavy(self):
        spec = {
            "agent": "x", "version": 1,
            "system": 'Reply: {"key": "@@VAL@@"}',
        }
        text, _ = render_spec(spec, style="@@", VAL="hello")
        assert text == 'Reply: {"key": "hello"}'

    def test_non_string_template_raises(self):
        spec = {"agent": "x", "version": 1, "system": ["not", "a", "string"]}
        with pytest.raises(InvalidInputError, match="must be a string"):
            render_spec(spec)

    def test_missing_variable_raises(self):
        spec = {"agent": "x", "version": 1, "system": "Hello {name}"}
        with pytest.raises(InvalidInputError, match="undefined variables"):
            render_spec(spec, unrelated_kwarg="x")

    def test_integration_with_load_prompt_spec(self, tmp_path):
        """End-to-end: load a file, render with context, get (text, version)."""
        p = _write_spec(
            tmp_path,
            version=7,
            system="Today is {today}.",
        )
        spec = load_prompt_spec(p)
        text, version = render_spec(spec, today="2026-04-21")
        assert text == "Today is 2026-04-21."
        assert version == 7
