"""Markdown export — incremental, atomic, pruning tree writers.

A foundation module (stdlib only, no extra) for turning a system-of-record
into a markdown tree for RAG ingestion or portable review. See
:mod:`pf_core.export.markdown` for the full contract.
"""

from __future__ import annotations

from pf_core.export.markdown import (
    ExportResult,
    MarkdownExporter,
    yaml_frontmatter,
)

__all__ = ["ExportResult", "MarkdownExporter", "yaml_frontmatter"]
