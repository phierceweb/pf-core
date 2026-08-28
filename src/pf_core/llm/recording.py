"""Ambient LLM call-recording window (ContextVar-based).

Open a window with :func:`begin_call_recording`; every :func:`record_call`
inside it appends one record, and ``tracked_messages_call`` merges the
window's ``session_metadata`` into each run's tags/metrics and appends a
per-call summary automatically. :func:`end_call_recording` drains and closes.

Windows are per-context, so concurrent tasks stay independent; pool workers
join one only when submitted via ``contextvars.copy_context().run(...)`` —
recipe and mechanics in ``docs/llm-recording.md``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_records: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "pf_core_llm_call_records", default=None
)
_session_md: ContextVar[dict[str, Any] | None] = ContextVar(
    "pf_core_llm_session_metadata", default=None
)


def begin_call_recording(*, session_metadata: dict[str, Any] | None = None) -> None:
    """Open (or reset) the current context's recording window."""
    _records.set([])
    _session_md.set(dict(session_metadata) if session_metadata else None)


def end_call_recording() -> list[dict[str, Any]]:
    """Drain and close the window. Returns ``[]`` when no window is open."""
    out = list(_records.get() or [])
    _records.set(None)
    _session_md.set(None)
    return out


def record_call(record: dict[str, Any]) -> None:
    """Append one record if a window is open; silent no-op otherwise."""
    records = _records.get()
    if records is not None:
        records.append(record)


def current_session_metadata() -> dict[str, Any]:
    """Copy of the open window's session metadata; ``{}`` when closed."""
    return dict(_session_md.get() or {})


def call_summary(
    *,
    agent_type: str,
    model: str,
    provider: str | None,
    spec: dict | None,
    usage: dict,
    success: bool,
    run_id: int | None,
) -> dict[str, Any]:
    """Recording-window summary for one call; usage values may be None → 0."""
    return {
        "agent_type": agent_type,
        "model": model,
        "provider": provider,
        "prompt_version": int(spec["version"]) if spec else None,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "cost_usd": float(usage.get("cost_usd", 0.0) or 0.0),
        "duration_ms": int(usage.get("duration_ms", 0) or 0),
        "success": success,
        "run_id": run_id,
    }


__all__ = [
    "begin_call_recording",
    "current_session_metadata",
    "end_call_recording",
    "record_call",
]
