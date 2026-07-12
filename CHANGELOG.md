# Changelog

Notable changes to pf-core, newest first. The project is pre-1.0 — pin to a tagged release; `main` is the development line.

## v0.5.0 — 2026-07-11

### Added
- Consumer test bootstrap in `pf_core.testing`: `framework_ddl()` emits DDL for every pf-core-owned table (tracking, jobs, cache, budget) and `metadata_ddl()` for any SQLAlchemy metadata, for splicing into the `pf_schema` fixture. Both accept `only={...}` to restrict to named tables (for projects whose migrations extend framework tables). `pf_engine` honors `PF_TEST_DATABASE_URL` (run the same suite against a disposable Postgres/MySQL database) and gains an overridable `pf_engine_teardown` hook run before `engine.dispose()`. New `pf_budget_disabled` fixture in the auto-loaded plugin. New `pf_core.testing.env` import-time conftest helpers: `hermetic_test_env()` (no-external-services env block) and `stub_model_router()` (temp router YAML + `MODEL_ROUTER_CONFIG`).
- `CACHE_CONFIG` accepts `off` / `disabled` / `none` / `0` — disables exact and semantic caching with no config file needed.
- `pf_core.llm.prompts.load_prompt(slug, ...)` — slug-based per-agent spec loading: maps `slug` → `<slug>.yaml` (fixed `dir=`, or override chain `env_dir_var` → CWD `config/prompts/` → `bundled_dir`), enforces `expected_agent=slug`, caches per process (`clear_prompt_cache()` resets; `cache=False` re-reads).

### Changed
- `pf_engine` clears the tracking resolver caches at setup.
- `BUDGET_ENFORCEMENT_DISABLED` now also short-circuits `project_cost()` to `0.0` before any DB access (previously only `check_budget()` honored it).

## v0.4.1 — 2026-07-11

### Changed
- The gate reads `.pf-guards.toml` at the repo root; `--config` overrides the path. `[tool.pf_guards]` in `pyproject.toml` is no longer read. A missing config file exits `2` — except the default path with `--root` given, which runs flag-specified (gate adoption / ad-hoc).
- Consumer templates, `bin/setup` self-heal, and `setup-common`'s `pf_ensure_guards_config` stamp/target `.pf-guards.toml`; all wiring (`bin/lint`, pre-commit, CI) runs the bare `python -m pf_core.guards`.

## v0.4.0 — 2026-07-09

### Added
- `pf_core.guards` is the single structural gate, configured via `[tool.pf_guards]` in `pyproject.toml`: `root` (string, or list for multi-tree scans with per-root path prefixes), `hard`, `soft`, `util`, `soft_fraction`, `layers`, `limits` (path-prefix budgets, longest prefix wins), `baseline`, `allowed_imports`, `layering_allowlist`. Bare `python -m pf_core.guards` reads it; CLI flags override.
- Per-layer file-size limits for `app/` trees, soft warn at `soft_fraction × hard`; default values live in `pf_core.guards.config`.
- The layering checker runs in the same gate: explicit per-layer allow-sets (`allowed_imports` overrides per key; a new key declares a new checked layer), `app/db/` as the checked bottom layer, relative imports resolved, file:line + hint output, `# lint-layers: skip` honored, `tests/`/`conftest.py` skipped.
- Stale-checked exceptions: a `baseline` or `layering_allowlist` entry that no longer matches a real violation fails the gate until removed.
- `--emit-baseline` / `--emit-allowlist` print paste-ready exception blocks for adopting the gate on a tree with existing violations.
- Misconfiguration exits `2`: malformed TOML, non-positive limits, `soft_fraction` outside `(0, 1]`, missing scan root.
- Consumer templates stamp the gate wiring: `[tool.pf_guards]`, config-driven `bin/lint`, `.pre-commit-config.yaml`, `guards.yml` CI workflow. `bin/setup` self-heals the config, installs pre-commit hooks, and symlinks the installed pf-core docs at `docs/pf-core` (gitignored). `setup-common` gains `pf_ensure_guards_config` and `pf_ensure_docs_link`.

### Changed
- pf-core passes its own gate with no baseline; the four over-limit modules were split by concern, public import paths unchanged. Pure URL parsing (`pf_core.utils.url_parse`) and HTML metadata extraction (`pf_core.utils.url_html`) now import without the `[http]` extra.
- pf-core's own gate config lives in repo-root `.pf-guards.toml` (via `--config`), not `pyproject.toml`. The baseline is a `[tool.pf_guards.baseline]` table; `--baseline file.json` remains as a CLI override.

### Fixed
- `docs/recipes/*.md` now ship in the wheel.

### Removed
- `bin/lint-size`, `bin/lint-layers`, and `.lint-size.yaml` support — superseded by the gate.

## v0.3.1 — 2026-07-09

### Added
- `pf-doctor --release` — opt-in release-state attestation via read-only git introspection of the current project: `versions` (pyproject `version` vs the top `## v…` heading in `CHANGELOG.md`; FAIL on mismatch), `tag` (whether `v<pyproject-version>` is among the tags at HEAD; FAIL when HEAD is tagged a different version), and `tree` (WARN on an uncommitted working tree). A local preflight mirroring the CI tag-vs-version guard in `publish.yml` — catch a version/tag/CHANGELOG/dirty-tree mismatch before tagging, not after CI rejects the upload. SKIPs outside a git repo; stays within doctor's read-only, no-network-by-default, no-consumer-import invariants.

## v0.3.0 — 2026-07-02

### Added
- `pf-doctor` (`pf_core.doctor`) — runtime ground-truth attestation CLI: loaded pf-core copy/version (with stale-editable detection), interpreter/venv, installed extras, env-var resolution (secrets redacted, `.env`-aware), model-router config validation, dependency versions; `--db` adds a strictly read-only database check (connectivity + alembic revision vs script head). Foundation-tier, zero new dependencies. See `docs/doctor.md`.
- Built-in Anthropic cache pricing: the bundled rate table now carries `cache_read` (0.1x input) and TTL-aware cache-write rates (`cache_write` at 1.25x input for 5m, new `ModelRates.cache_write_1h` at 2x for 1h). `estimate_cost()` gains `cache_ttl="5m"|"1h"` and `AnthropicClient.chat()` passes its `cache_ttl` through, so `usage["cost_usd"]` reflects cache pricing out of the box. Cost estimates for calls with cache tokens on built-in Anthropic models increase accordingly (previously cache tokens priced at 0).

### Fixed
- `AnthropicClient.chat()` no longer sends `top_p` by default — Claude 4+ models reject requests specifying both `temperature` and `top_p`, so default-kwargs calls 400'd against them (caught by a live smoke test). `top_p` is now opt-in; passing it explicitly still forwards it.

## v0.2.3 — 2026-07-01

### Added
- `AnthropicClient.chat()` honors `response_format`: `json_schema` maps to Anthropic-native structured outputs (`output_config.format`); `json_object` is enforced via a system instruction (no native equivalent, one-shot log); unknown types warn once and are ignored.
- Anthropic prompt caching: `chat(cache_system=True, cache_ttl="5m"|"1h")` marks the system prompt as a cache breakpoint. Settable per-agent via `model_router.yaml` backend kwargs. Cache read/write tokens flow into cost estimation, so `cost_usd` reflects cache pricing once the model's rates define `cache_read`/`cache_write` (the built-in table leaves these unset — register them with `pf_core.pricing.register_rates`).
- Leading `{"role": "system"}` messages are extracted to Anthropic's top-level `system=` parameter, making the three transports drop-in interchangeable for system-bearing calls.

### Changed
- `[anthropic]` extra floor raised to `anthropic>=0.105` (structured-outputs support).

## v0.1.0 — 2026-06-15

Initial public release: dependency-light foundation (structured logging, exception hierarchy, config + env resolvers, utils, `Service` base) with opt-in extras for the LLM clients and anti-slop guards, database layer, FastAPI web layer, job tracker, run tracking, and eval harness.
