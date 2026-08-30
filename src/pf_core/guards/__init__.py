"""Build-time guards: file size, function length, comment budget, layering, framework (stdlib-only)."""

from __future__ import annotations

from pf_core.guards.comments import CommentBudget, ProseViolation, scan_comment_budget
from pf_core.guards.config import FunctionLimits, GuardsConfig, load_guards_config
from pf_core.guards.framework import FrameworkBreach, check_framework
from pf_core.guards.framework_config import FrameworkConfig, FrameworkExemption
from pf_core.guards.functions import FunctionLengthViolation, scan_function_lengths
from pf_core.guards.layering import LayeringViolation, check_layering
from pf_core.guards.structure import FileSizeViolation, filter_baselined, scan_file_sizes

__all__ = [
    "CommentBudget",
    "FileSizeViolation",
    "FrameworkBreach",
    "FrameworkConfig",
    "FrameworkExemption",
    "FunctionLengthViolation",
    "FunctionLimits",
    "GuardsConfig",
    "LayeringViolation",
    "ProseViolation",
    "check_framework",
    "check_layering",
    "filter_baselined",
    "load_guards_config",
    "scan_comment_budget",
    "scan_file_sizes",
    "scan_function_lengths",
]
