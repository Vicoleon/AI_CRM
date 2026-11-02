"""EOS domain logic that is not persistence and not HTTP.

Lives outside ``backend/`` so scripts and Cloud Run Jobs can import it
without pulling in FastAPI.
"""

from rubik_core.eos.calendar_reconcile import (
    ReconcileReport,
    reconcile_l10_calendars,
)
from rubik_core.eos.calendar_sync import (
    STATE_ACTIVE,
    STATE_CANCELLED,
    STATE_UNKNOWN,
    SeriesState,
    apply_to_meeting,
    is_calendar_cancelled,
    may_persist,
    resolve_series_state,
)
from rubik_core.eos.session_lifecycle import (
    AUTO_CLOSE_ACTOR,
    BOX_GRACE,
    INACTIVITY_WINDOW,
    CloseReport,
    OpenSession,
    close_abandoned_sessions,
    live_session_ids,
    open_sessions,
)

__all__ = [
    "AUTO_CLOSE_ACTOR",
    "STATE_ACTIVE",
    "STATE_CANCELLED",
    "STATE_UNKNOWN",
    "ReconcileReport",
    "SeriesState",
    "apply_to_meeting",
    "is_calendar_cancelled",
    "may_persist",
    "reconcile_l10_calendars",
    "resolve_series_state",
    "BOX_GRACE",
    "INACTIVITY_WINDOW",
    "CloseReport",
    "OpenSession",
    "close_abandoned_sessions",
    "live_session_ids",
    "open_sessions",
]
