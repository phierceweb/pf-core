"""Tests for llm_step — cache → budget → tracked call → validate → store."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from pf_core.budget import CostBudgetExceeded
from pf_core.llm.validate import register


class Verdict(BaseModel):
    a: int


register(agent_type="step_probe", shape=Verdict)

MESSAGES = [
    {"role": "system", "content": "You are a classifier."},
    {"role": "user", "content": "Classify this."},
]


class FakeChat:
    def __init__(self, content: str = '{"a": 1}', *, raise_exc: Exception | None = None):
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, messages, model="", **kwargs):
        self.calls.append({"messages": messages, "model": model, **kwargs})
        if self.raise_exc:
            raise self.raise_exc
        return self.content, {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "cost_usd": 0.0001,
            "duration_ms": 10,
        }


@pytest.fixture
def pf_schema():
    from pf_core.testing.db_fixtures import framework_ddl

    return framework_ddl()


@pytest.fixture
def exact_cache(tmp_path, monkeypatch):
    from pf_core.llm.cache import clear_config_cache

    cfg = tmp_path / "cache.yaml"
    cfg.write_text("defaults:\n  exact: true\n  ttl_seconds: 3600\n")
    monkeypatch.setenv("CACHE_CONFIG", str(cfg))
    clear_config_cache()
    yield
    clear_config_cache()


def _runs(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            text("SELECT id, status, input_hash FROM llm_runs ORDER BY id")
        ).mappings()
    ]


class TestPlainCall:
    def test_returns_raw_content_and_records_one_run(self, pf_tables, pf_connection):
        from pf_core.llm.step import llm_step

        client = FakeChat(content="plain text")
        res = llm_step(client=client, agent_type="step_raw", messages=MESSAGES, model="m1")
        assert res.value == "plain text"
        assert res.content == "plain text"
        assert res.cache_hit is False
        assert res.validation is None
        assert isinstance(res.run_id, int)
        runs = _runs(pf_connection)
        assert len(runs) == 1
        assert runs[0]["status"] == "success"

    def test_client_error_records_failed_row_and_reraises(self, pf_tables, pf_connection):
        from pf_core.llm.step import llm_step

        client = FakeChat(raise_exc=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            llm_step(client=client, agent_type="step_raw", messages=MESSAGES, model="m1")
        runs = _runs(pf_connection)
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"

    def test_explicit_input_hash_stamped_without_cache(self, pf_tables, pf_connection):
        from pf_core.llm.step import llm_step

        given = "ab" * 32
        llm_step(
            client=FakeChat(),
            agent_type="step_raw",
            messages=MESSAGES,
            model="m1",
            input_hash=given,
        )
        assert _runs(pf_connection)[0]["input_hash"] == given


class TestValidate:
    def test_valid_object_returns_model_value(self, pf_tables, pf_connection):
        from pf_core.llm.step import llm_step

        res = llm_step(
            client=FakeChat(),
            agent_type="step_probe",
            messages=MESSAGES,
            model="m1",
            validate="object",
        )
        assert res.validation is not None
        assert res.validation.ok
        assert res.value.a == 1

    def test_invalid_returns_not_raises(self, pf_tables, pf_connection):
        from pf_core.llm.step import llm_step

        res = llm_step(
            client=FakeChat(content='{"a": "not-an-int"}'),
            agent_type="step_probe",
            messages=MESSAGES,
            model="m1",
            validate="object",
        )
        assert res.validation is not None
        assert res.validation.ok is False
        assert res.value is None
        assert res.cache_hit is False


class TestCache:
    def test_miss_then_hit_short_circuits_chat_and_budget(
        self, pf_tables, pf_connection, exact_cache, monkeypatch
    ):
        import pf_core.llm.step as step_mod
        from pf_core.llm.step import BudgetEstimate, llm_step

        client = FakeChat()
        r1 = llm_step(
            client=client,
            agent_type="step_probe",
            messages=MESSAGES,
            model="m1",
            cache=True,
            validate="object",
        )
        assert r1.cache_hit is False
        assert len(client.calls) == 1

        def _fail_if_consulted(**kwargs):
            raise AssertionError("budget consulted on cache hit")

        monkeypatch.setattr(step_mod, "project_cost", _fail_if_consulted)
        r2 = llm_step(
            client=client,
            agent_type="step_probe",
            messages=MESSAGES,
            model="m1",
            cache=True,
            validate="object",
            budget=BudgetEstimate(),
        )
        assert r2.cache_hit is True
        assert len(client.calls) == 1
        assert r2.value.a == 1
        assert r2.run_id != r1.run_id
        statuses = [r["status"] for r in _runs(pf_connection)]
        assert statuses.count("success") == 1
        assert "cache_hit" in statuses

    def test_validate_none_hit_returns_raw(self, pf_tables, pf_connection, exact_cache):
        from pf_core.llm.step import llm_step

        client = FakeChat(content='{"a": 2}')
        r1 = llm_step(
            client=client,
            agent_type="step_raw",
            messages=MESSAGES,
            model="m1",
            cache=True,
        )
        r2 = llm_step(
            client=client,
            agent_type="step_raw",
            messages=MESSAGES,
            model="m1",
            cache=True,
        )
        assert r1.cache_hit is False
        assert r2.cache_hit is True
        assert len(client.calls) == 1
        assert r2.value == '{"a": 2}'
        assert r2.content == '{"a": 2}'

    def test_no_store_on_validation_failure(self, pf_tables, pf_connection, exact_cache):
        from pf_core.llm.step import llm_step

        client = FakeChat(content="not json at all {{{")
        r1 = llm_step(
            client=client,
            agent_type="step_probe",
            messages=MESSAGES,
            model="m1",
            cache=True,
            validate="object",
        )
        assert r1.validation is not None
        assert r1.validation.ok is False
        r2 = llm_step(
            client=client,
            agent_type="step_probe",
            messages=MESSAGES,
            model="m1",
            cache=True,
            validate="object",
        )
        assert r2.cache_hit is False
        assert len(client.calls) == 2


class TestBudget:
    def test_block_records_blocked_run_and_raises(self, pf_tables, pf_connection, monkeypatch):
        import pf_core.llm.step as step_mod
        from pf_core.llm.step import BudgetEstimate, llm_step

        exc = CostBudgetExceeded(
            scope_kind="agent",
            scope_value="step_probe",
            period="daily",
            limit_usd=1.0,
            spent_usd=1.0,
            projected_usd=0.5,
        )

        def _block(**kwargs):
            raise exc

        monkeypatch.setattr(step_mod, "check_budget", _block)
        client = FakeChat()
        with pytest.raises(CostBudgetExceeded):
            llm_step(
                client=client,
                agent_type="step_probe",
                messages=MESSAGES,
                model="m1",
                budget=BudgetEstimate(job_kind="test_pass"),
            )
        assert client.calls == []
        runs = _runs(pf_connection)
        assert len(runs) == 1
        assert runs[0]["status"] == "budget_blocked"

    def test_estimates_forwarded_to_project_cost(self, pf_tables, pf_connection, monkeypatch):
        import pf_core.llm.step as step_mod
        from pf_core.llm.step import BudgetEstimate, llm_step

        seen: dict[str, Any] = {}

        def _project(**kwargs):
            seen.update(kwargs)
            return 0.0

        monkeypatch.setattr(step_mod, "project_cost", _project)
        monkeypatch.setattr(step_mod, "check_budget", lambda **kwargs: None)
        llm_step(
            client=FakeChat(),
            agent_type="step_raw",
            messages=MESSAGES,
            model="m1",
            budget=BudgetEstimate(prompt_tokens=42, completion_tokens=7),
        )
        assert seen["estimated_prompt_tokens"] == 42
        assert seen["estimated_completion_tokens"] == 7

    def test_no_budget_kwarg_skips_gate(self, pf_tables, pf_connection, monkeypatch):
        import pf_core.llm.step as step_mod
        from pf_core.llm.step import llm_step

        def _fail_if_consulted(**kwargs):
            raise AssertionError("budget consulted without budget=")

        monkeypatch.setattr(step_mod, "project_cost", _fail_if_consulted)
        res = llm_step(client=FakeChat(), agent_type="step_raw", messages=MESSAGES, model="m1")
        assert res.run_id is not None
