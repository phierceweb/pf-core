"""Tests for pf_core.clients.routing.get_routed_client."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pf_core.clients import claude_code, openrouter
from pf_core.clients.routing import get_routed_client


@pytest.fixture(autouse=True)
def _reset():
    """Both backends are singletons; reset both before/after each test."""
    claude_code.reset_client()
    openrouter.reset_client()
    yield
    claude_code.reset_client()
    openrouter.reset_client()


class TestRouting:
    def test_false_routes_to_openrouter(self, monkeypatch):
        """Default path: use_claude_code=False returns the OpenRouter
        singleton."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        client = get_routed_client(False)
        assert isinstance(client, openrouter.OpenRouterClient)

    def test_true_routes_to_claude_code(self):
        """Opt-in path: use_claude_code=True returns the Claude Code
        singleton."""
        client = get_routed_client(True)
        assert isinstance(client, claude_code.ClaudeCodeClient)

    def test_returns_singleton_on_repeat(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        a = get_routed_client(False)
        b = get_routed_client(False)
        assert a is b

    def test_routing_can_alternate(self, monkeypatch):
        """Successive calls with opposite flags return the right backend
        each time — the singletons live independently."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        or_client = get_routed_client(False)
        cc_client = get_routed_client(True)
        or_again = get_routed_client(False)
        assert or_client is or_again
        assert isinstance(cc_client, claude_code.ClaudeCodeClient)
        assert or_client is not cc_client


class TestLazyImport:
    """The routing helper imports claude_code lazily so consumers that
    never opt in don't need the ``claude`` CLI installed (or even the
    Python module imported)."""

    def test_false_path_does_not_import_claude_code(self, monkeypatch):
        """If use_claude_code=False is passed, the function must not
        touch pf_core.clients.claude_code.

        We detect this by patching ``claude_code.get_client`` to fail
        loudly if called.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        with patch.object(
            claude_code, "get_client",
            side_effect=AssertionError("claude_code.get_client should not run"),
        ):
            client = get_routed_client(False)
        assert isinstance(client, openrouter.OpenRouterClient)
