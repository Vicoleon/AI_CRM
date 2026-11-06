"""Shared weekend-aware lookback helper for UC-17 source modules.

Gmail (UC-17.2) and Slack (UC-17.3) both need the same widening behavior so
priorities don't get chopped at the Friday EOB boundary by a naive 24h Monday
tick. Calendar/transcript modules will reuse this once UC-17.4/.5 land.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

FRIDAY_EOB_HOUR_LOCAL = 17  # 5pm local


def widen_for_weekend(
    since: datetime,
    *,
    now: datetime,
    user_tz: str | None = None,
) -> tuple[datetime, bool]:
    """Widen `since` backward to Friday 17:00 local on weekends / early Monday.

    Returns `(effective_since, was_widened)`. Never narrows. The check is on
    `now` (when the orchestrator wakes) rather than `since` itself, because
    the failure mode is a Monday-02:00-UTC run with a naive 24h lookback
    chopping Friday evening.

    ``user_tz`` is the reader's IANA zone when the caller knows it. Without
    one, "local" is the company's default zone
    (``rubik_core.priorities.temporal.resolve_tz``: ``PRIORITIES_DEFAULT_TZ``,
    UTC until an admin sets it), never a zone written into the code.
    """
    from rubik_core.priorities.temporal import resolve_tz  # local: keep it light

    tz, _name = resolve_tz(user_tz)
    local_now = now.astimezone(tz)
    weekday = local_now.weekday()  # Mon=0 .. Sun=6

    if weekday == 0 and local_now.hour >= FRIDAY_EOB_HOUR_LOCAL:
        return since, False
    if weekday not in (5, 6, 0):
        return since, False

    days_back = (weekday - 4) % 7  # Fri=4
    friday_local = (local_now - timedelta(days=days_back)).replace(
        hour=FRIDAY_EOB_HOUR_LOCAL,
        minute=0,
        second=0,
        microsecond=0,
    )
    friday_utc = friday_local.astimezone(timezone.utc)
    if friday_utc < since:
        return friday_utc, True
    return since, False
