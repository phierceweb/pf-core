"""Tests for pf_core.eval._judge — config hard-fail behavior.

The judge routes via ``resolve_agent`` like any agent: a missing judge-agent
config raises ``ConfigurationError`` before anything is recorded, with no
silent fallback to a default model.
"""

from __future__ import annotations

import pytest

from pf_core.eval._judge import run_judge
from pf_core.exceptions import ConfigurationError
from pf_core.llm.router import clear_cache


@pytest.fixture(autouse=True)
def _reset_router_cache():
    clear_cache()
    yield
    clear_cache()


def _point_at(tmp_path, monkeypatch, body: str):
    path = tmp_path / "model_router.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(path))


def _call_judge():
    return run_judge(
        agent_type="drafter",
        judge_agent_type="drafter_judge",
        golden_payload={"rendered_user": "task", "parsed_output": {}},
        replay_content="candidate output",
        replay_run_id=1,
    )


def test_missing_judge_agent_raises_configuration_error(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch, "agents:\n  drafter:\n    model: m1\n")

    with pytest.raises(ConfigurationError, match="drafter_judge"):
        _call_judge()


def test_judge_agent_without_resolvable_backend_raises(tmp_path, monkeypatch):
    """Flat judge agent but no default_client anywhere -> hard fail, no
    silent OpenRouter assumption."""
    _point_at(
        tmp_path,
        monkeypatch,
        "agents:\n  drafter_judge:\n    model: judge-model\n",
    )

    with pytest.raises(ConfigurationError, match="drafter_judge"):
        _call_judge()


def test_judge_yaml_sampling_wins_over_defaults(pf_engine, monkeypatch):
    """The judge's YAML-declared sampling is honored; 0.0/512 only fill gaps
    (a reasoning judge capped at 512 tokens would emit nothing and score 0)."""
    from pf_core.llm.tracking import (
        clear_resolver_caches,
        llm_agent_types,
        llm_models,
        llm_runs,
        metadata,
    )

    metadata.create_all(pf_engine)
    clear_resolver_caches()
    try:
        with pf_engine.begin() as conn:
            mid = conn.execute(
                llm_models.insert().values(name="judge-parent-model")
            ).inserted_primary_key[0]
            aid = conn.execute(
                llm_agent_types.insert().values(slug="judge_parent_agent")
            ).inserted_primary_key[0]
            replay_id = conn.execute(
                llm_runs.insert().values(
                    agent_type_id=aid, model_id=mid, status="success"
                )
            ).inserted_primary_key[0]

        seen: dict = {}

        class _JudgeClient:
            def chat(self, *, messages, model="", **kwargs):
                seen.update(kwargs)
                seen["model"] = model
                return '{"score": 0.8, "rationale": "ok"}', {"duration_ms": 1}

        monkeypatch.setattr(
            "pf_core.eval._judge.resolve_agent",
            lambda slug, **k: (
                _JudgeClient(),
                {"model": "judge-m", "max_tokens": 2000, "reasoning_effort": "medium"},
                "fake_backend",
            ),
        )

        score = run_judge(
            agent_type="judge_parent_agent",
            judge_agent_type="judge_parent_agent_judge",
            golden_payload={"rendered_user": "q", "parsed_output": {"x": 1}},
            replay_content="candidate",
            replay_run_id=replay_id,
        )

        assert score == 0.8
        assert seen["model"] == "judge-m"
        assert seen["max_tokens"] == 2000  # YAML wins over the 512 default
        assert seen["temperature"] == 0.0  # default fills the gap
        assert seen["reasoning_effort"] == "medium"  # passes through untouched
    finally:
        clear_resolver_caches()
        metadata.drop_all(pf_engine)
