# Runtime dialect fragments

`pf_core.db.dialect` — the SQL fragments hand-written queries need, resolved from
the connection you already hold.

```python
from pf_core.db import now_sql, row_lock_suffix, utc_cutoff, transaction

with transaction() as conn:
    conn.execute(
        text(f"UPDATE jobs SET status='pending', updated_at={now_sql(conn)} WHERE id=:i"),
        {"i": job_id},
    )
```

## Why this exists separately from `json_compat`

[`json_compat`](database.md) helpers take a `dialect: str` because they build
DDL, where the dialect is known before any connection exists. Runtime query code
holds a `conn` instead. `insert_ignore_prefix` accepts either, so there is one
public name for it rather than two.

| Helper | Replaces the MySQL-only fragment |
|---|---|
| `now_sql(conn)` | `NOW(6)` |
| `insert_ignore_prefix(conn)` | hand-branched `INSERT IGNORE` / `INSERT OR IGNORE` |
| `insert_ignore_suffix(conn)` | Postgres's trailing `ON CONFLICT DO NOTHING` |
| `row_lock_suffix(conn)` | a bare `FOR UPDATE` |
| `utc_cutoff(seconds=/minutes=/days=)` | `DATE_SUB(NOW(6), INTERVAL :n MINUTE)` |

`now_sql(conn)` is [`now_expr`](database.md) resolved from a connection instead
of a dialect string — one definition, so a column stamped through either name
compares correctly against the other. It emits `CURRENT_TIMESTAMP(6)` on MySQL
(matching `TIMESTAMP(6)` column precision) and, on SQLite, a space-separated
`strftime` form that compares lexicographically against bound datetimes.

### Backfilling a SQLite column stamped before 0.22.0

Before 0.22.0 the SQLite form was ISO `T`/`Z` with three fractional digits. Those
values no longer compare against a bound `DateTime`, and they sort above
space-separated ones within the same calendar date, so a column holding both
shapes orders wrong. Convert the old rows once, per affected column:

```sql
UPDATE t SET c = replace(rtrim(c, 'Z'), 'T', ' ') || '000' WHERE c LIKE '%T%';
```

**Keep the `WHERE`**, which also makes the statement re-runnable. Without it,
rows that were never `T`-stamped — whole seconds from `CURRENT_TIMESTAMP` or
`datetime('now')` — get `000` appended to the seconds field, turning
`2026-08-08 05:17:59` into `2026-08-08 05:17:59000`.

`CREATE TABLE IF NOT EXISTS` leaves an existing column `DEFAULT` untouched, so
check whether the default still stamps the old form before treating the backfill
as final.

For insert-or-ignore, prefer [`insert_ignore`](db-upsert.md), which builds the
whole statement from `Table` metadata. The prefix/suffix pair is the escape
hatch for SQL that cannot go through it — always use **both**, because on
Postgres the skip-on-duplicate lives in the trailing clause, not the prefix:

```python
stmt = text(f"{insert_ignore_prefix(conn)} INTO t (k) VALUES (:k){insert_ignore_suffix(conn)}")
```

Skip-on-**duplicate** is the only portable guarantee here; the pair papers over
the syntax, not the semantics. When a *non*-duplicate constraint fails, MySQL's
`INSERT IGNORE` downgrades the error to a warning and writes a mangled row (NULL
into a NOT NULL column lands as `''`, an over-long string lands truncated),
SQLite's `INSERT OR IGNORE` silently skips the row, and Postgres still raises.
Reach for the pair only where a duplicate key is the sole constraint that can
fail; otherwise use [`insert_ignore`](db-upsert.md), which is duplicate-only on
all three.

## Choosing a cutoff style

Two correct answers, and the choice is **who computes "now"** — the application
host or the database host:

```sql
-- server-side: CURRENT_TIMESTAMP - INTERVAL n SECOND   (pf_core.jobs lease queries)
-- bound:       WHERE updated_at < :cutoff              (utc_cutoff)
```

They differ only when those two clocks disagree, so:

- **Server-side** when the comparison **arbitrates between concurrent actors or
  destroys data** — lease expiry, claim ownership, retention deletes. A worker
  whose clock runs fast would otherwise reclaim a job that is still running, or
  delete rows newer than intended.
- **Bound** (`utc_cutoff`) for **selection and reporting thresholds** — "jobs
  stuck longer than 30 minutes", "cancel jobs older than 7 days". Drift of a few
  seconds cannot change the answer in a way anyone cares about, and a bound value
  can be logged, returned to an operator, and asserted in a test.

`pf_core.jobs` follows this: `claim_next`, `reclaim_stale` and the retention
purge all compute server-side.

### Time zone is not part of this choice

`utc_cutoff` returns a **naive** UTC datetime. Bind it through a typed
`bindparam` so SQLAlchemy renders it as TEXT that compares lexicographically
against `now_sql` stamps; an untyped bind hands the raw datetime to the driver,
which on SQLite means the adapter deprecated in Python 3.12. An aware value
would render a trailing offset that breaks the ordering either way.

```python
from sqlalchemy import DateTime, bindparam, text

stmt = text("SELECT id FROM jobs WHERE updated_at < :cutoff").bindparams(
    bindparam("cutoff", type_=DateTime())
)
conn.execute(stmt, {"cutoff": utc_cutoff(minutes=30)})
```

[`get_engine`](database.md) pins the session to UTC on both MySQL (`+00:00`) and
Postgres (`SET TIME ZONE 'UTC'`), so a bound naive-UTC value round-trips
unchanged on every backend. This matters most on Postgres, where pf-core's own
timestamp columns are `timestamptz`: an unpinned session interprets a naive
literal in the server's `TimeZone` (initdb takes it from the host), which skews
every cutoff by that offset — a 30-day purge silently deleting an extra 6–7
hours of rows.

The skew therefore applies only to connections pf-core did **not** make: a raw
DBAPI connection, or an external client such as the `psql` / `mysql` CLI. If you
compare a bound cutoff by hand in a CLI session and the numbers look wrong, that
is why — set the session zone there too.

## See also

- [database.md](database.md) — connections, `transaction()`, the DDL-side `json_compat` helpers
- [db-upsert.md](db-upsert.md) — `insert_ignore` / `upsert` from `Table` metadata
- [dates.md](dates.md) — date parsing and window arithmetic
