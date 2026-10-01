# Changelog

Notable changes to pf-core, newest first. The project is pre-1.0 — pin to a tagged release; `main` is the development line.

## v0.24.0 — 2026-09-30

### Changed
- `Fetcher.not_modified` and the module-level `not_modified` return `True` on a 200 that carries
  the same strong ETag they sent, as well as on a 304. Weak ETags, a matching `Last-Modified`
  and any other 2xx status still return `False`. `require_304=True` counts only a 304.

### Fixed
- `Fetcher.get_text` / `fetch_text` decode as utf-8 when the Content-Type charset names no text
  codec (`utf8mb4`, `binary`); they raised `LookupError`. An unknown `encoding=` still raises.

## v0.23.0 — 2026-09-29

### Breaking
- `ClaudeCodeClient` calls are plain completions by default: no tools (`--tools ""` and
  `--strict-mcp-config`), and the `system` messages sent as the system prompt from a private
  temp file (`--system-prompt-file`) in place of Claude Code's agent prompt; only the other
  messages go on stdin. New `tools` and `agent_prompt` args (also on `get_client` /
  `new_client` and router `client_kwargs`) name the built-in tools the model may call and
  keep Claude Code's prompt, appending the `system` messages with
  `--append-system-prompt-file`. A `--tools` in `extra_args` replaces the client's tool
  flags; a consumer append flag composes with the client's `--system-prompt-file`, and a
  replace flag (or an append flag with `agent_prompt=True`) flattens the messages onto stdin
  as before.
- A prompt that relied on the old default tool set — asking the model to Read a file or run
  a command, with no flags — now gets an answer written without the tool, and nothing
  raises. Name the tools: `tools=["Read"]`.
- Granting tools only through `--allowedTools` in `extra_args` raises `ConfigurationError`
  at construction — pass `tools=[...]`. So do MCP names in `tools` (MCP tools need
  `isolate=False` and `--allowedTools`), `tools` given as a string, and `tools` alongside a
  `--tools` in `extra_args`.
- `claude_code.get_client()` shares one client per distinct set of arguments, not per
  `model`. A later call with other args gets its own client where it used to get the first
  call's, so one caller's `tools`, `extra_args` or `isolate=False` never reaches another's,
  and `get_client(model=m)` no longer inherits an earlier call's `timeout` or `retry`.
- Under `PF_TEST_DATABASE_URL`, `pf_engine` gives each test its own Postgres schema or MySQL
  database, dropped at teardown, and applies `get_engine()`'s session setup (UTC). The URL's
  role needs the privilege to create them, and apart from tables an extension installed, a
  test reads only the tables it creates (through `pf_schema` / `pf_tables`). On Postgres the
  `search_path` is the test schema then `public`, so extension types and functions there still
  resolve; because a table there would resolve too, setup raises `ConfigurationError` naming
  each table or view in `public` that no extension owns, tables an earlier `pf_engine` created
  there included. Point the URL at an empty database. Teardown's `DROP` is bounded by
  `PF_TEST_LOCK_TIMEOUT_S` (default 10s; `0` waits forever).
- `InvalidInputError` also subclasses `ValueError`, and `PreconditionError` subclasses
  `RuntimeError`. `except ValueError` / `except RuntimeError`, and `pytest.raises` of either,
  now catch them, including around code that already raised them; `isinstance(exc,
  ValueError)` is true for an `InvalidInputError`. pydantic turns an `InvalidInputError`
  raised in a validator into a `ValidationError` (FastAPI: its request, websocket or response
  validation error) instead of letting it escape validation, and argparse turns one raised by
  a `type=` converter into a usage error. click does the same to one raised by a typer
  `parser=`; `run_cli` answers that one with its message and exit 1, as before. The 422 / 409
  HTTP mapping still wins over a handler registered for the builtin. Code outside pf-core that
  catches `FlowException` around a model construction sees the `ValidationError`;
  `unwrap_flow_exception()` gives back the error. A class that lists the builtin first —
  `class E(ValueError, InvalidInputError)`, `class E(RuntimeError, PreconditionError)` — now
  raises `TypeError` at definition (no consistent MRO); list the pf-core class first.
- `.pf-guards.toml` refuses an unknown key under `[tool.pf_guards]` by name, with the nearest
  known key suggested; the gate exits 2.

### Added
- `python -m pf_core.guards` runs a framework check when `.pf-guards.toml` has a
  `[tool.pf_guards.framework]` table. It fails on code that hand-rolls what pf-core ships —
  importing `logging`, `dotenv`, `requests` / `httpx` / `aiohttp` / `urllib3`,
  `concurrent.futures`, `multiprocessing` or `hashlib`; raising builtin `Exception`,
  `RuntimeError` or `ValueError`; `os.environ` / `os.getenv` reads, `print()`, `.exception()`
  on a logger (`logger`, `_log`, `log`, `logging`, …), `os.replace()`;
  `.write_text(json.dumps(...))` — and names the pf-core replacement for each. `disable` turns
  rules off, `replace` renames the replacement a message names, and
  `[[tool.pf_guards.framework.exempt]]` exempts a rule for one path with a reason of
  at least four words; an exemption that suppresses nothing fails the gate. `os.environ` /
  `os.getenv` are also caught through another name (`import os as o`, `from os import environ,
  getenv`). Not breaches: `ValueError` in a pydantic validator (a decorator imported from
  pydantic, or a function handed to its `AfterValidator` / `BeforeValidator` /
  `PlainValidator` / `WrapValidator`) or in an argparse `type=` converter (the function, a
  method through its class, or each one a `type=lambda` calls, defined in or imported into the
  module that passes it, an import resolved to the module it names), and the environment
  unpacked into a dict literal passed as a call's `env=`. `--framework` runs the check alone;
  `--report` lists breaches, `0 framework breaches` on a clean tree, and exits 0.
  `pf_core.guards.check_framework()` is the Python entry point; it raises `ConfigurationError`
  on a stale exemption, and `FrameworkConfig` / `FrameworkExemption` refuse on construction
  what the TOML refuses.
- `max_function_lines` in `[tool.pf_guards]` turns on a function-length check: a hard limit,
  or a table of `hard`, `soft` (default `hard` x `soft_fraction`), per-layer `layers` and
  path-prefix `limits`. A function over its hard limit fails the gate and one over its soft
  target warns, each printed with its path, `def` line, qualified name and length. Length
  runs from the `def` line to the last line; a nested function is also measured on its own.
  `pf_core.guards.scan_function_lengths()` is the Python entry point.
- `[tool.pf_guards.comment_budget]` turns on a ceiling on comment and docstring lines per line
  of code, per module (`file`) and across the scanned roots (`total`); a module with fewer
  than `min_code_lines` lines of code counts only toward the total, and `total_root` limits the
  total to some of the scanned roots. A module or total over its ceiling fails the gate.
  `pf_core.guards.scan_comment_budget()` is the Python entry point.
- With the function-length limit, comment budget or framework check on, a file under their
  roots that does not parse fails the gate, `--report` included, and each check still runs over
  the other files. Files are read as Python reads them (a BOM, a coding cookie). The `scan_*`
  functions and `check_framework()` raise its `SyntaxError`; `scan_*` take
  `skip_unparsed=True` to pass over it.
- `pf_core.exceptions.unwrap_flow_exception()` returns the `InvalidInputError` a validator
  raised from a pydantic `ValidationError` or FastAPI's `RequestValidationError` /
  `WebSocketRequestValidationError` / `ResponseValidationError` — the first one, whatever
  else the error holds — or `None`. pf-core answers it as it answered the escaped error: the
  app from `create_app` with the handler for its class (422), `run_cli` with its message and
  exit 1, `log_exception` at WARNING as `APP-InvalidInputError`; `resilient` matches `catch`
  against it and records its message, `call_with_fallback` does not fall back on it, and a job
  kind's schema validation and `PydanticValidator.validate_shape` let it escape as itself. A
  validation error without one reaches the handler that answered it before: FastAPI's own,
  the app's, the app's `ValueError` handler, or the 500 path. `create_app` returns a `FastAPI`
  subclass that wraps the four validation-error handlers when the app starts, including ones
  registered after `create_app`.

### Changed
- `ClaudeCodeClient` passes `--no-session-persistence` by default, so calls no longer write
  session transcripts to `~/.claude/projects/`. New `persist_session` arg (also on
  `get_client` / `new_client`) restores them, as does a `--resume` / `--continue` /
  `--session-id` in `extra_args`.
- `metadata_ddl` / `framework_ddl` default `dialect` to the test backend
  (`PF_TEST_DATABASE_URL`'s, else SQLite) instead of always SQLite. On MySQL their index
  statements omit `IF NOT EXISTS`, which MySQL does not support.
- `import pf_core.utils.io` and `pf_core.utils.json` load no logging stack until they log, and
  `pf_core.__version__` reads package metadata on first access: importing either drops from
  about 80 ms to under 20 ms.

### Fixed
- `python -m pf_core.guards` crashed on a file in a declared encoding (a coding cookie). The
  size gate now counts a file's rows from its bytes, where Python ends them, so a form feed no
  longer adds a line; the layering check reads files as Python does.
- `insert_ignore` on MySQL/MariaDB returned `1` for a skipped duplicate. It now returns `0`,
  read from `lastrowid`; a skip leaves the session's `LAST_INSERT_ID()` at a sentinel value.
- `docs/database.md` recommended `result.lastrowid` for inserted ids, which raises on Postgres;
  it now shows `insert(...)` with `inserted_primary_key`.

## v0.22.0 — 2026-08-30

### Breaking
- `EvalRunner.compare_experiments` returns `{"pairs", "baseline_runs", "candidate_runs"}`
  instead of the bare pair list. It raises `PreconditionError` when either experiment tag
  matches zero scored replay runs or when the two tags share no golden parent, and
  `InvalidInputError` when both tags are the same string. Replays whose `eval_score`
  outcome has a NULL score are excluded. Duplicate replays per golden resolve to the
  latest.
- `llm_step` now passes its `tags` to `check_budget`, so existing `tag:` scopes in
  `budgets.yaml` begin gating `llm_step` calls that were previously uncapped.
- `CostBudgetExceeded` takes `limit_value` / `spent_value` / `projected_value` (was
  `limit_usd` / `spent_usd` / `projected_usd`). The `*_usd` attributes remain and now
  always carry USD, whichever dimension tripped; `*_value` carries the tripping
  dimension's unit.
- `OpenRouterClient.chat` reports `prompt_tokens` excluding cached input, matching the
  other clients and `pf_core.pricing`. Recorded `prompt_tokens` drops by
  `cache_read_tokens` on cached calls. Rows written by earlier versions hold the inclusive
  convention, so a table spanning the upgrade carries both: `cost_by_model`'s
  `billable_input` over-reports them and the `tokens` budget dimension double-counts their
  cached input. `docs/llm-tracking.md` has the one-line backfill.
- `now_expr("sqlite")` emits `strftime('%Y-%m-%d %H:%M:%f000', 'now')` — space-separated
  and six fractional digits (was ISO `T`/`Z` with three), matching how SQLAlchemy renders
  a bound `DateTime`. A SQLite column holding values stamped by the old form no longer
  compares against a bound datetime; backfill with
  `UPDATE t SET c = replace(rtrim(c, 'Z'), 'T', ' ') || '000' WHERE c LIKE '%T%'` — the
  `WHERE` is required, or whole seconds also gain `000`. Mixed old/new rows sort wrong
  within a calendar date (`T` sorts above a space) and read back as a mix of aware and
  naive datetimes. `docs/db-dialect.md` has the per-column recipe.
- `now_expr("mysql")` emits `CURRENT_TIMESTAMP(6)` (was `CURRENT_TIMESTAMP`). As a DDL
  default that is valid only against an fsp-6 column, or MySQL rejects the statement with
  `ERROR 1067`. `now_expr` and `on_update_now_clause` take `fractional=False` to pair with
  `timestamp_type(dialect, fractional=False)`.
- The exact cache is keyed on `(input_hash, agent_type_id)`, not `input_hash` alone —
  `input_hash` never covered the agent, so one agent could be served another's stored
  response and the hit was filed under the wrong agent. Schema: the
  `uq_llm_cache_input_hash` unique constraint is replaced by
  `uq_llm_cache_input_hash_agent`. Existing rows stay valid; the first call from each
  other agent misses once and stores its own entry.
- `BudgetSnapshotRepo.upsert` takes `spent_tokens`; `None` leaves an existing row's total
  unchanged.
- Schema: `llm_budgets.limit_usd` is now nullable and the table gains `limit_tokens` /
  `limit_calls`; `llm_budget_snapshots` gains `spent_tokens`. Consumers add these via an
  alembic revision — the column list is in `docs/cost-budget.md`.

### Added
- Token and call budget dimensions. A `budgets.yaml` period accepts a mapping —
  `daily: {usd: 20.0, tokens: 2000000, calls: 500}` — alongside the bare-number USD form;
  `check_budget` takes `projected_tokens=` / `projected_calls=` (calls default to 1; pass
  an explicit 0 for a pure read) and enforces every configured dimension; `current_usage()`
  returns spend in all three; `CostBudgetExceeded.dimension` names what tripped; the admin
  `/budgets` page shows per-dimension usage. Token counts sum all four token columns. Soft
  thresholds remain USD-only.
- The claude_code client reports token counts parsed from the CLI's `--output-format json`
  envelope. An envelope without usage counts logs `claude_code_envelope_without_usage`;
  `cost_usd` stays 0.0.
- `pf_core.llm.validate.validate_value` — run a registered pipeline (shape/semantic/
  cross-field) on an already-parsed value, persisting signals and the schema tag exactly as
  `parse_and_validate` does.
- `llm_step(dry_run=True)` — resolve the input hash and probe the exact cache with no
  client call, budget check, or DB write — and `cache_stats()`, a per-agent entries/hits/
  distinct-hash diagnostic (also on the admin `/cache` page and `/api/cache.json`).
- `LlmRunRepo.mark_failed(run_id, *, error, error_class=None)` — flip an already-recorded
  run to `failed` instead of inserting a second zero-token row; unknown ids warn without
  raising.
- `PERMANENT_FETCH_STATUSES` and `is_permanent(status)` in `pf_core.utils.article_fetch`;
  unrecognized statuses are retryable.
- `pf_core.db.dialect` — SQL fragments resolved from a live connection: `now_sql(conn)`,
  `row_lock_suffix(conn)`, `utc_cutoff(seconds=/minutes=/days=)`, and the
  `insert_ignore_prefix` / `insert_ignore_suffix` pair. `now_sql` is `now_expr` resolved
  from a connection, so the two cannot drift. `insert_ignore_prefix` accepts a connection
  or a dialect string; MariaDB names normalize to the MySQL family; unsupported arguments
  raise `InvalidInputError`. All five are re-exported from `pf_core.db`.
- `docs/db-dialect.md`, covering which cutoff style to use and how to bind `utc_cutoff`.

### Fixed
- `OpenRouterClient` prices its local cost estimate from the same token split it records,
  passing `cache_read_tokens` / `cache_write_tokens` to `estimate_cost`. It previously fed
  the cache-inclusive prompt count and no cache columns, so `cost_usd` on a route that
  reports no `usage.cost` could not be reproduced from the recorded row.
- `LlmRunStatsRepo.cost_by_model` no longer subtracts `cache_read_tokens` from
  `prompt_tokens`, which reported a negative `billable_input` for every Anthropic-family
  run. `prompt_tokens` is the uncached input across all clients.
- `ClaudeCodeError` subclasses `ClientError` (was `AppError` directly), matching
  `OpenRouterError` and `AnthropicError`. Existing catches are unaffected.
- `parse_and_validate` accepts `usage=` and derives truncation itself; under
  `on_truncation="warn"` a known-truncated response records a `<agent>_truncated` warn
  signal. `cache_store` accepts `usage=`/`truncated=` and refuses to store a
  known-truncated response. That signal is a `passed=False` row, so
  `purge_old_payloads(keep_flagged=True)` — the default — retains a truncated run's
  payload; pass `keep_flagged=False` to reclaim it.
- `tracked_call` marks the rejected run's row failed (`error_class="LlmJsonError"`) before
  raising on JSON-parse exhaustion — the retry row when a retry ran, else the original.
- `bin/pf-eval compare` reads the new `compare_experiments` return shape.
- `compare_experiments` skips replays whose `eval_score` outcome has a NULL score; it
  previously raised `TypeError` on the first one.
- Experiment tags match exactly on every backend. MySQL's default collation is
  case-insensitive, so a mistyped `experiment:V5` selected `experiment:v5`'s rows and
  reported a phantom all-zero-delta comparison instead of raising.
- `fetch_article` returns an `error` stub for a non-string url instead of raising
  `AttributeError`.
- The admin `/budgets` page renders a budget whose limit is `0` as fully consumed rather
  than as 0%, and labels its percentage with the dimension it came from.
- `bin/new-consumer` skips tool cache directories (`.ruff_cache`, `.pytest_cache`,
  `.mypy_cache`) when stamping a template.

### Changed
- `BUDGET_ENFORCEMENT_DISABLED` and `PF_ARTICLE_WAYBACK_FALLBACK` resolve through
  `pf_core.utils.env.resolve_bool`, so they accept `on`/`off`/`false`/`no` and tolerate
  surrounding whitespace. `PF_ARTICLE_WAYBACK_FALLBACK` previously honoured only a literal
  `0`, leaving every other falsy spelling switched on.
- `get_engine` pins the Postgres session to UTC (`SET TIME ZONE 'UTC'`), as it already did
  for MySQL. pf-core's timestamp columns are `timestamptz` there, so an unpinned session
  read a bound naive-UTC cutoff in the server's zone and skewed every comparison by that
  offset.
- `pf_core.db.insert_ignore_prefix` resolves through `pf_core.db.dialect` and raises
  `InvalidInputError` (was `ValueError`) for an unsupported dialect — a `FlowException`,
  so a web boundary renders it 4xx rather than 500.
  `pf_core.db.json_compat.insert_ignore_prefix` still raises `ValueError`.
- `pf_core.llm.tracking._resolvers` uses the shared `insert_ignore_prefix` /
  `insert_ignore_suffix` pair.
- `utc_cutoff` raises `InvalidInputError` when given no delta.
- `cache_stats` takes `since=`; the admin `/cache` page and `/api/cache.json` pass their
  window rather than aggregating all history on every request.
- `sync_budgets_from_yaml` logs `budget_config_soft_thresholds_ignored` for a period that
  sets `soft_thresholds` without a `usd` limit — thresholds anchor to the USD cap, so on a
  tokens-only period they never fire.
- `mypy` and `ruff` cover `bin/`'s extensionless entry points, which directory traversal
  skips; `tests/test_bin_gates.py` fails when a new one is not listed in both.

## v0.21.0 — 2026-08-27

Correctness fixes from an adversarial review of the framework. Behaviour changes throughout —
a minor bump, not a patch.

### Breaking
- `pf_core.web.health.require_db()` is a plain function, not a coroutine. `Depends(require_db)`
  is unchanged; a consumer calling `await require_db()` directly must drop the `await`.
  `require_db_sync()` is unchanged.
- `AgentCacheConfig.max_entries_per_agent` is removed. Nothing ever read it — it documented an
  LRU eviction that does not exist. A `cache.yaml` still carrying the key loads fine and logs
  `cache_config_ignored_key` once.

### Fixed
- `parse_llm_json(..., on_truncation="raise")` honours a provider-reported truncation. It
  previously governed only the structured-recovery step, so a response cut off before its first
  element closed fell through to `json_repair`, which sealed the payload and returned it — a
  chopped string, or a number missing its final digits, with no exception and no warning. The
  signal is authoritative, never inferred from the text: with no signal, parsing is unchanged,
  so a malformed-but-complete payload (an unescaped inner quote) still parses exactly as before.
- A known-truncated response is never written to the exact cache, and `tracked_call` does not
  spend its JSON retry on one — the retry resends the same prompt under the same token cap and
  truncates identically.
- Runs whose `usage` carries a truncation finish reason are tagged `truncated` in
  `llm_run_tags`, so cut-off calls in a batch are queryable after the fact.

- `compute_input_hash` keys off the whole `messages` list whenever rendering cannot represent
  it — multi-part (vision) content, assistant/tool turns, `tool_calls`, `name`. Every multipart
  call on one model previously collapsed to a single `input_hash`, so `llm_step(cache=True)`
  returned the first page's answer for every later page. `@track_run` and `tracked_messages_call`
  now compute the same key rather than leaving it to `LlmRunRepo.record()`, whose rendered-prompt
  fallback cannot see those parts. Lists of plain string `system`/`user` messages keep their
  existing hash; other shapes re-key and repopulate on next use.
- Job leases are renewed while a worker is alive: every job write restamps `claimed_at`, and
  `run_subprocess_job` renews on a timer while its child runs. `reclaim_stale` no longer
  re-queues a still-running job, so a second worker cannot execute it concurrently.
- `GET /health` and `require_db()` are sync, so FastAPI runs the blocking DB probe in the
  threadpool instead of on the event loop, where one slow connect stalled every route in the
  worker. The DB connect is bounded by `DB_CONNECT_TIMEOUT_S`, and an explicit `connect_timeout`
  already in `DATABASE_URL` still wins.
- The admin `/budgets` page computes spend the way the guard does — snapshot plus live delta,
  keyed on the UTC period — instead of a snapshot-only figure keyed on the host's local date.
  A budget already blocking calls no longer displays `$0.00`.
- `eval.yaml` metric gates fire. The runner read back metrics from a replay run nothing wrote
  them to, so every gate silently passed.
- `structured_diff` no longer scores 1.0 for a field absent from both sides, which made a
  typo'd or renamed `diff_fields` certify any replay as a perfect match.
- `parse_body_html` tracks its buffer offset incrementally instead of re-summing every chunk on
  each anchor — the walk was quadratic, so a 1.5 MB link-heavy page burned ~15 s of CPU.
- `pf_tables` applies a `pf_schema` fixture whether or not the test names it. A plain
  non-autouse `pf_schema`, exactly as `docs/testing.md` shows it, previously created zero tables.
  Marker DDL that redefines an object `pf_schema` already creates now raises `InvalidInputError`
  naming it; marker DDL that drops the object first is a replacement, not a collision.
- A `cache.yaml` section of the wrong YAML type (`agents: "searcher"`, `defaults: "nope"`) falls
  back to framework defaults instead of raising `AttributeError`, matching the loader's
  documented fail-empty contract.

### Added
- `pf_core.llm.truncated_from_usage(usage)` — three-way verdict (`True`/`False`/`None`) from
  `usage["finish_reason"]`, recognising each provider's vocabulary; unrecognised values are
  unknown, not complete. `parse_llm_json` and `parse_and_validate` accept `truncated=`;
  `llm_step` gains `on_truncation="warn"|"fail"` (`"fail"` needs `validate` set).
- `AnthropicClient` reports `usage["finish_reason"]` from the SDK's `stop_reason`;
  `ClaudeCodeClient` runs `claude --print` with `--output-format json` and reports it from the
  envelope's `stop_reason`. Passing `--output-format` or `--verbose` via `extra_args` suppresses
  the envelope (text mode, no signal); an unreadable envelope in the client's own JSON mode
  raises `ClaudeCodeError` naming the remediation rather than returning machine output.
- `JobRepo.renew_lease(job_id)` — heartbeat for worker loops that go longer than the lease
  without a write. Returns `False` once the claim is gone.

## v0.20.0 — 2026-08-26

### Added
- **mypy is a build gate** — `[tool.mypy]` config, zero errors across `src/`, typed via
  stubs (`types-PyYAML`, `types-PyMySQL`, `types-jsonschema`, `types-nanoid`) and
  behavior-neutral annotations. Strict mode is not enabled. Runs in pre-commit and CI.
- **`ruff format` is a build gate** — `src/` and `tests/` are formatted and both
  pre-commit and CI check it. E501 stays off: the formatter cannot break long string
  literals, so a line-length rule would force worse code in Jinja blocks and docstrings.

### Changed
- **Python 3.12 is the floor** — `requires-python >=3.12`; the 3.11 CI leg is dropped,
  ruff and mypy target py312, and the floor is applied everywhere it is restated:
  `pf-doctor`, `bin/setup-common`, `bin/verify-bare-install`, and the consumer templates.
- Dependency floors raised — see `pyproject.toml`. Environments older than these floors
  must upgrade. Three ceilings: `ruff` and `mypy` are compatible-release pinned so a new
  minor cannot turn CI red on its own, and `anthropic` is capped below 1.0 (see Fixed).
- The `guards` workflow installs `[dev]` rather than its own pinned ruff, which had
  drifted a minor behind `pyproject.toml`.
- `pf_core.doctor` is split into `doctor` plus private `_doctor_types` and
  `_doctor_release` modules, to stay inside the file-size gate after the format pass.
  Every public name is unchanged and still imports from `pf_core.doctor`.

### Fixed
- The `[anthropic]` extra is capped at `anthropic>=0.105,<1.0`. SDK 1.0 removed
  `temperature` / `top_p` / `top_k` from `messages.create()`, which
  `AnthropicClient.chat()` sends by default, so every call raised
  `TypeError`. The previous `>=0.105` floor already admitted 1.0, so this
  affects installs from earlier versions too. Supporting 1.0 requires porting
  the client — those parameters are gone, not renamed.
- `canonical_url` and `domain_of` honour their unparseable-input contract for a malformed
  netloc. `urlparse` defers netloc validation to the `.hostname`/`.port` properties, so an
  out-of-range or non-numeric port (`https://example.com:99999/x`) raised `ValueError` out
  of `canonical_url` instead of returning `""`, and an unclosed IPv6 bracket did the same
  out of `domain_of`.

## v0.19.0 — 2026-08-09

### Fixed
- `run_cli` escapes an exception message before printing it. Rich read any bracketed run as markup, so `give it two numbers [x, y]` printed without the `[x, y]`, and a message containing `[/]` raised `MarkupError` inside the handler — replacing the error report with a traceback. Both the `FlowException` and `AppError` branches print through `_print_error`.
- `ConsoleReporter` escapes the same way, on all five methods. A reported message containing `[/]` raised `MarkupError` from `error()` itself, so reporting a failure became a traceback.
- A `budgets.yaml` that cannot be read no longer disarms every cap — previously an unparseable file read as "no scopes" and disabled every enabled row. The three states are now distinct: absent is a no-op (the guard is opt-in), unreadable raises `ConfigurationError` out of `load_yaml()`, and a config resolving to zero scopes disables nothing and logs `budget_sync_refused_mass_disable`. That last case covers an absent file, an unmounted symlink target, and a mistyped section name (`agent:` for `agents:`), none of which raise.
- `ConfigurationError` now covers the whole malformed-config family, so one `except` clause catches it and `stale_on` can hold the last good config: a permission error, a symlink loop, a symlink to a missing target, a non-UTF-8 body, deep-nesting `RecursionError`, a section of the wrong type (`agents: "nope"`), and a non-numeric limit (`daily: "20 USD"`). Reading drives off `open()` rather than `Path.exists()`, closing the delete-between-check-and-open race.
- Unrecognised top-level keys in `budgets.yaml` log `budget_config_unknown_sections` at WARNING instead of silently defining no caps.
- `check_budget` logs `budget_no_scopes_matched` when no enabled row matches, so an uncapped call is visible — WARNING the first time a given `(agent_type, job_kind, tags)` combination goes unmatched, DEBUG after. `job_id` is excluded from the dedupe key as unbounded and the set is capped, so caller-supplied tags cannot grow it without limit. A process restart re-arms.

### Changed
- **Breaking (narrow):** `load_yaml()` raises `ConfigurationError` for a budget config that exists but is unreadable or is not a top-level mapping; it previously returned `{}` after logging a warning. The budget config `ReloadCache` is constructed with `stale_on=(ConfigurationError,)`, so a *reload* that fails serves the last good config and logs `reload_cache_kept_stale`; only a first load with nothing cached raises.
- **Breaking (narrow):** `BudgetRepo.sync_from_desired([])` disables nothing and logs `budget_sync_refused_mass_disable`; it previously disabled every enabled row. Removing a subset of scopes still disables that subset.
- `sync_budgets_from_yaml()` never raises on a bad config file. An unreadable config logs `budget_config_unreadable` at ERROR and leaves every existing row untouched, so consumers that call it at boot keep starting and the last synced caps keep enforcing. Database errors from the write still propagate. Call `load_yaml()` first to fail fast on the config instead.

## v0.18.1 — 2026-08-05

### Fixed
- `create_region` degrades when Redis is **down**, not only when it is unconfigured: reads return `NO_VALUE`, writes and deletes no-op, and `get_or_create` calls its creator. Degradation is per-operation, so a recovered server is used again without a restart. Any `RedisError` or `OSError` degrades, including `ResponseError` (out-of-memory, read-only replica); nothing outside those two is caught. `RedisCache.set()` / `.delete()` return `False` when the backend degraded and `True` on a null backend. Availability cannot be inferred from the backend type — PING `region.backend.reader_client`.
- `price_call` returns `None` for an unpriced model, where a genuinely zero-rate model returns `0.0`, so a `block` budget no longer passes spend it could not price. `llm_runs.cost_usd` stays a non-null float. `get_rates` searches every rate table when a provider has none of its own, so a bare model name resolves.
- Budget snapshot refresh and the live delta share one cutoff, read from the DB server clock (`repo.db_now`).
- `aggregate_spent` and `check._delta` share one `apply_scope_filter`, so an unrecognised `scope_kind` resolves identically in both.
- The soft-threshold dedupe key uses the UTC period start, matching every other period boundary.
- All three LLM clients back off between retries; OpenRouter honours `Retry-After`, clamped to 0–30 seconds.
- The Anthropic client retries only 408, 429, 500, 502, 503, 504, 529 and connect-class timeouts. Other 4xx/5xx and read/write timeouts raise immediately.
- `OpenRouterClient.chat` raises `OpenRouterError` on an empty `choices` array, and surfaces a `max_tokens` truncation rather than returning the short string as complete.
- A tracking-DB failure no longer replaces the LLM exception that caused it; `tracked_call` gains the `on_record_error` escape hatch `tracked_messages_call` already had.
- `run_parallel` attempts every item at both widths and surfaces every failure.
- `atomic_write_text` / `atomic_write_bytes` fsync the parent directory after `os.replace`, so the rename survives power loss.
- `write_run_record` writes atomically.
- `safe_markdown` leaves generated link targets alone; a URL containing `*` keeps its `href`.
- `get_engine` holds a lock across the cache check and assignment, so concurrent callers share one engine.
- `Fetcher(retries=-1)` raises `InvalidInputError`.
- The image localizer treats a trailing dot-segment as an extension only when it looks like one (a dot, then 1–5 alphanumerics), so an extensionless CDN ref carrying a version or variant — `…/whats-new-asset-v3.1-large`, `…/gen-upscale-2025.jpg-1` — is downloaded and counted rather than skipped.
- `extract_json`, `extract_json_array` and `extract_json_object` rank every balanced span rather than taking the first: a non-empty span at offset 0 wins, then one carrying a non-empty object, then the earliest. A prose bracket loses whether or not it is itself valid JSON, and `{}` / `{placeholder}` lose on the object side. A span that fails to parse is skipped whole, so `extract_json_array` and `extract_json_object` leave a malformed container to `json_repair`; `extract_json` scans braces and brackets independently and can still return an inner array from a failed object span. Residual: a scalar-only payload ranks equal to a scalar decoy and the earliest wins — see `docs/json-recovery.md`.
- `recover_truncated_json` anchors on the first **unclosed** bracket, so a complete bracket earlier in the text is treated as prose.
- `parse_llm_json(expect="any")` delegates to `extract_json`, so `expect="object"` and `expect="any"` resolve the same input identically.

### Changed
- **Breaking:** `tracked_call` defaults to `on_truncation="raise"` — a truncated JSON response raises instead of returning a short list. `parse_llm_json` keeps `on_truncation="warn"`, so the `parse_llm_json(...) or []` idiom is unaffected.
- **Breaking:** `RedisCache.set()` no longer accepts `ttl`; passing it raises `TypeError`. Use `cached_json(ttl=...)`, which forwards to `get_or_create(expiration_time=...)`.
- **Breaking (narrow):** `call_with_fallback` does not retry a `FlowException` on the next backend. Pass `retry_on` to override the set exactly.
- **Breaking (narrow):** the Anthropic SDK client is constructed with `max_retries=0`; pf-core's own loop owns retrying.
- **Breaking:** the balanced-span scan stops at the first **unclosed** `{` or `[`, which cannot be told apart from a stray brace in prose. `extract_json_object('A [ stray bracket. Then {"a": 1}')` was `{"a": 1}` and is now `None`; `parse_llm_json` falls through to `recover_truncated_json` / `json_repair`, and may log `parse_llm_json_recovered_truncated` on a response that was never truncated. The bound is what keeps a truncated array of records from being answered with an inner field's value.
- **Breaking (narrow):** `extract_json` returns the array on an array-of-objects that misses the whole-string fast path. `extract_json('[{"a":1}] trailing text')` was `{"a": 1}` and is now `[{"a": 1}]`. Only affects text where a leading or trailing remainder defeats `json.loads`. `extract_json_array` and `extract_json_object` still filter by type.
- `parse_llm_json` holds back an empty `{}` / `[]` and returns it only if truncation recovery and `json_repair` both come up empty.
- `assert_public_url` returns the vetted addresses as `tuple[str, ...]` instead of `None`, so a caller can pin what was cleared. The check is **not** TOCTOU-safe against DNS rebinding; the module docstring and `docs/urls.md` state the real guarantee.
- `bump_generation()` invalidates the calling process only; `docs/cache.md` documents the scope and the cross-process recipe.

### Added
- `bin/test`, matching the wrapper both consumer templates already scaffold.

### Docs
- `recipes/self-consistency.md` is rewritten against the real tagging mechanism; `brave.md` points at `Throttle`; `testing.md` lists every auto-loaded fixture; the `[validate]` extra names its real modules.
- `.ai/rules/scope.md` is rewritten around ownership, so the mountable routers and framework CLIs pf-core ships are in scope.

## v0.17.0 — 2026-08-02

### Security
- `Fetcher`'s `max_bytes` bounds the **decoded** body, not just the wire read. An over-budget body raises `ClientError` mid-inflate.
- `fetch_url_content` streams its body, stops at its size cap, and requests `Accept-Encoding: identity`. A server that sends `gzip` anyway is bounded by one httpx read chunk rather than by the cap (see `docs/urls.md`).
- `make_jobs_router` raises `ConfigurationError` without `auth_dep` unless `allow_unauthenticated=True`, matching `llm_admin.make_admin_router`. **Breaking:** an explicit `auth_dep=None` now raises — pass `allow_unauthenticated=True` or wire a dependency.
- `create_app` raises `ConfigurationError` when `cors_origins` contains `*` and credentials are enabled. Membership is what counts: `["*", "https://ok.example"]` is refused too.
- The SSRF guard blocks `100.64.0.0/10` (RFC 6598 shared address space). **Breaking** for anyone deliberately fetching that range — set `URL_FETCH_ALLOW_PRIVATE=1`.

### Added
- `create_app(cors_allow_credentials=...)` — default `True`. Set `False` to serve a public read-only API with `cors_origins=["*"]`.
- `Fetcher(verify_tls=...)` — per-instance TLS-verification override, resolved once at construction.
- `PF_VERIFY_TLS` supersedes `URL_CHECK_VERIFY_TLS`, which is still honored; the new name wins when both are set. The switch was never scoped to the `[http]` tier its old name implied — it governs every `Fetcher` in the process too.
- `URL_LIVENESS_TTL_SECONDS`, `URL_LIVENESS_NEGATIVE_TTL_SECONDS`, and `check_url_cached(negative_cache_ttl_seconds=...)`.
- `url_safety.guarded_stream` — the streaming counterpart of `guarded_get`, re-validating every redirect hop.
- `pf_core.utils` re-exports `domain_of`, `archive_timestamp_is_round`, `canonical_url`, `extract_path_date`, and `extract_article_metadata` without the `[http]` extra; `fetch_url_content` and `wayback_exists_at` join the lazy map.
- `sniff_image_ext` recognizes avif, heic, bmp, tiff, ico, and jxl; those extensions are localizable. The generic HEIF brands (`mif1`, `msf1`) label as `.heic`.
- `pf-doctor` reports `PF_VERIFY_TLS`, `URL_CHECK_VERIFY_TLS`, and `URL_FETCH_ALLOW_PRIVATE`.

### Changed
- `check_url_cached` gives transient verdicts — `timeout`, `error`, and 408/425/429/5xx — a short negative TTL instead of the full one, so a brief outage no longer marks every URL checked in that window dead for a day. Both TTLs are env-tunable; `cache_ttl_seconds` is now `int | None`.
- `ExactCacheRepo.store` uses the portable `upsert` helper. The hand-rolled INSERT-then-SELECT recovery raised `InFailedSqlTransaction` on PostgreSQL for any duplicate `input_hash`. A re-store now refreshes the row in place — same id, `created_at`, and hit counters — instead of returning a possibly-expired one.
- **Breaking:** `sniff_image_ext` returns `str | None`, giving `None` for unrecognized bodies instead of `.png` — a caller doing `base + sniff_image_ext(data)` now raises `TypeError`. `localize_images` treats `None` as a per-URL failure and keeps the ref remote; the check applies to extensioned URLs too. An XML body is claimed as SVG only when `<svg` appears in the first 4 KiB.
- Malformed or truncated `gzip`/`deflate` bodies and truncated transfers raise `ClientError` instead of escaping as `gzip.BadGzipFile` / `zlib.error` / `EOFError` / `http.client.IncompleteRead`. A corrupt gzip previously surfaced as an `OSError`. These are raised outside the retry loop and are not retried.
- `localize_images` / `localize_file` contain those per-URL rather than aborting the run.
- `API_RATE_LIMIT_PER_MINUTE`, `MAX_PER_PAGE`, `URL_CHECK_TIMEOUT`, `WAYBACK_TIMEOUT`, and `REQUEST_TIMEOUT` resolve through `utils.env` — a malformed value warns and falls back instead of raising `ValueError`. `get_client(request_timeout=0)` is now honored.

## v0.16.0 — 2026-08-02

### Added
- `article_fetch.FETCH_STATUSES` — the `fetch_status` vocabulary as a frozenset, for consumers that branch on status values.
- `article_fetch.looks_binary(text)` — magic-byte detection on a decoded response body; returns a format label or `None`.
- `PF_ARTICLE_EXTRACTOR_LOG_LEVEL` — caps the `trafilatura` and `htmldate` loggers, which emit `ERROR` for ordinary unparseable bodies. Defaults to `CRITICAL`; an unrecognized level warns and falls back.

### Changed
- `fetch_article` no longer reports `ok` for a 2xx it could not extract. A binary body gets `unsupported_content_type` (detected before extraction; skips the Wayback fallback). HTML that yields no text gets `no_content` (still attempts Wayback, and keeps any `title` / `date_published` extraction recovered). Callers branching on `ok` must handle both.
- `FETCHER_VERSION` 3 → 4 — both statuses reclassify rows that previously cached as `ok`.

### Fixed
- A Wayback snapshot that extracts to nothing no longer replaces the live status.

## v0.15.1 — 2026-08-01

### Security
- **Removed the TLS chain-recovery path added in 0.15.0** (`pf_core.utils.tls_chain`, wired into `Fetcher._attempt`) — it could install a fetched certificate as a trust anchor.

### Added
- `pf_core.utils.env.resolve_float` — the `resolve_int` contract for fractional values. `nan`/`inf` are rejected like any other malformed env value; they'd otherwise silently disable a bound instead of falling back to it.

### Fixed
- `brave.get_client()` raised `ValueError` out of client construction when `BRAVE_REQUEST_TIMEOUT` or `BRAVE_COST_PER_CALL_USD` held a non-numeric value — a typo in `.env` took down the client instead of falling back. Both now resolve through `utils.env`, warning and using the default. `BRAVE_BASE_URL` moved to the same path.
- `get_client(request_timeout=0)` is now honored; the previous truthiness check discarded an explicit `0` in favor of the env var.
- Scaffolded projects' `bin/run` now resolves pf-core's console scripts from the venv, so `bin/run pf-doctor` works as `docs/doctor.md` documents it. Both templates previously dispatched sibling `bin/` scripts and then fell straight through to the project's own entry point, so every `pf-*` command exited 2 with "No such command". Dispatch is limited to the `pf-` prefix, so an installed script cannot shadow a command the project owns.

### Changed
- Both templates' `bin/setup` now closes with `bin/run pf-doctor` alongside the layout's day-1 slice, and the lib template marks that slice as demo code to replace.

### Documentation
- `docs/fetch.md` documents that `timeout_s` is per-read and bounds no request as a whole — a dribbling server resets it indefinitely and the call never returns. The `[http]` (httpx) tier has the same gap. No total-request bound exists in either; the section covers what a caller must do instead.

## v0.14.1 — 2026-07-25

### Fixed
- **`pip install pf-core[jobs]` produced an unusable install.** `[jobs]` declared `[db] + [cli] + pydantic`, but `jobs/_schema` imports `llm.tracking.schema` at module scope so job-attribution foreign keys land on the shared metadata — so `import pf_core.jobs` raised `ImportError: pf_core.llm.tracking requires the 'tracking' extra`. `[jobs]` now includes `[tracking]`. Consumers on `[full]` were unaffected (it already pulled both).
- `pf_core.llm.step` raised a raw `ModuleNotFoundError: No module named 'sqlalchemy'` instead of the friendly extra error its siblings raise; it now names the `tracking` extra and the pip command. A nested gate's own message (e.g. `[validate]`) still propagates unchanged, being more precise.

### Added
- `pf_core._extras.required_extra(module)` and its backing `_MODULE_EXTRA` map — the declarative form of the tier contract `docs/INSTALLATION.md` states in prose: which extra each module needs to import. `tests/test_extras_tiers.py` enforces it against the source tree, so a module-scope import that escapes its extra's closure fails the build instead of failing in a consumer's install. The extras DAG is read from pyproject, and the cross-tier exemption list ships empty with a staleness ratchet.

## v0.14.0 — 2026-07-25

### Fixed
- `run_cli` no longer lets CLI usage errors escape as tracebacks. typer ≥ 0.26 vendors its own copy of click, so `typer.BadParameter` and friends are not instances of the installed `click`'s `ClickException` — `run_cli` caught neither, and an unknown option, a missing argument, or a `typer.BadParameter` raised in a command body printed ~50 lines of framework internals and exited 1. It now shows the usage message and exits with the exception's own code (2 for usage errors, 1 for a plain `ClickException`). Both hierarchies are resolved at import time, so this holds across the whole supported typer range.
- `typer.Abort` now reaches its handler: the existing clause caught the installed `click`'s `Abort`, which is a different class under a vendoring typer, so Ctrl-C at a prompt fell through to a traceback instead of exiting 130.
- `AppConfig` resolves the YAML tier its docs always promised: an UPPER-CASE key in the `yaml_file` document now resolves onto the matching attribute, below env vars and above class defaults. Non-attribute keys are untouched and stay available via `cfg.yaml`.
- `AppConfig` mutable class defaults (e.g. `CORS_ORIGINS`) are copied per instance — appending to one instance no longer pollutes every other instance and the class default.
- An explicit `setup_logging()` call now reconfigures — previously the first import-time `get_logger()` won and `main()` could not change the log level. Implicit setup still never overrides explicit configuration, and a consumer's own handlers are still respected on first setup.
- `log_exception` no longer raises `TypeError` from inside the error logger when caller context carries a reserved key (`message`, `event`, `exc_info`) — such keys are logged under a `ctx_` prefix.
- A malformed `AppConfig` YAML file raises `ConfigurationError` naming the path instead of degrading silently to `{}`. A missing file is still non-fatal.
- `OpenRouterClient` no longer guards an internal invariant with a bare `assert` (stripped under `python -O`); it raises a real error.

### Changed
- A malformed invocation of a `run_cli` CLI now exits **2** instead of 1. Domain errors are unaffected — `FlowException`/`AppError` still exit 1, and `typer.Exit(N)` still propagates `N`. Consumers asserting exit 1 for a *malformed* invocation need updating; consumers asserting exit 1 for domain failures do not.
- `CostBudgetExceeded` is now a `FlowException` subclass (was a bare `Exception`), and `create_app()` maps it to **429** with the standard domain-error rendering — a budget block from a route was previously an unhandled 500. By-name catches keep working; its constructor and attributes are unchanged.
- Perplexity citations are no longer appended to `content` as a synthetic `CITATIONS` block; they are returned in `usage["citations"]` (list of URLs, present only when the provider sent them). Recorded `raw_response` and cache entries now hold exactly what the model returned.
- `pymysql.install_as_MySQLdb()` no longer runs as an import side effect of `pf_core.db`; it runs at engine creation, only for bare `mysql://`/`mariadb://` URLs (which need it). `mysql+pymysql://` URLs never trigger it.
- `get_engine(url)` logs a `get_engine_url_ignored` warning (credentials redacted) when called with a URL that differs from the cached engine's — previously the argument was silently discarded.
- `pf-doctor`'s `copy.loaded` check reads the adjacent `pyproject.toml` only for editable/source-tree installs when comparing against installed metadata (the stale-editable-version warning).
- `docs/cli.md` and the `run_cli` docstring now distinguish `typer.Abort` (prints "Interrupted.", exits 130) from `KeyboardInterrupt` (exits 130 silently — typer converts it to `Exit(130)` before `run_cli` sees it).

## v0.13.0 — 2026-07-24

### Added
- `pf_core.fetch` — polite stdlib HTTP fetching in the base install (no extra): `Fetcher` (identifying UA with `PF_FETCH_UA` override, per-request `Throttle` pacing, streamed `max_bytes` cap, gzip/deflate decoding) plus module-level `fetch_text`/`fetch_bytes`/`fetch_bytes_meta`. Status-aware retries (permanent 4xx fail fast with zero sleeps; 429 honors a capped Retry-After; 5xx/408/network back off), raw urllib exceptions propagate to callers, and redirects are walked manually with an `assert_public_url` SSRF re-check on every hop; the first return element is always the final post-redirect URL. `not_modified` does one conditional GET (True only on a definitive 304, never raises); `browser_headers()` returns a full browser-fingerprint header set for public pages that 403 the lean default.
- `pf_core.fetch.images` — remote-image localizer for markdown/HTML-derived documents: downloads `![…](http…)` and `<img src>` refs (plus base_url-resolved relative refs) into a local `images/` dir and retargets them anchor-safely. Deterministic collision-resistant naming (`default_namer`, injectable), magic-byte extension sniffing for extensionless CDN URLs, reuse-not-refetch probing, per-URL failure containment (failed refs stay remote), and a resumable `localize_file` mode where the document is the progress ledger, checkpointed atomically every N images.
- `pf_core.utils.reload_cache.ReloadCache` — the TTL hot-reload primitive behind config loaders: double-checked lock, per-call TTL read (env changes land without restart), key-change invalidation, `force=`/`clear()`, optional `stale_on=` serve-stale with retry throttling. The model-router, LLM-cache, and budget config loaders now run on it.
- `pf_core.web.require_db_sync` — plain-function twin of `require_db` for inline guards (calling the async one inline returns an un-awaited coroutine and silently skips the check); also works under `Depends()`.
- `pf_core.utils.io.atomic_write_bytes` — bytes twin of `atomic_write_text`.

### Fixed
- Malformed `CACHE_CONFIG_RELOAD_SECONDS` / `BUDGET_CONFIG_RELOAD_SECONDS` values now warn and fall back to their defaults instead of crashing; `CACHE_CONFIG` / `BUDGET_CONFIG` path changes invalidate their caches immediately instead of waiting out the TTL. The LLM-cache config loader also gains the lock the other loaders already had.

### Changed
- The model-router kept-stale reload warning event is `reload_cache_kept_stale` (was `model_router_reload_failed_keeping_cache`).

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
