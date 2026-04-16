"""Structured logging bootstrap.

Importing :func:`configure` idempotently wires ``structlog`` to emit JSON
events with an ISO timestamp and the module logger name. Stdlib ``logging``
handlers route into the same structlog renderer so third-party libraries
(``httpx``, ``uvicorn``) get the same format.
"""
from __future__ import annotations

import logging
import os
import sys

import structlog

_CONFIGURED = False


def configure(level: str | int | None = None) -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level_name = (
        level if isinstance(level, str) and level else os.getenv("PISCO_LOG_LEVEL", "INFO")
    )
    log_level = (
        log_level_name
        if isinstance(log_level_name, int)
        else getattr(logging, str(log_level_name).upper(), logging.INFO)
    )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    _CONFIGURED = True


def get_logger(name: str | None = None):
    """Return a bound structlog logger. Auto-configures on first call."""
    configure()
    return structlog.get_logger(name)
