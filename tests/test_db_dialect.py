"""Tests for pf_core.db.dialect — conn-taking runtime SQL fragments."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import DateTime, bindparam, create_engine, text

from pf_core.db.dialect import (
    insert_ignore_prefix,
    insert_ignore_suffix,
    now_sql,
    row_lock_suffix,
    utc_cutoff,
)
from pf_core.exceptions import InvalidInputError


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeConn:
    """Stands in for a Connection/Engine — only ``.dialect.name`` is read."""

    def __init__(self, name: str) -> None:
        self.dialect = _FakeDialect(name)


# ---------------------------------------------------------------------------
# now_sql
# ---------------------------------------------------------------------------


def test_now_sql_per_dialect():
    assert now_sql(_FakeConn("mysql")) == "CURRENT_TIMESTAMP(6)"
    assert now_sql(_FakeConn("postgresql")) == "CURRENT_TIMESTAMP"
    assert "strftime" in now_sql(_FakeConn("sqlite"))


def test_now_sql_mysql_carries_microsecond_precision():
    """Bare CURRENT_TIMESTAMP truncates to seconds in a TIMESTAMP(6) column."""
    assert now_sql(_FakeConn("mysql")) == "CURRENT_TIMESTAMP(6)"


def test_now_sql_never_emits_mysql_only_now():
    """``NOW(6)`` is the fragment this helper exists to keep out of queries."""
    for name in ("mysql", "postgresql", "sqlite"):
        assert "NOW(6)" not in now_sql(_FakeConn(name))


def test_now_sql_sqlite_matches_bound_datetime_rendering(tmp_path):
    """A row stamped with now_sql must compare correctly against a bound
    utc_cutoff on SQLite, where the column is TEXT and comparison is
    lexicographic — in both directions."""
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, updated_at TEXT)"))
        conn.execute(text(f"INSERT INTO jobs (updated_at) VALUES ({now_sql(conn)})"))
        stmt = text("SELECT COUNT(*) FROM jobs WHERE updated_at < :c").bindparams(
            bindparam("c", type_=DateTime())
        )
        future_cutoff = utc_cutoff(minutes=-5)
        past_cutoff = utc_cutoff(minutes=5)
        assert conn.execute(stmt, {"c": future_cutoff}).scalar() == 1
        assert conn.execute(stmt, {"c": past_cutoff}).scalar() == 0
    eng.dispose()


def test_now_sql_sqlite_stamp_matches_its_own_round_trip(tmp_path):
    """A stamped row must match ``>=`` and ``==`` against its own round-trip;
    three fractional digits against SQLAlchemy's six sort it below itself.
    Clock-independent — the bind comes from the row."""
    from sqlalchemy import Column, Integer, MetaData, Table, select

    from pf_core.db.types import TIMESTAMP_US

    md = MetaData()
    jobs = Table("jobs", md, Column("id", Integer, primary_key=True), Column("ts", TIMESTAMP_US))
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    md.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text(f"INSERT INTO jobs (ts) VALUES ({now_sql(conn)})"))
        stamped = conn.execute(select(jobs.c.ts)).scalar()
        for op, expected in ((">=", 1), ("==", 1), ("<", 0)):
            stmt = text(f"SELECT COUNT(*) FROM jobs WHERE ts {op} :c").bindparams(
                bindparam("c", type_=TIMESTAMP_US)
            )
            assert conn.execute(stmt, {"c": stamped}).scalar() == expected, (
                f"stamped row is not {op} its own round-tripped value"
            )
    eng.dispose()


def test_now_sql_sqlite_renders_six_fractional_digits(tmp_path):
    """Shape check that does not depend on the wall clock or the date boundary:
    the stamp and a bound ``DateTime`` must render to the same width."""
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (ts TEXT)"))
        conn.execute(text(f"INSERT INTO t (ts) VALUES ({now_sql(conn)})"))
        stamped = conn.execute(text("SELECT ts FROM t")).scalar()
    eng.dispose()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}", stamped), stamped


# ---------------------------------------------------------------------------
# MariaDB and driver-variant normalization
# ---------------------------------------------------------------------------


def test_mariadb_normalizes_to_mysql_for_all_helpers():
    """SQLAlchemy reports ``mariadb`` for mariadb:// URLs; pf-core treats
    MariaDB as the MySQL family everywhere."""
    for name in ("mariadb", "mysql+pymysql", "MariaDB"):
        conn = _FakeConn(name)
        assert now_sql(conn) == "CURRENT_TIMESTAMP(6)"
        assert insert_ignore_prefix(conn) == "INSERT IGNORE"
        assert insert_ignore_suffix(conn) == ""
        assert row_lock_suffix(conn) == " FOR UPDATE"


# ---------------------------------------------------------------------------
# insert_ignore_prefix — one public name, both call styles
# ---------------------------------------------------------------------------


def test_insert_ignore_prefix_accepts_a_connection():
    assert insert_ignore_prefix(_FakeConn("mysql")) == "INSERT IGNORE"
    assert insert_ignore_prefix(_FakeConn("sqlite")) == "INSERT OR IGNORE"
    assert insert_ignore_prefix(_FakeConn("postgresql")) == "INSERT"


def test_insert_ignore_prefix_still_accepts_a_dialect_string():
    """The older json_compat signature keeps working — one name, not two."""
    assert insert_ignore_prefix("mysql") == "INSERT IGNORE"
    assert insert_ignore_prefix("sqlite") == "INSERT OR IGNORE"
    assert insert_ignore_prefix("postgresql") == "INSERT"


def test_conn_and_string_forms_agree():
    for name in ("mysql", "sqlite", "postgresql"):
        assert insert_ignore_prefix(_FakeConn(name)) == insert_ignore_prefix(name)


def test_postgres_dialect_variants_normalize():
    """SQLAlchemy reports ``postgresql``; a bare ``postgres`` must not fall
    through to the MySQL default."""
    assert insert_ignore_prefix(_FakeConn("postgres")) == "INSERT"
    assert row_lock_suffix(_FakeConn("postgres")) == " FOR UPDATE"


# ---------------------------------------------------------------------------
# insert_ignore_suffix
# ---------------------------------------------------------------------------


def test_insert_ignore_suffix_per_dialect():
    assert insert_ignore_suffix(_FakeConn("postgresql")) == " ON CONFLICT DO NOTHING"
    assert insert_ignore_suffix("postgres") == " ON CONFLICT DO NOTHING"
    assert insert_ignore_suffix(_FakeConn("mysql")) == ""
    assert insert_ignore_suffix(_FakeConn("sqlite")) == ""


def test_prefix_plus_suffix_skips_duplicates_end_to_end(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (k TEXT PRIMARY KEY)"))
        stmt = text(
            f"{insert_ignore_prefix(conn)} INTO t (k) VALUES (:k){insert_ignore_suffix(conn)}"
        )
        conn.execute(stmt, {"k": "a"})
        conn.execute(stmt, {"k": "a"})
        assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 1
    eng.dispose()


def test_prefix_plus_suffix_postgres_form_skips_duplicates(tmp_path):
    """Postgres needs the trailing clause; the bare prefix raises on a
    duplicate key. Run on SQLite, which accepts the same syntax — a proxy for
    the composition parsing, not for Postgres semantics."""
    sql = (
        f"{insert_ignore_prefix('postgresql')} INTO t (k) VALUES (:k)"
        f"{insert_ignore_suffix('postgresql')}"
    )
    assert sql == "INSERT INTO t (k) VALUES (:k) ON CONFLICT DO NOTHING"

    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (k TEXT PRIMARY KEY)"))
        conn.execute(text(sql), {"k": "a"})
        conn.execute(text(sql), {"k": "a"})
        assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 1
    eng.dispose()


# ---------------------------------------------------------------------------
# row_lock_suffix
# ---------------------------------------------------------------------------


def test_row_lock_suffix_locks_where_supported():
    assert row_lock_suffix(_FakeConn("mysql")) == " FOR UPDATE"
    assert row_lock_suffix(_FakeConn("postgresql")) == " FOR UPDATE"


def test_row_lock_suffix_is_empty_on_sqlite():
    """SQLite rejects the clause outright, so the query must omit it."""
    assert row_lock_suffix(_FakeConn("sqlite")) == ""


def test_row_lock_suffix_concatenates_cleanly():
    sql = "SELECT id FROM jobs WHERE id=:i" + row_lock_suffix(_FakeConn("mysql"))
    assert sql.endswith("WHERE id=:i FOR UPDATE")


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 42, object(), b"mysql"])
def test_garbage_argument_raises_invalid_input(bad):
    with pytest.raises(InvalidInputError, match="Connection/Engine or dialect string"):
        now_sql(bad)


def test_unsupported_dialect_string_raises():
    with pytest.raises(InvalidInputError, match="unsupported dialect"):
        now_sql("oracle")


# ---------------------------------------------------------------------------
# utc_cutoff
# ---------------------------------------------------------------------------


def test_utc_cutoff_is_naive_utc():
    """Naive so SQLite TEXT rendering and MySQL binds compare correctly."""
    assert utc_cutoff(minutes=1).tzinfo is None


@pytest.mark.parametrize(
    "kwargs,delta",
    [
        ({"seconds": 90}, timedelta(seconds=90)),
        ({"minutes": 30}, timedelta(minutes=30)),
        ({"days": 7}, timedelta(days=7)),
        ({"minutes": 30, "days": 1}, timedelta(days=1, minutes=30)),
    ],
)
def test_utc_cutoff_subtracts_the_delta(kwargs, delta):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    got = utc_cutoff(**kwargs)
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    assert before - delta <= got <= after - delta


def test_utc_cutoff_requires_a_delta():
    """A bare call would silently mean "now", selecting everything."""
    with pytest.raises(InvalidInputError, match="requires"):
        utc_cutoff()


def test_utc_cutoff_zero_is_honored_not_treated_as_missing():
    got = utc_cutoff(days=0)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert (now - got) < timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Re-export surface
# ---------------------------------------------------------------------------


def test_exported_from_pf_core_db():
    import pf_core.db as db

    for name in (
        "now_sql",
        "insert_ignore_prefix",
        "insert_ignore_suffix",
        "row_lock_suffix",
        "utc_cutoff",
    ):
        assert hasattr(db, name), f"{name} missing from pf_core.db"
        assert name in db.__all__, f"{name} missing from pf_core.db.__all__"


# ---------------------------------------------------------------------------
# One definition of "now" across the two exported helpers
# ---------------------------------------------------------------------------


def test_now_sql_and_now_expr_agree_for_every_dialect():
    """``now_sql`` is ``now_expr`` resolved from a connection, so this holds by
    construction; it is asserted to keep that wiring, not to catch drift."""
    from pf_core.db.json_compat import now_expr

    for name in ("sqlite", "mysql", "postgresql"):
        assert now_sql(_FakeConn(name)) == now_expr(name)


def test_now_expr_sqlite_matches_sqlalchemy_bound_rendering():
    """The pair that can actually drift: the stamp fragment and SQLAlchemy's
    rendering of a bound ``DateTime``. Compared as text, so a width mismatch
    breaks equality even when both name the same instant."""
    import datetime as dt

    from sqlalchemy.dialects import sqlite as sqlite_dialect

    from pf_core.db.json_compat import now_expr

    rendered = (
        DateTime()
        .dialect_impl(sqlite_dialect.dialect())
        .bind_processor(sqlite_dialect.dialect())(dt.datetime(2026, 8, 30, 17, 4, 5, 123456))
    )
    assert rendered == "2026-08-30 17:04:05.123456"
    stamp_format = now_expr("sqlite")
    assert stamp_format == "strftime('%Y-%m-%d %H:%M:%f000', 'now')", (
        "the stamp must render the same shape as the bound value above"
    )


def test_now_expr_stamped_row_is_visible_to_a_utc_cutoff_bind(tmp_path):
    from pf_core.db.json_compat import now_expr

    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, updated_at TEXT)"))
        conn.execute(text(f"INSERT INTO jobs (updated_at) VALUES ({now_expr('sqlite')})"))
        stmt = text("SELECT COUNT(*) FROM jobs WHERE updated_at < :c").bindparams(
            bindparam("c", type_=DateTime())
        )
        assert conn.execute(stmt, {"c": utc_cutoff(minutes=-5)}).scalar() == 1
    eng.dispose()


def test_documented_typed_bind_uses_no_deprecated_driver_adapter(tmp_path):
    """The docstring's form must not depend on the sqlite3 datetime adapter
    deprecated in Python 3.12."""
    import warnings

    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, updated_at TEXT)"))
        conn.execute(text(f"INSERT INTO jobs (updated_at) VALUES ({now_sql(conn)})"))
        stmt = text("SELECT COUNT(*) FROM jobs WHERE updated_at < :cutoff").bindparams(
            bindparam("cutoff", type_=DateTime())
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            conn.execute(stmt, {"cutoff": utc_cutoff(minutes=-5)})
    eng.dispose()
    assert not [w for w in caught if issubclass(w.category, DeprecationWarning)]


def test_dialect_name_rejects_a_non_string_dialect_name():
    class _Odd:
        dialect = type("D", (), {"name": object()})()

    with pytest.raises(InvalidInputError, match="Connection/Engine or dialect string"):
        now_sql(_Odd())


def test_reexported_insert_ignore_prefix_rejects_unsupported_dialect():
    """The re-export resolves through the dialect wrapper, so an unsupported
    dialect raises a FlowException, not ValueError."""
    from pf_core.db import insert_ignore_prefix as reexported

    with pytest.raises(InvalidInputError):
        reexported("oracle")
