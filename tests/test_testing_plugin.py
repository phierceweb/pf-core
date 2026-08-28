"""Tests for the pf_core.testing plugin itself.

Verifies that the fixtures work correctly: engine creation, connection
isolation, table creation, and transaction rollback between tests.
"""

import pytest
from sqlalchemy import text

from pf_core.testing._ddl import created_objects, dropped_objects


class TestEngineFixture:
    """pf_engine provides a working file-backed SQLite engine."""

    def test_engine_is_sqlite(self, pf_engine):
        assert str(pf_engine.url).startswith("sqlite")

    def test_engine_can_execute(self, pf_engine):
        with pf_engine.connect() as conn:
            result = conn.execute(text("SELECT 1 AS ok"))
            assert result.scalar() == 1


class TestConnectionFixture:
    """pf_connection provides an isolated, rolled-back connection."""

    def test_insert_and_query(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO items (name) VALUES (:name)"),
            {"name": "test_item"},
        )
        row = pf_connection.execute(
            text("SELECT name FROM items WHERE name = :name"),
            {"name": "test_item"},
        ).fetchone()
        assert row[0] == "test_item"

    def test_previous_insert_not_visible(self, pf_tables, pf_connection):
        """Data from test_insert_and_query should not leak here."""
        row = pf_connection.execute(
            text("SELECT COUNT(*) FROM items"),
        ).fetchone()
        assert row[0] == 0


class TestConcurrentAccess:
    """pf_engine must be safe under multi-threaded repo access.

    Regression: the previous in-memory ``StaticPool`` engine shared one
    DBAPI connection across all threads, so driving
    ``pf_core.parallel.run_parallel`` through ``transaction()`` raised
    ``sqlite3.InterfaceError: bad parameter or other API misuse``
    (SQLITE_MISUSE). The file-backed ``NullPool`` engine gives each
    worker thread its own connection.
    """

    @pytest.mark.pf_tables(
        "CREATE TABLE concurrent_items (id INTEGER PRIMARY KEY, val INTEGER NOT NULL)"
    )
    def test_parallel_writes_through_transaction(self, pf_tables):
        from pf_core.db import transaction
        from pf_core.parallel import run_parallel

        def _insert(n: int) -> None:
            with transaction() as conn:
                conn.execute(
                    text("INSERT INTO concurrent_items (val) VALUES (:v)"),
                    {"v": n},
                )

        run_parallel(items=list(range(50)), fn=_insert, workers=4, label="Inserted")

        with transaction() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM concurrent_items")).scalar()
        assert count == 50

    @pytest.mark.pf_tables(
        "CREATE TABLE repo_concurrent (id INTEGER PRIMARY KEY, val INTEGER NOT NULL)"
    )
    def test_parallel_writes_through_repository(self, pf_tables):
        """The layering-mandated path: services reach the DB via a
        ``Repository``, not raw ``transaction()``. A repo opening its own
        transaction per call from many threads must be just as safe."""
        from pf_core.db import Repository, transaction
        from pf_core.parallel import run_parallel

        class _CounterRepo(Repository):
            def add(self, n: int) -> None:
                with self._tx() as conn:
                    conn.execute(
                        text("INSERT INTO repo_concurrent (val) VALUES (:v)"),
                        {"v": n},
                    )

        run_parallel(
            items=list(range(50)),
            fn=lambda n: _CounterRepo().add(n),
            workers=4,
            label="Repo-inserted",
        )

        with transaction() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM repo_concurrent")).scalar()
        assert count == 50

    @pytest.mark.pf_tables("CREATE TABLE mixed_rw (id INTEGER PRIMARY KEY, val INTEGER NOT NULL)")
    def test_concurrent_reads_during_writes(self, pf_tables):
        """Readers must not error while writers are committing — the
        other half of the WAL contract (the regression test only
        exercised concurrent writers)."""
        from pf_core.db import transaction
        from pf_core.parallel import run_parallel

        def _write_then_read(n: int) -> None:
            with transaction() as conn:
                conn.execute(text("INSERT INTO mixed_rw (val) VALUES (:v)"), {"v": n})
            # Read while other workers are mid-write — must not raise.
            with transaction() as conn:
                conn.execute(text("SELECT COUNT(*) FROM mixed_rw")).scalar()

        run_parallel(items=list(range(50)), fn=_write_then_read, workers=4, label="RW")

        with transaction() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM mixed_rw")).scalar()
        assert count == 50


class TestFixtureThreadSafetyInvariant:
    """Guard against the *self-masking* property that hid the original bug.

    A ``StaticPool``-backed ``pf_engine`` — a single shared DBAPI
    connection — could not exercise concurrency: the fixture itself
    raised ``SQLITE_MISUSE`` before any concurrency assertion could
    run. That earlier shape had no test that *could*
    trip it because the test infrastructure was the broken thing.

    These assertions fail loudly if the fixture is ever reverted to a
    thread-unsafe configuration (e.g. back to in-memory ``StaticPool``
    "for speed"), instead of silently re-disabling every db test's
    ability to catch this class of bug. Pinning the invariant is the
    actual fix for what allowed the miss.
    """

    def test_pf_engine_uses_nullpool(self, pf_engine):
        from sqlalchemy.pool import NullPool

        assert isinstance(pf_engine.pool, NullPool), (
            f"pf_engine must use NullPool for thread-safe concurrent access; "
            f"got {type(pf_engine.pool).__name__}"
        )

    def test_pf_engine_is_file_backed_not_in_memory(self, pf_engine):
        db = pf_engine.url.database
        assert db not in (None, "", ":memory:"), (
            f"pf_engine must be file-backed (per-worker connections); "
            f"got in-memory url {pf_engine.url!r}"
        )


class TestTablesMarker:
    """@pytest.mark.pf_tables creates tables from inline DDL."""

    @pytest.mark.pf_tables("CREATE TABLE custom_table (id INTEGER PRIMARY KEY, val TEXT)")
    def test_marker_creates_table(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO custom_table (val) VALUES (:v)"),
            {"v": "hello"},
        )
        row = pf_connection.execute(text("SELECT val FROM custom_table")).fetchone()
        assert row[0] == "hello"


class TestSchemaFixture:
    """pf_schema fixture (from conftest.py) creates project tables."""

    def test_items_table_exists(self, pf_tables, pf_connection):
        pf_connection.execute(
            text("INSERT INTO items (name) VALUES (:name)"),
            {"name": "from_schema"},
        )
        row = pf_connection.execute(
            text("SELECT name FROM items WHERE name = :name"),
            {"name": "from_schema"},
        ).fetchone()
        assert row[0] == "from_schema"

    def test_llm_calls_table_exists(self, pf_tables, pf_connection):
        pf_connection.execute(
            text(
                "INSERT INTO llm_calls (prompt_tokens, completion_tokens, cost_usd) "
                "VALUES (:pt, :ct, :cost)"
            ),
            {"pt": 100, "ct": 50, "cost": 0.002},
        )
        row = pf_connection.execute(text("SELECT cost_usd FROM llm_calls")).fetchone()
        assert row[0] == pytest.approx(0.002)


_SCHEMA_CONFTEST = """
    import pytest

    pytest_plugins = ["pf_core.testing.db_fixtures"]

    @pytest.fixture
    def pf_schema():
        return ["CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)"]
"""

_BARE_CONFTEST = """
    pytest_plugins = ["pf_core.testing.db_fixtures"]
"""


class TestSchemaFixtureWithoutAutouse:
    """A plain (non-autouse) ``pf_schema`` applies whether or not a test names it.

    pf-core's own conftest marks ``pf_schema`` autouse, which hides this path —
    an inner pytest session is the only way to observe it.
    """

    def test_schema_alone_creates_tables(self, pytester):
        pytester.makeconftest(_SCHEMA_CONFTEST)
        pytester.makepyfile(
            """
            from sqlalchemy import text

            def test_items_exists(pf_tables, pf_connection):
                pf_connection.execute(text("INSERT INTO items (name) VALUES ('x')"))
                assert pf_connection.execute(text("SELECT COUNT(*) FROM items")).scalar() == 1
            """
        )
        pytester.runpytest_subprocess().assert_outcomes(passed=1)

    def test_marker_alone_creates_tables(self, pytester):
        pytester.makeconftest(_BARE_CONFTEST)
        pytester.makepyfile(
            """
            import pytest
            from sqlalchemy import text

            @pytest.mark.pf_tables("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")
            def test_widgets_exists(pf_tables, pf_connection):
                pf_connection.execute(text("INSERT INTO widgets (id) VALUES (1)"))
                assert pf_connection.execute(text("SELECT COUNT(*) FROM widgets")).scalar() == 1
            """
        )
        pytester.runpytest_subprocess().assert_outcomes(passed=1)

    def test_schema_and_marker_both_apply(self, pytester):
        pytester.makeconftest(_SCHEMA_CONFTEST)
        pytester.makepyfile(
            """
            import pytest
            from sqlalchemy import text

            @pytest.mark.pf_tables("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")
            def test_both_tables_exist(pf_tables, pf_connection):
                pf_connection.execute(text("INSERT INTO items (name) VALUES ('x')"))
                pf_connection.execute(text("INSERT INTO widgets (id) VALUES (1)"))
                assert pf_connection.execute(text("SELECT COUNT(*) FROM items")).scalar() == 1
                assert pf_connection.execute(text("SELECT COUNT(*) FROM widgets")).scalar() == 1
            """
        )
        pytester.runpytest_subprocess().assert_outcomes(passed=1)

    def test_marker_ddl_runs_after_schema(self, pytester):
        pytester.makeconftest(_SCHEMA_CONFTEST)
        pytester.makepyfile(
            """
            import pytest
            from sqlalchemy import text

            @pytest.mark.pf_tables("CREATE INDEX items_name_idx ON items (name)")
            def test_index_on_schema_table(pf_tables, pf_connection):
                row = pf_connection.execute(
                    text("SELECT name FROM sqlite_master WHERE name = 'items_name_idx'")
                ).fetchone()
                assert row is not None
            """
        )
        pytester.runpytest_subprocess().assert_outcomes(passed=1)

    def test_marker_redefining_schema_object_is_a_clear_error(self, pytester):
        pytester.makeconftest(_SCHEMA_CONFTEST)
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.pf_tables("CREATE TABLE items (id INTEGER PRIMARY KEY, other TEXT)")
            def test_collision(pf_tables):
                pass
            """
        )
        result = pytester.runpytest_subprocess()
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*redefines table items*pf_schema*"])

    def test_marker_if_not_exists_collision_is_not_silently_dropped(self, pytester):
        pytester.makeconftest(_SCHEMA_CONFTEST)
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.pf_tables(
                "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, other TEXT)"
            )
            def test_collision(pf_tables):
                pass
            """
        )
        result = pytester.runpytest_subprocess()
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*redefines table items*"])

    def test_marker_may_drop_and_recreate_a_pf_schema_table(self, pytester):
        """An explicit DROP means the marker is replacing the table, not colliding."""
        pytester.makeconftest(_SCHEMA_CONFTEST)
        pytester.makepyfile(
            """
            import pytest
            from sqlalchemy import text

            @pytest.mark.pf_tables(
                "DROP TABLE items",
                "CREATE TABLE items (id INTEGER PRIMARY KEY, other TEXT)",
            )
            def test_replaced(pf_tables):
                with pf_tables.connect() as conn:
                    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(items)"))}
                assert cols == {"id", "other"}
            """
        )
        result = pytester.runpytest_subprocess()
        result.assert_outcomes(passed=1)


class TestCreatedObjects:
    """The collision detector recognises the DDL forms pf_schema DDL takes."""

    @pytest.mark.parametrize(
        "stmt,expected",
        [
            ("CREATE TABLE items (id INTEGER)", ("table", "items")),
            ("create table if not exists Items (id INTEGER)", ("table", "items")),
            ('CREATE TABLE "items" (id INTEGER)', ("table", "items")),
            ("CREATE UNIQUE INDEX items_ix ON items (name)", ("index", "items_ix")),
            ("CREATE TEMPORARY VIEW v AS SELECT 1", ("view", "v")),
            ("CREATE OR REPLACE VIEW v AS SELECT 1", ("view", "v")),
        ],
    )
    def test_recognises_create_forms(self, stmt, expected):
        assert created_objects([stmt]) == {expected}

    def test_ignores_non_create_statements(self):
        assert created_objects(["INSERT INTO items (id) VALUES (1)"]) == set()


class TestDroppedObjects:
    """Marker DDL that drops an object before recreating it is replacing, not colliding."""

    @pytest.mark.parametrize(
        "stmt,expected",
        [
            ("DROP TABLE items", ("table", "items")),
            ("drop table if exists Items", ("table", "items")),
            ('DROP TABLE "items"', ("table", "items")),
            ("DROP INDEX items_ix", ("index", "items_ix")),
            ("DROP VIEW v", ("view", "v")),
        ],
    )
    def test_recognises_drop_forms(self, stmt, expected):
        assert dropped_objects([stmt]) == {expected}

    def test_ignores_non_drop_statements(self):
        assert dropped_objects(["CREATE TABLE items (id INTEGER)"]) == set()
