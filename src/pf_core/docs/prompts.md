# Prompt Loader

Load prompt templates from YAML files and render them with variable substitution. Standardizes where prompts live and how they're loaded across projects.

> **Install:** imports on the base install. It depends only on pyyaml (a base dependency), so no extra is required — `pf_core.llm.prompts` (along with `router`, `url_check`, and `safe_apply`) imports without the client/HTTP or LLM stack.

Two YAML layouts are supported:

- **Flat multi-agent files** — one YAML listing every agent. Load with `load_prompts()`; caller looks up by key.
- **Per-agent spec files** — one YAML per agent with a required schema carrying `agent`, `version`, and `system`. Load with `load_prompt_spec()` + render with `render_spec()` to get a `(text, version)` tuple. Recommended for apps with many agents or long prompts that benefit from dedicated version tracking.

For DB-backed version tracking, pair the spec loader with `pf_core.llm.tracking.resolve_prompt_id()` — see below.

## Loading prompts

```python
from pf_core.llm.prompts import load_prompts

prompts = load_prompts("config/prompts.yaml")
```

Returns the parsed YAML as a dict. The expected structure is one key per prompt group, each with `system` and `user` sub-keys:

```yaml
# config/prompts.yaml
summarize:
  system: |
    You are a summarizer. Summarize the text.
    Output only valid JSON with these keys: ...
  user: |
    Max words: {max_words}
    Text: {text}

classify:
  system: |
    You classify text into a category.
  user: |
    Category options: {category}
    Text: {text}
```

### Raises

`ConfigurationError` if the file is missing, unparseable, or not a YAML mapping.

## Rendering templates

Two placeholder styles are supported. Choose based on what your prompts contain.

### Brace style (default)

Uses Python's `str.format_map` with `{variable}` placeholders. Literal curly braces must be escaped as `{{` and `}}`.

**Best for:** Simple prompts without JSON or code examples.

```python
from pf_core.llm.prompts import render

system = render(prompts["summarize"]["system"])
user = render(prompts["summarize"]["user"], text=text, max_words=50)

# Escaped braces for literal JSON in the template
render('Output JSON: {{"key": "{val}"}}', val="hello")
# → 'Output JSON: {"key": "hello"}'
```

### Token style (`@@VARIABLE@@`)

Uses plain string replacement with `@@VARIABLE@@` placeholders. No escaping needed for curly braces.

**Best for:** Prompts that contain JSON examples, code blocks, or other text heavy with `{` and `}`.

```python
from pf_core.llm.prompts import render

system = render(
    'You analyze @@SUBJECT@@. Output: {"label": "..."}',
    style="@@",
    SUBJECT="text",
)
# → 'You analyze text. Output: {"label": "..."}'

# No escaping needed — curly braces pass through untouched
render(
    '{"role": "@@ROLE@@", "items": []}',
    style="@@",
    ROLE="classifier",
)
# → '{"role": "classifier", "items": []}'
```

### Shared behavior

Both styles:
- Raise `InvalidInputError` if the template references a variable not provided
- Silently ignore extra variables not referenced in the template
- Convert non-string values to strings automatically

## Parameters

### `load_prompts(path)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | `str \| Path` | (required) | Path to YAML file |

### `render(template, *, style="brace", **variables)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `template` | `str` | (required) | Template with placeholders |
| `style` | `str` | `"brace"` | `"brace"` for `{variable}`, `"@@"` for `@@VARIABLE@@` |
| `**variables` | `Any` | — | Values to substitute |

## When to use which style

| Scenario | Style | Why |
|----------|-------|-----|
| Simple prompts (summarize, classify) | `"brace"` | Familiar Python syntax, concise |
| Prompts with JSON examples | `"@@"` | No need to escape every `{` and `}` |
| Prompts with code blocks | `"@@"` | Code uses braces freely |
| Config-driven prompt builders | `"@@"` | Builders compose strings that may contain braces |

## YAML conventions

- Flat layout: one file per project: `config/prompts.yaml`
- Per-agent layout: one file per agent under `config/prompts/<agent>.yaml`
- Each prompt has `system` and optionally `user` keys (matching OpenRouter/OpenAI message roles)
- Use YAML block scalars (`|`) for multi-line prompts
- Variables use `{name}` or `@@NAME@@` syntax — document them in YAML comments
- With brace style, escape literal braces as `{{` and `}}`

## Per-agent spec files (recommended for large apps)

### The schema

```yaml
# config/prompts/summarizer.yaml
agent: summarizer                  # required; must match filename
version: 7                         # required; ≥ 1; bump on material change
system: |                          # required; the system prompt text
  You are a summarizer. Summarize the text.
  ...
user: |                            # optional; per-call user template
  Max words: {max_words}
  Text: {text}
changelog:                         # optional; human-readable history
  - "v1:Mar30 initial"
  - "v7:Apr21 tightened output format"
placeholders:                      # optional; documents the named slots
  - max_words
  - text
```

### Loading + rendering

```python
from pf_core.llm.prompts import load_prompt_spec, render_spec

spec = load_prompt_spec("config/prompts/summarizer.yaml", expected_agent="summarizer")
system_text, version = render_spec(spec)
# → ("You are a summarizer. Summarize the text.\n...", 7)
```

`render_spec()` returns a `(text, version)` tuple — pass both to your agent-run logger in one step:

```python
db.log_agent_run(
    job_id, "summarizer", model,
    prompt_version=version,
    system_prompt=system_text,
    rendered_prompts=(system_text, user_text),
    ...
)
```

### Registering in the DB

Use `resolve_prompt_id()` to upsert the prompt into `llm_prompts` so every `(agent, part, version)` triple has a canonical DB row:

```python
from pf_core.llm.tracking import resolve_agent_type_id, resolve_prompt_id

agent_id = resolve_agent_type_id("summarizer")
prompt_id = resolve_prompt_id(
    agent_type_id=agent_id,
    part="system",
    version=version,
    content=system_text,
)
# prompt_id → foreign key you can store on llm_runs.system_prompt_id
```

### Policy: what happens when content changes mid-version

`resolve_prompt_id(on_change=...)` controls behavior when the row already exists but `content` differs from the stored text:

| Policy | Behavior | When to use |
|--------|----------|-------------|
| `"keep_first"` (default) | Silently reuse the first-seen text. | You manage versions deliberately; bump the YAML version when text changes. |
| `"update_unused"` | If no `llm_runs` references the prompt, update in place. If any run does, INSERT a new row at `MAX(version)+1`. | You want automatic version tracking from text edits. |
| `"error"` | Raise `ValueError`. | CI safety net — catches "edited prompt but forgot to bump version." |

### Parameters

#### `load_prompt_spec(path, *, expected_agent=None)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | `str \| Path` | (required) | Path to per-agent YAML file |
| `expected_agent` | `str \| None` | `None` | If set, raises `ConfigurationError` when the file's `agent:` key doesn't match |

Returns the validated dict (at least `agent`, `version`, `system`).

#### `render_spec(spec, *, part="system", style="brace", **variables)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `spec` | `dict` | (required) | Spec returned by `load_prompt_spec` |
| `part` | `str` | `"system"` | Which section to render (`"system"`, `"user"`, …) |
| `style` | `str` | `"brace"` | Passed through to `render()` |
| `**variables` | `Any` | — | Substitution context |

Returns `(rendered_text, version)`.

#### `resolve_prompt_id(*, agent_type_id, part, version, content, on_change="keep_first")`

| Parameter | Type | Description |
|-----------|------|-------------|
| `agent_type_id` | `int` | Resolved via `resolve_agent_type_id(slug)` |
| `part` | `str` | `"system"`, `"user"`, or `"full"` |
| `version` | `int` | ≥ 1 — caller-authored cohort label |
| `content` | `str` | Prompt text to register |
| `on_change` | `str` | `"keep_first"` \| `"update_unused"` \| `"error"` |

Returns the `llm_prompts.id`, or `None` when `content` is empty.

## Migrating a project

**Replacing a hand-rolled loader** — swap a local `load_prompts()` in your services:

```python
# Before
import yaml
def load_prompts() -> dict:
    path = project_root() / "config" / "prompts.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

# After
from pf_core.llm.prompts import load_prompts
prompts = load_prompts("config/prompts.yaml")
```

**Replacing a hand-rolled injector** — swap a `.replace()`-based prompt injector in your project:

```python
# Before
def _inject_domain(text: str) -> str:
    return (
        text
        .replace("@@APP_NAME@@", cfg.APP_NAME)
        .replace("@@START_DATE@@", str(START_DATE))
        # ... more .replace() calls
    )

# After
from pf_core.llm.prompts import render

def _inject_domain(text: str) -> str:
    return render(
        text,
        style="@@",
        APP_NAME=cfg.APP_NAME,
        START_DATE=str(START_DATE),
        # ... all variables as keyword args
    )
```
