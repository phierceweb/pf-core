"""
Framework exception hierarchy.

Two branches — separating expected domain failures from actual errors.
Services raise domain exceptions; the HTTP layer translates them to status codes.

FlowException (expected domain failures — not bugs)
├── NotFoundError                   → 404  entity does not exist
├── InvalidInputError               → 422  bad data from caller
├── PreconditionError               → 409  state conflict (already done, wrong status)
├── ActionNotAllowedError           → 403  business rule says no
└── ConfigurationError              → 500  missing/invalid config = broken app
    └── PipelineNotRegisteredError  → 500  validator pipeline not registered

AppError (actual errors — bugs, infra failures)
├── ClientError             → 500  external API failed
├── DataError               → 500  database failure
└── TaskError               → 500  background task failure

The web app_factory registers a handler for each FlowException subclass so
every domain exception maps to the correct HTTP status automatically.

Service-layer rule: only raise FlowException or AppError subclasses.
Never raise bare Exception — it loses structured context and is unsearchable in logs.

Log key: APP-{ClassName}  (set by log_exception() in pf_core.log)
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# FlowException branch — expected domain failures, not bugs
# ---------------------------------------------------------------------------


class FlowException(Exception):
    """Base for expected domain failures.

    These are not bugs. They represent known conditions where the operation cannot
    proceed. Log at WARNING when caught explicitly; bubble to CLI/API boundary otherwise.
    """


class InvalidInputError(FlowException):
    """Bad input parameters — mapped to 422 by the HTTP layer.

    The caller supplied data that fails validation.
    """


class PreconditionError(FlowException):
    """Required state is not met — mapped to 409 Conflict by the HTTP layer.

    Examples: task already complete, entity not in expected state,
    can't delete a record that has dependents.
    """


class ActionNotAllowedError(FlowException):
    """Business rule forbids this action — mapped to 403 by the HTTP layer.

    The caller is authenticated but the domain says no.
    Examples: user can't grade their own submission, section is locked,
    export not available until review is complete.
    """


class NotFoundError(FlowException):
    """Requested entity does not exist.

    Carries the entity type and optional identifier for structured error
    messages and logging.  The HTTP layer maps this to 404 automatically.

    Usage::

        raise NotFoundError("Course", course_id)
        raise NotFoundError("Section")
    """

    def __init__(self, entity: str = "record", identifier: object = None) -> None:
        self.entity = entity
        self.identifier = identifier
        if identifier is not None:
            msg = f"{entity} not found: {identifier}"
        else:
            msg = f"{entity} not found"
        super().__init__(msg)


class ConfigurationError(FlowException):
    """Required configuration is missing or invalid — mapped to 500 by the HTTP layer.

    Missing config means the app is broken, not that the user did something wrong.
    Examples: DATABASE_URL not set, DB unreachable, config file missing a field.
    """


class PipelineNotRegisteredError(ConfigurationError):
    """Validator pipeline lookup failed — the consumer's validators module
    was not imported, or the agent slug is misspelled.

    Carries ``agent_type`` (the missing slug) and ``known_agents`` (slugs
    currently registered) for programmatic access. Raised by
    :func:`pf_core.llm.validate.parse_and_validate` when
    ``missing_pipeline="raise"`` (the default).
    """

    def __init__(self, agent_type: str, known_agents: list[str]) -> None:
        self.agent_type = agent_type
        self.known_agents = list(known_agents)
        known_str = ", ".join(self.known_agents) if self.known_agents else "(none)"
        msg = (
            f"agent_type '{agent_type}' has no registered validator pipeline. "
            f"Known agents: {known_str}. "
            "Ensure your project's validators module (typically "
            "`<project>.validators`) is imported before calling "
            "parse_and_validate, or pass missing_pipeline='fallback' to "
            "opt out of raising."
        )
        super().__init__(msg)


# ---------------------------------------------------------------------------
# AppError branch — actual errors, always log with traceback + context
# ---------------------------------------------------------------------------


class AppError(Exception):
    """Base for actual errors — unexpected failures that need investigation.

    Carries a structured context dict for log enrichment. Pass to log_exception()
    to log with full traceback and merged context chain.

    Args:
        message: Human-readable description of what failed.
        context: Key/value pairs added to the log record.
        cause:   Original exception, set as __cause__ for chaining and context inheritance.

    Example::

        raise AppError(
            "OpenRouter timed out",
            context={"task_id": task_id, "model": model},
            cause=e,
        )
    """

    def __init__(
        self,
        message: str = "",
        context: dict | None = None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.context: dict = context or {}
        if cause is not None:
            self.__cause__ = cause


class ClientError(AppError):
    """External API call failed (OpenRouter, Claude Code, etc.)."""


class DataError(AppError):
    """Database write or read failure."""


class TaskError(AppError):
    """Task-level failure — carries task_id (via context) and optional running_log.

    Always include task_id in context::

        raise TaskError(
            "search timed out",
            context={"task_id": task.id},
            running_log=notes_so_far,
            cause=e,
        )
    """

    def __init__(
        self,
        message: str = "",
        context: dict | None = None,
        *,
        running_log: str = "",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, context, cause=cause)
        self.running_log: str = running_log
