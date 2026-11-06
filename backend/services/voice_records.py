"""A voice command that names one record → the page that shows it.

"Open Acme Corp's X-Ray", "open Thursday's Systems L10", "prep for my 2pm",
"open the Globex proposal", "open my chat about pricing", "open Jordan's work
style guide". The model names the kind of record
and the words that pick it (``rubik_core/voice/commands.py``, ``open_record``);
this module finds the records THIS person could open by clicking, with the same
gates the pages use, and answers with where to go:

* one clear match → its page (the browser opens it);
* several → a "which one?" list of links;
* none → a sentence, never a guess.

An L10 always asks first, even for one match: opening its page starts the live
meeting, or rejoins it. Nothing here writes, and the paths are built from fixed
route shapes and record ids, never from the model's words.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.services.voice_actions import _said, _words, best_matches, match_people
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings

logger = logging.getLogger(__name__)

MAX_PLACES = 4


@dataclass
class Place:
    """One page the command could mean."""

    path: str
    label: str
    detail: str = ""
    #: Navigation state the page reads (the X-Ray takes the company's name).
    state: dict[str, Any] | None = None


@dataclass
class Opening:
    places: list[Place] = field(default_factory=list)
    #: Asked when there is more than one place, or always for an L10.
    prompt: str | None = None
    #: Ask even when there is one place (opening it has an effect).
    ask_first: bool = False
    message: str | None = None


class _Refusal(Exception):
    """Why nothing can be opened, worded for the person."""


@dataclass
class _Ctx:
    user: UserProfile
    db: Session
    today: date
    tz: str
    #: Now, naive UTC (how the EOS tables store times).
    now: datetime


def _clock_text(moment: datetime) -> str:
    """ "Thu, Oct 8, 11:30 AM" for an aware moment, in its own zone."""
    return f"{moment:%a}, {moment:%b} {moment.day}, {moment:%I:%M %p}".replace(
        " 0", " "
    )


def _show_when(moment: datetime, tz: str) -> str:
    """A naive-UTC moment on the person's own clock."""
    return _clock_text(moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)))


def _pick(
    words: str, items: list, title_of: Callable[[Any], str], *, what: str
) -> list:
    matches = best_matches(words, items, title_of)
    if not matches:
        raise _Refusal(f"I couldn't find {what} that matches “{words}”.")
    return matches


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(_said(value)[:10]) if value else None
    except ValueError:
        return None


def _clock(value: Any) -> time | None:
    text = _said(value)
    try:
        hh, mm = text.split(":")[:2]
        return time(int(hh), int(mm[:2]))
    except (ValueError, TypeError):
        return None


def _opening(places: list[Place], prompt: str) -> Opening:
    return Opening(places=places, prompt=prompt if len(places) > 1 else None)


# ── Customer X-Ray ───────────────────────────────────────────────────────────


def _xray_allowed(user: UserProfile) -> bool:
    from backend.auth import user_can_access_xray

    return user_can_access_xray(user)


def _xray(ctx: _Ctx, args: dict) -> Opening:
    from rubik_core.xray import service as xray_service

    words = _said(args.get("name"))
    if not words:
        raise _Refusal(
            "Which customer? Say the company's name, like “open Acme Corp's X-Ray”."
        )
    # The Customers page's own search: live HubSpot, 0.5 to 2 seconds.
    rows = xray_service.customer_search(words, limit=8)
    if not rows:
        raise _Refusal(f"I couldn't find a company called “{words}” in HubSpot.")
    picked = (
        best_matches(words, rows, lambda r: r.get("name") or "") or rows[:MAX_PLACES]
    )
    return _opening(
        [
            Place(
                path=f"/customers/{quote(str(r['company_id']), safe='')}/xray",
                label=f"{r.get('name') or 'Company'} — Customer X-Ray",
                detail=" · ".join(
                    x for x in (r.get("domain"), r.get("client_status")) if x
                ),
                state={"name": r.get("name")},
            )
            for r in picked[:MAX_PLACES]
        ],
        "Which company?",
    )


# ── L10 meetings ─────────────────────────────────────────────────────────────


def _l10(ctx: _Ctx, args: dict) -> Opening:
    from backend.routers.eos._authz import accessible_meeting_ids
    from backend.routers.eos.me import _calendar_next_dt, _next_scheduled_dt
    from rubik_core.database.eos import L10Meeting

    ids = accessible_meeting_ids(ctx.user, ctx.db)
    meetings = (
        ctx.db.query(L10Meeting)
        .filter(L10Meeting.id.in_(ids), L10Meeting.is_deleted.is_(False))
        .all()
        if ids
        else []
    )
    if not meetings:
        raise _Refusal("You aren't on any L10 meetings yet.")

    def next_at(m) -> datetime | None:
        return _calendar_next_dt(m, now=ctx.now) or _next_scheduled_dt(
            m.day_of_week, m.start_time, now=ctx.now
        )

    words = _said(args.get("name"))
    day = _day(args.get("day"))
    picked = meetings
    if words:
        picked = _pick(
            words,
            meetings,
            lambda m: f"{m.name or ''} {m.department_name or ''}",
            what="an L10 of yours",
        )
    if day is not None:
        on_day = [m for m in picked if m.day_of_week == day.weekday()]
        if not on_day:
            raise _Refusal(f"None of your L10s meets on a {day:%A}.")
        picked = on_day
    if not words and day is None:
        # "my L10", "my next L10": the soonest one.
        dated = sorted((m for m in picked if next_at(m)), key=next_at)
        picked = dated[:1] or picked[:1]
    places = []
    for m in picked[:MAX_PLACES]:
        when = next_at(m)
        places.append(
            Place(
                path=f"/eos/meetings/{m.id}",
                label=m.name or f"{m.department_name or 'Team'} L10",
                detail=f"Next: {_show_when(when, ctx.tz)}" if when else "",
            )
        )
    return Opening(
        places=places,
        prompt="Opening an L10 starts the meeting, or rejoins it if it has started.",
        ask_first=True,
    )


# ── Meeting Prep ─────────────────────────────────────────────────────────────


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_start(event: dict, tz: str) -> datetime | None:
    raw = (event.get("start") or {}).get("dateTime")
    if not raw:
        return None  # all-day: nothing to prep at a time
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(ZoneInfo(tz))


def _event_words(event: dict) -> str:
    people = " ".join(
        f"{a.get('displayName') or ''} {(a.get('email') or '').split('@')[-1].split('.')[0]}"
        for a in event.get("attendees") or []
    )
    return f"{event.get('summary') or ''} {people}"


def _meeting_prep(ctx: _Ctx, args: dict) -> Opening:
    from rubik_core.tools.calendar import GoogleCalendarClient

    calendar = GoogleCalendarClient(ctx.user.email)
    tz = calendar.get_calendar_timezone(default=ctx.tz) or ctx.tz
    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    day = _day(args.get("day"))
    at = _clock(args.get("time"))
    if day is None and at is not None:
        # "my 2pm" is a time on the person's own today, the same day every other
        # record kind resolves against (``ctx.today``), not the server clock's.
        day = ctx.today
    if day is not None:
        start = datetime.combine(day, time(0, 0), zone)
        end = start + timedelta(days=1)
    else:
        start, end = now - timedelta(hours=1), now + timedelta(days=7)
    events = calendar.get_upcoming_events(
        max_results=100, time_min=_rfc3339(start), time_max=_rfc3339(end), time_zone=tz
    )
    timed = [
        (e, s) for e in events if e.get("id") and (s := _event_start(e, tz)) is not None
    ]
    if not timed:
        raise _Refusal("I couldn't find a meeting on your calendar then.")
    words = _said(args.get("name"))
    picked = timed
    if at is not None:
        said_at = datetime.combine(day or ctx.today, at, zone)
        close = [
            (e, s) for e, s in timed if abs((s - said_at).total_seconds()) <= 30 * 60
        ]
        if not close:
            when = f"{said_at:%I:%M %p}".lstrip("0")
            raise _Refusal(f"You have no meeting near {when}.")
        picked = sorted(close, key=lambda es: abs((es[1] - said_at).total_seconds()))
    if words:
        # A company said as words ("Acme Corp") is one word in an email
        # domain (acmecorp.example), so attendee addresses are also checked
        # for the words run together.
        joined = "".join(_words(words))
        by_address = [
            (e, s)
            for e, s in picked
            if len(joined) >= 4
            and any(
                joined in (a.get("email") or "").lower()
                for a in e.get("attendees") or []
            )
        ]
        by_words = best_matches(words, picked, lambda es: _event_words(es[0]))
        picked = by_words + [es for es in by_address if es not in by_words]
        if not picked:
            raise _Refusal(f"I couldn't find a meeting that matches “{words}”.")
    elif at is None:
        # "prep for my next meeting": the first one not yet over.
        upcoming = [(e, s) for e, s in picked if s >= now - timedelta(minutes=10)]
        picked = upcoming[:1] or picked[-1:]
    return _opening(
        [
            Place(
                path=f"/meeting-prep/{quote(str(e['id']), safe='')}",
                label=f"Prep: {e.get('summary') or 'Meeting'}",
                detail=_clock_text(s),
            )
            for e, s in picked[:MAX_PLACES]
        ],
        "Which meeting?",
    )


# ── Proposals ────────────────────────────────────────────────────────────────


def _proposals_allowed(user: UserProfile) -> bool:
    from backend.auth import user_can_access_proposals

    return settings.PROPOSALS_ENABLED and user_can_access_proposals(user)


def _proposal(ctx: _Ctx, args: dict) -> Opening:
    from rubik_core.proposals.service import list_proposals

    # The person's own, newest first, as the Proposals page lists them.
    proposals = list_proposals(ctx.db, actor_email=ctx.user.email)
    if not proposals:
        raise _Refusal("You don't have any proposals yet.")
    words = _said(args.get("name"))
    picked = (
        _pick(
            words,
            proposals,
            lambda p: f"{p.company_name or ''} {p.title or ''}",
            what="a proposal of yours",
        )
        if words
        else proposals[:1]
    )
    return _opening(
        [
            Place(
                path=f"/sales/proposals/{quote(str(p.id), safe='')}",
                label=p.title or p.company_name or "Proposal",
                detail=" · ".join(x for x in (p.company_name, p.status) if x),
            )
            for p in picked[:MAX_PLACES]
        ],
        "Which proposal?",
    )


# ── Ask Cubie conversations ──────────────────────────────────────────────────


def _chat(ctx: _Ctx, args: dict) -> Opening:
    from rubik_core.database.models import ChatSession

    sessions = (
        ctx.db.query(ChatSession)
        .filter(func.lower(ChatSession.user_email) == ctx.user.email.lower())
        .order_by(ChatSession.updated_at.desc())
        .limit(300)
        .all()
    )
    if not sessions:
        raise _Refusal(f"You don't have any Ask {PRODUCT_NAME} conversations yet.")
    words = _said(args.get("name"))
    picked = (
        _pick(words, sessions, lambda s: s.title or "", what="a conversation of yours")
        if words
        else sessions[:1]
    )
    return _opening(
        [
            Place(
                path=f"/chat?session={quote(str(s.id), safe='')}",
                label=s.title or "Conversation",
                detail=f"{s.updated_at:%b} {s.updated_at.day}" if s.updated_at else "",
            )
            for s in picked[:MAX_PLACES]
        ],
        "Which conversation?",
    )


# ── Smart Teams work style guides ────────────────────────────────────────────


def _guides_allowed(user: UserProfile) -> bool:
    from backend.auth import user_can_access_smart_teams

    return user_can_access_smart_teams(user)


@dataclass
class _Named:
    email: str
    first_name: str
    last_name: str


def _guide(ctx: _Ctx, args: dict) -> Opening:
    from rubik_core.database.models import User
    from rubik_core.smart_teams import authz as st_authz
    from rubik_core.smart_teams.guide import published_guide

    words = _said(args.get("name"))
    if not words or set(_words(words)) <= {"my", "me", "mine", "i", "own"}:
        return Opening(places=[Place(path="/smart-teams", label="My Work Style")])
    people = []
    for email, name in (
        ctx.db.query(User.email, User.name)
        .filter(User.is_platform_support.is_(False))
        .all()
    ):
        if not email or not name or not st_authz.is_employee(email):
            continue
        first, _, last = " ".join(name.split()).partition(" ")
        people.append(_Named(email=email.lower(), first_name=first, last_name=last))
    named = match_people(words, people)
    # Only a PUBLISHED guide is offered, and "no guide" reads the same whether
    # there is none or it is still a draft (the page answers 404 for both).
    with_guide = [p for p in named if published_guide(ctx.db, p.email) is not None]
    if not with_guide:
        raise _Refusal(f"I couldn't find a published work style guide for “{words}”.")
    return _opening(
        [
            Place(
                path=f"/smart-teams/guide/{quote(p.email, safe='@')}",
                label=f"{p.first_name} {p.last_name}".strip() + " — work style guide",
            )
            for p in with_guide[:MAX_PLACES]
        ],
        "Whose guide?",
    )


# ── The kinds, their gates, and the entry point ─────────────────────────────


def _always(_user: UserProfile) -> bool:
    return True


def _eos_allowed(_user: UserProfile) -> bool:
    from rubik_core.control.repository import module_enabled

    return module_enabled("eos")


@dataclass(frozen=True)
class RecordKind:
    key: str
    #: How the model is told what this kind is.
    description: str
    allowed: Callable[[UserProfile], bool]
    find: Callable[[_Ctx, dict], Opening]


KINDS: tuple[RecordKind, ...] = (
    RecordKind(
        "customer_xray",
        "a customer company's X-Ray (name = the company)",
        _xray_allowed,
        _xray,
    ),
    RecordKind(
        "l10_meeting",
        "an L10 meeting (name = the team or meeting; day when said, e.g. Thursday's)",
        _eos_allowed,
        _l10,
    ),
    RecordKind(
        "meeting_prep",
        "the prep for a meeting on the person's calendar (name = who or what it is "
        "with; time and day when said, e.g. my 2pm)",
        _always,
        _meeting_prep,
    ),
    RecordKind(
        "proposal",
        "one of the person's proposals (name = the company or title)",
        _proposals_allowed,
        _proposal,
    ),
    RecordKind(
        "chat",
        f"one of the person's past Ask {PRODUCT_NAME} conversations (name = its topic)",
        _always,
        _chat,
    ),
    RecordKind(
        "work_style_guide",
        "a colleague's Smart Teams work style guide (name = the person; leave it out "
        "for their own)",
        _guides_allowed,
        _guide,
    ),
)
_BY_KEY = {k.key: k for k in KINDS}

#: Where an opened record may be. Every path is built above from one of these
#: shapes and a record id; the router pins it again before it leaves.
PLACE_PREFIXES = (
    "/customers/",
    "/eos/meetings/",
    "/meeting-prep/",
    "/sales/proposals/",
    "/chat?session=",
    "/smart-teams/guide/",
)
# Pages opened without an id: your own work style page.
PLACE_EXACT = ("/smart-teams",)


def place_allowed(path: str) -> bool:
    return path in PLACE_EXACT or path.startswith(PLACE_PREFIXES)


def available_kinds(user: UserProfile) -> list[RecordKind]:
    """The kinds of record this person can open, by the pages' own gates."""
    out = []
    for kind in KINDS:
        try:
            if kind.allowed(user):
                out.append(kind)
        except Exception:  # noqa: BLE001 — a broken gate hides its kind
            logger.warning("[voice] record gate failed for %s", kind.key, exc_info=True)
    return out


def open_record(
    args: dict,
    user: UserProfile,
    db: Session,
    *,
    today: date,
    tz: str,
    now: datetime | None = None,
) -> Opening:
    """Where the record ``args`` names is, for this person. May call HubSpot or
    Google Calendar, so call it off the event loop.

    ``now`` (naive UTC) defaults to the clock; tests pass it alongside ``today``
    so "the next L10" does not depend on the day the suite runs."""
    kind = _BY_KEY.get(_said(args.get("kind")))
    if kind is None or kind not in available_kinds(user):
        return Opening(message=f"That isn't something you can open in {PRODUCT_NAME}.")
    ctx = _Ctx(
        user=user,
        db=db,
        today=today,
        tz=tz,
        now=now or datetime.now(timezone.utc).replace(tzinfo=None),
    )
    try:
        opening = kind.find(ctx, args or {})
    except _Refusal as refusal:
        return Opening(message=str(refusal))
    except (TypeError, ValueError, AttributeError):
        logger.warning("[voice] could not open %s from the model's arguments", kind.key)
        return Opening(
            message=f"{PRODUCT_NAME} couldn't read that command. Please try again."
        )
    opening.places = [p for p in opening.places if place_allowed(p.path)]
    if not opening.places and not opening.message:
        opening.message = f"{PRODUCT_NAME} couldn't find that."
    return opening
