# Versioned Config

Append-only, latest-version-wins config resolution. Instead of `UPDATE`-ing a config row in place, you `INSERT` a new row at `version + 1`; readers always take the highest `version` for a given scope. You get a full, auditable history of every config a pipeline ran under, and "did the config change since I last read it?" becomes a cheap version comparison.

Requires the `[db]` extra. Works on SQLite, MySQL, and PostgreSQL.

## The table shape

You own the table; this helper only reads and appends. It expects an integer version column (default name `version`) and one or more scope columns. A typical shape:

```sql
CREATE TABLE section_config (
  id          INTEGER PRIMARY KEY,         -- DB-owned; fresh per version
  section_id  INTEGER,                     -- scope
  version     INTEGER NOT NULL,            -- latest wins
  beat_query  TEXT,                        -- payload
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Quick usage

```python
from pf_core.db import transaction
from pf_core.db.versioned_config import (
    get_latest, append_version, latest_version, get_latest_with_fallback,
)

with transaction() as conn:
    # Read the current config for a scope
    current = get_latest(conn, "section_config", {"section_id": 5})

    # Append a new version (only beat_query changes; the rest carries forward)
    v = append_version(
        conn, "section_config", {"section_id": 5},
        {"beat_query": "new query"}, carry_forward=True,
    )

    # Has it changed since I cached version N?
    stale = latest_version(conn, "section_config", {"section_id": 5}) > known_version
```

## Functions

### get_latest

Highest-`version` row for a scope, as a dict (or `None`).

```python
get_latest(conn, "section_config", {"section_id": 5})
get_latest(conn, "singleton_config", {})        # empty scope = whole table is one scope
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conn` | `Connection` | *(required)* | Open connection (e.g. from `transaction()`) |
| `table` | `str` | *(required)* | Table name — a validated SQL identifier |
| `scope` | `dict[str, Any]` | *(required)* | Column→value scope filter; `None` values match `IS NULL` |
| `version_col` | `str` | `"version"` | Name of the integer version column |

### latest_version

Highest `version` for a scope, or `0` if no rows. Use for staleness checks.

### get_latest_with_fallback

`get_latest(scope)`, or `get_latest(fallback_scope)` if the first has no rows — the "specific config, else a shared default" lookup.

```python
get_latest_with_fallback(
    conn, "essay_config",
    {"essay_id": 7},          # specific
    {"essay_id": None},       # project default (NULL essay_id)
)
```

### append_version

Insert a new version row and return its number (prior max + 1; `1` for a new scope).

```python
append_version(conn, "section_config", {"section_id": 5}, {"beat_query": "q"})
append_version(conn, "section_config", {"section_id": 5}, {"beat_query": "q2"},
               carry_forward=True)   # copy unspecified columns from the prior version
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conn` | `Connection` | *(required)* | Open connection |
| `table` | `str` | *(required)* | Table name (validated identifier) |
| `scope` | `dict[str, Any]` | *(required)* | Scope columns — filter and written onto the new row |
| `values` | `dict[str, Any]` | *(required)* | Payload columns for the new version |
| `version_col` | `str` | `"version"` | Version column name |
| `carry_forward` | `bool` | `False` | Copy unspecified columns from the prior latest row |
| `carry_forward_exclude` | `frozenset[str]` | `{"id", "created_at", "updated_at"}` | Columns never carried forward |

## Safety

Table and column **names** are caller-supplied identifiers, validated against `^[A-Za-z_][A-Za-z0-9_]*$` and never interpolated from user input. Scope filters and inserted **values** are always bound parameters, so values are never a SQL-injection vector.
