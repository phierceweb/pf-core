# Changelog

Notable changes to pf-core, newest first. The project is pre-1.0 — pin to a tagged release; `main` is the development line.

## v0.2.3 — 2026-07-01

### Added
- `AnthropicClient.chat()` honors `response_format`: `json_schema` maps to Anthropic-native structured outputs (`output_config.format`); `json_object` is enforced via a system instruction (no native equivalent, one-shot log); unknown types warn once and are ignored.
- Anthropic prompt caching: `chat(cache_system=True, cache_ttl="5m"|"1h")` marks the system prompt as a cache breakpoint. Settable per-agent via `model_router.yaml` backend kwargs. Cache read/write tokens flow into cost estimation, so `cost_usd` reflects cache pricing once the model's rates define `cache_read`/`cache_write` (the built-in table leaves these unset — register them with `pf_core.pricing.register_rates`).
- Leading `{"role": "system"}` messages are extracted to Anthropic's top-level `system=` parameter, making the three transports drop-in interchangeable for system-bearing calls.

### Changed
- `[anthropic]` extra floor raised to `anthropic>=0.105` (structured-outputs support).

## v0.1.0 — 2026-06-15

Initial public release: dependency-light foundation (structured logging, exception hierarchy, config + env resolvers, utils, `Service` base) with opt-in extras for the LLM clients and anti-slop guards, database layer, FastAPI web layer, job tracker, run tracking, and eval harness.
