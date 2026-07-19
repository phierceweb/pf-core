"""Tests for pf_core.llm.recording — the ambient call-recording window."""

from __future__ import annotations

import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

from pf_core.llm.recording import (
    begin_call_recording,
    current_session_metadata,
    end_call_recording,
    record_call,
)


def test_open_record_drain_order():
    begin_call_recording()
    record_call({"agent_type": "summarizer"})
    record_call({"agent_type": "labeler"})
    out = end_call_recording()
    assert [r["agent_type"] for r in out] == ["summarizer", "labeler"]


def test_end_twice_returns_empty():
    begin_call_recording()
    end_call_recording()
    assert end_call_recording() == []


def test_record_call_noop_when_closed():
    end_call_recording()
    record_call({"agent_type": "x"})  # must not raise
    assert end_call_recording() == []


def test_begin_resets_dirty_window():
    begin_call_recording(session_metadata={"a": 1})
    record_call({"n": 1})
    begin_call_recording(session_metadata={"b": 2})
    assert current_session_metadata() == {"b": 2}
    assert end_call_recording() == []


def test_session_metadata_copies_and_lifecycle():
    md = {"source_name": "report.pdf"}
    begin_call_recording(session_metadata=md)
    got = current_session_metadata()
    assert got == md
    got["mutated"] = True
    assert current_session_metadata() == md
    end_call_recording()
    assert current_session_metadata() == {}


def test_windows_isolated_across_threads():
    seen = {}

    def worker(name: str) -> None:
        begin_call_recording(session_metadata={"who": name})
        record_call({"who": name})
        seen[name] = (current_session_metadata(), end_call_recording())

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["a"] == ({"who": "a"}, [{"who": "a"}])
    assert seen["b"] == ({"who": "b"}, [{"who": "b"}])


def test_pool_worker_shares_window_via_copy_context():
    begin_call_recording(session_metadata={"source_name": "report.pdf"})
    ctx = contextvars.copy_context()

    def worker() -> dict:
        record_call({"agent_type": "summarizer"})
        return current_session_metadata()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker_md = pool.submit(ctx.run, worker).result()

    assert worker_md == {"source_name": "report.pdf"}
    assert end_call_recording() == [{"agent_type": "summarizer"}]
