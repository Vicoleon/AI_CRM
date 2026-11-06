"""Defer a deal — set it aside until a follow-up date, with a note.

    GET  /api/sales/deals/deferred           the caller's deferred deals
    POST /api/sales/deals/{deal_id}/defer    {follow_up_on, note, deal_name?}
    POST /api/sales/deals/{deal_id}/undefer

Long-term deals sit in the pipeline for months with no real close date, and
they keep showing up as noise on My Day, in the daily checks and on the morning
priorities card. What "deferred" hides, and from whom, is
``rubik_core/sales/deal_defer.py``; the table is
``rubik_core/database/deal_defers.py``.

Mounted on the copilot-gated ``core`` parent in ``backend/main.py``, so
``require_full_app_access`` already applies; every endpoint adds the
``deal_defer`` roster (Admin → Feature Access). It does not ride
``require_cockpit``: the Defer button also sits on the morning priorities card,
which people outside the cockpit rosters see.

**The HubSpot task is a write-through, through the wrap-up ledger.** Each defer
creates one task on the deal — assigned to the deal's owner, due at 9:00 on the
follow-up date in the owner's timezone, the note as its body — via
``HubSpotWriteClient.create_task`` and the ``call_hubspot_writes`` idempotency
key, the same path a call wrap-up's tasks take (``backend/routers/calls.py``).
Deferring twice with the same date and note reuses the defer row, so it keys the
same and writes one task. A failed task does NOT undo the defer: the deal is
off the rep's lists either way, the response says the task did not land, and
re-sending the same defer retries just the task.

**Out of scope, on purpose:** the deal's stage and close date in HubSpot are
never touched, and there is no "Deferred" stage — the sales leaders still owe
the stage boundaries and the hold/defer criteria.

Runs in the request path because ``HUBSPOT_WRITE_TOKEN`` is mounted only on the
main service. No model call, so no AI Cost category.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import get_current_user
from backend.routers.calls import (
    _already_written,
    _idempotency_key,
    _reserve_write,
    _stamp_failure,
    _stamp_success,
)
from backend.routers.cockpit import _hubspot_deal_url
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.call_capture import CallHubspotWrite
from rubik_core.database.commitments import Commitment
from rubik_core.database.db import get_db
from rubik_core.database.deal_defers import DealDefer
from rubik_core.database.models import User
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.retrieval.hubspot_api import HubSpotAPIError
from rubik_core.retrieval.hubspot_write import (
    HubSpotWriteClient,
    HubSpotWriteError,
    HubSpotWriteRefused,
    friendly_write_error,
)
from rubik_core.sales import deal_defer as dd

logger = logging.getLogger(__name__)

#: HubSpot deal ids are numeric. Anything else is refused before it reaches a
#: URL or a query.
_DEAL_ID = re.compile(r"^\d{1,20}$")

#: The task's due time on the follow-up date, in the owner's timezone. Morning,
#: so the task is waiting in their HubSpot queue when the deal reappears.
_DUE_LOCAL_TIME = time(9, 0)

#: HubSpot shows the subject in a one-line queue; keep it readable there.
_SUBJECT_MAX = 200

_TASK_PATH = "/crm/v3/objects/tasks"


def require_deal_defer(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """403 when the caller's team is not on the ``deal_defer`` roster."""
    if not feature_allows("deal_defer", user):
        raise HTTPException(
            status_code=403,
            detail="Deferring deals is not switched on for your team yet.",
        )
    return user


router = APIRouter(prefix="/sales/deals", tags=["deal-defer"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class DeferRequest(BaseModel):
    follow_up_on: date
    note: str
    #: The name the row showed. Used only when HubSpot cannot be read for it.
    deal_name: str | None = None


class DeferredDeal(BaseModel):
    deal_id: str
    deal_name: str | None = None
    #: ISO date — the day the deal comes back on its own.
    follow_up_on: str
    note: str
    owner_email: str
    owner_name: str | None = None
    deferred_by: str
    #: ISO date the defer was made.
    deferred_on: str
    #: pending | written | failed — whether the HubSpot follow-up task landed.
    hubspot_task_status: str
    hubspot_task_error: str | None = None
    hubspot_url: str | None = None


class DeferredList(BaseModel):
    items: list[DeferredDeal]


class TaskOutcome(BaseModel):
    #: written | already_written | write_failed | in_progress | unconfirmed.
    #: Only the first two mean the task is on the deal.
    status: str
    message: str | None = None


class DeferResponse(BaseModel):
    defer: DeferredDeal
    #: created | replaced | unchanged
    outcome: str
    hubspot_task: TaskOutcome


class UndeferResponse(BaseModel):
    deal_id: str
    message: str


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _is_admin(user: UserProfile) -> bool:
    return "admin" in (user.roles or [])


def _check_deal_id(deal_id: str) -> str:
    deal_id = (deal_id or "").strip()
    if not _DEAL_ID.match(deal_id):
        raise HTTPException(status_code=422, detail="That is not a HubSpot deal id.")
    return deal_id


def _visible_workspace_deals(deal_ids: set[str]) -> set[str]:
    """Recheck live pipeline ownership before exposing or changing stored rows."""
    from backend.routers.agents import visible_hubspot_deal_ids

    return visible_hubspot_deal_ids(deal_ids)


def _names(db: Session, emails: set[str]) -> dict[str, str]:
    from sqlalchemy import func

    wanted = {e for e in emails if e}
    if not wanted:
        return {}
    rows = (
        db.query(func.lower(User.email), User.name)
        .filter(func.lower(User.email).in_(sorted(wanted)))
        .all()
    )
    return {email: name for email, name in rows if name}


def _out(info: dd.DeferInfo, names: dict[str, str] | None = None) -> DeferredDeal:
    return DeferredDeal(
        deal_id=info.deal_id,
        deal_name=info.deal_name,
        follow_up_on=info.follow_up_on.isoformat(),
        note=info.note,
        owner_email=info.owner_email,
        owner_name=(names or {}).get(info.owner_email),
        deferred_by=info.deferred_by,
        deferred_on=dd.local_day(info.deferred_by, info.created_at).isoformat(),
        hubspot_task_status=info.hubspot_task_status,
        hubspot_task_error=info.hubspot_task_error,
        hubspot_url=_hubspot_deal_url(info.deal_id),
    )


def _open_commitment_owners(db: Session, deal_id: str) -> set[str]:
    """Who holds an open Cubie commitment on this deal — Cubie's own answer to
    "is this deal on your My Day?", which needs no HubSpot read."""
    from sqlalchemy import func

    rows = (
        db.query(func.lower(Commitment.owner_email))
        .filter(
            Commitment.hubspot_deal_id == deal_id,
            Commitment.closed_at.is_(None),
        )
        .distinct()
        .all()
    )
    return {email for (email,) in rows if email}


def _read_deal(
    client: HubSpotWriteClient, deal_id: str
) -> tuple[str | None, str | None]:
    """(deal name, owner email) from HubSpot. Raises on a failed read."""
    deal = client.get_object("deals", deal_id, ["dealname", "hubspot_owner_id"])
    props = deal.get("properties") or {}
    owner_id = str(props.get("hubspot_owner_id") or "")
    owner_email = None
    if owner_id:
        entry = (client.read_client.owner_directory() or {}).get(owner_id) or {}
        owner_email = _fold(entry.get("email")) or None
    name = (props.get("dealname") or "").strip() or None
    return name, owner_email


def _due_at(owner_email: str, follow_up_on: date) -> datetime:
    """9:00 on the follow-up date in the owner's timezone, as an aware instant."""
    from rubik_core.priorities.temporal import resolve_user_now

    tz = resolve_user_now(owner_email).now_local.tzinfo
    return datetime.combine(follow_up_on, _DUE_LOCAL_TIME, tzinfo=tz)


def _task_properties(row: DealDefer, *, by_name: str) -> dict:
    from rubik_core.retrieval.hubspot_write import _epoch_ms

    name = row.deal_name or f"deal {row.hubspot_deal_id}"
    footer = (
        f"Deferred in {PRODUCT_NAME} by {by_name} on "
        f"{dd.local_day(row.deferred_by, row.created_at).isoformat()}. "
        f"The deal comes back on the rep's {PRODUCT_NAME} lists on "
        f"{row.follow_up_on.isoformat()}."
    )
    return {
        "hs_timestamp": _epoch_ms(_due_at(row.owner_email, row.follow_up_on)),
        "hs_task_subject": f"Follow up: {name} (deferred in {PRODUCT_NAME})"[
            :_SUBJECT_MAX
        ],
        # The rep's note IS the body; the footer says where it came from.
        "hs_task_body": f"{row.note}\n\n{footer}",
        "hs_task_status": "NOT_STARTED",
        "hs_task_type": "TODO",
    }


def _write_task(
    db: Session, client: HubSpotWriteClient, row: DealDefer, *, by_name: str
) -> TaskOutcome:
    """Create the defer's HubSpot task once. Synchronous; run in a threadpool.

    The key is the defer row's id in the wrap-up ledger's session slot — a
    defer has no call session — with the deal as the record and ``deal_defer``
    as the property. The row is reused for the same date and note, so a
    double-click or a retry claims the same key, and the ledger's UNIQUE
    constraint lets exactly one of them write.
    """
    key = _idempotency_key(
        f"deal_defer:{row.id}", "tasks", row.hubspot_deal_id, "deal_defer"
    )
    if _already_written(db, key):
        done = (
            db.query(CallHubspotWrite)
            .filter(CallHubspotWrite.idempotency_key == key)
            .first()
        )
        row.hubspot_task_status = "written"
        row.hubspot_task_id = getattr(done, "hubspot_object_id", None)
        row.hubspot_task_error = None
        db.commit()
        return TaskOutcome(status="already_written")

    properties = _task_properties(row, by_name=by_name)
    ledger = _reserve_write(
        db,
        key=key,
        session_id=None,
        item_id=None,
        method="POST",
        path=_TASK_PATH,
        body=properties,
    )
    if ledger is None:
        # Another request holds the key: a double-click still writing it, or an
        # attempt that died mid-call (held for the ledger's lease, because the
        # task may have landed). Not a success to report — the row stays
        # pending, and the Deferred list offers a retry.
        return TaskOutcome(
            status="in_progress",
            message=(
                "The HubSpot task for this defer is already being created. Check "
                "Deferred deals on My Day in a few minutes."
            ),
        )
    try:
        response = client.create_task(
            properties,
            owner_email=row.owner_email,
            deal_id=row.hubspot_deal_id,
        )
    except (HubSpotWriteRefused, HubSpotWriteError, HubSpotAPIError) as exc:
        # HubSpotAPIError too: the write's gates READ live state (the pipeline
        # fence, the owner directory), so an unset read token surfaces here.
        message = friendly_write_error("the follow-up task", exc)
        logger.warning(
            "[deal-defer] task for deal %s failed: %s", row.hubspot_deal_id, message
        )
        _stamp_failure(db, [ledger], message)
        row.hubspot_task_status = "failed"
        row.hubspot_task_error = message[:500]
        db.commit()
        return TaskOutcome(status="write_failed", message=message)
    except Exception:  # noqa: BLE001 — the defer itself already stands
        # Something other than HubSpot's answer went wrong (an unreadable
        # response, a dropped connection after the request left). The task may
        # have landed, so the claim is NOT released — the ledger holds it for
        # its lease rather than risk a second task — and the rep is told to look
        # before retrying.
        logger.exception(
            "[deal-defer] task for deal %s is unconfirmed", row.hubspot_deal_id
        )
        message = (
            f"{PRODUCT_NAME} could not confirm the HubSpot follow-up task. Check the deal in "
            "HubSpot before retrying."
        )
        row.hubspot_task_error = message
        db.commit()
        return TaskOutcome(status="unconfirmed", message=message)
    _stamp_success(db, [ledger], response)
    row.hubspot_task_status = "written"
    row.hubspot_task_id = str(response.get("id") or "") or None
    row.hubspot_task_error = None
    db.commit()
    return TaskOutcome(status="written")


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/deferred", response_model=DeferredList, response_model_exclude_none=True)
def list_deferred(
    user: UserProfile = Depends(require_deal_defer),
    db: Session = Depends(get_db),
):
    """The caller's deferred deals — the ones they own or deferred — soonest back first.

    This is where Undefer lives: a deferred deal is off every other list the
    rep sees, so without this list it could not be found again before its date.
    """
    me = _fold(user.email)
    today = dd.reader_today(me)
    mine = [
        info
        for info in dd.active_defers(db, today=today)
        if me in (info.owner_email, info.deferred_by)
    ]
    visible = _visible_workspace_deals({info.deal_id for info in mine})
    mine = [info for info in mine if info.deal_id in visible]
    names = _names(db, {i.owner_email for i in mine})
    return DeferredList(items=[_out(i, names) for i in mine])


@router.post(
    "/{deal_id}/defer", response_model=DeferResponse, response_model_exclude_none=True
)
async def defer_deal(
    deal_id: str,
    body: DeferRequest,
    user: UserProfile = Depends(require_deal_defer),
    db: Session = Depends(get_db),
):
    """Defer one deal until ``follow_up_on`` and write its HubSpot task.

    Only the deal's owner may defer it: its HubSpot owner, or the AE who holds
    an OPEN Cubie commitment on it (an admin may act for them). Not a past call
    or a closed commitment: a defer hides the deal from its current owner too,
    so a rep who once worked a reassigned deal must not be able to bury it. Every
    Defer button sits on a deal the reader owns, so the check costs nothing in
    practice — it is what stops one rep hiding another rep's deal.
    """
    deal_id = _check_deal_id(deal_id)
    if deal_id not in await run_in_threadpool(_visible_workspace_deals, {deal_id}):
        raise HTTPException(status_code=404, detail="That deal is unavailable.")
    me = _fold(user.email)
    today = dd.reader_today(me)
    try:
        note = dd.validate(body.follow_up_on, body.note, today=today)
    except dd.DeferInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    local_owners = _open_commitment_owners(db, deal_id)
    client = HubSpotWriteClient()
    try:
        deal_name, hubspot_owner = await run_in_threadpool(_read_deal, client, deal_id)
    except HubSpotWriteRefused as exc:
        raise HTTPException(
            status_code=404, detail="That deal is unavailable."
        ) from exc
    except (HubSpotWriteError, HubSpotAPIError) as exc:
        if "HTTP 404" in str(exc):
            raise HTTPException(
                status_code=404,
                detail="That deal no longer exists in HubSpot, so it was not deferred.",
            ) from exc
        logger.warning("[deal-defer] deal %s read failed: %s", deal_id, exc)
        deal_name, hubspot_owner = None, None
        if me not in local_owners and not _is_admin(user):
            raise HTTPException(
                status_code=502,
                detail=(
                    "HubSpot could not be read to confirm this deal is yours, so it "
                    "was not deferred. Try again in a minute."
                ),
            ) from exc

    if me != hubspot_owner and me not in local_owners and not _is_admin(user):
        raise HTTPException(
            status_code=403,
            detail="Only the deal's owner can defer it.",
        )

    owner = hubspot_owner or me
    row, outcome = dd.defer(
        db,
        deal_id=deal_id,
        deal_name=deal_name or body.deal_name,
        owner_email=owner,
        deferred_by=me,
        follow_up_on=body.follow_up_on,
        note=note,
        today=today,
    )

    task = (
        TaskOutcome(status="already_written")
        if row.hubspot_task_status == "written"
        else await run_in_threadpool(
            _write_task, db, client, row, by_name=user.name or me
        )
    )

    record_audit(
        db,
        actor_email=me,
        action="DEAL_DEFERRED",
        target=f"deal:{deal_id}",
        status="failure"
        if task.status in ("write_failed", "unconfirmed")
        else "success",
        details={
            "defer_id": row.id,
            "owner_email": owner,
            "follow_up_on": row.follow_up_on.isoformat(),
            "outcome": outcome,
            "hubspot_task": task.status,
        },
    )
    info = dd.DeferInfo.from_row(row)
    return DeferResponse(
        defer=_out(info, _names(db, {info.owner_email})),
        outcome=outcome,
        hubspot_task=task,
    )


@router.post(
    "/{deal_id}/undefer",
    response_model=UndeferResponse,
    response_model_exclude_none=True,
)
def undefer_deal(
    deal_id: str,
    user: UserProfile = Depends(require_deal_defer),
    db: Session = Depends(get_db),
):
    """Bring a deferred deal back now.

    The HubSpot follow-up task is left where it is: it is still a reasonable
    reminder, and deleting a record in HubSpot is not something this button
    should do on the rep's behalf. The response says so.
    """
    deal_id = _check_deal_id(deal_id)
    if deal_id not in _visible_workspace_deals({deal_id}):
        raise HTTPException(status_code=404, detail="That deal is unavailable.")
    me = _fold(user.email)
    current = (
        db.query(DealDefer)
        .filter(DealDefer.hubspot_deal_id == deal_id, DealDefer.ended_at.is_(None))
        .first()
    )
    if current is None:
        raise HTTPException(status_code=404, detail="This deal is not deferred.")
    if me not in (current.owner_email, current.deferred_by) and not _is_admin(user):
        raise HTTPException(
            status_code=403,
            detail="Only the deal's owner or the person who deferred it can undefer it.",
        )
    dd.undefer(db, deal_id=deal_id, by=me)
    record_audit(
        db,
        actor_email=me,
        action="DEAL_UNDEFERRED",
        target=f"deal:{deal_id}",
        details={"defer_id": current.id},
    )
    message = "The deal is back on your lists."
    if current.hubspot_task_status == "written":
        message += (
            " Its follow-up task is still in HubSpot — close it there if you no "
            "longer need it."
        )
    return UndeferResponse(deal_id=deal_id, message=message)
