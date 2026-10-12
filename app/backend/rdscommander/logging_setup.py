"""Structured JSON logging. Every error log must carry `fix_suggestion`.

Example:
    >>> configure_structured_logging()
    >>> get_logger(__name__).info("control_db_connected", database_host="localhost")
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_structured_logging(level: str = "INFO") -> None:
    """Route structlog and stdlib logging to JSON lines on stdout."""
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger named `name`."""
    return structlog.get_logger(name)
