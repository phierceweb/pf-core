"""Extract and recover JSON from messy text (stdlib only).

See ``docs/json-recovery.md`` for the span-selection rule.
"""

from __future__ import annotations

import json
import re

# Dead ends are individually cheap but unbounded in count; past this many the
# scan gives up rather than keep hunting for a payload.
_MAX_SCAN_FAILURES = 64


def extract_json(raw: str) -> dict | list | None:
    """Extract the best JSON object or array from raw text.

    Strips markdown fences and trailing commentary, then ranks every balanced
    span that parses (``docs/json-recovery.md``). An array inside an object
    loses to the object containing it.

    Returns the parsed Python object, or None if no valid JSON is found.
    """
    cleaned = strip_markdown_fences(raw)

    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    found = _scan_candidates(cleaned, "{", "}") + _scan_candidates(cleaned, "[", "]")
    outermost = _drop_nested(found)
    if not outermost:
        return None
    return max(outermost, key=_rank)[2]


def extract_json_array(raw: str) -> list | None:
    """Extract the JSON array most likely to be the payload.

    Handles the common case where the text returns ``[...]`` followed by
    commentary, or wraps the array in markdown fences. Every balanced span is
    ranked rather than taking the first, so a prose bracket loses whether or
    not it is itself valid JSON — see ``docs/json-recovery.md`` for the rule.

    Returns the parsed list, or None if no valid array is found.
    """
    found = _scan_candidates(strip_markdown_fences(raw), "[", "]")
    if not found:
        return None
    result = max(found, key=_rank)[2]
    return result if isinstance(result, list) else [result]


def extract_json_object(raw: str) -> dict | None:
    """Extract the JSON object most likely to be the payload.

    Uses the selection rule described on :func:`extract_json_array`, so a
    placeholder brace (``{}``) or a non-JSON one (``{placeholder}``) loses to
    the real object.

    Returns the parsed dict, or None if no valid object is found.
    """
    found = _scan_candidates(strip_markdown_fences(raw), "{", "}")
    if not found:
        return None
    result = max(found, key=_rank)[2]
    return result if isinstance(result, dict) else None


def recover_truncated_json(raw: str) -> list[dict] | None:
    """Salvage complete JSON objects from a truncated array response.

    When a response is cut off mid-array, it ends with something like
    ``[{...}, {... <cut off>``. This function walks the string tracking brace
    depth to find the last fully-closed top-level object, then closes the
    array there.

    Anchors on the first *unclosed* ``[`` — a truncated array is by definition
    unclosed, so a complete bracket earlier in the text is prose, not the
    truncation site.

    Returns a list of the complete objects, or None if recovery fails.
    """
    cleaned = strip_markdown_fences(raw)

    arr_start = _first_unclosed(cleaned, "[", "]")
    if arr_start == -1:
        arr_start = cleaned.find("[")
    if arr_start == -1:
        return None

    brace_depth = 0
    in_string = False
    escape_next = False
    last_top_close = -1

    for i in range(arr_start + 1, len(cleaned)):
        c = cleaned[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            brace_depth += 1
        elif c == "}":
            brace_depth -= 1
            if brace_depth == 0:
                last_top_close = i

    if last_top_close <= 0:
        return None

    candidate = cleaned[arr_start : last_top_close + 1].rstrip().rstrip(",") + "]"

    try:
        result = json.loads(candidate)
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        return None


def strip_markdown_fences(raw: str) -> str:
    """Remove markdown code fences (```json ... ```) from text."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


def _carries_object(value: object) -> bool:
    """Whether a parsed candidate contains a non-empty dict at any depth."""
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return any(_carries_object(item) for item in value)
    return False


def _rank(candidate: tuple[int, int, object]) -> tuple[bool, bool, int]:
    start, _end, value = candidate
    return (start == 0 and bool(value), _carries_object(value), -start)


def _drop_nested(spans: list[tuple[int, int, object]]) -> list[tuple[int, int, object]]:
    """Discard spans contained in another."""
    outermost: list[tuple[int, int, object]] = []
    covered = -1
    for span in sorted(spans, key=lambda s: (s[0], -s[1])):
        if span[1] > covered:
            outermost.append(span)
            covered = span[1]
    return outermost


def _payload_cut(text: str) -> int:
    """Offset past which nothing can be payload — the first unclosed opener.

    Without it the scan answers from inside a truncated container, pre-empting
    recovery and repair (``docs/json-recovery.md``).
    """
    cuts = [
        p
        for p in (_first_unclosed(text, "{", "}"), _first_unclosed(text, "[", "]"))
        if p != -1
    ]
    return min(cuts) if cuts else len(text)


def _scan_candidates(text: str, opener: str, closer: str) -> list[tuple[int, int, object]]:
    """Left-to-right non-overlapping balanced spans that parse as JSON.

    Stops early once a span that cannot be outranked is found, and gives up
    after ``_MAX_SCAN_FAILURES`` dead ends.
    """
    found: list[tuple[int, int, object]] = []
    failures = 0
    cut = _payload_cut(text)
    i = text.find(opener)

    while i != -1 and i < cut and failures < _MAX_SCAN_FAILURES:
        end = _find_matching_close(text, i, opener, closer)
        if end is None:
            failures += 1
            i = text.find(opener, i + 1)
            continue
        try:
            value = json.loads(text[i : end + 1])
        except (json.JSONDecodeError, ValueError):
            # Never descend into a span that failed to parse: its contents are
            # a fragment of malformed JSON that json_repair should own.
            failures += 1
            i = text.find(opener, end + 1)
            continue
        found.append((i, end, value))
        if (i == 0 and value) or _carries_object(value):
            break
        i = text.find(opener, end + 1)

    return found


def _first_unclosed(text: str, opener: str, closer: str) -> int:
    i = text.find(opener)
    while i != -1:
        end = _find_matching_close(text, i, opener, closer)
        if end is None:
            return i
        i = text.find(opener, end + 1)
    return -1


def _find_matching_close(text: str, start: int, opener: str, closer: str) -> int | None:
    """Find the index of the matching closing bracket/brace."""
    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i

    return None
