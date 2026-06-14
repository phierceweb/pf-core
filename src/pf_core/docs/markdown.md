# Safe Markdown Rendering

Renders a controlled subset of markdown to sanitized HTML using an escape-first approach. All text is HTML-escaped, then safe markup is selectively applied. No sanitizer library needed.

## Supported syntax

| Markdown | HTML output |
|----------|-------------|
| `**bold**` | `<strong>bold</strong>` |
| `*italic*` | `<em>italic</em>` |
| `` `code` `` | `<code>code</code>` |
| `[label](url)` | `<a href="url" rel="nofollow noopener" target="_blank">label</a>` |
| `# Heading` | `<h3>Heading</h3>` (offset configurable) |
| `- item` or `* item` | `<ul><li>item</li></ul>` |
| `1. item` | `<ol><li>item</li></ol>` |
| Blank line | Paragraph break |

Links support nested parentheses in URLs (e.g. Wikipedia links).

## Direct usage

```python
from pf_core.web.markdown import safe_markdown

html = safe_markdown("**bold** and *italic*")
# Markup("<p><strong>bold</strong> and <em>italic</em></p>")

html = safe_markdown(None)  # Markup("")
html = safe_markdown("")    # Markup("")
```

## Jinja2 filter

```python
from pf_core.web.markdown import setup_markdown_filter

setup_markdown_filter(templates)
```

Then in templates:

```html
<div>{{ section.summary | markdown }}</div>
```

## Parameters

### `safe_markdown(text, *, extra_transforms=None, heading_offset=2)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | `str \| None` | (required) | Raw markdown text |
| `extra_transforms` | `list[Callable] \| None` | `None` | Functions applied to escaped text before inline transforms |
| `heading_offset` | `int` | `2` | Added to heading level (`#` → `<h3>` by default) |

### `setup_markdown_filter(templates, *, filter_name="markdown", extra_transforms=None, heading_offset=2)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `templates` | `Jinja2Templates` | (required) | Templates instance to register the filter on |
| `filter_name` | `str` | `"markdown"` | Filter name in templates |
| `extra_transforms` | `list[Callable] \| None` | `None` | Passed through to `safe_markdown()` |
| `heading_offset` | `int` | `2` | Passed through to `safe_markdown()` |

## Extra transforms

For project-specific inline patterns (e.g. converting entry IDs to links), pass `extra_transforms`. Each callable receives HTML-escaped text and returns transformed text. They run before the standard inline transforms (links, bold, italic, code).

```python
import re

_ENTRY_ID = re.compile(r"\b([A-Za-z0-9_-]{8,14})\b")

def linkify_entry_ids(text: str) -> str:
    return _ENTRY_ID.sub(r'<a href="/event/\1">\1</a>', text)

setup_markdown_filter(templates, extra_transforms=[linkify_entry_ids])
```

## Security

The escape-first approach means:

- All input is HTML-escaped via `markupsafe.escape()` before any processing
- Only the specific markdown patterns above produce HTML tags
- No raw HTML passes through — `<script>` becomes `&lt;script&gt;`
- Links get `rel="nofollow noopener" target="_blank"` automatically
- No external sanitizer library is required

## Migrating from consumer projects

**Example consumer** — replace the hand-rolled `_md_links` / `_inline_md` in `app/api/_templates.py`:

```python
# Before
from markupsafe import Markup, escape
# ... 100+ lines of hand-rolled markdown rendering

templates.env.filters["md_links"] = _md_links

# After
from pf_core.web.markdown import setup_markdown_filter

setup_markdown_filter(
    templates,
    filter_name="md_links",  # keep existing filter name
    extra_transforms=[_replace_entry_links, _replace_bare_entry_ids],
)
```

The consumer-specific transforms (entry ID links, bare ID citations) stay in the consumer project as `extra_transforms`.
