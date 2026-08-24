"""Structured logging with a correlation ID threaded through every record.

The legacy system logs with 46 stray ``print()`` calls sitting alongside a newer
``logger.py`` that was never fully adopted. Here there is one way to log, it emits JSON in
every deployed environment, and every line carries the correlation ID of the request or job
that produced it — which is what makes the developer panel's request tracing possible at all.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

import structlog

from app.core.config import Settings

#: Set by the correlation-ID middleware and by every Celery task prologue.
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid4().hex


def get_correlation_id() -> str | None:
    return correlation_id_var.get()


def bind_correlation_id(value: str | None = None) -> str:
    """Bind a correlation ID to the current context and return it."""
    resolved = value or new_correlation_id()
    correlation_id_var.set(resolved)
    return resolved


def _add_correlation_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    cid = correlation_id_var.get()
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Idempotent — safe to call from both the app factory and the Celery worker."""
    level = getattr(logging, settings.log_level)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_correlation_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # stdlib factory, not PrintLoggerFactory: add_logger_name needs a logger with a
        # .name, and this keeps app output on the same handler as uvicorn's.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, celery) through the same handler so we do
    # not end up with two log formats interleaved on stdout.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
