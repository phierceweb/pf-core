"""
Base service class for single-domain business logic.

A service owns one slice of domain logic — user profiles, report generation,
catalog scanning, etc.  It receives its dependencies (config, connection)
via the constructor rather than reaching for globals.

Usage::

    from pf_core.services import Service

    class ReportService(Service):
        def active_reports(self) -> list[dict]:
            repo = self._repo(ReportRepo)
            return repo.list_active()

        def score(self, text: str) -> float:
            threshold = self._config.SCORE_THRESHOLD
            ...

    # Standalone:
    svc = ReportService(config=cfg)

    # Inside an orchestrator (shared connection):
    with transaction() as conn:
        svc = ReportService(config=cfg, conn=conn)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pf_core.log import get_logger

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from pf_core.config import AppConfig
    from pf_core.db.repository import Repository


class Service:
    """Base class for single-domain business logic units.

    Subclasses get:
        - ``self._log`` — a structlog logger named after the concrete module.
        - ``self._config`` — the project's AppConfig instance (if provided).
        - ``self._repo(RepoCls)`` — factory that shares the service's connection
          with any repository it instantiates.

    Services MUST NOT:
        - Import from orchestrators or entry points.
        - Call ``transaction()`` directly — use ``self._repo()`` instead.
        - Read ``os.environ`` — use ``self._config``.
        - ``print()`` — use ``self._log``.
    """

    def __init__(
        self,
        *,
        conn: Connection | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self._conn = conn
        self._config = config
        self._log = get_logger(self.__class__.__module__)

    def _repo(self, repo_cls: type[Repository]) -> Repository:
        """Instantiate a repository, sharing our connection if we have one."""
        return repo_cls(self._conn)
