"""Who a release note reaches: the feature it is about, switched on and open.

Spec §4.6.1: workspaces see the changelog "filtered to features that
workspace has switched on". A note with no ``feature_key`` is for everyone
who has What's New. A note about a feature reaches a person only while that
feature runs here (its env kill switch is not off) and Feature Access lets
the person use it. The channel post has no single reader, so it only asks
whether the feature runs here (:func:`feature_switched_on`).

A key the registry no longer knows (a deleted module's) hides the note,
never raises: Review Focus 2. Any other failure hides it too (fail closed).
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.features import feature_allows
from rubik_core.features.policy import kill_switch_on
from rubik_core.features.registry import feature_by_key
from rubik_core.logsafe import safe_error
from rubik_core.whats_new.changelog import ChangelogEntry

logger = logging.getLogger(__name__)


def feature_switched_on(key: str | None) -> bool:
    """True for no key, or a registered feature whose kill switch is not off."""
    if not key:
        return True
    try:
        return kill_switch_on(feature_by_key(key)) is not False
    except KeyError:
        return False


def feature_label(key: str | None) -> str | None:
    """The registry label for ``key`` ("Scheduled tasks"); None for no key or
    one the registry no longer knows."""
    if not key:
        return None
    try:
        return feature_by_key(key).label
    except KeyError:
        return None


def can_see(entry: ChangelogEntry, user: Any) -> bool:
    """May ``user`` see ``entry``?"""
    key = entry.feature_key
    if not key:
        return True
    try:
        return feature_switched_on(key) and feature_allows(key, user)
    except KeyError:
        return False
    except Exception as exc:  # noqa: BLE001 — fail closed: hide, never leak
        logger.warning("[whats-new] audience check failed: %s", safe_error(exc))
        return False
