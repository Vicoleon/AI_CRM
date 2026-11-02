"""Today's client calls, off the AE's own calendar — the whole read path.

This is Phase 2 of Sales Call Capture (``reconcile_calendar_meetings``) and it
replaces "create a Meet space from Cubie" as the entry point. The AE opens the
Calls screen, sees the meetings already on their calendar, and each row arrives
pre-bound to a HubSpot company.

**What this screen is actually for.** Its PRIMARY job is to bind a meeting to
the right HubSpot company up front and hand the AE the calendar's own Meet link.
Arming transcription is SECONDARY and best-effort, because three facts make
"arm everything" impossible:

* a conference already in progress cannot be armed;
* a client-hosted Meet space lives in another Workspace tenant and cannot be
  patched at all;
* a recurring series reuses ONE space (``tools/meet.py``), so arming a weekly
  client call arms every future occurrence.

A row whose arming fails is therefore still valuable — the company binding is
what lets the wrap-up write to the right CRM record without asking. So: **never
let an unarmable row look broken, and never let it look armed.** Every verdict
in every row is computed here, server-side, and carries a reason. Nothing
defaults to "fine".

**Never raises, and an outage is never an empty day.** ``scan_day`` returns
``calendar_available: False`` with an empty list when the Calendar client cannot
be built or the read fails, and a company block with ``match="error"`` when
HubSpot could not be asked. A 500 here blanks the AE's whole day; "we could not
check" and "there is nothing" are different sentences and this module never
collapses them.

**Two external calls for the whole page.** One ``events.list`` for the day, then
ONE batched ``companies_for_domains`` across every distinct matchable domain the
day contains (with live positive matches and a short negative cache), plus one indexed DB read
for existing sessions. Not one lookup per row.

**The script block is additive and fail-soft.** Every row
carries ``scripts``: the products this AE can pick for the call, the ones they
picked for this client last time, and the checklist of facts each of those
products needs the call to surface. The envelope carries ``script_catalogue``
beside them — the checklist for EVERY product on offer, once for the whole day —
because the AE can tick a product nothing remembers (every product, on a first
call with a client) and its checklist has to render the moment they do. It is
all attached HERE rather than by ``POST /start`` because start fires when the AE
clicks CAPTURE, which is join time — a checklist handed back from there could
not render *before* the call connects, which is the whole acceptance criterion.
Two more constant-cost reads pay for it (the live script catalogue, and this
AE's remembered picks across the day's bound companies), never one per row, and
any failure in either yields ``scripts: null`` on the rows and
``script_catalogue: null`` on the envelope with the day still rendering: a
checklist is worth a great deal, and a blank day is worth less than nothing
because the AE believes it.

**Purity where it counts.** ``build_rows`` does no I/O: it takes already-fetched
events, already-resolved domain verdicts and an already-loaded session map. That
is what makes the router thin and the tests real — every filter, every verdict
and every clock string is unit-testable without a Google or HubSpot credential.
``scan_day`` is the only function here that touches the network or the database.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from rubik_core.brand import PRODUCT_NAME
from rubik_core.call_capture.domain_match import (
    _is_pseudo_domain,
    clear_match_cache,
    external_domains_for_event,
    own_domains,
    resolve_domain_matches,
)

# ``_ordered_keys`` is imported rather than reimplemented for the same reason
# ``_is_pseudo_domain`` above is: a reader that folded product keys DIFFERENTLY
# from the writer would not raise, it would quietly stop matching rows a human
# can see in the table. One fold, one owner.
from rubik_core.call_capture.scripts import _ordered_keys, script_payload
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession
from rubik_core.database.call_scripts import CallScript, CompanyProductDefault
from rubik_core.graph.keys import normalize_domain, normalize_email
from rubik_core.priorities.temporal import (
    TemporalContext,
    format_when_local,
    resolve_tz,
    resolve_user_now,
)
from rubik_core.retrieval.hubspot_api import CompanyMatch, DomainMatch
from rubik_core.tools.calendar import (
    RSVP_DECLINED,
    GoogleCalendarClient,
    is_non_meeting_event,
    is_placeholder_hold,
    self_response_status,
)

logger = logging.getLogger(__name__)


# ── The closed vocabularies the UI renders ────────────────────────────────────
# Every one of these is a *rendered* verdict, which is why they are frozensets
# and not comments: a typo'd state does not fail loudly, it renders as an empty
# badge and the AE reads "nothing is wrong". Tests assert membership.

#: Does a capture already exist for this calendar occurrence?
#: ``session_exists`` deliberately covers ``scheduled``/``ended``/
#: ``transcript_ready``/``extracted``/``no_transcript``/``failed`` — from the
#: calendar screen's point of view they are all "a row exists, the wrap-up path
#: owns the detail". Only ``wrapped`` is done, only ``in_progress`` is now.
CAPTURE_STATES = frozenset({"not_started", "session_exists", "in_progress", "wrapped"})

#: Three states, because "we have not tried yet" is not "it cannot be done".
ARMING_STATES = frozenset({"armed", "not_armable", "unknown"})

#: Why. ``armed_by_cubie``/``already_on`` are produced by the *write* path
#: (``POST /api/calls/start`` → ``arm_space_transcription``) and reach this
#: module only when it reads a session's persisted verdict back out of
#: ``attrs["capture_arming"]``; ``arming_unsupported`` likewise (the tenant-wide
#: edition/licence answer), and ``google_error`` (Google answered the write path
#: with an error no named reason covers). The rest are computed here for free,
#: with no Meet call at all.
ARMING_REASONS = frozenset(
    {
        "armed_by_cubie",
        "already_on",
        "external_host",
        "not_owner",
        "already_started",
        "recurring_series",
        "arming_unsupported",
        "write_disabled",
        "not_checked_yet",
        "lookup_failed",
        "google_error",
    }
)

#: ``error`` reads "we could not ask HubSpot", NEVER "not in HubSpot", and
#: ``skipped`` reads "there was nothing safe to look up" (a personal address, a
#: role mailbox on no company domain, a Google pseudo-domain). Collapsing any of
#: these into ``not_found`` is how an AE gets told a live client is not a
#: customer.
COMPANY_MATCH_STATES = frozenset(
    {"unique", "ambiguous", "not_found", "skipped", "error"}
)

# Only these hosts are Google Meet. Checked against the parsed hostname, never
# as a substring: ``https://meet.google.com.evil.example/x`` contains the string
# "meet.google.com" and is not Google Meet, and a Zoom link that merely mentions
# it in a query parameter is not either.
_MEET_HOSTS = frozenset({"meet.google.com"})

# A meeting code is one path segment. Multi-segment Meet paths (``lookup/<hash>``
# for a nicknamed meeting, ``tel/...`` for dial-in) are NOT codes, and passing
# one to ``spaces.get`` would 404 with a reason that lies about the cause.
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_CODE_MIN_LEN = 6
_CODE_MAX_LEN = 64

# Ceiling on one day's fetch. A day cannot hold 250 events; this exists so a
# calendar with a pathological recurring rule costs one bounded page.
_MAX_DAY_EVENTS = 250

# Sentinel for "the attribute is not there at all", which is not the same as
# "the attribute is None" — see ``scan_day``.
_MISSING = object()


def clear_cache() -> None:
    """Drop the cached HubSpot domain verdicts (tests; and after a manual bind).

    There is exactly ONE such cache and it lives in
    :mod:`rubik_core.call_capture.domain_match`, next to the blocklist gating
    and the retry that fill it. This function is the alias the router and the
    tests call so the caching detail stays where the lookups are — a second TTL
    dict in this module would be the same mistake as a fifth blocklist: two
    caches that answer the same question and drift.

    Call it after a human binds a company by hand, so the next render asks
    HubSpot again instead of replaying a 15-minute-old "no company found".
    """
    clear_match_cache()


# ── Google Meet link parsing ─────────────────────────────────────────────────


def meet_uri_for_event(event: dict) -> str | None:
    """The event's Google Meet URI, or ``None`` — Meet hosts ONLY.

    Prefers ``hangoutLink``, then the ``conferenceData.entryPoints[]`` entry
    whose ``entryPointType == "video"``.

    Deliberately NOT ``GoogleCalendarClient.extract_meeting_code``: that helper
    ends with an unconditional ``conferenceData.conferenceId`` fallback, so a
    Zoom or Teams event yields *Zoom's* conference id (behaviour pinned by
    ``backend/tests/test_meet_transcripts.py`` and correct for its own callers,
    which is why it is left alone). Here that fallback would put a non-Meet
    event on a screen whose Start button arms a Meet space, so this gates on the
    URI's parsed host and derives everything from that URI alone.
    """
    if not isinstance(event, dict):
        return None
    candidates: list[Any] = [event.get("hangoutLink")]
    conference = event.get("conferenceData") or {}
    if isinstance(conference, dict):
        for entry in conference.get("entryPoints") or []:
            if isinstance(entry, dict) and entry.get("entryPointType") == "video":
                candidates.append(entry.get("uri"))
    for candidate in candidates:
        uri = _meet_uri(candidate)
        if uri:
            return uri
    return None


def meeting_code_from_uri(uri: str | None) -> str | None:
    """``"https://meet.google.com/abc-defg-hij?hs=1"`` → ``"abc-defg-hij"``.

    ``None`` for a non-Meet host, for a multi-segment Meet path (``lookup/…``,
    ``tel/…`` — those are not meeting codes), and for a segment that cannot be
    one. A ``None`` here is not a broken row: the AE still gets the company
    binding and the join link, and the arming verdict says ``lookup_failed``
    with the URI in its detail. Handing ``spaces.get`` a ``lookup`` hash instead
    would 404 and the row would blame the wrong thing.
    """
    normalized = _meet_uri(uri)
    if not normalized:
        return None
    path = urlparse(normalized).path or ""
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) != 1:
        return None
    return normalize_meeting_code(segments[0])


def normalize_meeting_code(value: str | None) -> str | None:
    """A bare meeting code, folded, or ``None`` when it cannot be one.

    The one rule for what a code looks like, shared by the URI parser above and
    by the Meet add-on, which receives its code from Meet itself
    (``getMeetingInfo().meetingCode``). The two MUST fold identically: the
    add-on finds "this call" by comparing its code against the ``meeting_code``
    on today's calendar rows, and a casing or whitespace mismatch does not
    raise — it reports a real client call as "not one of your calls".
    """
    code = (value or "").strip().lower()
    if not _CODE_MIN_LEN <= len(code) <= _CODE_MAX_LEN:
        return None
    if not set(code) <= _CODE_CHARS:
        return None
    return code


def _meet_uri(value: Any) -> str | None:
    """``value`` as an absolute Meet URI, or ``None``. Host-parsed, not substring.

    Returns the scheme-bearing form, which matters twice: the row hands this
    string to the frontend as the join link (a scheme-less ``href`` resolves
    against Cubie's own origin and 404s), and :func:`meeting_code_from_uri`
    re-parses it — a scheme-less value would put the host in ``path`` and the
    code would look like a second path segment, i.e. no code at all.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # A bare "meet.google.com/abc-defg-hij" has no scheme, so urlparse would
    # read the whole thing as a path and find no host at all.
    candidate = raw if "//" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _MEET_HOSTS:
        return None
    return candidate


# ── The day window ───────────────────────────────────────────────────────────


def day_window_utc(tz_name: str, local_date: date) -> tuple[str, str]:
    """RFC3339 ``(timeMin, timeMax)`` for one LOCAL day, as ``"…Z"`` strings.

    The boundaries are built **aware in the reader's timezone and then
    converted** — ``datetime.combine(d, time.min, tzinfo=tz).astimezone(utc)`` —
    exactly as ``backend/routers/home.py`` does. This is the single easiest thing
    in this module to get silently wrong: passing a NAIVE local midnight makes
    ``tools/calendar.py`` stamp a ``"Z"`` on it, and the whole day shifts by the
    UTC offset. A Pacific AE at 23:30 local would then be shown *tomorrow's*
    meetings and told today's were over. (``availability/pto.py`` has that bug;
    it is not the thing to copy.)

    Concretely, for ``America/Los_Angeles`` on 2026-08-28 (PDT, UTC-7) this
    returns ``("2026-08-28T07:00:00Z", "2026-08-29T07:00:00Z")`` — provably the
    local day, whatever hour the AE happens to load the page.

    ``timeMax`` is the following local midnight, and Google treats it as an
    exclusive upper bound on the event start, so a meeting starting exactly at
    tomorrow's midnight belongs to tomorrow. An unknown ``tz_name`` falls back
    (loudly, via ``priorities.temporal.resolve_tz``) rather than raising, because
    every caller of this module is a screen that must still render.

    On a DST-transition day the local day is 23 or 25 hours long and both
    endpoints are still real instants; ``ZoneInfo`` resolves a midnight that a
    transition skipped to the pre-transition offset instead of raising.
    """
    tz, _name = resolve_tz(tz_name)
    start = datetime.combine(local_date, dt_time.min, tzinfo=tz)
    end = datetime.combine(local_date + timedelta(days=1), dt_time.min, tzinfo=tz)
    return _rfc3339_z(start), _rfc3339_z(end)


def _rfc3339_z(value: datetime) -> str:
    """UTC ``"%Y-%m-%dT%H:%M:%SZ"``. Google's list API dislikes ``+00:00``."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Which of the day's events belong on the Calls screen ──────────────────────


def is_capturable_meeting(event: dict, *, ae_email: str) -> bool:
    """Is this event a client call the AE could capture?

    The filter chain, in this order (the first three copied from
    ``priorities/agents/calendar_agent.py`` so the Calls screen and the AI
    Priorities calendar agent agree on what a meeting even is):

    1. ``is_non_meeting_event`` — focus time, OOO, birthdays, working location.
    2. ``is_placeholder_hold`` — opaque "Busy"/"BLOCKED"/"Focus" self-holds.
    3. the AE's own RSVP is ``declined`` — a decision already made.
    4. all-day (``start`` has ``date`` and no ``dateTime``) — a client call is
       an instant, not a date; an all-day row has no clock to arm against.
    5. no Google Meet URI — a Zoom/Teams/phone call cannot be armed or ingested,
       and this screen's Start button is a Meet operation.
    6. internal-only — nobody external on the invite.

    ``is_working_location_event`` is absent on purpose: ``get_upcoming_events``
    already applies it to every result.

    Externality (6) is decided by domain against :func:`own_domains`, NOT by
    ``is_company_matchable``: a client dialling in from ``gmail.com`` is a
    client, and the row must render as "attendee on a personal address — pick
    the company". A blocked domain suppresses the company MATCH, never the ROW.
    Rooms (``attendees[].resource``) and Google's calendar pseudo-domains are
    excluded, since a resource is not a guest.
    """
    if not isinstance(event, dict):
        return False
    if is_non_meeting_event(event):
        return False
    if is_placeholder_hold(event):
        return False
    if self_response_status(event, ae_email) == RSVP_DECLINED:
        return False
    start = event.get("start") or {}
    if isinstance(start, dict) and "date" in start and "dateTime" not in start:
        return False
    if not meet_uri_for_event(event):
        return False
    return bool(_external_attendees(event, ae_email=ae_email))


def _external_attendees(event: dict, *, ae_email: str) -> list[dict]:
    """Attendee dicts that are somebody outside the company (rooms/calendars excluded).

    Kept separate from ``domain_match.external_domains_for_event`` because the
    two answer different questions: that one returns the domains worth ASKING
    HUBSPOT about (blocklist-gated), this one decides whether the meeting has a
    counterparty at all. A gmail.com client is external here and unmatchable
    there, and both facts are true at once.
    """
    own = own_domains(ae_email)
    me = normalize_email(ae_email)
    out: list[dict] = []
    for attendee in event.get("attendees") or []:
        if not isinstance(attendee, dict):
            continue
        if attendee.get("resource") is True:
            continue
        email = normalize_email(attendee.get("email"))
        if not email:
            continue
        if attendee.get("self") is True or (me and email == me):
            continue
        domain = normalize_domain(email)
        if not domain or domain in own or _is_pseudo_domain(domain):
            continue
        out.append(attendee)
    return out


# ── Row building (pure) ──────────────────────────────────────────────────────


def build_rows(
    events: Iterable[dict],
    *,
    ae_email: str,
    tz_name: str,
    now_local: datetime,
    domain_matches: Mapping[str, DomainMatch],
    sessions_by_event: Mapping[str, Any],
) -> list[dict]:
    """The rendered meeting rows for one day. **No I/O — pure and pinnable.**

    ``domain_matches`` is keyed by normalized domain (whatever
    ``resolve_domain_matches`` returned; a missing key is treated as "not looked
    up" → an ``error`` company block, never as ``not_found``).
    ``sessions_by_event`` maps ``event["id"]`` → the existing ``CallSession``
    (any object with ``id``/``status``/``wrapped_at``/``attrs``, so tests can
    pass a stand-in).

    ``now_local`` is passed in rather than read from the clock: a function that
    calls ``datetime.now()`` internally cannot be pinned, and "starts in 4
    minutes" / "is live" are exactly the fields a test has to pin.

    Dedupes on ``event["id"]``. ``singleEvents=True`` means a recurring series
    arrives pre-flattened as one instance per occurrence, each with its own id —
    ``recurringEventId`` is the SERIES id and is reported but NEVER used as the
    key, because keying on it would collapse every occurrence of the weekly
    client call into one session forever.
    """
    ctx = _context(tz_name=tz_name, now_local=now_local)
    now = ctx.now_local
    me = normalize_email(ae_email)
    own = own_domains(ae_email)
    meet_writes_enabled = bool(getattr(settings, "CCS_MEET_ENABLED", False))

    rows: list[dict] = []
    seen: set[str] = set()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if not is_capturable_meeting(event, ae_email=ae_email):
            continue
        event_id = (event.get("id") or "").strip()
        if not event_id:
            # Nothing can be bound or started without the occurrence id it
            # would be persisted under, so a row here would be a button that
            # cannot work. Google always sends one; log rather than pretend.
            logger.warning(
                "calendar_scan: dropping a capturable event with no id (%r)",
                (event.get("summary") or "")[:80],
            )
            continue
        if event_id in seen:
            continue
        seen.add(event_id)

        start_raw = _instant(event.get("start"))
        end_raw = _instant(event.get("end"))
        start_dt = _parse_iso(start_raw, ctx)
        end_dt = _parse_iso(end_raw, ctx)

        started = bool(start_dt and now >= start_dt)
        is_live = bool(started and (end_dt is None or now < end_dt))

        organizer = event.get("organizer") or {}
        if not isinstance(organizer, dict):
            organizer = {}
        organizer_email = normalize_email(organizer.get("email"))
        organizer_is_me = bool(
            organizer.get("self") is True or (me and organizer_email == me)
        )

        uri = meet_uri_for_event(event)
        code = meeting_code_from_uri(uri)
        domains, truncated = external_domains_for_event(event, ae_email=ae_email)
        session = sessions_by_event.get(event_id)

        rows.append(
            {
                "google_event_id": event_id,
                "recurring_event_id": event.get("recurringEventId") or None,
                "title": (event.get("summary") or "").strip() or "(no title)",
                "start": start_raw,
                "end": end_raw,
                "start_local": format_when_local(start_raw, ctx),
                "starts_in_minutes": _minutes_until(start_dt, now),
                "is_live": is_live,
                "meet_uri": uri,
                "meeting_code": code,
                "organizer_email": organizer_email,
                "organizer_is_me": organizer_is_me,
                "rsvp": self_response_status(event, ae_email),
                "attendees": _attendee_rows(event, me=me, own=own),
                "external_domains": domains,
                "attendees_truncated": truncated,
                "company": _company_block(
                    domains, domain_matches, attendees_truncated=truncated
                ),
                "call_session_id": _session_id(session),
                "capture_state": _capture_state(session),
                "arming": _arming_verdict(
                    session,
                    organizer_email=organizer_email,
                    organizer_is_me=organizer_is_me,
                    own=own,
                    started=started,
                    is_live=is_live,
                    meeting_code=code,
                    meet_uri=uri,
                    recurring=bool(event.get("recurringEventId")),
                    meet_writes_enabled=meet_writes_enabled,
                ),
            }
        )
    # Chronological, always. Google's listing is already ``orderBy=startTime``,
    # but a caller assembling events itself (a test, a future two-day view) must
    # not be able to hand the AE a day out of order — the screen is read as a
    # timeline. Sorted on the parsed instant, not the ISO string, so a mixed-
    # offset list still sorts by real time. Undated rows sink to the bottom.
    rows.sort(key=lambda row: _sort_key(row, ctx))
    return rows


def _sort_key(row: dict, ctx: TemporalContext) -> tuple[int, datetime]:
    start = _parse_iso(row.get("start"), ctx)
    if start is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc))
    return (0, start.astimezone(timezone.utc))


def _attendee_rows(event: dict, *, me: str | None, own: frozenset[str]) -> list[dict]:
    """The invite list as the UI shows it. Rooms dropped, nothing inferred.

    Field names follow the homefeed briefing's ``CalendarEvent`` model
    (``display_name`` / ``is_organizer`` / RSVP), so the two calendar surfaces in
    Cubie do not disagree about what an attendee looks like.

    ``is_optional`` is read raw off Google's ``optional`` flag and surfaced —
    nothing in this repo used it before. It must never influence the company
    decision: an optional external guest is still the company on the call.
    """
    out: list[dict] = []
    for attendee in event.get("attendees") or []:
        if not isinstance(attendee, dict):
            continue
        if attendee.get("resource") is True:
            # A conference room read as a guest is a domain read as a company,
            # which is a wrong CRM write. Rooms are not people.
            continue
        email = normalize_email(attendee.get("email"))
        if not email:
            continue
        domain = normalize_domain(email)
        is_self = bool(attendee.get("self") is True or (me and email == me))
        out.append(
            {
                "email": email,
                "display_name": (attendee.get("displayName") or "").strip() or None,
                "is_organizer": bool(attendee.get("organizer")),
                "is_self": is_self,
                "is_external": bool(
                    domain and domain not in own and not _is_pseudo_domain(domain)
                ),
                "is_optional": bool(attendee.get("optional")),
                "response_status": (attendee.get("responseStatus") or "").strip()
                or None,
            }
        )
    return out


def _company_block(
    domains: Sequence[str],
    domain_matches: Mapping[str, DomainMatch],
    *,
    attendees_truncated: bool,
) -> dict:
    """Which HubSpot company this meeting is with — one of five honest answers.

    ``hubspot_company_id`` is NON-NULL only for ``match == "unique"``. That is
    the whole contract: ``DomainMatch.matched`` is false for two or more
    candidates precisely so no caller can reproduce the ``candidates[0]`` bug,
    and this block must not smuggle it back in. An ambiguous row is resolved by
    a human with the picker, never by list order.

    Precedence, most-cautious first:

    * ``attendees_truncated`` → **ambiguous**, always. Google sets
      ``attendeesOmitted`` when it truncates a large invite, so the only
      external attendee we can see may not be the only one there was. Any
      candidates found are still offered as suggestions.
    * any domain carrying ``error`` → **error** ("we could not ask"), even if a
      sibling domain matched cleanly: prefilling from half an answer is how a
      proposal lands on the wrong record.
    * one distinct company across every domain → **unique** (two domains owned
      by the same company still count as one company).
    * several → **ambiguous**. Zero → **not_found**.
    * no matchable domain at all → **skipped**: the externals were on personal
      addresses / pseudo-domains, so there was nothing safe to look up. NOT
      ``not_found``, which would claim HubSpot said no.

    ``domain`` is the domain this verdict is ABOUT — the matched company's for a
    unique hit, the single looked-up domain when there was exactly one, and
    ``None`` when several domains were in play and naming one would mislead.
    """
    if not domains:
        return _block("skipped")

    verdicts: list[DomainMatch] = []
    for domain in domains:
        verdict = domain_matches.get(domain)
        if verdict is None:
            # Absence is the caller's bug, not HubSpot's answer. Say so.
            verdict = DomainMatch(
                domain=domain, error="domain was not looked up in HubSpot"
            )
        verdicts.append(verdict)

    single_domain = domains[0] if len(domains) == 1 else None
    candidates: dict[str, CompanyMatch] = {}
    for verdict in verdicts:
        for candidate in verdict.candidates:
            candidates.setdefault(candidate.hubspot_company_id, candidate)

    if attendees_truncated:
        return _block("ambiguous", candidates=candidates, domain=single_domain)
    if any(verdict.error for verdict in verdicts):
        return _block("error", candidates=candidates, domain=single_domain)
    if len(candidates) == 1:
        only = next(iter(candidates.values()))
        return _block("unique", company=only)
    if len(candidates) > 1:
        return _block("ambiguous", candidates=candidates, domain=single_domain)
    return _block("not_found", domain=single_domain)


def _block(
    match: str,
    *,
    company: CompanyMatch | None = None,
    candidates: Mapping[str, CompanyMatch] | None = None,
    domain: str | None = None,
) -> dict:
    # ``match`` is always a literal from :func:`_company_block`; the closed
    # vocabulary is asserted by the tests rather than by a runtime ``assert``,
    # which would turn a typo into the 500 this whole module exists to avoid.
    return {
        "match": match,
        "hubspot_company_id": company.hubspot_company_id if company else None,
        "name": company.name if company else None,
        "domain": company.domain if company else domain,
        "candidates": [
            {
                "hubspot_company_id": c.hubspot_company_id,
                "name": c.name,
                "domain": c.domain,
                "lifecyclestage": c.lifecyclestage,
            }
            for c in sorted(
                (candidates or {}).values(),
                key=lambda c: (c.name.lower(), c.hubspot_company_id),
            )
        ],
    }


def _session_id(session: Any) -> str | None:
    value = getattr(session, "id", None) if session is not None else None
    return str(value) if value else None


def _capture_state(session: Any) -> str:
    """``not_started`` / ``in_progress`` / ``wrapped`` / ``session_exists``."""
    if session is None:
        return "not_started"
    status = getattr(session, "status", "") or ""
    if getattr(session, "wrapped_at", None) or status == "wrapped":
        return "wrapped"
    if status == "in_progress":
        return "in_progress"
    return "session_exists"


# ── The arming verdict (no Meet call) ────────────────────────────────────────


def _arming_verdict(
    session: Any,
    *,
    organizer_email: str | None,
    organizer_is_me: bool,
    own: frozenset[str],
    started: bool,
    is_live: bool,
    meeting_code: str | None,
    meet_uri: str | None,
    recurring: bool,
    meet_writes_enabled: bool,
) -> dict:
    """What we know about transcription for this row, without writing anything.

    A session that has already been started through Cubie carries the REAL
    verdict in ``attrs["capture_arming"]`` (that is how the sweep tells
    never-armed from armed-but-empty), so it is replayed here in preference to
    re-deriving a precondition — an armed row that renders "unknown" is the same
    class of lie as an unarmable row that renders "armed". A stored verdict is
    trusted only when both its state and its reason are in the closed
    vocabularies; anything else falls through to the precondition below rather
    than rendering a badge nobody can read.
    """
    stored = _stored_arming(session)
    if stored is not None:
        return stored
    return _arming_precondition(
        organizer_email=organizer_email,
        organizer_is_me=organizer_is_me,
        own=own,
        started=started,
        is_live=is_live,
        meeting_code=meeting_code,
        meet_uri=meet_uri,
        recurring=recurring,
        meet_writes_enabled=meet_writes_enabled,
    )


def _stored_arming(session: Any) -> dict | None:
    attrs = getattr(session, "attrs", None) if session is not None else None
    if not isinstance(attrs, dict):
        return None
    stored = attrs.get("capture_arming")
    if not isinstance(stored, dict):
        return None
    state = stored.get("state")
    reason = stored.get("reason")
    if state not in ARMING_STATES or reason not in ARMING_REASONS:
        return None
    detail = stored.get("detail")
    return {
        "state": state,
        "reason": reason,
        "detail": detail if isinstance(detail, str) and detail.strip() else None,
    }


def _arming_precondition(
    *,
    organizer_email: str | None,
    organizer_is_me: bool,
    own: frozenset[str],
    started: bool,
    is_live: bool,
    meeting_code: str | None,
    meet_uri: str | None,
    recurring: bool,
    meet_writes_enabled: bool,
) -> dict:
    """The verdict that costs nothing: no ``spaces.get``, no patch, no guess.

    Ordering is deliberate. The first three are facts about THIS meeting that no
    configuration change would alter, so they are the most useful thing to tell
    the AE; ``write_disabled`` is a system fact that applies to whatever is
    left; and a recurring occurrence that is otherwise armable stays ``unknown``
    — the series warning informs, it does not block.

    Note what is NOT claimed. Whether a Workspace user may patch a space they
    did not create is UNVERIFIED on this tenant, so ``not_owner`` is Cubie
    declining to try rather than a statement about Google's rules. The
    definitive per-row answers (``already_on``, ``arming_unsupported``,
    ``armed_by_cubie``) can only come from the write path, which is why they are
    in ``ARMING_REASONS`` but never produced here.
    """
    if not meeting_code:
        return _arming(
            "not_armable",
            "lookup_failed",
            f"The calendar's Meet link has no meeting code {PRODUCT_NAME} can resolve"
            + (f" ({meet_uri})" if meet_uri else "")
            + ", so its space cannot be looked up. Join with the link — the "
            "company binding still works.",
        )
    organizer_domain = normalize_domain(organizer_email)
    # A Google pseudo-domain organizer (``…@group.calendar.google.com`` on a
    # shared calendar) is not another company — calling it ``external_host``
    # would tell the AE their own team's shared calendar belongs to the client.
    # It falls through to ``not_owner``, which is the honest answer: the meeting
    # is not the AE's to change.
    if (
        organizer_email
        and organizer_domain
        and organizer_domain not in own
        and not _is_pseudo_domain(organizer_domain)
    ):
        return _arming(
            "not_armable",
            "external_host",
            f"{organizer_email} organized this meeting, so its Meet space lives "
            f"in another company's Workspace and {PRODUCT_NAME} cannot turn "
            "transcription on. Use their link and log the call afterwards.",
        )
    if started:
        return _arming(
            "not_armable",
            "already_started",
            "This call has already started, and Google can only pre-arm "
            "transcription before a conference begins."
            if is_live
            else "This call has already happened — transcription can only be "
            "armed before a conference begins.",
        )
    if not organizer_is_me:
        return _arming(
            "not_armable",
            "not_owner",
            f"{organizer_email or 'A colleague'} organized this meeting, so "
            f"{PRODUCT_NAME} does not change its Meet settings. Ask the organizer "
            "to turn on transcription, or bind the company here and log the call "
            "after.",
        )
    if not meet_writes_enabled:
        return _arming(
            "not_armable",
            "write_disabled",
            f"Meet transcription arming is switched off in {PRODUCT_NAME} right now "
            "(CCS_MEET_ENABLED), so no meeting can be armed. Binding the company "
            "still works.",
        )
    if recurring:
        return _arming(
            "unknown",
            "recurring_series",
            "This is one occurrence of a recurring meeting, and a series shares "
            "ONE Meet space — arming it turns transcription on for every future "
            "occurrence too.",
        )
    return _arming("unknown", "not_checked_yet", None)


def _arming(state: str, reason: str, detail: str | None) -> dict:
    # Same reasoning as :func:`_block`: literals in, tests pin the vocabulary.
    return {"state": state, "reason": reason, "detail": detail}


# ── Clock helpers ────────────────────────────────────────────────────────────


def _context(*, tz_name: str, now_local: datetime) -> TemporalContext:
    """A ``TemporalContext`` for :func:`format_when_local`, built with no I/O.

    ``resolve_user_now`` would hit the database for the user's saved timezone;
    here the timezone is already resolved (once, from the calendar itself) and
    "now" is already pinned by the caller, so the context is assembled directly
    to keep ``build_rows`` pure. Every displayed clock string still goes through
    ``format_when_local`` — the repo has a recorded +2h drift bug from letting
    anything else render a time.
    """
    _tz, name = resolve_tz(tz_name)
    aware = now_local if now_local.tzinfo else now_local.replace(tzinfo=_tz)
    return TemporalContext(
        now_utc=aware.astimezone(timezone.utc),
        now_local=aware.astimezone(_tz),
        tz_name=name,
    )


def _aware(value: datetime, ctx: TemporalContext) -> datetime:
    """``value`` as an aware datetime in the reader's zone."""
    if value.tzinfo is None:
        tz, _name = resolve_tz(ctx.tz_name)
        return value.replace(tzinfo=tz)
    return value


def _instant(node: Any) -> str | None:
    """``start``/``end`` → its ISO string (``dateTime``, else ``date``)."""
    if not isinstance(node, dict):
        return None
    value = node.get("dateTime") or node.get("date")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_iso(value: str | None, ctx: TemporalContext) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed, ctx)


def _minutes_until(start: datetime | None, now: datetime) -> int | None:
    """Signed whole minutes from ``now`` to ``start`` — negative once started.

    Floored, so a meeting 4½ minutes out reads "in 4 minutes" and one that
    started 4½ minutes ago reads "5 minutes ago": always the conservative
    direction for a countdown the AE is acting on.
    """
    if start is None:
        return None
    return int((start - now).total_seconds() // 60)


# ── The one impure function ──────────────────────────────────────────────────


def scan_day(
    ae_email: str,
    *,
    local_date: date | None = None,
    db: Any = None,
    calendar_client: Any = None,
    hubspot_client: Any = None,
    now_utc: datetime | None = None,
) -> dict:
    """One AE's capturable meetings for one local day. **Never raises.**

    The envelope is ``{"calendar_available", "timezone", "local_date",
    "meetings", "script_catalogue"}`` — the shape ``backend/routers/eos/l10.py``
    established for a calendar read, which never 500s on a Calendar outage, plus
    the day's checklists (:func:`_attach_scripts`). ``calendar_available:
    False`` with an empty list is "we could not read your calendar"; ``True``
    with an empty list is "you have no client calls today". The UI must say two
    different things, so this never collapses them.

    I/O, in order, and that is all of it:

    1. ``get_calendar_timezone()`` once, then ``resolve_user_now(..., tz_name=)``
       — an explicit ``tz_name`` always wins over the user's saved preference,
       and every clock string on the page is rendered against this one zone.
    2. ONE ``get_upcoming_events`` over :func:`day_window_utc`.
    3. ONE batched ``companies_for_domains`` across the day's distinct matchable
       domains, through ``domain_match``'s short negative cache (positive
       matches are checked live, errors never cached) and the existing HubSpot backoff.
    4. ONE indexed query for existing ``CallSession`` rows on
       ``(ae_email, calendar_event_id)``.
    5. ONE read of the live script catalogue and ONE of this AE's remembered
       product picks across the day's bound companies (:func:`_attach_scripts`)
       — both constant, neither per row, and both fail-soft into
       ``scripts: null`` and ``script_catalogue: null`` rather than into an
       unreadable day.

    ``db`` / ``calendar_client`` / ``hubspot_client`` / ``now_utc`` are optional
    injection points for the router (which already holds a session) and for
    tests; the documented call, ``scan_day(ae_email)``, is unchanged. When ``db``
    is omitted a ``SessionLocal()`` is opened and closed here.
    """
    ae = normalize_email(ae_email) or (ae_email or "").strip()
    try:
        client = calendar_client or GoogleCalendarClient(user_email=ae)
    except Exception:
        logger.exception("calendar_scan: could not build a Calendar client for %s", ae)
        ctx = resolve_user_now(ae, now_utc=now_utc)
        return _envelope(False, ctx.tz_name, local_date or ctx.today, [])

    # ``service is None`` is the client's own "I could not authenticate" signal.
    # Checked with a sentinel so only a client that HAS the attribute and set it
    # to None counts as unavailable — an injected stub without one is not.
    if getattr(client, "service", _MISSING) is None:
        # Calendar not configured (local dev with no Workspace key) — the same
        # empty-state answer l10.py gives, not an exception. The timezone falls
        # back to the user's saved preference here, since the calendar (the
        # better source) could not be asked.
        logger.info("calendar_scan: no Calendar service for %s", ae)
        ctx = resolve_user_now(ae, now_utc=now_utc)
        return _envelope(False, ctx.tz_name, local_date or ctx.today, [])

    # The calendar's OWN timezone, resolved once and then passed explicitly —
    # ``resolve_user_now`` guarantees an explicit ``tz_name`` wins. Left as None
    # if it cannot be read, so the fallback is the user's saved preference /the
    # org default rather than UTC (which would move an AE's day by their UTC offset).
    tz_name: str | None = None
    try:
        tz_name = client.get_calendar_timezone() or None
    except Exception:
        logger.warning(
            "calendar_scan: timezone lookup failed for %s", ae, exc_info=True
        )
    ctx = resolve_user_now(ae, tz_name=tz_name, now_utc=now_utc)
    day = local_date or ctx.today

    time_min, time_max = day_window_utc(ctx.tz_name, day)
    try:
        events = client.get_upcoming_events(
            max_results=_MAX_DAY_EVENTS,
            time_min=time_min,
            time_max=time_max,
            time_zone=ctx.tz_name,
        )
    except Exception:
        logger.exception("calendar_scan: calendar read failed for %s", ae)
        return _envelope(False, ctx.tz_name, day, [])

    # `get_upcoming_events` swallows its own errors and returns [] — an empty
    # list plus a recorded error is an OUTAGE, not an empty calendar, and the
    # client is explicit that callers surfacing results MUST check.
    if getattr(client, "init_error", None) or getattr(client, "last_error", None):
        logger.warning(
            "calendar_scan: calendar unavailable for %s (%s)",
            ae,
            getattr(client, "last_error", None) or getattr(client, "init_error", None),
        )
        return _envelope(False, ctx.tz_name, day, [])

    capturable = [
        event
        for event in events or []
        if isinstance(event, dict) and is_capturable_meeting(event, ae_email=ae)
    ]

    domains: list[str] = []
    for event in capturable:
        for domain in external_domains_for_event(event, ae_email=ae)[0]:
            if domain not in domains:
                domains.append(domain)

    try:
        domain_matches = resolve_domain_matches(
            domains, ae_email=ae, client=hubspot_client
        )
    except Exception as exc:
        # resolve_domain_matches is documented never to raise; if it ever does,
        # the day still renders and every company block says "we could not ask"
        # — which is not the same sentence as "not in HubSpot".
        logger.exception("calendar_scan: domain matching failed for %s", ae)
        message = str(exc)[:200] or exc.__class__.__name__
        domain_matches = {d: DomainMatch(domain=d, error=message) for d in domains}

    event_ids = [
        (event.get("id") or "").strip()
        for event in capturable
        if (event.get("id") or "").strip()
    ]
    sessions_by_event = _load_sessions(ae, event_ids, db=db)

    try:
        rows = build_rows(
            capturable,
            ae_email=ae,
            tz_name=ctx.tz_name,
            now_local=ctx.now_local,
            domain_matches=domain_matches,
            sessions_by_event=sessions_by_event,
        )
    except Exception:
        # build_rows is pure and total by construction, so this is a bug, not a
        # condition. It still must not 500: report the day as unreadable (which
        # the UI says out loud) rather than as an empty one (which it believes).
        logger.exception("calendar_scan: row building failed for %s", ae)
        return _envelope(False, ctx.tz_name, day, [])

    # Additive, and outside the block above on purpose: a script lookup that
    # fails must cost the CHECKLIST, not the day. ``_attach_scripts`` swallows
    # its own failures into ``scripts: null`` per row and a ``None`` catalogue
    # (never raises), so a day with an unreadable ``call_scripts`` table still
    # renders every meeting, every company binding and every join link.
    catalogue = _attach_scripts(rows, ae_email=ae, db=db)
    return _envelope(True, ctx.tz_name, day, rows, catalogue)


def _envelope(
    available: bool,
    tz_name: str,
    day: date,
    meetings: list[dict],
    script_catalogue: list[dict] | None = None,
) -> dict:
    return {
        "calendar_available": available,
        "timezone": tz_name,
        "local_date": day.isoformat(),
        "meetings": meetings,
        # Present on every path, ``None`` on each one that never read the
        # checklists — an outage day has no rows to show them on anyway.
        "script_catalogue": script_catalogue,
    }


def _load_sessions(
    ae_email: str, event_ids: Sequence[str], *, db: Any = None
) -> dict[str, Any]:
    """``event id -> CallSession`` for this AE. One indexed read, or ``{}``.

    Keyed on ``calendar_event_id`` — the per-occurrence ``event["id"]`` — which
    is a real column precisely so this lookup exists: nothing in this repo
    queries inside a JSON column, and this value is *found by*, not merely
    recorded. Filtered by ``ae_email`` too, because two colleagues are
    routinely on the same client call and each owns their own capture.

    A DB failure yields ``{}`` and a warning rather than a 500: the day still
    renders. The cost is real and worth naming — every row then reads
    ``not_started``, so a second Start could be attempted. That is survivable
    only because the ``(ae_email, calendar_event_id)`` unique constraint makes
    ``POST /api/calls/start`` idempotent at the database, which is the whole
    reason that constraint exists.
    """
    if not event_ids:
        return {}
    owns_session = db is None
    session = db
    try:
        if session is None:
            from rubik_core.database.db import SessionLocal

            session = SessionLocal()
        rows = (
            session.query(CallSession)
            .filter(
                func.lower(CallSession.ae_email) == (ae_email or "").lower(),
                CallSession.calendar_event_id.in_(list(event_ids)),
            )
            .all()
        )
        from rubik_core.call_capture.provenance import visible_session_ids

        visible = visible_session_ids(rows)
        rows = [row for row in rows if row.id in visible]
    except Exception:
        logger.exception(
            "calendar_scan: could not load existing sessions for %s", ae_email
        )
        return {}
    finally:
        if owns_session and session is not None:
            try:
                session.close()
            except Exception:
                logger.debug("calendar_scan: session close failed", exc_info=True)

    out: dict[str, Any] = {}
    for row in rows:
        key = (getattr(row, "calendar_event_id", None) or "").strip()
        if not key:
            continue
        existing = out.get(key)
        # Deterministic under the (impossible-by-constraint) duplicate: prefer
        # the row that has got furthest, so a stale scheduled row can never hide
        # a wrapped one.
        if existing is None or _state_rank(row) > _state_rank(existing):
            out[key] = row
    return out


_STATE_RANK = {"not_started": 0, "session_exists": 1, "in_progress": 2, "wrapped": 3}


def _state_rank(session: Any) -> int:
    return _STATE_RANK.get(_capture_state(session), 0)


# ── The script block (additive, fail-soft) ───────────────────────────────────


def _attach_scripts(
    rows: list[dict], *, ae_email: str, db: Any = None
) -> list[dict] | None:
    """Add ``scripts`` to every row, in place, and return the day's
    ``script_catalogue``. **Never raises.**

    The idea the whole checklist feature rests on: if something is not asked or
    not covered on the call, no amount of automation afterwards recovers it.
    This is where that reaches the AE — before the call, on the row they are
    about to join.

    Three keys, and the plural in every one of them is deliberate. A call that
    surfaces three products should produce THREE leads (lead↔deal is 1:1), so a
    call carries N products, the panel renders one section per product, and
    coverage is later computed per product rather than merged into one
    meaningless percentage:

    * ``selected_product_keys`` — what this AE picked for this client last time,
      ``[]`` when they never have. Not filtered against what has an approved
      script: "what the AE picked" and "what Cubie can show a checklist for" are
      two different facts, and a product whose script was retired should still
      render as selected with no section rather than silently unpick itself.
    * ``available`` — every product with a live checklist, for the picker. The
      SAME list object on every row (it does not vary by row); it is serialized,
      never mutated.
    * ``sections`` — the checklist for each selected product that resolves,
      in the order the AE picked them.

    **And the return value, the envelope's ``script_catalogue``: the checklist
    for every product in ``available``, in the same order.** ``sections`` covers
    only what this client's memory holds, and the AE can tick any product the
    picker offers — on a first call with a client there is no memory at all. A
    panel that resolved picks against ``sections`` alone rendered "no approved
    checklist" directly under a chip reading "5 facts to cover", on every first
    call and for every product added on a later one, so the checklist only ever
    appeared from the second call on. The catalogue is sent ONCE for the day
    rather than per row: it does not vary by row, and it is already in hand from
    the one read below. It is read even when the day has no rows, so that a
    ``None`` catalogue keeps meaning one thing.

    **``scripts`` is ``None`` only on failure, never on emptiness.** These
    tables ship EMPTY on purpose — the content is the sales leads' and the
    sales supervisor's to author and approve — so "no scripts authored yet" is the
    normal early state and it renders as a block with empty lists. ``None``
    means "we could not look", which is a different sentence, exactly as
    ``company.match == "error"`` is a different sentence from ``not_found`` a
    few fields to the left. The catalogue follows the same rule: ``None``
    exactly when the rows got ``scripts: null``, ``[]`` when nothing has been
    approved yet.

    Cost is CONSTANT, not per row: one read for the live catalogue (plus its
    eager item load) and one for this AE's remembered picks across every company
    the day is bound to. ``scan_day`` is documented as a pure read that never
    raises and ``GET /scheduled`` must never 500 — a blank day is an answer an AE
    believes and acts on by not capturing a real call — so every failure here is
    swallowed into ``scripts: null`` and logged.
    """
    for row in rows:
        # The honest default, set FIRST so an early return still leaves every
        # row carrying the key. A row silently missing ``scripts`` would make a
        # frontend read ``undefined`` and render "no products", which is the
        # "nothing is wrong" failure this module exists to avoid.
        row["scripts"] = None

    company_ids: list[str] = []
    for row in rows:
        company_id = (
            (row.get("company") or {}).get("hubspot_company_id") or ""
        ).strip()
        if company_id and company_id not in company_ids:
            company_ids.append(company_id)

    owns_session = db is None
    session = db
    try:
        if session is None:
            from rubik_core.database.db import SessionLocal

            session = SessionLocal()
        catalogue = _live_script_payloads(session)
        defaults = _remembered_by_company(session, ae_email, company_ids)
    except Exception:
        logger.exception("calendar_scan: could not load call scripts for %s", ae_email)
        return None
    finally:
        if owns_session and session is not None:
            try:
                session.close()
            except Exception:
                logger.debug("calendar_scan: session close failed", exc_info=True)

    # Everything below is plain dicts — nothing ORM-shaped escapes the session,
    # so closing it above cannot turn a render into a DetachedInstanceError.
    # ONE list, in the picker's order: ``available`` is its summary and the
    # returned catalogue is its full form, so the chip reading "5 facts to
    # cover" and the checklist under it can never describe two different scripts.
    ordered = sorted(
        catalogue.values(),
        key=lambda p: ((p["name"] or "").lower(), p["product_key"]),
    )
    available = [
        {
            "product_key": payload["product_key"],
            "name": payload["name"],
            "script_id": payload["script_id"],
            "item_count": len(payload["items"]),
        }
        for payload in ordered
    ]
    for row in rows:
        company_id = (
            (row.get("company") or {}).get("hubspot_company_id") or ""
        ).strip()
        selected = defaults.get(company_id, []) if company_id else []
        row["scripts"] = {
            "selected_product_keys": list(selected),
            "available": available,
            "sections": [catalogue[key] for key in selected if key in catalogue],
        }
    return ordered


def live_script_catalogue(db: Any) -> dict[str, dict]:
    """The public door to :func:`_live_script_payloads`, for the Meet add-on.

    The add-on renders a checklist for ANY product the AE picks mid-call, not
    only the ones remembered for this client, so it needs the whole catalogue
    rather than the per-row ``sections``. Routed through the private function
    so one rule decides what is live, and so the tests that patch
    ``_live_script_payloads`` cover this reader too. Raises like the function it
    wraps: the caller decides how a script-table failure is reported.
    """
    return _live_script_payloads(db)


def _live_script_payloads(db: Any) -> dict[str, dict]:
    """``product_key -> panel section`` for every product with a LIVE script.

    ONE query for the whole day rather than one per row — the AE is waiting on
    this screen, and the day's rows share one catalogue.

    Live means **approved**, not merely active. Scripts are mined from the
    historic transcript corpus by a batch job, so ``source="mined"`` rows land in
    this table written by a machine with ``is_active`` already true; that column
    is the author's on/off switch and costs nothing to set. ``approved_by`` is a
    named human's signature (an AI-generated script never ships as
    authoritative), and a reader that gated on ``is_active`` alone would put a
    model's guess in front of an AE as the company's own checklist — invisibly, because
    the panel renders and the questions look plausible.

    Newest approved version wins per product, picked in Python from the single
    read: version counts per product are in the low single digits (a script forks
    on approval-edit, not on every keystroke). This is deliberately the same rule
    ``scripts.scripts_for_products`` applies, and it is applied here rather than
    by calling that function because the picker needs the WHOLE catalogue and
    that function needs the keys up front.
    """
    rows = (
        db.execute(
            select(CallScript)
            .options(selectinload(CallScript.items))
            .where(
                CallScript.is_active.is_(True),
                CallScript.approved_by.isnot(None),
            )
            .order_by(CallScript.version)
        )
        .scalars()
        .all()
    )
    best: dict[str, CallScript] = {}
    for row in rows:
        key = (row.product_key or "").strip().lower()
        if not key:
            continue
        current = best.get(key)
        if current is None or (row.version or 0) >= (current.version or 0):
            best[key] = row
    return {key: script_payload(script) for key, script in best.items()}


def _remembered_by_company(
    db: Any, ae_email: str, company_ids: Sequence[str]
) -> dict[str, list[str]]:
    """``hubspot_company_id -> the products this AE picked there last time``.

    ONE query across the day's bound companies, which is the batched form of
    ``scripts.remembered_products`` — same table, same fold, same ``func.lower``
    read that ``ix_call_company_product_defaults_email_lower`` exists to serve.

    This memory is the whole of the "self-service" feel and it is deliberately
    cheap: the weekly call with the same client defaults to the same products
    every week, and the AE only touches the control when something changes.
    The better idea — read the products off the HubSpot lead — is not
    buildable, because there is no Leads READ anywhere in this repo (leads are
    write-only through ``_write_leads``, itself gated off pending a HubSpot
    permission that has not been granted yet).

    A row with no bound company is not an error and not a lookup: it simply has
    no remembered pick, and the caller renders ``[]``.
    """
    if not company_ids:
        return {}
    rows = (
        db.execute(
            select(CompanyProductDefault).where(
                func.lower(CompanyProductDefault.ae_email)
                == (ae_email or "").strip().lower(),
                CompanyProductDefault.hubspot_company_id.in_(list(company_ids)),
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, list[str]] = {}
    for row in rows:
        stored = row.product_keys
        if not isinstance(stored, list):
            # A hand-edited row, or a writer that bypassed ``remember_products``.
            # Not worth failing the day over — but say so, because the AE's picks
            # silently stopped being remembered and nothing else would mention it.
            logger.warning(
                "calendar_scan: call_company_product_defaults %s holds %s, not a "
                "list; ignoring the remembered products",
                row.id,
                type(stored).__name__,
            )
            continue
        out[row.hubspot_company_id] = _ordered_keys(stored)
    return out
