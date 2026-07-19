# LLM Call Recording

An ambient window for attributing and summarizing every tracked LLM call in
a batch, conversion, or request — without threading kwargs through the call
stack. Open the window at the entry point with session-level metadata; every
`tracked_messages_call` inside it inherits that metadata as tags/metrics and
appends a per-call summary; drain the summaries at exit.

State is ContextVar-based (the `jobs.runtime.current_job_id` pattern), so
concurrent tasks keep independent windows.

---

## Table of Contents

- [API](#api)
- [One window around a batch](#one-window-around-a-batch)
- [Thread pools](#thread-pools)
- [`split_metadata`](#split_metadata)

---

## API

```python
from pf_core.llm.recording import (
    begin_call_recording,   # open (or reset) the window; takes session_metadata=
    end_call_recording,     # drain + close; returns list[dict]; [] when closed
    record_call,            # append one dict; silent no-op when closed
    current_session_metadata,  # copy of the window's metadata; {} when closed
)
```

---

## One window around a batch

```python
from pf_core.llm.recording import begin_call_recording, end_call_recording

begin_call_recording(session_metadata={"source_name": "report.pdf"})
try:
    convert(...)   # every tracked_messages_call inside inherits the metadata
finally:
    calls = end_call_recording()
# calls → [{"agent_type": ..., "model": ..., "provider": ...,
#           "prompt_version": ..., "prompt_tokens": ..., "completion_tokens": ...,
#           "cost_usd": ..., "duration_ms": ..., "success": ..., "run_id": ...}, ...]
```

What `tracked_messages_call` does per call while a window is open:

- Merges `current_session_metadata()` beneath its `metadata=` kwarg (the
  call's keys win), splits the result via
  `pf_core.llm.tracking.split_metadata`, and records the tags/metrics on the
  run — failed rows included.
- Appends the summary dict above on success **and** failure
  (`success=False`, token/cost fields 0).

No window open ⇒ both are no-ops. `tracked_call` does not consult the
window; `record_call` is public for composing it manually.

---

## Thread pools

A ContextVar window is invisible to pool workers unless the task is
submitted through a copied context:

```python
import contextvars
from concurrent.futures import ThreadPoolExecutor

begin_call_recording(session_metadata={"source_name": "report.pdf"})
ctx = contextvars.copy_context()
with ThreadPoolExecutor() as pool:
    futures = [pool.submit(ctx.run, work, item) for item in items]
```

Worker appends land in the shared window because the copied context holds
the same list object; `list.append` is GIL-atomic, so no lock is needed.
Capture `ctx` **after** `begin_call_recording`.

---

## `split_metadata`

`pf_core.llm.tracking.split_metadata(metadata) -> (tags, metrics)` maps a
flat dict onto the tag/metric sidecar tables: bools → `"key:true"` /
`"key:false"` tags, int/float → float metrics, `None` dropped, everything
else a `"key:value"` tag. Tags and metric keys truncate to the sidecar
columns' 64-char cap (see `tracking.schema`).

---

See [llm-tracked.md](llm-tracked.md) for the tracked-call contract and
[llm-tracking.md](llm-tracking.md) for the tables these rows land in.
