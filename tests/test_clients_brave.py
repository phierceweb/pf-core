"""Tests for pf_core.clients.brave."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from pf_core.clients.brave import (
    BraveSearchClient,
    BraveSearchError,
    get_client,
    reset_client,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_client()
    yield
    reset_client()


class TestClientConstruction:
    def test_empty_api_key_raises(self):
        with pytest.raises(BraveSearchError, match="BRAVE_API_KEY"):
            BraveSearchClient(api_key="")

    def test_explicit_args_override_defaults(self):
        c = BraveSearchClient(
            api_key="k",
            base_url="https://example.com/v1/",
            request_timeout=10,
            cost_per_call_usd=0.01,
        )
        assert c.api_key == "k"
        assert c.base_url == "https://example.com/v1"  # trailing slash stripped
        assert c.request_timeout == 10
        assert c.cost_per_call_usd == 0.01

    def test_headers_include_subscription_token(self):
        c = BraveSearchClient(api_key="my-token")
        h = c._headers()
        assert h["X-Subscription-Token"] == "my-token"
        assert h["Accept"] == "application/json"


class TestSearchEmptyQuery:
    def test_empty_query_raises(self):
        c = BraveSearchClient(api_key="k")
        with pytest.raises(BraveSearchError, match="empty query"):
            c.search("")

    def test_whitespace_only_raises(self):
        c = BraveSearchClient(api_key="k")
        with pytest.raises(BraveSearchError, match="empty query"):
            c.search("   ")


def _mock_response(*, status_code=200, json_data=None, text=""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = text
    return resp


class TestSearchSuccess:
    def test_normalizes_results(self):
        c = BraveSearchClient(api_key="k", cost_per_call_usd=0.005)
        json_data = {
            "web": {
                "results": [
                    {
                        "url": "https://example.com/article/abc",
                        "title": "Product launch announced",
                        "description": "The product shipped to customers...",
                        "age": "2 days ago",
                        "page_age": "2026-04-13T12:00:00",
                    },
                    {
                        "url": "https://example.org/x",
                        "title": "Follow-up coverage",
                        "description": "",
                    },
                ],
            },
        }
        with patch("httpx.get", return_value=_mock_response(
            status_code=200, json_data=json_data,
        )):
            results, usage = c.search("product launch")
        assert len(results) == 2
        assert results[0]["url"] == "https://example.com/article/abc"
        assert results[0]["title"] == "Product launch announced"
        assert results[0]["age"] == "2 days ago"
        assert results[1]["age"] is None  # missing key → None
        assert usage["cost_usd"] == 0.005
        assert usage["prompt_tokens"] == 0

    def test_empty_results_returns_empty_list(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=200, json_data={"web": {"results": []}},
        )):
            results, usage = c.search("very narrow query")
        assert results == []
        assert "duration_ms" in usage

    def test_count_clamped_to_brave_max(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=200, json_data={"web": {"results": []}},
        )) as get_mock:
            c.search("q", count=99)
        params = get_mock.call_args.kwargs["params"]
        assert params["count"] == 20

    def test_count_clamped_to_one_minimum(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=200, json_data={"web": {"results": []}},
        )) as get_mock:
            c.search("q", count=0)
        params = get_mock.call_args.kwargs["params"]
        assert params["count"] == 1

    def test_freshness_param_passed_through(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=200, json_data={"web": {"results": []}},
        )) as get_mock:
            c.search("q", freshness="pw")
        params = get_mock.call_args.kwargs["params"]
        assert params["freshness"] == "pw"

    def test_extra_params_merged(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=200, json_data={"web": {"results": []}},
        )) as get_mock:
            c.search("q", extra_params={"spellcheck": "0"})
        params = get_mock.call_args.kwargs["params"]
        assert params["spellcheck"] == "0"

    def test_skips_non_dict_results(self):
        c = BraveSearchClient(api_key="k")
        json_data = {"web": {"results": [
            "garbage_string",
            {"url": "https://ok.com/x", "title": "ok"},
            {"title": "no url"},
            {"url": "", "title": "empty url"},
        ]}}
        with patch("httpx.get", return_value=_mock_response(
            status_code=200, json_data=json_data,
        )):
            results, _ = c.search("q")
        assert len(results) == 1
        assert results[0]["url"] == "https://ok.com/x"


class TestSearchErrors:
    def test_429_raises_rate_limited(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=429, text="Too Many Requests",
        )):
            with pytest.raises(BraveSearchError, match="rate-limited"):
                c.search("q")

    def test_401_raises_auth_failed(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=401, text="Unauthorized",
        )):
            with pytest.raises(BraveSearchError, match="auth failed"):
                c.search("q")

    def test_403_raises_auth_failed(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=403, text="Forbidden",
        )):
            with pytest.raises(BraveSearchError, match="auth failed"):
                c.search("q")

    def test_500_raises_with_status(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", return_value=_mock_response(
            status_code=500, text="Server Error",
        )):
            with pytest.raises(BraveSearchError, match="500"):
                c.search("q")

    def test_timeout_raises(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
            with pytest.raises(BraveSearchError, match="timed out"):
                c.search("q")

    def test_transport_error_raises(self):
        c = BraveSearchClient(api_key="k")
        with patch("httpx.get", side_effect=httpx.ConnectError("DNS")):
            with pytest.raises(BraveSearchError, match="transport error"):
                c.search("q")

    def test_non_json_response_raises(self):
        c = BraveSearchClient(api_key="k")
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json = MagicMock(side_effect=ValueError("not json"))
        resp.text = "<html>broken</html>"
        with patch("httpx.get", return_value=resp):
            with pytest.raises(BraveSearchError, match="non-JSON"):
                c.search("q")


class TestGetClientSingleton:
    def test_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        c1 = get_client()
        c2 = get_client()
        assert c1 is c2

    def test_reset_creates_new_instance(self, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        c1 = get_client()
        reset_client()
        c2 = get_client()
        assert c1 is not c2

    def test_explicit_api_key_used(self):
        c = get_client(api_key="explicit")
        assert c.api_key == "explicit"

    def test_missing_env_key_raises(self, monkeypatch):
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        with pytest.raises(BraveSearchError, match="BRAVE_API_KEY"):
            get_client()

    def test_env_cost_override(self, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        monkeypatch.setenv("BRAVE_COST_PER_CALL_USD", "0.01")
        c = get_client()
        assert c.cost_per_call_usd == 0.01
