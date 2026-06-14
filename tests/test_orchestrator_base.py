"""Tests for pf_core.orchestrators.Orchestrator base class."""

from sqlalchemy import text

from pf_core.config import AppConfig
from pf_core.db.repository import Repository
from pf_core.orchestrators import Orchestrator
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

    def count(self) -> int:
        with self._tx() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM items")).scalar()


class ItemService(Service):
    def add_item(self, name: str) -> int:
        return self._repo(ItemRepo).insert(name)

    def item_count(self) -> int:
        return self._repo(ItemRepo).count()


class BatchOrchestrator(Orchestrator):
    def run(self, names: list[str]) -> int:
        svc = self._service(ItemService)
        for i, name in enumerate(names, 1):
            svc.add_item(name)
            self._report(i, len(names), f"Added {name}")
        return svc.item_count()


# --- Tests ---


class TestOrchestratorLogger:
    def test_has_logger(self):
        orch = BatchOrchestrator()
        assert orch._log is not None

    def test_logger_uses_concrete_module(self):
        orch = BatchOrchestrator()
        bound = orch._log.bind()
        assert bound._logger.name == __name__


class TestOrchestratorConfig:
    def test_config_is_none_by_default(self):
        orch = BatchOrchestrator()
        assert orch._config is None

    def test_config_passed_to_services(self):
        cfg = AppConfig(overrides={"APP_NAME": "OrcTest"})
        orch = BatchOrchestrator(config=cfg)
        svc = orch._service(ItemService)
        assert svc._config is cfg
        assert svc._config.APP_NAME == "OrcTest"


class TestOrchestratorService:
    def test_service_creates_instance(self):
        orch = BatchOrchestrator()
        svc = orch._service(ItemService)
        assert isinstance(svc, ItemService)

    def test_service_forwards_kwargs(self, pf_tables, pf_connection):
        orch = BatchOrchestrator()
        svc = orch._service(ItemService, conn=pf_connection)
        assert svc._conn is pf_connection

    def test_end_to_end(self, pf_tables):
        orch = BatchOrchestrator()
        count = orch.run(["a", "b", "c"])
        assert count == 3


class TestOrchestratorProgress:
    def test_report_without_callback(self):
        """_report doesn't raise when no callback is set."""
        orch = BatchOrchestrator()
        orch._report(1, 1, "test")  # should not raise

    def test_report_calls_callback(self):
        calls = []
        orch = BatchOrchestrator(progress=lambda s, t, m: calls.append((s, t, m)))
        orch._report(1, 3, "first")
        orch._report(2, 3, "second")
        assert calls == [(1, 3, "first"), (2, 3, "second")]

    def test_end_to_end_with_progress(self, pf_tables):
        calls = []
        orch = BatchOrchestrator(
            progress=lambda s, t, m: calls.append((s, t, m)),
        )
        orch.run(["x", "y"])
        assert len(calls) == 2
        assert calls[0] == (1, 2, "Added x")
        assert calls[1] == (2, 2, "Added y")
