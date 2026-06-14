# LLM URL Check

Pluggable dispatcher for detecting LLM-hallucinated URLs. The framework ships the dispatcher only — **consumers own the rules**. pf-core contains no publisher-specific regexes or domain knowledge.

## Rule shape

A rule is any `Callable[[str], str | None]`. It receives a URL and returns a short reason string when the URL matches a hallucination pattern, or `None` when it looks plausible.

```python
from pf_core.llm.url_check import UrlHallucinationRule

def flag_apnews_keyword_year(url: str) -> str | None:
    import re
    if re.search(r"apnews\.com/article/[a-z][a-z-]+-\d{4}$", url):
        return "AP News keyword-year slug (real AP URLs use hex hashes)"
    return None

rules: list[UrlHallucinationRule] = [flag_apnews_keyword_year]
```

## Checking a single URL

```python
from pf_core.llm.url_check import url_looks_hallucinated

reason = url_looks_hallucinated(
    "https://apnews.com/article/fake-story-2025",
    rules=rules,
)
if reason:
    print(f"Likely hallucinated: {reason}")
```

Returns the reason from the first matching rule, or `None` if every rule passes (including when `rules=[]`).

## Batch validation

```python
from pf_core.llm.url_check import validate_urls

results = validate_urls(
    ["https://example.com/real-page", "https://apnews.com/article/fake-2025"],
    rules=rules,
)
for url, looks_ok, reason in results:
    if not looks_ok:
        print(f"  {url}: {reason}")
```

Returns `list[tuple[str, bool, str | None]]`.

## Integration with `url_sanity` validator

The built-in `url_sanity` semantic validator (see `llm-schema-validation.md`) delegates to a consumer-registered rule set:

```python
from pf_core.llm.validate import register_url_hallucination_rules

def _project_rules():
    return [flag_apnews_keyword_year, ...]  # consumer rules

register_url_hallucination_rules(_project_rules)
```

If no hook is registered, `url_sanity` passes trivially with `details={"reason": "no url hallucination rules registered"}`.

## Functions

### `url_looks_hallucinated(url, rules)`

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | `str` | URL to check |
| `rules` | `list[UrlHallucinationRule]` | Ordered rule list — first match wins |

Returns `str | None`.

### `validate_urls(urls, rules)`

| Parameter | Type | Description |
|-----------|------|-------------|
| `urls` | `list[str]` | URLs to check |
| `rules` | `list[UrlHallucinationRule]` | Rule list forwarded to `url_looks_hallucinated` |

Returns `list[tuple[str, bool, str | None]]`.

### Type aliases

- `UrlHallucinationRule = Callable[[str], str | None]`

## Related

- [URL Utilities](urls.md) — `domain_of`, `archive_timestamp_is_round` (general-purpose URL helpers that consumer rules commonly wrap)
- [LLM Schema Validation](llm-schema-validation.md) — for the `url_sanity` semantic validator integration
