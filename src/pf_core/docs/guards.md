# Structural Guards (Build Gate)

`pf_core.guards` turns documented structural rules into a **build gate** — a check that fails pre-commit and CI when a rule is broken, so neither a human nor an AI agent can land a violation. Foundation module, stdlib-only (`ast`, `pathlib`, `json`, `argparse`, `tomllib`); no extra required.

One command runs both checks — the **file-size gate** (flat limits for library code, per-layer limits for consumer `app/` trees) and the **layering checker**:

```bash
python -m pf_core.guards          # reads .pf-guards.toml at the repo root
```

Exit `1` on any hard size violation, any layering violation, or any **stale** baseline/allowlist entry (an exception that's no longer real must be removed — the ratchet's other half); soft violations WARN without failing; exit `2` on a misconfigured gate — missing config file, missing scan root, malformed TOML, or nonsense limit values (zero/negative limits, `soft_fraction` outside `(0, 1]`) — so misconfiguration fails loudly instead of silently passing. Installed as the `pf-guards` console script.

## Configuration — `[tool.pf_guards]`

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

[tool.pf_guards.allowed_imports]        # optional: per-layer layering overrides
api = ["services", "orchestrators", "repo", "db"]   # replaces the api allow-set
workers = ["services", "db"]                        # a new key declares a new checked layer

[tool.pf_guards.layering_allowlist]     # optional: named exceptions, stale-checked
"app/db/cache.py" = ["app.services.parsers.rss"]    # deliberate edge, tracked for burn-down
```

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

## Baseline (adopting the gate on a dirty tree)

A gate that requires a green tree on day one can't be adopted by a repo that already has violations. The **baseline** grandfathers known offenders — a `[tool.pf_guards.baseline]` table of `path → recorded line count` in `.pf-guards.toml` (generate it with `--emit-baseline`). The `--baseline file.json` CLI flag accepts the same map as a JSON file for ad-hoc runs.

- A baselined file at or **below** its recorded count → suppressed (no failure).
- A baselined file **grown beyond** its recorded count → reported as a hard FAIL. The ratchet only tightens.
- A **new** file over the hard limit (not in the baseline) → reported.
- A baselined file **no longer over its hard limit at all** → `STALE baseline entry`, exit 1
  until the entry is removed. Dead grandfathering is enforced away, not just discouraged —
  the baseline can only shrink.

When a baselined file legitimately must grow, **split it** — do not bump the number. Bumping defeats the ratchet.

The baseline is also the exemption mechanism: there is no permanent `exempt` list. A file that needs a pass goes in the baseline, where growth still fails — exemptions stay temporary by construction.

pf-core itself carries **no baseline** — the files grandfathered at the gate's adoption were split by concern, so the framework passes its own gate with zero exceptions.

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

## How it's wired

- **pre-commit** (`.pre-commit-config.yaml`) — runs `python -m pf_core.guards` + ruff on every commit. Run `pre-commit install` once per clone (from the project venv). Entries point at `.venv/bin/...` because pre-commit's `system` hooks don't inherit the venv PATH.
- **CI** (`.github/workflows/guards.yml`) — the unskippable backstop: `pip install -e .` then the same gate + ruff on push/PR.

## Consumer adoption

A consumer adds a `.pf-guards.toml` (typically `root = "app"`), optionally a size baseline and a `layering_allowlist` for pre-existing violations (`--emit-allowlist` generates it), and the same pre-commit/CI entries. The per-layer limits and the layering checker then apply to its `app/` tree automatically. Projects generated by `bin/new-consumer` get all of this stamped (`bin/lint`, `.pre-commit-config.yaml`, `guards.yml`, `.pf-guards.toml` — `bin/setup` self-heals the file if absent); existing consumers call `pf_ensure_guards_config` from pf-core's `bin/setup-common` in their own `bin/setup`.
