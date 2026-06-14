"""Tests for pf_core.llm.router — nested per-backend agent blocks.

The nested schema (absorbed from the consumer projects that built it
independently) lets one agent declare a model per backend::

    agents:
      drafter:
        default_backend: openrouter
        temperature: 0.3
        backends:
          openrouter:  {model: anthropic/claude-sonnet-4.6}
          claude_code: {model: sonnet}

Flat blocks (``model`` at the top of the agent) remain the single-backend
shorthand and are covered by test_llm_router.py.
"""

from __future__ import annotations

import pytest

from pf_core.exceptions import ConfigurationError
from pf_core.llm.router import (
    clear_cache,
    get_agent_block,
    get_agent_config,
    list_agents,
    resolve_backend,
)


@pytest.fixture(autouse=True)
def _reset_router_cache():
    clear_cache()
    yield
    clear_cache()


NESTED_YAML = """\
default_client: openrouter
env_prefix: TESTPROJ
non_chat_keys: [max_input_tokens]
agents:
  flat_agent:
    model: flat-model-1
    temperature: 0.1
  drafter:
    default_backend: claude_code
    temperature: 0.3
    max_tokens: 4000
    max_input_tokens: 800000
    backends:
      openrouter:  {model: anthropic/claude-sonnet-4.6}
      claude_code: {model: sonnet, max_tokens: 8000, client_kwargs: {retry: 2}}
      anthropic:   {model: claude-sonnet-4-6}
  searcher:
    backends:
      openrouter: {model: perplexity/sonar-pro}
"""


def _point_at(tmp_path, monkeypatch, body: str = NESTED_YAML):
    path = tmp_path / "model_router.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(path))
    monkeypatch.delenv("TESTPROJ_DRAFTER_BACKEND", raising=False)
    monkeypatch.delenv("TESTPROJ_SEARCHER_BACKEND", raising=False)
    return path


# ---------------------------------------------------------------------------
# get_agent_config on nested blocks
# ---------------------------------------------------------------------------


def test_nested_config_merges_shared_sampling_with_backend_block(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    cfg = get_agent_config("drafter")  # default_backend: claude_code

    assert cfg["model"] == "sonnet"
    assert cfg["temperature"] == 0.3  # agent-wide
    assert cfg["max_tokens"] == 8000  # backend override beats agent-wide 4000


def test_nested_config_explicit_backend_param(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    cfg = get_agent_config("drafter", backend="anthropic")

    assert cfg["model"] == "claude-sonnet-4-6"
    assert cfg["max_tokens"] == 4000  # agent-wide, no backend override


def test_nested_config_strips_structural_and_client_keys(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    cfg = get_agent_config("drafter")

    for key in ("backends", "default_backend", "fallback", "client_kwargs"):
        assert key not in cfg


def test_nested_config_strips_declared_non_chat_keys(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    cfg = get_agent_config("drafter")

    assert "max_input_tokens" not in cfg


def test_get_agent_block_returns_raw_block_with_non_chat_keys(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    block = get_agent_block("drafter")

    assert block["max_input_tokens"] == 800000
    assert "backends" in block


def test_model_override_wins_on_nested(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    cfg = get_agent_config("drafter", model_override="opus")

    assert cfg["model"] == "opus"


def test_model_override_wins_on_flat(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    cfg = get_agent_config("flat_agent", model_override="other-model")

    assert cfg["model"] == "other-model"
    assert cfg["temperature"] == 0.1


def test_undeclared_backend_param_raises(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    with pytest.raises(ConfigurationError, match="searcher.*openrouter"):
        get_agent_config("searcher", backend="anthropic")


def test_flat_and_nested_coexist(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    assert get_agent_config("flat_agent")["model"] == "flat-model-1"
    assert list_agents() == ["drafter", "flat_agent", "searcher"]


# ---------------------------------------------------------------------------
# resolve_backend precedence
# ---------------------------------------------------------------------------


def test_resolve_backend_uses_agent_default_backend(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    assert resolve_backend("drafter") == "claude_code"


def test_resolve_backend_env_override_beats_yaml(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    monkeypatch.setenv("TESTPROJ_DRAFTER_BACKEND", "anthropic")

    assert resolve_backend("drafter") == "anthropic"


def test_resolve_backend_env_undeclared_value_falls_through(tmp_path, monkeypatch):
    """An env value naming a backend the agent doesn't declare is ignored
    (ops resilience — same behavior the consumer implementations chose)."""
    _point_at(tmp_path, monkeypatch)
    monkeypatch.setenv("TESTPROJ_DRAFTER_BACKEND", "no_such_backend")

    assert resolve_backend("drafter") == "claude_code"


def test_resolve_backend_falls_to_default_client(tmp_path, monkeypatch):
    """No default_backend on the agent -> top-level default_client (if the
    agent declares that backend)."""
    _point_at(tmp_path, monkeypatch)

    assert resolve_backend("searcher") == "openrouter"


def test_resolve_backend_flat_agent_uses_default_client(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)

    assert resolve_backend("flat_agent") == "openrouter"


def test_resolve_backend_hard_fails_when_nothing_resolves(tmp_path, monkeypatch):
    """No env, no default_backend, no default_client -> ConfigurationError.
    There is deliberately no framework-hardcoded fallback backend."""
    body = """\
agents:
  drafter:
    backends:
      openrouter: {model: m1}
      anthropic: {model: m2}
"""
    _point_at(tmp_path, monkeypatch, body)

    with pytest.raises(ConfigurationError, match="drafter"):
        resolve_backend("drafter")


def test_resolve_backend_no_env_tier_without_env_prefix(tmp_path, monkeypatch):
    """Without a top-level env_prefix, env vars are not consulted."""
    body = """\
agents:
  drafter:
    default_backend: openrouter
    backends:
      openrouter: {model: m1}
      anthropic: {model: m2}
"""
    _point_at(tmp_path, monkeypatch, body)
    monkeypatch.setenv("TESTPROJ_DRAFTER_BACKEND", "anthropic")

    assert resolve_backend("drafter") == "openrouter"


def test_single_declared_backend_is_implicit_default(tmp_path, monkeypatch):
    """An agent declaring exactly one backend routes there without needing
    default_backend/default_client."""
    body = """\
agents:
  solo:
    backends:
      anthropic: {model: m2}
"""
    _point_at(tmp_path, monkeypatch, body)

    assert resolve_backend("solo") == "anthropic"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_nested_agent_with_empty_backends_raises(tmp_path, monkeypatch):
    body = """\
agents:
  bad:
    backends: {}
"""
    _point_at(tmp_path, monkeypatch, body)

    with pytest.raises(ConfigurationError, match="bad"):
        list_agents()


def test_backend_block_missing_model_raises(tmp_path, monkeypatch):
    body = """\
agents:
  bad:
    backends:
      openrouter: {temperature: 0.1}
"""
    _point_at(tmp_path, monkeypatch, body)

    with pytest.raises(ConfigurationError, match="bad.*openrouter"):
        list_agents()


def test_default_backend_must_be_declared(tmp_path, monkeypatch):
    body = """\
agents:
  bad:
    default_backend: anthropic
    backends:
      openrouter: {model: m1}
"""
    _point_at(tmp_path, monkeypatch, body)

    with pytest.raises(ConfigurationError, match="bad.*anthropic"):
        list_agents()


def test_agent_with_neither_model_nor_backends_raises(tmp_path, monkeypatch):
    body = """\
agents:
  bad:
    temperature: 0.1
"""
    _point_at(tmp_path, monkeypatch, body)

    with pytest.raises(ConfigurationError, match="bad"):
        list_agents()
