"""Calendar SignalSource — reshapes Google Calendar events into UC-17 Signals.

Reuses `backend.services.calendar_service.CalendarService` (thin wrapper over
`rubik_core.tools.calendar.GoogleCalendarClient`) for OAuth + event listing;
no new calendar client lives here.

Semantics of `since` differ from Gmail/Slack: calendar events have explicit
start/end timestamps, so weekend-aware lookback widening is not needed. We
treat `since` as "now" and pull a fixed window around it — `LOOKBACK_DAYS`
backward (to spot missed follow-ups) and `LOOKAHEAD_HOURS` forward (to surface
imminent customer meetings). The correlator (UC-17.7) joins to HubSpot/Gmail
on `contact_email` and `company_name` hints.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.signals.types import Signal

logger = logging.getLogger(__name__)


LOOKAHEAD_HOURS = 48
LOOKBACK_DAYS = 7
FOLLOWUP_WINDOW_DAYS = 14
HIGH_SEVERITY_HOURS = 4

# Heuristic: titles that look like recurring internal syncs. Only applied when
# the event has no external attendees — external participation always wins.
_INTERNAL_SYNC_PATTERNS: tuple[str, ...] = (
    "standup",
    "stand-up",
    "stand up",
    "1:1",
    "1-1",
    "one on one",
    "team sync",
    "all hands",
    "all-hands",
    "weekly",
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _parse_event_time(node: dict[str, Any] | None) -> datetime | None:
    """Google Calendar `start`/`end` is `{dateTime}` for timed events or
    `{date}` for all-day events. All-day events are skipped by callers, but we
    still handle the shape defensively."""
    if not node:
        return None
    raw = node.get("dateTime") or node.get("date")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _user_domain(user_email: str) -> str:
    return user_email.split("@", 1)[1].lower() if "@" in user_email else ""


def _attendee_email(att: dict[str, Any]) -> str:
    return (att.get("email") or "").lower()


def _external_attendees(
    attendees: list[dict[str, Any]],
    user_domain: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for att in attendees or []:
        email = _attendee_email(att)
        if not email or "@" not in email:
            continue
        if email.split("@", 1)[1] == user_domain:
            continue
        out.append(att)
    return out


def _pick_primary_contact(
    externals: list[dict[str, Any]],
    organizer_email: str,
) -> str:
    """First non-organizer external attendee — fine as a hint for the
    correlator. Falls back to first external if all are the organizer."""
    org = (organizer_email or "").lower()
    for att in externals:
        if _attendee_email(att) != org:
            return _attendee_email(att)
    return _attendee_email(externals[0]) if externals else ""


def _company_name_hint(email: str) -> str:
    """Strip common TLD suffixes from the domain to produce a fuzzy company
    name hint. Best-effort — `acme.com` → `acme`, `acme.co.uk` → `acme`. The
    correlator must treat this as a hint, not a key."""
    if not email or "@" not in email:
        return ""
    domain = email.split("@", 1)[1].lower()
    head = domain.split(".", 1)[0]
    return head


def _is_internal_sync(summary: str, externals: list[dict[str, Any]]) -> bool:
    if externals:
        return False
    s = (summary or "").lower()
    return any(p in s for p in _INTERNAL_SYNC_PATTERNS)


def _meet_link(event: dict[str, Any]) -> str | None:
    cd = event.get("conferenceData") or {}
    for entry in cd.get("entryPoints") or []:
        if entry.get("entryPointType") == "video":
            return entry.get("uri")
    return event.get("hangoutLink")


def _organizer_email(event: dict[str, Any]) -> str:
    return ((event.get("organizer") or {}).get("email") or "").lower()


def _truncate(text: str, limit: int = 140) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _hours_until(start: datetime, now: datetime) -> float:
    return (start - now).total_seconds() / 3600.0


def _format_when(hours: float) -> str:
    if hours < 1:
        mins = max(1, int(hours * 60))
        return f"in {mins}m"
    if hours < 24:
        return f"in {int(round(hours))}h"
    days = int(round(hours / 24))
    return f"in {days}d"


# ─── Signal builders ──────────────────────────────────────────────────────────


def _upcoming_meeting_signal(
    *,
    user_email: str,
    event: dict[str, Any],
    start: datetime,
    end: datetime,
    externals: list[dict[str, Any]],
    now: datetime,
) -> Signal:
    organizer = _organizer_email(event)
    contact_email = _pick_primary_contact(externals, organizer)
    company = _company_name_hint(contact_email)
    title = event.get("summary") or "(no title)"
    hours = _hours_until(start, now)
    severity = "high" if hours < HIGH_SEVERITY_HOURS else "medium"
    summary = _truncate(
        f"Upcoming meeting {_format_when(hours)} with {contact_email} — '{title}'"
    )
    attendee_emails = [_attendee_email(a) for a in externals if _attendee_email(a)]
    return Signal(
        user_email=user_email,
        source="calendar",
        signal_type="upcoming_meeting_with_customer",
        summary=summary,
        occurred_at=start,
        entity_refs={
            k: v
            for k, v in {
                "event_id": event.get("id"),
                "contact_email": contact_email,
                "company_name": company,
            }.items()
            if v
        },
        severity_hint=severity,
        payload={
            "event_id": event.get("id"),
            "event_title": title,
            "starts_at": start.isoformat(),
            "end_at": end.isoformat() if end else None,
            "attendee_emails": attendee_emails,
            "organizer_email": organizer,
            "is_recurring": bool(event.get("recurringEventId")),
            "meet_link": _meet_link(event),
        },
    )


def _missed_followup_signal(
    *,
    user_email: str,
    event: dict[str, Any],
    start: datetime,
    end: datetime,
    externals: list[dict[str, Any]],
) -> Signal:
    organizer = _organizer_email(event)
    contact_email = _pick_primary_contact(externals, organizer)
    company = _company_name_hint(contact_email)
    title = event.get("summary") or "(no title)"
    summary = _truncate(f"No follow-up scheduled with {contact_email} after '{title}'")
    attendee_emails = [_attendee_email(a) for a in externals if _attendee_email(a)]
    return Signal(
        user_email=user_email,
        source="calendar",
        signal_type="missed_followup",
        summary=summary,
        occurred_at=start,
        entity_refs={
            k: v
            for k, v in {
                "event_id": event.get("id"),
                "contact_email": contact_email,
                "company_name": company,
            }.items()
            if v
        },
        severity_hint="medium",
        payload={
            "event_id": event.get("id"),
            "event_title": title,
            "starts_at": start.isoformat(),
            "end_at": end.isoformat() if end else None,
            "attendee_emails": attendee_emails,
            "organizer_email": organizer,
            "is_recurring": bool(event.get("recurringEventId")),
            "meet_link": _meet_link(event),
        },
    )


# ─── SignalSource ─────────────────────────────────────────────────────────────


class CalendarSignalSource:
    """Implements `SignalSource`. Pulls calendar events via `CalendarService`
    and reshapes them into `upcoming_meeting_with_customer` and
    `missed_followup` Signals.

    `since` here functions as "now" — we pull a `[now - 7d, now + 48h]` window
    plus a forward look out to `now + 14d` for follow-up detection. This
    deviates from Gmail/Slack (where `since` is the lower bound of an
    incremental pull); calendar events have explicit start/end timestamps so a
    fixed window is the natural shape.
    """

    name = "calendar"

    def __init__(
        self,
        calendar_service: Any | None = None,
        *,
        now: datetime | None = None,
    ):
        self._svc = calendar_service
        self._now = now

    def _service_for(self, user_email: str) -> Any | None:
        if self._svc is not None:
            return self._svc
        try:
            from backend.services.calendar_service import CalendarService

            return CalendarService(user_email=user_email)
        except Exception as e:
            logger.warning(
                "[signals.calendar] CalendarService init failed for %s: %s",
                user_email,
                e,
            )
            return None

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]:
        svc = self._service_for(user_email)
        if svc is None:
            return []

        now = self._now or since or datetime.now(timezone.utc)
        rep = user_email.lower()
        rep_domain = _user_domain(rep)
        if not rep_domain:
            return []

        window_min = now - timedelta(days=LOOKBACK_DAYS)
        # Pull forward far enough to also evaluate follow-ups within 14 days.
        window_max = now + timedelta(days=FOLLOWUP_WINDOW_DAYS)

        try:
            events = svc.list_events_for_signals(
                time_min=window_min,
                time_max=window_max,
            )
        except Exception as e:
            logger.warning("[signals.calendar] list_events_for_signals failed: %s", e)
            return []

        upcoming_cutoff = now + timedelta(hours=LOOKAHEAD_HOURS)
        followup_cutoff = now + timedelta(days=FOLLOWUP_WINDOW_DAYS)

        # Index future events by external attendee email for follow-up detection.
        future_attendee_index: dict[str, list[datetime]] = {}
        parsed: list[
            tuple[dict[str, Any], datetime, datetime, list[dict[str, Any]]]
        ] = []
        for ev in events:
            start = _parse_event_time(ev.get("start"))
            end = _parse_event_time(ev.get("end"))
            if start is None:
                continue
            externals = _external_attendees(ev.get("attendees") or [], rep_domain)
            parsed.append((ev, start, end, externals))
            if start > now:
                for att in externals:
                    email = _attendee_email(att)
                    if email:
                        future_attendee_index.setdefault(email, []).append(start)

        signals: list[Signal] = []
        for ev, start, end, externals in parsed:
            if not externals:
                continue
            if _is_internal_sync(ev.get("summary") or "", externals):
                continue

            # Upcoming meeting in next 48h.
            if now <= start <= upcoming_cutoff:
                signals.append(
                    _upcoming_meeting_signal(
                        user_email=rep,
                        event=ev,
                        start=start,
                        end=end,
                        externals=externals,
                        now=now,
                    )
                )
                continue

            # Past meeting in last 7d: emit missed_followup iff no future event
            # with the same external attendee in the next 14d.
            if window_min <= start < now:
                has_followup = False
                for att in externals:
                    email = _attendee_email(att)
                    future_dates = future_attendee_index.get(email, [])
                    if any(now < fd <= followup_cutoff for fd in future_dates):
                        has_followup = True
                        break
                if not has_followup:
                    signals.append(
                        _missed_followup_signal(
                            user_email=rep,
                            event=ev,
                            start=start,
                            end=end,
                            externals=externals,
                        )
                    )

        logger.info(
            "[signals.calendar] user=%s emitted=%d (now=%s)",
            rep,
            len(signals),
            now.isoformat(),
        )
        return signals
