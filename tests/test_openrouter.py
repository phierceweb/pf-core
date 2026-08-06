"""Tests for pf_core.clients.openrouter — OpenRouter API client."""

from __future__ import annotations

import math
import time
from unittest.mock import MagicMock, patch

import pytest

from pf_core.clients.openrouter import (
    _retry_delay,
    OpenRouterClient,
    OpenRouterError,
    get_client,
    reset_client,
)
from pf_core.exceptions import ClientError


@pytest.fixture(autouse=True)
def _reset():
    reset_client()
    yield
    reset_client()


class TestOpenRouterClientInit:
    def test_requires_api_key(self):
        with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY"):
            OpenRouterClient(api_key="")

    def test_valid_init(self):
        client = OpenRouterClient(api_key="test-key")
        assert client.api_key == "test-key"
        assert client.base_url == "https://openrouter.ai/api/v1"

    def test_trailing_slash_stripped(self):
        client = OpenRouterClient(api_key="k", base_url="https://example.com/")
        assert client.base_url == "https://example.com"

    def test_custom_params(self):
        client = OpenRouterClient(
            api_key="k",
            app_name="MyApp",
            app_url="https://myapp.com",
            provider_ignore=["openai"],
            request_timeout=60,
        )
        assert client.app_name == "MyApp"
        assert client.app_url == "https://myapp.com"
        assert client.provider_ignore == ["openai"]
        assert client.request_timeout == 60

    def test_is_client_error(self):
        assert issubclass(OpenRouterError, ClientError)


class TestOpenRouterClientHeaders:
    def test_basic_headers(self):
        client = OpenRouterClient(api_key="test-key")
        h = client._headers()
        assert h["Authorization"] == "Bearer test-key"
        assert h["Content-Type"] == "application/json"
        assert "HTTP-Referer" not in h
        assert "X-Title" not in h

    def test_with_app_url(self):
        client = OpenRouterClient(api_key="k", app_url="https://myapp.com")
        h = client._headers()
        assert h["HTTP-Referer"] == "https://myapp.com"

    def test_with_app_name(self):
        client = OpenRouterClient(api_key="k", app_name="MyApp")
        h = client._headers()
        assert h["X-Title"] == "MyApp"


class TestOpenRouterClientChat:
    def _mock_response(
        self,
        content="Hello",
        status_code=200,
        usage=None,
        citations=None,
        error=None,
        system_fingerprint=None,
    ):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = content
        data = {
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
        }
        if citations:
            data["citations"] = citations
        if error:
            data["error"] = error
        if system_fingerprint is not None:
            data["system_fingerprint"] = system_fingerprint
        resp.json.return_value = data
        return resp

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_successful_chat(self, mock_post):
        mock_post.return_value = self._mock_response("Hello world")
        client = OpenRouterClient(api_key="k")
        content, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}],
            model="test/model",
        )
        assert content == "Hello world"
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 5
        assert "duration_ms" in usage

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_non_200_raises(self, mock_post):
        mock_post.return_value = self._mock_response(status_code=429, content="rate limited")
        client = OpenRouterClient(api_key="k")
        with pytest.raises(OpenRouterError, match="429"):
            client.chat(messages=[{"role": "user", "content": "Hi"}], model="test/model")

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_api_error_in_response(self, mock_post):
        mock_post.return_value = self._mock_response(error={"message": "bad model"})
        client = OpenRouterClient(api_key="k")
        with pytest.raises(OpenRouterError, match="OpenRouter error"):
            client.chat(messages=[{"role": "user", "content": "Hi"}], model="bad/model")

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_timeout_raises(self, mock_post):
        import httpx
        mock_post.side_effect = httpx.TimeoutException("timed out")
        client = OpenRouterClient(api_key="k", request_timeout=5)
        with pytest.raises(OpenRouterError, match="timed out"):
            client.chat(messages=[{"role": "user", "content": "Hi"}], model="test/model")

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_provider_ignore_merged(self, mock_post):
        mock_post.return_value = self._mock_response()
        client = OpenRouterClient(api_key="k", provider_ignore=["openai"])
        client.chat(
            messages=[{"role": "user", "content": "Hi"}],
            model="test/model",
            provider={"ignore": ["anthropic"]},
        )
        call_body = mock_post.call_args[1]["json"]
        assert "openai" in call_body["provider"]["ignore"]
        assert "anthropic" in call_body["provider"]["ignore"]

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_citations_in_usage_not_content(self, mock_post):
        """Citation URLs ride in usage["citations"]; content stays exactly
        what the model returned."""
        mock_post.return_value = self._mock_response(
            content="Answer",
            citations=["https://example.com/1", "https://example.com/2"],
        )
        client = OpenRouterClient(api_key="k")
        content, usage = client.chat(
            messages=[{"role": "user", "content": "search"}],
            model="perplexity/sonar-pro",
        )
        assert content == "Answer"
        assert usage["citations"] == [
            "https://example.com/1",
            "https://example.com/2",
        ]

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_no_citations_no_usage_key(self, mock_post):
        mock_post.return_value = self._mock_response(content="Answer")
        client = OpenRouterClient(api_key="k")
        content, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}],
            model="test/model",
        )
        assert content == "Answer"
        assert "citations" not in usage

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_none_content_returns_empty_string(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": None}}],
            "usage": {},
        }
        mock_post.return_value = resp
        client = OpenRouterClient(api_key="k")
        content, _ = client.chat(
            messages=[{"role": "user", "content": "Hi"}],
            model="test/model",
        )
        assert content == ""

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_response_format_passed(self, mock_post):
        mock_post.return_value = self._mock_response()
        client = OpenRouterClient(api_key="k")
        client.chat(
            messages=[{"role": "user", "content": "Hi"}],
            model="test/model",
            response_format={"type": "json_object"},
        )
        call_body = mock_post.call_args[1]["json"]
        assert call_body["response_format"] == {"type": "json_object"}


class TestOpenRouterClientChatUsageExpansion:
    """Round 3: expanded usage dict with fingerprint, cache/reasoning tokens."""

    def _mock(self, **data):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            **data,
        }
        return resp

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_new_usage_keys_always_present(self, mock_post):
        """Every response yields a usage dict with the full key set."""
        mock_post.return_value = self._mock(usage={"prompt_tokens": 10, "completion_tokens": 5})
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        for key in (
            "prompt_tokens", "completion_tokens",
            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
            "cost_usd", "duration_ms", "system_fingerprint",
        ):
            assert key in usage, f"missing {key}"
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_write_tokens"] == 0
        assert usage["reasoning_tokens"] == 0
        assert usage["system_fingerprint"] is None

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_system_fingerprint_passed_through(self, mock_post):
        mock_post.return_value = self._mock(
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            system_fingerprint="fp_abc123",
        )
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        assert usage["system_fingerprint"] == "fp_abc123"

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_cache_tokens_from_flat_keys(self, mock_post):
        """Anthropic-via-OpenRouter shape: flat cache_read_tokens / cache_write_tokens."""
        mock_post.return_value = self._mock(
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 800,
                "cache_read_tokens": 900,
                "cache_write_tokens": 100,
            },
        )
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        assert usage["cache_read_tokens"] == 900
        assert usage["cache_write_tokens"] == 100

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_cache_tokens_from_nested_details(self, mock_post):
        """OpenAI-via-OpenRouter shape: nested prompt_tokens_details.cached_tokens."""
        mock_post.return_value = self._mock(
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 800,
                "prompt_tokens_details": {"cached_tokens": 750},
            },
        )
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        assert usage["cache_read_tokens"] == 750

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_cache_write_from_anthropic_creation_key(self, mock_post):
        """Direct Anthropic shape leaks through with ``cache_creation_input_tokens``."""
        mock_post.return_value = self._mock(
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 800,
                "cache_creation_input_tokens": 240,
            },
        )
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        assert usage["cache_write_tokens"] == 240

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_reasoning_tokens_from_flat_and_nested(self, mock_post):
        """Reasoning-token extraction supports both flat and nested shapes."""
        mock_post.return_value = self._mock(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 400,
                "completion_tokens_details": {"reasoning_tokens": 300},
            },
        )
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        assert usage["reasoning_tokens"] == 300

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_flat_reasoning_tokens_preferred_when_both_present(self, mock_post):
        mock_post.return_value = self._mock(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 400,
                "reasoning_tokens": 250,
                "completion_tokens_details": {"reasoning_tokens": 999},
            },
        )
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        assert usage["reasoning_tokens"] == 250

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_missing_usage_object_still_yields_zeros(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_post.return_value = resp
        client = OpenRouterClient(api_key="k")
        _, usage = client.chat(
            messages=[{"role": "user", "content": "Hi"}], model="test/model"
        )
        assert usage["prompt_tokens"] == 0
        assert usage["completion_tokens"] == 0
        assert usage["cache_read_tokens"] == 0
        assert usage["reasoning_tokens"] == 0


class TestGetClient:
    def test_creates_client(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        client = get_client()
        assert client.api_key == "test-key"

    def test_singleton(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        c1 = get_client()
        c2 = get_client()
        assert c1 is c2

    def test_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setenv("APP_NAME", "TestApp")
        monkeypatch.setenv("APP_URL", "https://test.com")
        monkeypatch.setenv("REQUEST_TIMEOUT", "30")
        client = get_client()
        assert client.app_name == "TestApp"
        assert client.app_url == "https://test.com"
        assert client.request_timeout == 30

    def test_explicit_params_override_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
        client = get_client(api_key="explicit-key")
        assert client.api_key == "explicit-key"

    def test_no_api_key_raises(self, monkeypatch):
        # Hermetic: an ambient OPENROUTER_API_KEY (a real key in the dev's
        # .env, loaded by config.py's load_dotenv, or a CI secret) would
        # otherwise let get_client() succeed and this "must raise" assertion
        # fail under the full suite. Clear it so the no-key path is asserted
        # regardless of the environment the test runs in.
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(OpenRouterError):
            get_client()


class TestResetClient:
    def test_reset_allows_recreation(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
        c1 = get_client()
        reset_client()
        monkeypatch.setenv("OPENROUTER_API_KEY", "k2")
        c2 = get_client()
        assert c1 is not c2


# ---------------------------------------------------------------------------
# Retry on transient failure (cross-client parity with ClaudeCodeClient A3)
# ---------------------------------------------------------------------------


class TestRetry:
    """Auto-retry on transient HTTP failures: timeout, 429 (rate limit),
    5xx (server error). 4xx-other (400, 401, 403, etc.) are caller errors
    and are NOT retried even with retry > 0 — retrying just burns API
    calls on a deterministic failure."""

    def _resp(self, status_code=200, body="ok"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = body
        resp.headers = {}
        resp.json.return_value = {
            "choices": [{"message": {"content": body}}],
            "usage": {},
        }
        return resp

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_default_retry_zero_no_retry_on_failure(self, mock_post):
        mock_post.return_value = self._resp(status_code=503, body="upstream busy")
        client = OpenRouterClient(api_key="k")  # retry default 0
        with pytest.raises(OpenRouterError):
            client.chat(
                messages=[{"role": "user", "content": "x"}], model="m"
            )
        assert mock_post.call_count == 1

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_retry_on_5xx(self, mock_post, sleeps):
        mock_post.side_effect = [self._resp(status_code=502), self._resp()]
        client = OpenRouterClient(api_key="k", retry=1)
        content, _ = client.chat(
            messages=[{"role": "user", "content": "x"}], model="m"
        )
        assert content == "ok"
        assert mock_post.call_count == 2

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_retry_on_429(self, mock_post, sleeps):
        mock_post.side_effect = [
            self._resp(status_code=429, body="rate limit"),
            self._resp(),
        ]
        client = OpenRouterClient(api_key="k", retry=1)
        content, _ = client.chat(
            messages=[{"role": "user", "content": "x"}], model="m"
        )
        assert content == "ok"
        assert mock_post.call_count == 2

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_retry_on_timeout(self, mock_post, sleeps):
        import httpx

        mock_post.side_effect = [
            httpx.TimeoutException("timed out"),
            self._resp(),
        ]
        client = OpenRouterClient(api_key="k", retry=1)
        content, _ = client.chat(
            messages=[{"role": "user", "content": "x"}], model="m"
        )
        assert content == "ok"
        assert mock_post.call_count == 2

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_no_retry_on_4xx_other_than_429(self, mock_post):
        """400/401/403 are caller errors — retrying wastes API budget on
        a deterministic failure."""
        mock_post.return_value = self._resp(status_code=401, body="bad key")
        client = OpenRouterClient(api_key="k", retry=3)
        with pytest.raises(OpenRouterError):
            client.chat(
                messages=[{"role": "user", "content": "x"}], model="m"
            )
        assert mock_post.call_count == 1  # not retried

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_retry_exhausted_raises(self, mock_post, sleeps):
        mock_post.return_value = self._resp(status_code=503)
        client = OpenRouterClient(api_key="k", retry=2)
        with pytest.raises(OpenRouterError):
            client.chat(
                messages=[{"role": "user", "content": "x"}], model="m"
            )
        assert mock_post.call_count == 3  # initial + 2 retries

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_negative_retry_raises_client_error(self, mock_post):
        """retry < 0 leaves the attempt loop unentered — must raise a real
        OpenRouterError, not an assert (stripped under python -O)."""
        client = OpenRouterClient(api_key="k", retry=-1)
        with pytest.raises(OpenRouterError, match="retry"):
            client.chat(
                messages=[{"role": "user", "content": "x"}], model="m"
            )
        assert mock_post.call_count == 0

    def test_retry_via_get_client(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        c = get_client(retry=2)
        assert c.retry == 2

    def test_retry_default_zero(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        c = OpenRouterClient(api_key="k")
        assert c.retry == 0


# ---------------------------------------------------------------------------
# Preflight (cross-client parity with ClaudeCodeClient A2)
# ---------------------------------------------------------------------------


class TestPreflight:
    """OpenRouterClient.preflight() — fail-fast auth + connectivity check
    before launching a batch. Hits the cheap GET /models endpoint instead
    of burning an LLM call. Raises OpenRouterError with actionable
    remediation message on any failure."""

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_succeeds_returns_none(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="k")
        assert client.preflight() is None

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_calls_models_endpoint(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="k", base_url="https://example.com/v1")
        client.preflight()
        called_url = mock_get.call_args.args[0]
        assert called_url == "https://example.com/v1/models"

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_uses_auth_headers(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="my-key", app_name="MyApp")
        client.preflight()
        headers = mock_get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my-key"
        assert headers["X-Title"] == "MyApp"

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_default_timeout_is_short(self, mock_get):
        """Preflight should fail fast — short default timeout."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="k", request_timeout=600)
        client.preflight()
        assert mock_get.call_args.kwargs["timeout"] < 60

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_per_call_timeout_respected(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="k")
        client.preflight(timeout=5)
        assert mock_get.call_args.kwargs["timeout"] == 5

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_401_raises_with_actionable_message(self, mock_get):
        """Auth failure → raise with `OPENROUTER_API_KEY` remediation."""
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "Unauthorized"
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="bad")
        with pytest.raises(OpenRouterError, match=r"OPENROUTER_API_KEY"):
            client.preflight()

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_5xx_raises_with_preflight_marker(self, mock_get):
        resp = MagicMock()
        resp.status_code = 503
        resp.text = "service unavailable"
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="k")
        with pytest.raises(OpenRouterError, match=r"preflight"):
            client.preflight()

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_timeout_raises_with_preflight_marker(self, mock_get):
        import httpx

        mock_get.side_effect = httpx.TimeoutException("timed out")
        client = OpenRouterClient(api_key="k")
        with pytest.raises(OpenRouterError, match=r"preflight"):
            client.preflight()

    @patch("pf_core.clients.openrouter.httpx.get")
    def test_error_carries_preflight_context_flag(self, mock_get):
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "boom"
        mock_get.return_value = resp
        client = OpenRouterClient(api_key="k")
        with pytest.raises(OpenRouterError) as excinfo:
            client.preflight()
        assert excinfo.value.context.get("preflight") is True


# ---------------------------------------------------------------------------
# Retry pacing — backoff, jitter, Retry-After
# ---------------------------------------------------------------------------


@pytest.fixture
def sleeps(monkeypatch):
    """Record retry sleeps instead of sleeping.

    Asserts what the real ``time.sleep`` accepts — a permissive stub hides an
    invalid delay from every test here.
    """
    recorded: list[float] = []

    def _record(seconds: float) -> None:
        assert math.isfinite(seconds) and seconds >= 0, f"time.sleep would reject {seconds!r}"
        recorded.append(seconds)

    monkeypatch.setattr(time, "sleep", _record)
    return recorded


class TestRetryPacing:
    """A retry budget must not burn inside a millisecond: every re-attempt
    sleeps, 429 honours a sane Retry-After (capped), and the final attempt
    never sleeps."""

    def _resp(self, status_code=200, body="ok", headers=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = body
        resp.headers = headers if headers is not None else {}
        resp.json.return_value = {
            "choices": [{"message": {"content": body}}],
            "usage": {},
        }
        return resp

    def _chat(self, client):
        return client.chat(messages=[{"role": "user", "content": "x"}], model="m")

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_5xx_backoff_grows_between_attempts(self, mock_post, sleeps):
        mock_post.return_value = self._resp(status_code=503)
        client = OpenRouterClient(api_key="k", retry=2)
        with pytest.raises(OpenRouterError):
            self._chat(client)
        assert len(sleeps) == 2
        assert 0.5 <= sleeps[0] < 0.75
        assert 1.0 <= sleeps[1] < 1.5

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_429_honours_retry_after(self, mock_post, sleeps):
        mock_post.side_effect = [
            self._resp(status_code=429, headers={"Retry-After": "7"}),
            self._resp(),
        ]
        client = OpenRouterClient(api_key="k", retry=1)
        content, _ = self._chat(client)
        assert content == "ok"
        assert sleeps == [7.0]

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_retry_after_capped(self, mock_post, sleeps):
        mock_post.side_effect = [
            self._resp(status_code=429, headers={"Retry-After": "600"}),
            self._resp(),
        ]
        client = OpenRouterClient(api_key="k", retry=1)
        self._chat(client)
        assert sleeps == [30.0]

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_malformed_retry_after_falls_back_to_backoff(self, mock_post, sleeps):
        mock_post.side_effect = [
            self._resp(status_code=429, headers={"Retry-After": "in a bit"}),
            self._resp(),
        ]
        client = OpenRouterClient(api_key="k", retry=1)
        self._chat(client)
        assert len(sleeps) == 1
        assert 0.5 <= sleeps[0] < 0.75

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_429_without_retry_after_uses_backoff(self, mock_post, sleeps):
        mock_post.side_effect = [self._resp(status_code=429), self._resp()]
        client = OpenRouterClient(api_key="k", retry=1)
        self._chat(client)
        assert len(sleeps) == 1
        assert 0.5 <= sleeps[0] < 0.75

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_5xx_retry_after_ignored(self, mock_post, sleeps):
        mock_post.side_effect = [
            self._resp(status_code=503, headers={"Retry-After": "9"}),
            self._resp(),
        ]
        client = OpenRouterClient(api_key="k", retry=1)
        self._chat(client)
        assert len(sleeps) == 1
        assert 0.5 <= sleeps[0] < 0.75

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_timeout_retry_sleeps(self, mock_post, sleeps):
        import httpx

        mock_post.side_effect = [httpx.TimeoutException("timed out"), self._resp()]
        client = OpenRouterClient(api_key="k", retry=1)
        self._chat(client)
        assert len(sleeps) == 1
        assert 0.5 <= sleeps[0] < 0.75

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_exhausted_timeout_no_final_sleep(self, mock_post, sleeps):
        import httpx

        mock_post.side_effect = httpx.TimeoutException("timed out")
        client = OpenRouterClient(api_key="k", retry=1)
        with pytest.raises(OpenRouterError):
            self._chat(client)
        assert len(sleeps) == 1

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_jitter_varies_delay(self, mock_post, sleeps):
        mock_post.return_value = self._resp(status_code=503)
        for _ in range(12):
            client = OpenRouterClient(api_key="k", retry=1)
            with pytest.raises(OpenRouterError):
                self._chat(client)
        assert len(set(sleeps)) > 1

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_no_sleep_on_success(self, mock_post, sleeps):
        mock_post.return_value = self._resp()
        client = OpenRouterClient(api_key="k", retry=2)
        self._chat(client)
        assert sleeps == []

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_no_sleep_on_non_retryable_status(self, mock_post, sleeps):
        mock_post.return_value = self._resp(status_code=401, body="bad key")
        client = OpenRouterClient(api_key="k", retry=2)
        with pytest.raises(OpenRouterError):
            self._chat(client)
        assert sleeps == []


# ---------------------------------------------------------------------------
# Malformed choices + truncation
# ---------------------------------------------------------------------------


class TestChoicesAndFinishReason:
    def _resp(self, data):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = data
        return resp

    def _chat(self, client):
        return client.chat(messages=[{"role": "user", "content": "x"}], model="m")

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_empty_choices_raises(self, mock_post):
        mock_post.return_value = self._resp({"choices": [], "usage": {}})
        client = OpenRouterClient(api_key="k")
        with pytest.raises(OpenRouterError, match="choices"):
            self._chat(client)

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_missing_choices_key_raises(self, mock_post):
        mock_post.return_value = self._resp({"usage": {}})
        client = OpenRouterClient(api_key="k")
        with pytest.raises(OpenRouterError, match="choices"):
            self._chat(client)

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_missing_message_yields_empty_content(self, mock_post):
        mock_post.return_value = self._resp({"choices": [{}], "usage": {}})
        client = OpenRouterClient(api_key="k")
        content, _ = self._chat(client)
        assert content == ""

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_finish_reason_surfaced_in_usage(self, mock_post):
        mock_post.return_value = self._resp(
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
        client = OpenRouterClient(api_key="k")
        _, usage = self._chat(client)
        assert usage["finish_reason"] == "stop"

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_finish_reason_none_when_absent(self, mock_post):
        mock_post.return_value = self._resp(
            {"choices": [{"message": {"content": "hi"}}], "usage": {}}
        )
        client = OpenRouterClient(api_key="k")
        _, usage = self._chat(client)
        assert usage["finish_reason"] is None

    @patch("pf_core.clients.openrouter.httpx.post")
    def test_max_tokens_truncation_surfaced(self, mock_post):
        mock_post.return_value = self._resp(
            {
                "choices": [
                    {"message": {"content": "half an ans"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
            }
        )
        client = OpenRouterClient(api_key="k")
        with patch("pf_core.clients.openrouter._log") as mock_log:
            content, usage = self._chat(client)
        assert content == "half an ans"
        assert usage["finish_reason"] == "length"
        assert mock_log.warning.call_args.args[0] == "openrouter_truncated"


class TestRetryAfterClamping:
    """A hostile or broken ``Retry-After`` must not reach ``time.sleep``, which
    raises on a negative or NaN argument."""

    @pytest.mark.parametrize(
        "header",
        ["5", "0", "-1", "-5", "nan", "inf", "-inf", "120", "garbage",
         "Wed, 21 Oct 2015 07:28:00 GMT"],
    )
    def test_delay_is_always_sleepable(self, header):
        delay = _retry_delay(0, header)
        assert math.isfinite(delay) and delay >= 0
        time.sleep(0) if delay > 1 else time.sleep(delay)

    def test_negative_floors_to_zero(self):
        assert _retry_delay(0, "-5") == 0.0

    def test_nan_falls_through_to_backoff(self):
        assert _retry_delay(0, "nan") > 0

    def test_oversized_still_caps(self):
        assert _retry_delay(0, "inf") == 30.0
        assert _retry_delay(0, "120") == 30.0
