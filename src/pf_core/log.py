"""
Structured logging via structlog.

Usage::

    from pf_core.log import get_logger, log_context, log_exception, setup_logging

    # At app startup (once):
    setup_logging(level="INFO", log_file="logs/app.jsonl")

    # In any module:
    logger = get_logger(__name__)

    with log_context(task_id=42, section_name="intro"):
        logger.info("search_started", model="perplexity/sonar-pro")

Environment (read by setup_logging when no explicit args):
    LOG_LEVEL   Console log level: DEBUG / INFO / WARNING / ERROR  (default INFO)
    LOG_FILE    Path for JSON-lines log file; empty = file logging off
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import structlog

_setup_done = False

# Logger name the handlers are attached to, resolved by setup_logging().
# "" means the root logger (the default). log_exception() logs under it.
_app_logger_name = ""

_shared_processors: list = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.StackInfoRenderer(),
    # NOTE: format_exc_info is deliberately NOT in the shared chain. It must not
    # run before ConsoleRenderer, which renders exceptions itself (prettily) and
    # warns if a pre-formatted `exception` string is already present. It's
    # applied only in the JSON/file path below, which does want the string form.
]


def setup_logging(
    *,
    level: str | None = None,
    log_file: str | None = None,
    app_logger_name: str | None = None,
) -> None:
    """Configure structlog + stdlib handlers. Safe to call multiple times.

    Args:
        level: Console log level. Falls back to LOG_LEVEL env var, then "INFO".
        log_file: Path for JSON-lines log file. Falls back to LOG_FILE env var.
        app_logger_name: Logger to attach handlers to. Default (``None``) is the
            **root** logger, so every logger propagates to the handlers no
            matter what the consumer's top-level package is named — a project
            that adopts pf-core after the fact need not be called ``app`` and
            need not pass anything here. Pass a name (e.g. your package) to
            scope handlers to that one logger instead.
    """
    global _setup_done, _app_logger_name
    if _setup_done:
        return
    _setup_done = True

    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    console_level = getattr(logging, level_name, logging.INFO)

    structlog.configure(
        processors=_shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # "" -> root logger (logging.getLogger("") is the root).
    _app_logger_name = app_logger_name if app_logger_name is not None else ""
    app_log = logging.getLogger(_app_logger_name)
    app_log.setLevel(logging.DEBUG)

    if app_log.handlers:
        return

    # Console handler with colored output
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(sort_keys=False),
            foreign_pre_chain=_shared_processors,
        )
    )
    app_log.addHandler(ch)

    # JSON-lines file handler (always at DEBUG)
    file_path = log_file if log_file is not None else os.environ.get("LOG_FILE", "")
    if file_path and file_path.strip():
        log_path = Path(file_path.strip())
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                # format_exc_info runs here (JSON wants the traceback as a
                # string), kept out of the shared chain so it never precedes the
                # console's ConsoleRenderer.
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
                foreign_pre_chain=_shared_processors,
            )
        )
        app_log.addHandler(fh)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger, calling setup_logging() if needed."""
    setup_logging()
    return structlog.get_logger(name)


# Context binding — attach fields to all log records within a with-block.
log_context = structlog.contextvars.bound_contextvars


def log_verbose(
    logger: structlog.stdlib.BoundLogger,
    msg: str,
    verbose: bool = True,
    **kw,
) -> None:
    """Log at INFO when verbose, DEBUG otherwise."""
    if verbose:
        logger.info(msg, **kw)
    else:
        logger.debug(msg, **kw)


def log_exception(
    exc: BaseException,
    *,
    message_prepend: str = "",
    additional_context: dict | None = None,
    log_level: str | None = None,
    event_prefix: str = "APP",
) -> None:
    """Log an exception with structured context.

    Log event key: {event_prefix}-{ClassName} — use as the primary search key in
    log files, e.g. ``grep APP-SearchError app.log``.

    Behaviour by exception type:
        AppError: full traceback + merged context chain; default ERROR.
        FlowException: no traceback; default WARNING.
        Anything else: no traceback; default ERROR.

    Context merging (highest → lowest priority):
        1. additional_context — always wins on duplicate keys.
        2. exc.context — from the thrown AppError.
        3. __cause__ / __context__ chain — ancestor AppError instances fill
           in missing keys only.

    Args:
        exc:                The exception to log.
        message_prepend:    Prefix for the log message.
        additional_context: Extra key/value pairs merged into the log record.
        log_level:          Override default level.
        event_prefix:       Prefix for the log event key (default "APP").
    """
    from pf_core.exceptions import AppError, FlowException

    setup_logging()
    # Log under the configured app-logger tree so the record reaches the same
    # handlers (root by default) — not a hardcoded "app.exceptions" that a
    # non-"app" consumer's logging tree never sees.
    exc_logger_name = f"{_app_logger_name}.exceptions" if _app_logger_name else "exceptions"
    logger = structlog.get_logger(exc_logger_name)

    # Determine log level
    if log_level is None:
        if isinstance(exc, FlowException):
            log_level = "warning"
        else:
            log_level = "error"

    # Build merged context
    ctx: dict = {}

    # Walk __cause__ / __context__ chain
    ancestors: list[AppError] = []
    seen_ids: set[int] = set()
    chain: BaseException | None = exc.__cause__ or exc.__context__
    while chain is not None and id(chain) not in seen_ids:
        seen_ids.add(id(chain))
        if isinstance(chain, AppError):
            ancestors.append(chain)
        chain = chain.__cause__ or chain.__context__

    # Lowest priority: oldest ancestor context
    for ancestor in reversed(ancestors):
        for k, v in ancestor.context.items():
            ctx.setdefault(k, v)

    # exc.context overwrites ancestor duplicates
    if isinstance(exc, AppError):
        ctx.update(exc.context)

    # additional_context wins everything
    if additional_context:
        ctx.update(additional_context)

    # Build log message
    exc_msg = str(exc)
    message = f"{message_prepend}: {exc_msg}" if message_prepend else exc_msg

    # Log event key
    event = f"{event_prefix}-{type(exc).__name__}"

    # Full traceback only for AppError
    exc_info: BaseException | bool = exc if isinstance(exc, AppError) else False

    log_fn = getattr(logger, log_level, logger.error)
    log_fn(event, message=message, exc_info=exc_info, **ctx)
