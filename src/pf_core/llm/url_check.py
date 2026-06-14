"""Pluggable URL hallucination checks.

LLMs frequently fabricate plausible-looking URLs. This module provides a
generic dispatcher; callers supply their own rules describing what a
hallucinated URL looks like in their domain. pf-core does not ship any
publisher-specific rules — those are project-owned (see each consumer's
rule module).

A rule is any ``Callable[[str], str | None]`` that returns a short reason
when the URL matches its hallucination pattern, or ``None`` when it looks
plausible.

Usage::

    from pf_core.llm.url_check import UrlHallucinationRule, url_looks_hallucinated

    def _flag_date_keyword_slug(url: str) -> str | None:
        if url.endswith("-2025") and "/news/" in url:
            return "date-keyword slug"
        return None

    rules: list[UrlHallucinationRule] = [_flag_date_keyword_slug]
    reason = url_looks_hallucinated("https://example.com/news/story-2025", rules)
"""

from __future__ import annotations

from typing import Callable

UrlHallucinationRule = Callable[[str], str | None]


def url_looks_hallucinated(
    url: str,
    rules: list[UrlHallucinationRule],
) -> str | None:
    """Return the first non-None reason from *rules*, or ``None``.

    Args:
        url: URL to check.
        rules: Ordered list of rule callables. Each receives the URL and
            returns either a short reason string (match) or ``None`` (pass).
            First match wins.

    Returns:
        Reason string from the first matching rule, or ``None`` if every
        rule passed (including the empty-rules case).
    """
    for rule in rules:
        reason = rule(url)
        if reason:
            return reason
    return None


def validate_urls(
    urls: list[str],
    rules: list[UrlHallucinationRule],
) -> list[tuple[str, bool, str | None]]:
    """Check *urls* against *rules*.

    Args:
        urls: URLs to validate.
        rules: Rule list passed through to :func:`url_looks_hallucinated`.

    Returns:
        List of ``(url, looks_ok, reason)`` tuples. ``looks_ok`` is ``True``
        when no rule matched; ``reason`` is ``None`` in that case.
    """
    out: list[tuple[str, bool, str | None]] = []
    for url in urls:
        reason = url_looks_hallucinated(url, rules)
        out.append((url, reason is None, reason))
    return out
