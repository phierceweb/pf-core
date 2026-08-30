"""The checks a project turns on in ``.pf-guards.toml``, run after the size and layering gate."""

from __future__ import annotations

from pf_core.guards.comments import report_comment_budget
from pf_core.guards.config import GuardsConfig
from pf_core.guards.framework import report_framework
from pf_core.guards.framework_config import FrameworkConfig
from pf_core.guards.functions import report_function_lengths
from pf_core.guards.sources import report_unparsed


def report_opt_in(
    roots: list[str],
    cfg: GuardsConfig,
    *,
    framework_roots: list[str] | None,
    framework: FrameworkConfig,
    report: bool,
) -> bool:
    """Run every check ``cfg`` turns on, printing what each finds; True when any fails.

    ``framework_roots`` is None when the framework check is not to run; ``report`` keeps its
    breaches from failing. A file one of them cannot parse fails the gate, and each check still
    runs over the files that do.
    """
    own = cfg.max_function_lines is not None or cfg.comment_budget is not None
    failed = report_unparsed([*(roots if own else []), *(framework_roots or [])])
    if cfg.max_function_lines is not None:
        failed = report_function_lengths(roots, cfg.max_function_lines) or failed
    if cfg.comment_budget is not None:
        failed = report_comment_budget(roots, cfg.comment_budget) or failed
    if framework_roots is not None:
        failed = report_framework(framework_roots, framework, report=report) or failed
    return failed
