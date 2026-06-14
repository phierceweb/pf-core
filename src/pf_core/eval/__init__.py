"""
pf-core eval harness — golden-set replay and regression testing.

Run historical LLM calls against a new model or prompt version; compare
results field-by-field (structured diff) or via an LLM judge; surface
regressions before they hit production.

Public surface::

    from pf_core.eval import (
        # Config
        EvalConfig,
        AgentEvalConfig,
        MetricGate,
        load_eval_config,
        clear_config_cache,

        # Golden set
        GoldenSetRepo,

        # Comparators
        EvalResult,
        EvalReport,
        register_comparator,
        get_comparator,
        list_comparators,

        # Runner
        EvalRunner,
    )

See ``docs/eval-harness.md`` for the full reference.
"""

from pf_core.eval._compare import (  # noqa: F401
    get_comparator,
    list_comparators,
    register_comparator,
)
from pf_core.eval._config import (  # noqa: F401
    AgentEvalConfig,
    EvalConfig,
    MetricGate,
    clear_config_cache,
    load_eval_config,
)
from pf_core.eval._golden import GoldenSetRepo  # noqa: F401
from pf_core.eval._report import EvalReport, EvalResult  # noqa: F401
from pf_core.eval._runner import EvalRunner  # noqa: F401

__all__ = [
    # Config
    "AgentEvalConfig",
    "EvalConfig",
    "MetricGate",
    "clear_config_cache",
    "load_eval_config",
    # Golden set
    "GoldenSetRepo",
    # Comparators
    "get_comparator",
    "list_comparators",
    "register_comparator",
    # Results
    "EvalReport",
    "EvalResult",
    # Runner
    "EvalRunner",
]


# ---------------------------------------------------------------------------
# Register the eval_replay job kind so any project importing pf_core.eval
# can create eval_replay jobs without extra setup.
# ---------------------------------------------------------------------------

def _register_eval_kind() -> None:
    """Register the eval_replay job kind (idempotent)."""
    from pf_core.exceptions import ConfigurationError
    from pf_core.jobs import get_kind, register_kind

    try:
        get_kind("eval_replay")
        return  # already registered
    except ConfigurationError:
        pass

    register_kind(
        kind="eval_replay",
        description=(
            "Replay one golden set version against a new model or prompt. "
            "One job per EvalRunner.run() call."
        ),
        states=["pending", "running", "succeeded", "failed", "canceled"],
        transitions={
            "pending": ["running", "canceled"],
            "running": ["succeeded", "failed", "canceled"],
            "failed": ["pending"],
        },
        default_priority=40,
    )


_register_eval_kind()
