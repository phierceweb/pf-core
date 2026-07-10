# Changelog

Notable changes to pf-core, newest first. The project is pre-1.0 — pin to a tagged release; `main` is the development line.

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
