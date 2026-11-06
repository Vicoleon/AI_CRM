"""Is this L10 still on the calendar, and when is it *actually* next?

An ``eos_l10_meetings`` row is a photograph of a Google Calendar event taken
once, at import. It stores a recurring PATTERN — ``day_of_week`` + ``HH:MM`` —
and every surface that wants to say "your next L10 is Wednesday" has been
synthesising that date by rolling the pattern forward. The pattern never ends,
so the date never stops: cancel the calendar event and Cubie keeps promising a
meeting that is not going to happen (an L10 owner cancelled two L10s on
their calendar and EOS kept showing the next date: "Shouldn't it update based
on my calendar?").

It should. This module is the one place that asks Google instead of guessing,
and the three states it can return are all load-bearing:

``ACTIVE``
    The series is on the calendar. ``next_start`` is a real occurrence Google
    listed — already correct for a moved meeting, a skipped week, or a changed
    time, none of which the pattern can express.
``CANCELLED``
    Google says the event is gone (deleted series) or that nothing survives in
    the look-ahead window. This is what stops the phantom date.
``UNKNOWN``
    We could not ask — no Workspace credentials, an API error, or a meeting
    that was never imported from a calendar at all.

**UNKNOWN must never be rendered as CANCELLED.** A Google outage would
otherwise tell every team in the company that their L10 was called off, which
is a far worse failure than one stale date. Callers fall back to the stored
pattern on UNKNOWN, and :func:`apply_to_meeting` refuses to persist it.

**Whose calendar counts.** The series lives on the ORGANIZER's calendar, so
only the meeting's own manager may write the answer back to the shared row
(:func:`may_persist`). Anyone else gets a correct read for their own view and
changes nothing: an attendee who deletes their personal copy has not cancelled
the team's meeting, and must not be able to blank the card for everyone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: The series is on the calendar and has at least one surviving occurrence.
STATE_ACTIVE = "active"
#: Google says the series (or every occurrence in the window) is cancelled.
STATE_CANCELLED = "cancelled"
#: We could not ask. Never persisted, never shown as a cancellation.
STATE_UNKNOWN = "unknown"

#: How far ahead we look for a surviving occurrence. Wide enough to survive a
#: team skipping a couple of weeks (holidays, an offsite) without declaring the
#: meeting dead, narrow enough that a genuinely abandoned series is recognised
#: inside a month rather than never.
LOOKAHEAD_DAYS = 35

#: Occurrences to pull per series. We only need the first surviving one; the
#: slack covers a reader who has declined the next few.
MAX_INSTANCES = 10

# Why a series came back CANCELLED. The gating is identical either way — there
# is no next meeting — but the wording a user reads should not be, so the two
# travel as a STABLE KIND plus human prose. The client branches on the KIND;
# rewording the prose must never change what a card says.
#: The calendar event itself is gone (deleted series, or a 404/410 from Google).
KIND_EVENT_DELETED = "event_deleted"
REASON_EVENT_DELETED = "the calendar event was cancelled"
#: The event still exists; it simply has no occurrence inside the look-ahead.
KIND_NOTHING_SCHEDULED = "nothing_scheduled"
REASON_NOTHING_SCHEDULED = (
    f"no occurrence is scheduled in the next {LOOKAHEAD_DAYS} days"
)


@dataclass(frozen=True)
class SeriesState:
    """What Google says about one imported L10 series, for one reader."""

    state: str
    #: Start of the next surviving occurrence, tz-aware UTC. ``None`` whenever
    #: the state is not ACTIVE.
    next_start: datetime | None = None
    #: Why the answer is what it is, in words a person can read: on UNKNOWN,
    #: what stopped us asking; on CANCELLED, which kind of cancellation. Live
    #: only — the row stores the STATE, not the prose.
    reason: str | None = None
    #: Stable machine-readable companion to ``reason`` on CANCELLED
    #: (:data:`KIND_EVENT_DELETED` / :data:`KIND_NOTHING_SCHEDULED`). Clients
    #: branch on THIS, never on the prose.
    kind: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return self.state == STATE_CANCELLED

    @property
    def is_known(self) -> bool:
        return self.state in (STATE_ACTIVE, STATE_CANCELLED)


def _parse_start(instance: dict, key: str = "start") -> datetime | None:
    """Tz-aware UTC start (or ``key="end"``: end) of one Google instance.

    All-day occurrences carry a bare ``date``; treat those as midnight UTC so
    an all-day L10 (rare, but teams do it for planning days) still orders
    against timed ones.
    """
    start = instance.get(key) or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_series_state(
    client,
    series_id: str | None,
    *,
    reader_email: str,
    now: datetime | None = None,
    lookahead_days: int = LOOKAHEAD_DAYS,
) -> SeriesState:
    """Ask Google what is actually left of ``series_id``.

    ``client`` is a :class:`~rubik_core.tools.calendar.GoogleCalendarClient`
    already bound to ``reader_email``. Occurrences the reader has DECLINED are
    skipped when picking ``next_start`` — "your next L10" means the next one
    you are going to, not the next one on the wall — but a fully-declined
    window is still ACTIVE, because declining is not cancelling and only the
    organizer's answer may retire the team's meeting.
    """
    if not series_id:
        return SeriesState(STATE_UNKNOWN, reason="meeting is not linked to a calendar")
    if client is None or not getattr(client, "service", None):
        return SeriesState(
            STATE_UNKNOWN,
            reason=getattr(client, "init_error", None) or "calendar is unavailable",
        )

    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=max(1, lookahead_days))

    instances = client.list_event_instances(
        series_id,
        time_min=now,
        time_max=horizon,
        max_results=MAX_INSTANCES,
    )

    if instances is None:
        return SeriesState(
            STATE_UNKNOWN,
            reason=getattr(client, "last_error", None) or "calendar read failed",
        )
    if instances == getattr(client, "SERIES_GONE", "series_gone"):
        return SeriesState(
            STATE_CANCELLED, reason=REASON_EVENT_DELETED, kind=KIND_EVENT_DELETED
        )
    if not instances:
        # The series exists but nothing survives in the window — every
        # occurrence between here and the horizon was cancelled one by one,
        # or the recurrence simply ended. Either way there is no next meeting,
        # and every consequence of CANCELLED is the right one; the two are told
        # apart by ``reason`` so the UI can word it honestly rather than
        # accusing a team of cancelling a meeting they only paused.
        return SeriesState(
            STATE_CANCELLED,
            reason=REASON_NOTHING_SCHEDULED,
            kind=KIND_NOTHING_SCHEDULED,
        )

    # An occurrence that has started but not yet ended is still "next": the
    # team is in it right now, and rolling the card to next week mid-meeting
    # hides today's L10 (seen 8 minutes into an 11:00 meeting).
    starts = sorted(
        s
        for s, e in (
            (_parse_start(i), _parse_start(i, "end"))
            for i in instances
            if not _declined(i, reader_email)
        )
        if s is not None and (s >= now or (e is not None and e > now))
    )
    if not starts:
        # Present but all declined / unparseable: the meeting is still real,
        # we just have no date to promise.
        return SeriesState(STATE_ACTIVE)
    return SeriesState(STATE_ACTIVE, next_start=starts[0])


def _declined(instance: dict, reader_email: str) -> bool:
    from rubik_core.tools.calendar import RSVP_DECLINED, self_response_status

    try:
        return self_response_status(instance, reader_email) == RSVP_DECLINED
    except Exception:  # noqa: BLE001 — a malformed attendee list is not a decline
        return False


def may_persist(meeting, reader_email: str, manager_email: str | None) -> bool:
    """True when ``reader_email`` speaks for this meeting's calendar series.

    Only the manager (who is the calendar organizer for an imported L10 — the
    import resolves ``manager_id`` from ``organizer.email``) may write the
    reconciled state onto the shared row. See the module docstring: an
    attendee's personal calendar is not the team's schedule.
    """
    reader = (reader_email or "").strip().lower()
    if not reader:
        return False
    if manager_email and reader == manager_email.strip().lower():
        return True
    # A meeting whose roster is a single person is that person's own slot.
    roster = [
        e.strip().lower() for e in (getattr(meeting, "roster_emails", None) or [])
    ]
    return roster == [reader]


def apply_to_meeting(
    meeting,
    state: SeriesState,
    *,
    now: datetime | None = None,
    set_next: bool = True,
) -> bool:
    """Write a KNOWN state onto the row. Returns True when anything changed.

    Refuses UNKNOWN outright — persisting "we could not ask" would turn a
    transient Google error into a durable cancellation, which is the one
    outcome this whole module exists to prevent.

    ``set_next=False`` records the STATE without touching
    ``next_occurrence_at``. The calendar-import path uses it: reading the event
    proves the series is alive, but the occurrence the importer happened to
    click is not necessarily the next one (Sync passes the series master, whose
    start is the series' first-ever date). A cancellation always clears the
    date regardless — a meeting that is not happening has no next occurrence.
    """
    if not state.is_known:
        return False
    now = now or datetime.now(timezone.utc)
    next_naive = (
        state.next_start.astimezone(timezone.utc).replace(tzinfo=None)
        if state.next_start
        else None
    )
    write_next = set_next or state.is_cancelled
    changed = meeting.calendar_state != state.state or (
        write_next and meeting.next_occurrence_at != next_naive
    )
    meeting.calendar_state = state.state
    if write_next:
        meeting.next_occurrence_at = next_naive
    meeting.calendar_checked_at = now.replace(tzinfo=None)
    return changed


def is_calendar_cancelled(meeting) -> bool:
    """True when a meeting's LAST reconciled answer was "cancelled".

    The read side of the persisted state, for the surfaces that cannot afford
    a Google round-trip (the PTO handoff brief, the next-L10 nudge). A meeting
    never reconciled reads as not-cancelled, so nothing regresses for teams
    whose L10 was scheduled by hand.
    """
    return (getattr(meeting, "calendar_state", None) or "") == STATE_CANCELLED
