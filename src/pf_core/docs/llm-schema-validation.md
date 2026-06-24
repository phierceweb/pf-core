# LLM Schema Validation

Declarative shape, semantic, and cross-field validation for parsed LLM responses, with per-agent pipelines and automatic persistence to `llm_run_validations`.

This module is **not** [`llm-validation.md`](llm-validation.md). That doc covers `pf_core.llm.url_check` — a pluggable dispatcher for URL hallucination rules (no schema, no registry, no DB integration; rules are consumer-owned). This module (`pf_core.llm.validate`) validates one parsed JSON response against a registered Pydantic model or JSON Schema, runs domain-content checks, and writes every signal as a tracked row. The two are complementary; the `url_sanity` semantic validator here delegates to the consumer-registered rule set from there.

> **Install:** the Pydantic path requires the `[validate]` extra (`pip install 'pf-core[validate]'` → `json-repair` + `pydantic`, no httpx/client stack); the JSON Schema path additionally needs `[jsonschema]`. Persisting signals to `llm_run_validations` (the "DB integration" section) needs `[tracking]`. Importing `pf_core.llm.validate` without `[validate]` raises a friendly `ImportError`.

---

## Table of Contents

- [Concepts](#concepts)
- [Quick start](#quick-start)
- [Registering a pipeline](#registering-a-pipeline)
- [Built-in semantic validators](#built-in-semantic-validators)
- [Cross-field validators](#cross-field-validators)
- [Running the pipeline](#running-the-pipeline)
- [DB integration](#db-integration)
- [Service call-site pattern](#service-call-site-pattern)
- [Query recipes](#query-recipes)
- [Adding a new semantic validator](#adding-a-new-semantic-validator)
- [Adding a new agent type pipeline](#adding-a-new-agent-type-pipeline)

---

## Concepts

The pipeline runs in three tiers. Each produces zero or more `ValidationSignal` rows; together they form the response's audit trail.

| Tier | What it checks | Default severity |
|------|----------------|------------------|
| **Shape** | JSON matches the registered Pydantic model or JSON Schema. | `error` |
| **Semantic** | Field contents are domain-valid (URLs look real, lists meet minimums, dates are in range). | `warn` |
| **Cross-field** | Invariants spanning multiple fields or external context (e.g. `word_count ≤ config.max_words`). | Validator chooses. |

**Severity drives the result.** `ValidationResult.ok` is `True` iff no signal fired with `severity="error"`. Warnings and info signals never block — they record the issue for later analytics.

**One pipeline per agent type.** Pipelines live in a process-local registry keyed by the same agent slug used by [`llm-tracking.md`](llm-tracking.md) and [`model-router.md`](model-router.md). Last registration wins. Schemas carry a `schema_version` integer, written as `schema:<agent>_v<n>` into `llm_run_tags` so analytics slice by cohort.

**Do:** keep agent slugs identical across the router YAML, `@track_run(agent_type=...)`, and `register(agent_type=...)`.

**Do not:** raise on shape failure inside the pipeline. The pipeline returns a structured result; the service decides retry, fallback, or job failure.

---

## Quick start

Define the response shape, register it once at import time, then call `parse_and_validate` from the service.

```python
# app/validators/summarizer.py
from pydantic import BaseModel, Field, HttpUrl
from pf_core.llm.validate import register

class SummaryOutput(BaseModel):
    headline: str = Field(min_length=10, max_length=200)
    body: str = Field(min_length=200)
    citations: list[HttpUrl]
    model_config = {"extra": "forbid"}

register(
    agent_type="summarizer",
    shape=SummaryOutput,
    semantic=["url_sanity", "field_non_empty:headline,body"],
    schema_version=1,
)
```

```python
# app/services/summarizer.py
from pf_core.llm.validate import parse_and_validate

def summarize_one(messages, *, run_id):
    content, _ = tracked_chat(messages=messages, **get_agent_config("summarizer"))
    result = parse_and_validate(content, agent_type="summarizer", run_id=run_id)
    if not result.ok:
        raise SummarizeError("summarizer validation failed",
                             context={"failures": [s.validator for s in result.failures]})
    return result.value  # SummaryOutput instance
```

Wire the package so registration runs at startup: `app/validators/__init__.py` imports each module (`from app.validators import summarizer, classifier  # noqa: F401`).

---

## Registering a pipeline

```python
register(
    agent_type: str,
    *,
    shape: type[BaseModel] | Validator | None = None,
    semantic: list[str] | None = None,
    cross_field: list[str] | None = None,
    schema_version: int = 1,
) -> None
```

| Argument | Description |
|----------|-------------|
| `agent_type` | Agent slug. Must match `llm_agent_types.slug`. |
| `shape` | A Pydantic `BaseModel` subclass (auto-wrapped), a pre-built `PydanticValidator`/`JsonSchemaValidator`, or any `Validator`-protocol object. `None` skips shape. |
| `semantic` | Built-in semantic validator config strings (see below). |
| `cross_field` | Names registered via `@cross_field_validator`. Raises `KeyError` at register time if unknown. |
| `schema_version` | Integer written as `schema:<agent>_v<n>` into `llm_run_tags`. Bump when the schema changes materially. |

### Pydantic shape (most common)

```python
from pydantic import BaseModel, Field
from pf_core.llm.validate import register

class ClassifyOutput(BaseModel):
    category: str = Field(pattern="^(news|opinion|analysis)$")
    confidence: float = Field(ge=0.0, le=1.0)
    model_config = {"extra": "forbid"}

register(agent_type="classifier", shape=ClassifyOutput)
```

Pass a pre-built `PydanticValidator(Model)` instance instead of the bare class when sharing one validator across agent types or unit-testing it in isolation.

### `JsonSchemaValidator`

Use this when the schema is generated from a frontend type, shipped from another language, or otherwise authored as raw JSON Schema. Requires the optional `pf-core[jsonschema]` extra.

```python
from pf_core.llm.validate import register, JsonSchemaValidator

CLASSIFY_SCHEMA = {
    "type": "object",
    "required": ["category", "confidence"],
    "additionalProperties": False,
    "properties": {
        "category": {"type": "string", "enum": ["news", "opinion", "analysis"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

register(agent_type="classifier", shape=JsonSchemaValidator(CLASSIFY_SCHEMA))
```

The schema is checked at construction via `Draft202012Validator.check_schema` — bad schemas fail loud at import time. If `pf-core[jsonschema]` is not installed, the constructor raises `ConfigurationError`.

---

## Built-in semantic validators

Each entry in `semantic=[...]` is a colon-delimited config string. The optional last token is a severity override; without it the validator's default applies.

| Name | Config syntax | What it checks |
|------|---------------|----------------|
| `url_sanity` | `url_sanity` | Every URL string passes the consumer-registered URL hallucination rules (heuristic, no network). Project supplies rules via `register_url_hallucination_rules(hook)`. With no hook, passes trivially. |
| `tier1_ratio` | `tier1_ratio:<float>` | At least the given fraction of URLs are tier-1 domains. Project supplies the domain set via `register_tier1_domains(hook)`. With no hook, passes trivially. |
| `field_non_empty` | `field_non_empty:<f1>,<f2>,...` | Named string fields are non-empty after `.strip()`. |
| `min_items` | `min_items:<field>:<n>` | Named list has at least `n` items. |
| `no_duplicate_urls` | `no_duplicate_urls` | URLs across all fields are unique. |
| `date_range` | `date_range:<field>:<start>:<end>` | Date is within `[start, end]`. Bounds accept ISO dates; `today` resolves per-call. |

### Severity overrides

Append `:error`, `:warn`, or `:info` as the final token to override the default on failure (passing signals always emit `info`):

```python
register(agent_type="summarizer", shape=SummaryOutput, semantic=[
    "url_sanity:error",            # promote URL hallucination to a blocking failure
    "tier1_ratio:0.6",             # default warn severity
    "min_items:citations:3:warn",  # explicit
])
```

### Configuring `tier1_ratio`

```python
from pf_core.llm.validate import register_tier1_domains

register_tier1_domains(lambda: {"apnews.com", "reuters.com", "npr.org"})
```

The hook is invoked once per validation, so the set can be reloaded from config without restart. Hostnames match by suffix (`apnews.com` matches `www.apnews.com`).

### Configuring `url_sanity`

```python
from pf_core.llm.validate import register_url_hallucination_rules
from pf_core.llm.url_check import UrlHallucinationRule

def _flag_apnews_keyword_year(url: str) -> str | None:
    import re
    if re.search(r"apnews\.com/article/[a-z][a-z-]+-\d{4}$", url):
        return "AP News keyword-year slug"
    return None

rules: list[UrlHallucinationRule] = [_flag_apnews_keyword_year]
register_url_hallucination_rules(lambda: rules)
```

Rules are consumer-owned (`pf-core` ships no publisher-specific rules). See [llm-validation.md](llm-validation.md) for rule authoring details. If no hook is registered, `url_sanity` passes trivially with `details={"reason": "no url hallucination rules registered"}`.

---

## Cross-field validators

Cross-field validators span multiple fields or pull data from `validation_context`. Declare them with the `@cross_field_validator` decorator; reference them by name in `register(cross_field=[...])`.

```python
from pf_core.llm.validate import cross_field_validator, ValidationSignal

@cross_field_validator("within_max_words")
def within_max_words(parsed, *, context: dict) -> ValidationSignal:
    max_words = context["summary_config"]["max_words"]
    count = parsed.word_count
    if 0 <= count <= max_words:
        return ValidationSignal("within_max_words", "info", passed=True)
    return ValidationSignal(
        "within_max_words", "error", passed=False,
        details={"count": count, "max": max_words},
    )

register(agent_type="summarizer", shape=SummaryOutput,
         cross_field=["within_max_words"])
```

The decorated function takes the validated `parsed` value (Pydantic instance when shape uses Pydantic, dict otherwise) and a keyword-only `context` dict. Return one `ValidationSignal` or a list. Pass context at call time via `parse_and_validate(..., validation_context={"summary_config": sc})`.

If a cross-field validator raises, the pipeline catches the exception, records a synthetic `error`-severity signal with `details={"exception": repr(e)}`, and continues. One bad validator never derails the rest of the pipeline.

---

## Running the pipeline

```python
parse_and_validate(
    raw_response: str,
    *,
    agent_type: str,
    run_id: int | None = None,
    validation_context: dict | None = None,
    stages: tuple[str, ...] = ("shape", "semantic", "cross_field"),
    expect: str = "any",
    missing_pipeline: Literal["raise", "fallback"] = "raise",
) -> ValidationResult
```

| Argument | Description |
|----------|-------------|
| `raw_response` | Raw LLM text. Forwarded to `parse_llm_json` for fence/prose cleanup. |
| `agent_type` | Slug for the registered pipeline. If unregistered, see `missing_pipeline`. |
| `run_id` | If set, every signal is persisted and the schema version is tagged. If `None`, runs in-memory (useful for tests and offline replay). |
| `validation_context` | Dict passed to every cross-field validator's `context=` kwarg. |
| `stages` | Stages to run. Pass `("shape",)` to skip semantic and cross-field during migration. |
| `expect` | Forwarded to `parse_llm_json`: `"any"`, `"array"`, or `"object"`. |
| `missing_pipeline` | `"raise"` (default) raises `PipelineNotRegisteredError` naming the missing slug and currently-registered agents. `"fallback"` preserves pre-0.13 behavior: emit a WARNING log and return `ok=False` with one `no_pipeline_registered` error signal. Use `"fallback"` only for generic replay tooling that legitimately expects unregistered agents. |

### `ValidationResult`

| Field | Type | Description |
|-------|------|-------------|
| `ok` | `bool` | `True` iff no signal has `severity="error"` and `passed=False`. |
| `value` | `Any \| None` | Parsed object on shape-pass (Pydantic instance for `PydanticValidator`, dict for `JsonSchemaValidator`); `None` on shape failure. |
| `signals` | `list[ValidationSignal]` | All signals — pass and fail — in pipeline order. |
| `failures` | `list[ValidationSignal]` | Subset with `passed=False, severity="error"`. |
| `warnings` | `list[ValidationSignal]` | Subset with `passed=False, severity in ("info", "warn")`. |
| `schema_version` | `int` | The registered schema version. |

### `ValidationSignal`

Fields: `validator: str` (named `{agent}_shape` for the shape stage, `{agent}_parse` when JSON extraction itself fails, bare slug otherwise), `severity: str` (`"info" | "warn" | "error"`), `passed: bool`, `details: dict | None` (failure context — Pydantic `errors()`, JSON-Schema paths, threshold values).

### What `parse_and_validate` does not do

- It does not raise on shape or content failure — it returns `ok=False`. The caller chooses the policy.
- It does not retry against a different model. Re-prompting is a service-level decision (no router integration).
- It does not require an `llm_run_id`. Pass `run_id=None` to skip the DB write.

### Pre-flight registration checks

Pair `list_agent_types()` and `has_pipeline(slug)` with your project's startup wiring to fail loudly at boot if a required agent is missing. This is the validator-side equivalent of the model router's `assert_agents_registered(...)`:

```python
from pf_core.llm.validate import has_pipeline, list_agent_types

EXPECTED = ["summarizer", "classifier", "extractor", "reviewer"]
missing = [a for a in EXPECTED if not has_pipeline(a)]
if missing:
    raise ConfigurationError(
        f"validator pipelines missing: {missing}. "
        f"Known: {list_agent_types()}. "
        "Did you forget to import your validators registrar?"
    )
```

Catches typos and un-staged registrar edits before the first validation call.

### `PipelineNotRegisteredError`

Raised by `parse_and_validate` when `missing_pipeline="raise"` (the default) and the lookup fails. Subclass of `ConfigurationError`, so CLI/API boundaries that already handle `ConfigurationError` need no changes. Attributes: `agent_type` (the missing slug), `known_agents` (list of registered slugs).

---

## DB integration

When `run_id` is provided, the pipeline writes every signal — pass and fail — into `llm_run_validations` via `LlmRunValidationRepo.record`. Each `(run_id, validator)` write is a portable `upsert`, so re-running the pipeline cleanly overwrites prior signals.

```
llm_run_id | validator           | severity | passed | details
-----------+---------------------+----------+--------+--------------------------
      1042 | summarizer_shape    | error    | 1      | NULL
      1042 | url_sanity          | warn     | 0      | {"flagged":[{"url":...}]}
      1042 | tier1_ratio         | info     | 1      | {"ratio":0.83}
      1042 | within_max_words    | info     | 1      | NULL
```

The pipeline also writes a `schema:<agent>_v<schema_version>` row into `llm_run_tags` (an idempotent `insert_ignore`).

DB writes are best-effort: validation row failures log `validation_record_failed`, tag failures log `validation_tag_write_failed`, and the pipeline continues. The pipeline never raises on DB issues — validation results always reach the service.

See [`llm-tracking.md`](llm-tracking.md) for the underlying tracking schema and read helpers.

---

## Service call-site pattern

Replace ad-hoc `parse_llm_json` plus per-field guards with one `parse_and_validate` call. The `run_id` comes from the tracking decorator's `_llm_run_id` on the returned `usage` dict.

```python
# Before
content, usage = tracked_chat(messages=msgs, **get_agent_config("summarizer"))
try:
    parsed = parse_llm_json(content, expect="object", strict=True)
except InvalidInputError:
    raise SummarizeError("bad JSON")
if not parsed.get("headline"):
    raise SummarizeError("missing headline")

# After
content, usage = tracked_chat(messages=msgs, **get_agent_config("summarizer"))
result = parse_and_validate(content, agent_type="summarizer", run_id=usage["_llm_run_id"])
if not result.ok:
    raise SummarizeError("summarizer validation failed",
                         context={"failures": [s.validator for s in result.failures]})
parsed = result.value  # SummaryOutput instance
```

The hand-rolled checks now live in `SummaryOutput` (Pydantic constraints) and the `semantic=[...]` list. Failures land in `llm_run_validations` automatically.

---

## Query recipes

Dialect-portable across SQLite, MySQL, and Postgres.

```sql
-- Shape-failure rate by agent type, last 30 days
SELECT at.slug,
       AVG(CASE WHEN v.passed THEN 0.0 ELSE 1.0 END) AS shape_fail_rate,
       COUNT(*) AS runs
FROM llm_runs r
JOIN llm_agent_types at ON at.id = r.agent_type_id
JOIN llm_run_validations v
  ON v.llm_run_id = r.id AND v.validator LIKE '%_shape'
WHERE r.created_at >= CURRENT_TIMESTAMP - INTERVAL '30' DAY
GROUP BY at.slug
ORDER BY shape_fail_rate DESC;
```

```sql
-- Validator fail-rate over time, sliced by schema version cohort
SELECT t.tag AS schema_version,
       DATE(r.created_at) AS day,
       AVG(CASE WHEN v.passed THEN 0.0 ELSE 1.0 END) AS fail_rate,
       COUNT(*) AS runs
FROM llm_runs r
JOIN llm_run_tags t        ON t.llm_run_id = r.id AND t.tag LIKE 'schema:%'
JOIN llm_run_validations v ON v.llm_run_id = r.id
WHERE v.validator = 'url_sanity'
  AND r.created_at >= CURRENT_DATE - INTERVAL '14' DAY
GROUP BY t.tag, day;
```

```sql
-- Validator false-positive rate vs reviewer outcomes
SELECT v.validator,
       COUNT(CASE WHEN NOT v.passed THEN 1 END) AS flagged,
       1.0 * COUNT(CASE WHEN NOT v.passed AND o.outcome_kind = 'reviewer_accepted' THEN 1 END)
           / NULLIF(COUNT(CASE WHEN NOT v.passed THEN 1 END), 0) AS fpr
FROM llm_run_validations v
LEFT JOIN llm_run_outcomes o ON o.llm_run_id = v.llm_run_id
WHERE v.validator IN ('url_sanity', 'tier1_ratio', 'within_max_words')
GROUP BY v.validator;
```

---

## Adding a new semantic validator

The built-in semantic registry (`url_sanity`, `tier1_ratio`, `field_non_empty`, `min_items`, `no_duplicate_urls`, `date_range`) is **not extensible from project code** — adding a new entry requires changing pf-core's `_BUILDERS` table. This is intentional: built-ins ship with versioned semantics that cross-project dashboards depend on.

For project-specific content checks, **use a cross-field validator instead.** The decorator-registered hook accepts the parsed value and arbitrary context, has no config-string ceremony, and lives entirely in your project:

```python
from pf_core.llm.validate import cross_field_validator, ValidationSignal

@cross_field_validator("min_tagged_items_v2")
def min_tagged_items_v2(parsed, *, context) -> ValidationSignal:
    tagged = [item for item in parsed.items if item.category]
    if len(tagged) >= 3:
        return ValidationSignal("min_tagged_items_v2", "info", passed=True)
    return ValidationSignal(
        "min_tagged_items_v2", "warn", passed=False,
        details={"actual": len(tagged), "minimum": 3},
    )
```

If the check turns out to be useful across projects, propose promoting it to pf-core's built-in set in a follow-up plan.

---

## Adding a new agent type pipeline

1. **Define the response shape** as a Pydantic model (preferred) or JSON Schema dict in `app/validators/<agent>.py`.

   ```python
   from pydantic import BaseModel, Field
   from pf_core.llm.validate import register

   class ReviewOutput(BaseModel):
       verdict: str = Field(pattern="^(accept|revise|reject)$")
       comments: list[str] = Field(min_length=1)
       model_config = {"extra": "forbid"}

   register(
       agent_type="reviewer",
       shape=ReviewOutput,
       semantic=["field_non_empty:verdict", "min_items:comments:1"],
       schema_version=1,
   )
   ```

2. **Import the module from `app/validators/__init__.py`** so registration runs at startup.

3. **Call `parse_and_validate(content, agent_type="reviewer", run_id=usage["_llm_run_id"])` from the service.** The shape signal will land as `reviewer_shape`; the run is tagged `schema:reviewer_v1` in `llm_run_tags`.

4. **Bump `schema_version`** the next time the model changes materially. Old runs keep their old tag — analytics compare cohorts cleanly.
