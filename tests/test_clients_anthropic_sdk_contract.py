"""Contract tests binding AnthropicClient's call kwargs to the REAL installed SDK.

Every other Anthropic test mocks ``anthropic.Anthropic`` at the module boundary,
so it passes whether the SDK is present, absent, or incompatible. These bind the
kwargs ``chat()`` actually builds against the real ``Messages.create`` signature,
which is the only thing that catches the SDK dropping a parameter pf-core sends.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest

anthropic = pytest.importorskip("anthropic", reason="requires the [anthropic] extra")

from pf_core.clients.anthropic import AnthropicClient  # noqa: E402


def _canned_response():
    """Minimal stand-in for anthropic.types.Message, matching what chat() reads."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="pong")],
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            thinking_tokens=0,
        ),
        stop_reason="end_turn",
    )


@pytest.fixture
def binding_client():
    """An AnthropicClient whose messages.create binds kwargs to the real signature.

    Yields ``(client, captured)``; ``captured`` accumulates each call's kwargs.
    """
    client = AnthropicClient(api_key="contract-test-key", model="claude-sonnet-4-5")
    real_sig = inspect.signature(client._client.messages.create)  # bound — no `self`
    captured: list[dict] = []

    def _binding_create(**kwargs):
        real_sig.bind(**kwargs)
        captured.append(kwargs)
        return _canned_response()

    with patch.object(client._client.messages, "create", _binding_create):
        yield client, captured


class TestChatKwargsMatchSdkSignature:
    """chat() must not send a kwarg the installed SDK's messages.create rejects."""

    def test_default_call(self, binding_client):
        client, captured = binding_client
        content, usage = client.chat(messages=[{"role": "user", "content": "hi"}])
        assert content == "pong"
        assert usage["prompt_tokens"] == 3
        # temperature is sent by default — the parameter this guards.
        assert captured[0]["temperature"] == 0.2

    def test_temperature_opted_out(self, binding_client):
        client, captured = binding_client
        client.chat(messages=[{"role": "user", "content": "hi"}], temperature=None)
        assert "temperature" not in captured[0]

    def test_top_p(self, binding_client):
        client, captured = binding_client
        client.chat(messages=[{"role": "user", "content": "hi"}], temperature=None, top_p=0.9)
        assert captured[0]["top_p"] == 0.9

    def test_system_prompt(self, binding_client):
        client, _ = binding_client
        client.chat(
            messages=[
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ]
        )

    def test_cached_system_prompt(self, binding_client):
        client, captured = binding_client
        client.chat(
            messages=[
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ],
            cache_system=True,
            cache_ttl="1h",
        )
        assert captured[0]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_json_schema_response_format(self, binding_client):
        client, _ = binding_client
        client.chat(
            messages=[{"role": "user", "content": "hi"}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "reply",
                    "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
                },
            },
        )

    def test_max_tokens(self, binding_client):
        client, captured = binding_client
        client.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=64)
        assert captured[0]["max_tokens"] == 64
