"""Extract and recover JSON from messy text.

Generic, foundation-level helpers: they operate on plain strings and have no
LLM or third-party coupling (stdlib only). Common sources of "messy" JSON are
LLM responses (markdown fences, trailing commentary, truncation at a token
limit), but nothing here is LLM-specific — hence the home under
``pf_core.utils`` rather than ``pf_core.llm``.

Usage::

    from pf_core.utils.json_recovery import extract_json_array, recover_truncated_json

    result = extract_json_array(text)
    if result is None:
        result = recover_truncated_json(text)
"""

from __future__ import annotations

import json
import re


def extract_json(raw: str) -> dict | list | None:
    """Extract the first valid JSON object or array from raw text.

    Strips markdown fences and trailing commentary. Returns the parsed
    Python object, or None if no valid JSON is found.
    """
    cleaned = strip_markdown_fences(raw)

    # Try the whole string first
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find an object or array
    for opener, closer in [("{", "}"), ("[", "]")]:
        start = cleaned.find(opener)
        if start == -1:
            continue
        end = _find_matching_close(cleaned, start, opener, closer)
        if end is not None:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    return None


def extract_json_array(raw: str) -> list | None:
    """Extract the first complete JSON array from raw text.

    Handles the common case where the text returns ``[...]`` followed by
    commentary, or wraps the array in markdown fences.

    Returns the parsed list, or None if no valid array is found.
    """
    cleaned = strip_markdown_fences(raw)
    start = cleaned.find("[")
    if start == -1:
        return None

    end = _find_matching_close(cleaned, start, "[", "]")
    if end is None:
        return None

    try:
        result = json.loads(cleaned[start : end + 1])
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        return None


def extract_json_object(raw: str) -> dict | None:
    """Extract the first complete JSON object from raw text.

    Returns the parsed dict, or None if no valid object is found.
    """
    cleaned = strip_markdown_fences(raw)
    start = cleaned.find("{")
    if start == -1:
        return None

    end = _find_matching_close(cleaned, start, "{", "}")
    if end is None:
        return None

    try:
        result = json.loads(cleaned[start : end + 1])
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def recover_truncated_json(raw: str) -> list[dict] | None:
    """Salvage complete JSON objects from a truncated array response.

    When a response is cut off mid-array, it ends with something like
    ``[{...}, {... <cut off>``. This function walks the string tracking brace
    depth to find the last fully-closed top-level object, then closes the
    array there.

    Returns a list of the complete objects, or None if recovery fails.
    """
    cleaned = strip_markdown_fences(raw)

    # Find the opening bracket to know our base depth
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
            if brace_depth == 0:  # just closed a top-level object inside the array
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
    # Remove opening fence: ```json or ``` at start
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    # Remove closing fence: ``` at end
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


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
