# Structural Guards (Build Gate)

`pf_core.guards` turns documented structural rules into a **build gate** — a check that fails pre-commit and CI when a rule is broken, so neither a human nor an AI agent can land a violation. Foundation module, stdlib-only (`ast`, `tokenize`, `pathlib`, `json`, `argparse`, `tomllib`); no extra required.

One command runs every check — the **file-size gate** (flat limits for library code, per-layer limits for consumer `app/` trees), the **layering checker**, and, once a consumer opts in, the **function-length limit**, the **comment budget** and the **framework check** (code that hand-rolls what pf-core already ships):

```bash
python -m pf_core.guards          # reads .pf-guards.toml at the repo root
```

Exit `1` on any hard size violation, any function over its hard limit, any module or tree over its comment budget, any layering violation, any framework breach, any file an opted-in check cannot parse, or any **stale** baseline/allowlist/exemption entry (an exception that's no longer real must be removed — the ratchet's other half); soft violations WARN without failing; exit `2` on a misconfigured gate — missing config file, missing scan root, malformed TOML, an unknown key under `[tool.pf_guards]`, nonsense limit values (zero/negative limits, `soft_fraction` outside `(0, 1]`), or a framework exemption without a reason — so misconfiguration fails loudly instead of silently passing. Installed as the `pf-guards` console script.

---

## Table of Contents

- [Configuration](#configuration)
- [File-size gate](#file-size-gate)
- [Baseline (adopting the gate on a dirty tree)](#baseline-adopting-the-gate-on-a-dirty-tree)
- [Function-length limit](#function-length-limit)
- [Comment budget](#comment-budget)
- [Layering checker](#layering-checker)
- [Framework check](#framework-check)
- [Files that do not parse](#files-that-do-not-parse)
- [How it's wired](#how-its-wired)
- [Consumer adoption](#consumer-adoption)
- [Adding a framework rule](#adding-a-framework-rule)

## Configuration

`[tool.pf_guards]` in `.pf-guards.toml` configures the gate.

Gate config lives in a dedicated `.pf-guards.toml` at the repo root — pf-core and every consumer alike. It's repo state (scan root, baseline burn-down, allowlisted edges), not package metadata; `pyproject.toml` is never read. The gate reads `.pf-guards.toml` by default; `--config` overrides the path. A missing config file exits `2` — except the default path with `--root` given, which runs flag-specified (adoption / ad-hoc runs). Explicit CLI flags (`--config`, `--root`, `--hard`, `--soft`, `--baseline`) override config values.

```toml
[tool.pf_guards]
root = "src/pf_core"                    # scan root (default "src")
hard = 600                              # optional: override the flat hard limit
soft = 350                              # optional: override the flat soft target
util = 200                              # optional: override the _util*.py budget
soft_fraction = 0.9                     # optional: layer soft warn as a fraction of hard

[tool.pf_guards.layers]                 # optional: override built-in per-layer limits
orchestrators = 450

[tool.pf_guards.limits]                 # optional: path-prefix hard-limit overrides
"app/api/admin" = 600                   # longest matching prefix wins

[tool.pf_guards.baseline]               # optional: grandfathered files, stale-checked
"pkg/big_module.py" = 750               # path -> line count when grandfathered

[tool.pf_guards.max_function_lines]     # optional: turns the function-length limit on
hard = 60                               # or the shorthand max_function_lines = 60

[tool.pf_guards.comment_budget]         # optional: turns the comment budget on; empty = defaults

[tool.pf_guards.allowed_imports]        # optional: per-layer layering overrides
api = ["services", "orchestrators", "repo", "db"]   # replaces the api allow-set
workers = ["services", "db"]                        # a new key declares a new checked layer

[tool.pf_guards.layering_allowlist]     # optional: named exceptions, stale-checked
"app/db/cache.py" = ["app.services.parsers.rss"]    # deliberate edge, tracked for burn-down
```

**An unknown key under `[tool.pf_guards]` is refused by name** (exit `2`), with the nearest known key suggested — each opt-in check is switched on by its key's presence, so a misspelling (`framwork`, `max_function_line`) would otherwise leave it off with nothing saying so. Keys inside `layers`, `limits`, `baseline`, `allowed_imports` and `layering_allowlist` are names and paths, and are not checked.

`root` may also be a **list** (`root = ["app", "tests"]`) to gate several trees in one
run — reported paths are then prefixed with their root, and `[tool.pf_guards.limits]`
prefix budgets apply to non-app roots too (e.g. `tests = 600`).

The canonical limit values are code, not this doc: the flat defaults on `GuardsConfig`, and the per-layer table in `LAYER_DEFAULTS` / `UTIL_LIMIT` / `SOFT_FRACTION` — all in [`pf_core/guards/config.py`](../guards/config.py). The example values above are deliberately arbitrary overrides.

## File-size gate

Library code (anything not under an `app/<layer>/` path) uses the flat limits:

- **Over the hard limit** → `FAIL`, exit 1. Blocks the commit / CI.
- **Over the soft target** → `WARN`, exit 0. Self-reports without blocking.

Files under a consumer **`app/` tree** get per-layer hard limits instead — tightest for `app/cli/`, a dedicated low budget for `_util*.py` anywhere under `app/`, a higher budget for `app/orchestrators/` — with the soft target at a fixed fraction of each hard limit (`SOFT_FRACTION`). The values live in `LAYER_DEFAULTS` / `UTIL_LIMIT` in [`pf_core/guards/config.py`](../guards/config.py); read them there.

Precedence per file: `[tool.pf_guards.limits]` prefix override (longest wins) > `_util*` rule > layer limit > flat hard. Both scan shapes work: root above the app dir, or root *being* the app dir.

A file's size is its rows as Python numbers them — ended by `\n`, `\r\n` or a bare `\r`, never by a form feed — counted from its bytes, so a file in any source encoding counts.

## Baseline (adopting the gate on a dirty tree)

A gate that requires a green tree on day one can't be adopted by a repo that already has violations. The **baseline** grandfathers known offenders — a `[tool.pf_guards.baseline]` table of `path → recorded line count` in `.pf-guards.toml` (generate it with `--emit-baseline`). The `--baseline file.json` CLI flag accepts the same map as a JSON file for ad-hoc runs.

- A baselined file at or **below** its recorded count → suppressed (no failure).
- A baselined file **grown beyond** its recorded count → reported as a hard FAIL. The ratchet only tightens.
- A **new** file over the hard limit (not in the baseline) → reported.
- A baselined file **no longer over its hard limit at all** → `STALE baseline entry`, exit 1
  until the entry is removed. Dead grandfathering is enforced away, not just discouraged —
  the baseline can only shrink.

When a baselined file legitimately must grow, **split it** — do not bump the number. Bumping defeats the ratchet.

The baseline is also the file-size gate's exemption mechanism: there is no permanent `exempt` list for sizes. A file that needs a pass goes in the baseline, where growth still fails — exemptions stay temporary by construction.

pf-core itself carries **no baseline** — the files grandfathered at the gate's adoption were split by concern, so the framework passes its own gate with zero exceptions.

## Function-length limit

Off until `.pf-guards.toml` sets `max_function_lines`, so upgrading pf-core never turns a consumer's gate red. It uses the gate's own roots:

```toml
[tool.pf_guards]
max_function_lines = 60                 # the hard limit; soft = 60 x soft_fraction
```

or, with every knob:

```toml
[tool.pf_guards.max_function_lines]
hard = 60                               # required
soft = 45                               # optional; default hard x soft_fraction
layers = { cli = 30 }                   # optional: hard limits per app/<layer>/
limits = { tests = 100 }                # optional: path-prefix hard limits, longest wins
```

- **Over the hard limit** → `FAIL`, exit 1. **Over the soft target** → `WARN`, exit 0. Set `soft` equal to `hard` to silence warnings under the flat limit.
- A layer or prefix limit takes its soft target from `soft_fraction`, as the file-size gate does. Precedence per file: prefix limit > layer limit > flat `hard` / `soft`.
- A function is measured from its `def` line to its last line: blanks, comments and the docstring count, decorators do not. A nested function is reported on its own under its qualified name (`outer.inner`, `Class.method`) and also counts toward its parent.
- Every offender is named with its length and its limit:

```
FAIL  pkg/export.py:88 Exporter.run: 74 lines (function hard limit 60)
```

There is no baseline for functions: an over-long function is split, or its path gets a higher prefix limit. From Python, `scan_function_lengths(root, limits)` returns the offenders, with `limits` from `load_guards_config().max_function_lines`.

## Comment budget

A ceiling on comment and docstring prose, as a ratio of prose lines to code lines, for a project whose written comment rule has not held on its own. Off until `.pf-guards.toml` has the table; an empty table takes the defaults on `CommentBudget` in [`pf_core/guards/comments.py`](../guards/comments.py):

```toml
[tool.pf_guards.comment_budget]         # every key optional; these values are arbitrary overrides
file = 0.6                              # ceiling per module
total = 0.3                             # ceiling across total_root
min_code_lines = 40                     # a module with less code is not judged on its own
total_root = "src/mypkg"                # the scanned roots the total counts; default every root
```

- A line is **prose** when it holds a comment (a trailing one included) or falls inside a module, class or function docstring; **code** is every other non-blank line. A string that is not a docstring is code.
- The per-module ceiling is looser than the total because one honest module docstring dominates a short module; below `min_code_lines` the module's ratio says more about its size than its prose, so only the total counts it.
- Any module over `file`, or the whole scan over `total`, fails the gate:

```
PROSE services/export.py: 41 prose lines to 60 code (68%, limit 60%)
PROSE total: 2610 prose lines to 8400 code (31%, limit 30%)
```

The defaults suit an application; a library that documents its public API in docstrings runs higher, so set its ceilings from its measured ratio instead. It scans the gate's roots, tests included when `root` lists them: every module is held to `file`, but the total counts only `total_root` (a path or a list, each one of the gate's roots; exit `2` otherwise). With `root = ["src/mypkg", "tests"]`, set `total_root = "src/mypkg"` — otherwise the tests' plain code dilutes the package's ratio, and a package over its total passes. The total's line then names its scope: `PROSE total (src/mypkg): …`. From Python, `scan_comment_budget(roots, CommentBudget())` returns the modules over budget and, last, the total when it is over.

## Layering checker

`check_layering(root)` flags imports that violate the four-layer call direction, using explicit per-layer allow-sets:

```
cli / api → orchestrators → services → repo / clients → db
```

| Layer | May import |
|---|---|
| `api`, `cli` | `services`, `orchestrators`, `db` |
| `orchestrators` | `services`, `db` — importing `repo`/`clients` is a violation, as is importing `pf_core.db` (opening `transaction()`) directly |
| `services` | `repo`, `clients`, `db` |
| `repo`, `clients` | `db` only |
| `db` | no app layers — the bottom layer |

Violations report the file, **line number**, and a hint (`LAYER app/api/_util.py:12: import app.repo.catalog (api → repo, should go through services)`). **Relative imports are resolved** against the importing file's package (`from ..repo import entries` in an orchestrator is caught as `app.repo.entries`), so they can't evade the check. Files under `tests/`, `conftest.py`, and files carrying `# lint-layers: skip` in their first 5 lines are skipped. Layer is inferred from the `app/<layer>/` path segment; files outside that structure are ignored — which is why pf-core itself (a library, not a four-layer app) is a no-op for this check.

The rules are policy a consumer owns, not code it must fork: `[tool.pf_guards.allowed_imports]` replaces the allow-set for any layer it names (defaults stay for the rest; a new key declares a new checked layer), and `[tool.pf_guards.layering_allowlist]` permits named `(app-relative path → exact imported module)` edges — deliberate exceptions, visible in config. **The allowlist is stale-checked:** an entry that no longer matches a real violation is reported as `STALE allowlist entry` and fails the gate until deleted, so the list only ever shrinks — it cannot rot into a legacy pile. Prefer it over `# lint-layers: skip` (which silences a whole file). Allowlist keys are always app-relative (`app/…`), regardless of the scan root shape.

**Adopting the gate on a tree with existing violations:** run `python -m pf_core.guards --root app --emit-baseline --emit-allowlist` (`--root` lets the run work before `.pf-guards.toml` exists) — it prints paste-ready `[tool.pf_guards.baseline]` and `[tool.pf_guards.layering_allowlist]` blocks for the current violations. Paste them into `.pf-guards.toml`, re-run, and the gate is green with every exception named; fix a file or an edge and the stale check forces its entry out.

## Framework check

The framework check refuses code that hand-rolls what pf-core already ships; every message names the replacement, so reading the failure is the whole of learning the fix:

```
FRAMEWORK store/cache.py:41: os.replace(tmp, path) — use pf_core.utils.io.atomic_write_text / _json / _bytes (the temp-file-then-rename dance is already written and tested) [atomic-write]
```

**It is opt-in:** it runs only when `.pf-guards.toml` has a `[tool.pf_guards.framework]` table, so upgrading pf-core never turns a consumer's gate red. An empty table turns every rule on.

### What it checks

- **Imports** of modules pf-core replaces — `logging`, `dotenv`, HTTP clients, thread and process pools, `hashlib`. Submodules and `from x import y` count; a relative import of a same-named local module does not.
- **Raises** of the builtins `Exception`, `RuntimeError` and `ValueError` — raise a `pf_core.exceptions` class instead. The named replacements, `PreconditionError` and `InvalidInputError`, subclass the builtin, so the swap keeps every existing `except` working.
- **Calls**, as regexes over code: `env-read` (`os.environ` / `os.getenv`), `print` (a call — a `def print` is not one), `logger-exception` (`.exception(` on a logger name: `logger`, `_log`, `log`, `self._logger`, `logging`, `app_log`), `atomic-write` (`os.replace`). `env-read` also follows the names a module binds instead, from the AST: `import os as o` then `o.environ` / `o.getenv`, and every use of a name `from os import environ, getenv` binds (`as` included). One breach per line however it is spelled.
- **`json-write`** — `.write_text(json.dumps(...))`, a torn write in waiting.

The canonical table is `RULES` in [`pf_core/guards/framework_rules.py`](../guards/framework_rules.py). A breach line ends with its rule name in brackets — the name `disable`, `replace` and `exempt` take.

**Prose never trips it.** Strings, comments and f-string text are blanked before the regex rules run (an f-string's `{expressions}` stay code); imports, raises and the JSON write are read from the AST. A gate that fires on a docstring gets `--no-verify`'d and then protects nothing.

### Carve-outs

These are not breaches and need no exemption:

- **`ValueError` lexically inside a pydantic validator** — pydantic converts only `ValueError` and `AssertionError` into a `ValidationError`. A validator is a function decorated with `field_validator` / `model_validator` / `validator` / `root_validator` **imported from pydantic** (`from pydantic import field_validator as fv`, `pydantic.field_validator`, `pydantic.v1`), or one handed to pydantic's `AfterValidator` / `BeforeValidator` / `PlainValidator` / `WrapValidator` in an `Annotated[...]` (a lambda there carves out the functions it calls). A same-named decorator from anywhere else — attrs' `@field.validator` — is not pydantic. A helper the validator *calls* is still checked.
- **`ValueError` inside an argparse converter** — the function passed as `type=` to `add_argument`, or each function a `type=lambda` calls, resolved through the passing module: a function defined there, a method of a class defined or imported there (`type=self._positive`, `type=cls.parse`, `type=Level.parse`), or a function imported into it from another file under the scan root (`from .conv import lpi`, `type=conv.lpi`). An import is resolved to the module it names — the scan root's package chain when the root is a package (`src/mypkg` is `mypkg`), else top-level or the namespace package the root's directory names — never by name alone: `type=json.loads` carves out no one's `loads`, and `from pf_core.utils import dates` carves out nothing in a local `dates.py`. argparse turns the `ValueError` into a usage error.
- **`**os.environ` in a dict literal passed as a call's `env=`** — handing the environment to a subprocess (`subprocess.run(cmd, env={**os.environ, "LC_ALL": "C"})`) reads no setting, and neither does the same through another name (`**o.environ`, `**environ`). Only that shape: a copy kept in a variable (`env = {**os.environ}`), unpacked into anything else (`Settings(**os.environ)`), or passed as `env=dict(os.environ)` / `os.environ.copy()` is a read, and so are `os.environ[...]`, `.get(...)` and `os.getenv(...)`, even beside the unpacking.

Only `ValueError` is carved out: neither pydantic nor argparse converts a `RuntimeError`. `InvalidInputError` is itself a `ValueError`, so raising it there converts too and is never a breach.

### Framework configuration

```toml
[tool.pf_guards.framework]
root = "src/mypkg"                        # optional; default: the gate's root
disable = ["logging", "logger-exception"] # rules this project does not take

[tool.pf_guards.framework.replace]        # name a different fix in the message
requests = ["mypkg.http", "the crawl User-Agent, size caps and the SSRF guard"]
Exception = "a class from mypkg.errors"   # a bare string keeps the rule's reason

[[tool.pf_guards.framework.exempt]]
rule = "print"
path = "cli.py"
reason = "the CLI entry point is where user-facing output belongs"
```

- `root` narrows the check when the gate scans several trees (sizes on `["app", "tests"]`, framework on `app`). `--root` overrides both.
- **Exemptions are per rule and per whole path** relative to the scan root: `cli.py` does not cover `sub/cli.py`. Each states a **reason of at least four words**; a missing or token reason is a configuration error (exit `2`), not a warning.
- **Exemptions are stale-checked**: one that suppresses nothing (file fixed or moved, rule disabled) prints `STALE framework exemption` and fails the gate until removed.
- An unknown key, rule name or malformed `replace` is refused by name (exit `2`), so a typo cannot quietly switch a rule off.

### Report mode

```bash
python -m pf_core.guards --framework --report   # every breach and stale exemption, exit 0
```

`--framework` runs the framework check alone; `--report` lists its breaches with a per-rule count — `0 framework breaches` on a clean tree — and never fails on them (a file that does not parse still fails). Both work before the table exists (every rule on), so adoption can be costed before writing config. From Python, `check_framework(root, config)` returns the breaches with exemptions applied, and holds the same line as the gate: a stale exemption raises `ConfigurationError`, and `FrameworkConfig` / `FrameworkExemption` refuse on construction what the TOML refuses (an unknown rule, an empty replacement, a missing path, a reason under four words).

## Files that do not parse

Once any of the function-length limit, the comment budget or the framework check is on, a file under the roots they read that does not parse **fails the gate** — named once however many checks read it, and not excused by `--report`:

```
FAIL  pkg/broken.py:4: does not parse ('(' was never closed)
```

Every check still runs over the files that do parse, so one broken file hides no other result; the comment-budget total leaves it out, and a framework exemption for it is not reported stale. Files are read as Python reads them — a UTF-8 BOM, a `# -*- coding: … -*-` cookie, `\r` or `\r\n` row endings — so a file Python imports parses here too, and bytes Python cannot decode count as not parsing. A check that skipped it would have vouched for code it never read. From Python, `scan_function_lengths`, `scan_comment_budget`, `scan_framework` and `check_framework` raise the file's `SyntaxError`, its `filename` set; the three `scan_*` functions take `skip_unparsed=True` to pass over it instead, as the gate does after naming it. Neither the size gate nor the layering checker fails on one, so a project that has opted into none of the three sees no change.

## How it's wired

- **pre-commit** (`.pre-commit-config.yaml`) — runs `python -m pf_core.guards` + ruff on every commit. Run `pre-commit install` once per clone (from the project venv). Entries point at `.venv/bin/...` because pre-commit's `system` hooks don't inherit the venv PATH.
- **CI** (`.github/workflows/guards.yml`) — the unskippable backstop: `pip install -e .` then the same gate + ruff on push/PR.

## Consumer adoption

A consumer adds a `.pf-guards.toml` (typically `root = "app"`), optionally a size baseline and a `layering_allowlist` for pre-existing violations (`--emit-allowlist` generates it), and the same pre-commit/CI entries. The per-layer limits and the layering checker then apply to its `app/` tree automatically. Projects generated by `bin/new-consumer` get all of this stamped (`bin/lint`, `.pre-commit-config.yaml`, `guards.yml`, `.pf-guards.toml` — `bin/setup` self-heals the file if absent); existing consumers call `pf_ensure_guards_config` from pf-core's `bin/setup-common` in their own `bin/setup`.

**Adopting the function-length limit:** set `max_function_lines` and run the gate — every offender is listed with its length. With none baselined, either split them first or start `hard` at the longest function and lower it as they are split.

**Adopting the comment budget:** add the empty table and run the gate; every module over the per-module ceiling is listed, with the total last. Loosen `file` / `total` to today's numbers and tighten them as prose is cut, or cut first.

**Adopting the framework check:** run `python -m pf_core.guards --framework --report`, fix what pf-core covers, and exempt (with a reason), `disable` or `replace` the rest. Once the report is clean, commit the `[tool.pf_guards.framework]` table — the existing pre-commit and CI entries enforce it. Delete any local copy of this check and its test in the same change.

## Adding a framework rule

Add a rule when a review finds the same hand-roll twice — not the first time.

1. Add a `FrameworkRule` to `RULES` in `pf_core/guards/framework_rules.py`: a stable `name` (configs refer to it — never rename one), its `kind`, the replacement in `use` and what it buys in `why`. A `call` rule is a one-line regex over blanked code; a new AST shape needs its own `kind` and hits function in `pf_core/guards/framework.py`.
2. Name only replacements that exist — `test_every_named_replacement_exists` resolves each `pf_core.*` path.
3. Add a breaching source to `BREACHES` in `tests/test_guards_framework.py`; the parametrized tests require it to trip exactly that rule, and nothing once it sits in a docstring or comment.
4. A new rule switches on for every consumer with the table at its next upgrade — list it under **Breaking** in the CHANGELOG.
