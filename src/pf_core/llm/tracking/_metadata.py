"""Split a flat metadata dict into ``llm_run_tags`` / ``llm_run_metrics`` shapes."""

from __future__ import annotations

from typing import Any

# Whole rendered "key:value" tag and metric-name cap — mirrors the String(64)
# columns in tracking.schema (llm_run_tags.tag, llm_run_metrics.metric_name).
_TAG_MAX_CHARS = 64


def split_metadata(metadata: dict[str, Any]) -> tuple[list[str], dict[str, float]]:
    """Split *metadata* into (``"key:value"`` tags, ``{key: float}`` metrics).

    Bools → tags ``"key:true"``/``"key:false"``; int/float → metrics; ``None``
    values dropped; everything else stringified into a tag. Tags and metric
    keys truncate to 64 chars. Values must be stringifiable.
    """
    tags: list[str] = []
    metrics: dict[str, float] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, bool):
            tags.append(f"{key}:{'true' if value else 'false'}"[:_TAG_MAX_CHARS])
        elif isinstance(value, (int, float)):
            metrics[str(key)[:_TAG_MAX_CHARS]] = float(value)
        else:
            tags.append(f"{key}:{value}"[:_TAG_MAX_CHARS])
    return tags, metrics


__all__ = ["split_metadata"]
