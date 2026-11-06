import datetime
import logging
import re
import uuid
from typing import Any

from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Boilerplate that shows up in auto-generated transcript filenames ("… - Notes
# by Gemini") but carries no meaning for matching the calendar event title.
_TITLE_STOPWORDS = {"notes", "by", "gemini", "meeting", "transcript", "recording"}


def _http_status(exc: Exception) -> int | None:
    """HTTP status carried by a googleapiclient error, or ``None``.

    ``HttpError`` keeps it on ``resp.status``; some transports surface
    ``status_code`` instead. Read defensively rather than importing
    ``HttpError`` and isinstance-ing it — a retry wrapper or a mocked service
    in tests raises something that merely looks like it, and the distinction
    between "404, the series is gone" and "we could not ask" is exactly what
    must not collapse.
    """
    resp = getattr(exc, "resp", None)
    for candidate in (getattr(resp, "status", None), getattr(exc, "status_code", None)):
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _title_tokens(title: str | None) -> set[str]:
    """Normalize a meeting title to a set of comparable tokens.

    Lowercases, splits on non-alphanumerics, drops transcript boilerplate and
    bare numeric/date fragments so "Sales / HubSpot Weekly Open Office" matches
    "Sales / HubSpot Weekly Open Office - 2026/06/26 - Notes by Gemini".
    """
    if not title:
        return set()
    tokens = re.split(r"[^a-z0-9]+", title.lower())
    return {
        t
        for t in tokens
        if t and t not in _TITLE_STOPWORDS and not t.isdigit() and len(t) > 1
    }


def is_working_location_event(event: dict[str, Any]) -> bool:
    """True for Google Calendar "working location" entries (Home/Office markers).

    Google auto-creates these as all-day events (eventType == "workingLocation",
    carrying a workingLocationProperties block) to represent where someone is
    working. They are not meetings or blocks, so we never surface them in the UI,
    briefing, free-slot math, or signal extraction.
    """
    return (
        event.get("eventType") == "workingLocation"
        or "workingLocationProperties" in event
    )


# Google Calendar special `eventType` values that are never a meeting a user
# would "prepare 30 minutes before": focus blocks, out-of-office, birthdays,
# and the working-location markers above. See
# https://developers.google.com/calendar/api/v3/reference/events (eventType).
_NON_MEETING_EVENT_TYPES = frozenset(
    {"focusTime", "outOfOffice", "birthday", "workingLocation"}
)


def is_non_meeting_event(event: dict[str, Any]) -> bool:
    """True for calendar entries that are never a meeting to prepare for.

    Focus time, out-of-office, birthdays and working-location markers are solo
    blocks — real time on the calendar, but nothing to prep for. This is used
    by the AI-Priorities calendar extractor so they stop producing junk
    "Prepare for Focus time…" cards. It is deliberately NOT applied inside
    :meth:`GoogleCalendarClient.get_upcoming_events`: a focus/OOO block is still
    genuine busy time the briefing and free-slot math must continue to see.
    """
    return event.get("eventType") in _NON_MEETING_EVENT_TYPES


# Opaque self-hold titles people type to block their own time — "Busy",
# "Blocked", "Focus", etc. They are real busy time but never a meeting to
# prepare for. Matched EXACTLY against the normalized title (see
# :func:`_normalize_hold_title`) so a real meeting that merely *contains* one
# of these words — "Busy season planning", "Focus group with Acme",
# "Blocked-account remediation" — is left untouched. Google's native Focus
# Time / OOO features set ``eventType`` and are handled by
# :func:`is_non_meeting_event`; this set covers the manually-typed titles that
# carry no ``eventType``.
_PLACEHOLDER_HOLD_TITLES = frozenset(
    {
        "busy",
        "blocked",
        "block",
        "blocked off",
        "blocked time",
        "time blocked",
        "focus",
        "focus time",
        "focused time",
        "focus block",
        "deep focus",
        "deep work",
        "hold",
        "held",
        "do not book",
        "do not schedule",
        "no meetings",
        "ooo",
        "out of office",
        "dnd",
        "reserved",
        "unavailable",
    }
)


def _normalize_hold_title(summary: str | None) -> str:
    """Reduce a title to its lowercase word core for exact hold matching.

    Casefolds, then replaces every run of non-alphanumeric characters (emoji,
    brackets, hyphens, extra spaces) with a single space and trims. So
    "🔴 BLOCKED" -> "blocked", "[Focus Time]" -> "focus time",
    "Do-Not-Book" -> "do not book".
    """
    if not summary:
        return ""
    collapsed = re.sub(r"[^a-z0-9]+", " ", summary.casefold())
    return " ".join(collapsed.split())


def is_placeholder_hold(event: dict[str, Any]) -> bool:
    """True for opaque self-hold entries ("Busy"/"Blocked"/"Focus"/…).

    Events synced from another system (Outlook free/busy), copied from a
    private secondary calendar, or manually created to protect time often land
    on the primary calendar with a bare hold title like ``"Busy"``,
    ``"BLOCKED"`` or ``"Focus"`` and no agenda or attendees. They ARE genuine
    busy time, so — exactly like :func:`is_non_meeting_event` — this is
    deliberately NOT applied inside
    :meth:`GoogleCalendarClient.get_upcoming_events`; the free-slot /
    availability math must keep counting them. It is used by the briefing
    calendar and the AI-Priorities extractor to skip them for *display* /
    *prep*, so a hold block never shows a "Get Ready" button or spawns a junk
    "Prepare for your BLOCKED time…" priority card.
    """
    return _normalize_hold_title(event.get("summary")) in _PLACEHOLDER_HOLD_TITLES


# The reader's own RSVP on an event, normalized from Google's per-attendee
# `responseStatus`. `RSVP_NONE` means the question doesn't apply — a solo event
# with no invitee list, or one the reader organized.
RSVP_ACCEPTED = "accepted"
RSVP_TENTATIVE = "tentative"
RSVP_DECLINED = "declined"
RSVP_NEEDS_ACTION = "needs_action"
RSVP_NONE = "none"

# RSVP states that mean "the reader is going, or the question is moot" — the
# only ones a meeting-prep surface should act on.
RSVP_COMMITTED = frozenset({RSVP_ACCEPTED, RSVP_TENTATIVE, RSVP_NONE})


def self_response_status(event: dict[str, Any], user_email: str) -> str:
    """The reader's own RSVP on ``event`` — one of the ``RSVP_*`` constants.

    Prep surfaces must not nag about invites the reader never accepted: an
    unanswered rollout-call invite is not a commitment, and a declined one is a
    decision already made. Callers gate prep on :data:`RSVP_COMMITTED` and
    surface ``RSVP_NEEDS_ACTION`` as a calendar-hygiene nudge instead.

    Deliberately NOT applied inside
    :meth:`GoogleCalendarClient.get_upcoming_events` — the same reasoning as
    :func:`is_non_meeting_event`. A tentative meeting is still busy time, and
    free-slot math has its own declined handling (see ``_event_busy_interval``).

    Returns :data:`RSVP_NONE` when the RSVP question doesn't apply:

    * No attendee list at all — a solo event the reader put on their own
      calendar ("Draft the board deck"). There is nothing to accept, and these
      must keep prepping.
    * The reader organized it AND has not explicitly answered "No". Google
      usually self-marks the organizer ``accepted``, but events created through
      the API can leave the organizer on ``needsAction`` — dropping someone's
      own meeting over an unanswered RSVP would be a far worse bug than
      prepping one extra.

      An organizer's ``declined``, though, is a real answer and is honoured.
      Google lets you RSVP "No" to a meeting you own, and a manager who does
      that to their own recurring L10 has decided they are not going. Reading
      that as "meaningless" is what kept Cubie nagging an owner to prep for a
      meeting they had already stood down from. Only
      an EXPLICIT ``declined`` crosses this line; ``needsAction`` on your own
      event still means committed, which is the case the paragraph above
      protects.
    """
    me = (user_email or "").strip().lower()
    attendees = event.get("attendees") or []

    self_entry: dict | None = None
    for a in attendees:
        if not isinstance(a, dict):
            continue
        # Prefer Google's own `self` marker; fall back to an email match so a
        # hand-built event dict (tests, the briefing's own payloads) still works.
        if a.get("self") or (a.get("email") or "").strip().lower() == me:
            self_entry = a
            break

    if self_entry is None:
        return RSVP_NONE

    declined = (self_entry.get("responseStatus") or "").strip().lower() == "declined"

    # The organizer's RSVP is meaningless — with one exception, an explicit
    # "No" (see the docstring). Everything else on your own event is noise.
    if self_entry.get("organizer"):
        return RSVP_DECLINED if declined else RSVP_NONE
    organizer = event.get("organizer") or {}
    if isinstance(organizer, dict) and (
        organizer.get("self") or (organizer.get("email") or "").strip().lower() == me
    ):
        return RSVP_DECLINED if declined else RSVP_NONE

    raw = (self_entry.get("responseStatus") or "").strip().lower()
    if raw == "accepted":
        return RSVP_ACCEPTED
    if raw == "tentative":
        return RSVP_TENTATIVE
    if raw == "declined":
        return RSVP_DECLINED
    if raw == "needsaction":
        return RSVP_NEEDS_ACTION
    # Unknown/absent status — treat as committed rather than silently hiding a
    # real meeting. Google always sends one of the four above.
    return RSVP_NONE


class GoogleCalendarClient:
    def __init__(self, user_email: str):
        """
        Initializes the Google Calendar API client for a specific user.
        Always uses the authenticated user's email via domain-wide delegation.
        """
        self.user_email = user_email
        # Distinguish "the calendar is genuinely empty" from "we could not read
        # the calendar." Callers that surface results to a user/LLM MUST check
        # these before reporting "no events" — an empty list plus a non-None
        # error means an OUTAGE, not an empty calendar.
        self.init_error: str | None = None
        self.last_error: str | None = None
        self.service = self._build_service()

    def _build_service(self):
        """Builds the Calendar API service using domain-wide delegation.

        On failure returns ``None`` and records the reason in ``self.init_error``
        so callers can tell an auth/config outage apart from an empty calendar.
        """
        from rubik_core.tools._workspace_auth import google_workspace_configured

        if not google_workspace_configured():
            logger.error("Workspace DWD credentials are not configured.")
            self.init_error = "calendar credentials are not configured"
            return None

        try:
            from rubik_core.tools._workspace_auth import (
                load_workspace_credentials,
                safe_refresh,
            )

            creds = load_workspace_credentials(SCOPES)
            if creds is None:
                self.init_error = "workspace credentials are unavailable"
                return None

            # Domain-wide delegation: impersonate the user
            delegated_creds = creds.with_subject(self.user_email)
            # UC-40.2: serialize OAuth refresh — concurrent refreshes across
            # parallel channel agents race on SSL state and produce SSLEOFError.
            safe_refresh(delegated_creds)
            return build(
                "calendar", "v3", credentials=delegated_creds, cache_discovery=False
            )
        except Exception as e:
            logger.error(
                f"Failed to initialize Google Calendar client for {self.user_email}: {e}"
            )
            self.init_error = f"calendar authentication failed: {e}"
            return None

    def get_upcoming_events(
        self,
        max_results: int = 10,
        time_min=None,
        time_zone: str | None = None,
        time_max=None,
    ) -> list[dict[str, Any]]:
        """Fetches upcoming events for the user's primary calendar.

        ``time_zone`` (an IANA name, e.g. ``"America/Los_Angeles"``) is passed
        to the Google API so ``start.dateTime`` comes back in the reader's
        offset. Callers that render a human clock time should still normalize
        deterministically (see :func:`format_when_local`) — this only aligns
        the raw offset, it is not a substitute for reader-tz formatting.

        ``time_max`` (optional, same string/datetime forms as ``time_min``)
        bounds the fetch by a *date horizon* instead of only the ``max_results``
        event count. Without it, a packed calendar exhausts ``max_results``
        within a few days while a sparse one spans weeks — so two users see
        different look-aheads from the same call. Pass ``time_max`` (and a
        ``max_results`` large enough to cover it) for a horizon that's the same
        for everyone regardless of how busy the calendar is.
        """
        if not self.service:
            return []

        def _norm_rfc3339(value) -> str:
            """Normalize a datetime/ISO-string to an RFC3339 string with a zone."""
            if isinstance(value, str):
                # Accept ISO strings directly, keep timezone offsets if they exist
                if value.endswith("Z") or "+" in value[-6:] or "-" in value[-6:]:
                    return value
                return value + "Z"
            return value.isoformat() + "Z"

        if time_min is None:
            time_min_str = datetime.datetime.utcnow().isoformat() + "Z"
        else:
            time_min_str = _norm_rfc3339(time_min)
        time_max_str = _norm_rfc3339(time_max) if time_max is not None else None

        try:
            logger.info(f"Fetching calendar events for {self.user_email}")
            list_kwargs: dict[str, Any] = {
                "calendarId": "primary",
                "timeMin": time_min_str,
                "maxResults": max_results,
                "singleEvents": True,
                "orderBy": "startTime",
            }
            if time_max_str:
                list_kwargs["timeMax"] = time_max_str
            if time_zone:
                list_kwargs["timeZone"] = time_zone
            events_result = self.service.events().list(**list_kwargs).execute()
            events = events_result.get("items", [])
            return [e for e in events if not is_working_location_event(e)]
        except Exception as e:
            logger.error(f"Error fetching calendar events for {self.user_email}: {e}")
            self.last_error = f"calendar read failed: {e}"
            return []

    def get_calendar_timezone(self, default: str = "UTC") -> str:
        """Return the IANA timezone of the user's primary calendar.

        e.g. ``"America/Los_Angeles"``. Free-slot / availability math is only
        correct when the 9am-6pm work window is built in the user's own
        timezone rather than UTC, so callers resolve this first. Falls back to
        ``default`` if the calendar can't be reached.
        """
        if not self.service:
            return default
        try:
            cal = self.service.calendars().get(calendarId="primary").execute()
            return cal.get("timeZone") or default
        except Exception as e:
            logger.warning(
                f"Could not fetch calendar timezone for {self.user_email}: {e}"
            )
            return default

    def get_event(self, event_id: str) -> dict[str, Any]:
        """Fetches a single calendar event by ID with all fields."""
        if not self.service:
            return {}
        try:
            return (
                self.service.events()
                .get(
                    calendarId="primary",
                    eventId=event_id,
                )
                .execute()
            )
        except Exception as e:
            logger.error(f"Error fetching event {event_id} for {self.user_email}: {e}")
            return {}

    #: Sentinel returned by :meth:`list_event_instances` when Google says the
    #: series is gone (404 Not Found / 410 Gone / every instance cancelled).
    #: Deliberately distinct from ``[]`` — see the method docstring.
    SERIES_GONE = "series_gone"

    def list_event_instances(
        self,
        series_id: str,
        *,
        time_min,
        time_max=None,
        max_results: int = 10,
    ) -> list[dict[str, Any]] | str | None:
        """Real, dated occurrences of a recurring event — Google's own answer.

        This is the antidote to inferring a schedule from a weekly PATTERN. A
        stored ``(day_of_week, start_time)`` says a meeting recurs forever; only
        Google knows whether next Wednesday's occurrence still exists, moved, or
        was cancelled. Cancelled instances are excluded (``showDeleted`` is left
        at its default false), so what comes back is what is actually going to
        happen.

        Three outcomes, and the difference between them is load-bearing:

        * ``list``  — the surviving occurrences in the window, soonest first.
          An EMPTY list means "the series exists, nothing left in this window."
        * :data:`SERIES_GONE` — Google says the event is not there (404) or was
          deleted (410). The series itself is cancelled.
        * ``None``  — we could not ask (no service, a transient API error).
          Callers MUST fail open on this: a Google blip must never be rendered
          as "your L10 was cancelled."

        A NON-recurring event has no instances; Google answers 400 for those, so
        we fall back to reading the single event and returning it (or
        :data:`SERIES_GONE` when its own ``status`` is ``cancelled``). That
        keeps one-off imported L10s on the same code path as recurring ones.
        """
        if not self.service or not series_id:
            return None

        def _rfc3339(value) -> str:
            if isinstance(value, str):
                if value.endswith("Z") or "+" in value[-6:] or "-" in value[-6:]:
                    return value
                return value + "Z"
            return (
                value.isoformat() + "Z" if value.tzinfo is None else value.isoformat()
            )

        kwargs: dict[str, Any] = {
            "calendarId": "primary",
            "eventId": series_id,
            "timeMin": _rfc3339(time_min),
            "maxResults": max(1, max_results),
        }
        if time_max is not None:
            kwargs["timeMax"] = _rfc3339(time_max)

        try:
            result = self.service.events().instances(**kwargs).execute()
            items = result.get("items", []) or []
            return [i for i in items if (i.get("status") or "") != "cancelled"]
        except Exception as e:  # googleapiclient raises HttpError; stay generic
            status_code = _http_status(e)
            if status_code in (404, 410):
                logger.info(
                    "Calendar series %s is gone for %s (HTTP %s)",
                    series_id,
                    self.user_email,
                    status_code,
                )
                return self.SERIES_GONE
            if status_code == 400:
                # Not a recurring event — read it directly instead.
                single = self.get_event(series_id)
                if not single or not single.get("id"):
                    return self.SERIES_GONE
                if (single.get("status") or "") == "cancelled":
                    return self.SERIES_GONE
                return [single]
            logger.warning(
                "Could not list instances of %s for %s: %s",
                series_id,
                self.user_email,
                e,
            )
            self.last_error = f"calendar instances read failed: {e}"
            return None

    def find_meeting_event(
        self, title_hint: str | None, around_time, window_hours: int = 12
    ) -> dict[str, Any] | None:
        """Best-effort: the calendar event that best matches ``title_hint`` near
        ``around_time`` — the source of truth for a transcript's meeting.

        ``around_time`` is when the transcript doc was created (Gemini writes it
        within minutes of the meeting ending), so we search a window that starts
        ``window_hours`` before it and ends a little after. Among the events in
        that window we pick the one whose title matches ``title_hint``; with no
        usable hint, the latest event that started at/before the anchor — i.e.
        the meeting that just ended. Returns the event dict, or ``None`` on any
        error or if nothing confidently matches (fail-closed).
        """
        if not self.service or around_time is None:
            return None

        def _rfc3339(dt) -> str:
            return dt.isoformat() if dt.tzinfo else dt.isoformat() + "Z"

        try:
            anchor = around_time
            time_min = anchor - datetime.timedelta(hours=max(1, window_hours))
            time_max = anchor + datetime.timedelta(hours=2)
            events_result = (
                self.service.events()
                .list(
                    calendarId="primary",
                    timeMin=_rfc3339(time_min),
                    timeMax=_rfc3339(time_max),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=25,
                )
                .execute()
            )
            events = [
                e
                for e in events_result.get("items", [])
                if not is_working_location_event(e)
            ]
            if not events:
                return None

            hint_tokens = _title_tokens(title_hint)

            def _matches(ev) -> bool:
                if not hint_tokens:
                    return False
                ev_tokens = _title_tokens(ev.get("summary"))
                if not ev_tokens:
                    return False
                overlap = len(hint_tokens & ev_tokens)
                # Either side fully contained, or a solid majority of the
                # (smaller) token set overlaps — tolerant of " - Notes by
                # Gemini"/date noise without matching unrelated meetings.
                return (
                    hint_tokens <= ev_tokens
                    or ev_tokens <= hint_tokens
                    or overlap >= max(1, min(len(hint_tokens), len(ev_tokens)) * 0.6)
                )

            matched = [e for e in events if _matches(e)]
            if matched:
                # Latest-starting matching event (best instance of a recurring series).
                return matched[-1]
            if not hint_tokens:
                # No hint at all → fall back to the meeting that just ended.
                return events[-1]
            return None
        except Exception as e:
            logger.warning(
                f"find_meeting_event failed for {self.user_email} "
                f"(title={title_hint!r}): {e}"
            )
            return None

    def find_meeting_attendee_emails(
        self, title_hint: str | None, around_time, window_hours: int = 12
    ) -> list[str]:
        """Emails of everyone on the invite for the best-matching meeting.

        The *fallback* attendee source for a transcript's ACL when actual Meet
        attendance can't be read (a calendar invite over-grants — it includes
        no-shows — so prefer :class:`GoogleMeetClient` attendance when available).
        Returns lowercased, de-duplicated emails; ``[]`` when nothing confidently
        matches (fail-closed: never widen access on a bad guess).
        """
        event = self.find_meeting_event(title_hint, around_time, window_hours)
        if not event:
            return []
        emails: set[str] = set()
        for key in ("organizer", "creator"):
            who = event.get(key) or {}
            if who.get("email"):
                emails.add(who["email"].lower())
        for att in event.get("attendees", []) or []:
            if att.get("resource"):
                continue  # rooms / equipment, not people
            if att.get("email"):
                emails.add(att["email"].lower())
        return sorted(e for e in emails if "@" in e)

    @staticmethod
    def extract_meeting_code(event: dict[str, Any] | None) -> str | None:
        """The Google Meet meeting code (e.g. ``"abc-defg-hij"``) for an event.

        Prefers the ``meet.google.com/<code>`` link (guaranteed a Meet code),
        then falls back to ``conferenceData.conferenceId``. ``None`` when the
        event has no Google Meet conference (e.g. a Zoom/Teams event) — the caller
        then falls back to the invite list.
        """
        if not event:
            return None
        conf = event.get("conferenceData") or {}
        link = event.get("hangoutLink")
        if not link:
            for ep in conf.get("entryPoints", []) or []:
                uri = ep.get("uri") or ""
                if ep.get("entryPointType") == "video" and "meet.google.com" in uri:
                    link = uri
                    break
        if link and "meet.google.com/" in link:
            seg = link.rstrip("/").split("/")[-1].split("?")[0]
            if seg:
                return seg
        return conf.get("conferenceId") or None

    def get_availability(
        self, start_time, end_time, emails: list[str] = None
    ) -> dict[str, Any]:
        """Checks free/busy status for requested users within a timeframe.

        Accepts either datetime objects or ISO 8601 strings.
        If emails is not provided, checks only the authenticated user.
        """
        if not self.service:
            return {"error": "Calendar service not initialized"}

        def _to_rfc3339(t) -> str:
            if isinstance(t, str):
                if t.endswith("Z") or "+" in t[-6:] or "-" in t[-6:]:
                    return t
                return t + "Z"
            return t.isoformat() + "Z"

        emails_to_check = emails if emails else [self.user_email]

        try:
            body = {
                "timeMin": _to_rfc3339(start_time),
                "timeMax": _to_rfc3339(end_time),
                "items": [{"id": email} for email in emails_to_check],
            }
            logger.info(f"Fetching calendar availability for {emails_to_check}")
            events_result = self.service.freebusy().query(body=body).execute()
            calendars = events_result.get("calendars", {})

            report = {}
            has_busy = False
            for email in emails_to_check:
                user_cal = calendars.get(email, {})
                busy_slots = user_cal.get("busy", [])
                report[email] = busy_slots
                if busy_slots:
                    has_busy = True

            return {"is_busy": has_busy, "busy_slots": report}
        except Exception as e:
            logger.error(
                f"Error fetching calendar availability for {self.user_email}: {e}"
            )
            return {"error": str(e)}

    def create_event(
        self,
        summary: str,
        start_time: str,
        end_time: str,
        description: str = "",
        attendees: list[str] = None,
    ) -> dict[str, Any]:
        """Creates a new event on the user's primary calendar."""
        if not self.service:
            return {"error": "Calendar service not initialized"}

        def _to_rfc3339(t) -> str:
            if isinstance(t, str):
                if t.endswith("Z") or "+" in t[-6:] or "-" in t[-6:]:
                    return t
                return t + "Z"
            return t.isoformat() + "Z"

        try:
            event_body = {
                "summary": summary,
                "description": description,
                "start": {"dateTime": _to_rfc3339(start_time)},
                "end": {"dateTime": _to_rfc3339(end_time)},
                "conferenceData": {
                    "createRequest": {
                        "requestId": str(uuid.uuid4()),
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                },
            }
            if attendees:
                event_body["attendees"] = [{"email": email} for email in attendees]

            logger.info(f"Creating calendar event for {self.user_email}: {summary}")
            event = (
                self.service.events()
                .insert(
                    calendarId="primary",
                    body=event_body,
                    sendUpdates="all" if attendees else "none",
                    conferenceDataVersion=1,
                )
                .execute()
            )

            return event
        except Exception as e:
            logger.error(f"Error creating calendar event for {self.user_email}: {e}")
            return {"error": str(e)}
