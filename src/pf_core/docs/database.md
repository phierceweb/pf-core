# Database

SQLAlchemy-based database layer supporting SQLite, MySQL/MariaDB, and PostgreSQL. Provides engine management, transactions, helper functions, a model name resolver, and a repository base class.

## Installation

Database support is included in the base `pf-core` install (SQLAlchemy). For MySQL/MariaDB add `pf-core[mysql]` (PyMySQL); for PostgreSQL add `pf-core[postgres]` (psycopg 3 with the binary wheel). SQLite needs no extra.

## Engine setup

The engine initializes from `DATABASE_URL` env var. Call `get_engine()` at startup to validate the connection:

```python
from pf_core.db import get_engine, db_url

# Reads DATABASE_URL from env (works for any supported dialect)
get_engine()

# PostgreSQL example URL:
#   DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname

# SQLite with fallback (if DATABASE_URL is not set, uses SQLite)
get_engine(db_url(fallback_sqlite="app.db"))
```

The engine is cached as a module-level singleton — subsequent calls return the same engine.

### URL resolution

`db_url()` resolves the database URL in this order:

1. `DATABASE_URL` environment variable
2. `fallback_sqlite` path (becomes `sqlite:///path`)
3. Raises `DatabaseUnavailableError`

### Dialect detection

For code that needs to branch on the active dialect (Alembic env, raw-SQL helpers, etc.):

```python
from pf_core.db import is_sqlite, is_postgres, dialect_of

is_sqlite()      # True / False
is_postgres()    # True / False
dialect_of()     # "sqlite" | "mysql" | "postgresql"
```

`dialect_of()` returns the same string the [`json_compat`](../db/json_compat.py) helpers expect, so the two compose:

```python
from pf_core.db import dialect_of
from pf_core.db.json_compat import json_col_type

ddl = f"payload {json_col_type(dialect_of())} NOT NULL"
```

Each accepts an optional explicit URL (`is_postgres("postgresql+psycopg://...")`); without one they resolve from `DATABASE_URL`. `dialect_of()` raises `ConfigurationError` for an unsupported dialect.

### SQLite pragmas

When using SQLite, the engine automatically enables:
- `PRAGMA foreign_keys = ON`
- `PRAGMA journal_mode = WAL`
- `PRAGMA busy_timeout = <SQLITE_BUSY_TIMEOUT>` — how long (ms) concurrent writers wait for a lock before failing. Defaults to `30000` (30s). Set the `SQLITE_BUSY_TIMEOUT` env var to override (e.g. `0` to fail immediately).

### MySQL foreign keys

When using MySQL, the engine enables `SET foreign_key_checks = 1` on each connection.

### PostgreSQL

No per-connection setup. psycopg negotiates UTC for `TIMESTAMPTZ` via the server's `timezone` setting (default UTC); FK constraints are always enforced. JSON columns use `JSONB` (preferred over `JSON`) — see `json_compat.json_col_type("postgresql")`.

## Transactions

`transaction()` is the primary way to run queries. It yields a `Connection` inside a transaction that commits on clean exit and rolls back on exception:

```python
from sqlalchemy import text
from pf_core.db import transaction

# Read
with transaction() as conn:
    rows = conn.execute(
        text("SELECT * FROM entries WHERE section_id = :sid"),
        {"sid": 3},
    ).mappings().fetchall()
    entries = [dict(r) for r in rows]

# Write (auto-commits on exit)
with transaction() as conn:
    conn.execute(
        text("INSERT INTO entries (id, title) VALUES (:id, :title)"),
        {"id": "DOJ_001", "title": "New entry"},
    )

# Multiple writes in one transaction
with transaction() as conn:
    conn.execute(text("DELETE FROM entry_sections WHERE entry_id = :eid"), {"eid": "DOJ_001"})
    conn.execute(
        text("INSERT INTO entry_sections (entry_id, section_id) VALUES (:eid, :sid)"),
        {"eid": "DOJ_001", "sid": 3},
    )
```

### Key patterns

**Named parameters**: Always use `:name` style, never `%s`:

```python
conn.execute(text("SELECT * FROM t WHERE id = :id"), {"id": 42})
```

**Dict-like rows**: Use `.mappings()` to get dict-like results:

```python
row = conn.execute(text("SELECT * FROM t WHERE id = :id"), {"id": 42}).mappings().fetchone()
if row:
    return dict(row)  # {"id": 42, "name": "..."}
```

**Dynamic IN clauses**: Build named parameter placeholders:

```python
params = {f"id_{i}": v for i, v in enumerate(ids)}
placeholders = ", ".join(f":id_{i}" for i in range(len(ids)))
rows = conn.execute(
    text(f"SELECT * FROM entries WHERE id IN ({placeholders})"),
    params,
).mappings().fetchall()
```

**Dynamic SET clauses** (for partial updates):

```python
sets = ", ".join(f"{col} = :{col}" for col in fields)
params = dict(fields)
params["_id"] = record_id
conn.execute(text(f"UPDATE mytable SET {sets} WHERE id = :_id"), params)
```

**Last inserted ID**:

```python
result = conn.execute(text("INSERT INTO t (name) VALUES (:name)"), {"name": "x"})
new_id = result.lastrowid
```

## Migrating from pymysql

If you're converting code that used raw pymysql cursors, watch for these:

| pymysql | SQLAlchemy `text()` |
|---------|-------------------|
| `%s` positional params | `:name` named params |
| `.fetchone()` returns dict | `.mappings().fetchone()` for dict-like rows |
| `cur.lastrowid` | `result.lastrowid` |
| `cur.rowcount` | `result.rowcount` |
| `%%` to escape `%` in SQL strings | Single `%` — no escaping needed |
| `WHERE id IN %s` with tuple | Build `:p0, :p1, ...` placeholders (see dynamic IN above) |

**The `%%` gotcha**: pymysql uses `%s` for parameters, so literal `%` in SQL (like MySQL's `DATE_FORMAT(col, '%Y-%m')`) must be doubled: `%%Y-%%m`. With SQLAlchemy `text()`, parameters use `:name`, so `%` is just a literal character. If you forget to un-double them, MySQL receives `%%Y-%%m` and returns the literal string `%Y-%m` instead of a formatted date. This is silent — no error, just wrong data.

## Helpers

```python
from pf_core.db import coerce_json_col, dumps_json, now_iso, row_to_dict
```

### coerce_json_col

Safely coerce a database column value to a Python list. Handles `None`, JSON strings, lists, and other iterables. Never raises.

```python
coerce_json_col(None)              # []
coerce_json_col('["a", "b"]')     # ["a", "b"]
coerce_json_col([1, 2, 3])        # [1, 2, 3]
coerce_json_col("")               # []
```

### dumps_json

Serialize to JSON without ASCII-escaping unicode:

```python
dumps_json({"name": "Roe v. Wade"})  # '{"name": "Roe v. Wade"}'
```

### now_iso

Current UTC time as ISO 8601 string. Canonical home is `pf_core.utils.dates`; re-exported here for backward compatibility.

```python
from pf_core.db import now_iso           # works (backward compat)
from pf_core.utils.dates import now_iso  # preferred

now_iso()  # "2026-04-14T14:30:00Z"
```

See [dates.md](dates.md) for additional date utilities (`parse_date`, `month_label`, `date_range`, etc.).

### row_to_dict

Convert a SQLAlchemy Row, RowMapping, or dict to a plain dict:

```python
row = conn.execute(text("SELECT ...")).fetchone()
entry = row_to_dict(row)  # plain dict or None
```

## Model name resolver

Thread-safe, cached resolver that maps LLM model names to database IDs. Uses a `models` table with `id` (auto-increment) and `name` (unique) columns.

```python
from pf_core.db import resolve_model_id

model_id = resolve_model_id("anthropic/claude-3.5-sonnet")  # int or None
```

- Thread-safe with a process-level lock and cache
- Insert-or-ignore + SELECT via the dialect-agnostic [`insert_ignore`](db-upsert.md) helper — `ON CONFLICT (name) DO NOTHING` on SQLite/PostgreSQL, a no-op `ON DUPLICATE KEY UPDATE` on MySQL/MariaDB; never the Postgres-invalid `INSERT IGNORE`
- Each model name is inserted at most once per process lifetime
- Returns `None` for empty strings

## Repository base class

For organizing query functions into classes with shared transaction management:

```python
from pf_core.db.repository import Repository
from sqlalchemy import text

class EntryRepo(Repository):
    def get_by_id(self, entry_id: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                text("SELECT * FROM entries WHERE id = :id"),
                {"id": entry_id},
            ).mappings().fetchone()
            return dict(row) if row else None

    def list_by_section(self, section_id: int) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                text("SELECT * FROM entries WHERE section_id = :sid ORDER BY date_of_action"),
                {"sid": section_id},
            ).mappings().fetchall()
            return [dict(r) for r in rows]
```

### Standalone vs shared transaction

```python
# Standalone — each method creates its own transaction
repo = EntryRepo()
entry = repo.get_by_id("DOJ_001")

# Shared — multiple operations in one transaction
with transaction() as conn:
    repo = EntryRepo(conn)
    entry = repo.get_by_id("DOJ_001")
    sections = repo.list_by_section(3)
    # Both queries share the same transaction
```

## Soft delete

Helpers for soft-deleting rows using a `deleted_at` timestamp column. See [soft-delete.md](soft-delete.md) for full documentation.

```python
from pf_core.db.soft_delete import soft_delete, restore, not_deleted

with transaction() as conn:
    soft_delete(conn, "entries", "id", entry_id, reason="duplicate")
    restore(conn, "entries", "id", entry_id)

# In queries:
sql = f"SELECT * FROM entries WHERE section_id = :sid {not_deleted()}"
```

## Connectivity check

```python
from pf_core.db import transaction, ping

ping()  # raises if database is unreachable
```

## Testing

See [testing.md](testing.md) for database test fixtures (`pf_engine`, `pf_connection`, `pf_tables`).
