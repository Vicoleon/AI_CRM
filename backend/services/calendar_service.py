"""CalendarService — thin signal-oriented wrapper around `GoogleCalendarClient`.

Mirrors `GmailService.list_threads_for_signals`: gives the UC-17 signal layer a
single call to fetch the raw event dicts it needs for a `[time_min, time_max]`
window without each consumer re-implementing OAuth / DWD setup.

UI-facing callers keep using `GoogleCalendarClient` directly; this module
exists so `backend.signals.calendar` does not have to.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from rubik_core.tools.calendar import GoogleCalendarClient, is_working_location_event

logger = logging.getLogger(__name__)


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CalendarService:
    def __init__(self, user_email: str, client: GoogleCalendarClient | None = None):
        self.user_email = user_email
        self._client = client or GoogleCalendarClient(user_email=user_email)

    def list_events_for_signals(
        self,
        *,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """Raw event dicts in `[time_min, time_max]` for UC-17 signal extraction.

        Returns Google Calendar's hydrated event shape (attendees, summary,
        start/end, conferenceData, recurringEventId, organizer). Signal
        extraction needs all of these; the trimmed UI shape used by
        `get_upcoming_events` would drop them.
        """
        if not self._client.service:
            return []
        try:
            resp = (
                self._client.service.events()
                .list(
                    calendarId="primary",
                    timeMin=_to_rfc3339(time_min),
                    timeMax=_to_rfc3339(time_max),
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return [
                e for e in resp.get("items", []) if not is_working_location_event(e)
            ]
        except Exception as e:
            logger.warning(
                "[calendar_service] list_events_for_signals failed for %s: %s",
                self.user_email,
                e,
            )
            return []
