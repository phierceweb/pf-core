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
