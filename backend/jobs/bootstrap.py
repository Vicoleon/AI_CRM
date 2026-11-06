"""Validate a Job's workspace before importing tenant-aware work."""

from __future__ import annotations

import logging

from rubik_core.automation import automations_enabled
from rubik_core.tenancy.context import bootstrap_process_workspace

logger = logging.getLogger(__name__)


def bootstrap_job_workspace() -> bool:
    """Return false when a Job lacks an active, registered workspace."""
    if not automations_enabled():
        logger.info("Automatic processing is paused; Job exits without work")
        # A held execution is intentional, not a failure to retry. Migration
        # maintenance has its own entrypoint and does not use this bootstrap.
        raise SystemExit(0)
    try:
        bootstrap_process_workspace()
    except Exception:  # noqa: BLE001 - fail closed for unavailable control data too
        logger.error("Job workspace is missing or unavailable")
        return False
    return True
