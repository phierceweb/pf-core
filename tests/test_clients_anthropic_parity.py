"""Tests for the Anthropic parity pack: system-message extraction,
``response_format`` mapping, prompt caching, and cache-aware cost.

Mocks the official ``anthropic.Anthropic`` SDK class at the module
boundary; no real API calls. Mock-response helper mirrors
``test_clients_anthropic.py`` (kept local — test files don't import
each other).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pf_core.clients.anthropic import AnthropicClient, AnthropicError


def _mock_sdk_response(
    *,
    text: str = "Hello",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
):
    """Build a MagicMock that imitates anthropic.types.Message shape."""
    block = MagicMock()
    block.text = text
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_read_input_tokens = cache_read_input_tokens
    usage.cache_creation_input_tokens = cache_creation_input_tokens
    usage.thinking_tokens = 0
    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _chat_kwargs(mock_sdk):
    """The kwargs the client sent to messages.create()."""
    return mock_sdk.return_value.messages.create.call_args.kwargs


@pytest.fixture(autouse=True)
def _clear_response_format_warn_state():
    from pf_core.clients import anthropic as anthropic_mod

    anthropic_mod._response_format_warned.clear()
    yield
    anthropic_mod._response_format_warned.clear()


class TestSystemExtraction:
    def test_no_system_messages_request_shape_unchanged(self):
        """Plain user-only call sends exactly today's key set — no system=."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(messages=[{"role": "user", "content": "Hi"}])
            kw = _chat_kwargs(mock_sdk)
            assert set(kw) == {"model", "max_tokens", "messages", "temperature", "top_p"}
            assert kw["messages"] == [{"role": "user", "content": "Hi"}]

    def test_leading_system_extracted_to_param(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=[
                    {"role": "system", "content": "You are terse."},
                    {"role": "user", "content": "Hi"},
                ]
            )
            kw = _chat_kwargs(mock_sdk)
            assert kw["system"] == "You are terse."
            assert kw["messages"] == [{"role": "user", "content": "Hi"}]

    def test_multiple_leading_system_concatenated(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=[
                    {"role": "system", "content": "Rule A."},
                    {"role": "system", "content": "Rule B."},
                    {"role": "user", "content": "Hi"},
                ]
            )
            kw = _chat_kwargs(mock_sdk)
            assert kw["system"] == "Rule A.\n\nRule B."
            assert kw["messages"] == [{"role": "user", "content": "Hi"}]

    def test_non_leading_system_left_in_messages(self):
        """System entries after the first non-system message pass through."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            msgs = [
                {"role": "user", "content": "Hi"},
                {"role": "system", "content": "late"},
            ]
            client.chat(messages=msgs)
            kw = _chat_kwargs(mock_sdk)
            assert "system" not in kw
            assert kw["messages"] == msgs

    def test_non_string_system_content_not_extracted(self):
        """Block-form system content isn't extracted — forwarded untouched."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            msgs = [
                {"role": "system", "content": [{"type": "text", "text": "x"}]},
                {"role": "user", "content": "Hi"},
            ]
            client.chat(messages=msgs)
            kw = _chat_kwargs(mock_sdk)
            assert "system" not in kw
            assert kw["messages"] == msgs


_SYS = [{"role": "system", "content": "You are terse."}, {"role": "user", "content": "Hi"}]


class TestCacheSystem:
    def test_cache_system_sends_block_form_with_cache_control(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(messages=list(_SYS), cache_system=True)
            kw = _chat_kwargs(mock_sdk)
            assert kw["system"] == [
                {
                    "type": "text",
                    "text": "You are terse.",
                    "cache_control": {"type": "ephemeral"},
                }
            ]

    def test_cache_ttl_1h_adds_ttl_field(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(messages=list(_SYS), cache_system=True, cache_ttl="1h")
            kw = _chat_kwargs(mock_sdk)
            assert kw["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_invalid_cache_ttl_raises(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            client = AnthropicClient(api_key="k", model="m")
            with pytest.raises(AnthropicError, match="cache_ttl"):
                client.chat(messages=list(_SYS), cache_system=True, cache_ttl="2h")
            mock_sdk.return_value.messages.create.assert_not_called()

    def test_cache_system_without_system_is_noop(self):
        """Router-config safe: agents without a system prompt don't error."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(messages=[{"role": "user", "content": "Hi"}], cache_system=True)
            kw = _chat_kwargs(mock_sdk)
            assert "system" not in kw

    def test_default_no_caching_sends_plain_string(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(messages=list(_SYS))
            assert _chat_kwargs(mock_sdk)["system"] == "You are terse."


_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


class TestResponseFormatJsonSchema:
    def test_bare_schema_maps_to_output_config(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                response_format={"type": "json_schema", "schema": _SCHEMA},
            )
            kw = _chat_kwargs(mock_sdk)
            assert kw["output_config"] == {
                "format": {"type": "json_schema", "schema": _SCHEMA}
            }
            assert "response_format" not in kw

    def test_openai_nested_schema_maps_to_output_config(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "extract", "schema": _SCHEMA},
                },
            )
            kw = _chat_kwargs(mock_sdk)
            assert kw["output_config"] == {
                "format": {"type": "json_schema", "schema": _SCHEMA}
            }

    def test_json_schema_without_schema_raises(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            client = AnthropicClient(api_key="k", model="m")
            with pytest.raises(AnthropicError, match="schema"):
                client.chat(
                    messages=[{"role": "user", "content": "Hi"}],
                    response_format={"type": "json_schema"},
                )
            mock_sdk.return_value.messages.create.assert_not_called()

    def test_none_response_format_untouched(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(messages=[{"role": "user", "content": "Hi"}], response_format=None)
            kw = _chat_kwargs(mock_sdk)
            assert "output_config" not in kw


class TestResponseFormatJsonObject:
    def test_json_object_appends_instruction_to_system(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=list(_SYS),
                response_format={"type": "json_object"},
            )
            kw = _chat_kwargs(mock_sdk)
            assert kw["system"].startswith("You are terse.")
            assert kw["system"].endswith("no prose, no code fences.")
            assert "output_config" not in kw

    def test_json_object_creates_system_when_absent(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                response_format={"type": "json_object"},
            )
            kw = _chat_kwargs(mock_sdk)
            assert kw["system"].startswith("Respond with a single valid JSON object")

    def test_json_object_warns_once_per_process(self):
        from pf_core.clients import anthropic as anthropic_mod

        with patch("anthropic.Anthropic") as mock_sdk, patch.object(
            anthropic_mod._log, "warning"
        ) as mock_warn:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            for _ in range(2):
                client.chat(
                    messages=[{"role": "user", "content": "Hi"}],
                    response_format={"type": "json_object"},
                )
            warn_events = [c.args[0] for c in mock_warn.call_args_list]
            assert warn_events.count("anthropic_json_object_prompt_enforced") == 1

    def test_json_object_composes_with_cache_system(self):
        """Instruction lands inside the cached block — stable prefix."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=list(_SYS),
                response_format={"type": "json_object"},
                cache_system=True,
            )
            kw = _chat_kwargs(mock_sdk)
            (block,) = kw["system"]
            assert block["cache_control"] == {"type": "ephemeral"}
            assert block["text"].startswith("You are terse.")
            assert block["text"].endswith("no prose, no code fences.")


class TestResponseFormatUnknown:
    def test_unknown_type_ignored_with_one_shot_warning(self):
        from pf_core.clients import anthropic as anthropic_mod

        with patch("anthropic.Anthropic") as mock_sdk, patch.object(
            anthropic_mod._log, "warning"
        ) as mock_warn:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            for _ in range(2):
                client.chat(
                    messages=[{"role": "user", "content": "Hi"}],
                    response_format={"type": "grammar"},
                )
            kw = _chat_kwargs(mock_sdk)
            assert "output_config" not in kw
            assert "system" not in kw
            warn_events = [c.args[0] for c in mock_warn.call_args_list]
            assert warn_events.count("anthropic_response_format_ignored") == 1


class TestCacheAwareCost:
    def test_cache_tokens_passed_to_estimate_cost(self):
        from pf_core.clients import anthropic as anthropic_mod

        with patch("anthropic.Anthropic") as mock_sdk, patch.object(
            anthropic_mod, "estimate_cost", return_value=0.5
        ) as mock_cost:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=800,
                cache_creation_input_tokens=200,
            )
            client = AnthropicClient(api_key="k", model="m")
            _, usage = client.chat(messages=[{"role": "user", "content": "Hi"}])
            mock_cost.assert_called_once_with(
                "anthropic",
                "m",
                prompt_tokens=100,
                completion_tokens=50,
                cache_read_tokens=800,
                cache_write_tokens=200,
            )
            assert usage["cost_usd"] == 0.5
