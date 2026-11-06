"""
Logging configuration with conditional JSON formatting for Admins.
"""

import logging
import sys

from pythonjsonlogger import jsonlogger

from rubik_core.config import settings
from rubik_core.telemetry import is_admin_context


class ConditionalFormatter(logging.Formatter):
    """
    A formatter that outputs JSON if in an admin context,
    otherwise outputs standard human-readable text.
    """

    def __init__(self, fmt=None, datefmt=None, style="%"):
        super().__init__(fmt, datefmt, style)
        self.standard_formatter = logging.Formatter(fmt, datefmt, style)
        # JSON formatter includes OTEL trace/span IDs automatically via LoggingInstrumentor
        self.json_formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s %(otelTraceID)s %(otelSpanID)s"
        )

    def format(self, record):
        if is_admin_context():
            return self.json_formatter.format(record)
        return self.standard_formatter.format(record)


def setup_logging():
    """
    Configures logging with conditional formatting:
    - Standard text for regular users.
    - Structured JSON for admins and system background tasks.
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    formatter = ConditionalFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Silence verbose third-party loggers
    if log_level > logging.DEBUG:
        for logger_name in ["google", "urllib3", "asyncio", "uvicorn"]:
            logging.getLogger(logger_name).setLevel(logging.WARNING)

    logging.info(
        f"Logging initialized (Level: {settings.LOG_LEVEL}, Conditional JSON: Enabled for Admins)"
    )
