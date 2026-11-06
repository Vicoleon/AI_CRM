"""Cubie in Google Meet — the one read behind the call checklist in Meet's side panel.

The add-on (``frontend/src/meetAddon/``) runs in an iframe inside Google Meet.
It knows one thing about where it is: the meeting code Meet hands it
(``getMeetingInfo().meetingCode``). This router turns that code, for the
signed-in AE and only ever against the AE's OWN calendar and call sessions,
into the call Cubie already knows about and the checklist that call has to
cover.

**Why it exists.** The checklist already renders on the Today's Calls row, but
an AE on a call is looking at Meet, not at Cubie. The AE should see what to ask
"before and during the call"; the side panel is where "during" happens.

**What it cannot do, stated once so nobody builds on it: open itself.** Google
gives an add-on no way to launch when a meeting starts — the AE opens it from
Meet's Activities panel (``docs/meet_addon/README.md``). What this endpoint does
instead is make that one click land on the right call with nothing to choose.

**Three ways a code resolves, most specific first:**

1. ``calendar`` — one of today's client calls on the AE's calendar carries this
   code. It is read through the same ``calendar_scan.scan_day`` as the Today's
   Calls screen, so the two surfaces cannot disagree about which call this is.
   If the AE captured the call, its products are the ones ATTACHED to the call
   session — exactly what the wrap-up will measure coverage against. If not (or
   if nothing is attached), they are what this AE picked for this client last
   time, labelled as remembered.
2. ``session`` — no calendar row, but the AE has a call session on this code
   from the last :data:`SESSION_LOOKBACK` (an ad-hoc call Cubie created, or a
   day the calendar could not be read).
3. ``none`` — an internal meeting, a call not on the calendar, or a
   client-hosted Meet Cubie never saw. Said as such; the panel then offers every
   live checklist to pick from rather than pretending it knows.

**A pure read.** No Meet writes, no session rows, no remembered picks. Every
write about a call still happens on the Cubie screen that owns it; a pick made
in the side panel lives in the panel.

**Gates, in the CCS order.** The audience gate (``sales_meet_addon`` in Admin →
Feature Access) is a dependency, so it runs before the ``MEET_ADDON_ENABLED``
404 in the handler: someone outside the audience gets the same 403 whether the
feature is on or off. The router also sits under the copilot ``core`` parent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from rubik_core.brand import PRODUCT_NAME
from rubik_core.call_capture import calendar_scan
from rubik_core.call_capture.provenance import session_source_visible
from rubik_core.call_capture.scripts import remembered_products
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession
from rubik_core.database.call_scripts import CallSessionScript
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.tenancy.crm_visibility import visible_source_ids

logger = logging.getLogger(__name__)

FEATURE_KEY = "sales_meet_addon"

#: How old a session on the same code may be and still count as "this call"
#: when no calendar row claims the code. A recurring series reuses ONE Meet
#: code for every occurrence, so an unbounded lookup would hand this week's
#: panel last week's session (and its wrap-up link). Eighteen hours covers a
#: call captured in the morning and joined in the afternoon, and stops short
#: of yesterday's occurrence of a daily series.
SESSION_LOOKBACK = timedelta(hours=18)


def require_meet_addon(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """The audience gate — who Cubie answers inside Meet. See the module docstring."""
    if not feature_allows(FEATURE_KEY, user, ignore_kill_switch=True):
        raise HTTPException(
            status_code=403,
            detail=f"{PRODUCT_NAME} in Google Meet is not enabled for your account yet.",
        )
    return user


router = APIRouter(prefix="/meet-addon", tags=["meet-addon"])


def _fold(email: str | None) -> str:
    """Same fold as ``backend/routers/calls.py::_fold`` — the rows are CCS's."""
    return (email or "").strip().lower()


def _product_label(key: str) -> str:
    """A readable name for a product with no live checklist to take one from.

    Server-side twin of ``productKeyLabel`` in ``frontend/src/api/calls.ts``,
    so the panel never has to import that module (and the axios client behind
    it) into Meet's iframe. An authored ``name`` always wins over this.
    """
    words = " ".join((key or "").replace("_", " ").replace("-", " ").split())
    if not words:
        return "Unnamed product"
    return words[0].upper() + words[1:]


def _reset(db: Session) -> None:
    """Hand the next read a live transaction after a failure was swallowed.

    On Postgres a failed statement aborts the transaction, and every later
    statement on the same session raises ``InFailedSqlTransaction`` — which
    would turn a failure this module reports ("could not load the checklists")
    into the 500 it promises not to return. Everything here is a read, so a
    rollback discards nothing. Same move as ``rubik_core/features/policy.py``.
    """
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 — a failed rollback is not worth a 500 either
        logger.debug(
            "[meet-addon] rollback after a swallowed failure failed", exc_info=True
        )


def _pick_row(rows: list[dict]) -> dict | None:
    """The one calendar row a code means, when several share it.

    Usually zero or one. Several happens when an AE pastes their own standing
    Meet link into more than one client invite: the call that is live wins,
    then the one whose start is nearest to now.
    """
    if not rows:
        return None
    live = [row for row in rows if row.get("is_live")]
    if live:
        return live[0]

    def distance(row: dict) -> int:
        minutes = row.get("starts_in_minutes")
        return abs(minutes) if isinstance(minutes, int) else 10**9

    return min(rows, key=distance)


def _own_session(db: Session, ae_email: str, session_id: Any) -> CallSession | None:
    """The row's session, re-read and re-checked against the caller.

    ``scan_day`` already scopes sessions to the AE; the ownership check is here
    anyway because this is the one place a session id turns into data on
    screen, and a check that lives only upstream is one refactor from gone.
    """
    if not session_id:
        return None
    session = db.get(CallSession, str(session_id))
    if session is None or _fold(session.ae_email) != ae_email:
        return None
    return session


def _recent_session_on_code(
    db: Session, ae_email: str, code: str, now_utc: datetime
) -> CallSession | None:
    """The AE's newest session on this code inside :data:`SESSION_LOOKBACK`."""
    cutoff = now_utc - SESSION_LOOKBACK
    return (
        db.query(CallSession)
        .filter(
            CallSession.ae_email == ae_email,
            func.lower(CallSession.meet_meeting_code) == code,
            CallSession.created_at >= cutoff,
        )
        .order_by(CallSession.created_at.desc())
        .first()
    )


def _attached_keys(db: Session, session_id: str) -> list[str]:
    """The products the call session runs against, in the order they were attached."""
    rows = (
        db.query(CallSessionScript)
        .filter(CallSessionScript.call_session_id == session_id)
        .order_by(CallSessionScript.attached_at, CallSessionScript.product_key)
        .all()
    )
    keys: list[str] = []
    for row in rows:
        key = (row.product_key or "").strip().lower()
        if key and key not in keys:
            keys.append(key)
    return keys


def _company(row: dict | None, session: CallSession | None) -> dict | None:
    """Which client this is, and how sure Cubie is — never more sure than it is.

    * ``confirmed`` — the AE bound it when they captured the call.
    * ``suggested`` — one clear HubSpot match from the guests' email domains,
      not confirmed by anyone yet.
    * ``uncertain`` — the calendar row could not settle it (several matches,
      nothing found, personal addresses, or HubSpot unreachable). The panel
      says so instead of naming a company.
    """
    attrs = (getattr(session, "attrs", None) or {}) if session is not None else {}
    block = (row or {}).get("company") or {}
    if session is not None and session.hubspot_company_id:
        same = block.get("hubspot_company_id") == session.hubspot_company_id
        return {
            "state": "confirmed",
            "hubspot_company_id": session.hubspot_company_id,
            "name": attrs.get("company_name") or (block.get("name") if same else None),
            "domain": block.get("domain") if same else None,
        }
    if row is None:
        return None
    if block.get("match") == "unique" and block.get("hubspot_company_id"):
        return {
            "state": "suggested",
            "hubspot_company_id": block["hubspot_company_id"],
            "name": block.get("name"),
            "domain": block.get("domain"),
        }
    return {
        "state": "uncertain",
        "hubspot_company_id": None,
        "name": None,
        "domain": block.get("domain"),
        "reason": block.get("match") or "unknown",
    }


def _session_sources_visible(session: CallSession) -> bool:
    return session_source_visible(session)


def build_context(
    ae_email: str,
    code: str,
    *,
    db: Session,
    scan_day: Callable[..., dict] | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """The side panel's whole payload for one AE and one meeting code.

    ``scan_day`` and ``now_utc`` are injection points for tests. Never raises
    on a Calendar, HubSpot or script-table failure: each one is reported in its
    own field (``calendar_available``, ``company.state``, ``catalogue_available``)
    because "we could not look" and "there is nothing" lead to different
    actions, and a panel that collapsed them would tell an AE on a live client
    call that Cubie has no checklist for them.
    """
    now = now_utc or datetime.utcnow()
    scan = scan_day or calendar_scan.scan_day
    try:
        day = scan(ae_email, db=db)
    except Exception:  # noqa: BLE001 — scan_day is documented never to raise
        logger.exception("[meet-addon] calendar scan failed for %s", ae_email)
        day = {"calendar_available": False, "meetings": []}
    # ``scan_day`` swallows its own failures too (its script block is fail-soft),
    # so the transaction may be aborted even when it returned normally.
    _reset(db)
    calendar_available = bool(day.get("calendar_available"))
    rows = [
        row
        for row in day.get("meetings") or []
        if isinstance(row, dict) and row.get("meeting_code") == code
    ]
    row = _pick_row(rows)

    session: CallSession | None = None
    if row is not None:
        session = _own_session(db, ae_email, row.get("call_session_id"))
    else:
        session = _recent_session_on_code(db, ae_email, code, now)
    # A session and its saved company label may outlive pipeline access. Prove
    # every linked CRM source before exposing its ID, label or attached picks.
    if session is not None and not _session_sources_visible(session):
        session = None
        if row is not None:
            row = {**row, "call_session_id": None, "capture_state": "not_started"}
    # Calendar rows are freshly scanned, but a suggested company ID and its
    # remembered products still need a live complete-association proof here.
    block = (row or {}).get("company") or {}
    company_id = block.get("hubspot_company_id")
    if (
        company_id
        and str(company_id)
        not in visible_source_ids({str(company_id)}, set(), set())[0]
    ):
        row = {
            **row,
            "company": {
                "match": "unavailable",
                "hubspot_company_id": None,
                "name": None,
                "domain": block.get("domain"),
            },
            "scripts": {},
        }
    match = "calendar" if row is not None else ("session" if session else "none")

    try:
        catalogue = calendar_scan.live_script_catalogue(db)
        catalogue_available = True
    except Exception:  # noqa: BLE001 — reported, not raised; see the docstring
        logger.exception("[meet-addon] could not load the script catalogue")
        _reset(db)
        catalogue, catalogue_available = {}, False

    # What the wrap-up will grade comes first. A captured call with nothing
    # attached (captured from a row whose checklist block failed, or from the
    # ad-hoc form, which takes no products) falls back to what this AE picked
    # for the call's BOUND company last time — the checklist is still worth
    # reading, and the panel labels it remembered rather than attached.
    keys: list[str] = []
    products_source = "none"
    if session is not None:
        keys = _attached_keys(db, session.id)
        if keys:
            products_source = "call"
        elif session.hubspot_company_id:
            try:
                keys = remembered_products(db, ae_email, session.hubspot_company_id)
            except Exception:  # noqa: BLE001 — a missing hint, not a failed panel
                logger.exception("[meet-addon] could not read remembered products")
                _reset(db)
                keys = []
    else:
        scripts = (row or {}).get("scripts") or {}
        keys = [
            key
            for key in (scripts.get("selected_product_keys") or [])
            if isinstance(key, str) and key
        ]
    if keys and products_source == "none":
        products_source = "remembered"

    call = None
    if row is not None or session is not None:
        call = {
            # A session carries no title of its own; only the calendar row does.
            "title": (row or {}).get("title"),
            "start_local": (row or {}).get("start_local"),
            "is_live": bool((row or {}).get("is_live")),
            "call_session_id": session.id if session else None,
            "capture_state": (row or {}).get("capture_state")
            or ("session_exists" if session else "not_started"),
            "confidential": bool(session and session.capture_consent == "suppressed"),
        }

    logger.info(
        "[meet-addon] %s opened the panel on %s: match=%s products=%d source=%s",
        ae_email,
        code,
        match,
        len(keys),
        products_source,
    )
    return {
        "meeting_code": code,
        "match": match,
        "calendar_available": calendar_available,
        "calls_sharing_code": len(rows),
        "call": call,
        "company": _company(row, session),
        "products_source": products_source,
        "selected": [
            {
                "product_key": key,
                "name": (catalogue.get(key) or {}).get("name") or _product_label(key),
                "has_checklist": key in catalogue,
            }
            for key in keys
        ],
        "catalogue_available": catalogue_available,
        # Every live checklist, sorted by name — the panel renders the selected
        # ones from here and offers the rest to pick mid-call. One read for
        # every product; the whole catalogue is a few hundred short lines.
        "catalogue": sorted(
            catalogue.values(),
            key=lambda section: (
                (section.get("name") or "").lower(),
                section["product_key"],
            ),
        ),
    }


@router.get("/status")
async def meet_addon_status(user: UserProfile = Depends(require_meet_addon)):
    """200 only when the panel would work for the caller: on the roster AND on.

    The client cannot see ``MEET_ADDON_ENABLED`` and fails CLOSED on it
    (``frontend/src/context/AuthContext.tsx``), so Today's Calls asks this once
    before pointing an AE at Meet. Without it, a roster-only gate would tell the
    pilot to open a panel that answers "not switched on yet". Same gates and the
    same order as ``/context``: 403 outside the audience, then 404 when off.
    """
    if not settings.MEET_ADDON_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    return {"enabled": True}


@router.get("/context")
async def meet_addon_context(
    meeting_code: str = Query(..., max_length=64),
    user: UserProfile = Depends(require_meet_addon),
    db: Session = Depends(get_db),
):
    """The call and checklist behind one Meet meeting code, for the caller only."""
    if not settings.MEET_ADDON_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    ae_email = _fold(getattr(user, "email", None))
    if not ae_email:
        raise HTTPException(status_code=403, detail="no authenticated identity")
    code = calendar_scan.normalize_meeting_code(meeting_code)
    if not code:
        raise HTTPException(
            status_code=422, detail="That is not a Google Meet meeting code."
        )
    # Calendar and HubSpot are blocking clients, so the scan runs off the loop,
    # exactly as GET /api/calls/scheduled runs it.
    return await run_in_threadpool(build_context, ae_email, code, db=db)
