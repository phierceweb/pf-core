"""Tests for the client registry in pf_core.clients.routing.

The registry is the extension point that lets the model router's YAML
``backends:`` keys name consumer-provided clients (ollama, openai-direct, …)
alongside the built-in three.
"""

from __future__ import annotations

import pytest

from pf_core.clients import claude_code, openrouter
from pf_core.clients.routing import (
    BUILTIN_BACKENDS,
    clear_client_cache,
    get_client_for_backend,
    get_routed_client,
    register_client,
    registered_backends,
    unregister_client,
)
from pf_core.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def _reset_singletons():
    claude_code.reset_client()
    openrouter.reset_client()
    clear_client_cache()
    yield
    claude_code.reset_client()
    openrouter.reset_client()
    clear_client_cache()


class _Fake:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def chat(self, messages, model, **kwargs):
        return "ok", {}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_register_and_resolve_custom_backend():
    register_client("fake", _Fake)
    try:
        client = get_client_for_backend("fake", retry=3)
        assert isinstance(client, _Fake)
        assert client.kwargs == {"retry": 3}
    finally:
        unregister_client("fake")


def test_registered_backends_lists_builtins_and_customs():
    assert set(BUILTIN_BACKENDS) <= set(registered_backends())
    register_client("fake", _Fake)
    try:
        assert "fake" in registered_backends()
    finally:
        unregister_client("fake")
    assert "fake" not in registered_backends()


def test_custom_registration_overrides_builtin_name():
    register_client("openrouter", _Fake)
    try:
        client = get_client_for_backend("openrouter")
        assert isinstance(client, _Fake)
    finally:
        unregister_client("openrouter")


def test_unknown_backend_raises_with_known_names():
    with pytest.raises(ConfigurationError, match="openrouter"):
        get_client_for_backend("nope")


def test_unregister_unknown_raises():
    with pytest.raises(ConfigurationError):
        unregister_client("never_registered")


def test_builtin_resolves_to_real_singleton(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = get_client_for_backend("openrouter")
    assert isinstance(client, openrouter.OpenRouterClient)
    assert client is openrouter.get_client()


# ---------------------------------------------------------------------------
# client_kwargs vs singletons — per-signature instances for built-ins
# ---------------------------------------------------------------------------


def test_builtin_with_kwargs_gets_distinct_instances_per_signature():
    """Two agents tuning the same built-in differently must not collide on
    the module singleton — distinct client_kwargs get distinct instances."""
    a = get_client_for_backend("claude_code", retry=2)
    b = get_client_for_backend("claude_code", retry=5)

    assert a is not b
    assert a.retry == 2
    assert b.retry == 5


def test_builtin_with_same_kwargs_is_cached():
    """Same signature -> same instance; resolve_agent in a loop must not
    construct a fresh client per call."""
    a = get_client_for_backend("claude_code", retry=2)
    b = get_client_for_backend("claude_code", retry=2)

    assert a is b


def test_builtin_without_kwargs_is_the_module_singleton(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    client = get_client_for_backend("openrouter")

    assert client is openrouter.get_client()


def test_kwargs_instances_do_not_poison_the_singleton():
    """Acquiring a tuned instance first must not become the kwargs-less
    singleton afterwards (the documented first-call-wins trap)."""
    tuned = get_client_for_backend("claude_code", retry=7)
    plain = get_client_for_backend("claude_code")

    assert plain is not tuned
    assert plain.retry == 0


def test_clear_client_cache_drops_signature_instances():
    a = get_client_for_backend("claude_code", retry=2)
    clear_client_cache()
    b = get_client_for_backend("claude_code", retry=2)

    assert a is not b


def test_custom_factories_are_not_cached_by_the_registry():
    """Custom factories own their lifecycle — the registry calls them
    every time, kwargs or not."""
    calls = []

    def factory(**kw):
        calls.append(kw)
        return _Fake(**kw)

    register_client("fake", factory)
    try:
        a = get_client_for_backend("fake", retry=1)
        b = get_client_for_backend("fake", retry=1)
        assert a is not b
        assert calls == [{"retry": 1}, {"retry": 1}]
    finally:
        unregister_client("fake")


def test_new_client_returns_fresh_env_resolved_instances(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    a = openrouter.new_client(retry=1)
    b = openrouter.new_client(retry=1)

    assert a is not b
    assert a.retry == 1
    assert a.api_key == "test-key"
    assert a is not openrouter.get_client()


# ---------------------------------------------------------------------------
# Deprecated boolean shim
# ---------------------------------------------------------------------------


def test_get_routed_client_warns_and_still_routes(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    with pytest.warns(DeprecationWarning, match="resolve_agent"):
        client = get_routed_client(False)
    assert isinstance(client, openrouter.OpenRouterClient)

    with pytest.warns(DeprecationWarning):
        client = get_routed_client(True)
    assert isinstance(client, claude_code.ClaudeCodeClient)
