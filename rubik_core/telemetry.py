"""
Telemetry and Observability Configuration.

Configures OpenTelemetry for Cloud Trace and instruments FastAPI.
Also provides a context-aware logging formatter for Admin-only JSON logs.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ContextVar to track if the current request is from an admin
admin_context: ContextVar[bool] = ContextVar("admin_context", default=False)


def setup_telemetry(app: FastAPI):
    """Initializes OpenTelemetry and instruments the FastAPI app."""

    # 1. Setup TracerProvider
    tracer_provider = TracerProvider()

    # 2. Add Cloud Trace Exporter (GCP)
    # The user confirmed they have full gcloud access/auth locally, but to prevent
    # 403 spam when Trace API is still propagating, we only enable this in GCP mode.
    from rubik_core.config import AppMode, settings

    if (
        settings.APP_MODE == AppMode.GCP
        and not os.getenv("DISABLE_TELEMETRY", "false").lower() == "true"
    ):
        try:
            exporter = CloudTraceSpanExporter()
            tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(tracer_provider)
        except Exception as e:
            logging.warning(
                f"Failed to initialize CloudTraceSpanExporter: {e}. Tracing will be disabled."
            )
            trace.set_tracer_provider(tracer_provider)
    else:
        trace.set_tracer_provider(tracer_provider)

    # 3. Instrument FastAPI
    FastAPIInstrumentor.instrument_app(app)

    # 4. Instrument Logging (injects trace_id/span_id into log records)
    LoggingInstrumentor().instrument(set_logging_format=False)

    logging.info("Telemetry initialized: OpenTelemetry + Cloud Trace")


def set_admin_context(is_admin: bool):
    """Sets the admin context for the current task/request."""
    admin_context.set(is_admin)


def is_admin_context() -> bool:
    """Returns True if the current context is an admin context."""
    return admin_context.get()
