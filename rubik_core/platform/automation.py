"""Conservative defaults for explicit platform operator entrypoints."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from rubik_core.automation import automations_enabled


def operator_automations_enabled() -> bool:
    """A platform operation needs an explicit opt-in to activate automations."""
    return "CUBIE_AUTOMATIONS_ENABLED" in os.environ and automations_enabled()


@contextmanager
def operator_automation_policy() -> Iterator[None]:
    """Carry the conservative default into provisioning and schedule helpers."""
    was_missing = "CUBIE_AUTOMATIONS_ENABLED" not in os.environ
    os.environ.setdefault("CUBIE_AUTOMATIONS_ENABLED", "false")
    try:
        yield
    finally:
        if was_missing:
            os.environ.pop("CUBIE_AUTOMATIONS_ENABLED", None)
