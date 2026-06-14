"""Tests for pf_core.db.repository.Repository base class."""

from sqlalchemy import text

from pf_core.db.repository import Repository


class ItemRepo(Repository):
    """Test repo operating on the 'items' table from conftest pf_schema."""

    def insert(self, name: str) -> int:
        with self._tx() as conn:
            result = conn.execute(
                text("INSERT INTO items (name) VALUES (:name)"),
                {"name": name},
            )
            return result.lastrowid

    def get_by_name(self, name: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                text("SELECT id, name, status FROM items WHERE name = :name"),
                {"name": name},
            ).mappings().fetchone()
            return dict(row) if row else None

    def count(self) -> int:
        with self._tx() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM items")).scalar()


class TestStandaloneTransaction:
    """Repository with no conn arg creates its own transactions."""

    def test_insert_and_get(self, pf_tables, pf_connection):
        repo = ItemRepo()
        repo.insert("standalone_item")
        result = repo.get_by_name("standalone_item")
        assert result is not None
        assert result["name"] == "standalone_item"
        assert result["status"] == "active"

    def test_count(self, pf_tables, pf_connection):
        repo = ItemRepo()
        assert repo.count() == 0
        repo.insert("one")
        repo.insert("two")
        assert repo.count() == 2


class TestSharedTransaction:
    """Repository with conn arg participates in caller's transaction."""

    def test_shared_conn_sees_same_data(self, pf_tables, pf_connection):
        # Insert via raw connection
        pf_connection.execute(
            text("INSERT INTO items (name) VALUES (:name)"),
            {"name": "shared_item"},
        )

        # Repo using same connection should see the uncommitted data
        repo = ItemRepo(pf_connection)
        result = repo.get_by_name("shared_item")
        assert result is not None
        assert result["name"] == "shared_item"

    def test_repo_insert_visible_to_caller(self, pf_tables, pf_connection):
        repo = ItemRepo(pf_connection)
        repo.insert("repo_item")

        # Caller's connection should see the repo's insert
        row = pf_connection.execute(
            text("SELECT name FROM items WHERE name = :name"),
            {"name": "repo_item"},
        ).fetchone()
        assert row is not None

    def test_multiple_repos_share_transaction(self, pf_tables, pf_connection):
        repo_a = ItemRepo(pf_connection)
        repo_b = ItemRepo(pf_connection)

        repo_a.insert("from_a")
        # repo_b should see repo_a's insert since they share the connection
        assert repo_b.get_by_name("from_a") is not None
