"""Build-time structural guards: file-size and layering checks (stdlib-only)."""
from __future__ import annotations

from pf_core.guards.structure import (
    FileSizeViolation,
    LayeringViolation,
    check_layering,
    filter_baselined,
    scan_file_sizes,
)

__all__ = [
    "FileSizeViolation",
    "LayeringViolation",
    "check_layering",
    "filter_baselined",
    "scan_file_sizes",
]
