"""High-level LLM response JSON parser.

Composes the individual extraction and recovery helpers from
:mod:`pf_core.utils.json_recovery` into a single call that walks the
full fallback pipeline: strip fences → json.loads → extract → recover
→ ``json_repair`` (permissive repair for malformed LLM output).

Usage::

    from pf_core.llm.parse import parse_llm_json

    result = parse_llm_json(llm_response_text, expect="array")
    if result is None:
        print("Could not parse response")
"""

from __future__ import annotations

import json

try:
    import json_repair  # type: ignore[import-untyped]
except ImportError as e:  # pragma: no cover - exercised by bare-install CI
    from pf_core._extras import extra_import_error

    raise extra_import_error("validate", "json_repair", feature="pf_core.llm.parse") from e

from pf_core.exceptions import InvalidInputError
from pf_core.utils.json_recovery import (
    extract_json_array,
    extract_json_object,
    recover_truncated_json,
    strip_markdown_fences,
)
from pf_core.log import get_logger

logger = get_logger(__name__)


def parse_llm_json(
    raw: str,
    *,
    expect: str = "any",
    recover: bool = True,
    strict: bool = False,
) -> dict | list | None:
    """Parse JSON from an LLM response, with fallbacks.

    Walks a multi-step pipeline to extract valid JSON from the messy
    output that LLMs typically produce (markdown fences, trailing prose,
    truncated arrays, unescaped inner quotes).

    Pipeline:

    1. Strip markdown fences.
    2. ``json.loads`` (strict — zero-tolerance for malformed JSON).
    3. ``extract_json_*`` — find the first balanced array / object in
       mixed text and try to load just that substring.
    4. ``recover_truncated_json`` — close unbalanced brackets on a
       mid-stream-truncated response (e.g., model hit ``max_tokens``
       while writing the last element of an array).
    5. ``json_repair.loads`` on the cleaned text — last-resort permissive
       repair. Handles unescaped inner double quotes (verbatim quoted
       dialogue inside string values), backslash-escaped single quotes,
       trailing commas, unquoted keys, single-quoted strings.
       Intentionally last in the chain: strict parsing on a well-formed
       response is faster and cheaper, and ``json_repair``'s tolerance
       can mask genuine structural defects if used too eagerly.

    Args:
        raw: Raw LLM response text.
        expect: Expected result type — ``"any"``, ``"array"``, or
            ``"object"``. Filters the parsed result by type.
        recover: If ``True`` and *expect* is ``"array"`` or ``"any"``,
            attempt :func:`recover_truncated_json` as a last resort.
        strict: If ``True``, raise :class:`InvalidInputError` instead
            of returning ``None`` on parse failure.

    Returns:
        Parsed ``dict`` or ``list``, or ``None`` if parsing fails and
        *strict* is ``False``.

    Raises:
        InvalidInputError: If *strict* is ``True`` and no JSON could
            be extracted.
    """
    # Step 1: strip markdown fences
    cleaned = strip_markdown_fences(raw)

    result = None

    # Step 2: try json.loads on the cleaned text
    try:
        result = json.loads(cleaned)
        logger.debug("parse_llm_json_succeeded", step="json.loads")
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 3: fallback to targeted extraction
    if result is None:
        if expect == "array":
            result = extract_json_array(cleaned)
        elif expect == "object":
            result = extract_json_object(cleaned)
        else:
            # For "any", try both and prefer whichever starts earlier
            arr_pos = cleaned.find("[")
            obj_pos = cleaned.find("{")
            if arr_pos != -1 and (obj_pos == -1 or arr_pos < obj_pos):
                result = extract_json_array(cleaned) or extract_json_object(cleaned)
            elif obj_pos != -1:
                result = extract_json_object(cleaned) or extract_json_array(cleaned)

        if result is not None:
            logger.debug("parse_llm_json_succeeded", step="extract")

    # Step 4: truncated-array recovery salvages a prefix and DROPS the tail;
    # the return carries no flag, so the WARNING is the only signal.
    if result is None and recover and expect in ("array", "any"):
        result = recover_truncated_json(cleaned)
        if result is not None:
            logger.debug("parse_llm_json_succeeded", step="recover_truncated")
            logger.warning(
                "parse_llm_json_recovered_truncated",
                recovered_items=len(result) if isinstance(result, list) else None,
                raw_len=len(raw),
                hint="response was truncated (likely max_tokens); tail dropped",
            )

    # Step 5: json_repair — permissive last-resort repair for malformed LLM
    # output. Handles the specific failure modes stdlib json.loads rejects:
    # unescaped inner double-quotes in string values, backslash-escaped
    # single quotes, trailing commas, unquoted keys. Runs AFTER the
    # strict + extract + recover chain so well-formed responses stay on
    # the fast path and don't pay repair cost. Gated on ``recover`` —
    # callers that want strict parse behavior (``recover=False``) opt out
    # of BOTH truncation-recovery and permissive JSON repair.
    if result is None and recover:
        try:
            repaired = json_repair.loads(cleaned)
            if repaired is not None and repaired != "":
                result = repaired
                logger.debug("parse_llm_json_succeeded", step="json_repair")
        except Exception:
            # json_repair rarely raises — this is belt-and-suspenders.
            pass

    # Step 6: type-check against expect
    if result is not None:
        if expect == "array" and not isinstance(result, list):
            result = None
        elif expect == "object" and not isinstance(result, dict):
            result = None

    # Step 7: strict mode
    if result is None and strict:
        raise InvalidInputError("Failed to parse JSON from LLM response")

    return result
