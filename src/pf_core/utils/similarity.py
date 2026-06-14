"""Text similarity helpers for near-duplicate detection.

Provides character-level shingling and Jaccard similarity for fast,
dependency-free duplicate detection. Useful for catching LLM-generated
content that closely mirrors existing records.

Usage::

    from pf_core.utils.similarity import is_near_duplicate, shingle, jaccard

    if is_near_duplicate(draft_text, existing_text):
        print("Too similar — likely a duplicate")

    # Or use the primitives directly:
    sim = jaccard(shingle(text_a), shingle(text_b))
"""

from __future__ import annotations


def shingle(text: str, *, k: int = 4) -> set[str]:
    """Return character *k*-shingles (k-grams) of *text*.

    Args:
        text: Input string.
        k: Shingle size. Larger values are more specific; smaller values
           are more forgiving of minor edits.

    Returns:
        Set of all *k*-character substrings. Strings shorter than *k*
        return a set containing the full string.
    """
    return {text[i : i + k] for i in range(max(len(text) - k + 1, 1))}


def jaccard(a: set, b: set) -> float:
    """Jaccard similarity coefficient between two sets.

    Args:
        a: First set (typically shingle output).
        b: Second set.

    Returns:
        Float in ``[0.0, 1.0]``. Returns ``0.0`` when both sets are empty.
    """
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def is_near_duplicate(
    text_a: str,
    text_b: str,
    *,
    k: int = 4,
    threshold: float = 0.75,
) -> bool:
    """Return ``True`` if *text_a* and *text_b* exceed a similarity threshold.

    Convenience wrapper around :func:`shingle` and :func:`jaccard`.

    Args:
        text_a: First text.
        text_b: Second text.
        k: Shingle size passed to :func:`shingle`.
        threshold: Jaccard similarity must be ``>=`` this value to be
            considered a near-duplicate.

    Returns:
        ``True`` if the texts are near-duplicates.
    """
    return jaccard(shingle(text_a, k=k), shingle(text_b, k=k)) >= threshold
