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


__all__ = [
    "begin_call_recording",
    "current_session_metadata",
    "end_call_recording",
    "record_call",
]
