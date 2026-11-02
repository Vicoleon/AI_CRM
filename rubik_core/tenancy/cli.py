"""Fail closed before a hand-run command touches tenant resources."""

from __future__ import annotations

import logging

from rubik_core.tenancy.context import bootstrap_process_workspace

logger = logging.getLogger(__name__)


def require_cli_workspace() -> str:
    """Select an active workspace from CUBIE_WORKSPACE or exit with code 2."""
    try:
        return bootstrap_process_workspace()
    except Exception:  # noqa: BLE001 - control DB failures must prevent work too
        logger.error(
            "Set CUBIE_WORKSPACE to an active workspace before running this command"
        )
        raise SystemExit(2) from None
