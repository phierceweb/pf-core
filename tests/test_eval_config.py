"""Tests for pf_core.eval._config."""

from __future__ import annotations

import textwrap

import pytest

from pf_core.eval._config import (
    AgentEvalConfig,
    EvalConfig,
    clear_config_cache,
    load_eval_config,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_config_cache()
    yield
    clear_config_cache()


# ---------------------------------------------------------------------------
# AgentEvalConfig defaults
# ---------------------------------------------------------------------------


def test_agent_eval_config_defaults():
    cfg = AgentEvalConfig()
    assert cfg.compare == "structured_diff"
    assert cfg.pass_threshold == 0.85
    assert cfg.parallelism == 4
    assert cfg.sampling == {"temperature": 0.0}
    assert cfg.diff_fields == []
    assert cfg.tolerances == {}
    assert cfg.judge_agent_type is None
    assert cfg.metrics == []


def test_agent_eval_config_custom():
    cfg = AgentEvalConfig(compare="llm_judge", pass_threshold=0.9, judge_agent_type="my_judge")
    assert cfg.compare == "llm_judge"
    assert cfg.pass_threshold == 0.9
    assert cfg.judge_agent_type == "my_judge"


# ---------------------------------------------------------------------------
# EvalConfig from raw dict
# ---------------------------------------------------------------------------


def test_eval_config_empty():
    cfg = EvalConfig({})
    agent_cfg = cfg.for_agent("drafter")
    assert agent_cfg.compare == "structured_diff"
    assert agent_cfg.pass_threshold == 0.85


def test_eval_config_defaults_propagate():
    cfg = EvalConfig({
        "defaults": {"pass_threshold": 0.70, "parallelism": 2},
        "agents": {},
    })
    agent_cfg = cfg.for_agent("unknown_agent")
    assert agent_cfg.pass_threshold == 0.70
    assert agent_cfg.parallelism == 2


def test_eval_config_agent_overrides_defaults():
    cfg = EvalConfig({
        "defaults": {"pass_threshold": 0.80, "compare": "structured_diff"},
        "agents": {
            "drafter": {
                "compare": "llm_judge",
                "judge_agent_type": "drafter_judge",
                "pass_threshold": 0.75,
            }
        },
    })
    drafter_cfg = cfg.for_agent("drafter")
    assert drafter_cfg.compare == "llm_judge"
    assert drafter_cfg.judge_agent_type == "drafter_judge"
    assert drafter_cfg.pass_threshold == 0.75

    # Unlisted agent falls back to defaults
    other_cfg = cfg.for_agent("classifier")
    assert other_cfg.compare == "structured_diff"
    assert other_cfg.pass_threshold == 0.80


def test_eval_config_metrics_gates():
    cfg = EvalConfig({
        "agents": {
            "drafter": {
                "metrics": [
                    {"name": "tier1_ratio", "min": 0.70},
                    {"name": "n_sources", "min": 3, "max": 20},
                ]
            }
        }
    })
    drafter_cfg = cfg.for_agent("drafter")
    assert len(drafter_cfg.metrics) == 2
    assert drafter_cfg.metrics[0].name == "tier1_ratio"
    assert drafter_cfg.metrics[0].min == 0.70
    assert drafter_cfg.metrics[1].max == 20


def test_eval_config_diff_fields_and_tolerances():
    cfg = EvalConfig({
        "agents": {
            "reviewer": {
                "diff_fields": ["score", "category"],
                "tolerances": {"score": 3.0},
            }
        }
    })
    reviewer_cfg = cfg.for_agent("reviewer")
    assert reviewer_cfg.diff_fields == ["score", "category"]
    assert reviewer_cfg.tolerances == {"score": 3.0}


# ---------------------------------------------------------------------------
# load_eval_config — file loading
# ---------------------------------------------------------------------------


def test_load_eval_config_missing_file(tmp_path, monkeypatch):
    """Missing file → returns default config without error."""
    monkeypatch.setenv("EVAL_CONFIG", str(tmp_path / "nonexistent.yaml"))
    cfg = load_eval_config()
    assert isinstance(cfg, EvalConfig)
    assert cfg.for_agent("any").compare == "structured_diff"


def test_load_eval_config_from_file(tmp_path, monkeypatch):
    yaml_content = textwrap.dedent("""\
        defaults:
          pass_threshold: 0.60
        agents:
          drafter:
            compare: llm_judge
            judge_agent_type: drafter_judge
    """)
    config_file = tmp_path / "eval.yaml"
    config_file.write_text(yaml_content)
    monkeypatch.setenv("EVAL_CONFIG", str(config_file))

    cfg = load_eval_config()
    assert cfg.defaults.pass_threshold == 0.60
    drafter = cfg.for_agent("drafter")
    assert drafter.compare == "llm_judge"
    assert drafter.judge_agent_type == "drafter_judge"


def test_load_eval_config_cached(tmp_path, monkeypatch):
    config_file = tmp_path / "eval.yaml"
    config_file.write_text("defaults:\n  pass_threshold: 0.77\n")
    monkeypatch.setenv("EVAL_CONFIG", str(config_file))

    cfg1 = load_eval_config()
    cfg2 = load_eval_config()
    assert cfg1 is cfg2  # same object from cache


def test_load_eval_config_invalid_yaml(tmp_path, monkeypatch):
    from pf_core.exceptions import ConfigurationError

    config_file = tmp_path / "eval.yaml"
    config_file.write_text(":\ninvalid: [unclosed\n")
    monkeypatch.setenv("EVAL_CONFIG", str(config_file))

    with pytest.raises(ConfigurationError):
        load_eval_config()
