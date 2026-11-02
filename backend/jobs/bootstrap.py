"""Validate a Job's workspace before importing tenant-aware work."""

from __future__ import annotations

import logging

from rubik_core.tenancy.context import bootstrap_process_workspace

logger = logging.getLogger(__name__)


def bootstrap_job_workspace() -> bool:
    """Return false when a Job lacks an active, registered workspace."""
    try:
        bootstrap_process_workspace()
    except Exception:  # noqa: BLE001 - fail closed for unavailable control data too
        logger.error("Job workspace is missing or unavailable")
        return False
    return True
