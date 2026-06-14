"""
Base orchestrator class for multi-step workflow coordination.

An orchestrator coordinates services to accomplish a multi-step task —
exporting entries, running auto-review, scanning a catalog.  It never
touches the database directly; all data access goes through services
(which use repos).

Usage::

    from pf_core.orchestrators import Orchestrator

    class ExportOrchestrator(Orchestrator):
        def run(self, entry_ids: list[str]) -> ExportResult:
            entry_svc = self._service(EntryService)
            tag_svc = self._service(TagService)

            self._report(1, 3, "Loading entries")
            entries = entry_svc.load_many(entry_ids)

            self._report(2, 3, "Resolving tags")
            tagged = tag_svc.attach_tags(entries)

            self._report(3, 3, "Building export")
            return ExportResult(tagged)

    # With progress callback (CLI, web job, etc.):
    orch = ExportOrchestrator(config=cfg, progress=my_callback)
    result = orch.run(ids)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pf_core.log import get_logger

if TYPE_CHECKING:
    from pf_core.config import AppConfig
    from pf_core.services.base import Service


@runtime_checkable
class ProgressCallback(Protocol):
    """Callable signature for progress reporting."""

    def __call__(self, step: int, total: int, message: str) -> None: ...


class Orchestrator:
    """Base class for multi-step workflow coordination.

    Subclasses get:
        - ``self._log`` — a structlog logger named after the concrete module.
        - ``self._config`` — the project's AppConfig instance (if provided).
        - ``self._service(SvcCls, **kw)`` — factory that passes config to services.
        - ``self._report(step, total, message)`` — progress reporting.

    Orchestrators MUST NOT:
        - Import or instantiate repositories directly.
        - Call ``transaction()`` — services own data access.
        - Contain domain logic — delegate to services.
    """

    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self._config = config
        self._progress = progress
        self._log = get_logger(self.__class__.__module__)

    def _service(self, svc_cls: type[Service], **kwargs) -> Service:
        """Instantiate a service with our config.

        Extra keyword arguments are forwarded to the service constructor,
        allowing callers to pass a shared ``conn`` when needed.
        """
        return svc_cls(config=self._config, **kwargs)

    def _report(self, step: int, total: int, message: str) -> None:
        """Report progress if a callback was provided.

        Always logs at INFO regardless of whether a callback exists.
        """
        if self._progress:
            self._progress(step, total, message)
        self._log.info("progress", step=step, total=total, message=message)
