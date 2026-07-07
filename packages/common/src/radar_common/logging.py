"""Structured logging for RADAR services.

Every service logs one JSON object per line to stdout, which Fluent Bit ships to
Elasticsearch. ``correlation_id`` is threaded through the whole incident
pipeline, so it is bound once per request via :func:`bind_correlation_id` and
then rides on every log line automatically through ``structlog`` context
variables.

Usage::

    configure_logging(service_name="watcher-agent")
    bind_correlation_id(event.correlation_id)
    log = get_logger(__name__)
    log.info("incident.opened", incident_id=str(incident.id))

Which emits (one line, pretty-printed here)::

    {"event": "incident.opened", "level": "info", "service": "watcher-agent",
     "correlation_id": "…", "incident_id": "…", "timestamp": "2026-07-06T…Z"}
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
)
from structlog.typing import EventDict, FilteringBoundLogger, Processor, WrappedLogger

CORRELATION_ID_KEY = "correlation_id"
"""Log key carrying the pipeline-wide correlation id."""


def configure_logging(*, service_name: str, level: str = "INFO") -> None:
    """Configure structlog to emit JSON to stdout for one service.

    Idempotent: safe to call once at process start. ``level`` is a standard
    logging level name (e.g. ``"INFO"``, ``"DEBUG"``); unknown names fall back
    to ``INFO``.
    """
    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    processors: list[Processor] = [
        merge_contextvars,
        _inject_service_name(service_name),
        _ensure_correlation_id,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Return a bound logger, optionally named after the calling module."""
    return cast(FilteringBoundLogger, structlog.get_logger(name))


def bind_correlation_id(correlation_id: UUID | str) -> None:
    """Bind ``correlation_id`` so every subsequent log line in this context
    carries it. Call once per request/event before doing any work."""
    bind_contextvars(**{CORRELATION_ID_KEY: str(correlation_id)})


def clear_context() -> None:
    """Clear all bound context variables (e.g. at the end of a request)."""
    clear_contextvars()


def _inject_service_name(service_name: str) -> Processor:
    """Build a processor that stamps the owning service on every log line."""

    def processor(
        logger: WrappedLogger, method_name: str, event_dict: EventDict
    ) -> EventDict:
        event_dict.setdefault("service", service_name)
        return event_dict

    return processor


def _ensure_correlation_id(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Guarantee a ``correlation_id`` key exists, even outside a request."""
    event_dict.setdefault(CORRELATION_ID_KEY, None)
    return event_dict
