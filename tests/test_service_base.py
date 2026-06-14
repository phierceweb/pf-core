"""Tests for pf_core.services.Service base class."""

from sqlalchemy import text

from pf_core.config import AppConfig
from pf_core.db.repository import Repository
from pf_core.services import Service


# --- Test doubles ---


class ItemRepo(Repository):
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
                text("SELECT id, name FROM items WHERE name = :name"),
                {"name": name},
            ).mappings().fetchone()
            return dict(row) if row else None


class ItemService(Service):
    def add_item(self, name: str) -> int:
        repo = self._repo(ItemRepo)
        return repo.insert(name)

    def find_item(self, name: str) -> dict | None:
        repo = self._repo(ItemRepo)
        return repo.get_by_name(name)


# --- Tests ---


class TestServiceLogger:
    def test_has_logger(self):
        svc = ItemService()
        assert svc._log is not None

    def test_logger_uses_concrete_module(self):
        svc = ItemService()
        # Bind forces lazy resolution; name should reflect this test module
        bound = svc._log.bind()
        assert bound._logger.name == __name__


class TestServiceConfig:
    def test_config_is_none_by_default(self):
        svc = ItemService()
        assert svc._config is None

    def test_config_accessible(self):
        cfg = AppConfig()
        svc = ItemService(config=cfg)
        assert svc._config is cfg

    def test_config_attributes_accessible(self):
        cfg = AppConfig(overrides={"APP_NAME": "TestApp"})
        svc = ItemService(config=cfg)
        assert svc._config.APP_NAME == "TestApp"


class TestServiceRepo:
    def test_repo_without_conn(self, pf_tables):
        """Service with no conn — repos create their own transactions."""
        svc = ItemService()
        row_id = svc.add_item("no_conn_item")
        assert row_id is not None

        result = svc.find_item("no_conn_item")
        assert result is not None
        assert result["name"] == "no_conn_item"

    def test_repo_with_shared_conn(self, pf_tables, pf_connection):
        """Service with conn — repos share the caller's transaction."""
        svc = ItemService(conn=pf_connection)
        svc.add_item("shared_item")

        # The raw connection should see the insert (same transaction)
        row = pf_connection.execute(
            text("SELECT name FROM items WHERE name = :name"),
            {"name": "shared_item"},
        ).fetchone()
        assert row is not None

    def test_multiple_repos_share_conn(self, pf_tables, pf_connection):
        """Two repos from the same service share the connection."""
        svc = ItemService(conn=pf_connection)
        svc.add_item("multi_repo")

        # A second repo call should see the first repo's data
        result = svc.find_item("multi_repo")
        assert result is not None
