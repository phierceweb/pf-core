"""Parser failure shapes shared by all consumer parsers.

Two exception types form the contract between per-source parser modules
and the consumer's ingest orchestrator. Individual parsers may subclass
them for source-specific signals; orchestrators only ever catch these
two (and a generic ``Exception`` fallback).

The split exists so paywall skips do not pollute parser-health metrics:
:class:`ParseError` counts toward ``parser_errors``;
:class:`PaywalledPost` is a soft signal that a post is paid-only and
should be skipped without counting as a failure.
"""

from __future__ import annotations

from pf_core.exceptions import AppError


class ParseError(AppError):
    """Unrecoverable parser failure (network, shape mismatch, empty body).

    Orchestrator callers catch this and skip the source for this run
    while logging a WARNING; a single source failing does not fail the
    whole ingest. Counts toward ``parser_errors`` and triggers the
    parser-health signal.
    """


class PaywalledPost(AppError):
    """Soft signal from ``fetch_post``: this post is paid-only and should
    be skipped without counting as a parser error.

    Distinct from :class:`ParseError` so paywall skips don't pollute the
    parser-health metric. Orchestrator catches per-post, increments a
    paywalled-skip counter, and logs at WARNING.
    """
