"""Built-in semantic validators — shipped out-of-the-box.

Each validator is built from a config string like ``"tier1_ratio:0.6"``. The
last token may be ``"error"``, ``"warn"``, or ``"info"`` to override the
default severity. Projects register entries via ``register(semantic=[...])``.

Available validators:

* ``url_sanity`` — flag LLM-hallucinated URLs (pattern-based, no network)
* ``tier1_ratio:<ratio>`` — at least <ratio> of URLs are tier-1 domains
* ``field_non_empty:<f1>,<f2>,...`` — named string fields non-empty after strip
* ``min_items:<field>:<n>`` — named list has ≥ n items
* ``no_duplicate_urls`` — URLs across all fields are unique
* ``date_range:<field>:<start>:<end>`` — ISO dates within range (``today`` allowed)
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable

from pf_core.exceptions import ConfigurationError
from pf_core.llm.url_check import UrlHallucinationRule, url_looks_hallucinated
from pf_core.llm.validate._pipeline import ValidationSignal

_VALID_SEVERITIES = frozenset({"info", "warn", "error"})

# Hook: project supplies the tier-1 domain list. Empty default → tier1_ratio
# passes trivially when no domains are configured.
def _tier1_hook_default() -> set[str]:
    return set()


_TIER1_HOOK: Callable[[], set[str]] = _tier1_hook_default


# Hook: project supplies the URL hallucination rule set. Empty default →
# url_sanity passes trivially when no rules are registered.
def _url_rules_hook_default() -> list[UrlHallucinationRule]:
    return []


_URL_RULES_HOOK: Callable[[], list[UrlHallucinationRule]] = _url_rules_hook_default


def register_tier1_domains(hook: Callable[[], set[str]]) -> None:
    """Register a callable that returns the project's tier-1 domain set.

    The hook is called once per validation (cheap) so the set can be
    reloaded from config without restart. Domains should be bare hostnames
    (e.g. ``"example.com"``), matched as suffixes.
    """
    global _TIER1_HOOK
    _TIER1_HOOK = hook


def register_url_hallucination_rules(
    hook: Callable[[], list[UrlHallucinationRule]],
) -> None:
    """Register a callable that returns the project's URL hallucination rules.

    Rules are ``Callable[[str], str | None]`` — each returns a short reason
    when a URL matches a hallucination pattern, or ``None`` otherwise. The
    hook is called once per ``url_sanity`` signal (cheap) so rules can be
    swapped at runtime. If no hook is registered, ``url_sanity`` passes
    trivially.
    """
    global _URL_RULES_HOOK
    _URL_RULES_HOOK = hook


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------

def _split_spec(spec: str) -> tuple[str, list[str], str]:
    """Split a semantic-validator spec into ``(name, args, severity)``.

    Severity override is the last token iff it's one of ``info/warn/error``.
    """
    parts = spec.split(":")
    name = parts[0]
    tail = parts[1:]
    severity = "warn"
    if tail and tail[-1] in _VALID_SEVERITIES:
        severity = tail[-1]
        tail = tail[:-1]
    return name, tail, severity


def build_semantic_validator(spec: str) -> Callable:
    """Build a callable semantic validator from its config string."""
    name, args, severity = _split_spec(spec)
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ConfigurationError(
            f"unknown semantic validator '{name}'. Known: {sorted(_BUILDERS)}"
        )
    fn = builder(args, severity)
    fn.name = name  # type: ignore[attr-defined]
    return fn


# ---------------------------------------------------------------------------
# Value walking helpers
# ---------------------------------------------------------------------------

def _iter_values(value: Any):
    """Yield every leaf value inside *value*, depth-first.

    Handles Pydantic models (via ``model_dump``), dicts, lists, tuples.
    """
    try:
        from pydantic import BaseModel
    except ImportError:  # pragma: no cover
        BaseModel = None  # type: ignore[assignment]

    if BaseModel is not None and isinstance(value, BaseModel):
        value = value.model_dump(mode="python")

    if isinstance(value, dict):
        for v in value.values():
            yield from _iter_values(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_values(v)
    else:
        yield value


def _collect_urls(value: Any) -> list[str]:
    """All strings that look like HTTP URLs, recursively."""
    out: list[str] = []
    for v in _iter_values(value):
        if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
            out.append(v)
    return out


def _get_field(value: Any, name: str) -> Any:
    """Attribute-first, dict-second field access."""
    if hasattr(value, name) and not isinstance(value, dict):
        return getattr(value, name)
    if isinstance(value, dict):
        return value.get(name)
    return None


# ---------------------------------------------------------------------------
# Individual validators
# ---------------------------------------------------------------------------

def _build_url_sanity(args: list[str], severity: str) -> Callable:
    def _check(parsed: Any, *, context: dict) -> ValidationSignal:
        rules = _URL_RULES_HOOK()
        urls = _collect_urls(parsed)
        if not rules:
            return ValidationSignal(
                "url_sanity", "info", passed=True,
                details={"reason": "no url hallucination rules registered"},
            )
        bad = []
        for u in urls:
            reason = url_looks_hallucinated(u, rules)
            if reason:
                bad.append({"url": u, "reason": reason})
        passed = not bad
        return ValidationSignal(
            validator="url_sanity",
            severity=severity if not passed else "info",
            passed=passed,
            details={"checked": len(urls), "flagged": bad} if bad else None,
        )
    return _check


def _build_tier1_ratio(args: list[str], severity: str) -> Callable:
    if len(args) != 1:
        raise ConfigurationError(
            f"tier1_ratio expects one arg (threshold), got: {args}"
        )
    try:
        threshold = float(args[0])
    except ValueError as e:
        raise ConfigurationError(f"tier1_ratio threshold must be float: {args[0]}") from e

    def _check(parsed: Any, *, context: dict) -> ValidationSignal:
        tier1 = _TIER1_HOOK()
        urls = _collect_urls(parsed)
        if not urls:
            return ValidationSignal(
                "tier1_ratio", "info", passed=True,
                details={"reason": "no urls to evaluate"},
            )
        if not tier1:
            return ValidationSignal(
                "tier1_ratio", "info", passed=True,
                details={"reason": "no tier1 domain hook registered"},
            )
        def _is_tier1(u: str) -> bool:
            try:
                host = u.split("://", 1)[1].split("/", 1)[0].lower()
            except IndexError:
                return False
            return any(host == d or host.endswith("." + d) for d in tier1)

        hits = sum(1 for u in urls if _is_tier1(u))
        ratio = hits / len(urls)
        passed = ratio >= threshold
        return ValidationSignal(
            validator="tier1_ratio",
            severity=severity if not passed else "info",
            passed=passed,
            details={"ratio": round(ratio, 3), "threshold": threshold, "hits": hits, "total": len(urls)},
        )
    return _check


def _build_field_non_empty(args: list[str], severity: str) -> Callable:
    if len(args) != 1:
        raise ConfigurationError(
            f"field_non_empty expects one comma-separated arg, got: {args}"
        )
    fields = [f.strip() for f in args[0].split(",") if f.strip()]
    if not fields:
        raise ConfigurationError("field_non_empty requires at least one field name")

    def _check(parsed: Any, *, context: dict) -> ValidationSignal:
        missing = []
        for f in fields:
            v = _get_field(parsed, f)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(f)
        passed = not missing
        return ValidationSignal(
            validator="field_non_empty",
            severity=severity if not passed else "info",
            passed=passed,
            details={"empty_fields": missing, "checked": fields} if missing else None,
        )
    return _check


def _build_min_items(args: list[str], severity: str) -> Callable:
    if len(args) != 2:
        raise ConfigurationError(
            f"min_items expects two args (field:n), got: {args}"
        )
    field_name = args[0]
    try:
        n = int(args[1])
    except ValueError as e:
        raise ConfigurationError(f"min_items count must be int: {args[1]}") from e

    def _check(parsed: Any, *, context: dict) -> ValidationSignal:
        v = _get_field(parsed, field_name)
        if not isinstance(v, (list, tuple)):
            return ValidationSignal(
                "min_items", severity, passed=False,
                details={"field": field_name, "reason": "not a list", "value_type": type(v).__name__},
            )
        actual = len(v)
        passed = actual >= n
        return ValidationSignal(
            validator="min_items",
            severity=severity if not passed else "info",
            passed=passed,
            details={"field": field_name, "actual": actual, "minimum": n},
        )
    return _check


def _build_no_duplicate_urls(args: list[str], severity: str) -> Callable:
    def _check(parsed: Any, *, context: dict) -> ValidationSignal:
        urls = _collect_urls(parsed)
        seen: dict[str, int] = {}
        for u in urls:
            seen[u] = seen.get(u, 0) + 1
        dupes = {u: c for u, c in seen.items() if c > 1}
        passed = not dupes
        return ValidationSignal(
            validator="no_duplicate_urls",
            severity=severity if not passed else "info",
            passed=passed,
            details={"duplicates": dupes} if dupes else None,
        )
    return _check


def _parse_date_token(token: str) -> _dt.date:
    if token == "today":
        return _dt.date.today()
    return _dt.date.fromisoformat(token)


def _build_date_range(args: list[str], severity: str) -> Callable:
    if len(args) != 3:
        raise ConfigurationError(
            f"date_range expects three args (field:start:end), got: {args}"
        )
    field_name, start_tok, end_tok = args
    try:
        start = _parse_date_token(start_tok)
        # ``today`` is dynamic — capture the token, resolve per-call
    except ValueError as e:
        raise ConfigurationError(f"date_range start invalid: {start_tok}") from e

    def _resolve_end() -> _dt.date:
        return _parse_date_token(end_tok)

    def _check(parsed: Any, *, context: dict) -> ValidationSignal:
        raw = _get_field(parsed, field_name)
        if raw is None:
            return ValidationSignal(
                "date_range", severity, passed=False,
                details={"field": field_name, "reason": "missing"},
            )
        if isinstance(raw, _dt.date) and not isinstance(raw, _dt.datetime):
            d = raw
        elif isinstance(raw, _dt.datetime):
            d = raw.date()
        else:
            try:
                d = _dt.date.fromisoformat(str(raw)[:10])
            except ValueError:
                return ValidationSignal(
                    "date_range", severity, passed=False,
                    details={"field": field_name, "reason": "not an ISO date", "value": str(raw)},
                )
        end = _resolve_end()
        passed = start <= d <= end
        return ValidationSignal(
            validator="date_range",
            severity=severity if not passed else "info",
            passed=passed,
            details={
                "field": field_name,
                "value": d.isoformat(),
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
    return _check


_BUILDERS: dict[str, Callable[[list[str], str], Callable]] = {
    "url_sanity": _build_url_sanity,
    "tier1_ratio": _build_tier1_ratio,
    "field_non_empty": _build_field_non_empty,
    "min_items": _build_min_items,
    "no_duplicate_urls": _build_no_duplicate_urls,
    "date_range": _build_date_range,
}
