"""Tests for pf_core.llm.router — per-agent model/sampling YAML loader."""

from __future__ import annotations

import pytest

from pf_core.exceptions import ConfigurationError
from pf_core.llm.router import (
    assert_agents_registered,
    clear_cache,
    get_agent_config,
    list_agents,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_router_cache():
    clear_cache()
    yield
    clear_cache()


VALID_YAML = """\
agents:
  drafter:
    model: claude-opus-4-7
    temperature: 0.3
    max_tokens: 4000
  grader:
    model: claude-sonnet-4-6
    temperature: 0.0
"""


def _write_yaml(tmp_path, body: str):
    path = tmp_path / "model_router.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _point_env_at(monkeypatch, path, *, ttl: str | None = None):
    monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(path))
    if ttl is not None:
        monkeypatch.setenv("MODEL_ROUTER_RELOAD_SECONDS", ttl)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_get_agent_config_returns_model_and_sampling(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path)

    cfg = get_agent_config("drafter")

    assert cfg["model"] == "claude-opus-4-7"
    assert cfg["temperature"] == 0.3
    assert cfg["max_tokens"] == 4000


def test_list_agents_returns_sorted_slugs(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path)

    assert list_agents() == ["drafter", "grader"]


def test_assert_agents_registered_passes_when_all_present(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path)

    # Should not raise
    assert_agents_registered(["drafter", "grader"])


def test_assert_agents_registered_raises_and_names_missing(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError) as exc_info:
        assert_agents_registered(["drafter", "reviewer", "summarizer"])

    msg = str(exc_info.value)
    assert "reviewer" in msg
    assert "summarizer" in msg
    # Present slugs should not be listed as missing
    assert "drafter" not in msg.split("missing from")[-1]


def test_sampling_passthrough_preserves_custom_keys(tmp_path, monkeypatch):
    body = """\
agents:
  custom:
    model: some-model
    temperature: 0.7
    max_tokens: 2048
    top_p: 0.95
    foo: bar
"""
    path = _write_yaml(tmp_path, body)
    _point_env_at(monkeypatch, path)

    cfg = get_agent_config("custom")

    assert cfg["model"] == "some-model"
    assert cfg["temperature"] == 0.7
    assert cfg["max_tokens"] == 2048
    assert cfg["top_p"] == 0.95
    assert cfg["foo"] == "bar"


# ---------------------------------------------------------------------------
# Error paths — file/parse level
# ---------------------------------------------------------------------------


def test_missing_file_raises_with_path(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist.yaml"
    _point_env_at(monkeypatch, missing)

    with pytest.raises(ConfigurationError) as exc_info:
        get_agent_config("drafter")

    assert str(missing) in str(exc_info.value)


def test_malformed_yaml_raises_with_path(tmp_path, monkeypatch):
    # Unclosed bracket / bad indentation that PyYAML rejects
    path = _write_yaml(tmp_path, "agents: [this is: not valid\n  :::\n")
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError) as exc_info:
        get_agent_config("drafter")

    assert str(path) in str(exc_info.value)


def test_top_level_not_a_mapping_raises(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, "- just\n- a\n- list\n")
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError, match="mapping at the top level"):
        get_agent_config("drafter")


def test_missing_agents_section_raises(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, "other_key: value\n")
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError, match="missing required 'agents' section"):
        get_agent_config("drafter")


def test_agents_not_a_mapping_raises(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, "agents:\n  - drafter\n  - grader\n")
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError, match="'agents' must be a mapping"):
        get_agent_config("drafter")


# ---------------------------------------------------------------------------
# Error paths — per-agent validation
# ---------------------------------------------------------------------------


def test_agent_entry_not_a_dict_raises(tmp_path, monkeypatch):
    body = """\
agents:
  drafter: just-a-string
"""
    path = _write_yaml(tmp_path, body)
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError, match="drafter"):
        get_agent_config("drafter")


def test_agent_missing_model_key_raises_naming_slug(tmp_path, monkeypatch):
    body = """\
agents:
  drafter:
    temperature: 0.3
"""
    path = _write_yaml(tmp_path, body)
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError) as exc_info:
        get_agent_config("drafter")

    msg = str(exc_info.value)
    assert "drafter" in msg
    assert "model" in msg


def test_agent_with_empty_model_string_raises(tmp_path, monkeypatch):
    body = """\
agents:
  drafter:
    model: ""
"""
    path = _write_yaml(tmp_path, body)
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError, match="non-empty string"):
        get_agent_config("drafter")


# ---------------------------------------------------------------------------
# Error paths — lookup level
# ---------------------------------------------------------------------------


def test_unknown_slug_raises_with_slug_and_path(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError) as exc_info:
        get_agent_config("nonexistent")

    msg = str(exc_info.value)
    assert "nonexistent" in msg
    assert str(path) in msg


def test_empty_slug_raises(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path)

    with pytest.raises(ConfigurationError, match="slug is required"):
        get_agent_config("")


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_ttl_cache_holds_until_expiry(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path, ttl="60")

    first = get_agent_config("drafter")
    assert first["model"] == "claude-opus-4-7"

    # Mutate the file — but within TTL, cache should win
    new_body = """\
agents:
  drafter:
    model: completely-different-model
"""
    path.write_text(new_body, encoding="utf-8")

    second = get_agent_config("drafter")
    assert second["model"] == "claude-opus-4-7"  # still cached


def test_ttl_zero_rereads_on_every_call(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path, ttl="0")

    first = get_agent_config("drafter")
    assert first["model"] == "claude-opus-4-7"

    new_body = """\
agents:
  drafter:
    model: fresh-model
"""
    path.write_text(new_body, encoding="utf-8")

    second = get_agent_config("drafter")
    assert second["model"] == "fresh-model"


def test_clear_cache_forces_reread(tmp_path, monkeypatch):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path, ttl="60")

    first = get_agent_config("drafter")
    assert first["model"] == "claude-opus-4-7"

    new_body = """\
agents:
  drafter:
    model: post-clear-model
"""
    path.write_text(new_body, encoding="utf-8")

    # Without clear, cache still holds
    assert get_agent_config("drafter")["model"] == "claude-opus-4-7"

    clear_cache()

    assert get_agent_config("drafter")["model"] == "post-clear-model"


def test_reload_failure_falls_back_to_cached_config(tmp_path, monkeypatch, caplog):
    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path, ttl="0")  # always re-read

    # Populate the cache with a successful read
    first = get_agent_config("drafter")
    assert first["model"] == "claude-opus-4-7"

    # Now corrupt the file — next call should log a warning and return cached
    path.write_text("agents: [this is: not valid\n  :::\n", encoding="utf-8")

    second = get_agent_config("drafter")
    assert second["model"] == "claude-opus-4-7"  # cached fallback
    assert list_agents() == ["drafter", "grader"]


def test_reload_failure_with_no_prior_cache_raises(tmp_path, monkeypatch):
    # No prior successful load — broken file should raise, not silently fall back.
    path = _write_yaml(tmp_path, "agents: [this is: not valid\n  :::\n")
    _point_env_at(monkeypatch, path, ttl="0")

    with pytest.raises(ConfigurationError):
        get_agent_config("drafter")


def test_clear_cache_resets_module_state(tmp_path, monkeypatch):
    # Cache state lives in the loader module (router re-exports clear_cache).
    from pf_core.llm import _router_loader as loader

    path = _write_yaml(tmp_path, VALID_YAML)
    _point_env_at(monkeypatch, path)

    get_agent_config("drafter")
    assert loader._cache is not None

    clear_cache()

    assert loader._cache is None
    assert loader._cache_path is None
    assert loader._cache_loaded_at == 0.0
