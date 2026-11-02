"""The rep's OWN Gmail and Calendar, as contact signals for the rhythm checks.

HubSpot only knows what a rep logged. A deal they emailed yesterday without
logging it reads as "no contact in 12 days", and a client call held as a
calendar meeting never shows up at all. This module reads two more sources —
for the rep's own checks only — so "last contact" means the newest of:

* HubSpot's logged contact (``notes_last_contacted``, read by the checks);
* the newest email the rep SENT to the deal's client side, and the newest one
  the client side sent BACK (Gmail);
* the most recent past MEETING with the client side (Calendar).

An UPCOMING meeting with the client side also counts as a booked next step.

**Whose mailbox.** Only the signed-in rep's own, for their own checks. The team
view and a named-colleague view never call this module: reading reps'
mailboxes to build someone else's report would be a view of the person, and
those views say instead that they count only what is logged in HubSpot.

**What is fetched.** Every request carries a field mask, so Google returns only
what is used: message ids and timestamps for the checks; for the end-of-day
recap, the recipients and subject of each email the rep SENT that day; and for
meetings, the start time, status, title and invitees (with their replies).

**What does not count.**

* Drafts and scheduled sends — only mail with the SENT label is "sent", so an
  unsent follow-up Cubie drafted cannot quiet a deal.
* Automatic mail from the client side: out-of-office and auto-replies,
  promotions / social / forum mail, and no-reply senders — an auto-reply is not
  the decision-maker answering, and a daily rate sheet is not contact.
* A meeting the rep declined, or one where every client invitee declined.
  "They replied" (for "waiting on the client") needs a client invitee who
  ACCEPTED; merely being invited is not an answer.
* Meeting rooms and group calendars, which are not people.

**The client side** of a deal is its HubSpot contacts. A business address
stands for its whole company domain (the rep may email a colleague of the
contact); a free-mail address (gmail.com, outlook.com…) stands only for itself,
or every consumer mailbox in the world would count as the client.

Never raises, and never logs a client address: a source that cannot be read is
named in ``notes``, the checks fall back to HubSpot for it, and the log line
carries only the error type and HTTP status (a Gmail error's URL contains the
search query, which contains client addresses).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any

from rubik_core.brand import PRODUCT_NAME

logger = logging.getLogger(__name__)

#: How far back an email or meeting counts as contact.
LOOKBACK_DAYS = 90
#: How far ahead a booked meeting counts as a next step.
LOOKAHEAD_DAYS = 60
#: Requests per Gmail batch call. Small on purpose: Gmail limits concurrent
#: requests per user, and a batch is billed as its individual requests.
_GMAIL_BATCH = 20
#: Attempts for a throttled or failing Gmail request before the source is
#: reported unreadable.
_GMAIL_ATTEMPTS = 4
#: Search terms per deal. Real deals carry two to five contacts.
_MAX_TERMS = 12
#: Calendar pages (2,500 events each) and recap pages (100 messages each)
#: before the source is reported incomplete instead of silently short.
_MAX_CALENDAR_PAGES = 8
_MAX_RECAP_PAGES = 5
#: How long one rep's calendar window and mail answers are reused. Matches the
#: checks' own cache, so one My Day tab costs one calendar read.
_CACHE_TTL_SECONDS = 300

FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "comcast.net",
        "att.net",
        "verizon.net",
        "sbcglobal.net",
        "protonmail.com",
        "proton.me",
        "ymail.com",
    }
)

#: Gmail search clauses that drop automatic mail from "the client wrote".
_NOT_AUTOMATIC = (
    '-subject:("automatic reply" OR "auto-reply" OR autoreply OR "out of office" '
    'OR "out of the office") -category:promotions -category:social -category:forums '
    "-from:(noreply OR no-reply OR donotreply OR do-not-reply OR mailer-daemon)"
)

#: Calendar "attendees" that are rooms or group calendars, not people.
_NOT_PEOPLE = ("resource.calendar.google.com", "group.calendar.google.com")


@dataclass(frozen=True)
class Touch:
    """What the rep's own mailbox and calendar say about one deal."""

    last_in: datetime | None = None  # newest email FROM the client side
    last_out: datetime | None = None  # newest email the rep SENT to them
    last_meeting: datetime | None = None  # most recent past meeting with them
    #: Most recent past meeting a client invitee ACCEPTED — the only kind of
    #: meeting that counts as the client answering.
    last_meeting_accepted: datetime | None = None
    next_meeting: datetime | None = None  # soonest upcoming meeting with them

    @property
    def last_contact(self) -> datetime | None:
        """The newest email in either direction, or past meeting."""
        seen = [t for t in (self.last_in, self.last_out, self.last_meeting) if t]
        return max(seen) if seen else None


@dataclass
class SignalReport:
    """Per-deal touches, plus what could not be read."""

    touches: dict[str, Touch] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    gmail_ok: bool = True
    calendar_ok: bool = True

    @property
    def complete(self) -> bool:
        return self.gmail_ok and self.calendar_ok


@dataclass
class DayActivity:
    """The rep's own sent emails and meetings for one day (the recap)."""

    emails: list[dict[str, Any]] = field(default_factory=list)  # subject, to, at
    meetings: list[dict[str, Any]] = field(default_factory=list)  # title, with, at
    notes: list[str] = field(default_factory=list)
    gmail_ok: bool = True
    calendar_ok: bool = True

    @property
    def complete(self) -> bool:
        return self.gmail_ok and self.calendar_ok


# ── Matching ─────────────────────────────────────────────────────────────────


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""


def match_terms(emails: Iterable[str], own_domain: str) -> list[str]:
    """The search terms that stand for a deal's client side.

    A business address becomes its domain (``acme.com``); a free-mail address
    stays the exact address. The company's own domain is never a term — an internal
    thread about a deal is not contact with the client.
    """
    own = (own_domain or "").strip().lower()
    terms: list[str] = []
    for raw in emails:
        addr = (raw or "").strip().lower()
        dom = _domain(addr)
        if not dom or dom == own:
            continue
        term = addr if dom in FREE_MAIL_DOMAINS else dom
        if term not in terms:
            terms.append(term)
    return terms[:_MAX_TERMS]


def exact_terms(emails: Iterable[str], own_domain: str) -> list[str]:
    """The exact addresses (never widened to a domain), the company's own left out —
    for "has THIS person answered", where a colleague of theirs does not count."""
    own = (own_domain or "").strip().lower()
    terms: list[str] = []
    for raw in emails:
        addr = (raw or "").strip().lower()
        dom = _domain(addr)
        if dom and dom != own and addr not in terms:
            terms.append(addr)
    return terms[:_MAX_TERMS]


def matches(address: str, terms: Iterable[str]) -> bool:
    """Whether ``address`` belongs to the client side ``terms`` describe."""
    addr = (address or "").strip().lower()
    if not addr:
        return False
    for term in terms:
        if "@" in term:
            if addr == term:
                return True
        elif addr.endswith("@" + term):
            return True
    return False


def _or(terms: list[str]) -> str:
    return "(" + " OR ".join(terms) + ")"


def _safe(exc: BaseException) -> str:
    """An error as type + HTTP status only — never its message, which for a
    Gmail error is a URL carrying the search query and so client addresses."""
    if isinstance(exc, GmailUnreadable):
        return str(exc)  # built from _safe() parts only, never a URL
    status = getattr(getattr(exc, "resp", None), "status", None)
    return f"{type(exc).__name__}{f' {status}' if status else ''}"


# ── Caches ───────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_mail_cache: dict[tuple, tuple[float, dict[str, datetime | None]]] = {}
_cal_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


def clear_cache() -> None:
    with _lock:
        _mail_cache.clear()
        _cal_cache.clear()


def _sweep(cache: dict) -> None:
    """Drop expired entries so a long-lived process does not grow without bound."""
    cutoff = time.monotonic() - _CACHE_TTL_SECONDS
    for key in [k for k, (ts, _) in cache.items() if ts < cutoff]:
        cache.pop(key, None)


# ── Gmail ────────────────────────────────────────────────────────────────────


class GmailUnreadable(RuntimeError):
    """Gmail could not answer. The message is safe to log (no addresses)."""


def _gmail_service(mailbox: str):
    """The Gmail API for ``mailbox`` (domain-wide delegation), or None."""
    from rubik_core.tools.gmail import GmailClient  # local: Google client libs

    return GmailClient(user_email=mailbox).service


def _retryable(exc: BaseException) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    if status == 403:
        text = str(getattr(exc, "content", b"") or b"")
        return "rateLimitExceeded" in text or "userRateLimitExceeded" in text
    return False


def _run_batch(service, requests: list[tuple[str, Any]]) -> dict[str, Any]:
    """Execute ``(request_id, request)`` pairs in small Gmail batch calls.

    Throttled or failing requests are retried with backoff, only the failed
    ones. Anything still failing raises :class:`GmailUnreadable`, so an
    unreadable query never turns into a confident "no email" for a deal.
    """
    out: dict[str, Any] = {}
    pending = list(requests)
    delay = 0.5
    for attempt in range(_GMAIL_ATTEMPTS):
        failed: list[tuple[str, Any]] = []
        fatal: list[BaseException] = []
        by_id = dict(pending)

        def _cb(
            request_id, response, exception, by_id=by_id, failed=failed, fatal=fatal
        ):
            if exception is None:
                out[request_id] = response
            elif _retryable(exception):
                failed.append((request_id, by_id[request_id]))
            else:
                fatal.append(exception)

        for start in range(0, len(pending), _GMAIL_BATCH):
            batch = service.new_batch_http_request(callback=_cb)
            for rid, req in pending[start : start + _GMAIL_BATCH]:
                batch.add(req, request_id=rid)
            batch.execute()
        if fatal:
            raise GmailUnreadable(f"gmail request failed: {_safe(fatal[0])}")
        if not failed:
            return out
        pending = failed
        if attempt < _GMAIL_ATTEMPTS - 1:
            time.sleep(delay)
            delay *= 2
    raise GmailUnreadable(f"gmail throttled {len(pending)} requests after retries")


def _gmail_latest(service, queries: dict[str, str]) -> dict[str, datetime | None]:
    """``{key: newest message date}`` for each Gmail search query."""
    msgs = service.users().messages()
    listed = _run_batch(
        service,
        [
            (k, msgs.list(userId="me", q=q, maxResults=1, fields="messages/id"))
            for k, q in queries.items()
        ],
    )
    ids = {k: (r.get("messages") or [{}])[0].get("id") for k, r in listed.items()}
    wanted = [
        (
            k,
            msgs.get(userId="me", id=mid, format="minimal", fields="id,internalDate"),
        )
        for k, mid in ids.items()
        if mid
    ]
    got = _run_batch(service, wanted) if wanted else {}
    out: dict[str, datetime | None] = {k: None for k in queries}
    for k, msg in got.items():
        try:
            out[k] = datetime.fromtimestamp(
                int(msg.get("internalDate")) / 1000, tz=timezone.utc
            )
        except (TypeError, ValueError):
            out[k] = None
    return out


def _gmail_touches(
    mailbox: str, deal_terms: dict[str, list[str]], days: int
) -> dict[str, tuple[datetime | None, datetime | None]]:
    """``{deal: (last_in, last_out)}`` from the rep's own mailbox."""
    queries: dict[str, str] = {}
    for deal, terms in deal_terms.items():
        if not terms:
            continue
        group = _or(terms)
        queries[f"{deal}|in"] = (
            f"from:{group} newer_than:{days}d -in:chats -in:drafts {_NOT_AUTOMATIC}"
        )
        # in:sent — a draft (including one Cubie wrote) or a scheduled send
        # was never sent and must not quiet a deal.
        queries[f"{deal}|out"] = (
            f"in:sent to:{group} newer_than:{days}d -in:chats -in:drafts"
        )
    if not queries:
        return {}
    from rubik_core.tenancy.context import current_workspace

    key = (current_workspace(), mailbox, tuple(sorted(queries.items())))
    latest: dict[str, datetime | None] | None = None
    with _lock:
        hit = _mail_cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
            latest = hit[1]
    if latest is None:
        service = _gmail_service(mailbox)
        if service is None:
            raise GmailUnreadable("Gmail is not reachable for this mailbox")
        latest = _gmail_latest(service, queries)
        with _lock:
            _sweep(_mail_cache)
            _mail_cache[key] = (time.monotonic(), latest)
    return {
        deal: (latest.get(f"{deal}|in"), latest.get(f"{deal}|out"))
        for deal in deal_terms
    }


# ── Calendar ─────────────────────────────────────────────────────────────────

_EVENT_FIELDS = (
    "nextPageToken,items(status,summary,start,"
    "organizer(email,self),attendees(email,self,resource,responseStatus))"
)


def _calendar_service(mailbox: str):
    """The Calendar API for ``mailbox`` (domain-wide delegation), or raise."""
    from rubik_core.tools.calendar import GoogleCalendarClient  # local: Google libs

    cal = GoogleCalendarClient(user_email=mailbox)
    if cal.service is None:
        raise RuntimeError("calendar is not reachable for this mailbox")
    return cal.service


def _calendar_events(mailbox: str, now: datetime) -> list[dict[str, Any]]:
    """The rep's events from LOOKBACK_DAYS ago to LOOKAHEAD_DAYS ahead, every page.

    Raises when the window does not fit in ``_MAX_CALENDAR_PAGES`` pages: a
    window silently cut short would read as "no upcoming meeting".
    """
    from rubik_core.tenancy.context import current_workspace

    key = (current_workspace(), mailbox)
    with _lock:
        hit = _cal_cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    service = _calendar_service(mailbox)
    utc_now = now.astimezone(timezone.utc)
    params: dict[str, Any] = {
        "calendarId": "primary",
        "timeMin": (utc_now - timedelta(days=LOOKBACK_DAYS)).isoformat(),
        "timeMax": (utc_now + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
        "singleEvents": True,
        "maxResults": 2500,
        "fields": _EVENT_FIELDS,
    }
    events: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(_MAX_CALENDAR_PAGES):
        page_params = dict(params, pageToken=token) if token else params
        page = service.events().list(**page_params).execute()
        events.extend(page.get("items") or [])
        token = page.get("nextPageToken")
        if not token:
            break
    else:
        raise RuntimeError(f"calendar window has more events than {PRODUCT_NAME} reads")
    with _lock:
        _sweep(_cal_cache)
        _cal_cache[key] = (time.monotonic(), events)
    return events


def _event_start(event: dict[str, Any]) -> datetime | None:
    """The event's start; an all-day event starts at local midnight."""
    start = event.get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A date-only (all-day) event: anchor it in the portal's timezone, so
        # it lands on the same day the rep sees on their calendar.
        from rubik_core.retrieval.hubspot_api import _tz  # local: heavy leaf

        dt = dt.replace(tzinfo=_tz())
    return dt


def _people(event: dict[str, Any], mailbox: str) -> list[dict[str, Any]] | None:
    """The event's human invitees, or None when the rep did not take part
    (the event is cancelled, or the rep declined it)."""
    if event.get("status") == "cancelled":
        return None
    me = mailbox.strip().lower()
    out: list[dict[str, Any]] = []
    for person in event.get("attendees") or []:
        addr = (person.get("email") or "").strip().lower()
        if not addr or person.get("resource") or addr.endswith(_NOT_PEOPLE):
            continue
        if addr == me or person.get("self"):
            if person.get("responseStatus") == "declined":
                return None
            continue
        out.append({"email": addr, "response": person.get("responseStatus") or ""})
    organizer = ((event.get("organizer") or {}).get("email") or "").strip().lower()
    if organizer and organizer != me and not organizer.endswith(_NOT_PEOPLE):
        if not any(p["email"] == organizer for p in out):
            # The organizer is not always in the guest list; their invite is
            # their own "yes".
            out.append({"email": organizer, "response": "accepted"})
    return out


def _calendar_touches(
    mailbox: str, deal_terms: dict[str, list[str]], now: datetime
) -> dict[str, tuple[datetime | None, datetime | None, datetime | None]]:
    """``{deal: (last_meeting, last_meeting_accepted, next_meeting)}``."""
    events = _calendar_events(mailbox, now)
    out: dict[str, tuple[datetime | None, datetime | None, datetime | None]] = {}
    for deal, terms in deal_terms.items():
        last = accepted = nxt = None
        if terms:
            for event in events:
                start = _event_start(event)
                people = _people(event, mailbox)
                if start is None or not people:
                    continue
                client = [
                    p
                    for p in people
                    if matches(p["email"], terms) and p["response"] != "declined"
                ]
                if not client:
                    continue
                if start <= now:
                    last = start if last is None or start > last else last
                    if any(p["response"] == "accepted" for p in client):
                        accepted = (
                            start if accepted is None or start > accepted else accepted
                        )
                else:
                    nxt = start if nxt is None or start < nxt else nxt
        out[deal] = (last, accepted, nxt)
    return out


# ── Entry points ─────────────────────────────────────────────────────────────


def collect(
    mailbox: str,
    deal_terms: dict[str, list[str]],
    *,
    now: datetime | None = None,
    gmail: bool = True,
    calendar: bool = True,
) -> SignalReport:
    """Gmail + Calendar touches for each deal, from ``mailbox`` only. Never raises.

    ``gmail`` / ``calendar`` False skips that source (a check that only needs
    "is a meeting booked" does not search the mailbox). A skipped source is
    reported as not read (``gmail_ok`` / ``calendar_ok`` False) with no note.
    """
    now = now or datetime.now(timezone.utc)
    report = SignalReport(gmail_ok=gmail, calendar_ok=calendar)
    mail: dict[str, tuple[datetime | None, datetime | None]] = {}
    cal: dict[str, tuple[datetime | None, datetime | None, datetime | None]] = {}
    if gmail:
        try:
            mail = _gmail_touches(mailbox, deal_terms, LOOKBACK_DAYS)
        except Exception as exc:  # noqa: BLE001 — an outage is a note, not a crash
            logger.warning("[rhythm] Gmail unreadable: %s", _safe(exc))
            report.gmail_ok = False
            report.notes.append(
                "Your Gmail could not be read just now, so emails you did not log "
                "in HubSpot are not counted this time."
            )
    if calendar:
        try:
            cal = _calendar_touches(mailbox, deal_terms, now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[rhythm] Calendar unreadable: %s", _safe(exc))
            report.calendar_ok = False
            report.notes.append(
                "Your calendar could not be read just now, so meetings are not "
                "counted this time."
            )
    for deal in deal_terms:
        last_in, last_out = mail.get(deal, (None, None))
        last_meeting, accepted, next_meeting = cal.get(deal, (None, None, None))
        report.touches[deal] = Touch(
            last_in=last_in,
            last_out=last_out,
            last_meeting=last_meeting,
            last_meeting_accepted=accepted,
            next_meeting=next_meeting,
        )
    return report


def day_activity(mailbox: str, start: datetime, end: datetime) -> DayActivity:
    """What the rep SENT to people outside the company, and the meetings they had with
    them, between ``start`` and ``end``. Recipients and subjects of sent mail;
    titles and invitees of meetings. Never raises."""
    own = _domain(mailbox)
    act = DayActivity()
    try:
        service = _gmail_service(mailbox)
        if service is None:
            raise GmailUnreadable("Gmail is not reachable for this mailbox")
        msgs = service.users().messages()
        q = (
            f"in:sent after:{int(start.timestamp())} before:{int(end.timestamp())} "
            "-in:chats -in:drafts"
        )
        ids: list[str] = []
        token: str | None = None
        for _ in range(_MAX_RECAP_PAGES):
            extra = {"pageToken": token} if token else {}
            page = msgs.list(
                userId="me",
                q=q,
                maxResults=100,
                fields="messages/id,nextPageToken",
                **extra,
            ).execute()
            ids.extend(m.get("id") for m in page.get("messages") or [] if m.get("id"))
            token = page.get("nextPageToken")
            if not token:
                break
        else:
            act.notes.append(
                f"You sent more than {len(ids)} emails today; the recap lists the "
                f"newest {len(ids)}."
            )
            act.gmail_ok = False
        got = (
            _run_batch(
                service,
                [
                    (
                        mid,
                        msgs.get(
                            userId="me",
                            id=mid,
                            format="metadata",
                            metadataHeaders=["To", "Cc", "Subject"],
                            fields="id,internalDate,payload/headers",
                        ),
                    )
                    for mid in ids
                ],
            )
            if ids
            else {}
        )
        for mid in ids:
            msg = got.get(mid) or {}
            headers = {
                (h.get("name") or "").lower(): h.get("value") or ""
                for h in (msg.get("payload") or {}).get("headers") or []
            }
            recipients = [
                addr.strip().lower()
                for _, addr in getaddresses(
                    [headers.get("to", ""), headers.get("cc", "")]
                )
                if "@" in addr
            ]
            external = [r for r in recipients if _domain(r) and _domain(r) != own]
            if not external:
                continue
            try:
                at = datetime.fromtimestamp(
                    int(msg.get("internalDate")) / 1000, tz=timezone.utc
                )
            except (TypeError, ValueError):
                at = None
            act.emails.append(
                {
                    "subject": headers.get("subject") or "(no subject)",
                    "to": external,
                    "at": at,
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[rhythm] Gmail day read failed: %s", _safe(exc))
        act.gmail_ok = False
        act.notes.append(
            "Your Gmail could not be read just now, so emails you did not log in "
            "HubSpot are not in this recap."
        )
    try:
        for event in _calendar_events(mailbox, end):
            at = _event_start(event)
            people = _people(event, mailbox)
            if at is None or not people or not (start <= at < end):
                continue
            outside = [
                p["email"]
                for p in people
                if _domain(p["email"]) != own and p["response"] != "declined"
            ]
            if outside:
                act.meetings.append(
                    {
                        "title": event.get("summary") or "Meeting",
                        "with": outside,
                        "at": at,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[rhythm] Calendar day read failed: %s", _safe(exc))
        act.calendar_ok = False
        act.notes.append(
            "Your calendar could not be read just now, so meetings you did not log "
            "in HubSpot are not in this recap."
        )
    return act
