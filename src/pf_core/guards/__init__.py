"""Build-time structural guards: file-size and layering checks (stdlib-only)."""
from __future__ import annotations

from pf_core.guards.config import GuardsConfig, load_guards_config
from pf_core.guards.layering import LayeringViolation, check_layering
from pf_core.guards.structure import FileSizeViolation, filter_baselined, scan_file_sizes

__all__ = [
    "FileSizeViolation",
    "GuardsConfig",
    "LayeringViolation",
    "check_layering",
    "filter_baselined",
    "load_guards_config",
    "scan_file_sizes",
]
