"""Controlled-vocabulary slug normalization.

Maps free-text strings (typically from an LLM) to a project-specific
controlled-vocabulary slug. Useful when a downstream column or filter
requires a canonical value but the upstream producer emits descriptive
free text ("executive order" / "Social Media Post" / "court order").

Three lookup paths in priority order:

  1. Pass-through — input already in the canonical slug set.
  2. Synonym lookup — input is a known free-text variant.
  3. Explicit reject — input is a category that should drop the row
     entirely (e.g. "news report" / "market reaction" / "public
     statement" in a catalog of *actions*). Returned as ``None``.

Anything else returns ``None``. The caller decides what ``None`` means
in their domain (skip the row, raise a validation error, fall back to
"other", etc.).

Lookups are case-insensitive and whitespace-normalized. The vocabulary
itself is project-specific; pf-core only owns the lookup machinery. See
``vocab.md`` for full design notes and a worked example.

Usage::

    from pf_core.utils.vocab import SlugNormalizer

    normalizer = SlugNormalizer(
        canonical_slugs={"eo", "memo", "post", "court_ruling"},
        synonyms={
            "executive order": "eo",
            "social media post": "post",
            "court order": "court_ruling",
        },
        explicit_rejects={
            "public statement", "news report", "market reaction",
        },
    )

    normalizer.normalize("Executive Order")     # "eo"
    normalizer.normalize("court_ruling")        # "court_ruling"
    normalizer.normalize("public statement")    # None  (explicit reject)
    normalizer.normalize("kerfuffle")           # None  (unknown)
    normalizer.is_explicit_reject("news report")  # True
    normalizer.is_explicit_reject("kerfuffle")    # False
"""

from __future__ import annotations


def _key(raw: str | None) -> str:
    """Normalize whitespace + case for lookup. Empty/None → empty string."""
    if not raw:
        return ""
    return " ".join(str(raw).strip().lower().split())


class SlugNormalizer:
    """Normalize free-text values to a controlled-vocabulary slug.

    Args:
        canonical_slugs: The set of valid slugs (the canonical
            vocabulary). Inputs already in this set pass through
            unchanged.
        synonyms: Mapping of free-text variant → canonical slug. Both
            keys and values are case-insensitive on the keys side; values
            are returned as-given. Synonym values pointing at slugs not
            in ``canonical_slugs`` are still returned (the class does not
            enforce that synonyms map to known slugs — caller is trusted
            to keep the two in sync).
        explicit_rejects: Free-text values that mean "drop this row" —
            categories that should never normalize to anything. Distinct
            from "unknown" (also returns ``None``) because callers may
            want to distinguish "the producer named a non-action
            category we explicitly reject" from "the producer named
            something we don't recognize". Use
            :meth:`is_explicit_reject` to disambiguate.
    """

    def __init__(
        self,
        *,
        canonical_slugs: set[str],
        synonyms: dict[str, str] | None = None,
        explicit_rejects: set[str] | None = None,
    ) -> None:
        self.canonical_slugs: set[str] = {s.lower() for s in canonical_slugs}
        self.synonyms: dict[str, str] = {
            _key(k): v for k, v in (synonyms or {}).items() if _key(k)
        }
        self.explicit_rejects: set[str] = {
            _key(s) for s in (explicit_rejects or set()) if _key(s)
        }

    def normalize(self, raw: str | None) -> str | None:
        """Map *raw* to a canonical slug, or ``None``.

        Returns:
            - The matching canonical slug if *raw* is a known canonical
              slug or a known synonym.
            - ``None`` if *raw* is empty, an explicit reject, or unknown.

        The function does not raise. Callers that want strict failure
        should check the return value explicitly.
        """
        key = _key(raw)
        if not key:
            return None

        # Already canonical (case-insensitive)
        if key in self.canonical_slugs:
            return key

        # Tolerate "court ruling" → "court_ruling" when the canonical
        # slug uses underscores.
        underscored = key.replace(" ", "_")
        if underscored in self.canonical_slugs:
            return underscored

        # Explicit reject before synonym lookup so a project that lists
        # a category as both never silently ends up in the canonical set.
        if key in self.explicit_rejects:
            return None

        return self.synonyms.get(key)

    def is_explicit_reject(self, raw: str | None) -> bool:
        """Return True iff *raw* is in the explicit-reject set.

        Distinct from ``normalize(raw) is None``: the latter is also
        true for unknown free-text. Use this when a caller needs to
        treat "this is a category we deliberately drop" differently
        from "we don't recognize this string".
        """
        return _key(raw) in self.explicit_rejects
