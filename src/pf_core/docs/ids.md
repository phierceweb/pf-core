# ID Generation

URL-safe nanoid generation with optional collision checking against a database table.

## Simple generation

```python
from pf_core.utils.ids import generate_id

generate_id()            # "V1StGXR8_Z5j" (12 chars default)
generate_id(size=8)      # "k3J9xQ2m"
generate_id(size=20)     # "aB3kL9xQ2mR7nP4wT6yJ"
```

Characters are drawn from `0-9A-Za-z_-` (64-char URL-safe alphabet).

## Configuration

Default length is read from the `ID_LENGTH` environment variable. Clamped to 8–36; falls back to 12 when unset.

```bash
# In .env
ID_LENGTH=16
```

`generate_id()` reads `os.environ["ID_LENGTH"]` directly, not an `AppConfig` instance. Declaring `ID_LENGTH` on an `AppConfig` subclass only affects ID generation if the value also reaches the environment (e.g. set in `.env`, which dotenv loads).

The `size` parameter on `generate_id()` and `allocate_id()` overrides the default when provided.

## Collision-safe allocation

For IDs stored in a database table, use `allocate_id` to generate + verify uniqueness:

```python
from pf_core.db import transaction
from pf_core.utils.ids import allocate_id

with transaction() as conn:
    entry_id = allocate_id(conn, table="entries")
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `conn` | `Connection` | (required) | SQLAlchemy connection (inside a transaction) |
| `table` | `str` | (required) | Table name to check for collisions |
| `column` | `str` | `"id"` | Column name for the ID |
| `preferred` | `str \| None` | `None` | Try this ID first before generating |
| `size` | `int \| None` | `None` | Override default length |
| `max_attempts` | `int` | `24` | Maximum retries before raising |

### With a preferred ID

When integrating external data that may suggest an ID:

```python
entry_id = allocate_id(
    conn,
    table="entries",
    preferred="user-suggested-id",  # used if not taken
)
```

If the preferred ID is already in the table, a new nanoid is generated instead.

### Raises

`PreconditionError` if a unique ID cannot be found after `max_attempts` tries. In practice this only happens if the ID space is nearly full (extremely unlikely with 12+ character nanoids).

## Migrating from consumer projects

**Custom per-table id helper** — replace a hand-rolled `app/utils/ids.py` with `generate_entry_id()` / `allocate_entry_id()`:

```python
# Before
from app.utils.ids import allocate_entry_id
entry_id = allocate_entry_id(conn, preferred=source_id)

# After
from pf_core.utils.ids import allocate_id
entry_id = allocate_id(conn, table="entries", preferred=source_id)
```

**Ad-hoc UUID slicing** — replace `uuid.uuid4().hex[:12]`:

```python
# Before
import uuid
job_id = uuid.uuid4().hex[:12]

# After
from pf_core.utils.ids import generate_id
job_id = generate_id()
```
