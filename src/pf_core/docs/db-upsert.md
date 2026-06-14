# Dialect-agnostic upserts

`pf_core.db.insert_ignore` and `pf_core.db.upsert` build the right insert-on-conflict statement for the live engine's dialect, so consumers using `Table` metadata never write SQLite-only `INSERT OR IGNORE` / `INSERT OR REPLACE` (nor a hand-rolled `is_mysql` branch). SQLite, PostgreSQL, and MySQL/MariaDB are all supported.

This complements [`json_compat`](../db/json_compat.py), which returns portable type/SQL *strings* for raw DDL. Reach for these when you're building SQLAlchemy `insert()` constructs (the common case); reach for `json_compat` when you're hand-writing raw SQL/DDL.

## API

```python
from pf_core.db import insert_ignore, upsert, transaction

with transaction() as conn:
    # INSERT, do nothing on conflict. Returns 1 if inserted, 0 if skipped.
    n = insert_ignore(conn, artists, {"slug": "fugazi", "name": "Fugazi"}, conflict=["slug"])

    # INSERT, overwrite the named columns on conflict (replaces INSERT OR REPLACE).
    upsert(conn, sources, {"source": "itunes", "account": acct, "note": note},
           conflict=["source"], update=["account", "note"])
```

- **`conflict`** — the columns of the unique/primary-key constraint to conflict on. On MySQL/MariaDB the conflict target is implicit (`ON DUPLICATE KEY UPDATE` keys off any duplicate key), so the argument is accepted for a uniform call site but not emitted into the SQL — see the caveat below.
- **`update`** (upsert only) — the columns to overwrite from the incoming row when it already exists.
- `insert_ignore` returns `1` if a row was inserted, `0` if a conflicting row already existed — reliable on every supported backend. On SQLite/Postgres the count comes from `RETURNING` (because `ON CONFLICT DO NOTHING` reports `rowcount = -1` on psycopg); on MySQL/MariaDB it comes from `rowcount`.

## Dialects

| Dialect | `insert_ignore` | `upsert` | Inserted-count source |
|---|---|---|---|
| SQLite, PostgreSQL | `ON CONFLICT (conflict) DO NOTHING … RETURNING` | `ON CONFLICT (conflict) DO UPDATE` | `RETURNING` |
| MySQL, MariaDB | `ON DUPLICATE KEY UPDATE pk = pk` (no-op) | `ON DUPLICATE KEY UPDATE col = VALUES(col)` | `rowcount` |

Any other dialect raises `NotImplementedError` rather than guessing at an untested construct.

**Why a no-op `ON DUPLICATE KEY UPDATE` and not `INSERT IGNORE`?** `INSERT IGNORE` would also downgrade *non-duplicate* errors (string truncation, NULL into a NOT-NULL column, FK violations) to silent warnings — so a row that raises on Postgres/SQLite would be silently mangled on MySQL. The no-op `pk = pk` fires only on a duplicate key, so behaviour is identical across every dialect. The `upsert` `col = VALUES(col)` form is kept deliberately (rather than the MySQL-8.0.20 `AS new` row-alias) because MariaDB does not support the alias syntax.

### Caveats

- **Multiple unique constraints.** On MySQL/MariaDB the conflict target is implicit — *any* duplicate key triggers the skip/update, whereas SQLite/Postgres scope to `conflict`. For the common single-constraint table they are identical; if you must scope to one specific constraint among several, these helpers can't express that on MySQL.
- **`rowcount`-based count.** The MySQL `0`/`1` count assumes the connection does not enable `CLIENT_FOUND_ROWS` (SQLAlchemy's default), so an unchanged no-op reports `0` affected rows.

These helpers cover the **single-row, plain-column-copy** case. If you need richer conflict logic — casting incoming values (e.g. pgvector `CAST(:embedding AS vector)`), a `WHERE` on the update, or non-column expressions like `updated_at = NOW()` — write the `insert()` construct directly; that's outside the scope of these helpers.
