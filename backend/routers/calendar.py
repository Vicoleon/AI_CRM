import logging
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.db import get_db
from rubik_core.database.models import AuditLog
from rubik_core.tools.calendar import GoogleCalendarClient

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/calendar",
    tags=["Calendar"],
)


def _log_audit_action(db: Session, user_email: str, action: str, details: str):
    logger.info(f"AuditLog: {user_email} -> {action}")
    try:
        log_entry = AuditLog(
            actor_email=user_email, action=action, status="success", details=details
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to write AuditLog for {user_email}: {e}")
        db.rollback()


@router.get("/events")
def get_calendar_events(
    max_results: int = 10,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """
    Fetches upcoming events for the authenticated user's calendar.
    Requires domain-wide delegation to be configured.
    """
    # STRICT RULE: Always use authenticated user's email, never impersonated
    # The `get_current_user` method handles this for us cleanly if impersonation
    # was configured to mask the main email, but to guarantee it, we use the user.email.
    # To be extremely safe, we ensure it is tracking the session owner.
    true_email = user.email

    _log_audit_action(
        db, true_email, "ACCESS_CALENDAR_EVENTS", f"Fetched up to {max_results} events"
    )

    client = GoogleCalendarClient(user_email=true_email)
    if not client.service:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar integration is not configured or failed to initialize.",
        )

    try:
        events = client.get_upcoming_events(max_results=max_results)
        return events
    except Exception as e:
        logger.error(f"Error serving /events for {true_email}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch calendar events")


@router.get("/availability")
def get_calendar_availability(
    start_time: str,
    end_time: str,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Checks free/busy status for the authenticated user within a timeframe.
    start_time and end_time must be ISO 8601 strings.
    """
    true_email = user.email

    _log_audit_action(
        db,
        true_email,
        "ACCESS_CALENDAR_AVAILABILITY",
        f"Checked availability from {start_time} to {end_time}",
    )

    client = GoogleCalendarClient(user_email=true_email)
    if not client.service:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar integration is not configured or failed to initialize.",
        )

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))

        availability = client.get_availability(start_time=start_dt, end_time=end_dt)
        return availability
    except ValueError as ve:
        raise HTTPException(
            status_code=400, detail=f"Invalid date format. Use ISO 8601. Error: {ve}"
        )
    except Exception as e:
        logger.error(f"Error serving /availability for {true_email}: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to fetch calendar availability"
        )


# ─── UC-39: focus-block endpoint ──────────────────────────────────────────────


from datetime import timedelta, timezone

from pydantic import BaseModel


def _parse_hhmm(value) -> time | None:
    """Parse a stored "HH:MM" working-hours value, or None if invalid/empty."""
    if not value or not isinstance(value, str):
        return None
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return time(h, m)
    return None


def _user_working_hours(user_email: str) -> tuple[time, time]:
    """The user's local working-hours window as ``(start, end)`` times.

    Reads ``UserPreference.working_hours_start/end`` (General tab). Falls back
    to 09:00–18:00 on anything unset, malformed, or inverted, so the focus-block
    search always has a sane window. Best-effort: never raises.
    """
    default = (time(9, 0), time(18, 0))
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import UserPreference

        with SessionLocal() as db:
            row = (
                db.query(
                    UserPreference.working_hours_start,
                    UserPreference.working_hours_end,
                )
                .filter(UserPreference.user_email == user_email)
                .first()
            )
        if not row:
            return default
        start = _parse_hhmm(row[0]) or default[0]
        end = _parse_hhmm(row[1]) or default[1]
        return (start, end) if end > start else default
    except Exception:  # noqa: BLE001 — working hours are best-effort
        return default


class FocusBlockRequest(BaseModel):
    title: str
    duration_minutes: int = 30
    priority_id: str | None = None  # for audit trail; not used in event body


class FocusBlockResponse(BaseModel):
    event_id: str
    start_iso: str
    end_iso: str
    html_link: str | None = None


@router.post("/focus-block", response_model=FocusBlockResponse)
def create_focus_block(
    body: FocusBlockRequest,
    user: UserProfile = Depends(get_current_user),
):
    """Drop a focus block on the user's primary calendar at the next free slot.

    Searches the next 24h for the first gap ≥ duration_minutes inside the
    9am-6pm work window. Returns 409 with `no_free_slot` if none exists.
    Powers the "Schedule 30 min focus" smart action on AI Priorities cards.
    """
    if body.duration_minutes < 5 or body.duration_minutes > 240:
        raise HTTPException(status_code=400, detail="duration_minutes must be 5..240")

    client = GoogleCalendarClient(user_email=user.email)
    if not getattr(client, "service", None):
        raise HTTPException(status_code=500, detail="Calendar service not initialized")

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=24)
    now_z = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    horizon_z = horizon.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        avail = client.get_availability(start_time=now_z, end_time=horizon_z)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Availability fetch failed for {user.email}: {exc}")
        raise HTTPException(status_code=503, detail="Calendar availability unavailable")

    # The 9am-6pm work window is the *user's* local 9-6, not UTC.
    try:
        tzinfo = ZoneInfo(client.get_calendar_timezone())
    except Exception:  # noqa: BLE001
        tzinfo = timezone.utc

    # get_availability() returns {"is_busy": ..., "busy_slots": {email: [{start,end}]}}
    busy_periods = []
    for _email, slots in (avail or {}).get("busy_slots", {}).items():
        for b in slots or []:
            try:
                bs = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
                be = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
                busy_periods.append((bs, be))
            except (KeyError, ValueError, AttributeError):
                continue
    busy_periods.sort()

    # Honor the user's configured working hours (General tab); default 9–6.
    wh_start, wh_end = _user_working_hours(user.email)

    duration = timedelta(minutes=body.duration_minutes)
    now_local = now.astimezone(tzinfo)
    horizon_local = horizon.astimezone(tzinfo)
    cursor = now_local
    chosen_start: datetime | None = None
    while cursor < horizon_local:
        wd_start = datetime.combine(cursor.date(), wh_start, tzinfo)
        wd_end = datetime.combine(cursor.date(), wh_end, tzinfo)
        slot_cursor = max(cursor, wd_start)
        for bs, be in busy_periods:
            if be <= slot_cursor:
                continue
            if bs >= wd_end:
                break
            if bs - slot_cursor >= duration:
                chosen_start = slot_cursor
                break
            slot_cursor = max(slot_cursor, be)
        if chosen_start is None and slot_cursor + duration <= wd_end:
            chosen_start = slot_cursor
        if chosen_start is not None:
            break
        cursor = datetime.combine((cursor + timedelta(days=1)).date(), wh_start, tzinfo)

    if chosen_start is None:
        raise HTTPException(status_code=409, detail="no_free_slot")

    chosen_end = chosen_start + duration
    start_iso = chosen_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = chosen_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    description = (
        f"Focus block created by {PRODUCT_NAME}.\n"
        f"Linked priority id: {body.priority_id or '(unspecified)'}"
    )
    try:
        event = client.create_event(
            summary=body.title[:200],
            start_time=start_iso,
            end_time=end_iso,
            description=description,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"create_event failed for {user.email}: {exc}")
        raise HTTPException(status_code=502, detail="Failed to create calendar event")

    if isinstance(event, dict) and event.get("error"):
        raise HTTPException(status_code=502, detail=str(event["error"]))

    return FocusBlockResponse(
        event_id=event.get("id", ""),
        start_iso=start_iso,
        end_iso=end_iso,
        html_link=event.get("htmlLink"),
    )
