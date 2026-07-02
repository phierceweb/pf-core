"""Tests for pf_core.clients.anthropic — Anthropic API client.

Mocks the official ``anthropic.Anthropic`` SDK class at the module
boundary; no real API calls. Verifies constructor validation, the
``(content, usage)`` return shape, error translation, env-var-driven
singleton behavior, and SDK-import failure handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pf_core.clients.anthropic import (
    AnthropicClient,
    AnthropicError,
    get_client,
    reset_client,
)
from pf_core.exceptions import ClientError


@pytest.fixture(autouse=True)
def _reset():
    reset_client()
    yield
    reset_client()


def _mock_sdk_response(
    *,
    text: str = "Hello",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    thinking_tokens: int = 0,
    extra_blocks: list | None = None,
):
    """Build a MagicMock that imitates anthropic.types.Message shape."""
    block = MagicMock()
    block.text = text
    content_blocks = [block]
    if extra_blocks:
        content_blocks.extend(extra_blocks)

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_read_input_tokens = cache_read_input_tokens
    usage.cache_creation_input_tokens = cache_creation_input_tokens
    usage.thinking_tokens = thinking_tokens

    response = MagicMock()
    response.content = content_blocks
    response.usage = usage
    return response


class TestAnthropicClientInit:
    def test_requires_api_key(self):
        with pytest.raises(AnthropicError, match="api_key"):
            AnthropicClient(api_key="")

    @patch("pf_core.clients.anthropic.AnthropicClient.__init__", autospec=False)
    def test_is_client_error(self, _):
        # Subclass check doesn't require instantiation.
        assert issubclass(AnthropicError, ClientError)

    def test_valid_init(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            client = AnthropicClient(
                api_key="test-key", model="claude-haiku-4-5", request_timeout=60
            )
            assert client.api_key == "test-key"
            assert client.model == "claude-haiku-4-5"
            assert client.request_timeout == 60
            mock_sdk.assert_called_once_with(api_key="test-key", timeout=60)

    def test_missing_sdk_raises_import_error(self):
        """If anthropic isn't importable, constructor raises ImportError with a clear message."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("No module named 'anthropic'")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=fake_import),
            pytest.raises(ImportError, match=r"pf-core\[anthropic\]"),
        ):
            AnthropicClient(api_key="k")


class TestAnthropicClientChat:
    def test_successful_chat(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                text="Hello world",
                input_tokens=12,
                output_tokens=7,
            )
            client = AnthropicClient(api_key="k", model="claude-haiku-4-5")
            content, usage = client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                model="claude-haiku-4-5",
            )
            assert content == "Hello world"
            assert usage["prompt_tokens"] == 12
            assert usage["completion_tokens"] == 7
            assert "duration_ms" in usage

    def test_chat_passes_model_and_messages(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k")
            client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                model="claude-sonnet-4-5",
                temperature=0.7,
                max_tokens=1024,
                top_p=0.9,
            )
            call_kwargs = mock_sdk.return_value.messages.create.call_args.kwargs
            assert call_kwargs["model"] == "claude-sonnet-4-5"
            assert call_kwargs["messages"] == [{"role": "user", "content": "Hi"}]
            assert call_kwargs["temperature"] == 0.7
            assert call_kwargs["max_tokens"] == 1024
            assert call_kwargs["top_p"] == 0.9

    def test_chat_falls_back_to_instance_model(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="claude-haiku-4-5")
            client.chat(messages=[{"role": "user", "content": "Hi"}])
            call_kwargs = mock_sdk.return_value.messages.create.call_args.kwargs
            assert call_kwargs["model"] == "claude-haiku-4-5"

    def test_chat_requires_some_model(self):
        with patch("anthropic.Anthropic"):
            client = AnthropicClient(api_key="k")
            with pytest.raises(AnthropicError, match="No model"):
                client.chat(messages=[{"role": "user", "content": "Hi"}])

    def test_chat_translates_sdk_exception(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = RuntimeError(
                "boom"
            )
            client = AnthropicClient(api_key="k", model="claude-haiku-4-5")
            with pytest.raises(AnthropicError, match="boom"):
                client.chat(messages=[{"role": "user", "content": "Hi"}])

    def test_chat_concatenates_text_blocks(self):
        extra = MagicMock()
        extra.text = " world"
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                text="Hello", extra_blocks=[extra]
            )
            client = AnthropicClient(api_key="k", model="claude-haiku-4-5")
            content, _ = client.chat(
                messages=[{"role": "user", "content": "Hi"}]
            )
            assert content == "Hello world"

    def test_chat_skips_non_text_blocks(self):
        non_text = MagicMock()
        non_text.text = None  # e.g. a tool_use block
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                text="Hi there",
                extra_blocks=[non_text],
            )
            client = AnthropicClient(api_key="k", model="claude-haiku-4-5")
            content, _ = client.chat(
                messages=[{"role": "user", "content": "Hi"}]
            )
            assert content == "Hi there"

    def test_chat_usage_dict_has_full_key_set(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=20,
            )
            client = AnthropicClient(api_key="k", model="claude-haiku-4-5")
            _, usage = client.chat(messages=[{"role": "user", "content": "Hi"}])
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "cost_usd",
                "duration_ms",
                "system_fingerprint",
            ):
                assert key in usage, f"missing {key}"
            assert usage["prompt_tokens"] == 100
            assert usage["completion_tokens"] == 50
            assert usage["cache_read_tokens"] == 80
            assert usage["cache_write_tokens"] == 20
            assert usage["reasoning_tokens"] == 0
            # claude-haiku-4-5 → claude-haiku-4 prefix: $0.80/1M in, $4.0/1M out,
            # cache read $0.08/1M, 5m cache write $1.0/1M.
            # 100*0.80/1e6 + 50*4.0/1e6 + 80*0.08/1e6 + 20*1.0/1e6 = 0.0003064
            assert usage["cost_usd"] == pytest.approx(0.0003064)
            assert usage["system_fingerprint"] is None

    def test_chat_thinking_tokens_carried_to_reasoning_tokens(self):
        """Reasoning models emit usage.thinking_tokens → usage["reasoning_tokens"]."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                input_tokens=100,
                output_tokens=200,
                thinking_tokens=150,
            )
            client = AnthropicClient(api_key="k", model="claude-opus-4-7")
            _, usage = client.chat(messages=[{"role": "user", "content": "Hi"}])
            assert usage["reasoning_tokens"] == 150
            # claude-opus-4 prefix: $15/1M in, $75/1M out. Thinking tokens are
            # billed at the output rate and already counted in output_tokens,
            # so cost is output_tokens * output_rate (not + thinking again).
            # 100 * 15/1e6 + 200 * 75/1e6 = 0.0015 + 0.015 = 0.0165
            assert usage["cost_usd"] == pytest.approx(0.0165)

    def test_chat_cost_for_sonnet_family(self):
        """Prefix match resolves claude-sonnet-4-5 → claude-sonnet-4 rates."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                input_tokens=1000,
                output_tokens=500,
            )
            client = AnthropicClient(api_key="k", model="claude-sonnet-4-5")
            _, usage = client.chat(messages=[{"role": "user", "content": "Hi"}])
            # $3/1M in, $15/1M out: 1000 * 3/1e6 + 500 * 15/1e6 = 0.0105
            assert usage["cost_usd"] == pytest.approx(0.0105)

    def test_chat_unknown_model_cost_zero_with_one_shot_warning(self):
        """Unmatched model id → cost 0.0, and the warning fires once per id."""
        from pf_core.pricing import _resolver

        _resolver._unknown_warned.discard("anthropic:totally-made-up-model")
        with patch("anthropic.Anthropic") as mock_sdk, patch.object(
            _resolver.logger, "warning"
        ) as mock_warn:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response(
                input_tokens=100, output_tokens=50
            )
            client = AnthropicClient(api_key="k", model="totally-made-up-model")
            _, usage1 = client.chat(messages=[{"role": "user", "content": "Hi"}])
            _, usage2 = client.chat(messages=[{"role": "user", "content": "Hi"}])
            assert usage1["cost_usd"] == 0.0
            assert usage2["cost_usd"] == 0.0
            # One-shot: warned on the first call only, not the second.
            assert mock_warn.call_count == 1
        _resolver._unknown_warned.discard("anthropic:totally-made-up-model")


class TestGetClient:
    def test_creates_client_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        with patch("anthropic.Anthropic"):
            client = get_client()
            assert client.api_key == "env-key"

    def test_singleton(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        with patch("anthropic.Anthropic"):
            c1 = get_client()
            c2 = get_client()
            assert c1 is c2

    def test_explicit_api_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        with patch("anthropic.Anthropic"):
            client = get_client(api_key="explicit-key")
            assert client.api_key == "explicit-key"

    def test_no_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(AnthropicError, match="ANTHROPIC_API_KEY"):
            get_client()


class TestResetClient:
    def test_reset_allows_recreation(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k1")
        with patch("anthropic.Anthropic"):
            c1 = get_client()
            reset_client()
            monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
            c2 = get_client()
            assert c1 is not c2


# ---------------------------------------------------------------------------
# Retry on transient failure (cross-client parity with ClaudeCodeClient A3)
# ---------------------------------------------------------------------------


class TestRetry:
    """Auto-retry on AnthropicError. Layered on top of the SDK's own
    internal retries — pf-core retries kick in once the SDK has
    exhausted its own. Validation failures (no model specified) raise
    immediately even with retry > 0."""

    def test_default_retry_is_zero(self):
        with patch("anthropic.Anthropic"):
            c = AnthropicClient(api_key="k", model="m")
            assert c.retry == 0

    def test_default_retry_no_retry_on_failure(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = RuntimeError("boom")
            client = AnthropicClient(api_key="k", model="m")
            with pytest.raises(AnthropicError):
                client.chat(messages=[{"role": "user", "content": "x"}])
            assert mock_sdk.return_value.messages.create.call_count == 1

    def test_retry_one_succeeds_on_second_attempt(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = [
                RuntimeError("transient"),
                _mock_sdk_response(text="recovered"),
            ]
            client = AnthropicClient(api_key="k", model="m", retry=1)
            content, _ = client.chat(messages=[{"role": "user", "content": "x"}])
            assert content == "recovered"
            assert mock_sdk.return_value.messages.create.call_count == 2

    def test_retry_exhausted_raises(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = RuntimeError("boom")
            client = AnthropicClient(api_key="k", model="m", retry=2)
            with pytest.raises(AnthropicError):
                client.chat(messages=[{"role": "user", "content": "x"}])
            # initial + 2 retries = 3 attempts
            assert mock_sdk.return_value.messages.create.call_count == 3

    def test_no_model_validation_not_retried(self):
        """Caller-error validation (no model) raises immediately — retry
        won't help when the input is wrong, just burns time."""
        with patch("anthropic.Anthropic") as mock_sdk:
            client = AnthropicClient(api_key="k", retry=3)  # no model
            with pytest.raises(AnthropicError, match="No model"):
                client.chat(messages=[{"role": "user", "content": "x"}])
            # SDK never called — failure was at validation
            assert mock_sdk.return_value.messages.create.call_count == 0

    def test_retry_via_get_client(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        with patch("anthropic.Anthropic"):
            c = get_client(retry=2)
            assert c.retry == 2


# ---------------------------------------------------------------------------
# Per-call timeout (was documented as "ignored"; now honored)
# ---------------------------------------------------------------------------


class TestPerCallTimeout:
    """Per-call ``chat(timeout=N)`` is now honored via the SDK's
    ``with_options(timeout=...)`` derived-client pattern. Closes the gap
    where the docstring openly admitted it was ignored."""

    def test_per_call_timeout_uses_with_options(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            derived = MagicMock()
            derived.messages.create.return_value = _mock_sdk_response()
            mock_sdk.return_value.with_options.return_value = derived

            client = AnthropicClient(api_key="k", model="m")
            client.chat(
                messages=[{"role": "user", "content": "x"}], timeout=10
            )
            mock_sdk.return_value.with_options.assert_called_once_with(timeout=10)
            derived.messages.create.assert_called_once()

    def test_no_per_call_timeout_uses_base_client(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _mock_sdk_response()
            client = AnthropicClient(api_key="k", model="m")
            client.chat(messages=[{"role": "user", "content": "x"}])
            # No with_options call — base client used directly
            mock_sdk.return_value.with_options.assert_not_called()
            mock_sdk.return_value.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# Preflight (cross-client parity with ClaudeCodeClient A2)
# ---------------------------------------------------------------------------


class TestPreflight:
    """AnthropicClient.preflight() — fail-fast auth + connectivity check
    before launching a batch. Hits the cheap models.list() endpoint
    instead of burning an LLM call. Raises AnthropicError with an
    actionable message on any failure; ``context["preflight"] = True``
    so log filters distinguish preflight failures from per-call failures."""

    def test_succeeds_returns_none(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            # with_options() returns a derived client; chain it back to
            # the base mock so .models.list configuration on the base
            # propagates through.
            mock_sdk.return_value.with_options.return_value = mock_sdk.return_value
            mock_sdk.return_value.models.list.return_value = MagicMock()
            client = AnthropicClient(api_key="k")
            assert client.preflight() is None

    def test_calls_models_list(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.with_options.return_value = mock_sdk.return_value
            mock_sdk.return_value.models.list.return_value = MagicMock()
            client = AnthropicClient(api_key="k")
            client.preflight()
            mock_sdk.return_value.models.list.assert_called_once()

    def test_failure_raises_with_preflight_marker(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.with_options.return_value = mock_sdk.return_value
            mock_sdk.return_value.models.list.side_effect = RuntimeError("auth fail")
            client = AnthropicClient(api_key="bad")
            with pytest.raises(AnthropicError, match=r"preflight"):
                client.preflight()

    def test_failure_message_mentions_api_key(self):
        """Actionable message points at the env var operators can fix."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.with_options.return_value = mock_sdk.return_value
            mock_sdk.return_value.models.list.side_effect = RuntimeError(
                "401 Unauthorized"
            )
            client = AnthropicClient(api_key="bad")
            with pytest.raises(AnthropicError, match=r"ANTHROPIC_API_KEY"):
                client.preflight()

    def test_error_carries_preflight_context_flag(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.with_options.return_value = mock_sdk.return_value
            mock_sdk.return_value.models.list.side_effect = RuntimeError("boom")
            client = AnthropicClient(api_key="k")
            with pytest.raises(AnthropicError) as excinfo:
                client.preflight()
            assert excinfo.value.context.get("preflight") is True

    def test_default_timeout_is_short(self):
        """Preflight uses a short derived timeout — fail fast."""
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.models.list.return_value = MagicMock()
            derived = MagicMock()
            derived.models.list.return_value = MagicMock()
            mock_sdk.return_value.with_options.return_value = derived

            client = AnthropicClient(
                api_key="k", request_timeout=600
            )  # long instance default
            client.preflight()
            # with_options called with a short timeout (< 60s)
            (call,) = mock_sdk.return_value.with_options.call_args_list
            assert call.kwargs["timeout"] < 60
