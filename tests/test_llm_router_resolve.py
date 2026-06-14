"""Tests for pf_core.llm.router.resolve_agent — YAML-routed client acquisition.

Uses fake backends registered through pf_core.clients.routing.register_client
so no real transport (or API key) is involved.
"""

from __future__ import annotations

import pytest

from pf_core.clients.routing import register_client, unregister_client
from pf_core.exceptions import ConfigurationError
from pf_core.llm.router import (
    ResolvedAgent,
    call_with_fallback,
    clear_cache,
    resolve_agent,
    resolve_agent_candidates,
)


class _FakeClient:
    def __init__(
        self,
        name: str,
        *,
        preflight_ok: bool = True,
        chat_exc: Exception | None = None,
        **kwargs,
    ):
        self.name = name
        self.kwargs = kwargs
        self.preflight_ok = preflight_ok
        self.preflight_calls = 0
        self.chat_exc = chat_exc
        self.chat_calls: list[dict] = []

    def preflight(self):
        self.preflight_calls += 1
        if not self.preflight_ok:
            raise RuntimeError(f"{self.name} unavailable")

    def chat(self, messages, model, **kwargs):
        self.chat_calls.append({"model": model, **kwargs})
        if self.chat_exc is not None:
            raise self.chat_exc
        return f"ok-{self.name}", {"model": model}


class _Factory:
    """Records constructions; can be told to fail or build dead clients."""

    def __init__(self, name: str, *, construct_ok: bool = True, preflight_ok: bool = True):
        self.name = name
        self.construct_ok = construct_ok
        self.preflight_ok = preflight_ok
        self.chat_exc: Exception | None = None
        self.calls: list[dict] = []
        self.instances: list[_FakeClient] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.construct_ok:
            raise RuntimeError(f"cannot construct {self.name}")
        client = _FakeClient(
            self.name, preflight_ok=self.preflight_ok, chat_exc=self.chat_exc, **kwargs
        )
        self.instances.append(client)
        return client


@pytest.fixture()
def fakes():
    factories = {name: _Factory(name) for name in ("fake_a", "fake_b")}
    for name, factory in factories.items():
        register_client(name, factory)
    yield factories
    for name in factories:
        unregister_client(name)


@pytest.fixture(autouse=True)
def _reset_router_cache():
    clear_cache()
    yield
    clear_cache()


ROUTED_YAML = """\
env_prefix: TESTPROJ
agents:
  routed:
    default_backend: fake_a
    fallback: true
    temperature: 0.2
    backends:
      fake_a: {model: model-a, client_kwargs: {retry: 2}}
      fake_b: {model: model-b}
  pinned:
    default_backend: fake_a
    backends:
      fake_a: {model: model-a}
      fake_b: {model: model-b}
"""

FLAT_YAML = """\
default_client: fake_a
agents:
  flat_agent:
    model: flat-model-1
    temperature: 0.1
"""


def _point_at(tmp_path, monkeypatch, body: str):
    path = tmp_path / "model_router.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(path))
    monkeypatch.delenv("TESTPROJ_ROUTED_BACKEND", raising=False)
    monkeypatch.delenv("TESTPROJ_PINNED_BACKEND", raising=False)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_resolve_agent_returns_client_kwargs_and_backend(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    resolved = resolve_agent("routed")

    assert isinstance(resolved, ResolvedAgent)
    client, chat_kwargs, backend = resolved
    assert client.name == "fake_a"
    assert backend == "fake_a"
    assert chat_kwargs == {"model": "model-a", "temperature": 0.2}


def test_resolve_agent_passes_client_kwargs_to_factory(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    resolve_agent("routed")

    assert fakes["fake_a"].calls == [{"retry": 2}]


def test_resolve_agent_flat_agent_via_default_client(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, FLAT_YAML)

    client, chat_kwargs, backend = resolve_agent("flat_agent")

    assert client.name == "fake_a"
    assert backend == "fake_a"
    assert chat_kwargs == {"model": "flat-model-1", "temperature": 0.1}


def test_resolve_agent_model_override_wins(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    _, chat_kwargs, _ = resolve_agent("routed", model_override="model-x")

    assert chat_kwargs["model"] == "model-x"


def test_resolve_agent_explicit_backend_param_wins(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    client, chat_kwargs, backend = resolve_agent("routed", backend="fake_b")

    assert backend == "fake_b"
    assert chat_kwargs["model"] == "model-b"


# ---------------------------------------------------------------------------
# Fallback semantics — opt-in, availability-based, YAML-derived selection only
# ---------------------------------------------------------------------------


def test_fallback_skips_backend_that_fails_preflight(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].preflight_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    client, chat_kwargs, backend = resolve_agent("routed")

    assert backend == "fake_b"
    assert chat_kwargs["model"] == "model-b"


def test_fallback_skips_backend_whose_factory_raises(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].construct_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    client, _, backend = resolve_agent("routed")

    assert backend == "fake_b"


def test_fallback_preflights_candidates(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    resolve_agent("routed")

    assert fakes["fake_a"].instances[0].preflight_calls == 1


def test_no_fallback_means_no_preflight_and_errors_propagate(tmp_path, monkeypatch, fakes):
    """Without fallback: true, acquisition is direct — no availability scan,
    and a factory error reaches the caller unchanged."""
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    client, _, _ = resolve_agent("pinned")
    assert client.preflight_calls == 0

    fakes["fake_a"].construct_ok = False
    with pytest.raises(RuntimeError, match="cannot construct fake_a"):
        resolve_agent("pinned")


def test_explicit_backend_param_disables_fallback(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].preflight_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    client, _, backend = resolve_agent("routed", backend="fake_a")

    assert backend == "fake_a"
    assert client.preflight_calls == 0  # explicit choice -> no scan


def test_env_selected_backend_participates_in_fallback(tmp_path, monkeypatch, fakes):
    """An env-pinned backend still benefits from the availability scan when
    the agent opted into fallback — 'ops pinned us to claude_code, but skip
    it if the CLI is missing'. The env choice goes first in the chain."""
    fakes["fake_a"].preflight_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)
    monkeypatch.setenv("TESTPROJ_ROUTED_BACKEND", "fake_a")

    client, _, backend = resolve_agent("routed")

    assert backend == "fake_b"


def test_env_selected_backend_without_fallback_is_deterministic(tmp_path, monkeypatch, fakes):
    """Without fallback: true, env selection stays direct — no scan."""
    fakes["fake_a"].preflight_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)
    monkeypatch.setenv("TESTPROJ_PINNED_BACKEND", "fake_a")

    client, _, backend = resolve_agent("pinned")

    assert backend == "fake_a"
    assert client.preflight_calls == 0


def test_env_choice_goes_first_in_fallback_chain(tmp_path, monkeypatch, fakes):
    """Env override reorders the chain: its choice is tried before the
    YAML default_backend."""
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)
    monkeypatch.setenv("TESTPROJ_ROUTED_BACKEND", "fake_b")

    client, chat_kwargs, backend = resolve_agent("routed")

    assert backend == "fake_b"
    assert chat_kwargs["model"] == "model-b"


def test_fallback_exhausted_raises_configuration_error(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].preflight_ok = False
    fakes["fake_b"].construct_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    with pytest.raises(ConfigurationError, match="fake_a.*fake_b"):
        resolve_agent("routed")


# ---------------------------------------------------------------------------
# Hard failures
# ---------------------------------------------------------------------------


def test_unknown_backend_name_raises(tmp_path, monkeypatch, fakes):
    """A YAML backend key with no registered client is a config error."""
    body = """\
agents:
  routed:
    default_backend: no_such_client
    backends:
      no_such_client: {model: m}
"""
    _point_at(tmp_path, monkeypatch, body)

    with pytest.raises(ConfigurationError, match="no_such_client"):
        resolve_agent("routed")


def test_flat_agent_without_default_client_hard_fails(tmp_path, monkeypatch, fakes):
    body = """\
agents:
  flat_agent:
    model: flat-model-1
"""
    _point_at(tmp_path, monkeypatch, body)

    with pytest.raises(ConfigurationError, match="flat_agent"):
        resolve_agent("flat_agent")


# ---------------------------------------------------------------------------
# Call-failure fallback — resolve_agent_candidates + call_with_fallback
# ---------------------------------------------------------------------------


def test_candidates_yields_chain_in_order_with_per_backend_models(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    candidates = list(resolve_agent_candidates("routed"))

    assert [c.backend for c in candidates] == ["fake_a", "fake_b"]
    assert [c.chat_kwargs["model"] for c in candidates] == ["model-a", "model-b"]


def test_candidates_skips_construction_failures_lazily(tmp_path, monkeypatch, fakes):
    fakes["fake_b"].construct_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    candidates = list(resolve_agent_candidates("routed"))

    assert [c.backend for c in candidates] == ["fake_a"]


def test_candidates_does_not_preflight(tmp_path, monkeypatch, fakes):
    """The chat call itself is the probe in the call-fallback path —
    preflight is only for resolve_agent's acquisition-time scan."""
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    candidates = list(resolve_agent_candidates("routed"))

    assert all(c.client.preflight_calls == 0 for c in candidates)


def test_candidates_without_fallback_yields_single(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    candidates = list(resolve_agent_candidates("pinned"))

    assert [c.backend for c in candidates] == ["fake_a"]


def test_candidates_applies_model_override_to_every_candidate(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    candidates = list(resolve_agent_candidates("routed", model_override="model-x"))

    assert [c.chat_kwargs["model"] for c in candidates] == ["model-x", "model-x"]


def test_call_with_fallback_returns_first_success(tmp_path, monkeypatch, fakes):
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    content, usage, resolved = call_with_fallback("routed", [{"role": "user", "content": "hi"}])

    assert content == "ok-fake_a"
    assert resolved.backend == "fake_a"
    assert usage == {"model": "model-a"}


def test_call_with_fallback_moves_on_when_chat_raises(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].chat_exc = RuntimeError("boom-a")
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    content, usage, resolved = call_with_fallback("routed", [{"role": "user", "content": "hi"}])

    assert content == "ok-fake_b"
    assert resolved.backend == "fake_b"
    # The failed attempt actually called chat with its own backend's model.
    assert fakes["fake_a"].instances[0].chat_calls[0]["model"] == "model-a"
    # No preflight anywhere — the call is the probe.
    assert fakes["fake_a"].instances[0].preflight_calls == 0


def test_call_with_fallback_respects_retry_on_filter(tmp_path, monkeypatch, fakes):
    """An exception outside retry_on propagates immediately — fake_b is
    never tried."""
    fakes["fake_a"].chat_exc = RuntimeError("boom-a")
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    with pytest.raises(RuntimeError, match="boom-a"):
        call_with_fallback(
            "routed", [{"role": "user", "content": "hi"}], retry_on=(ValueError,)
        )

    assert fakes["fake_b"].instances == []


def test_call_with_fallback_reraises_last_error_when_exhausted(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].chat_exc = RuntimeError("boom-a")
    fakes["fake_b"].chat_exc = RuntimeError("boom-b")
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    with pytest.raises(RuntimeError, match="boom-b"):
        call_with_fallback("routed", [{"role": "user", "content": "hi"}])


def test_call_with_fallback_without_fallback_flag_is_single_attempt(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].chat_exc = RuntimeError("boom-a")
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    with pytest.raises(RuntimeError, match="boom-a"):
        call_with_fallback("pinned", [{"role": "user", "content": "hi"}])

    assert fakes["fake_b"].instances == []


def test_call_with_fallback_all_unconstructable_raises_config_error(tmp_path, monkeypatch, fakes):
    fakes["fake_a"].construct_ok = False
    fakes["fake_b"].construct_ok = False
    _point_at(tmp_path, monkeypatch, ROUTED_YAML)

    with pytest.raises(ConfigurationError, match="fake_a.*fake_b"):
        call_with_fallback("routed", [{"role": "user", "content": "hi"}])
