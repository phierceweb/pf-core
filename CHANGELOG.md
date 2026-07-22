# Changelog

Notable changes to pf-core, newest first. The project is pre-1.0 — pin to a tagged release; `main` is the development line.

## v0.12.0 — 2026-07-21

### Added
- `MarkdownExporter.check(root)` — dry-run freshness gate: the sorted relative paths `export` would touch (missing, content-stale, prunable orphans), writing nothing. An empty list means the tree on disk is exactly what `export` would produce — wire it into pre-commit/CI for committed generated trees.
- `MarkdownExporter.force_prune_dirs` — root-relative directories always in prune scope, so a stable subdirectory that yields zero artifacts in a run still sheds its orphans (default scope only prunes directories the run produced into).

### Fixed
- The wheel now includes `pf_core/web/jobs_admin/templates/` — 0.11.0's wheel omitted it (package-data declared llm_admin's templates only), so `make_jobs_router` pages failed on wheel installs while working editable. `tests/test_packaging.py` now asserts every shipped `templates/` dir has a package-data entry.

## v0.11.0 — 2026-07-19

### Added
- `pf_core.jobs.workers` — the jobs execution layer: `start_workers`/`stop_workers` (daemon claim-loop pool over `claim_next` with non-halting error handling, `JOB_POLL_SECONDS` cadence, and a `reclaim_stale` sweep on start so jobs stranded `running` by a killed worker re-enter the queue), `run_subprocess_job` + `SubprocessJobSpec` (argv/log-path/outputs hooks, job-id env injection — default `PF_JOB_ID` — own-session child, stderr-merged log with `$ argv` header, exit-code → terminal transition with a canceled-row guard), `terminate_job` (process-group SIGTERM with SIGKILL escalation), and `tail_log` (byte-offset log reads).
- `pf_core.jobs.submit` — background thread submitter for web-triggered jobs: `submit_tracked` (create → `Job` window → progress callback → succeeded; failures recorded by the context manager), `submit_detached` (service creates its own job; the new id is resolved for the caller), `JobAlreadyRunning` dedup via an injectable inputs-predicate, and `wait_all` — the test-suite drain hook for `pf_engine_teardown`.
- `pf_core.web.jobs_admin.make_jobs_router` — mountable jobs dashboard (sortable/paginated list, polling detail page) + JSON API (`GET .../api/{id}` bundle, `POST .../api/{id}/cancel` — soft cancel, 409 on terminal, optional `terminate_hook`) with `auth_dep`/`kind_labels`/`describe`/`templates` injection; templates are self-contained.
- `JobRepo.find_page(sort=, direction=, limit=, offset=)` — one sorted page + total, with a fixed sort allowlist (id/kind/status/created_at).

## v0.10.0 — 2026-07-19

### Added
- `pf_core.llm.step.llm_step` — the per-item batch hot path as one call: input-hash → cache lookup (a hit records a `cache_hit` run and returns, budget skipped; with `validate=` the stored raw is re-validated) → budget gate (`BudgetEstimate`; a block records the blocked run and raises) → `tracked_messages_call` (all its kwargs pass through by name) → `parse_and_validate` (a failing result returns, never raises) → cache store (only on valid; raw always, parsed only when dict/list). Returns `StepResult(value, content, run_id, cache_hit, validation)`. The batch shell — Job/steps, `run_parallel`, persistence — stays in the caller; the batch-llm-service recipe now shows the composed form.

## v0.9.0 — 2026-07-19

### Added
- `pf_core.utils.slugify` — fold free text to a stable lowercase ASCII slug (`slugify("Crème brûlée") → "creme-brulee"`, keyword-only `sep=`): strip/lowercase, special-letter map for what NFKD can't decompose (ø, å, æ, œ, ð, þ, ł, ß), NFKD diacritic strip, non-alphanumeric runs collapsed to the separator. Pure stdlib; re-exported from `pf_core.utils`.

## v0.8.0 — 2026-07-19

### Added
- `pf_core.llm.recording` — ambient call-recording window (ContextVar-based, the jobs-runtime pattern): `begin_call_recording(session_metadata=...)` opens a window, `tracked_messages_call` attributes the session metadata to every run inside it and appends a per-call summary, `end_call_recording()` drains. Pool workers join a window via `contextvars.copy_context().run(...)`.
- `pf_core.llm.tracking.split_metadata(metadata)` — flat dict → (`"key:value"` tags, float metrics): bools tag as `true`/`false`, `None` dropped, 64-char caps matching the sidecar columns.
- `tracked_messages_call` accepts `metadata=` (split + merged beneath explicit `tags=`/`metrics=`, failed rows included) and `job_id=` (explicit run attribution; `None` keeps the ambient-Job fallback).
- `llms.txt` at the repo root — AI-discovery index of every shipped doc (completeness enforced by `tests/test_llms_txt.py`).
- `pf-setup` console script — links the installed package's bundled docs at `docs/pf-core/` in any consumer, so in-repo AI assistants read version-matched docs. Idempotent; never replaces a real file or directory. `pf-doctor` gains a read-only `wiring.docs_link` row reporting the link.

### Fixed
- `docs/orchestrators.md` no longer contradicts itself on `transaction()`: the shared-connection example is labeled as the sanctioned transactional-orchestration exception, and the "must not" rule is scoped to data access. The layering/anti-patterns rule files carry the same scoping.

### Changed
- The sdist no longer ships `tests/` (distutils' legacy default included the test files but not `conftest.py`, so the shipped suite could never run).

## v0.7.3 — 2026-07-17

### Fixed
- Install/versioning docs corrected: pin examples track the current minor line (now enforced by `tests/test_docs_pins.py`).
- CI lint installs a pinned ruff instead of unpinned latest; `[dev]` extras (pf-core and the consumer templates) declare a compatible-release ruff band.
- `publish.yml` gates the PyPI upload on the full suite passing at the tagged sha (previously build-only).

### Changed
- Build floor raised to `setuptools>=77` (PEP 639 license metadata requires it).

## v0.7.2 — 2026-07-15

### Fixed
- `parse_llm_json` logs a WARNING (`parse_llm_json_recovered_truncated`, with recovered item count) when truncation recovery salvages a partial array — the return value carries no truncation flag, so the previous DEBUG-level line let batch pipelines silently drop the tail of every `max_tokens`-cut response while reporting success.
- `LlmRunRepo.record()` computes `input_hash` with the same sampling-key filter as the public `compute_input_hash` (the filter now lives in the shared internal, so the two paths cannot diverge again). Callers passing non-sampling keys in `sampling` previously stored a hash the exact cache — which keys on the public function — could never match, so cache lookups and `find_by_hash` silently missed. Runs recorded before this fix keep their old hashes; affected cache entries re-fill on the next call.

## v0.7.1 — 2026-07-15

### Fixed
- Eval replays resolve their client through the model router — replays run on the backend the agent declares instead of a hardcoded OpenRouter client, so an eval measures the transport production uses (the judge already routed this way). `target` accepts `backend` and `model` overrides; an agent absent from the router degrades to the OpenRouter client with a `replay_router_unavailable` warning, and resolution failures other than `ConfigurationError` surface as error results instead of being silently swallowed.
- Structured comparison requires a non-empty dict golden: a golden whose parsed output is a list or irrecoverably empty errors before the replay call is spent, instead of crashing (list) or scoring `{}` vs `{}` as 1.0 (empty). `GoldenSetRepo.add()` warns `golden_non_dict_parsed_output` at promote time.
- `structured_diff` no longer coerces bools through the int↔float path: `True` vs `1.0` scores 0.0 — bools compare exact on every path, matching the tolerance rule from 0.6.3.
- The eval judge honors its agent's YAML sampling; `temperature 0.0` / `max_tokens 512` are defaults for unset keys, not overrides (a `reasoning_effort` judge is no longer token-starved into scoring 0).

### Changed
- `tracked_call` records its rendered text in the payload's `rendered_user` slot, matching the user role it is sent with — eval replays rebuild message roles from the slots, so replays of `tracked_call` goldens now keep production's role. Goldens recorded by earlier versions carry the text in `rendered_system` and replay system-role; re-promote them for role-faithful replays.
- Docs surface: README gains a PyPI badge, a project-history section, and a collapsed all-docs index linking every file in `src/pf_core/docs/`; `modules.md` now indexes every doc (`periods`, `scaffold`, and `test-migration` were missing) and points at `INSTALLATION.md`.

## v0.7.0 — 2026-07-13

### Changed
- Framework JSON columns (`pf_core.db.types.JSON_`, used by the tracking, jobs, cache, and budget tables) now store Python `None` as SQL NULL instead of JSON `null` (`none_as_null=True`): `IS NULL` / `IS NOT NULL` predicates match reality and raw SELECTs no longer return the truthy text `'null'`. Typed reads are unchanged (both decode to `None`). To store an explicit JSON null, pass `sqlalchemy.JSON.NULL`. Rows written by earlier versions keep their JSON nulls — match both in SQL until backfilled: `col IS NULL OR JSON_TYPE(col) = 'NULL'` (MySQL).

## v0.6.3 — 2026-07-13

### Fixed
- `structured_diff` tolerances now apply to int-valued fields: `_field_score` coerced ints to float only in mixed pairs, so int-vs-int comparisons (the common case — LLM JSON numbers parse as ints) silently ignored the configured tolerance and compared exact. Bools still compare exact. Consumers with int-valued tolerance fields: the tolerance takes effect as configured.

## v0.6.2 — 2026-07-13

### Fixed
- `EvalRunner` no longer scores replays against `{}` when a golden's stored `parsed_output` is empty (consumers that validate post-record can overwrite it with JSON null, which SQL `IS NOT NULL` can't detect): the comparison falls back to re-parsing the golden's stored `raw_response`.
- `GoldenSetRepo.add()` / `seed_from_outcomes()` warn at promote time when the run has no payload sidecar (`golden_missing_payload`) or an empty `parsed_output` (`golden_missing_parsed_output`), so unreplayable goldens surface at seeding instead of as uniform eval scores.

## v0.6.1 — 2026-07-13

### Fixed
- Eval replays no longer join the golden set: `EvalRunner` tagged each replay run `eval:<version>` — the exact golden-membership tag — so every eval added its replays to the set it was evaluating (and the next run would replay the replays). Replays are now tagged `eval:replay:<version>`. Consumers whose sets were contaminated: delete the `eval:<version>` tag rows on replay-linked runs (`llm_run_links.relation='replay'`).
- The `pf-jobs` console script is now actually installed (the CLI existed and was documented, but the `[project.scripts]` entry point was missing).
- Docs corrected to match what ships: the eval harness is Python-API-only (the documented `pf-eval` CLI does not exist — CLI/CI use goes through a small project runner script); install-guidance pin examples updated from `~=0.2.0`/`~=0.4.1` to the current release line.

## v0.6.0 — 2026-07-12

### Added
- `pf_core.llm.tracked.tracked_messages_call` — the messages-based tracked call: sends a verbatim message list, records one `llm_runs` row (failure rows with error/class/http_status on client exceptions, then re-raises), extracts rendered system/user by role for payloads, optionally registers system (+ user) prompt ids from a spec dict (`spec_on_change` forwarded), and carries `sampling` (recorded) separately from `chat_kwargs` (forwarded only). Supports `input_hash`, `configs`, `tags`, `metrics`, `items_out`; `on_record_error="warn"` makes the tracking sink best-effort (`run_id=None` on sink failure). Returns `(content, usage, run_id)`.
- `pf_core.db.types` — public home for the cross-dialect column-type variants the framework tables are built from: `PK_INT`/`PK_SMALL`/`PK_BIG`, `FK_INT`/`FK_SMALL`/`FK_BIG`, `TIMESTAMP_US`, `LARGE_TEXT`, `JSON_`, `server_now()`. The underscored names in `pf_core.llm.tracking.schema` remain as aliases of the same objects.

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
