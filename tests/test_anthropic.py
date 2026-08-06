"""Retry-safety regressions for pf_core.clients.anthropic.

Covers what the retry loop must NOT do: re-send deterministic 4xx, let the
SDK's own retries multiply with pf-core's, re-send a read timeout whose
completion may already be billed, or hammer with zero delay.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from pf_core.clients.anthropic import AnthropicClient, AnthropicError, reset_client


@pytest.fixture(autouse=True)
def _reset():
    reset_client()
    yield
    reset_client()


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("pf_core.clients.anthropic.time.sleep") as sleep:
        yield sleep


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, code: int):
    return cls(
        f"{code}", response=httpx.Response(code, request=_request()), body=None
    )


def _timeout(cause: Exception) -> APITimeoutError:
    err = APITimeoutError(request=_request())
    err.__cause__ = cause
    return err


def _ok_response():
    block = MagicMock()
    block.text = "ok"
    usage = MagicMock()
    usage.input_tokens = 1
    usage.output_tokens = 1
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    usage.thinking_tokens = 0
    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


class TestDeterministicErrorsNotRetried:
    """400/401/403/404 fail the same way every time — re-sending only burns
    budget and wall-clock."""

    @pytest.mark.parametrize(
        ("cls", "code"),
        [
            (BadRequestError, 400),
            (AuthenticationError, 401),
            (PermissionDeniedError, 403),
            (NotFoundError, 404),
        ],
    )
    def test_client_error_attempted_once(self, cls, code):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = _status_error(cls, code)
            client = AnthropicClient(api_key="k", model="m", retry=3)
            with pytest.raises(AnthropicError):
                client.chat(messages=[{"role": "user", "content": "x"}])
            assert mock_sdk.return_value.messages.create.call_count == 1

    @pytest.mark.parametrize(
        ("cls", "code"),
        [(RateLimitError, 429), (InternalServerError, 500)],
    )
    def test_transient_status_is_retried(self, cls, code):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = _status_error(cls, code)
            client = AnthropicClient(api_key="k", model="m", retry=2)
            with pytest.raises(AnthropicError):
                client.chat(messages=[{"role": "user", "content": "x"}])
            assert mock_sdk.return_value.messages.create.call_count == 3


class TestSdkRetriesDoNotMultiply:
    """The SDK defaults to max_retries=2; left unset it multiplies with the
    pf-core loop (retry=2 → 3 x 3 = 9 HTTP calls for one logical request)."""

    def test_constructor_disables_sdk_retries(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            AnthropicClient(api_key="k", request_timeout=60)
            assert mock_sdk.call_args.kwargs["max_retries"] == 0


class TestReadTimeoutNotRetried:
    """A read timeout means the request reached the server: the completion may
    already be generated and billed. A connect timeout never left the client."""

    def test_read_timeout_attempted_once(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = _timeout(
                httpx.ReadTimeout("read timed out")
            )
            client = AnthropicClient(api_key="k", model="m", retry=3)
            with pytest.raises(AnthropicError):
                client.chat(messages=[{"role": "user", "content": "x"}])
            assert mock_sdk.return_value.messages.create.call_count == 1

    def test_connect_timeout_is_retried(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = [
                _timeout(httpx.ConnectTimeout("connect timed out")),
                _ok_response(),
            ]
            client = AnthropicClient(api_key="k", model="m", retry=1)
            content, _ = client.chat(messages=[{"role": "user", "content": "x"}])
            assert content == "ok"
            assert mock_sdk.return_value.messages.create.call_count == 2

    def test_connection_error_is_retried(self):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = [
                APIConnectionError(request=_request()),
                _ok_response(),
            ]
            client = AnthropicClient(api_key="k", model="m", retry=1)
            content, _ = client.chat(messages=[{"role": "user", "content": "x"}])
            assert content == "ok"
            assert mock_sdk.return_value.messages.create.call_count == 2


class TestRetryBacksOff:
    """Zero-delay retries re-hit a rate-limited or overloaded endpoint inside
    the same window and just burn the budget faster."""

    def test_sleeps_between_attempts(self, _no_sleep):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.side_effect = _status_error(
                RateLimitError, 429
            )
            client = AnthropicClient(api_key="k", model="m", retry=2)
            with pytest.raises(AnthropicError):
                client.chat(messages=[{"role": "user", "content": "x"}])
            delays = [c.args[0] for c in _no_sleep.call_args_list]
            assert len(delays) == 2
            assert all(d > 0 for d in delays)
            assert delays[1] > delays[0]

    def test_no_sleep_on_success(self, _no_sleep):
        with patch("anthropic.Anthropic") as mock_sdk:
            mock_sdk.return_value.messages.create.return_value = _ok_response()
            client = AnthropicClient(api_key="k", model="m", retry=2)
            client.chat(messages=[{"role": "user", "content": "x"}])
            _no_sleep.assert_not_called()
