# Project Portability

A pf-core consumer is **portable** when its codebase contains zero project-specific names, organizations, jurisdictions, or domains. All project-specific values live in a config layer (typically a YAML file parsed at startup), and code reads them through placeholders.

When portability holds, forking the application for a different domain — a different document set, a different product, a different industry — requires **only** edits to the config file. No prompt edits, no Python edits, no test edits.

When portability is broken — when a proper noun is hardcoded in a prompt YAML, when a domain-specific value is hardcoded in a service module, when a test asserts a literal proper noun — the fork is no longer a config swap. It's a search-and-replace pass across YAML and Python files. Every forgotten hardcoded value becomes a quiet bug in the new project.

## Why it matters

pf-core consumers tend to ship with one obvious user. A document-conversion pipeline ships pointed at one library of manuals. A knowledge-base assistant ships pointed at one product. A data-extraction tool ships pointed at one source format. **Each is a worked example, not the only valid configuration.**

Project-specific values that bleed into framework-shaped code (prompts, services, helpers) eventually get noticed by the next person who tries to point the codebase at a different subject. The cost of *adding* a placeholder when the value is first introduced is small. The cost of *finding and removing* a hardcoded value scattered across files months later is large.

## Hard rules

### 1. Project-specific names live in the config layer

The corpus a tool is pointed at, the product an assistant answers for, the source format an extractor parses — these belong in the config layer, not in code or in YAML prompt files. Render them into prompts via placeholders.

```python
# In your config schema (e.g. subclass of pf_core.config.AppConfig
# plus a project-specific YAML):
class MyProjectConfig(AppConfig):
    SUBJECT: str = ""  # "the subject this project is configured for"
```

```yaml
# In project.yaml:
subject: "Acme Corporation and its products"
```

```yaml
# In your prompt YAML — placeholder, not literal:
system: |
  You answer questions about {subject}. ...
```

### 2. Project-specific domain lists, source rosters, and vocabulary live in the config layer

Lists of trusted domains, prohibited sources, controlled tags, controlled action types, scope filters, curated source lists — all data, not framework. Hand them to prompts via builders that read from config:

```python
# In a prompt builder module (per-consumer):
def _build_tier1_domain_block() -> str:
    """Render the Tier-1 trusted-domain list from config."""
    domains = MY_PROJECT.source_tiers[0].get("trusted_domains", [])
    return "\n".join(f"  - {d}" for d in domains)
```

The prompt YAML then uses `{tier1_domain_block}` — a generic placeholder name that doesn't bake the project's specific list into the prompt.

### 3. Tests must not assert literal project-specific strings

Tests that check whether a prompt contains a hardcoded proper noun, domain, or organization slogan are forbidden. They lock the test to the current project and fail noisily on any fork.

**Bad:**
```python
def test_prompt_mentions_acme():
    assert "Acme Corporation" in SUMMARIZER_SYSTEM
```

**Good:**
```python
def test_prompt_renders_subject_from_config():
    from myapp.config import MY_PROJECT
    # Whatever the config says the subject is, it should appear.
    assert MY_PROJECT.subject in SUMMARIZER_SYSTEM
    # And the placeholder must have been substituted.
    assert "{subject}" not in SUMMARIZER_SYSTEM
```

Test fixture data — `actors`, `entities`, `URLs`, `names` — should use generic values (`"Acme Corp"`, `"Senator Smith"`, `"example.com"`) when the test is exercising mechanics rather than project-specific wiring. Reserve real project values for tests that *specifically* exercise project-config plumbing.

### 4. One-off scripts may inline project values; long-running pipeline code may not

A throwaway audit script under `.ai/scripts/` can hardcode `"the Acme Corporation"` in a query string for the duration of its life. It must not edit `app/` or `config/prompts/` files to embed that literal — those are pipeline files, governed by rules 1–3.

## How to add a new placeholder

When you discover a project-specific value sitting in a prompt YAML or a service module, the migration is mechanical:

1. **Add the field to the config layer** with a comment explaining what it represents and where it's used. (For pf-core consumers that subclass `AppConfig`, this is either a new env var on the subclass or a new key in the project YAML.)
2. **Expose it on the project's domain config object** with a sensible project-neutral default. The default should be honest — typically a phrase like `"the subject this project covers"`, not a placeholder string.
3. **Add a builder or constant** in your prompt-config module that renders the value into the form the prompt needs (raw string, formatted block, list).
4. **Pass it into the relevant prompt loader** as a placeholder.
5. **Reference the placeholder in the YAML prompt**: `{name}` for f-string-style prompts, `@@NAME@@` for token-style prompts (see [prompts.md](prompts.md)).
6. **Bump the prompt version** in the YAML changelog.
7. **Update the project's config schema doc** with the new field.

## Audit checklist

A change is suspect if it:

- Edits a file under `config/prompts/` (or wherever the project keeps its prompt YAMLs) to add a proper noun, organization name, or domain-specific value that isn't already in the project config.
- Adds a hardcoded list of domains, names, or actors to a Python module under the project's services / orchestrators / prompts packages outside the dedicated `_config.py`-style builders that read from the project config.
- Adds a test asserting a literal proper-noun or domain string instead of asserting the placeholder is present or the config value is rendered.
- Adds a hardcoded region- or platform-specific concept (a country's tax codes, a vendor's API quirks, etc.) to a service module meant for general use.

When the linter, code review, or CI flags this, fix it by promoting the value to the project config and rendering it via a placeholder. Do not silence the warning.

## Worked example — a content-aggregation app

A content-aggregation consumer ships with `project.yaml` as its project config layer. The same principles in this doc are applied:

| Project-specific concern | Lives at |
|---|---|
| Display name of the app | `project.yaml:name` (rendered via `cfg.APP_NAME` / `@@APP_NAME@@`) |
| Noun phrase naming the subject the app covers | `project.yaml:subject` (rendered via `{subject}` / `@@SUBJECT@@`) |
| Temporal scope start | `project.yaml:start_date` (rendered via `cfg.START_DATE`) |
| Trusted primary-source domains | `project.yaml:source_tiers[0].trusted_domains` (rendered via `{tier1_domain_block}`) |
| Trusted secondary-source domains | `project.yaml:source_tiers[1].trusted_domains` |
| Prohibited source domains | `project.yaml:prohibited_sources` / `prohibited_domains` |
| Curated source list | `project.yaml:curated_sources` |
| Controlled-tag vocabulary | `project.yaml:tags` |
| Action-type vocabulary | `project.yaml:action_types` |
| Scope filter entity names | `project.yaml:scope_filters` |

The consumer-side rule that codifies these bindings — and links to this doc as the source of the principle — lives at `.ai/rules/no-hardcoded-project-values.md` in the consumer's repo.

## See also

- [config.md](config.md) — `pf_core.config.AppConfig` for project settings (env + YAML + class defaults resolution).
- [prompts.md](prompts.md) — `load_prompt_spec` / `render_spec` for loading prompts with placeholders.
- [vocab.md](vocab.md) — `SlugNormalizer` for project-specific controlled vocabularies (the machinery is generic; the vocab is project-specific).
