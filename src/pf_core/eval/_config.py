"""
Eval harness config — YAML schema and loader.

YAML format (``config/eval.yaml``)::

    defaults:
      compare: structured_diff
      pass_threshold: 0.85
      parallelism: 4
      sampling:
        temperature: 0.0

    agents:
      drafter:
        compare: llm_judge
        judge_agent_type: drafter_judge
        pass_threshold: 0.80
      classifier:
        compare: structured_diff
        diff_fields: [category, confidence]
        tolerances:
          confidence: 0.10
        pass_threshold: 0.95

Environment variable:

- ``EVAL_CONFIG`` — path to eval.yaml. Default: ``config/eval.yaml``.

See ``docs/eval-harness.md`` for the full reference.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

try:
    from pydantic import BaseModel, Field
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("eval", "pydantic", feature="pf_core.eval") from e

from pf_core.exceptions import ConfigurationError

_DEFAULT_PATH = "config/eval.yaml"

_lock = threading.Lock()
_cache: dict[str, "EvalConfig"] = {}


class MetricGate(BaseModel):
    """Hard gate on a named metric stored in ``llm_run_metrics``."""

    name: str
    min: float | None = None
    max: float | None = None


class AgentEvalConfig(BaseModel):
    """Per-agent eval policy. Loaded from ``agents.<slug>`` block in eval.yaml."""

    compare: str = "structured_diff"
    pass_threshold: float = 0.85
    parallelism: int = 4
    sampling: dict[str, Any] = Field(default_factory=lambda: {"temperature": 0.0})
    diff_fields: list[str] = Field(default_factory=list)
    tolerances: dict[str, float] = Field(default_factory=dict)
    judge_agent_type: str | None = None
    metrics: list[MetricGate] = Field(default_factory=list)


class EvalConfig:
    """Container for a parsed eval.yaml file.

    Args:
        data: Raw YAML-decoded dict.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._defaults_raw: dict[str, Any] = data.get("defaults", {})
        self._agents_raw: dict[str, Any] = data.get("agents", {})
        self.defaults: AgentEvalConfig = AgentEvalConfig(**self._defaults_raw)

    def for_agent(self, agent_type: str) -> AgentEvalConfig:
        """Return merged config for an agent (defaults + agent-specific overrides).

        Agent-level keys win over defaults. Missing agent entries fall back to
        defaults entirely.
        """
        merged = {**self._defaults_raw}
        if agent_type in self._agents_raw:
            merged.update(self._agents_raw[agent_type])
        return AgentEvalConfig(**merged)

    def __repr__(self) -> str:
        return (
            f"EvalConfig(agents={sorted(self._agents_raw)}, "
            f"defaults={self._defaults_raw})"
        )


def load_eval_config(path: str | None = None) -> EvalConfig:
    """Load eval.yaml, caching by resolved path.

    Args:
        path: Override path. Falls back to ``EVAL_CONFIG`` env var, then
            ``config/eval.yaml``. Returns a default (empty) config if the
            file does not exist — a runner can work from per-call overrides
            alone, without a project config file.

    Raises:
        ConfigurationError: If the file exists but cannot be parsed.
    """
    config_path = path or os.environ.get("EVAL_CONFIG", _DEFAULT_PATH)
    with _lock:
        if config_path in _cache:
            return _cache[config_path]
        p = Path(config_path)
        if not p.exists():
            cfg = EvalConfig({})
            _cache[config_path] = cfg
            return cfg
        try:
            import yaml

            data = yaml.safe_load(p.read_text()) or {}
        except Exception as exc:
            raise ConfigurationError(
                f"Cannot parse eval config {config_path!r}: {exc}"
            ) from exc
        cfg = EvalConfig(data)
        _cache[config_path] = cfg
        return cfg


def clear_config_cache() -> None:
    """Clear the in-memory config cache (useful in tests)."""
    with _lock:
        _cache.clear()
