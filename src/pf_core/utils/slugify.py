"""ASCII slug generation from free text.

``slugify`` folds a display string ("São Paulo", "Crème brûlée") to a
stable lowercase ASCII slug ("sao-paulo", "creme-brulee") for filenames,
ids, and URL fragments. Distinct from ``pf_core.utils.vocab``: that maps
free text onto a *known* controlled vocabulary; ``slugify`` generates a
slug from arbitrary text.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["slugify"]

# Letters NFKD can't decompose to ASCII. Consumers persist slugify output as
# durable keys, so any change to this map (or the fold steps) changes
# existing slugs — a behavior change, never a silent patch.
_SPECIAL = {
    "ø": "o", "å": "a", "æ": "ae", "œ": "oe", "ð": "d", "þ": "th", "ł": "l", "ß": "ss",
}


def slugify(text: str, *, sep: str = "-") -> str:
    """Fold *text* to a lowercase ASCII slug.

    Steps: strip + lowercase, map special letters (ø→o, ß→ss, …),
    NFKD-decompose and drop combining marks, drop remaining non-ASCII,
    collapse every non-alphanumeric run to one ``sep``, trim leading and
    trailing separators.

    Args:
        text: Free text to slug. Text with no ASCII alphanumerics (including
            ``""``) returns ``""`` — the caller owns the empty-slug fallback.
        sep: Separator between word runs (default ``"-"``).

    Returns:
        The slug, e.g. ``slugify("Crème brûlée") == "creme-brulee"``.
    """
    s = text.strip().lower()
    s = "".join(_SPECIAL.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", sep, s).strip(sep)
