"""
Centralized logging configuration for Terrapod API server.

Configures structlog for JSON output in production and console in development.
"""

import logging
import sys
from datetime import UTC, datetime
from typing import Any

import structlog
from structlog.types import EventDict, Processor


def add_app_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add application context to log events."""
    event_dict["app"] = "terrapod-api"
    return event_dict


def reorder_keys(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Reorder keys so level and timestamp come first."""
    level = event_dict.pop("level", None)
    timestamp = event_dict.pop("timestamp", None)

    new_dict: EventDict = {}
    if level is not None:
        new_dict["level"] = level
    if timestamp is not None:
        new_dict["timestamp"] = timestamp

    new_dict.update(event_dict)
    return new_dict


def utc_timestamper(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add ISO8601 UTC timestamp to log events."""
    now = datetime.now(UTC)
    event_dict["timestamp"] = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return event_dict


def configure_logging(json_logs: bool = True, log_level: str = "INFO") -> None:
    """Configure logging for the entire application."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        utc_timestamper,
        structlog.processors.StackInfoRenderer(),
        add_app_context,
    ]

    if json_logs:
        structlog.configure(
            processors=shared_processors
            + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                reorder_keys,
                structlog.processors.JSONRenderer(),
            ],
        )
    else:
        structlog.configure(
            processors=shared_processors
            + [
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=True),
            ],
        )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Reduce noise from verbose libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)
