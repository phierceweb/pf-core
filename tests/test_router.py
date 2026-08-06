"""Tests for pf_core.llm.router fallback safety — which exceptions may burn a
second (paid) backend.

Fake backends are registered through pf_core.clients.routing.register_client so
no real transport or API key is involved.
"""

from __future__ import annotations

import pytest

from pf_core.budget.check import CostBudgetExceeded
from pf_core.clients.routing import register_client, unregister_client
from pf_core.exceptions import ConfigurationError, FlowException, InvalidInputError
from pf_core.llm.router import call_with_fallback, clear_cache


class _FakeClient:
    def __init__(self, name: str, *, chat_exc: Exception | None = None, **kwargs):
        self.name = name
        self.chat_exc = chat_exc
        self.chat_calls: list[dict] = []

    def chat(self, messages, model, **kwargs):
        self.chat_calls.append({"model": model, **kwargs})
        if self.chat_exc is not None:
            raise self.chat_exc
        return f"ok-{self.name}", {"model": model}


class _Factory:
    def __init__(self, name: str):
        self.name = name
        self.chat_exc: Exception | None = None
        self.instances: list[_FakeClient] = []

    def __call__(self, **kwargs):
        client = _FakeClient(self.name, chat_exc=self.chat_exc, **kwargs)
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
    backends:
      fake_a: {model: model-a}
      fake_b: {model: model-b}
"""


@pytest.fixture()
def routed(tmp_path, monkeypatch, fakes):
    path = tmp_path / "model_router.yaml"
    path.write_text(ROUTED_YAML, encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(path))
    monkeypatch.delenv("TESTPROJ_ROUTED_BACKEND", raising=False)
    return fakes


MSGS = [{"role": "user", "content": "hi"}]


def _budget_exc() -> CostBudgetExceeded:
    return CostBudgetExceeded(
        scope_kind="agent",
        scope_value="routed",
        period="daily",
        limit_usd=1.0,
        spent_usd=1.0,
        projected_usd=0.5,
    )


def test_budget_exceeded_does_not_burn_a_second_backend(routed):
    routed["fake_a"].chat_exc = _budget_exc()

    with pytest.raises(CostBudgetExceeded):
        call_with_fallback("routed", MSGS)

    assert routed["fake_b"].instances == []


def test_flow_exception_does_not_burn_a_second_backend(routed):
    routed["fake_a"].chat_exc = InvalidInputError("messages are malformed")

    with pytest.raises(InvalidInputError):
        call_with_fallback("routed", MSGS)

    assert routed["fake_b"].instances == []


def test_configuration_error_does_not_burn_a_second_backend(routed):
    routed["fake_a"].chat_exc = ConfigurationError("API key missing")

    with pytest.raises(ConfigurationError):
        call_with_fallback("routed", MSGS)

    assert routed["fake_b"].instances == []


def test_transport_error_still_falls_back(routed):
    routed["fake_a"].chat_exc = RuntimeError("connection reset")

    content, _, resolved = call_with_fallback("routed", MSGS)

    assert content == "ok-fake_b"
    assert resolved.backend == "fake_b"


def test_explicit_retry_on_is_honoured_as_given(routed):
    routed["fake_a"].chat_exc = InvalidInputError("nope")

    content, _, resolved = call_with_fallback("routed", MSGS, retry_on=(FlowException,))

    assert resolved.backend == "fake_b"
    assert content == "ok-fake_b"
