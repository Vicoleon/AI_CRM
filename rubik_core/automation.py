"""One operator switch for automatic and detached billable processing."""

from __future__ import annotations

import os

from rubik_core.config import settings

AUTOMATIONS_PAUSED_MESSAGE = (
    "Automatic processing is paused while development continues."
)


class AutomationsPausedError(RuntimeError):
    """The operator has paused automatic/background processing."""


def automations_enabled() -> bool:
    """Read an explicit process switch first; malformed overrides stay paused.

    Release scripts source the operator environment before launching Python.
    Reading the override here also keeps CLI preflights and request workers on
    the same policy when settings were imported earlier in the process.
    """
    override = os.environ.get("CUBIE_AUTOMATIONS_ENABLED")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return settings.CUBIE_AUTOMATIONS_ENABLED


def require_automations_enabled() -> None:
    if not automations_enabled():
        raise AutomationsPausedError(AUTOMATIONS_PAUSED_MESSAGE)
