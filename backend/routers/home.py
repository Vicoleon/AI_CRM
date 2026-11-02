import asyncio
import html
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from functools import partial
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from backend.jobs.launcher import (
    PRIORITIES_JOB_NAME,
    reject_support_background_work,
    submit_background_work,
)
from backend.services.gmail_service import classify_addressing, focus_sort_key
from backend.services.user_context import build_user_context_block
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import attribute_to
from rubik_core.database.db import SessionLocal, get_db
from rubik_core.database.models import HomeCard, User
from rubik_core.priorities import run_state
from rubik_core.priorities.models import EntityKind, filter_card_attrs
from rubik_core.priorities.temporal import (
    format_when_local,
    resolve_user_now,
    user_today_str,
)
from rubik_core.sales.flavour import is_sales_flavoured

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/homefeed", tags=["homefeed"])


import json as _json

import anyio


def _resolve_attendee_names(attendees: list[dict]) -> None:
    """Replace calendar usernames (e.g. "jdoe") with full names from the users
    table when available. Falls back to Google's displayName, then to the email.
    Mutates each attendee dict in place by setting `display_name`.
    """
    emails = {a.get("email", "").lower() for a in attendees if a.get("email")}
    emails.discard("")
    name_by_email: dict[str, str] = {}
    if emails:
        db = SessionLocal()
        try:
            rows = db.query(User.email, User.name).filter(User.email.in_(emails)).all()
            name_by_email = {email.lower(): name for email, name in rows if name}
        except Exception as e:
            logger.warning(f"Attendee name resolution failed: {e}")
        finally:
            db.close()

    for a in attendees:
        email = (a.get("email") or "").lower()
        resolved = name_by_email.get(email)
        if not resolved:
            resolved = (
                a.get("display_name") or a.get("displayName") or a.get("email") or ""
            )
        a["display_name"] = resolved


# ─── Models ───────────────────────────────────────────────────────────────────


class EmailSummary(BaseModel):
    thread_id: str
    subject: str
    sender: str
    snippet: str
    date: str
    is_unread: bool
    # How the user was addressed: "direct" (To), "cc", or "list" (distribution
    # list / bulk). The briefing drops "list" and ranks direct above cc.
    addressing: str = "direct"
    # Lightweight LLM triage: True when a single cheap Flash pass judged this
    # email likely to need the user's personal attention. Drives a "May be
    # important" badge in the Email view. Fail-open: False on any LLM error.
    may_be_important: bool = False
    importance_unavailable: bool = False


class Attendee(BaseModel):
    email: str
    display_name: str | None = None
    is_organizer: bool = False


class EventAttachment(BaseModel):
    file_id: str
    title: str
    mime_type: str
    file_url: str


class CalendarEvent(BaseModel):
    title: str
    start: str
    end: str
    all_day: bool = False
    event_id: str | None = None
    description: str | None = None
    location: str | None = None
    conference_url: str | None = None
    attendees: list[Attendee] = []
    attachments: list[EventAttachment] = []
    # The reader's own RSVP — "accepted" / "tentative" / "declined" /
    # "needs_action" / "none" (solo event or one they organized). Drives the
    # RSVP badge and hides the prep buttons on a meeting they declined.
    rsvp: str = "none"


class FreeSlot(BaseModel):
    date: str
    slots: list[str]  # e.g. ["9:00 AM – 12:00 PM", "2:00 PM – 5:00 PM"]


# UC-39 — ActionItem class removed alongside the _extract_action_items
# LLM call. The UC-20→25 AI Priorities pipeline now owns "what to act
# on" with proper cross-channel correlation, instead of an inferior
# gmail-only pass.


class BriefingResponse(BaseModel):
    generated_at: str
    emails: list[EmailSummary]
    events: list[CalendarEvent]
    free_slots: list[FreeSlot]


class MeetingPrepRequest(BaseModel):
    event: dict[str, Any]  # full CalendarEvent dict (title, attendees, description, …)
    focus_on: str | None = None  # optional user intent, e.g. "sell Product X"

    company_info: str | None = None  # present only for external attendees
    recommended_products: list[str] = []
    # A new version keeps the sections the reader edited by hand in the
    # current latest version (rubik_core/meeting_prep/versions.py).
    keep_edits: bool = True


class MeetingPrepResponse(BaseModel):
    title: str
    executive_summary: str = ""
    meeting_objective: str = ""
    client_profile: str = ""
    stakeholder_map: str = ""
    recent_interactions: str = ""
    past_meeting_context: str = ""
    product_fit_analysis: str = ""
    recommended_positioning: str = ""
    likely_objections: str = ""
    discovery_questions: list[str] = []
    suggested_agenda: list[str] = []
    talking_points: list[str] = []
    next_steps: list[str] = []
    # What the reader still owes an attendee (unanswered email, unkept
    # commitment). Internal/1:1 briefs only — populated from the email threads
    # where the counterparty's message is the most recent one.
    open_items_you_owe: list[str] = []
    source_evidence: list[str] = []
    partial_errors: list[str] = []
    recommended_products: list[str] = []
    # UC-47 — one researched card per EXTERNAL attendee: position, public
    # activity, and the read on their style (`AttendeeIntel.as_dict()` shape).
    # Copied from the research step, NOT generated by the briefing model: the
    # profile facts and post summaries carry citations and must reach the UI
    # exactly as researched. The model consumes this block; it never rewrites it.
    attendee_intel: list[dict[str, Any]] = []
    is_sales_user: bool = False
    is_internal: bool = False
    # Persisted MeetingPrepRun id — the handle the Word-download and
    # save-to-Assets buttons act on. None if persistence failed (best-effort).
    run_id: str | None = None

    # ── Newer signals (rubik_core/meeting_prep/signals.py) ──────────────────
    # The client's CRM + X-Ray picture for external meetings, built from the
    # records verbatim (never written by the model) so the numbers on the card
    # are the numbers in HubSpot. None when there is nothing to show.
    account_snapshot: dict[str, Any] | None = None
    # Which sources actually informed this brief, for the "Built from" strip:
    # [{"key": "hubspot", "label": "HubSpot", "detail": "2 open deals"}, …].
    signals_used: list[dict[str, Any]] = []
    # The meeting's start, already formatted in the reader's timezone.
    meeting_time: str | None = None

    # ── Version (rubik_core/meeting_prep/versions.py) ───────────────────────
    version: int | None = None
    latest_version: int | None = None
    is_latest: bool = True
    base_version: int | None = None  # the version this one was built on
    created_at: str | None = None  # ISO, naive UTC like the rest of the API
    # generated | regenerated | section_rewritten | section_edited | restored
    change_kind: str | None = None
    change_note: str | None = None
    changed_section: str | None = None
    # {section_key: {"origin": "edited"|"rewritten", "version": n, "at": iso,
    #                "instruction": str|None}}
    section_meta: dict[str, dict[str, Any]] = {}
    # True when the model call failed and this is the error placeholder, not a
    # brief. Endpoints use it to keep a failure from becoming a version.
    generation_failed: bool = False
    # A background build in flight for this meeting (e.g. a Regenerate the
    # reader started), rubik_core/meeting_prep/background.py:
    # {"run_id", "status": "queued"|"running", "kind": "first"|"regenerate",
    #  "started_at": iso}. None when nothing is being built.
    pending_run: dict[str, Any] | None = None


class MeetingPrepVersionSummary(BaseModel):
    """One row of a meeting's version list (no brief body)."""

    run_id: str
    version: int
    created_at: str | None = None
    change_kind: str | None = None
    change_note: str | None = None
    changed_section: str | None = None
    base_version: int | None = None
    is_latest: bool = False


# ─── AI Priorities (multi-agent pipeline) ─────────────────────────────────────


class PriorityItem(BaseModel):
    id: str
    title: str
    why_it_matters: str
    urgency: str  # "high" | "medium" | "low"
    source_channels: list[str]
    source_links: list[str] = []
    suggested_action: str = ""
    # UC-39 — entity tags exposed so the frontend can derive smart action
    # affordances (e.g. deal → "Open in HubSpot", transcript → "Generate
    # follow-up email"). UC-24 wrote these into HomeCard.metadata_json
    # already; we just stopped hiding them.
    priority_kind: str | None = None  # "follow_up_debt" | … | None (pre-UC-24)
    entity_kind: str | None = (
        None  # "deal" | "thread" | "event" | "transcript" | "person" | "company" | "document" | None
    )
    entity_id: str | None = None
    entity_label: str | None = None
    # Set only when this item is about a RECURRING calendar event: the series
    # id behind every occurrence's instance-specific entity_id. Its presence is
    # what makes the card show "Never prep this again" — dismissing a single
    # occurrence cannot silence next week's.
    series_entity_id: str | None = None
    # For a team-scoped EOS card (a direct report's item) surfaced to a
    # manager/exec: who the item belongs to, so the UI can offer a Slack
    # follow-up targeting them. None for the user's own items.
    target_email: str | None = None
    target_label: str | None = None
    # "priority" (default) or "recommendation" — a recommendation is an
    # overnight agent_recommendation surfaced by /priorities/more. The frontend
    # routes a recommendation's actions to the /agents/recommendations endpoints
    # rather than the priority-memory ones. "next_week" tags a Friday look-ahead
    # item merged in from the next-week report; its actions use the normal
    # priority-memory endpoints (same as "priority").
    origin: str = "priority"
    # For origin="next_week": the Monday (ISO date) the target week begins,
    # so the frontend badge can label "week of …".
    week_of: str | None = None
    # Stable natural signal ids (calendar event id, Gmail thread id, …) used
    # server-side to dedup next-week items against daily ones. Harmless to
    # expose; empty for pre-existing cards that didn't project it.
    signal_ids: list[str] = []
    # EVERY entity this card speaks for, primary first — a card that aggregates
    # siblings ("two overdue To-Dos") carries them all. The server has always
    # had these on HomeCard.metadata_json; projecting them lets the UI tell a
    # one-item card from a multi-item one. Empty for pre-covered-ids cards,
    # which yield just the primary.
    covered_entity_ids: list[str] = []
    covered_entity_labels: list[str] = []
    # Ownership PER covered entity, positional against ``covered_entity_ids``.
    # A card can mix the reader's item with a colleague's — siblings are
    # grouped by situation, not by owner — so the card-level ``is_own`` below
    # cannot answer for a sibling. ``None`` in a slot means the card never
    # carried the flag, which is "unknown", never "not yours".
    covered_entity_is_own: list[bool | None] = []
    # Does the READER own the thing this card is about? A manager legitimately
    # gets cards about a direct report's EOS items ("check in with Sam"). The
    # pipeline never persists ``is_own``; it stamps ``target_email`` when (and
    # only when) the item belongs to someone else, so this is derived from that
    # (see :func:`_is_own_item`). Defaults True, which is what every card
    # without a target has always meant.
    is_own: bool = True
    # The card's own renderable facts — today only My To-Dos cards carry any.
    # Keys are constrained by ``CARD_ATTR_ALLOWLIST`` at both ends of
    # persistence and again in :func:`_priority_dict_from_metadata`, so this
    # stays an open dict here rather than a second, drifting schema.
    #
    # It has to be DECLARED, not merely projected: this response model is
    # applied to the read path (``PrioritiesResponse(**cached)``), so an
    # undeclared key is silently dropped on the way out, while both the writer
    # and the reader look correct in isolation.
    #
    # ``None`` (not ``{}``) when the card carries no facts: the panel renders
    # nothing at all in that case.
    attrs: dict[str, Any] | None = None


class PrioritiesResponse(BaseModel):
    generated_at: str
    headline: str
    priorities: list[PriorityItem]
    revision_count: int
    last_critique_score: float | None = None
    # How many priorities today's card was generated with, BEFORE the UC-25
    # read-path memory filter. Lets the UI distinguish "you cleared your list"
    # (generated_count > 0 and none remain → kudos) from "nothing surfaced
    # today" (generated_count == 0). ``priorities`` is the post-filter remainder.
    generated_count: int = 0


class PrioritiesRequestBody(BaseModel):
    # All None/omitted → fall back to the user's saved AI-Personalization
    # prefs (then org defaults). A value here is an explicit per-run override.
    focus: str | None = None
    max_priorities: int | None = None
    channels: list[str] | None = None  # subset of gmail/calendar/meet/hubspot/drive
    force_refresh: bool = False  # if false, return today's stored bundle when present


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _fetch_emails(user_email: str) -> list[dict]:
    """Fetch the user's unread *inbox* threads (only).

    Scoped to `in:inbox` so mail the user's own Gmail filters routed elsewhere
    (archived / skip-inbox / auto-forwarded — e.g. a busy distribution list)
    never surfaces. Over-fetches so the later distribution-list/bulk drop in
    `generate_briefing` doesn't starve the view.

    Returns ONLY unread mail — there is deliberately no "recent read" fallback.
    Replaying already-read threads when the inbox is clear reads like a stale
    cache to users; instead the Email view shows an "all caught up" empty state.
    """
    from rubik_core.tools.gmail import GmailClient

    client = GmailClient(user_email=user_email)
    return client.search_threads(query="is:unread in:inbox", max_results=25)


def _fetch_calendar(user_email: str) -> dict:
    """Fetch events (next `_CAL_HORIZON_DAYS`) and compute free slots (next 4 days)."""
    from rubik_core.tools.calendar import GoogleCalendarClient

    client = GoogleCalendarClient(user_email=user_email)

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=4)
    horizon = now + timedelta(days=_CAL_HORIZON_DAYS)

    # Calendar API expects RFC3339 with Z suffix — avoid +00:00 which URL-encodes badly
    now_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_z = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    horizon_z = horizon.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Bound the event list by a fixed date horizon, not just an event count:
    # a packed calendar (e.g. an exec's) would otherwise exhaust a small
    # `max_results` within a few days and the week grid would stop there, while
    # a sparse calendar spans weeks. `max_results` is only a safety ceiling now.
    events = client.get_upcoming_events(
        max_results=250, time_min=now_z, time_max=horizon_z
    )
    avail = client.get_availability(start_time=now_z, end_time=end_z)
    # The 9am-6pm work window has to be built in the user's own timezone, not
    # UTC — otherwise a Pacific/Eastern user's real meetings fall outside the
    # UTC window and the day looks free (see _compute_free_slots).
    tz = client.get_calendar_timezone()

    return {"events": events, "availability": avail, "start": now, "end": end, "tz": tz}


# Free-slot work window + granularity. The window is interpreted in the user's
# calendar timezone, never UTC.
_WORK_START_HOUR = 9
_WORK_END_HOUR = 18
_MIN_SLOT = timedelta(minutes=30)
_DAYS_AHEAD = 4
# How far ahead the calendar event list reaches. Free slots stay at
# `_DAYS_AHEAD`; the rendered week grid shows events out to this horizon so a
# busy calendar isn't truncated to the first few days (a fixed event-count cap
# made an exec's week grid stop ~5 days out while a light one spanned ~2 weeks).
_CAL_HORIZON_DAYS = 14
_NO_SLOTS_SENTINEL = "Fully booked or no data"


def _parse_event_dt(value: str | None) -> datetime | None:
    """Parse an RFC3339 datetime string into an aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # defensive — Google always sends an offset
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_busy_interval(
    event: dict, tzinfo: timezone | ZoneInfo
) -> tuple[datetime, datetime] | None:
    """Absolute (start, end) busy interval for an event, or None if it should
    not count as busy.

    Skips events the user declined or marked "Free" (transparent). All-day
    events block the whole day (Google's end.date is exclusive). Returns
    timezone-aware datetimes.
    """
    # Declined → not busy.
    for a in event.get("attendees", []) or []:
        if a.get("self") and a.get("responseStatus") == "declined":
            return None
    # Marked "Free" → not busy.
    if event.get("transparency") == "transparent":
        return None

    start_info = event.get("start", {}) or {}
    end_info = event.get("end", {}) or {}

    s_dt_str = start_info.get("dateTime")
    if s_dt_str:  # timed event
        s_dt = _parse_event_dt(s_dt_str)
        if not s_dt:
            return None
        e_dt = _parse_event_dt(end_info.get("dateTime")) or (s_dt + timedelta(hours=1))
    else:  # all-day event: start.date / end.date, end exclusive
        s_date_str = start_info.get("date")
        if not s_date_str:
            return None
        try:
            s_date = date.fromisoformat(s_date_str)
            e_date_str = end_info.get("date")
            e_date = (
                date.fromisoformat(e_date_str)
                if e_date_str
                else s_date + timedelta(days=1)
            )
        except ValueError:
            return None
        s_dt = datetime.combine(s_date, dtime(0, 0), tzinfo)
        e_dt = datetime.combine(e_date, dtime(0, 0), tzinfo)

    if e_dt <= s_dt:
        return None
    return (s_dt, e_dt)


def _compute_free_slots(
    events: list[dict],
    start: datetime,
    end: datetime,
    tz: timezone | ZoneInfo | None = None,
    now: datetime | None = None,
    work_start_t: dtime | None = None,
    work_end_t: dtime | None = None,
) -> list[FreeSlot]:
    """Compute free slots per day inside the user's working-hours window.

    ``tz`` is the user's calendar timezone (defaults to UTC). ``now`` is the
    reference instant used to drop already-elapsed time from *today's* window
    so we never advertise a slot in the past. ``work_start_t`` / ``work_end_t``
    are the user's configured working hours (General tab); they default to
    9 AM – 6 PM. Both event times and the work window are normalized to ``tz``
    so labels and gap math agree.
    """
    tzinfo = tz or timezone.utc
    now = (now or datetime.now(timezone.utc)).astimezone(tzinfo)
    start_local = start.astimezone(tzinfo)
    ws_t = work_start_t or dtime(_WORK_START_HOUR, 0)
    we_t = work_end_t or dtime(_WORK_END_HOUR, 0)

    busy_intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        interval = _event_busy_interval(event, tzinfo)
        if interval:
            busy_intervals.append(interval)

    free_slots: list[FreeSlot] = []
    for day_offset in range(_DAYS_AHEAD):
        day = (start_local + timedelta(days=day_offset)).date()
        work_start = datetime.combine(day, ws_t, tzinfo)
        work_end = datetime.combine(day, we_t, tzinfo)
        date_label = day.strftime("%A, %B %-d")

        # Can't book the past: for today, start the window at "now".
        window_start = work_start
        if day == now.date() and now > work_start:
            window_start = now
        if window_start >= work_end:
            # Work day is already over (or no bookable window left today).
            free_slots.append(FreeSlot(date=date_label, slots=[_NO_SLOTS_SENTINEL]))
            continue

        # Clip every busy interval to this day's bookable window.
        day_busy: list[tuple[datetime, datetime]] = []
        for s_dt, e_dt in busy_intervals:
            cs = max(s_dt.astimezone(tzinfo), window_start)
            ce = min(e_dt.astimezone(tzinfo), work_end)
            if ce > cs:
                day_busy.append((cs, ce))
        day_busy.sort()

        # Merge overlapping/adjacent busy intervals (handles double-booked or
        # back-to-back meetings that would otherwise create phantom gaps).
        merged: list[tuple[datetime, datetime]] = []
        for cs, ce in day_busy:
            if merged and cs <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], ce))
            else:
                merged.append((cs, ce))

        slots: list[str] = []
        cursor = window_start
        for cs, ce in merged:
            if cs - cursor >= _MIN_SLOT:
                slots.append(
                    f"{cursor.strftime('%-I:%M %p')} – {cs.strftime('%-I:%M %p')}"
                )
            cursor = max(cursor, ce)
        if work_end - cursor >= _MIN_SLOT:
            slots.append(
                f"{cursor.strftime('%-I:%M %p')} – {work_end.strftime('%-I:%M %p')}"
            )

        free_slots.append(
            FreeSlot(
                date=date_label,
                slots=slots if slots else [_NO_SLOTS_SENTINEL],
            )
        )

    return free_slots


def _parse_email_summary(thread: dict, user_email: str = "") -> EmailSummary | None:
    """Extract EmailSummary from a raw Gmail thread dict.

    ``user_email`` classifies how the user was addressed (To / Cc / list).
    """
    msgs = thread.get("messages", [])
    if not msgs:
        return None
    # Use most recent message
    msg = msgs[-1]
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    label_ids = msg.get("labelIds", [])
    lower_headers = {k.lower(): v for k, v in headers.items()}

    # Parse date header
    date_header = headers.get("Date", "")
    date_str = date_header[:16] if date_header else ""

    return EmailSummary(
        thread_id=thread.get("id", ""),
        subject=headers.get("Subject", "(no subject)"),
        sender=headers.get("From", "unknown"),
        snippet=msg.get("snippet", "")[:200],
        date=date_str,
        is_unread="UNREAD" in label_ids,
        addressing=classify_addressing(lower_headers, user_email),
    )


@attribute_to("email")
async def _flag_important_emails(
    emails: list[EmailSummary], user_first_name: str
) -> None:
    """Best-effort: flag which emails likely need the user's personal attention.

    One cheap batched Flash call over the already-filtered inbox view; mutates
    each email's ``may_be_important`` in place. Fail-open — on ANY error every
    flag stays False (no badge shown), so the briefing never degrades or 500s
    on this optional signal.
    """
    if not emails:
        return

    blocks = []
    for i, e in enumerate(emails):
        addressed = "Cc'd" if e.addressing == "cc" else "directly (To)"
        blocks.append(
            f"[{i}] Addressed: {addressed}\n"
            f"From: {e.sender}\n"
            f"Subject: {e.subject}\n"
            f"Snippet: {e.snippet}"
        )
    joined = "\n\n---\n\n".join(blocks)

    prompt = (
        "You are triaging a busy professional's inbox. For each email below, decide "
        "if it is likely IMPORTANT — something that plausibly needs their personal "
        "attention, a decision, or a reply from them specifically.\n\n"
        f"User's first name: {user_first_name or '(unknown)'}\n\n"
        "Mark important=true for: a real person asking them to do / decide / review / "
        "reply to something, time-sensitive requests, escalations, or messages where "
        "they are the clear owner. Mark important=false for: newsletters, FYI "
        "broadcasts, automated notifications, receipts, and mail where they are only "
        "loosely looped in.\n\n"
        'Output ONLY a JSON object with an "items" array, one object per email in input order, with keys '
        "index (int) and important (bool). No prose.\n\n"
        f"{joined}"
    )

    try:
        from rubik_core.config import settings
        from rubik_core.inference_workloads import run_text_async
        from rubik_core.models import get_genai_client

        async def managed(route):
            response = await get_genai_client().aio.models.generate_content(
                model=route.model,
                contents=prompt,
            )
            return response.text or ""

        raw = await run_text_async(
            "email_importance",
            prompt,
            default_model=settings.VERTEX_LLM_MODEL_FLASH,
            managed=managed,
            json_mode=True,
        )
        raw = raw.strip()
        raw = (
            raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
        data = _json.loads(raw) if raw else []
        if isinstance(data, dict):
            data = data.get("items")
        if not isinstance(data, list):
            raise ValueError("AI triage shape is invalid")
    except Exception:
        from rubik_core.ai_providers.registry import model_routes

        try:
            selected = model_routes().get("email_importance") or {}
            unavailable = selected.get("provider") not in (None, "vertex_managed")
        except Exception:
            unavailable = True
        if unavailable:
            for email in emails:
                email.importance_unavailable = True
        logger.warning("Email importance triage failed")
        return

    for d in data:
        if not isinstance(d, dict):
            continue
        try:
            idx = int(d["index"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= idx < len(emails):
            emails[idx].may_be_important = bool(d.get("important"))


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/generate", response_model=BriefingResponse)
async def generate_briefing(
    user: UserProfile = Depends(get_current_user),
):
    """
    Generate a real-time daily briefing:
    - Emails (unread first, fall back to recent read)
    - AI-extracted action items
    - Calendar events + computed free slots for next 4 days
    """
    try:
        # Fetch Gmail and Calendar in parallel (blocking calls in threads)
        email_task = anyio.to_thread.run_sync(_fetch_emails, user.email)
        cal_task = anyio.to_thread.run_sync(_fetch_calendar, user.email)

        raw_threads, cal_data = await asyncio.gather(
            email_task, cal_task, return_exceptions=True
        )

        # Parse emails, then keep only mail addressed to the user (To/Cc) and
        # rank direct (To) above Cc. Distribution-list / bulk mail is dropped:
        # a user on a busy list doesn't want every list message in their view.
        emails = []
        if raw_threads and not isinstance(raw_threads, Exception):
            for t in raw_threads:
                summary = _parse_email_summary(t, user.email)
                if summary and summary.addressing != "list":
                    emails.append(summary)
            emails.sort(key=lambda e: focus_sort_key(e.addressing, e.is_unread))
            emails = emails[:12]

            # Lightweight LLM triage: flag emails that likely need the user's
            # personal attention (drives the "May be important" badge). One
            # cheap Flash call, fail-open so the briefing never depends on it.
            first_name = (user.name or "").split()[0] if user.name else ""
            await _flag_important_emails(emails, first_name)

        # UC-39 — removed _extract_action_items LLM call; the UC-20→25
        # AI Priorities pipeline now owns "what to act on" with proper
        # cross-channel correlation, instead of an inferior gmail-only pass.

        # Parse calendar events
        from rubik_core.tools.calendar import is_placeholder_hold, self_response_status

        events = []
        raw_events = []
        if cal_data and not isinstance(cal_data, Exception):
            raw_events = cal_data.get("events", [])
            for ev in raw_events:
                # Opaque self-holds ("Busy"/"Blocked"/"Focus", Outlook free/busy
                # sync, private secondary-calendar blocks) are not meetings to
                # show or prep — skip them for display. They stay in
                # `raw_events`, so the free-slot math below still counts them as
                # busy time.
                if is_placeholder_hold(ev):
                    continue
                start_info = ev.get("start", {})
                end_info = ev.get("end", {})
                s = start_info.get("dateTime") or start_info.get("date", "")
                e = end_info.get("dateTime") or end_info.get("date", "")

                # Extract attendees
                raw_attendees = ev.get("attendees", [])
                attendees = [
                    Attendee(
                        email=a.get("email", ""),
                        display_name=a.get("displayName"),
                        is_organizer=a.get("organizer", False),
                    )
                    for a in raw_attendees
                    if a.get("email")
                ]

                # Extract conference URL (Google Meet / Zoom / etc.)
                conf_url: str | None = None
                conf_data = ev.get("conferenceData", {})
                for ep in conf_data.get("entryPoints", []):
                    if ep.get("entryPointType") == "video":
                        conf_url = ep.get("uri")
                        break

                raw_attachments = ev.get("attachments", [])
                event_attachments = []
                for a in raw_attachments:
                    if "fileId" in a:
                        event_attachments.append(
                            EventAttachment(
                                file_id=a["fileId"],
                                title=a.get("title", ""),
                                mime_type=a.get("mimeType", ""),
                                file_url=a.get("fileUrl", ""),
                            )
                        )

                events.append(
                    CalendarEvent(
                        title=ev.get("summary", "(untitled)"),
                        start=s,
                        end=e,
                        all_day="date" in start_info and "dateTime" not in start_info,
                        event_id=ev.get("id"),
                        description=ev.get("description"),
                        location=ev.get("location"),
                        conference_url=conf_url,
                        attendees=attendees,
                        attachments=event_attachments,
                        rsvp=self_response_status(ev, user.email),
                    )
                )

        # Compute free slots. Respect the user's configured working hours
        # (General tab) so the strip never advertises slots outside them.
        from backend.routers.calendar import _user_working_hours

        wh_start, wh_end = _user_working_hours(user.email)
        free_slots = []
        if cal_data and not isinstance(cal_data, Exception):
            try:
                tzinfo = ZoneInfo(cal_data.get("tz") or "UTC")
            except Exception:
                tzinfo = timezone.utc
            cal_start = cal_data.get("start", datetime.now(timezone.utc))
            free_slots = _compute_free_slots(
                raw_events,
                cal_start,
                cal_data.get("end", datetime.now(timezone.utc) + timedelta(days=4)),
                tz=tzinfo,
                now=cal_start,
                work_start_t=wh_start,
                work_end_t=wh_end,
            )
        else:
            # Fallback for slot computation if calendar failed
            free_slots = _compute_free_slots(
                [],
                datetime.now(timezone.utc),
                datetime.now(timezone.utc) + timedelta(days=4),
                work_start_t=wh_start,
                work_end_t=wh_end,
            )

        return BriefingResponse(
            generated_at=datetime.now(timezone.utc).isoformat(),
            emails=emails,
            events=events,
            free_slots=free_slots,
        )

    except Exception as e:
        logger.exception(f"Failed to generate briefing for {user.email}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Meeting Prep ─────────────────────────────────────────────────────────────


def _fetch_emails_for_domains(
    user_email: str, domains: list[str], max_threads: int = 5
) -> list[dict]:
    """Search the user's Gmail for recent threads from any of the given external domains."""
    from rubik_core.tools.gmail import GmailClient

    try:
        client = GmailClient(user_email=user_email)
        # Build a query like: from:(@acme.com OR @corp.io)
        domain_clauses = " OR ".join(f"@{d}" for d in domains)
        query = (
            f"from:({domain_clauses})" if len(domains) > 1 else f"from:@{domains[0]}"
        )
        threads = client.search_threads(query=query, max_results=max_threads)
        return threads or []
    except Exception as e:
        logger.warning(f"Could not fetch emails for domains {domains}: {e}")
        return []


def _fetch_emails_with_people(
    user_email: str, people: list[str], max_threads: int = 6
) -> list[dict]:
    """Recent threads involving any of ``people``, in EITHER direction.

    Complements :func:`_fetch_emails_for_domains` (which is company-wide and
    inbound-only). Two gaps this closes for meeting prep:

    * An INTERNAL meeting has no external domain, so the domain search returned
      nothing at all — a 1:1 briefing had zero email context and read "no
      historical context or related documents were found".
    * ``from:`` alone can't surface a thread the user themselves sent, so
      "emails I owe this person feedback on" was structurally invisible.
    """
    from rubik_core.tools.gmail import GmailClient

    addrs = [p.strip() for p in people if p and p.strip()]
    if not addrs:
        return []
    try:
        client = GmailClient(user_email=user_email)
        # Threads where the person is sender OR recipient, so an unanswered
        # message in either direction shows up.
        clauses = " OR ".join(
            f"from:{a} OR to:{a} OR cc:{a}"
            for a in addrs[:5]  # bound the query
        )
        threads = client.search_threads(
            query=f"({clauses}) -in:spam -in:trash", max_results=max_threads
        )
        return threads or []
    except Exception as e:  # noqa: BLE001 — prep must survive a Gmail miss
        logger.warning(f"Could not fetch emails with {addrs}: {e}")
        return []


def _merge_threads(*groups: list[dict]) -> list[dict]:
    """Concatenate thread lists, keeping first-seen order, deduped by thread id."""
    seen: set[str] = set()
    out: list[dict] = []
    for group in groups:
        for t in group or []:
            tid = str(t.get("id") or "")
            key = tid or str(id(t))
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
    return out


def _awaiting_reply_from_user(thread: dict, user_email: str) -> bool:
    """True when the newest message in ``thread`` came from somebody else.

    Surfaced in the prompt so the briefing can name what the reader still owes
    this person — a 1:1 brief that leaves out "the emails I owe them feedback
    on" misses the point of the meeting.
    """
    msgs = thread.get("messages") or []
    if not msgs:
        return False
    headers = {
        (h.get("name") or "").lower(): (h.get("value") or "")
        for h in (msgs[-1].get("payload", {}).get("headers") or [])
    }
    return (user_email or "").strip().lower() not in (headers.get("from", "")).lower()


def _format_email_threads_for_prompt(
    threads: list[dict], max_chars: int = 3000, user_email: str = ""
) -> str:
    """Turn raw Gmail thread dicts into a compact, token-friendly text block.

    When ``user_email`` is supplied, each thread is tagged with whether the
    reader still owes a reply so the brief can raise it as an open item.
    """
    parts = []
    for t in threads:
        msgs = t.get("messages", [])
        if not msgs:
            continue
        msg = msgs[-1]  # most recent message in thread
        headers = {
            h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
        }
        subject = headers.get("Subject", "(no subject)")
        sender = headers.get("From", "unknown")
        date = headers.get("Date", "")[:16]
        snippet = msg.get("snippet", "")[:400]
        status = ""
        if user_email and _awaiting_reply_from_user(t, user_email):
            status = "\nStatus: their message is the most recent — you have not replied"
        parts.append(
            f"[{date}] From: {sender}\nSubject: {subject}{status}\nSnippet: {snippet}"
        )
        if sum(len(p) for p in parts) > max_chars:
            break
    return "\n\n---\n\n".join(parts) if parts else "(none found)"


# ── Phase 0 of the customer relationship graph (docs/customer_relationship_graph.md):
# a deterministic "prior meetings with these attendees" block for prep, read from
# meeting_participants — attendance records, not semantic retrieval.
_PRIOR_MEETINGS_LIMIT = 8
# An invite bigger than this would match half the company's meeting history;
# fall back to external attendees only, and skip entirely if still bigger.
_PRIOR_MEETINGS_MAX_COUNTERPARTS = 15


def _format_prior_meetings_block(shared) -> str:
    """Render the attendance-record block for the prep prompt.

    Returns "" when there is nothing to show — the same discipline as the
    ATTENDEE INTELLIGENCE block: no block → no clauses referencing it → the
    prompt stays byte-identical to a run without the feature.
    """
    if not shared:
        return ""
    lines = [
        "## PRIOR MEETINGS WITH THESE ATTENDEES (attendance records — treat as ground truth)"
    ]
    newest = shared[0]
    if newest.when:
        lines.append(
            f"You last met someone on this invite on {newest.when.strftime('%A, %B %d, %Y')}."
        )
    for m in shared:
        when = m.when.strftime("%A, %B %d, %Y") if m.when else "date unknown"
        lines.append(
            f'- {when} — "{m.title or m.doc_id}" — with {", ".join(m.overlap)}'
        )
    lines.append(
        f"(up to {_PRIOR_MEETINGS_LIMIT} most recent; dates are ingest dates, "
        "normally within a day of the meeting)"
    )
    return "\n".join(lines)


def _outside_attendees(
    attendees: list[dict], internal_domains: frozenset[str]
) -> list[dict]:
    """Only attendees outside all verified workspace domains need client prep."""
    return [
        attendee
        for attendee in attendees
        if "@" in (email := attendee.get("email", ""))
        and email.rsplit("@", 1)[-1].lower() not in internal_domains
    ]


async def collect_meeting_prep_context(
    user_email: str,
    user_department: str,
    user_roles: list[str],
    event: dict[str, Any] | None = None,
    event_id: str | None = None,
    focus_on: str | None = None,
    product_to_emphasize: str | None = None,
    *,
    reader: UserProfile | None = None,
) -> dict[str, Any]:
    """Gather all necessary context for meeting prep: calendar, attendees, emails,
    RAG docs, product catalog, and the newer signals (HubSpot account record,
    Customer X-Ray, open to-dos, last meeting's notes, team time off, today's
    flagged priorities — see ``rubik_core/meeting_prep/signals.py``).

    ``reader`` is the signed-in user's profile, used only for the per-feature
    gates of the reader-gated sources (X-Ray, to-dos). When None (the Deep Prep
    job), it is loaded from the users table by email; if that fails, those
    sources are skipped.
    """
    from rubik_core.adk_agent.tools import AgentTools
    from rubik_core.tools.calendar import GoogleCalendarClient

    agent_tools = AgentTools(user_email=user_email)

    # 1. Get Event Details
    if not event and event_id:
        try:
            cal_client = GoogleCalendarClient(user_email=user_email)
            event = cal_client.get_event(event_id)
        except Exception as e:
            logger.error(f"Failed to fetch event {event_id}: {e}")
            raise HTTPException(status_code=404, detail=f"Event {event_id} not found.")

    if not event:
        raise HTTPException(
            status_code=400, detail="Either event or event_id must be provided."
        )

    # UC-26 — keep the event_id in context so generate_meeting_prep_analysis
    # can resolve the matching UC-25 PREP_DEBT after a successful run.
    resolved_event_id = event.get("id") or event.get("event_id") or event_id

    title = event.get("title") or event.get("summary", "(untitled)")
    start_info = event.get("start")
    if isinstance(start_info, dict):
        start = start_info.get("dateTime") or start_info.get("date", "")
        start_all_day = "date" in start_info and "dateTime" not in start_info
    else:
        start = start_info or ""
        start_all_day = False
    end = (
        event.get("end", {}).get("dateTime")
        if isinstance(event.get("end"), dict)
        else event.get("end", "")
    )

    # Pre-format the meeting time in the reader's timezone. The prompt below
    # must NOT hand the LLM a raw offset-bearing ISO string to turn into a
    # clock time — that drifted "11:00 AM" into "1:00 PM" on the homepage.
    start_local: str | None = None
    try:
        start_local = format_when_local(
            start, resolve_user_now(user_email), all_day=start_all_day
        )
    except Exception:  # noqa: BLE001 — a formatting miss must never break prep
        logger.debug(
            "meeting-prep: local start formatting failed for %s",
            user_email,
            exc_info=True,
        )
    description = event.get("description") or ""
    location = event.get("location") or ""
    attendees: list[dict] = event.get("attendees", [])

    # Resolve calendar usernames (e.g. "jdoe") to real names from our user directory.
    _resolve_attendee_names(attendees)

    # Picks the brief's sales flavour (battle brief, product match), not who may
    # read it: compared by department slug, like every department check.
    is_sales_user = is_sales_flavoured(user_department, user_roles)

    # Verified hosted domains, including secondary domains, define colleagues.
    from starlette.concurrency import run_in_threadpool

    from rubik_core.company.domain import workspace_domains

    internal_domains = await run_in_threadpool(workspace_domains)
    external_attendees = _outside_attendees(attendees, internal_domains)

    external_domains = list(
        {a["email"].split("@")[-1] for a in external_attendees if "email" in a}
    )

    context = {
        "event": event,
        "event_id": resolved_event_id,
        "title": title,
        "start": start,
        "start_local": start_local,
        "end": end,
        "description": description,
        "location": location,
        "attendees": attendees,
        "external_domains": external_domains,
        "is_sales_user": is_sales_user,
        "focus_on": focus_on,
        "product_to_emphasize": product_to_emphasize,
        "errors": [],
    }

    # Everyone on the invite except the reader — the people whose mail actually
    # matters for this meeting. Internal colleagues included: on an internal 1:1
    # they are the ONLY counterparty, and gating the Gmail fetch on
    # `external_domains` meant those briefings fetched no email whatsoever.
    counterparty_emails = [
        (a.get("email") or "").strip()
        for a in attendees
        if (a.get("email") or "").strip().lower() != user_email.strip().lower()
        and (a.get("email") or "").strip()
    ]

    # 2. Parallel Data Collection
    async def fetch_emails():
        try:
            # Company-wide inbound context (external meetings only) PLUS
            # per-person two-way threads (always). The person search is what
            # surfaces "you still owe them a reply".
            with_people = await anyio.to_thread.run_sync(
                _fetch_emails_with_people, user_email, counterparty_emails, 6
            )
            by_domain: list[dict] = []
            if external_domains:
                by_domain = await anyio.to_thread.run_sync(
                    _fetch_emails_for_domains, user_email, external_domains, 5
                )
            return _merge_threads(with_people, by_domain)
        except Exception as e:
            context["errors"].append(f"Gmail failure: {e}")
            return []

    async def fetch_past_meetings():
        try:
            query_str = title
            if external_domains:
                query_str += " " + " ".join(external_domains)
            res = await agent_tools.search_past_meetings(query=query_str, max_results=3)
            return res.get("report") if res.get("status") == "success" else ""
        except Exception as e:
            context["errors"].append(f"Meeting transcript RAG failure: {e}")
            return ""

    def _do_fetch_drive_docs():
        try:
            import re

            from rubik_core.tools.drive import GoogleDriveClient

            drive_client = GoogleDriveClient(user_email=user_email)
            search_text = description + " " + location
            drive_links = re.findall(
                r"https?://(?:docs|drive)\.google\.com/(?:document|spreadsheets|presentation|file)/d/([a-zA-Z0-9_-]+)",
                search_text,
            )
            attachments = event.get("attachments", [])

            docs_texts = []
            for att in attachments:
                fid = att.get("file_id") or att.get("fileId")
                mtype = att.get("mime_type") or att.get("mimeType")
                if fid:
                    try:
                        text = drive_client.export_document_text(fid, mtype)
                        if text:
                            docs_texts.append(f"Doc: {att.get('title')}\n{text[:2000]}")
                    except Exception:
                        pass

            for fid in drive_links:
                try:
                    text = drive_client.export_document_text(fid)
                    if text:
                        docs_texts.append(f"Linked Doc: {fid}\n{text[:2000]}")
                except Exception:
                    pass

            return "\n\n---\n\n".join(docs_texts)
        except Exception as e:
            logger.error(f"Drive fetch failure: {e}")
            return ""

    async def fetch_drive_docs():
        return await anyio.to_thread.run_sync(_do_fetch_drive_docs)

    async def fetch_products():
        try:
            from rubik_core.retrieval.product_catalog import get_product_catalog

            catalog = get_product_catalog()
            search_query = product_to_emphasize or focus_on or title
            matches = catalog.search_products(search_query)
            return matches
        except Exception as e:
            context["errors"].append(f"Product catalog failure: {e}")
            return []

    async def fetch_attendee_intel():
        """UC-47 — who these external people are and how to talk to them.

        Runs for every reader, not just Sales: a Systems lead meeting a vendor's
        architect needs the same "what does this person actually work on" read.
        Internal-only meetings do no research at all (nothing to look up, and we
        don't profile colleagues), so the whole step is skipped when there are no
        external attendees. Fail-soft: research problems land in
        ``context["errors"]`` and surface as partial_errors, never as a 500.
        """
        if not external_attendees:
            return []
        try:
            from rubik_core.people import research_attendees

            result = await research_attendees(external_attendees, user_email=user_email)
            context["errors"].extend(result.errors)
            return result.as_dicts()
        except Exception as e:  # noqa: BLE001 - a briefing must survive this
            logger.warning("Attendee intel failure: %s", e, exc_info=True)
            context["errors"].append(f"Attendee research failure: {e}")
            return []

    async def fetch_prior_meetings():
        """Phase 0 of the relationship graph — when did the reader ACTUALLY last
        meet these people, from meeting_participants (the attendance ACL),
        instead of hoping the semantic transcript search surfaces it. ACL-safe
        by construction: only meetings the reader attended can match. Fail-soft:
        a lookup problem lands in partial_errors, never breaks the briefing.
        """
        from rubik_core.config import settings

        if not settings.CUBIE_PREP_ATTENDANCE_LOOKUP:
            return []
        try:
            emails = counterparty_emails
            if len(emails) > _PRIOR_MEETINGS_MAX_COUNTERPARTS:
                emails = [
                    (a.get("email") or "").strip()
                    for a in external_attendees
                    if (a.get("email") or "").strip()
                ]
            if not emails or len(emails) > _PRIOR_MEETINGS_MAX_COUNTERPARTS:
                return []
            from rubik_core.database.repository import (
                SQLMeetingParticipantsRepository,
            )

            repo = SQLMeetingParticipantsRepository()
            return await anyio.to_thread.run_sync(
                repo.list_shared_meetings,
                user_email,
                emails,
                _PRIOR_MEETINGS_LIMIT,
            )
        except Exception as e:  # noqa: BLE001 — prep must survive this
            context["errors"].append(f"Attendance lookup failure: {e}")
            return []

    # ── Meeting Prep v2: the newer signals (rubik_core/meeting_prep/signals.py).
    # Each source has its own kill switch and stage budget, fails soft into a
    # short partial-error line, and only adds a prompt block when it found
    # something. Sources that depend on another one (X-Ray and to-dos on the
    # company match, last meeting's notes on the attendance lookup) await that
    # step as a shared task, so nothing is fetched twice.
    from rubik_core.config import settings as _settings
    from rubik_core.meeting_prep import signals as prep_signals

    crm_domains, crm_skipped = prep_signals.crm_domains(attendees, user_email)
    internal_emails = prep_signals.internal_attendee_emails(attendees, user_email)

    def _signal_failed(what: str, exc: BaseException) -> None:
        if isinstance(exc, asyncio.TimeoutError):
            context["errors"].append(f"{what} took too long and was skipped.")
        else:
            logger.warning("meeting-prep signal failed: %s", what, exc_info=exc)
            context["errors"].append(f"{what} was unavailable for this brief.")

    async def resolve_signal_reader():
        if reader is not None:
            return reader
        if not prep_signals.reader_gated_sources_enabled():
            return None
        try:
            return await prep_signals.run_bounded(
                prep_signals.resolve_reader, user_email
            )
        except Exception:  # noqa: BLE001 — no profile: gated sources skip
            return None

    async def resolve_crm_companies():
        if not crm_domains or not prep_signals.crm_configured():
            return {}
        if not (_settings.CUBIE_PREP_CRM_ENABLED or _settings.CUBIE_PREP_XRAY_ENABLED):
            return {}
        try:
            return await prep_signals.run_bounded(
                prep_signals.resolve_companies, crm_domains, user_email
            )
        except Exception as e:  # noqa: BLE001
            _signal_failed("The HubSpot company lookup", e)
            return {}

    reader_task = asyncio.ensure_future(resolve_signal_reader())
    companies_task = asyncio.ensure_future(resolve_crm_companies())
    prior_task = asyncio.ensure_future(fetch_prior_meetings())

    async def fetch_crm_records():
        if not _settings.CUBIE_PREP_CRM_ENABLED:
            return None
        matches = await companies_task
        if not matches:
            return None
        try:
            records = await prep_signals.run_bounded(
                prep_signals.fetch_account_records, crm_domains, matches
            )
        except Exception as e:  # noqa: BLE001
            _signal_failed("The HubSpot account record", e)
            return None
        context["errors"].extend(records.errors)
        return records

    async def fetch_xray():
        if not _settings.CUBIE_PREP_XRAY_ENABLED:
            return {}
        matches = await companies_task
        ids = [m.company.hubspot_company_id for m in matches.values() if m.matched]
        if not ids:
            return {}
        rdr = await reader_task
        try:
            # X-Ray's audience is narrower than Meeting Prep's.
            allowed = await prep_signals.run_bounded(
                prep_signals.reader_allows, "customer_xray", rdr
            )
            if not allowed:
                return {}
            return await prep_signals.run_bounded(
                prep_signals.fetch_xray_summaries, ids
            )
        except Exception as e:  # noqa: BLE001
            _signal_failed("The Customer X-Ray lookup", e)
            return {}

    async def fetch_open_promises():
        if not _settings.CUBIE_PREP_PROMISES_ENABLED or not counterparty_emails:
            return []
        rdr = await reader_task
        if rdr is None:
            return []
        try:
            allowed = await prep_signals.run_bounded(prep_signals.promises_allowed, rdr)
            if not allowed:
                return []
            return await prep_signals.run_bounded(
                prep_signals.fetch_open_promises, user_email
            )
        except Exception as e:  # noqa: BLE001
            _signal_failed("The To-Do lookup", e)
            return []

    async def fetch_last_meeting_notes():
        if not _settings.CUBIE_PREP_LAST_MEETING_NOTES_ENABLED:
            return []
        shared = await prior_task
        if not shared:
            return []
        try:
            return await prep_signals.run_bounded(
                prep_signals.read_last_meeting_notes,
                user_email,
                shared,
                title=title,
                attendees=attendees,
            )
        except Exception as e:  # noqa: BLE001
            _signal_failed("The last-meeting notes lookup", e)
            return []

    async def fetch_time_off():
        if not _settings.CUBIE_PREP_AVAILABILITY_ENABLED or not internal_emails:
            return []
        try:
            return await prep_signals.run_bounded(
                prep_signals.time_off_for_meeting, internal_emails, user_email, start
            )
        except Exception as e:  # noqa: BLE001
            _signal_failed("The team time-off lookup", e)
            return []

    async def fetch_flagged():
        if not _settings.CUBIE_PREP_FLAGGED_ENABLED or not counterparty_emails:
            return None
        try:
            return await prep_signals.run_bounded(_load_today_priorities, user_email)
        except Exception as e:  # noqa: BLE001
            _signal_failed("The AI Priorities lookup", e)
            return None

    # Gather all
    (
        email_threads,
        past_meetings,
        drive_context,
        matched_products,
        attendee_intel,
        prior_meetings,
        _signal_reader,
        crm_matches,
        crm_records,
        xray_by_company,
        open_promises,
        last_meeting_notes,
        time_off,
        priorities_bundle,
    ) = await asyncio.gather(
        fetch_emails(),
        fetch_past_meetings(),
        fetch_drive_docs(),
        fetch_products(),
        fetch_attendee_intel(),
        prior_task,
        reader_task,
        companies_task,
        fetch_crm_records(),
        fetch_xray(),
        fetch_open_promises(),
        fetch_last_meeting_notes(),
        fetch_time_off(),
        fetch_flagged(),
    )

    is_internal = len(external_attendees) == 0

    context.update(
        {
            "recent_emails": email_threads,
            "past_meetings_report": past_meetings,
            "drive_context": drive_context,
            "matched_products": matched_products,
            "attendee_intel": attendee_intel,
            "prior_meetings": prior_meetings,
            "prior_meetings_block": _format_prior_meetings_block(prior_meetings),
            "is_sales_user": is_sales_user,
            "is_internal": is_internal,
        }
    )
    try:
        context.update(
            prep_signals.assemble(
                user_email=user_email,
                attendees=attendees,
                event_id=resolved_event_id,
                start=start,
                crm_domains=crm_domains,
                skipped_domains=crm_skipped,
                matches=crm_matches,
                records=crm_records,
                xrays=xray_by_company,
                promises=open_promises,
                last_meeting_notes=last_meeting_notes,
                time_off=time_off,
                priorities_bundle=priorities_bundle,
            )
        )
    except Exception:  # noqa: BLE001 — the brief must survive a formatting bug
        logger.exception("meeting-prep: assembling the newer signals failed")
        context.update(
            {
                "signal_blocks": {},
                "account_snapshot": None,
                "crm_provenance": None,
            }
        )
    # "Built from" counts the threads the prompt will carry, not those fetched.
    context["email_threads_used"] = prep_signals.email_threads_in_block(
        _format_email_threads_for_prompt(email_threads, user_email=user_email)
    )
    context["signals_used"] = prep_signals.compute_signals_used(context)

    return context


@attribute_to("meeting_prep")
async def generate_meeting_prep_analysis(
    context: dict[str, Any],
    user_email: str,
    personalization_block: str = "",
    wgll_context: str = "",
) -> MeetingPrepResponse:
    """Synthesize collected context into a structured MeetingPrepResponse.

    `personalization_block` is the UC-11 user-context string (empty if the
    user hasn't saved preferences). When present, it's appended to whichever
    system instruction this run uses so the briefing reflects the user's voice.

    `wgll_context` is the WGLL feedback block — "what good looks like" learned
    from prior good/meh/bad ratings on meeting-prep briefings
    (`rubik_core.feedback.wgll.build_wgll_context`, surface="meeting_prep").
    Empty string = no change; it's appended after the personalization block.
    """
    from google.genai import types

    from rubik_core.config import settings
    from rubik_core.models import get_genai_client

    model_id = settings.VERTEX_LLM_MODEL_PRO

    is_sales_user = context.get("is_sales_user", False)
    is_internal = context.get("is_internal", False)

    # Build blocks for prompt
    attendees_list = []
    for a in context["attendees"]:
        name = a.get("display_name") or a.get("email", "Unknown")
        attendees_list.append(f"- {name} <{a.get('email', '')}>")

    products_block = ""
    if is_sales_user and not is_internal and context["matched_products"]:
        from rubik_core.meeting_prep.rewrite import (
            PRODUCT_KNOWLEDGE_HEADING,
            product_knowledge_lines,
        )

        products_block = f"\n{PRODUCT_KNOWLEDGE_HEADING}\n" + "\n".join(
            product_knowledge_lines(context["matched_products"])
        )

    email_block = _format_email_threads_for_prompt(
        context["recent_emails"], user_email=user_email
    )

    # UC-47 — public professional research on the external attendees. Empty
    # string for internal meetings (nobody researched), and also when the
    # feature is off or research failed.
    from rubik_core.people.prompt import format_intel_block_from_dicts

    intel_block = format_intel_block_from_dicts(context.get("attendee_intel"))
    has_intel = bool(intel_block)

    # Every instruction that points AT the block is conditional on the block
    # existing. Telling a model to consult a section that isn't in its prompt
    # invites it to invent the section's contents — and it also has to leave the
    # no-research path behaving exactly as it did before UC-47, since that is
    # what the feature flag being off must mean.
    intel_role_clause = (
        " Where ATTENDEE INTELLIGENCE gives a researched position for that "
        'email, use it verbatim as "role" instead of guessing, and let their '
        'public priorities shape "context".'
        if has_intel
        else ""
    )
    intel_talking_clause = (
        " Where ATTENDEE INTELLIGENCE shows what an attendee has publicly "
        "written or said, build the opening around THAT — quote or reference "
        "their own framing so it lands as informed rather than as a cold pitch."
        if has_intel
        else ""
    )
    intel_evidence_clause = (
        " Include the ATTENDEE INTELLIGENCE URLs behind any claim you make "
        "about a person."
        if has_intel
        else ""
    )
    # The external-research mandate has two forms. With research in hand, search
    # is redirected to what it does NOT cover (the company). Without it, the
    # original mandate stands unchanged — otherwise disabling the feature would
    # leave the sales brief worse off than before it existed.
    external_research_mandate = (
        """The people on the invite have already been researched for you — see ATTENDEE INTELLIGENCE above, which is cited and preferred over anything you would re-derive. Use the google_search tool for what is NOT covered there: the COMPANY (recent news, strategic shifts, regulatory exposure, leadership changes), and any attendee the block reports no presence for. Do not contradict the researched positions; if search disagrees, say so rather than silently picking one."""
        if has_intel
        else """For every attendee with an external domain (outside the company), you MUST use the google_search tool to research the individual (career history, LinkedIn activity, public talks) and their company (recent news, strategic shifts)."""
    )
    # The grounding rule for the block, likewise only when there is a block.
    intel_grounding_rule = (
        "\n- ATTENDEE INTELLIGENCE is the researched public record on the "
        "external people in the room (colleagues are never researched). Its "
        'position/activity lines are cited — use them freely. Its "Read" line '
        "is an inference carrying a confidence: at low confidence, do NOT "
        "characterize the person's style or motives. Never attribute a statement "
        "to someone that is not listed there."
        if has_intel
        else ""
    )

    # Phase 0 relationship graph — the attendance-record block from
    # collect_meeting_prep_context. Same discipline as the intel block: when
    # empty, neither the section nor any instruction pointing at it exists, so
    # the prompt is byte-identical to a run without the feature.
    prior_meetings_block = context.get("prior_meetings_block") or ""
    prior_meetings_section = f"{prior_meetings_block}\n" if prior_meetings_block else ""
    prior_meetings_grounding_rule = (
        "\n- PRIOR MEETINGS WITH THESE ATTENDEES comes from attendance records, "
        "not retrieval: its dates, titles, and who-was-there facts are "
        'authoritative — prefer them for any "last met" or "we have met N '
        'times" claim. What was said in those meetings must still come from '
        "transcript excerpts or other sources; the list proves the meetings "
        "happened, not their content."
        if prior_meetings_block
        else ""
    )

    # Meeting Prep v2 — the newer signal blocks (CRM record, X-Ray, last
    # meeting's notes, open to-dos, team time off, flagged priorities). Every
    # string here is "" unless the block it points at exists, so a run with none
    # of the new data yields a prompt byte-identical to the one before them.
    from rubik_core.meeting_prep.signals import prompt_parts

    sig = prompt_parts(
        context.get("signal_blocks"), battle_brief=is_sales_user and not is_internal
    )

    # ─── Case 1: Elite Sales Battle Brief (External Meetings for Sales Reps) ───
    if is_sales_user and not is_internal:
        from rubik_core.company.profile import get_company_profile
        from rubik_core.meeting_prep.rewrite import about_company, battle_persona

        seller = get_company_profile()
        system_instruction = f"""{battle_persona(seller)} Your goal is to prepare a "Battle Brief" for an upcoming meeting with a prospect or client.
Focus on GAP SELLING, identifying their pain points, and positioning our solutions as the definitive cure. Provide a strong Point of View (POV).

{about_company(seller)}## GROUNDING RULES (these override the persona — a false fact is worse than a thin brief)
- Every FACT about the client, the relationship, or its history must be traceable to the RESEARCH DATA below. Never state whether a past bid, proposal, deal, or engagement was won, lost, renewed, active, or pending unless a source explicitly says so.
- Do NOT infer outcomes or backstory from tone or phrasing. "Better positioned this time" or "a deeper analysis" is NOT evidence that a prior effort failed — if the record does not state an outcome, do not assert one.
- Transcript excerpts are dated; treat them as historical. Never present something said in a past meeting as the current state of the relationship.
- Keep your strategic judgment in the editorial fields (recommended_positioning, likely_objections, discovery_questions, talking_points, next_steps). The factual fields (executive_summary, client_profile, recent_interactions, past_meeting_context) must contain only what the sources support.
- When the data is insufficient for a field, say so plainly (e.g. "No record of a prior engagement in the available data") instead of inventing. An honest gap is useful; a confident fabrication destroys trust.
- Every fact you assert about the relationship must be backed by an entry in source_evidence.{intel_grounding_rule}{prior_meetings_grounding_rule}{sig["grounding"]}"""

        prompt = f"""Create a deep intelligence briefing for: {context["title"]}

## MEETING CONTEXT
- Description: {context["description"]}
- Location: {context["location"]}
- Time: {context.get("start_local") or context.get("start") or "N/A"}
- Focus Area: {context["focus_on"] or "Not specified"}

When you reference the meeting time, copy the Time value above verbatim; never reformat a time from an ISO/offset string.

## ATTENDEES
{chr(10).join(attendees_list)}

## RESEARCH DATA
{sig["research"]}{prior_meetings_section}{context["past_meetings_report"]}
{email_block}
{context["drive_context"]}
{products_block}

{intel_block}

## OUTPUT STRUCTURE
Return a JSON object with these EXACT keys:
- executive_summary: A grounded summary of what the provided data actually establishes about this meeting and relationship. State only facts present in the sources; do not infer deal history or outcomes. If little is known, say so plainly.
- meeting_objective: The single most important outcome we want from this meeting.
- client_profile: Analysis of the company's current state, scale, and market position.{sig["client_profile"]}
- stakeholder_map: A JSON ARRAY of objects, one per person in the ## ATTENDEES section. Each object MUST have keys: "name" (string), "email" (string), "role" (string, their title/function), "context" (string, 1-2 sentences on their agenda and influence for THIS meeting).{intel_role_clause} CRITICAL: Only include people in the ## ATTENDEES list. Do NOT include anyone from contextual data (emails/transcripts) who is not explicitly listed as an attendee.
- recent_interactions: Summary of the most important recent emails or touchpoints.
- past_meeting_context: Insights from previous transcripts or RAG docs.{sig["past_meeting_context"]}
- product_fit_analysis: How our solutions map to their specific pain points.
- recommended_positioning: How to frame the conversation (The POV).
- likely_objections: 3-5 specific pushbacks they might give and how to handle them.
- discovery_questions: 5-7 deep, non-obvious questions.
- suggested_agenda: A proposed 30-60 minute flow.
- talking_points: Specific talk tracks for opening, problem statements, and the "why us" part.{intel_talking_clause}{sig["talking_points"]}
- next_steps: 3-5 concrete action items to propose at the end.{sig["next_steps"]}{sig["open_items"]}
- source_evidence: List of specific sources used.{intel_evidence_clause}

## EXTERNAL RESEARCH MANDATE
{external_research_mandate}

IMPORTANT: Every list field (agenda, talking points, questions, etc.) MUST be a JSON array of separate strings. Do NOT return one long string with line breaks.

No markdown fences. Raw JSON only.
"""

    # ─── Case 2: Executive Productivity Assistant (Internal/1:1 or Non-Sales) ───
    else:
        system_instruction = f"""You are a World-Class Executive Assistant. Your goal is to prepare a productivity brief for an internal meeting or 1:1.
Focus on operational efficiency, project status, relationship management, and ensuring no balls are dropped.
Be concise, actionable, and observant of past commitments.

## GROUNDING RULES (non-negotiable — a false fact is worse than a thin brief)
- Every factual statement about people, projects, clients, or history must be traceable to the CONTEXTUAL DATA below. Never assert whether a past bid, deal, proposal, or engagement was won, lost, renewed, active, or pending unless a source explicitly says so.
- Do NOT infer backstory or outcomes from tone or phrasing ("better positioned this time" does not mean a prior effort failed). If the record does not state it, do not claim it.
- Transcript excerpts are dated — treat them as historical, not the current state.
- When the data is insufficient for a field, say so plainly (e.g. "Not enough in the available records to say") rather than inventing. An honest gap is useful; a confident fabrication destroys trust.
- Anything you present as fact should be backed by an entry in source_evidence.{intel_grounding_rule}{prior_meetings_grounding_rule}{sig["grounding"]}"""

        meeting_type = "1:1" if len(context["attendees"]) <= 2 else "Internal Meeting"

        prompt = f"""Create a {meeting_type} briefing for: {context["title"]}

## MEETING CONTEXT
- Description: {context["description"]}
- Time: {context.get("start_local") or context.get("start") or "N/A"}

When you reference the meeting time, copy the Time value above verbatim; never reformat a time from an ISO/offset string.

## ATTENDEES
{chr(10).join(attendees_list)}

## CONTEXTUAL DATA
{sig["research"]}{prior_meetings_section}{context["past_meetings_report"]}

### RECENT EMAIL WITH THESE PEOPLE
{email_block}

{context["drive_context"]}

{intel_block}

## OUTPUT STRUCTURE
Return a JSON object with these EXACT keys:
- executive_summary: A grounded summary of this {meeting_type}'s purpose, based on the calendar entry and the contextual data provided. State only what the sources support; do not infer history, outcomes, or stakes that are not evidenced. If little is known, say so plainly.
- open_items_you_owe: A JSON ARRAY of things the reader still owes an attendee, drawn ONLY from email threads above marked "you have not replied" and from unfulfilled commitments in the transcripts. Name the thread subject and what is being waited on. Empty array if the sources show nothing outstanding — never invent an obligation.{sig["open_items"]}
- meeting_objective: The intended goal for this session.
- client_profile: (Rename as "Relationship Context") Summary of your history with these individuals.{sig["client_profile"]}
- stakeholder_map: A JSON ARRAY of objects, one per person in ## ATTENDEES. Each object MUST have keys: "name" (string), "email" (string), "role" (string, their title/function), "context" (string, 1-2 sentences on what they care about and their dynamic in this meeting).{intel_role_clause} DO NOT include anyone from history who is not in the ## ATTENDEES list.
- recent_interactions: Summary of the last few emails or messages.
- past_meeting_context: What was discussed or decided in the last {meeting_type}.{sig["past_meeting_context"]}
- product_fit_analysis: (Rename as "Project/Status Context") Ongoing projects or blockers.
- recommended_positioning: (Rename as "Communication Style") Advice on how to approach this specific person/group.
- likely_objections: (Rename as "Potential Friction") Anticipated pushback or difficult topics.
- discovery_questions: (Rename as "Key Questions") Items you need clarity on.
- suggested_agenda: Structured flow for the {meeting_type}.
- talking_points: Specific items to bring up or status updates to give.{sig["talking_points"]}
- next_steps: 3-5 concrete action items to leave with.{sig["next_steps"]}
- source_evidence: List of sources used.

IMPORTANT: Every list field (agenda, talking points, questions, etc.) MUST be a JSON array of separate strings. Do NOT return one long string with line breaks.

No markdown fences. Raw JSON only.
"""

    if personalization_block:
        system_instruction = f"{system_instruction}\n\n{personalization_block}"
    if wgll_context:
        system_instruction = f"{system_instruction}\n\n{wgll_context}"

    try:
        # Only allow external web research when the meeting actually involves
        # external parties. Internal meetings must rely on supplied context.
        external_research = is_sales_user and not is_internal
        from rubik_core.ai_providers.registry import resolve_model_route
        from rubik_core.ai_providers.runtime import (
            generate_grounded_text_on_route,
            generate_text_on_route,
        )

        workload = (
            "meeting_prep_external" if external_research else "meeting_prep_internal"
        )
        route = await asyncio.to_thread(
            resolve_model_route,
            workload,
            "grounding" if external_research else "json",
            default_model=model_id,
        )
        if route.provider == "vertex_managed":
            tools = (
                [types.Tool(google_search=types.GoogleSearch())]
                if external_research
                else None
            )
            res = await get_genai_client().aio.models.generate_content(
                model=route.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=tools,
                    temperature=0.2,
                ),
            )
            answer = res.text or ""
        elif external_research:
            answer = (
                await asyncio.to_thread(
                    generate_grounded_text_on_route,
                    route,
                    prompt,
                    system_instruction=system_instruction,
                    temperature=0.2,
                )
            ).text
        else:
            answer = (
                await asyncio.to_thread(
                    generate_text_on_route,
                    route,
                    prompt,
                    json_mode=True,
                    system_instruction=system_instruction,
                    temperature=0.2,
                )
            ).text
        raw = (
            answer.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        data = _json.loads(raw)

        # --- Normalization Logic ---
        def _to_str(v):
            if isinstance(v, str):
                return v
            if v is None:
                return ""
            if isinstance(v, (dict, list)):
                if isinstance(v, dict):
                    parts = [
                        f"**{k.replace('_', ' ').title()}**: {val}"
                        for k, val in v.items()
                    ]
                    return " | ".join(parts)
                if isinstance(v, list):
                    return "\n".join([f"- {item}" for item in v])
                return _json.dumps(v)
            return str(v)

        # stakeholder_map must reach the frontend as a JSON-encoded array string
        # (the UI calls JSON.parse on it). Don't run it through _to_str, which
        # would flatten dicts to Python repr.
        sm = data.get("stakeholder_map")
        if isinstance(sm, list):
            data["stakeholder_map"] = _json.dumps(sm)
        elif isinstance(sm, str):
            pass
        else:
            data["stakeholder_map"] = "[]"

        for k in [
            "executive_summary",
            "meeting_objective",
            "client_profile",
            "recent_interactions",
            "past_meeting_context",
            "product_fit_analysis",
            "recommended_positioning",
            "likely_objections",
        ]:
            data[k] = _to_str(data.get(k))

        # List fields (ensure each item is a string and try to split if LLM returned a single block)
        for key in [
            "discovery_questions",
            "suggested_agenda",
            "talking_points",
            "next_steps",
            "open_items_you_owe",
            "source_evidence",
        ]:
            raw_val = data.get(key)
            if isinstance(raw_val, str):
                # Split on common list markers (1., -, etc) if they follow a newline or start of string
                parts = re.split(r"\n\d+\.\s+|\n-\s+|^\d+\.\s+|^-\s+", raw_val)
                items = [p.strip() for p in parts if p.strip()]
                data[key] = items if len(items) > 1 else [raw_val]
            elif isinstance(raw_val, list):
                data[key] = [_to_str(item) for item in raw_val]
            else:
                data[key] = []

        data["partial_errors"] = context["errors"]
        data["title"] = context["title"]
        # UC-47 — the attendee cards come from the research step verbatim, never
        # from `data`: these are cited observations, and letting the briefing
        # model re-emit them is exactly how a citation gets detached from the
        # claim it supports. Overwrite whatever the model may have volunteered.
        data["attendee_intel"] = context.get("attendee_intel") or []
        data["recommended_products"] = (
            [p.get("name") for p in context["matched_products"]]
            if is_sales_user and not is_internal
            else []
        )
        data["is_sales_user"] = is_sales_user
        data["is_internal"] = is_internal
        # Meeting Prep v2 — the client card and the "Built from" strip come from
        # the records, never from the model (same rule as the attendee cards).
        data["account_snapshot"] = context.get("account_snapshot")
        data["signals_used"] = list(context.get("signals_used") or [])
        data["meeting_time"] = context.get("start_local")

        # UC-26 — auto-resolve the matching UC-25 PREP_DEBT for this event so
        # the priorities pipeline stops re-firing prep reminders once the
        # user has actually run meeting prep. Best-effort: never block the
        # response on a memory hiccup.
        _resolve_prep_debt(user_email, context.get("event_id"))

        return MeetingPrepResponse(**data)
    except Exception:
        logger.warning("Meeting Prep analysis generation failed")
        return MeetingPrepResponse(
            title=context["title"],
            executive_summary=(
                "Error generating analysis: check this workspace's AI model route "
                "and provider."
            ),
            partial_errors=context["errors"] + ["AI model request failed"],
            # Research already succeeded and is already paid for — a failed
            # synthesis call shouldn't also cost the reader the attendee cards.
            attendee_intel=context.get("attendee_intel") or [],
            account_snapshot=context.get("account_snapshot"),
            signals_used=list(context.get("signals_used") or []),
            meeting_time=context.get("start_local"),
            generation_failed=True,
        )


def _prep_kind(is_sales: bool, is_internal: bool) -> str:
    return "sales_battle_brief" if (is_sales and not is_internal) else "internal_ea"


# How many times a generated brief is rebuilt on a newer latest version when
# another tab or device saved one while it was being stored.
_PREP_PERSIST_ATTEMPTS = 3


def _persist_meeting_prep_run(
    db: Session,
    user_email: str,
    ctx: dict[str, Any],
    resp: MeetingPrepResponse,
    *,
    event_id: str | None = None,
    keep_edits: bool = True,
    pending_row=None,
    request: dict[str, Any] | None = None,
):
    """Store one generated brief + the exact inputs it was built from as the
    meeting's next version. Best-effort; returns the new ``MeetingPrepRun``
    row, or ``None`` if persisting failed.

    Never raises: persistence failing must not fail the user's request. Storing
    the context bundle (not just the answer) is what makes a grounding
    regression reproducible, lets us audit already-delivered briefs for the
    same failure class that produced the "previous unsuccessful bid" error, and
    gives a later one-section rewrite the same evidence the brief was built on.

    * A successful brief becomes version max+1 for (reader, meeting), built on
      the current latest. With ``keep_edits`` the sections the reader edited by
      hand in that latest version are carried over unchanged. If another tab
      saves a version between reading the latest and storing this one, the
      brief is rebuilt on that newer version (up to ``_PREP_PERSIST_ATTEMPTS``
      times), so a concurrent hand edit is carried forward, never dropped.
    * A failed generation (``resp.generation_failed``) is kept for audit with
      no version number, so it never becomes what the reader lands on.

    ``event_id`` overrides ``ctx["event_id"]`` as the chain key (the
    background build passes the id the page asked for).

    ``pending_row``: a background build's pending row
    (``rubik_core/meeting_prep/background.py``) — finalized IN PLACE as the
    version (``delivery_status`` "ready"), or as the failed row, instead of a
    new row being inserted. ``request`` (what was asked) is stored in the
    bundle under ``"request"``.
    """
    from rubik_core.config import settings
    from rubik_core.meeting_prep import versions
    from rubik_core.meeting_prep.provenance import context_visible
    from rubik_core.meeting_prep.rewrite import product_details_for_bundle
    from rubik_core.meeting_prep.signals import bundle_fields

    try:
        is_sales = bool(ctx.get("is_sales_user"))
        is_internal = bool(ctx.get("is_internal"))
        products = ctx.get("matched_products") or []
        bundle = {
            "title": ctx.get("title"),
            "description": ctx.get("description"),
            "location": ctx.get("location"),
            "focus_on": ctx.get("focus_on"),
            "start": ctx.get("start_local") or ctx.get("start"),
            "attendees": [
                {"name": a.get("display_name"), "email": a.get("email")}
                for a in (ctx.get("attendees") or [])
            ],
            # Exactly the strings that go into the prompt.
            "past_meetings_report": ctx.get("past_meetings_report"),
            # Phase 0 relationship graph — the attendance-record block, verbatim.
            "prior_meetings_block": ctx.get("prior_meetings_block"),
            "email_block": _format_email_threads_for_prompt(
                ctx.get("recent_emails") or [], user_email=user_email
            ),
            "drive_context": ctx.get("drive_context"),
            "matched_products": [p.get("name") for p in products],
            # The catalog lines behind the full brief's PRODUCT KNOWLEDGE
            # block, so a one-section rewrite knows what each product does and
            # who it is for, not only its name.
            "matched_product_details": product_details_for_bundle(products),
            # UC-47 — the researched attendee block, so a brief that mis-reads a
            # person can be traced to the research it was handed rather than
            # re-researched (by which time the web has moved).
            "attendee_intel": ctx.get("attendee_intel") or [],
            "errors": ctx.get("errors") or [],
            # The newer signals (CRM, X-Ray, …): the formatted blocks exactly
            # as prompted, plus the client card and the "Built from" strip.
            **bundle_fields(ctx),
        }
        if not context_visible(bundle):
            logger.warning("meeting_prep: prompt source scope is no longer available")
            return None
        if request is not None:
            # A background build: what the reader asked for, and when.
            bundle["request"] = dict(request)
        chain_event_id = event_id or ctx.get("event_id")
        common = {
            "user_email": user_email,
            "event_id": chain_event_id,
            "title": resp.title or ctx.get("title"),
            "prep_kind": _prep_kind(is_sales, is_internal),
            "is_sales_user": is_sales,
            "is_internal": is_internal,
            "model_id": settings.VERTEX_LLM_MODEL_PRO,
            "context_bundle": bundle,
            "partial_errors": list(resp.partial_errors or []),
        }
        fresh = resp.model_dump()
        latest = versions.latest_version_row(db, user_email, chain_event_id)
        if latest is not None and not context_visible(latest.context_bundle):
            # A prior hand edit may contain CRM facts from a revoked pipeline.
            keep_edits = False

        in_place = (
            {"existing_row": pending_row, "delivery_status": None}
            if pending_row is not None
            else {}
        )

        if getattr(resp, "generation_failed", False):
            if pending_row is not None:
                in_place["delivery_status"] = "failed"
            return versions.record_failed_run(
                db,
                **common,
                response=fresh,
                base_run_id=latest.id if latest is not None else None,
                **in_place,
            )
        if pending_row is not None:
            in_place["delivery_status"] = "ready"

        for attempt in range(_PREP_PERSIST_ATTEMPTS):
            response, section_meta = versions.carry_forward_edits(
                latest if keep_edits else None, fresh
            )
            try:
                # Only valid while ``latest`` is still the latest (an empty
                # chain stays empty for a first generate).
                return versions.append_version(
                    db,
                    **common,
                    response=response,
                    change_kind="regenerated" if latest is not None else "generated",
                    base_run_id=latest.id if latest is not None else None,
                    section_meta=section_meta,
                    must_build_on_latest=True,
                    **in_place,
                )
            except versions.StaleVersionError as exc:
                if attempt == _PREP_PERSIST_ATTEMPTS - 1:
                    raise
                # Another tab saved a version meanwhile: rebuild on that one.
                logger.info(
                    "meeting_prep for %s/%s: v%s landed while storing; rebuilding on it",
                    user_email,
                    chain_event_id,
                    getattr(exc.latest, "version", None),
                )
                latest = exc.latest
                if latest is not None and not context_visible(latest.context_bundle):
                    keep_edits = False
        raise AssertionError("unreachable")
    except Exception:  # noqa: BLE001 — persistence is never allowed to break prep
        logger.exception("Failed to persist meeting_prep_run for %s", user_email)
        db.rollback()
        return None


def _prep_failure_detail(
    resp: MeetingPrepResponse, *, has_current_version: bool = True
) -> str:
    """The 502 message for a failed generation: plain words for the reader,
    never the raw exception (that stays in the logs and the failed row)."""
    raw = (resp.executive_summary or "") + " " + " ".join(resp.partial_errors or [])
    low = raw.lower()
    if any(s in low for s in ("429", "resource_exhausted", "quota", "rate limit")):
        reason = "the AI service is busy right now, so try again in a minute"
    elif any(s in low for s in ("timeout", "timed out", "deadline")):
        reason = "the AI service took too long to answer"
    elif any(s in low for s in ("json", "expecting value", "decode")):
        reason = "the AI's answer could not be read"
    else:
        reason = "the AI service returned an error"
    if has_current_version:
        return (
            f"{PRODUCT_NAME} couldn't build this briefing — {reason}. "
            "Your current version is unchanged."
        )
    # No version yet: the page's error screen carries its own Try Again.
    return f"{PRODUCT_NAME} couldn't build this briefing — {reason}."


def _prep_response_from_row(db: Session, row, *, latest=None) -> MeetingPrepResponse:
    """A stored version as the page's ``MeetingPrepResponse``, version fields
    filled from the row's columns. Tolerates old rows whose stored brief has a
    field of the wrong shape: that field falls back to its default rather than
    the whole page 500ing."""
    from pydantic import ValidationError

    from rubik_core.meeting_prep import versions

    data = versions.row_to_response_dict(db, row, latest=latest)
    try:
        return MeetingPrepResponse(**data)
    except ValidationError as exc:
        bad = {err["loc"][0] for err in exc.errors() if err.get("loc")}
        logger.warning(
            "meeting_prep_run %s: dropping malformed stored fields %s",
            row.id,
            sorted(map(str, bad)),
        )
        for key in bad:
            data.pop(key, None)
        data["title"] = data.get("title") or row.title or "Meeting Prep"
        return MeetingPrepResponse(**data)


_PREP_UNSAVED_DETAIL = (
    f"{PRODUCT_NAME} built a new version but couldn't save it — your current version "
    "is unchanged. Try again."
)


async def _generate_prep_version(
    db: Session,
    user: UserProfile,
    ctx: dict[str, Any],
    *,
    event_id: str | None = None,
    keep_edits: bool = True,
) -> MeetingPrepResponse:
    """Run the model on a collected context IN THE REQUEST and store the brief
    as the meeting's next version (``POST /meeting-prep`` only — the page's
    own first open and Regenerate build in the background,
    ``rubik_core/meeting_prep/background.py``). A failed generation is
    recorded (no version) and answers 502; the reader's current version, if
    any, is untouched.

    A good brief that could not be STORED answers 503: handing it back would
    put a brief with no version over the saved latest on the page."""
    from rubik_core.feedback.wgll import build_wgll_context
    from rubik_core.meeting_prep.provenance import context_visible

    if not await run_in_threadpool(context_visible, ctx):
        raise HTTPException(status_code=404, detail="Meeting prep is unavailable")

    personalization = build_user_context_block(user, db)
    wgll = build_wgll_context(db, surface="meeting_prep", user_email=user.email)
    resp = await generate_meeting_prep_analysis(ctx, user.email, personalization, wgll)
    row = _persist_meeting_prep_run(
        db, user.email, ctx, resp, event_id=event_id, keep_edits=keep_edits
    )
    if resp.generation_failed:
        has_current = (
            getattr(row, "base_run_id", None) is not None if row is not None else True
        )
        raise HTTPException(
            status_code=502,
            detail=_prep_failure_detail(resp, has_current_version=has_current),
        )
    if row is None:
        # Persisting failed (logged).
        raise HTTPException(status_code=503, detail=_PREP_UNSAVED_DETAIL)
    return _prep_response_from_row(db, row, latest=row)


async def _start_background_prep(
    db: Session,
    user: UserProfile,
    *,
    event_id: str,
    kind: str,
    focus_on: str | None,
    keep_edits: bool,
    title: str | None = None,
    product_to_emphasize: str | None = None,
):
    """Start (or join) the reader's background build of this meeting's brief
    and answer 202 with it; 429 when the reader already has
    ``CUBIE_PREP_MAX_INFLIGHT_PER_USER`` builds in flight; 502 only when it
    could not even be dispatched.

    Recording the build and launching it block (database writes; on GCP the
    Cloud Run Admin call, up to 30s), so they run on a worker thread, never on
    the event loop every other request shares. An in-process build is still
    scheduled on THIS loop (``loop=``) — the one the shared genai client is
    bound to (``background.dispatch``)."""
    from fastapi.responses import JSONResponse

    from rubik_core.meeting_prep import background

    reject_support_background_work()
    if not (event_id or "").strip():
        raise HTTPException(status_code=400, detail="An event_id must be provided.")
    loop = asyncio.get_running_loop()

    def start() -> dict[str, Any]:
        row, _started = background.request_build(
            db,
            user_email=user.email,
            event_id=event_id,
            kind=kind,
            focus_on=focus_on,
            keep_edits=keep_edits,
            title=title,
            product_to_emphasize=product_to_emphasize,
            loop=loop,
        )
        return background.accepted_payload(row)

    try:
        payload = await asyncio.to_thread(start)
    except background.TooManyBuilds as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except background.BuildNotStarted as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=payload)


_PREP_BUILD_202 = {
    202: {
        "description": (
            "No version yet: a background build was started, or the one "
            "already running is returned — {status, run_id, event_id, kind, "
            "started_at, title}. Poll /prep-runs/{run_id}/status."
        )
    }
}


@router.get(
    "/meeting-prep/{event_id:path}",
    response_model=MeetingPrepResponse,
    responses=_PREP_BUILD_202,
)
async def get_meeting_prep(
    event_id: str,
    focus_on: str | None = None,
    product_to_emphasize: str | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The reader's latest version of this meeting's brief.

    Opening the page again is free: an existing version is served from
    ``meeting_prep_runs`` with no model call and no Google API call, with
    ``pending_run`` set when a build (e.g. a Regenerate) is in flight. When
    the reader has no version yet, v1 is built IN THE BACKGROUND
    (``rubik_core/meeting_prep/background.py``): 202 at once with the build's
    handle, and a notification linking back here when it is ready. Opening
    the page again while it builds joins the same build. ``focus_on`` /
    ``product_to_emphasize`` steer that first build.
    """
    from rubik_core.meeting_prep import background, versions

    latest = versions.latest_version_row(db, user.email, event_id)
    if latest is not None:
        from rubik_core.meeting_prep.provenance import context_visible

        if await run_in_threadpool(context_visible, latest.context_bundle):
            resp = _prep_response_from_row(db, latest, latest=latest)
            resp.pending_run = background.pending_run(db, user.email, event_id)
            return resp

    return await _start_background_prep(
        db,
        user,
        event_id=event_id,
        kind=background.KIND_FIRST,
        focus_on=focus_on,
        keep_edits=False,
        product_to_emphasize=product_to_emphasize,
    )


@router.post("/meeting-prep", response_model=MeetingPrepResponse)
async def meeting_prep(
    body: MeetingPrepRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Generate a structured meeting brief for a given calendar event, stored as
    that meeting's next version. Accepts the full event object, an optional
    'focus_on' intent string and ``keep_edits`` (default true).
    """
    ctx = await collect_meeting_prep_context(
        user_email=user.email,
        user_department=user.department,
        user_roles=user.roles,
        event=body.event,
        focus_on=body.focus_on,
        reader=user,
    )
    return await _generate_prep_version(db, user, ctx, keep_edits=body.keep_edits)


# ─────────────────────────── Deep Prep (async, emailed) ─────────────────────
# Runs the SAME grounded generation in the background, renders a branded PDF,
# and emails it — so it can be richer without a latency budget. `generate_...`
# below is the pluggable seam the multi-model "council that agrees what to
# deliver" replaces later; PDF rendering, delivery, and status don't change.


def _deep_prep_filename(title: str) -> str:
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title or "").strip("-").lower()[:60]
    return f"deep-prep-{slug or 'meeting'}.pdf"


def _deep_prep_email_html(title: str) -> str:
    """The HTML part of the Deep Prep email. The title is the model's or the
    calendar event's, and an outside inviter writes the event title, so it is
    escaped: raw, it would put their HTML (an image, a link) in mail we send."""
    return (
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:560px">'
        f"<p>Your deep meeting prep for <strong>{html.escape(title)}</strong> is "
        "attached as a PDF.</p>"
        f'<p style="color:#6d6467;font-size:13px">Generated by {PRODUCT_NAME}. It '
        "stays grounded in your records — verify anything before you rely on "
        "it.</p></div>"
    )


# ─────────────── Prep docs: editable Word download + save-to-Assets ─────────
# Own path segment (NOT under /meeting-prep/…) because the
# `/meeting-prep/{event_id:path}` catch-all above would swallow anything there.
# Both act on the persisted MeetingPrepRun the on-screen brief returns as
# `run_id` — any version, not only the latest — so the exported doc is
# byte-identical to what the user reviewed.


def _owns_prep_run(row, user: UserProfile) -> bool:
    return (row.user_email or "").strip().lower() == (user.email or "").strip().lower()


async def _load_prep_run_for_user(db: Session, run_id: str, user: UserProfile):
    """A stored run the caller may READ: its owner, or an admin."""
    from rubik_core.database.models import MeetingPrepRun

    row = db.get(MeetingPrepRun, run_id)
    if row is None or not row.response:
        raise HTTPException(status_code=404, detail="Meeting prep run not found")
    if not _owns_prep_run(row, user) and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Not your meeting prep")
    from rubik_core.meeting_prep.provenance import context_visible

    if not await run_in_threadpool(context_visible, row.context_bundle):
        raise HTTPException(status_code=404, detail="Meeting prep run not found")
    return row


def _render_prep_run_docx(row) -> tuple[bytes, str, str]:
    """Render a persisted run to .docx → (bytes, filename, title).

    A numbered version says so in both the filename (``…-v3.docx``) and the
    title (``… (v3)``), so two saved versions of one meeting are told apart
    on the Assets page."""
    from rubik_core.deep_prep.docx import (
        render_deep_prep_docx,
        render_meeting_prep_docx,
    )

    payload = row.response or {}
    title = row.title or payload.get("title") or "Meeting Prep"
    meeting_time = str((row.context_bundle or {}).get("start") or "")
    # Council-mode deep runs persist a DeepPrepResult (variable `sections`
    # list); everything else is a MeetingPrepResponse dump.
    if isinstance(payload.get("sections"), list):
        data = render_deep_prep_docx(
            payload, prepared_for=row.user_email, meeting_time=meeting_time
        )
    else:
        data = render_meeting_prep_docx(
            payload, prepared_for=row.user_email, meeting_time=meeting_time
        )
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title or "").strip("-").lower()[:60]
    version = getattr(row, "version", None)
    if version:
        filename = f"meeting-prep-{slug or 'meeting'}-v{version}.docx"
        return data, filename, f"{title} (v{version})"
    return data, f"meeting-prep-{slug or 'meeting'}.docx", title


@router.get("/prep-docs/{run_id}/docx")
async def download_meeting_prep_docx(
    run_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The on-screen brief as an editable Word doc (tweak before forwarding)."""
    from backend.services.generated_docs import DOCX_MIME

    row = await _load_prep_run_for_user(db, run_id, user)
    data, filename, _ = _render_prep_run_docx(row)
    return Response(
        content=data,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/prep-docs/{run_id}/save-to-assets")
async def save_meeting_prep_to_assets(
    run_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist the brief as a Word doc on the caller's Assets page, from where
    it can be shared with teammates or attached to a Gmail draft."""
    from backend.services.generated_docs import save_generated_doc

    row = await _load_prep_run_for_user(db, run_id, user)
    data, filename, title = _render_prep_run_docx(row)
    asset, created = save_generated_doc(
        user.email,
        title=title,
        filename=filename,
        data=data,
        kind="meeting-prep",
        source_prep_run_id=row.id,
        # Scope the saved doc to the owner's department so RBAC applies — a
        # department-less asset escapes scoped-Exec oversight (has_document_access
        # rule 3 needs a department) and access policies.
        department=user.department,
    )
    return {
        "doc_id": asset.doc_id,
        "created": created,
        "filename": asset.original_filename,
    }


# ─────────────── Prep versions: history, regenerate, edit, rewrite, restore ──
# One reader's briefs for one meeting are a numbered chain (v1, v2, v3…) on
# meeting_prep_runs; the rules live in rubik_core/meeting_prep/versions.py.
# Every change APPENDS a version — nothing a person read is overwritten — and
# changes are only accepted on the latest version (409 otherwise). Writes are
# owner-only (admins included: an admin may read someone's brief, never edit
# it). Own `/prep-runs/` segment for the same catch-all reason as prep-docs.


class PrepRegenerateBody(BaseModel):
    event_id: str
    focus_on: str | None = None
    # Carry the sections the reader edited by hand into the new version.
    keep_edits: bool = True


class PrepSectionEditBody(BaseModel):
    # prose → str; list → list[str] (or one newline-separated str);
    # stakeholder_map → list of {name, email, role, context}. Validated by
    # rubik_core.meeting_prep.sections.normalize_section_value.
    value: Any


class PrepSectionRewriteBody(BaseModel):
    # Optional steer for Cubie ("shorter", "lead with pricing").
    instruction: str | None = None


async def _load_prep_run_for_owner(db: Session, run_id: str, user: UserProfile):
    """A stored on-screen run the caller may CHANGE: its owner only."""
    from rubik_core.database.models import MeetingPrepRun

    row = db.get(MeetingPrepRun, run_id)
    if row is None or not row.response or row.mode == "deep":
        raise HTTPException(status_code=404, detail="Meeting prep run not found")
    if not _owns_prep_run(row, user):
        raise HTTPException(status_code=403, detail="Not your meeting prep")
    from rubik_core.meeting_prep.provenance import context_visible

    if not await run_in_threadpool(context_visible, row.context_bundle):
        raise HTTPException(status_code=404, detail="Meeting prep run not found")
    return row


def _require_prep_section(section_key: str):
    from rubik_core.meeting_prep.sections import get_section

    spec = get_section(section_key)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"'{section_key}' is not a section of the briefing that can be changed.",
        )
    return spec


def _require_latest_prep_version(db: Session, row) -> None:
    """Raise ``StaleVersionError`` unless ``row`` is the latest version of
    its meeting (an unnumbered failed run is never one)."""
    from rubik_core.meeting_prep import versions

    if row.version is None:
        raise versions.StaleVersionError(
            versions.latest_version_row(db, row.user_email, row.event_id)
        )
    if not row.event_id:
        return  # no event id → no chain: the row is its own latest
    latest = versions.latest_version_row(db, row.user_email, row.event_id)
    if latest is None or latest.id != row.id:
        raise versions.StaleVersionError(latest)


def _stale_prep_version_response(latest, *, detail: str | None = None):
    from fastapi.responses import JSONResponse

    n = getattr(latest, "version", None)
    if detail is None:
        detail = (
            f"A newer version (v{n}) exists — open it to edit."
            if n
            else "This briefing is not a version that can be edited — "
            "reopen the meeting's prep page."
        )
    return JSONResponse(
        status_code=409,
        content={
            "detail": detail,
            "latest_version": n,
            "latest_run_id": getattr(latest, "id", None),
        },
    )


def _merge_sources(existing: Any, added: Any) -> list[str]:
    """``existing`` then ``added``, blanks dropped, duplicates removed (first
    occurrence wins, order kept)."""
    out: list[str] = []
    seen: set[str] = set()
    for src in list(existing or []) + list(added or []):
        if not isinstance(src, str):
            continue
        text = src.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


@router.get("/prep-runs")
async def list_meeting_prep_versions(
    event_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The caller's versions of one meeting's brief, newest first, plus the
    build in flight for it (``pending``, the ``pending_run`` shape, or null)."""
    from rubik_core.meeting_prep import background, versions
    from rubik_core.meeting_prep.provenance import contexts_visible

    version_rows = versions.list_versions(db, user.email, event_id)
    live = await run_in_threadpool(
        contexts_visible, [row.context_bundle for row in version_rows]
    )
    visible_ids = {row.id for row, is_visible in zip(version_rows, live) if is_visible}
    rows = [
        summary
        for summary in versions.version_summaries(db, user.email, event_id)
        if summary["run_id"] in visible_ids
    ]
    return {
        "event_id": event_id,
        "latest_version": rows[0]["version"] if rows else None,
        "versions": [MeetingPrepVersionSummary(**r).model_dump() for r in rows],
        "pending": background.pending_run(db, user.email, event_id),
    }


@router.post("/prep-runs/regenerate", status_code=202, responses=_PREP_BUILD_202)
async def regenerate_meeting_prep(
    body: PrepRegenerateBody,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Build the brief again from fresh sources as the meeting's next version,
    IN THE BACKGROUND: 202 at once with the build's handle (the one already
    running for this meeting, if any), then a notification when it is ready.
    Sections the reader edited by hand are kept unless ``keep_edits`` is
    false. A failed build leaves the chain as it was."""
    from rubik_core.meeting_prep import background, versions

    latest = versions.latest_version_row(db, user.email, body.event_id)
    if latest is not None:
        from rubik_core.meeting_prep.provenance import context_visible

        if not await run_in_threadpool(context_visible, latest.context_bundle):
            latest = None
            body.keep_edits = False
    return await _start_background_prep(
        db,
        user,
        event_id=body.event_id,
        kind=background.KIND_REGENERATE,
        focus_on=body.focus_on,
        keep_edits=body.keep_edits,
        title=latest.title if latest is not None else None,
    )


@router.get("/prep-runs/{run_id}/status")
async def get_meeting_prep_build_status(
    run_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Where a background build stands: queued | running | ready (with its
    ``version``) | failed (with a reader-safe ``detail``). Owner or admin.
    A build past ``CUBIE_PREP_BACKGROUND_TTL_MINUTES`` is reported — and
    marked — failed here."""
    from rubik_core.database.models import MeetingPrepRun
    from rubik_core.meeting_prep import background

    row = db.get(MeetingPrepRun, run_id)
    if row is None or row.mode == "deep":
        # Deep Prep has its own status route.
        raise HTTPException(status_code=404, detail="Meeting prep run not found")
    if not _owns_prep_run(row, user) and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Not your meeting prep")
    if row.response:
        from rubik_core.meeting_prep.provenance import context_visible

        if not await run_in_threadpool(context_visible, row.context_bundle):
            raise HTTPException(status_code=404, detail="Meeting prep run not found")
    background.expire_if_stale(db, row)
    return background.status_payload(row)


@router.get("/prep-runs/{run_id}", response_model=MeetingPrepResponse)
async def get_meeting_prep_version(
    run_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One stored version (owner or admin), with where it sits in the chain."""
    row = await _load_prep_run_for_user(db, run_id, user)
    if row.mode == "deep":
        # Deep Prep is emailed, not a version of the on-screen brief.
        raise HTTPException(status_code=404, detail="Meeting prep run not found")
    return _prep_response_from_row(db, row)


@router.put(
    "/prep-runs/{run_id}/sections/{section_key}",
    response_model=MeetingPrepResponse,
)
async def edit_meeting_prep_section(
    run_id: str,
    section_key: str,
    body: PrepSectionEditBody,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the reader's own text for one section as a new version."""
    from rubik_core.meeting_prep import versions
    from rubik_core.meeting_prep.sections import normalize_section_value

    row = await _load_prep_run_for_owner(db, run_id, user)
    _require_prep_section(section_key)
    try:
        _require_latest_prep_version(db, row)
    except versions.StaleVersionError as exc:
        return _stale_prep_version_response(exc.latest)
    is_internal = bool(row.is_internal)
    try:
        value = normalize_section_value(
            section_key, body.value, is_internal=is_internal
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response = versions.strip_version_fields(row.response)
    try:
        current = normalize_section_value(
            section_key, response.get(section_key), is_internal=is_internal
        )
        unchanged = current == value
    except ValueError:
        unchanged = False
    if unchanged:
        # Saving what is already there is not a new version.
        return _prep_response_from_row(db, row)

    response[section_key] = value
    meta = dict(row.section_meta or {})
    meta[section_key] = {"origin": "edited", "instruction": None}
    try:
        new_row = versions.append_version(
            db,
            **versions.copy_row_fields(row),
            response=response,
            change_kind="section_edited",
            base_run_id=row.id,
            changed_section=section_key,
            section_meta=meta,
            must_build_on_latest=True,
        )
    except versions.StaleVersionError as exc:
        return _stale_prep_version_response(exc.latest)
    return _prep_response_from_row(db, new_row, latest=new_row)


@router.post(
    "/prep-runs/{run_id}/sections/{section_key}/rewrite",
    response_model=MeetingPrepResponse,
)
async def rewrite_meeting_prep_section(
    run_id: str,
    section_key: str,
    body: PrepSectionRewriteBody | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Have Cubie rewrite ONE section (optionally steered by an instruction)
    from the evidence the brief was built on; the result is a new version."""
    from rubik_core.feedback.wgll import build_wgll_context
    from rubik_core.meeting_prep import versions
    from rubik_core.meeting_prep.rewrite import RewriteError, rewrite_section
    from rubik_core.meeting_prep.sections import (
        normalize_instruction,
        normalize_section_value,
    )

    row = await _load_prep_run_for_owner(db, run_id, user)
    _require_prep_section(section_key)
    try:
        _require_latest_prep_version(db, row)
    except versions.StaleVersionError as exc:
        return _stale_prep_version_response(exc.latest)
    try:
        instruction = normalize_instruction(body.instruction if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    brief = versions.strip_version_fields(row.response)
    personalization = build_user_context_block(user, db)
    wgll = build_wgll_context(db, surface="meeting_prep", user_email=user.email)
    unchanged_note = "Your current version is unchanged."
    try:
        result = await rewrite_section(
            section_key=section_key,
            brief=brief,
            context_bundle=row.context_bundle,
            is_sales_user=bool(row.is_sales_user),
            is_internal=bool(row.is_internal),
            instruction=instruction,
            personalization_block=personalization,
            wgll_context=wgll,
        )
        value = normalize_section_value(
            section_key, result.value, is_internal=bool(row.is_internal)
        )
    except RewriteError as exc:
        message = str(exc).strip() or f"{PRODUCT_NAME} couldn't rewrite this section."
        raise HTTPException(
            status_code=502, detail=f"{message} {unchanged_note}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — never a 500 for a model hiccup
        logger.exception(
            "meeting_prep section rewrite failed (run %s, section %s)",
            run_id,
            section_key,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} couldn't rewrite this section — try again. {unchanged_note}",
        ) from exc

    brief[section_key] = value
    brief["source_evidence"] = _merge_sources(
        brief.get("source_evidence"), result.sources
    )
    meta = dict(row.section_meta or {})
    meta[section_key] = {"origin": "rewritten", "instruction": instruction or None}
    try:
        new_row = versions.append_version(
            db,
            **versions.copy_row_fields(row),
            response=brief,
            change_kind="section_rewritten",
            base_run_id=row.id,
            change_note=instruction or None,
            changed_section=section_key,
            section_meta=meta,
            must_build_on_latest=True,
        )
    except versions.StaleVersionError as exc:
        # Someone saved a newer version while Cubie was writing.
        return _stale_prep_version_response(exc.latest)
    return _prep_response_from_row(db, new_row, latest=new_row)


@router.post("/prep-runs/{run_id}/restore", response_model=MeetingPrepResponse)
async def restore_meeting_prep_version(
    run_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Make an older version the latest again by appending a copy of it
    ("v1 → v2 → v3 = v1 restored"); the versions in between stay reachable.
    Restoring the latest version is a no-op."""
    from rubik_core.meeting_prep import versions

    row = await _load_prep_run_for_owner(db, run_id, user)
    if row.version is None:
        return _stale_prep_version_response(
            versions.latest_version_row(db, row.user_email, row.event_id),
            detail="Only a saved version of the briefing can be restored.",
        )
    latest = (
        versions.latest_version_row(db, row.user_email, row.event_id)
        if row.event_id
        else None
    )
    if latest is None or latest.id == row.id:
        return _prep_response_from_row(db, row, latest=latest or row)
    new_row = versions.append_version(
        db,
        **versions.copy_row_fields(row),
        response=versions.strip_version_fields(row.response),
        change_kind="restored",
        base_run_id=row.id,
        section_meta=row.section_meta,
    )
    return _prep_response_from_row(db, new_row, latest=new_row)


def _set_deep_prep_status(run_id: str, status: str, **fields) -> None:
    """Best-effort status write on a fresh session (runs outside request scope)."""
    from rubik_core.database.models import MeetingPrepRun

    db = SessionLocal()
    try:
        row = db.get(MeetingPrepRun, run_id)
        if row is None:
            return
        row.delivery_status = status
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    except Exception:
        logger.exception("deep_prep: could not set status=%s for %s", status, run_id)
        db.rollback()
    finally:
        db.close()


def _fetch_crm_relationships(user_email: str, ctx: dict) -> str:
    """Best-effort live-CRM relationship block for the meeting's external
    attendees, or "". Never raises — CRM is enrichment, not a hard dependency.

    Deep-prep only (runs in the background job), so the extra HubSpot calls never
    slow the interactive on-screen prep. Restricts deals to the company's own pipelines
    so a client-hosted pipeline in the same portal can't masquerade as history.
    """
    try:
        from rubik_core.company.domain import workspace_domains
        from rubik_core.retrieval.hubspot_api import (
            HubSpotAPIClient,
            format_client_relationship_block,
        )
        from rubik_core.tenancy.hubspot_config import get_hubspot_config

        internal_domains = workspace_domains()
        domains: list[str] = []
        for a in ctx.get("attendees") or []:
            email = (a.get("email") or "").lower()
            dom = email.split("@")[-1] if "@" in email else ""
            if dom and dom not in internal_domains and dom not in domains:
                domains.append(dom)
        if not domains:
            return ""
        allowed_pipelines = get_hubspot_config().pipeline_ids
        if not allowed_pipelines:
            return ""
        rels = HubSpotAPIClient().company_relationships_for_domains(
            domains[:3],
            allowed_pipelines=list(allowed_pipelines),
        )
        return format_client_relationship_block(rels)
    except Exception:
        logger.warning("deep_prep: CRM enrichment skipped", exc_info=True)
        return ""


@attribute_to("deep_prep")
async def _run_deep_prep(
    run_id: str,
    user_email: str,
    user_department: str,
    user_roles: list[str],
    event_id: str,
    focus_on: str | None,
    force_sales: bool | None = None,
) -> None:
    """Background pipeline: context → grounded brief → PDF → email. Never raises.

    ``force_sales`` (admin dev-preview only) overrides the role routing so the
    "view as regular/sales" toggle previews the other persona; None keeps the
    user's real role.
    """
    from rubik_core.ai_providers.registry import IncompatibleModelRoute
    from rubik_core.ai_providers.runtime import ProviderRequestError
    from rubik_core.deep_prep.pdf import render_meeting_prep_pdf
    from rubik_core.meeting_prep.signals import bundle_fields
    from rubik_core.tools.email_sender import EmailMessageSpec, send_email

    _set_deep_prep_status(run_id, "running")
    try:
        ctx = await collect_meeting_prep_context(
            user_email=user_email,
            user_department=user_department,
            user_roles=user_roles,
            event_id=event_id,
            focus_on=focus_on,
        )
        from rubik_core.meeting_prep.provenance import context_visible

        # The typed account record carries source IDs. The older relationship
        # fallback was free text without IDs and could not survive revocation.
        if not await run_in_threadpool(context_visible, ctx):
            raise ValueError("Meeting prep source scope is unavailable")
        title = ctx.get("title") or "Meeting Prep"
        meeting_time = str(ctx.get("start_local") or ctx.get("start") or "")
        is_sales = (
            force_sales if force_sales is not None else bool(ctx.get("is_sales_user"))
        )
        is_internal = bool(ctx.get("is_internal"))

        # The council IS the deep prep: agree what to deliver → write grounded →
        # verify faithfulness. If any stage errors, fall back to the grounded
        # on-screen brief so the user still gets something trustworthy.
        try:
            from rubik_core.deep_prep.council import generate_deep_prep
            from rubik_core.deep_prep.pdf import render_deep_prep_pdf

            council = await generate_deep_prep(
                ctx,
                user_email=user_email,
                is_sales_user=is_sales,
                is_internal=is_internal,
                department=user_department,
            )
            title = council.title or title
            pdf_bytes = render_deep_prep_pdf(
                council, prepared_for=user_email, meeting_time=meeting_time
            )
            response_payload = council.to_dict()
            council_verdict = {
                "mode": "council",
                "verdict": council.verdict,
                "plan_rationale": (council.plan or {}).get("rationale"),
            }
        except (IncompatibleModelRoute, ProviderRequestError):
            # A workspace-selected provider failed: never spend on the
            # managed Meeting Prep fallback or email a different model's brief.
            raise
        except Exception:
            logger.exception(
                "deep_prep %s: council failed; using grounded fallback brief", run_id
            )
            resp = await generate_meeting_prep_analysis(ctx, user_email, "")
            title = resp.title or title
            pdf_bytes = render_meeting_prep_pdf(
                resp, prepared_for=user_email, meeting_time=meeting_time
            )
            response_payload = resp.model_dump()
            council_verdict = {"mode": "fallback"}

        subject = f"Your Deep Prep: {title}"
        text_body = (
            f'Your deep meeting prep for "{title}" is attached as a PDF.\n\n'
            f"Generated by {PRODUCT_NAME}. It stays grounded in your records — verify "
            "anything before you rely on it.\n"
        )
        html_body = _deep_prep_email_html(title)
        from rubik_core.company.operations import get_operations_config

        impersonate = get_operations_config().deep_prep_sender_mailbox or user_email
        if not await run_in_threadpool(context_visible, ctx):
            raise ValueError("Meeting prep source scope is unavailable")
        result = send_email(
            EmailMessageSpec(
                to=[user_email],
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                pdf=pdf_bytes,
                pdf_filename=_deep_prep_filename(title),
            ),
            impersonate=impersonate,
        )
        # "disabled" = PDF built fine but the send kill switch is off (dark
        # rollout); surface that distinctly from a real failure.
        status = {"sent": "delivered", "disabled": "generated"}.get(
            result.status, "failed"
        )
        _set_deep_prep_status(
            run_id,
            status,
            is_internal=is_internal,
            response=response_payload,
            context_bundle={
                "title": title,
                "past_meetings_report": ctx.get("past_meetings_report"),
                "start": ctx.get("start_local") or ctx.get("start"),
                **bundle_fields(ctx),
            },
            council_verdict={
                **council_verdict,
                "email_status": result.status,
                "message_id": result.message_id,
                "redirected_to": result.redirected_to,
                "error": result.error,
            },
        )
        logger.info(
            "deep_prep %s finished: status=%s email=%s", run_id, status, result.status
        )
    except Exception as exc:
        logger.exception("deep_prep %s failed", run_id)
        _set_deep_prep_status(
            run_id, "failed", council_verdict={"error": "Meeting prep failed"}
        )


def _run_deep_prep_sync(*args) -> None:
    """Sync entrypoint the Cloud Run Job invokes (drives the async pipeline).

    Called by ``backend.jobs.deep_prep_job`` inside a CPU-allocated Job task —
    NOT from a FastAPI BackgroundTask. The service runs CPU-throttled after the
    response, so a multi-call pipeline fired post-200 gets starved and silently
    never finishes; the Job task avoids that.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_deep_prep(*args))
    finally:
        loop.close()


DEEP_PREP_JOB_NAME = "cubie-deep-prep"
# The on-screen brief's background build runs as this module on the SAME
# Cloud Run Job (rubik_core/meeting_prep/background.py): same image, same
# CPU-allocated task, no extra GCP resource.
MEETING_PREP_JOB_MODULE = "backend.jobs.meeting_prep_job"


def _trigger_deep_prep_job(
    args: list[str],
    *,
    what: str,
    env: list[dict[str, str]] | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    """Trigger one execution of the ``cubie-deep-prep`` Cloud Run Job via the Cloud
    Run Admin v2 REST API with ``args`` as the container arg override.

    ``args`` must start with ``-m <module>``: containerOverrides.args REPLACES
    the container's args entirely (command stays "python"); sending only the
    flags made the container run `python --run-id …` → "unknown option" exit
    2. ``env`` (``[{"name", "value"}]``) overrides those env vars for this
    execution only; ``timeout_seconds`` caps each of its task attempts
    (``overrides.timeout``) below the Job's own --task-timeout. Raises
    HTTPException(502) on failure so the caller can surface it.
    """
    from backend.jobs.launcher import JobLaunchError, run_job_request

    container: dict[str, Any] = {"args": args}
    if env:
        container["env"] = env
    overrides: dict[str, Any] = {"containerOverrides": [container]}
    if timeout_seconds:
        overrides["timeout"] = f"{int(timeout_seconds)}s"
    body = {"overrides": overrides}

    try:
        return run_job_request(DEEP_PREP_JOB_NAME, body)
    except JobLaunchError as exc:
        logger.error("%s: Cloud Run Job launch failed: %s", what, exc)
        raise HTTPException(
            status_code=502, detail="Failed to launch cubie-deep-prep job."
        ) from exc


def _launch_deep_prep_job_execution(
    *,
    run_id: str,
    user_email: str,
    user_department: str,
    user_roles: list[str],
    event_id: str,
    focus_on: str | None,
    force_sales: bool | None,
) -> dict:
    """Trigger a Cloud Run Job execution for one Deep Prep via the Cloud Run
    Admin v2 REST API, passing per-run params as container arg overrides.

    Mirrors ``_launch_priorities_job_execution`` in backend/routers/jobs.py.
    Raises HTTPException on failure so the caller can surface it.
    """
    args = [
        "-m",
        "backend.jobs.deep_prep_job",
        "--run-id",
        run_id,
        "--user-email",
        user_email,
        "--user-department",
        user_department or "",
        "--user-roles",
        ",".join(user_roles or []),
        "--event-id",
        event_id,
    ]
    if focus_on:
        args += ["--focus-on", focus_on]
    if force_sales is not None:
        args += ["--force-sales", "true" if force_sales else "false"]
    return _trigger_deep_prep_job(args, what="deep_prep")


def _launch_meeting_prep_job_execution(
    *,
    run_id: str,
    user_email: str,
    event_id: str,
    focus_on: str | None,
    keep_edits: bool,
    product_to_emphasize: str | None = None,
) -> dict:
    """Run one background on-screen Meeting Prep build
    (``python -m backend.jobs.meeting_prep_job``) as an execution of the
    ``cubie-deep-prep`` Cloud Run Job. Raises on failure; the caller
    (``background.dispatch``) then falls back to in-process.

    * Every value goes as ``--flag=value``: an event id or a focus that
      begins with a dash ("-pricing first") would otherwise be read by
      argparse as a flag and kill the task (exit 2) before it builds.
    * The Service's own source switches go along as env overrides
      (:func:`_meeting_prep_job_env`), so they govern background briefs.
    * Each attempt is capped at the build TTL: an execution can never outlive
      the moment the reader was told the build took too long."""
    from rubik_core.meeting_prep import background

    args = [
        "-m",
        MEETING_PREP_JOB_MODULE,
        f"--run-id={run_id}",
        f"--user-email={user_email}",
        f"--event-id={event_id}",
    ]
    if focus_on:
        args.append(f"--focus-on={focus_on}")
    if product_to_emphasize:
        args.append(f"--product-to-emphasize={product_to_emphasize}")
    args.append(f"--keep-edits={'true' if keep_edits else 'false'}")
    return _trigger_deep_prep_job(
        args,
        what="meeting_prep",
        env=_meeting_prep_job_env(),
        timeout_seconds=background.ttl_minutes() * 60,
    )


def _meeting_prep_job_env() -> list[dict[str, str]]:
    """The Service's CURRENT values of every switch a brief reads, as Cloud Run
    env overrides for one background build: ``TODOS_ENABLED``,
    ``PROMISE_ACCOUNTABILITY_ENABLED``, ``CUBIE_PREP_ATTENDANCE_LOOKUP`` and
    every ``CUBIE_PREP_*_ENABLED`` source switch. The cubie-deep-prep Job's own env
    hard-sets some of them on for Deep Prep; without this, turning a source
    off on the Service would leave it on in the briefs the Job builds."""
    from rubik_core.config import Settings, settings

    names = [
        "TODOS_ENABLED",
        "PROMISE_ACCOUNTABILITY_ENABLED",
        "CUBIE_PREP_ATTENDANCE_LOOKUP",
        *sorted(
            name
            for name in Settings.model_fields
            if name.startswith("CUBIE_PREP_") and name.endswith("_ENABLED")
        ),
    ]
    return [
        {"name": name, "value": "true" if getattr(settings, name) else "false"}
        for name in names
    ]


@router.post("/deep-prep/{event_id:path}")
async def queue_deep_meeting_prep(
    event_id: str,
    focus_on: str | None = None,
    view: str | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Queue an async Deep Prep for a calendar event; emails a PDF when ready.

    ``view`` ("sales" | "regular") is an admin-only dev preview override for the
    calendar's "view as" toggle; non-admins always get their real role's brief.
    """
    from rubik_core.config import settings
    from rubik_core.database.models import MeetingPrepRun

    reject_support_background_work()
    if not settings.CUBIE_DEEP_PREP_ENABLED:
        raise HTTPException(status_code=404, detail="Deep Prep is not enabled.")

    is_sales = is_sales_flavoured(user.department, user.roles)
    force_sales: bool | None = None
    if "admin" in [r.lower() for r in user.roles] and view in ("sales", "regular"):
        force_sales = view == "sales"
        is_sales = force_sales  # reflect the preview on the stored run

    from rubik_core.sales.model_config import model_for as deep_prep_model_for

    run_id = uuid.uuid4().hex
    try:
        db.add(
            MeetingPrepRun(
                id=run_id,
                user_email=user.email,
                event_id=event_id,
                prep_kind="sales_battle_brief" if is_sales else "internal_ea",
                is_sales_user=is_sales,
                # The admin's Deep Prep choice, read the same way the job reads
                # it (Admin → Sales Models), so the row says what will run.
                model_id=deep_prep_model_for("deep_prep"),
                mode="deep",
                delivery_status="queued",
            )
        )
        db.commit()
    except Exception:
        logger.exception("deep_prep: failed to enqueue for %s", user.email)
        db.rollback()
        raise HTTPException(status_code=500, detail="Could not queue Deep Prep.")

    # Run on a Cloud Run Job (CPU-allocated), NOT a FastAPI BackgroundTask — the
    # service throttles CPU after the response, which starves multi-call jobs.
    try:
        _launch_deep_prep_job_execution(
            run_id=run_id,
            user_email=user.email,
            user_department=user.department,
            user_roles=user.roles,
            event_id=event_id,
            focus_on=focus_on,
            force_sales=force_sales,
        )
    except Exception:
        logger.exception("deep_prep: job launch failed for %s", user.email)
        _set_deep_prep_status(
            run_id, "failed", council_verdict={"error": "job launch failed"}
        )
        raise HTTPException(
            status_code=502, detail="Could not start the Deep Prep job."
        )
    return {"run_id": run_id, "status": "queued"}


@router.get("/deep-prep/{run_id}/status")
async def get_deep_meeting_prep_status(
    run_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll a Deep Prep run (queued | running | delivered | generated | failed)."""
    from rubik_core.database.models import MeetingPrepRun

    row = db.get(MeetingPrepRun, run_id)
    if row is None or row.user_email != user.email:
        raise HTTPException(status_code=404, detail="Deep Prep run not found.")
    verdict = row.council_verdict or {}
    return {
        "run_id": run_id,
        "status": row.delivery_status,
        "email_status": verdict.get("email_status"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ─── UC-20 AI Priorities endpoint (BookWriter-style multi-agent pipeline) ─────
#
# Opt-in alternative to main's ADK SequentialAgent overnight pipeline. Reads
# HomeCard.metadata_json (the structured bundle persisted overnight) and
# falls back to running the pipeline synchronously when no card exists for
# today or the caller asks for a fresh refresh.


def _bundle_dict_from_metadata(metadata: dict) -> dict | None:
    """Project a persisted ``HomeCard.metadata_json`` payload into the API shape."""
    from rubik_core.priorities.cached_visibility import (
        safe_headline,
        verified_bundle_deals,
    )
    from rubik_core.priorities.privacy import is_private_gmail_bundle

    verified = verified_bundle_deals(metadata)
    if verified is None:
        return None
    stored = metadata["priorities"]
    visible = _visible_stored_crm_priorities(stored, verified_deals=verified)
    return {
        "generated_at": metadata.get("generated_at") or "",
        # Shared delivery reads this run-level boundary before projecting any
        # card. Per-card attrs alone cannot protect an older partial snapshot.
        "private_gmail_context": is_private_gmail_bundle(metadata),
        # Private prompt provenance for Meeting Prep's flagged-card block.
        # Pydantic response models discard this key on the Priorities API.
        "_crm_provenance": [metadata.get("crm_provenance")],
        # The stored narrative may cite CRM facts even when no card survived.
        "headline": safe_headline(visible),
        "revision_count": int(metadata.get("revision_count") or 0),
        "last_critique_score": metadata.get("last_critique_score"),
        "generated_count": len(visible),
        # What the pipeline said about this run (ok / no_signals / on_pto / …).
        # Delivery needs it to tell a legitimately quiet day from a card whose
        # items were all eaten by the read-path filter; it was missing here, so
        # `slack_delivery._today_bundle_status` always reported None.
        "generation_status": metadata.get("generation_status"),
        # PTO banner payload ({on_pto,start,end,return_date}) when the user is
        # out today, else None — lets the home UI render "you're on PTO".
        "pto": metadata.get("pto"),
        # Unanswered-invite note ({unanswered_count,invites}) when the user has
        # upcoming invites they never RSVP'd to, else None. Those meetings are
        # deliberately kept out of the priority list — this is how the user
        # still hears they exist.
        "calendar_hygiene": metadata.get("calendar_hygiene"),
        "priorities": [_priority_dict_from_metadata(p) for p in visible],
    }


def _stored_crm_priority_deal_ids(priority: dict) -> set[str] | None:
    """Return cited deal IDs, None for non-CRM, empty for unprovable CRM."""
    from rubik_core.priorities.cached_visibility import crm_deal_ids

    return crm_deal_ids(priority)


def _visible_stored_crm_priorities(
    priorities: list[dict], *, verified_deals: set[str] | None = None
) -> list[dict]:
    """Drop cached HubSpot cards whose cited deals lost workspace ownership."""
    from backend.routers.agents import visible_hubspot_deal_ids
    from rubik_core.priorities.cached_visibility import visible_cached_priorities

    proof = (
        visible_hubspot_deal_ids
        if verified_deals is None
        else lambda ids: ids & verified_deals
    )
    return visible_cached_priorities(priorities, visible_deals=proof)


def _priority_dict_from_metadata(p: dict) -> dict:
    """One stored priority → the API shape."""
    covered_ids, covered_labels = _covered_pairs(p)
    return {
        "id": p.get("id") or "",
        "title": p.get("title") or "",
        "why_it_matters": p.get("why_it_matters") or "",
        "urgency": p.get("urgency") or "medium",
        "source_channels": list(p.get("source_channels") or []),
        "source_links": list(p.get("source_links") or []),
        "suggested_action": p.get("suggested_action") or "",
        # UC-39 — surface entity tags to the frontend so smart action
        # affordances can be derived. None for pre-UC-24 rows.
        "priority_kind": p.get("priority_kind"),
        "entity_kind": p.get("entity_kind"),
        "entity_id": p.get("entity_id"),
        "entity_label": p.get("entity_label"),
        # Present only for a recurring calendar event — the card uses it
        # to offer "never prep this series again". None otherwise.
        "series_entity_id": p.get("series_entity_id"),
        "target_email": p.get("target_email"),
        "target_label": p.get("target_label"),
        # Natural signal ids, kept so the next-week merge can dedup
        # against daily items by real event/thread ids.
        "signal_ids": list(p.get("signal_ids") or []),
        # Every sibling entity the card speaks for (see PriorityItem).
        # Persisted by bundle_to_metadata since UC-24.
        "covered_entity_ids": covered_ids,
        "covered_entity_labels": covered_labels,
        # Per-sibling ownership, positional against covered_entity_ids. A card
        # can mix one person's item with another's (siblings are grouped on
        # kind alone), so a single card-level answer is not enough. None where
        # the card predates the flag — the panel falls back to `is_own`.
        "covered_entity_is_own": _covered_own_flags(p),
        # Derived, not stored — see PriorityItem.is_own.
        "is_own": _is_own_item(p),
        # The card's own renderable facts (the My To-Dos promise lines). The
        # orchestrator persists `attrs` (bundle_to_metadata) and the panel
        # reads `p.attrs`; this is the hop between them.
        #
        # Re-filtered rather than passed through, because `filter_card_attrs`
        # is an allow-list and this is the boundary where it has to hold: a row
        # written by an older build, hand-edited, or produced by some future
        # path that sets `PriorityCandidate.attrs` directly still cannot put an
        # un-allow-listed key (or a nested object, or a wall of text) in front
        # of a reader. `{}` and absent read identically to the panel, so an
        # empty result is projected as None rather than an empty dict.
        "attrs": filter_card_attrs(p.get("attrs")) or None,
    }


def _covered_pairs(p: dict) -> tuple[list[str], list[str]]:
    """``(ids, labels)`` for a card's covered entities, index-for-index.

    The two lists are STRICTLY positional — ``covered_entity_labels[i]`` names
    ``covered_entity_ids[i]``, and the Slack follow-up quotes those labels at
    the person being chased. Filtering them independently (drop falsy ids, drop
    falsy labels) would let one empty label shift every later label up by one,
    so a row would name a different item than the one it stands for. The
    orchestrator builds a label as ``entity_label or title or ""``, so an empty
    one is reachable for any channel whose signal carries neither.

    Zipping them here makes the alignment structural: an entry survives or is
    dropped as a PAIR, and a missing label becomes "" in its own slot rather
    than borrowing its neighbour's.
    """
    raw_ids = list(p.get("covered_entity_ids") or [])
    raw_labels = list(p.get("covered_entity_labels") or [])
    ids: list[str] = []
    labels: list[str] = []
    for i, entity_id in enumerate(raw_ids):
        if not entity_id:
            continue  # nothing to act on — drop the whole pair
        ids.append(str(entity_id))
        label = raw_labels[i] if i < len(raw_labels) else ""
        labels.append(str(label or ""))
    return ids, labels


def _covered_own_flags(p: dict) -> list[bool | None]:
    """Per-covered-entity ``is_own``, aligned with :func:`_covered_pairs`.

    Dropped index-for-index by the SAME rule as the labels — an entity with a
    falsy id takes its flag down with it — because these lists are read
    positionally and a one-row slip here answers "is this mine?" about a
    different item than the one being acted on. ``None`` means the card never
    carried the flag (built before the orchestrator stamped it), which is not
    the same as False and must not be flattened into one.
    """
    raw_ids = list(p.get("covered_entity_ids") or [])
    raw_flags = list(p.get("covered_entity_is_own") or [])
    out: list[bool | None] = []
    for i, entity_id in enumerate(raw_ids):
        if not entity_id:
            continue
        flag = raw_flags[i] if i < len(raw_flags) else None
        out.append(None if flag is None else bool(flag))
    return out


def _is_own_item(p: dict, user_email: str | None = None) -> bool:
    """Is this card about the READER's own item, or someone else's?

    ``is_own`` is decided deterministically by the channel agent (for an EOS
    item: "am I the owner?") but never persisted. What IS persisted is its
    consequence: ``_reattach_channel_entities`` stamps ``target_email`` /
    ``target_label`` **only** when the signal said ``is_own`` was false. So a
    card carrying a target is a card about somebody else's work.

    ``user_email`` is optional and only tightens the answer: a target that IS
    the reader means the item is theirs after all.

    This is the UI's HINT: it decides which buttons the panel offers. It is
    deliberately NOT an authorization control.
    """
    target = (p.get("target_email") or "").strip().lower()
    if target:
        return target == (user_email or "").strip().lower()
    return True


def _parse_iso_naive(value: str | None) -> datetime | None:
    """Parse an ISO timestamp to a naive UTC datetime, or None.

    Memory rows store naive UTC (``datetime.utcnow``); a bundle's
    ``generated_at`` is tz-aware UTC. Normalising both to naive UTC lets
    us compare them directly.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _memory_row_keeps(row, card_dt: datetime | None, now: datetime) -> bool:
    """Does this memory row let the card through? ``None`` (no row) always does.

    Split out of :func:`_memory_filter` so the verdict for one row reads on
    its own; two copies of these rules would drift.
    """
    from rubik_core.priorities import memory as priority_memory

    if row is None or row.state == priority_memory.STATE_ACTIVE:
        return True
    if row.state == priority_memory.STATE_RESOLVED:
        return False  # durable — the user (or the source system) marked it done
    if row.state == priority_memory.STATE_SNOOZED:
        until = _parse_iso_naive(row.snoozed_until)
        return not (until and until > now)  # elapsed snooze revives
    if row.state == priority_memory.STATE_DISMISSED:
        # Kept only when a LATER run chose to resurface it on new evidence.
        changed = _parse_iso_naive(row.last_state_change_at)
        return bool(card_dt and changed and card_dt > changed)
    return True  # unknown state -> fail open


def _apply_priority_memory(user_email: str, bundle: dict) -> dict:
    """UC-25 read-path filter — make dismiss / snooze / resolve stick.

    The dismiss/snooze/resolve endpoints only write ``priority_memory``;
    they never rewrite the cached HomeCard, and the orchestrator's
    post-filter runs only on a fresh pipeline run. So a priority the user
    handled this morning re-appears on the next page load, because the
    cached card still lists it — the bug behind "I checked it off and it
    came back after I logged in again".

    Re-applying the user's memory here, on every read, makes their choice
    survive a reload without waiting for a regen. Best-effort: any failure
    logs and returns the bundle unfiltered.

    The card's ``generated_at`` stands in for the live signal timestamps
    the orchestrator's post-filter uses but the read path doesn't have:

      * ``resolved``                        -> drop (durable)
      * ``snoozed`` with a future deadline  -> drop
      * ``snoozed`` past / ``active`` / none -> keep
      * ``dismissed``                       -> drop, UNLESS this card was
        generated after the dismiss (a later pipeline run chose to
        resurface it on new evidence) -> keep

    The verdict is the PRIMARY entity's alone. It is not taken over every
    entity the card covers, because "no row" means KEEP and rows only exist
    where something wrote one: ``memory.record_seen`` writes the PRIMARY entity
    only, and the EOS completion hooks UPDATE existing rows and silently no-op
    when there are none, so a sibling with no row would keep a finished card
    on the homepage for the rest of the day.
    """
    priorities = bundle.get("priorities") or []
    if not priorities:
        return bundle
    try:
        kept, _drops = _memory_filter(
            user_email, priorities, _parse_iso_naive(bundle.get("generated_at"))
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "UC-25 read-path memory load failed for %s", user_email, exc_info=True
        )
        return bundle
    if len(kept) == len(priorities):
        return bundle
    return {**bundle, "priorities": kept}


@dataclass(frozen=True)
class ReadFilterDrop:
    """One stored priority the read path hid, and what hid it.

    Explains a Slack delivery that found a card full of items and nothing left
    to send. ``changed_at`` is what separates the two very different reasons:
    a row written AFTER the card was built means the person (or Cubie, on their
    behalf) handled the item today, while a row that already existed when the
    card was built is one generation should have honoured and didn't.

    Ids and timestamps only, never the title: the delivery alert DMs these to
    the ops team, and someone else's priority titles are not ops material.
    """

    entity_kind: str
    entity_id: str
    priority_kind: str | None
    cause: str  # resolved | snoozed | dismissed | deal_deferred
    changed_at: datetime | None  # naive UTC; None when the row carries no time
    changed_by: str | None


def _memory_filter(
    user_email: str, priorities: list[dict], card_dt: datetime | None
) -> tuple[list[dict], list[ReadFilterDrop]]:
    """``(kept, drops)`` under the UC-25 rules of :func:`_apply_priority_memory`.

    The one implementation of those rules, shared by the read path and by the
    delivery alert that explains it, so the two cannot disagree about why a
    card went. ``memory.load_all`` turns a failed read into no rows (keep
    everything); anything else unexpected raises and the caller decides.
    """
    from rubik_core.priorities import memory as priority_memory

    rows = priority_memory.load_all(user_email)
    if not rows:
        return list(priorities), []

    by_key = {r.key: r for r in rows}
    now = datetime.utcnow()

    kept: list[dict] = []
    drops: list[ReadFilterDrop] = []
    for p in priorities:
        ek, eid, pk = (
            p.get("entity_kind"),
            p.get("entity_id"),
            p.get("priority_kind"),
        )
        if not ek or not eid or not pk:
            kept.append(p)  # not entity-tagged (pre-UC-24) -> nothing to apply
            continue
        row = by_key.get((ek, str(eid), pk))
        if _memory_row_keeps(row, card_dt, now):
            kept.append(p)
            continue
        # The row said drop (no row always keeps), so it is not None.
        drops.append(
            ReadFilterDrop(
                entity_kind=str(ek),
                entity_id=str(row.entity_id),
                priority_kind=str(pk),
                cause=row.state,
                changed_at=_parse_iso_naive(row.last_state_change_at),
                changed_by=row.last_state_change_by,
            )
        )
    return kept, drops


def _apply_deal_defers(user_email: str, bundle: dict) -> dict:
    """Drop the cards about a deal this user deferred, until its follow-up date.

    The nightly run stops producing signals for a deferred deal
    (``backend/signals/hubspot.py::_without_deferred``), but this morning's card
    was built before the rep pressed Defer — so, like the UC-25 memory filter,
    the defer is re-applied on every read. Covers the homepage panel and the
    Slack morning DM, which read this same bundle. Only cards whose primary
    entity is the deal: a meeting or company card that mentions it stays.
    Fails open — an unreadable defer table hides nothing.
    """
    priorities = bundle.get("priorities") or []
    kept, drops = _deal_defer_filter(user_email, priorities)
    if not drops:
        return bundle
    return {**bundle, "priorities": kept}


def _deal_defer_filter(
    user_email: str, priorities: list[dict]
) -> tuple[list[dict], list[ReadFilterDrop]]:
    """``(kept, drops)`` for :func:`_apply_deal_defers`. Never raises (the
    defer read fails open on its own)."""
    deal_ids = {
        str(p.get("entity_id"))
        for p in priorities
        if p.get("entity_kind") == "deal" and p.get("entity_id")
    }
    if not deal_ids:
        return list(priorities), []
    from rubik_core.sales.deal_defer import active_by_deal

    defers = active_by_deal(deal_ids, reader=user_email)
    if not defers:
        return list(priorities), []
    kept: list[dict] = []
    drops: list[ReadFilterDrop] = []
    for p in priorities:
        defer = (
            defers.get(str(p.get("entity_id")))
            if p.get("entity_kind") == "deal"
            else None
        )
        if defer is None:
            kept.append(p)
            continue
        drops.append(
            ReadFilterDrop(
                entity_kind="deal",
                entity_id=defer.deal_id,
                priority_kind=p.get("priority_kind"),
                cause="deal_deferred",
                changed_at=defer.created_at,
                changed_by=defer.deferred_by,
            )
        )
    return kept, drops


def _today_daily_bundle(user_email: str) -> tuple[str, dict | None]:
    """``(today_str, bundle)``: the user's daily card for their local today,
    projected but NOT filtered. ``bundle`` is None when no card exists."""
    from rubik_core.priorities import DAILY_BRIEFING_CATEGORY

    today_str = user_today_str(user_email)
    with SessionLocal() as db:
        card = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == user_email)
            .filter(HomeCard.date == today_str)
            .filter(HomeCard.category == DAILY_BRIEFING_CATEGORY)
            .order_by(desc(HomeCard.created_at))
            .first()
        )
        if not card or not card.metadata_json:
            return today_str, None
        return today_str, _bundle_dict_from_metadata(card.metadata_json)


def today_read_filter_drops(
    user_email: str,
) -> tuple[datetime | None, list[ReadFilterDrop]] | None:
    """Why today's daily card lost items on the read path.

    ``(card_built_at, drops)``: when the card was generated (naive UTC, None if
    it never said) and one :class:`ReadFilterDrop` per stored priority that the
    memory and deal-defer filters hid, in the order :func:`_load_today_priorities`
    applies them. ``None`` when there is no card for the user's local today.

    A memory read that fails yields no drops rather than raising, because
    ``memory.load_all`` swallows its own errors; the delivery alert treats "a
    card emptied with nothing to show for it" as unexplained, never as handled.
    A failed card read raises.
    """
    _today_str, bundle = _today_daily_bundle(user_email)
    if bundle is None:
        return None
    card_dt = _parse_iso_naive(bundle.get("generated_at"))
    kept, memory_drops = _memory_filter(
        user_email, bundle.get("priorities") or [], card_dt
    )
    _kept, defer_drops = _deal_defer_filter(user_email, kept)
    return card_dt, [*memory_drops, *defer_drops]


def _load_today_priorities(user_email: str) -> dict | None:
    """Return today's persisted priorities bundle (if any), else None.

    Applies the UC-25 priority-memory filter so anything the user has
    resolved / dismissed / snoozed stays gone across reloads — see
    :func:`_apply_priority_memory` — and drops the cards about deals the user
    deferred (:func:`_apply_deal_defers`).
    """
    today_str, bundle = _today_daily_bundle(user_email)
    if bundle is None:
        return None
    daily = _apply_deal_defers(user_email, _apply_priority_memory(user_email, bundle))
    # FT-XX — additively merge the Friday next-week look-ahead (if one is
    # currently visible) into the same bundle, tagged origin="next_week".
    return _hide_eos_priorities_if_disabled(
        _merge_next_week(user_email, daily, today_str)
    )


def _hide_eos_priorities_if_disabled(bundle: dict) -> dict:
    """Keep cached EOS cards off core surfaces after the module is switched off."""
    from rubik_core.control.repository import module_enabled
    from rubik_core.priorities.models import is_eos_priority_card

    try:
        if module_enabled("eos"):
            return bundle
    except Exception:  # noqa: BLE001 — omit EOS on control lookup failure
        logger.warning("priorities: EOS module lookup failed", exc_info=True)
    original = bundle.get("priorities") or []
    visible = [card for card in original if not is_eos_priority_card(card)]
    if len(visible) == len(original):
        return bundle
    return {
        **bundle,
        "priorities": visible,
        "generated_count": len(visible),
        # The persisted summary can mention the now-disabled EOS cards.
        "headline": "",
    }


# FT-XX — Friday "next week" look-ahead. Cards live under NEXT_WEEK_CATEGORY
# with the target-week window in metadata; the read path decides visibility
# and merges them into the daily bundle so they ride the same pane + actions.
#
# Visibility: "until_week_start" (a product decision) — show Fri/Sat/Sun, then
# hide once the user's local date reaches the target Monday, because the daily
# briefing (72h lookahead) then covers those meetings itself.
NEXT_WEEK_VISIBILITY = "until_week_start"


def _load_visible_next_week(user_email: str, today_str: str) -> dict | None:
    """Return the latest currently-visible next-week bundle, else None.

    Applies the visibility window and the UC-25 memory filter (so a dismissed
    look-ahead item stays gone on reload).
    """
    from rubik_core.priorities import NEXT_WEEK_CATEGORY

    with SessionLocal() as db:
        card = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == user_email)
            .filter(HomeCard.category == NEXT_WEEK_CATEGORY)
            .order_by(desc(HomeCard.created_at))
            .first()
        )
        if not card or not card.metadata_json:
            return None
        meta = card.metadata_json
        bundle = _bundle_dict_from_metadata(meta)
        if bundle is None:
            return None
    week_start = meta.get("week_start")
    week_end = meta.get("week_end")
    # ISO date strings (YYYY-MM-DD) compare lexicographically == chronologically.
    if not week_start or not week_end or today_str > week_end:
        return None
    if NEXT_WEEK_VISIBILITY == "until_week_start" and today_str >= week_start:
        return None
    bundle["week_start"] = week_start
    bundle["week_end"] = week_end
    return _hide_eos_priorities_if_disabled(
        _apply_deal_defers(user_email, _apply_priority_memory(user_email, bundle))
    )


def load_next_week_priorities(user_email: str) -> dict | None:
    """The user's currently-visible next-week look-ahead bundle, on its OWN.

    Unlike :func:`_load_today_priorities` this returns ONLY the look-ahead —
    no daily items merged in, and no dedup against them. The Friday Slack
    look-ahead DM reads through here precisely so it cannot re-send the daily
    briefing the user already received that morning: going through the merged
    accessor is what made every Friday's second DM a duplicate of the first
    (see ``backend/agents/slack_delivery.py``).

    Carries ``week_start`` / ``week_end`` so a caller can label the window.
    Returns ``None`` when no next-week card exists or its visibility window
    has passed.
    """
    return _load_visible_next_week(user_email, user_today_str(user_email))


def _merge_next_week(user_email: str, daily: dict, today_str: str) -> dict:
    """Append currently-visible next-week items to the daily bundle.

    Dedups against daily items by the UC-25 memory key
    ``(entity_kind, entity_id, priority_kind)`` and by natural signal-id
    overlap — Friday's daily 72h lookahead already reaches Monday, so a
    Monday-anchored item can appear in both. On a collision we DROP the
    next-week copy and keep the daily one (its urgency is today-appropriate).
    """
    nw = _load_visible_next_week(user_email, today_str)
    if not nw or not nw.get("priorities"):
        return daily

    daily_items = daily.get("priorities") or []
    seen_keys = {
        (p.get("entity_kind"), p.get("entity_id"), p.get("priority_kind"))
        for p in daily_items
        if p.get("entity_id")
    }
    seen_signals: set[str] = {
        sid for p in daily_items for sid in (p.get("signal_ids") or [])
    }
    week_of = nw.get("week_start")

    extra: list[dict] = []
    for p in nw["priorities"]:
        key = (p.get("entity_kind"), p.get("entity_id"), p.get("priority_kind"))
        if all(key) and key in seen_keys:
            continue
        if seen_signals and set(p.get("signal_ids") or []) & seen_signals:
            continue
        extra.append({**p, "origin": "next_week", "week_of": week_of})

    if not extra:
        return daily
    return {
        **daily,
        "private_gmail_context": bool(
            daily.get("private_gmail_context") or nw.get("private_gmail_context")
        ),
        "priorities": [*daily_items, *extra],
        "_crm_provenance": [
            *(daily.get("_crm_provenance") or []),
            *(nw.get("_crm_provenance") or []),
        ],
    }


def _load_priorities_ui_prefs(user_email: str) -> tuple[int | None, list[str] | None]:
    """The user's saved AI-Priorities UI defaults: ``(max, sources)``.

    Reads ``UserPreference.priorities_max`` / ``priorities_sources`` (AI
    Personalization tab). Returns ``(None, None)`` on any failure so the
    caller falls back to the org defaults. (``focus`` / ``about_me`` /
    muted-topics are resolved inside the orchestrator for ALL entry points;
    only the two UI-tunable knobs that need request-vs-default disambiguation
    are resolved here.)
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import UserPreference

        with SessionLocal() as db:
            row = (
                db.query(UserPreference)
                .filter(UserPreference.user_email == user_email)
                .first()
            )
            if row is None:
                return (None, None)
            sources = row.priorities_sources
            if not isinstance(sources, list) or not sources:
                sources = None
            return (row.priorities_max, sources)
    except Exception:  # noqa: BLE001 — prefs are best-effort
        logger.warning(
            "priorities UI-prefs lookup failed for %s", user_email, exc_info=True
        )
        return (None, None)


class EmptyBundleGenerated(RuntimeError):
    """Generation produced an empty bundle it could not justify.

    Raised instead of persisting so the on-demand path can never write the
    blank briefing an empty generation would otherwise ship — mirrors the batch
    delivery gate in ``rubik_core/priorities/batch.py``. The background
    runners catch it and mark the UC-41 run failed; the read path keeps
    serving the previous good card.
    """


@attribute_to("ai_priorities", user_arg="user_email")
def _run_and_persist_priorities(user_email: str, body: PrioritiesRequestBody) -> dict:
    """Run the orchestrator and persist a fresh HomeCard."""
    from rubik_core.priorities import (
        GENERATION_EMPTY_WITH_SIGNALS,
        ChannelKind,
        PrioritiesOrchestrator,
        PrioritiesRequest,
        bundle_to_metadata,
        persist_bundle,
    )

    pref_max, pref_sources = _load_priorities_ui_prefs(user_email)

    # Channels: explicit body wins; else the saved sources; else orchestrator
    # default. Both paths validate against ChannelKind.
    requested_channels = None
    if body.channels:
        try:
            requested_channels = [ChannelKind(c.lower()) for c in body.channels]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"unknown channel: {exc}")
    elif pref_sources:
        # A saved pref never 400s the run. A source that no longer exists (a
        # removed channel) is dropped and the rest of the choice is kept; if
        # nothing known is left, the defaults apply.
        known = {kind.value for kind in ChannelKind}
        requested_channels = [
            ChannelKind(c.lower())
            for c in pref_sources
            if isinstance(c, str) and c.lower() in known
        ] or None

    # Max: explicit body wins; else saved pref; else the request default (5).
    effective_max = body.max_priorities if body.max_priorities is not None else pref_max

    request_kwargs = {
        "user_email": user_email,
        "focus": body.focus,  # None → orchestrator fills from saved focus
        "max_priorities": max(1, min(effective_max if effective_max else 5, 10)),
    }
    if requested_channels is not None:
        request_kwargs["channels"] = requested_channels

    request = PrioritiesRequest(**request_kwargs)
    bundle = PrioritiesOrchestrator().generate(request)

    if getattr(bundle, "generation_status", None) == GENERATION_EMPTY_WITH_SIGNALS:
        # Same delivery gate as the overnight batch: never persist (or serve
        # as "ready") an empty bundle the generation can't justify. Persisting
        # here would poison the created_at-DESC read path with a blank card
        # for the rest of the day.
        logger.error(
            "empty_with_signals for %s on the on-demand path — bundle NOT persisted",
            user_email,
        )
        raise EmptyBundleGenerated(
            f"priorities generation produced an unjustified empty bundle for {user_email}"
        )

    today_str = user_today_str(user_email)
    try:
        persist_bundle(user_email, today_str, bundle)
    except Exception:  # noqa: BLE001
        # Persistence is best-effort. A DB failure shouldn't block the
        # response — the next call (or the overnight job) writes a fresh copy.
        logger.exception("Failed to persist priorities bundle for %s", user_email)

    return _bundle_dict_from_metadata(bundle_to_metadata(bundle))


# ─── UC-41: non-blocking priorities (202 + poll) ──────────────────────────────
#
# The pipeline is a 3–5 min multi-agent LLM run. Running it synchronously inside
# the request blew past the 90s axios timeout — the "first load of the day times
# out, then works if you navigate away and back" bug. UC-41 makes POST never
# block: on a cache miss / force_refresh it starts a run and returns 202 +
# run_id; the frontend polls the status endpoint and shows an "indexing"
# skeleton until the bundle is ready.
#
# #9 (background-job hardening): the run executes on a SINGLE-TASK execution of
# the cubie-priorities-batch Cloud Run Job (CPU-allocated, survives instance
# recycling) via backend.jobs.priorities_on_demand_job — not a FastAPI
# BackgroundTask, which Cloud Run CPU-throttles the instant the 202 returns.
# The BackgroundTask path survives only as the local-dev / launch-failure
# fallback. Run state lives in the priorities_runs TABLE
# (rubik_core/priorities/run_state.py), not per-instance memory, so any
# instance can serve any poll and a finished run is never forgotten.

# Job cold start (~30s) + the 3–5 min pipeline.
_PRIORITIES_ETA_SECONDS = 240


def _iso_utc(dt: datetime | None) -> str | None:
    """ISO string for a run timestamp (rows store naive UTC — suffix Z)."""
    if dt is None:
        return None
    return dt.isoformat() if dt.tzinfo else dt.isoformat() + "Z"


def _run_priorities_bg(
    user_email: str, body: PrioritiesRequestBody, run_id: str
) -> None:
    """Inline fallback executor (local dev, or the Job launch failed).

    Runs the same pipeline as backend.jobs.priorities_on_demand_job and
    reports the outcome on the same priorities_runs row. On the deployed
    service this path is CPU-throttled after the response — that's why it is
    only the fallback.
    """
    from rubik_core.telemetry import set_admin_context

    set_admin_context(True)
    try:
        _run_and_persist_priorities(user_email, body)
        run_state.mark_finished(run_id, succeeded=True)
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "[priorities] background run %s failed for %s", run_id, user_email
        )
        run_state.mark_finished(run_id, succeeded=False, error=str(e))


def _dispatch_priorities_run(
    background_tasks: BackgroundTasks,
    run_id: str,
    user_email: str,
    body: PrioritiesRequestBody,
) -> str:
    """Start a run on the durable executor; fall back to an in-process task.

    Returns the executor that took the run ("job" | "inline") after recording
    it on the run row for debuggability.
    """
    args = ["--run-id", run_id, "--user-email", user_email]
    if body.focus:
        args += ["--focus", body.focus]
    if body.max_priorities is not None:
        args += ["--max-priorities", str(body.max_priorities)]
    if body.channels:
        args += ["--channels", ",".join(body.channels)]

    executor, execution_name = submit_background_work(
        background_tasks,
        job_name=PRIORITIES_JOB_NAME,
        module="backend.jobs.priorities_on_demand_job",
        args=args,
        fallback=partial(_run_priorities_bg, user_email, body, run_id),
        label=f"priorities-on-demand:{user_email}",
    )
    run_state.mark_executor(run_id, executor, execution_name)
    return executor


@router.post("/priorities")
async def generate_priorities(
    background_tasks: BackgroundTasks,
    response: Response,
    body: PrioritiesRequestBody | None = None,
    user: UserProfile = Depends(get_current_user),
):
    """Return today's AI Priorities bundle, or enqueue a run (UC-41).

    - Cache hit (a HomeCard for today exists) and not ``force_refresh`` → 200
      with the bundle, same as before.
    - Cache miss or ``force_refresh`` → start the multi-agent pipeline on a
      single-task cubie-priorities-batch Cloud Run Job execution (#9) and return
      **202** with ``{run_id, status: "pending"}``. The pipeline takes 3–5
      min; the client polls ``GET /api/homefeed/priorities/runs/{run_id}`` and
      shows an indexing skeleton until status flips to ``ready`` — so the
      homefeed never eats the 90s axios timeout on a cold cache.
    """
    reject_support_background_work()
    payload = body or PrioritiesRequestBody()

    if not payload.force_refresh:
        cached = await asyncio.to_thread(_load_today_priorities, user.email)
        if cached is not None:
            return PrioritiesResponse(**cached)

    # Miss or force_refresh → start a run (dedupe an already-pending run per
    # user — cross-instance, via the priorities_runs table).
    def _start() -> dict:
        run = run_state.active_run(user.email)
        if run is not None:
            return run
        run = run_state.create_run(user.email, trigger=run_state.TRIGGER_ON_DEMAND)
        _dispatch_priorities_run(background_tasks, run["run_id"], user.email, payload)
        logger.info(
            "[priorities] started run %s for %s (force=%s)",
            run["run_id"],
            user.email,
            payload.force_refresh,
        )
        return run

    run = await asyncio.to_thread(_start)

    response.status_code = 202
    return {
        "run_id": run["run_id"],
        "status": "pending",
        "estimated_completion_seconds": _PRIORITIES_ETA_SECONDS,
        "queued_at": _iso_utc(run.get("queued_at")),
    }


@router.get("/priorities/runs/{run_id}")
async def get_priorities_run(
    run_id: str,
    user: UserProfile = Depends(get_current_user),
):
    """Poll the status of a UC-41 priorities run.

    Returns ``{status: pending|ready|failed}``; ``ready`` includes the bundle.
    Reads only the priorities_runs table (#9) — never per-instance memory —
    so any instance can serve any poll. An unknown run_id (pre-migration id,
    or a legacy in-memory run from an older revision) degrades to "is there a
    fresh card yet?" so the client stays coherent."""
    run = await asyncio.to_thread(run_state.get_run, run_id)

    if run is None:
        cached = await asyncio.to_thread(_load_today_priorities, user.email)
        if cached is not None:
            return {"run_id": run_id, "status": "ready", "bundle": cached}
        return {
            "run_id": run_id,
            "status": "pending",
            "progress": {
                "elapsed_seconds": 0,
                "estimated_seconds": _PRIORITIES_ETA_SECONDS,
            },
        }

    if run["user_email"] != user.email:
        raise HTTPException(status_code=403, detail="run does not belong to you")

    status = run["status"]
    if status == run_state.RUN_FAILED:
        return {
            "run_id": run_id,
            "status": "failed",
            "error": run.get("error") or "priorities pipeline failed",
        }
    if status == run_state.RUN_READY:
        cached = await asyncio.to_thread(_load_today_priorities, user.email)
        if cached is None:
            return {
                "run_id": run_id,
                "status": "failed",
                "error": "pipeline finished but no card was persisted",
            }
        return {"run_id": run_id, "status": "ready", "bundle": cached}

    # Pending. Belt-and-braces: if the fresh card already landed but the
    # executor's state write was lost (Job wrote the card, then died), the
    # card is authoritative — flip the row and serve it.
    queued_at = run.get("queued_at") or datetime.utcnow()
    fresh = await asyncio.to_thread(run_state.fresh_card_since, user.email, queued_at)
    if fresh:
        cached = await asyncio.to_thread(_load_today_priorities, user.email)
        if cached is not None:
            await asyncio.to_thread(run_state.mark_finished, run_id, succeeded=True)
            return {"run_id": run_id, "status": "ready", "bundle": cached}

    elapsed = max(0, int((datetime.utcnow() - queued_at).total_seconds()))
    if elapsed > run_state.RUN_TIMEOUT_SECONDS:
        await asyncio.to_thread(run_state.fail_timed_out, run_id)
        return {
            "run_id": run_id,
            "status": "failed",
            "error": f"run timed out after {run_state.RUN_TIMEOUT_SECONDS}s",
        }
    return {
        "run_id": run_id,
        "status": "pending",
        "progress": {
            "elapsed_seconds": elapsed,
            "estimated_seconds": _PRIORITIES_ETA_SECONDS,
        },
    }


# ─── UC-25 priority memory (dismiss / snooze / resolve) ───────────────────────
#
# These endpoints translate a priority_id (the ephemeral PriorityCandidate.id
# returned by /api/homefeed/priorities) into the durable memory key
# (entity_kind, entity_id, priority_kind) so the user's choice survives
# across pipeline runs.


class _MemoryActionResult(BaseModel):
    state: str
    snoozed_until: str | None = None
    entity_kind: str
    entity_id: str
    priority_kind: str


class _SnoozeBody(BaseModel):
    until: str  # ISO date or datetime; required


class _DismissBody(BaseModel):
    # "occurrence" (default) dismisses just this item — the long-standing
    # behavior of the ✗ button. "series" is only meaningful for a recurring
    # calendar event and suppresses every future occurrence; it is opt-in
    # precisely because widening ✗ silently would make one click far more
    # destructive than it looks.
    scope: Literal["occurrence", "series"] = "occurrence"


class _SlackFollowupBody(BaseModel):
    # Preview-then-send: the first call (confirm=False) returns the proposed
    # nudge text without sending; the manager reviews/edits, then sends with
    # confirm=True. `message`, when provided on the confirm call, overrides the
    # default text.
    confirm: bool = False
    message: str | None = None


def _resolve_prep_debt(user_email: str, event_id: str | None) -> None:
    """UC-26 hook — best-effort resolve of the UC-25 PREP_DEBT for an event.

    Called after a successful meeting-prep run. Memory failures must not
    block the prep response, so we swallow exceptions.
    """
    if not event_id:
        return
    try:
        from rubik_core.priorities import memory as priority_memory
        from rubik_core.priorities.models import EntityKind, PriorityKind

        # Resolve BOTH the legacy prep_debt and the forward-looking
        # upcoming_prep kind — the dreamer now emits upcoming_prep, so
        # running meeting prep must clear whichever one was surfaced.
        for kind in (PriorityKind.PREP_DEBT, PriorityKind.UPCOMING_PREP):
            priority_memory.resolve(
                user_email=user_email,
                entity_kind=EntityKind.EVENT,
                entity_id=str(event_id),
                priority_kind=kind,
                evidence={"kind": "meeting_prep", "event_id": str(event_id)},
                actor="system",
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "UC-26: failed to resolve prep debt for %s / event %s",
            user_email,
            event_id,
            exc_info=True,
        )


def _find_priority_card_in_today(
    user_email: str, priority_id: str
) -> tuple[dict | None, dict | None]:
    """Look up a priority by id across ALL of today's stored HomeCards.

    Returns ``(priority, card_metadata)``. The card is returned alongside
    because its ``generated_at`` is what the dismiss-resurface rule compares
    against — an action that wants to know whether it just cleared the card
    needs both halves.

    An action (dismiss/snooze/resolve) carries the ephemeral
    ``PriorityCandidate.id`` from whatever card the user is *looking at*. A
    Refresh or a /priorities/more run persists a NEW card with regenerated ids,
    so matching only the newest card 404'd actions fired against the visible
    (now-older) card — the "priority not found in today's bundle" error. Scanning
    every card for today finds the id wherever it lives; the id then maps to the
    stable (entity_kind, entity_id, priority_kind) memory key, so the action
    applies correctly regardless of which run produced the card.
    """
    from rubik_core.priorities import DAILY_BRIEFING_CATEGORY, NEXT_WEEK_CATEGORY

    today_str = user_today_str(user_email)
    with SessionLocal() as db:
        cards = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == user_email)
            .filter(HomeCard.date == today_str)
            .filter(HomeCard.category == DAILY_BRIEFING_CATEGORY)
            .order_by(desc(HomeCard.created_at))
            .all()
        )
        # Also scan recent next-week cards — with NO date filter. A next-week
        # card is dated its Friday generation day, but the user may act on it
        # Saturday/Sunday when today_str has moved on; a date filter would 404
        # the action and the dismissal would never reach memory.
        nw_cards = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == user_email)
            .filter(HomeCard.category == NEXT_WEEK_CATEGORY)
            .order_by(desc(HomeCard.created_at))
            .limit(5)
            .all()
        )
    for card in [*cards, *nw_cards]:
        from rubik_core.priorities.cached_visibility import verified_bundle_deals

        verified = verified_bundle_deals(card.metadata_json)
        if verified is None:
            continue
        for p in card.metadata_json.get("priorities") or []:
            if p.get("id") == priority_id:
                if _visible_stored_crm_priorities([p], verified_deals=verified):
                    return p, card.metadata_json
                return None, None
    return None, None


def _find_priority_in_today(user_email: str, priority_id: str) -> dict | None:
    """The priority alone — see :func:`_find_priority_card_in_today`."""
    p, _ = _find_priority_card_in_today(user_email, priority_id)
    return p


# How long a close-date card stays quiet once the AE has answered it. The
# probe re-fires every morning for as long as the condition holds, so a short
# window would just re-nag two days later. The quiet threshold is 21 days, so a
# fortnight is short enough that "I'll sort it out" cannot quietly become
# never.
_DEAL_HANDLED_SNOOZE_DAYS = 14
# ✗ on a recurring condition means "not this one", not "never again". Long
# enough to be a real answer, bounded so one click cannot permanently blind an
# AE to a deal's close date slipping.
_DEAL_DISMISS_SNOOZE_DAYS = 90


def _snooze_only_days(pk: str | None) -> int | None:
    """Days to snooze instead of writing a terminal state, or None to proceed.

    Keyed on the PRIORITY kind, not the entity kind: ``entity_kind="deal"``
    also carries ``neglect_debt`` and ``follow_up_debt``, where a durable
    resolve is the right answer — the deal really can be done with.
    """
    from rubik_core.priorities.models import (
        SNOOZE_ONLY_PRIORITY_KINDS,
        PriorityKind,
    )

    try:
        kind = PriorityKind(pk)
    except (ValueError, TypeError):
        return None
    return _DEAL_HANDLED_SNOOZE_DAYS if kind in SNOOZE_ONLY_PRIORITY_KINDS else None


def _memory_key_from_priority(p: dict) -> tuple[str | None, str | None, str | None]:
    return (
        p.get("entity_kind"),
        p.get("entity_id"),
        p.get("priority_kind"),
    )


def _covered_entity_ids(p: dict) -> list[str]:
    """Every entity_id this card speaks for, primary first.

    A card that aggregates siblings ("Jordan has two overdue To-Dos") carries
    them all in ``covered_entity_ids``; ``entity_id`` is merely whichever one
    the decision-maker cited first. An action has to write a memory row for
    each, or the next run cites a different sibling first and the card the user
    just handled comes straight back. Older cards predate the field and yield
    just the primary.
    """
    primary = p.get("entity_id")
    ids = [str(e) for e in (p.get("covered_entity_ids") or []) if e]
    if primary and str(primary) not in ids:
        ids.insert(0, str(primary))
    return ids


async def _apply_to_covered(
    memory_fn,
    *,
    user_email: str,
    entity_kind: str,
    priority_kind: str,
    entity_ids: list[str],
    **kwargs,
):
    """Apply a ``priority_memory`` state change to every id the card covers.

    Returns the row for the primary (first) id, which is what the caller
    reports back to the UI.
    """
    primary_row = None
    for idx, eid in enumerate(entity_ids):
        row = await asyncio.to_thread(
            memory_fn,
            user_email=user_email,
            entity_kind=entity_kind,
            entity_id=eid,
            priority_kind=priority_kind,
            **kwargs,
        )
        if idx == 0:
            primary_row = row
    return primary_row


def _result_from_row(row, ek: str, eid: str, pk: str) -> _MemoryActionResult:
    return _MemoryActionResult(
        state=row.state if row else "unknown",
        snoozed_until=getattr(row, "snoozed_until", None) if row else None,
        entity_kind=ek,
        entity_id=eid,
        priority_kind=pk,
    )


async def _record_card_action(
    kind: str,
    user: UserProfile,
    priority_id: str,
    p: dict,
    *,
    entity_id: str | None = None,
) -> None:
    """Append an "acted on" card action to the Insights card history.

    Called only after the action itself succeeded (the DM went out, the Help
    Desk accepted the write), so the history never claims an action that did
    not happen. Off the event loop, and best effort: it never raises, so a
    telemetry hiccup can never turn a delivered action into an error. The
    snooze each action also writes is recorded separately by
    ``priority_memory``; this row says WHICH action it was.
    """
    try:
        from rubik_core.insights.priority_events import record_priority_event
        from rubik_core.rag.config import slugify_department

        await asyncio.to_thread(
            partial(
                record_priority_event,
                user_email=user.email,
                kind=kind,
                priority_id=priority_id,
                entity_kind=p.get("entity_kind"),
                entity_id=entity_id or p.get("entity_id"),
                priority_kind=p.get("priority_kind"),
                actor=user.email,
                department_slug=slugify_department(user.department or "") or None,
            )
        )
    except Exception:  # noqa: BLE001 — the action already happened
        logger.warning(
            "priority_events: %s not recorded for %s", kind, user.email, exc_info=True
        )


def _promise_writeback(user_email: str, p: dict) -> None:
    """✓ on a "Your To-Do" or "You said you would…" card marks the promise
    itself kept.

    Same reasoning as the offboard write-back: memory suppression alone would
    hide the card while the My To-Dos page (and the promises channel on the
    next run) still called it open. Best-effort — a ledger failure must not
    undo the memory action the user asked for. Dismiss (✗) deliberately does
    NOT touch the ledger: "stop showing me this" is not "I kept my word".
    """
    if p.get("entity_kind") != "promise":
        return
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.promises import store

        with SessionLocal() as db:
            changed = 0
            for pid in _covered_entity_ids(p):
                row = store.get_owned(db, user_email, pid)
                # ``keep_from_card`` also takes a heard ``suggested`` promise
                # (accept, then kept) — most people a "You said you would…"
                # card reaches have no My To-Dos page to accept it on.
                if row is not None and store.keep_from_card(db, row, actor=user_email):
                    changed += 1
            if changed:
                db.commit()
    except Exception:  # noqa: BLE001 — the memory action already happened
        logger.warning("promise write-back failed for %s", user_email, exc_info=True)


def _offboard_item_writeback(user_email: str, p: dict, status: str) -> None:
    """Inherited cards close their offboard_items too, not just memory rows.

    Memory suppression alone would hide the card from THIS user's feed while
    the Handoffs checklist (and the inherited channel on the next run) still
    called the item open. Best-effort: a write-back failure must not undo the
    memory action the user asked for.
    """
    if p.get("entity_kind") != "offboard_item":
        return
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.offboarding import store as offboard_store

        with SessionLocal() as db:
            changed = offboard_store.resolve_items_by_entity(
                db,
                successor_email=user_email,
                entity_ids=_covered_entity_ids(p),
                status=status,
                actor=user_email,
            )
            db.commit()
        if changed:
            logger.info(
                "[offboard] %s inherited item(s) marked %s by %s",
                changed,
                status,
                user_email,
            )
    except Exception:  # noqa: BLE001 — the memory action already succeeded
        logger.warning(
            "[offboard] item write-back failed for %s", user_email, exc_info=True
        )


@router.post("/priorities/{priority_id}/dismiss", response_model=_MemoryActionResult)
async def dismiss_priority(
    priority_id: str,
    body: _DismissBody | None = None,
    user: UserProfile = Depends(get_current_user),
):
    p = await asyncio.to_thread(_find_priority_in_today, user.email, priority_id)
    if p is None:
        raise HTTPException(
            status_code=404, detail="priority not found in today's bundle"
        )
    ek, eid, pk = _memory_key_from_priority(p)
    if not ek or not eid or not pk:
        raise HTTPException(
            status_code=400,
            detail="priority is not entity-tagged (pre-UC-24); cannot be dismissed",
        )

    # A recurring meeting gives each occurrence its own entity_id, so a normal
    # dismissal cannot reach next week's. scope="series" keys the row on the
    # SERIES id instead; apply_post_filter checks that key for every occurrence.
    scope = (body.scope if body else "occurrence") or "occurrence"
    if scope == "series":
        series_id = p.get("series_entity_id")
        if not series_id:
            raise HTTPException(
                status_code=400,
                detail="this priority is not part of a recurring series",
            )
        # The series key stands in for every occurrence on its own.
        target_ids = [str(series_id)]
    else:
        target_ids = _covered_entity_ids(p)

    from rubik_core.priorities import memory as priority_memory

    # ✗ on a RECURRING condition must also be bounded. `dismiss` only revives
    # when a cited signal carries a timestamp newer than the dismissal, and a
    # deal whose defining property is that nothing is happening never produces
    # one — so a plain dismissal here is permanent in everything but name.
    from rubik_core.priorities.models import PriorityKind as _PK

    if pk == _PK.CLOSE_DATE_AT_RISK.value:
        row = await _apply_to_covered(
            priority_memory.snooze,
            user_email=user.email,
            entity_kind=ek,
            priority_kind=pk,
            entity_ids=target_ids,
            actor=user.email,
            until=datetime.utcnow() + timedelta(days=_DEAL_DISMISS_SNOOZE_DAYS),
            priority_id=priority_id,
        )
        return _result_from_row(row, ek, target_ids[0], pk)

    row = await _apply_to_covered(
        priority_memory.dismiss,
        user_email=user.email,
        entity_kind=ek,
        priority_kind=pk,
        entity_ids=target_ids,
        actor=user.email,
        priority_id=priority_id,
    )
    _offboard_item_writeback(user.email, p, "dismissed")
    return _result_from_row(row, ek, target_ids[0], pk)


@router.post("/priorities/{priority_id}/snooze", response_model=_MemoryActionResult)
async def snooze_priority(
    priority_id: str,
    body: _SnoozeBody,
    user: UserProfile = Depends(get_current_user),
):
    p = await asyncio.to_thread(_find_priority_in_today, user.email, priority_id)
    if p is None:
        raise HTTPException(
            status_code=404, detail="priority not found in today's bundle"
        )
    ek, eid, pk = _memory_key_from_priority(p)
    if not ek or not eid or not pk:
        raise HTTPException(
            status_code=400,
            detail="priority is not entity-tagged (pre-UC-24); cannot be snoozed",
        )

    raw = body.until.strip()
    try:
        until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if until.tzinfo is not None:
            until = until.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="`until` must be ISO datetime")

    if until <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="`until` must be in the future")

    from rubik_core.priorities import memory as priority_memory

    row = await _apply_to_covered(
        priority_memory.snooze,
        user_email=user.email,
        entity_kind=ek,
        priority_kind=pk,
        entity_ids=_covered_entity_ids(p),
        actor=user.email,
        until=until,
        priority_id=priority_id,
    )
    return _result_from_row(row, ek, eid, pk)


@router.post("/priorities/{priority_id}/resolve", response_model=_MemoryActionResult)
async def resolve_priority(
    priority_id: str,
    user: UserProfile = Depends(get_current_user),
):
    p = await asyncio.to_thread(_find_priority_in_today, user.email, priority_id)
    if p is None:
        raise HTTPException(
            status_code=404, detail="priority not found in today's bundle"
        )
    ek, eid, pk = _memory_key_from_priority(p)
    if not ek or not eid or not pk:
        raise HTTPException(
            status_code=400,
            detail="priority is not entity-tagged (pre-UC-24); cannot be resolved",
        )
    from rubik_core.priorities import memory as priority_memory

    snooze_days = _snooze_only_days(pk)
    if snooze_days is not None:
        # ``resolve_manual`` stamps a `resolved` row that never expires, and
        # ``reopen_entity_everywhere`` deliberately skips `manual_resolve`
        # evidence, so on a recurring condition one click would permanently
        # blind the reader to that situation. A bounded snooze instead.
        # (Wanting it gone for good is what ✗ / Settings → AI Personalization
        # are for, and both of those are reversible from the history list.)
        row = await _apply_to_covered(
            priority_memory.snooze,
            user_email=user.email,
            entity_kind=ek,
            priority_kind=pk,
            entity_ids=_covered_entity_ids(p),
            actor=user.email,
            until=datetime.utcnow() + timedelta(days=snooze_days),
            priority_id=priority_id,
        )
        return _result_from_row(row, ek, eid, pk)

    row = await _apply_to_covered(
        priority_memory.resolve_manual,
        user_email=user.email,
        entity_kind=ek,
        priority_kind=pk,
        entity_ids=_covered_entity_ids(p),
        actor=user.email,
        priority_id=priority_id,
    )
    _offboard_item_writeback(user.email, p, "done")
    _promise_writeback(user.email, p)
    return _result_from_row(row, ek, eid, pk)


# ─── Manager Slack nudge for a report's team-scoped priority ──────────────────
#
# A manager/exec sees, on their own briefing, a *report's* about-to-be-due EOS
# item (a team-scoped PriorityItem carries ``target_email`` / ``target_label``).
# This endpoint lets them fire a one-tap Slack DM nudge to that report, posted
# AS the Cubie bot on their behalf. Preview-then-send: confirm=False returns the
# proposed text without sending; confirm=True delivers it.


def _manager_manages_target(user: UserProfile, target_email: str) -> bool:
    """True iff ``user`` manages the report at ``target_email`` (own + direct
    reports + led-team), and the target is not the caller themselves.

    Fails CLOSED: any EOS lookup error returns False (no nudge is sent).

    The email match is case-INSENSITIVE on purpose. ``target_email`` is
    lowercased when the card is built (``eos_agent._owner_bits``) while
    ``eos_people.email`` is stored verbatim, so an exact match silently 403s
    the follow-up for anyone whose row carries a capital letter — live today
    for one person, and the failure looks to the manager like the button is
    broken rather than like a data mismatch.
    """
    try:
        from backend.routers.eos._authz import _compute_scope, person_by_email

        with SessionLocal() as db:
            scope = _compute_scope(user, db)
            # Both lookups go through the shared ``person_by_email`` helper so
            # this rule lives in exactly one place — it folds case on
            # both sides and excludes soft-deleted rows, same semantics as the
            # inline ``func.lower()`` match this replaces. A miss on ``own``
            # would quietly drop the "don't let someone nudge themselves"
            # guard rather than fail closed.
            target = person_by_email(db, target_email)
            if target is None:
                return False
            own = person_by_email(db, user.email)
            own_pid = own.id if own is not None else None
            return target.id in scope.owner_pids and target.id != own_pid
    except Exception:  # noqa: BLE001
        logger.warning(
            "slack-followup: authz lookup failed for %s → %s",
            getattr(user, "email", "?"),
            target_email,
            exc_info=True,
        )
        return False


def _default_followup_text(p: dict, user: UserProfile) -> str:
    """A short, friendly nudge from Cubie on the manager's behalf.

    Written to the REPORT, about their own items. The card's ``title`` and
    ``why_it_matters`` are the manager's view of the situation ("Follow up with
    Jordan Hayes on overdue To-Dos" / "Jordan has two overdue To-Dos…"); quoting
    either at Jordan addresses them in the third person about a task that is
    really their manager's. The entity labels are the actual EOS items, so those
    are what the DM names.
    """
    label = (p.get("target_label") or "").strip()
    first_name = label.split()[0] if label else "there"
    flagged_by = (getattr(user, "name", None) or user.email or "your manager").strip()

    items = [str(x).strip() for x in (p.get("covered_entity_labels") or []) if x]
    if not items:
        primary = (p.get("entity_label") or "").strip()
        if primary:
            items = [primary]

    if len(items) == 1:
        opening = (
            f"Hi {first_name} — a quick nudge from {PRODUCT_NAME} on *{items[0]}*. "
            "Could you share where it stands?"
        )
    elif items:
        listed = "\n".join(f"• {item}" for item in items)
        opening = (
            f"Hi {first_name} — a quick nudge from {PRODUCT_NAME} on {len(items)} of "
            f"your open items:\n{listed}\n\nCould you share where they stand?"
        )
    else:
        # No entity label survived (pre-covered-ids card, or an unlabelled row).
        # Still better than quoting the manager's card title back at them.
        opening = (
            f"Hi {first_name} — a quick nudge from {PRODUCT_NAME}: one of your EOS "
            "items is due for an update. Could you share where it stands?"
        )

    return f"{opening}\n\n_Flagged by {flagged_by} via {PRODUCT_NAME}._"


@router.post("/priorities/{priority_id}/slack-followup")
async def slack_followup_priority(
    priority_id: str,
    body: _SlackFollowupBody,
    user: UserProfile = Depends(get_current_user),
):
    p = await asyncio.to_thread(_find_priority_in_today, user.email, priority_id)
    if p is None:
        raise HTTPException(
            status_code=404, detail="priority not found in today's bundle"
        )

    target_email = p.get("target_email")
    if not target_email:
        raise HTTPException(
            status_code=400,
            detail="This priority has no follow-up target (it isn't a team member's item)",
        )

    manages = await asyncio.to_thread(_manager_manages_target, user, target_email)
    if not manages:
        raise HTTPException(
            status_code=403,
            detail="You do not manage the owner of this priority",
        )

    default_text = _default_followup_text(p, user)

    # Preview only — do not send.
    if not body.confirm:
        return {
            "preview": default_text,
            "target_email": target_email,
            "target_label": p.get("target_label"),
        }

    text = (body.message or "").strip() or default_text

    # ── Send the DM. A Slack/transport failure returns a clean JSON status,
    #    never a 500 that loses the manager's click. ──
    try:
        from backend.services.slack_service import SlackBotClient

        client = SlackBotClient()
        if not client.is_available:
            return {"sent": False, "reason": "slack_not_connected"}

        result = await asyncio.to_thread(client.send_dm, target_email, text=text)
    except Exception:  # noqa: BLE001
        logger.warning("slack-followup: DM to %s failed", target_email, exc_info=True)
        return {"sent": False, "reason": "slack_send_failed"}

    if result is None:
        return {"sent": False, "reason": "recipient_not_in_workspace"}

    await _record_card_action("slack_followup", user, priority_id, p)

    # Snooze the manager's OWN card for this priority 3 days so it stops
    # nagging them now that the ball is in the report's court. Every entity the
    # card covers, not just the primary — a nudge about "two overdue To-Dos"
    # that silenced only one of them saw the card return the next morning,
    # which reads exactly like the DM never went out. Best-effort: a memory
    # failure must not turn a delivered DM into an error.
    ek, eid, pk = _memory_key_from_priority(p)
    if ek and eid and pk:
        try:
            from rubik_core.priorities import memory as priority_memory

            until = datetime.utcnow() + timedelta(days=3)
            await _apply_to_covered(
                priority_memory.snooze,
                user_email=user.email,
                entity_kind=ek,
                priority_kind=pk,
                entity_ids=_covered_entity_ids(p),
                actor=user.email,
                until=until,
                priority_id=priority_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "slack-followup: post-send snooze failed for %s (%s/%s/%s)",
                user.email,
                ek,
                eid,
                pk,
                exc_info=True,
            )
    else:
        # No entity key means nothing was suppressed — the card WILL be back
        # tomorrow looking untouched. Previously silent; say so.
        logger.warning(
            "slack-followup: DM to %s delivered but the card is not "
            "entity-tagged (%s/%s/%s) — it will resurface for %s",
            target_email,
            ek,
            eid,
            pk,
            user.email,
        )

    return {"sent": True, "target_email": target_email}


# ─── Dismissed & snoozed history (AI Personalization → manage) ────────────────


class _PriorityHistoryItem(BaseModel):
    entity_kind: str
    entity_id: str
    priority_kind: str
    state: str  # "dismissed" | "snoozed" | "resolved"
    snoozed_until: str | None = None
    label: str  # human-readable summary derived from the memory key
    last_state_change_at: str | None = None


class _ReopenBody(BaseModel):
    entity_kind: str
    entity_id: str
    priority_kind: str


def _humanize_priority(
    entity_kind: str | None, entity_id: str | None, priority_kind: str | None
) -> str:
    """Best-effort readable label from a (kind, entity) memory key.

    The memory table doesn't snapshot the priority's title, so the list shows
    e.g. "Follow up debt · deal 12345". Enough for the user to recognize and
    reopen it.
    """
    kind = (priority_kind or "").replace("_", " ").strip().capitalize() or "Priority"
    ent = (entity_kind or "").replace("_", " ").strip()
    label = kind
    if ent:
        label += f" · {ent}"
        if entity_id:
            label += f" {entity_id}"
    return label


@router.get("/priorities/history", response_model=list[_PriorityHistoryItem])
async def list_priority_history(
    user: UserProfile = Depends(get_current_user),
):
    """The user's handled priorities (dismissed, snoozed, resolved), newest first.

    Powers the management list in Settings → AI Personalization, where the
    user can reopen something they hid so it can resurface. Resolved items are
    included because ✓ is the one PERMANENT choice — excluding them would leave
    it with no undo anywhere in the product.
    """
    from rubik_core.priorities import memory as priority_memory

    rows = await asyncio.to_thread(priority_memory.load_inactive, user.email)
    deal_ids = {
        str(row.entity_id)
        for row in rows
        if str(row.entity_kind).casefold() == EntityKind.DEAL.value and row.entity_id
    }
    visible_deals: set[str] = set()
    if deal_ids:
        from backend.routers.agents import visible_hubspot_deal_ids

        visible_deals = await asyncio.to_thread(visible_hubspot_deal_ids, deal_ids)
    rows = [
        row
        for row in rows
        if str(row.entity_kind).casefold() != EntityKind.DEAL.value
        or (row.entity_id and str(row.entity_id) in visible_deals)
    ]
    return [
        _PriorityHistoryItem(
            entity_kind=r.entity_kind,
            entity_id=r.entity_id,
            priority_kind=r.priority_kind,
            state=r.state,
            snoozed_until=r.snoozed_until,
            label=_humanize_priority(r.entity_kind, r.entity_id, r.priority_kind),
            last_state_change_at=r.last_state_change_at or None,
        )
        for r in rows
    ]


@router.post("/priorities/history/reopen", response_model=_MemoryActionResult)
async def reopen_priority(
    body: _ReopenBody,
    user: UserProfile = Depends(get_current_user),
):
    """Reopen a dismissed / snoozed / resolved priority (clears any snooze)."""
    ek, eid, pk = body.entity_kind, body.entity_id, body.priority_kind
    if not ek or not eid or not pk:
        raise HTTPException(
            status_code=400,
            detail="entity_kind, entity_id and priority_kind are required",
        )
    if ek.strip().casefold() == EntityKind.DEAL.value:
        from backend.routers.agents import visible_hubspot_deal_ids

        visible = await asyncio.to_thread(visible_hubspot_deal_ids, {eid})
        if eid not in visible:
            raise HTTPException(status_code=404, detail="priority not found")
    from rubik_core.priorities import memory as priority_memory

    row = await asyncio.to_thread(
        priority_memory.reopen,
        user_email=user.email,
        entity_kind=ek,
        entity_id=eid,
        priority_kind=pk,
        actor=user.email,
    )
    return _result_from_row(row, ek, eid, pk)


# ─── "Get me other important things" ──────────────────────────────────────────
#
# Shown after the user clears all of today's AI Priorities. Two-step:
#   1. Surface the user's active overnight ``agent_recommendations`` (the pool
#      the nightly UC-10 agent already ingested) that aren't already in today's
#      priorities card.
#   2. If that pool is empty, start a deeper pipeline run (max 10) on the same
#      durable executor as /priorities (#9) and tell the caller to poll the
#      read endpoint. The run is idempotent (last-write-wins on today's card),
#      so a stray double-run is wasteful, not harmful; the priorities_runs
#      dedupe blocks the obvious rapid double-click — across instances, unlike
#      the old in-process guard.


class MoreResponse(BaseModel):
    status: str  # "recommendations" | "running" | "already_running"
    items: list[PriorityItem] = []
    run_id: str | None = None


def _confidence_to_urgency(confidence: float | None) -> str:
    c = confidence or 0.0
    if c >= 0.8:
        return "high"
    if c >= 0.5:
        return "medium"
    return "low"


# Friendly title prefix per overnight recommendation type.
_REC_TYPE_TITLE: dict[str, str] = {
    "stale_lead": "Re-engage",
    "stage_stuck": "Unstick",
    "new_product_match": "New product fit",
    "market_trigger": "Market trigger",
}


def _drive_document_fields(rec) -> dict | None:
    """If this recommendation came from a Drive-label signal, return the
    document-shaped card fields (title label, web link, entity tags) so it
    renders as a 'document' with an 'Open in Drive' action. Returns None for
    non-Drive recommendations, leaving the default (deal/HubSpot) shape intact.
    """
    sources = getattr(rec, "sources", None) or []
    for s in sources:
        if not isinstance(s, dict) or s.get("source") != "drive":
            continue
        payload = s.get("payload") or {}
        return {
            "label": payload.get("file_name") or rec.lead_name or "document",
            "web_view_link": payload.get("web_view_link"),
            "file_id": payload.get("file_id"),
        }
    return None


def _recommendation_to_item(rec) -> PriorityItem:
    """Project an ``AgentRecommendation`` row into the priorities card shape,
    tagged ``origin="recommendation"`` so the frontend routes its actions to
    the ``/agents/recommendations`` endpoints instead of the priority-memory
    ones.
    """
    # Drive-label recommendations render as documents with an Open-in-Drive link.
    doc = _drive_document_fields(rec)
    if doc:
        return PriorityItem(
            id=rec.id,
            title=f"Review: {doc['label']}",
            why_it_matters=rec.recommendation_text or "",
            urgency=_confidence_to_urgency(rec.confidence),
            source_channels=["drive"],
            source_links=[doc["web_view_link"]] if doc["web_view_link"] else [],
            suggested_action=rec.next_action or "",
            priority_kind=None,
            entity_kind="document",
            entity_id=doc["file_id"] or rec.lead_id,
            entity_label=doc["label"],
            origin="recommendation",
        )

    label = rec.lead_name or rec.lead_id or "lead"
    prefix = _REC_TYPE_TITLE.get(rec.type, "Follow up")
    return PriorityItem(
        id=rec.id,
        title=f"{prefix}: {label}",
        why_it_matters=rec.recommendation_text or "",
        urgency=_confidence_to_urgency(rec.confidence),
        source_channels=["hubspot"],
        source_links=[],
        suggested_action=rec.next_action or "",
        priority_kind=None,
        entity_kind="deal" if rec.lead_id else None,
        entity_id=rec.lead_id,
        entity_label=rec.lead_name,
        origin="recommendation",
    )


def _today_card_entity_ids(user_email: str) -> set[str]:
    """``entity_id``s already represented in today's priorities card (any
    state) — so /priorities/more doesn't re-surface a recommendation for
    something the pipeline already put in front of the user today.
    """
    from rubik_core.priorities import DAILY_BRIEFING_CATEGORY

    today_str = user_today_str(user_email)
    ids: set[str] = set()
    with SessionLocal() as db:
        card = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == user_email)
            .filter(HomeCard.date == today_str)
            .filter(HomeCard.category == DAILY_BRIEFING_CATEGORY)
            .order_by(desc(HomeCard.created_at))
            .first()
        )
        if card and card.metadata_json:
            from rubik_core.priorities.cached_visibility import verified_bundle_deals

            verified = verified_bundle_deals(card.metadata_json)
            if verified is None:
                return ids
            for p in _visible_stored_crm_priorities(
                card.metadata_json["priorities"], verified_deals=verified
            ):
                eid = p.get("entity_id")
                if eid:
                    ids.add(str(eid))
    return ids


def _fetch_other_recommendations(user_email: str, limit: int = 5) -> list[PriorityItem]:
    """Active overnight recommendations not already in today's card."""
    from backend.routers.agents import fetch_top_recommendations

    seen = _today_card_entity_ids(user_email)
    items: list[PriorityItem] = []
    with SessionLocal() as db:
        rows = fetch_top_recommendations(db, user_email, limit=limit + len(seen) + 5)
        for r in rows:
            if r.lead_id and str(r.lead_id) in seen:
                continue
            items.append(_recommendation_to_item(r))
            if len(items) >= limit:
                break
    return items


@router.post("/priorities/more", response_model=MoreResponse)
async def get_more_priorities(
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(get_current_user),
):
    """Surface more to do after the user has cleared today's priorities.

    Returns active overnight recommendations when any exist; otherwise starts
    a deeper pipeline run (max 10, so it digs past today's top-5; the UC-25
    post-filter drops what the user already resolved) on the durable executor
    and returns ``status="running"`` so the UI can poll the read endpoint and
    swap in the fresh card when it lands.
    """
    reject_support_background_work()
    items = await asyncio.to_thread(_fetch_other_recommendations, user.email, 5)
    if items:
        return MoreResponse(status="recommendations", items=items)

    def _start() -> dict | None:
        if run_state.active_run(user.email) is not None:
            return None
        run = run_state.create_run(user.email, trigger=run_state.TRIGGER_MORE)
        _dispatch_priorities_run(
            background_tasks,
            run["run_id"],
            user.email,
            PrioritiesRequestBody(max_priorities=10, force_refresh=True),
        )
        return run

    run = await asyncio.to_thread(_start)
    if run is None:
        return MoreResponse(status="already_running")
    logger.info(
        "[priorities-more] no recs for %s — started run %s",
        user.email,
        run["run_id"],
    )
    return MoreResponse(status="running", run_id=run["run_id"])
