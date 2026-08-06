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
    extract_json,
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
    on_truncation: str = "warn",
) -> dict | list | None:
    """Parse JSON from an LLM response, with fallbacks.

    Walks a multi-step pipeline to extract valid JSON from the messy
    output that LLMs typically produce (markdown fences, trailing prose,
    truncated arrays, unescaped inner quotes).

    Pipeline:

    1. Strip markdown fences.
    2. ``json.loads`` (strict — zero-tolerance for malformed JSON).
    3. ``extract_json_*`` — try every balanced array / object span in
       mixed text, not just the first, and pick the one most likely to
       be the payload (see :mod:`pf_core.utils.json_recovery`).
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
        recover: If ``True``, attempt :func:`recover_truncated_json` (when
            *expect* is ``"array"`` or ``"any"``) and the ``json_repair``
            fallback. ``False`` opts out of both.
        strict: If ``True``, raise :class:`InvalidInputError` instead
            of returning ``None`` on parse failure.
        on_truncation: What step 4 does when it salvages a prefix —
            ``"warn"`` (default) returns the partial list and logs a
            WARNING; ``"raise"`` raises :class:`InvalidInputError`
            instead, for callers that need completeness. Independent of
            *strict*.

    Returns:
        Parsed ``dict`` or ``list``, or ``None`` if parsing fails and
        *strict* is ``False``.

    Raises:
        InvalidInputError: If *strict* is ``True`` and no JSON could
            be extracted, if *on_truncation* is ``"raise"`` and the
            response was truncated, or if *on_truncation* is not one of
            the two allowed values.
    """
    if on_truncation not in ("warn", "raise"):
        raise InvalidInputError(
            f"on_truncation must be 'warn' or 'raise', got {on_truncation!r}"
        )

    cleaned = strip_markdown_fences(raw)

    result = None

    try:
        result = json.loads(cleaned)
        logger.debug("parse_llm_json_succeeded", step="json.loads")
    except (json.JSONDecodeError, ValueError):
        pass

    # An empty container is usually a decoy (``Result: {}``); accepting it here
    # would strand the truncated or malformed payload. Restored below if
    # nothing beats it.
    empty_span = None

    if result is None:
        if expect == "array":
            result = extract_json_array(cleaned)
        elif expect == "object":
            result = extract_json_object(cleaned)
        else:
            result = extract_json(cleaned)

        if result == [] or result == {}:
            empty_span, result = result, None
        elif result is not None:
            logger.debug("parse_llm_json_succeeded", step="extract")

    # Recovery salvages a prefix and DROPS the tail; the return carries no
    # flag, so the WARNING is the only signal under on_truncation="warn".
    if result is None and recover and expect in ("array", "any"):
        result = recover_truncated_json(cleaned)
        if result is not None:
            recovered = len(result) if isinstance(result, list) else None
            if on_truncation == "raise":
                raise InvalidInputError(
                    f"LLM response was truncated; recovered {recovered} complete "
                    "item(s) and dropped the tail"
                )
            logger.debug("parse_llm_json_succeeded", step="recover_truncated")
            logger.warning(
                "parse_llm_json_recovered_truncated",
                recovered_items=recovered,
                raw_len=len(raw),
                hint="response was truncated (likely max_tokens); tail dropped",
            )

    if result is None and recover:
        try:
            repaired = json_repair.loads(cleaned)
            if repaired is not None and repaired != "":
                result = repaired
                logger.debug("parse_llm_json_succeeded", step="json_repair")
        except Exception:
            # json_repair rarely raises — this is belt-and-suspenders.
            pass

    if result is None and empty_span is not None:
        result = empty_span
        logger.debug("parse_llm_json_succeeded", step="extract_empty")

    if result is not None:
        if expect == "array" and not isinstance(result, list):
            result = None
        elif expect == "object" and not isinstance(result, dict):
            result = None

    if result is None and strict:
        raise InvalidInputError("Failed to parse JSON from LLM response")

    return result
