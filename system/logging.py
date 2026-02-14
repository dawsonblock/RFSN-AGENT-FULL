"""Structured logging configuration."""

import logging
import os
import sys
from typing import Any, Optional

import structlog


def configure_logger() -> None:
    """Configure structlog and standard logging."""

    # Check dev mode
    dev_mode = os.getenv("RFSN_DEV_MODE", "0") == "1"

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if dev_mode:
        # Pretty print for local dev
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        # JSON for production
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: Optional[str] = None) -> Any:
    """Get a structured logger."""
    return structlog.get_logger(name)
