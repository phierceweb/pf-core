# Doctor — runtime ground-truth attestation

`pf-doctor` prints verified facts about the current environment so agents and
humans stop acting on stale assumptions.

---

## Table of Contents

- [Running it](#running-it)
- [Core checks (always on)](#core-checks-always-on)
- [`--db` (opt-in)](#--db-opt-in)
- [`--release` (opt-in)](#--release-opt-in)
- [Invariants](#invariants)
- [Adding a new check](#adding-a-new-check)

## Running it

Run it from any consumer project:

```bash
bin/run pf-doctor            # core checks: local, read-only, no network
bin/run pf-doctor --db       # + read-only database attestation
bin/run pf-doctor --release  # + release-state checks (tag / version / CHANGELOG / tree)
```

The console script lands with the install; on an editable checkout that
predates it, `bin/run pip install -e .` once (or use the always-available
`bin/run python -m pf_core.doctor`).

Exit code: `0` when no check FAILs (WARNs don't flip it), `1` otherwise.

## Core checks (always on)

| Check | Attests |
|---|---|
| `copy.loaded` | Which pf-core is actually imported (path, editable/source vs site-packages, `importlib.metadata` version). WARNs when an editable install's metadata version disagrees with the adjacent `pyproject.toml` — the stale-editable trap. |
| `python.interpreter` | Interpreter version (FAIL below the 3.11 floor) and active venv path. |
| `extras.available` | Which optional-dependency extras are importable (probed via `find_spec`, nothing gets imported). Informational — absence is legitimate. |
| `env.resolution` | The pf-core-recognized env vars as the app would see them — a `.env` in the working directory is loaded first (shell values win), and the report names which. Values redacted: key/token/secret vars presence-only, URL credentials masked. |
| `router.config` | Model-router config path (loader's chain), parse + schema validation, agent slugs, `default_client`. SKIPs when no config exists. |
| `deps.versions` | Installed versions of the key third-party libraries. |

## `--db` (opt-in)

Read-only: resolves `DATABASE_URL` (redacted display), opens a connection,
runs `SELECT 1`, and compares the database's `alembic_version` against the
script head when an `alembic/` directory exists in the working directory
(WARN on mismatch). For SQLite, a missing database file FAILs *without*
connecting — connecting would create the file, and doctor never writes.

SKIPs with an install hint when the `[db]` extra isn't installed.

## `--release` (opt-in)

Read-only git introspection of the working-directory project — a local preflight
before you tag a release. Mirrors the CI tag-vs-version guard in `publish.yml`, so a
mismatch surfaces at your desk instead of when the upload 400s.

| Check | Attests |
|---|---|
| `release.versions` | `pyproject.toml` `version` vs the top `## v…` heading in `CHANGELOG.md`. FAIL on mismatch ("sync before tagging"); WARN when the CHANGELOG has no v-heading; SKIP without a pyproject version. |
| `release.tag` | Whether `v<pyproject-version>` is among the git tags pointing at HEAD. FAIL when HEAD is tagged a different version (a build of that tag won't match the package); SKIP when nothing is tagged at HEAD. |
| `release.tree` | `git status --porcelain`. WARN on uncommitted changes — they're not part of any build of HEAD. |

SKIPs the whole group when the working directory isn't a git repo (or `git` is absent).

## Invariants

Doctor never writes, never touches the network except the opt-in `--db`
connect (`--release` runs read-only local git commands), and never imports
consumer application code. It is safe to run at
any time, in any state, including mid-incident.

## Adding a new check

Write a function returning `list[CheckResult]` in `pf_core/doctor.py` and
append it to the internal checks tuple (or gate it behind a new flag like
`--db`). Do not build a plugin system — an append is the extension model.
Keep new checks inside the invariants above: read-only, no network by
default, no consumer-code imports.
