# Parsers

`pf_core.parsers` holds framework-level pieces for content-ingest pipelines — the kind of consumer code that fetches from external sources (RSS feeds, sitemaps, web archives) and hands structured records to an orchestrator for downstream processing.

The package is intentionally small. Per-source parser modules and the parser-to-orchestrator data classes (`PostRef`, `Post`, link extractors) live in consumers until a second consumer needs the same shape — see "Lift policy" below.

## What's here today

### Exception types

```python
from pf_core.parsers import ParseError, PaywalledPost
```

Two exception classes form the contract between per-source parser modules and the consumer's ingest orchestrator. Both subclass `pf_core.exceptions.AppError` so they participate in the standard `log_exception()` flow.

| Exception | When to raise | Orchestrator response |
|---|---|---|
| `ParseError` | Network failure, shape mismatch, empty body — anything that means "this source failed for this run" | Skip the source, log a WARNING, increment a `parser_errors` counter. A single source failing should not fail the whole ingest. |
| `PaywalledPost` | A specific post is paid-only and cannot be extracted | Skip the post, increment `posts_skipped_paywalled`, log at WARNING. Distinct from `ParseError` so paywall skips don't pollute the parser-health metric. |

Individual parsers may subclass these for source-specific signals; orchestrators only need to catch the two base classes (and a generic `Exception` fallback).

### HTML body extractor

```python
from pf_core.parsers import parse_body_html, BodyExtractor, PostLink

text, links = parse_body_html(post_html)
# text: str — paragraphs preserved, blank lines collapsed
# links: list[PostLink] — each with url + anchor_text + surrounding_text
```

Pure-stdlib (`html.parser.HTMLParser`) walker that turns post body HTML into a normalized plain-text rendering plus a list of inline links with surrounding-text context. Designed for the content-ingest pattern: the LLM gets clean prose for record extraction and per-link context for "which event does this URL back?" disambiguation.

| Symbol | Purpose |
|---|---|
| `parse_body_html(html, *, context_window_chars=120) -> (text, links)` | High-level entry point. Raises `ParseError` if the parser crashes; empty input returns `("", [])`. |
| `BodyExtractor` | The `HTMLParser` subclass. Subclass for custom tag handling; expose `text_parts` + `link_records` after `feed()`+`close()`. |
| `normalize_plain_text(text) -> str` | Strip leading/trailing whitespace; collapse 3+ consecutive blank lines to one. Idempotent. |
| `BLOCK_TAGS` | `frozenset` of tags whose open/close emits a paragraph break in the buffer (`p`, `div`, `li`, headers, `blockquote`, …). |
| `SKIP_TAGS` | `frozenset` of tags whose inner text is dropped (`script`, `style`). |
| `DEFAULT_CONTEXT_WINDOW_CHARS` | Default ± window for `PostLink.surrounding_text` (120). |
| `PostLink` (`pf_core.parsers.types`) | Dataclass: `url`, `anchor_text`, `surrounding_text`. |

Empty-href and empty-anchor `<a>` tags are filtered automatically (many sites sprinkle these for layout). Whitespace inside `surrounding_text` is collapsed to single spaces so the LLM sees clean prose.

### Example use

```python
# In a per-source parser module:
from pf_core.parsers import ParseError, PaywalledPost

def fetch_post(ref):
    resp = httpx.get(ref.url)
    if resp.status_code != 200:
        raise ParseError(f"fetch failed: {resp.status_code}", context={"url": ref.url})
    if "subscribe to read" in resp.text.lower():
        raise PaywalledPost(ref.url)
    return _parse(resp.text)


# In the orchestrator:
from pf_core.parsers import ParseError, PaywalledPost

for source in sources:
    try:
        posts = parser.list_posts(source, since, until)
    except ParseError as e:
        log_exception(e, message_prepend="parser failed, skipping source")
        continue
    for ref in posts:
        try:
            post = parser.fetch_post(ref)
        except PaywalledPost:
            paywalled_count += 1
            continue
        except ParseError as e:
            parse_errors += 1
            log_exception(e)
            continue
        # ... handle `post` ...
```

## Lift policy

The `pf_core.parsers` surface grows by extraction, not by speculation. New entries land here only when:

1. **A second consumer needs the same code.** The first consumer keeps its implementation local; the second triggers the lift. Today's contents (the two exception types) hit that bar via a content-ingest pipeline plus the framework's general-purpose error hierarchy — they're universal enough to belong here from day one.
2. **The contract has stabilized.** Whatever moves up should not need versioning churn for at least 90 days of two consumers exercising it. Premature lift forces a generic abstraction that has not yet seen real use.
3. **The piece is policy-free.** Anything that encodes a specific consumer's source-ranking rules, source list, or business logic stays in the consumer. Parsers belong here; "this is a Tier-1 source" does not.

Today's deliberate non-goals:

- **No per-source parser modules** (per-platform RSS/HTML, etc.) — those have one consumer today; they stay there until a second project needs the same content ingest.
- **No `PostRef` / `Post` data classes** — these are the parser→orchestrator handoff shape. A different consumer's orchestrator will likely want a different shape; lifting forces a generic abstraction with one real user.
- **No content-ingest framework** — the orchestration shape is consumer policy.

When the next piece is ready to lift, follow the `parsers.md` contract: small, explicit, documented here.
