"""Offboard handoff endpoints — offboard a user, read the dossier, work the list.

Mounted UNGATED by the copilot pilot (identity/admin section of main.py) on
purpose: a successor can sit in any department, and inheriting a departed
teammate's open work is org-critical, not a pilot toy. Every endpoint carries
its own explicit gate instead:

- offboarding someone: ``admin_required`` (it deletes the users row).
- reading a record / working its items / reading the departed person's
  priorities history: the record's successor, an org-wide exec
  (``sees_all_departments`` — e.g. the CEO), or an admin. Reads of a departed
  person's history are audit-logged.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import admin_required, get_current_user
from backend.jobs.launcher import (
    reject_support_background_work,
    require_automations_http,
    submit_background_work,
)
from rubik_core.database.db import get_db
from rubik_core.database.models import Asset, HomeCard, OffboardRecord, User
from rubik_core.database.schemas import UserProfile
from rubik_core.offboarding import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/offboarding", tags=["offboarding"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class OffboardBody(BaseModel):
    email: str
    successor_email: str
    # False only for junk/test accounts: still creates the record and
    # reassigns assets, but skips the exit sweep.
    run_sweep: bool = True


class ItemStatusBody(BaseModel):
    status: str = Field(pattern="^(open|done|dismissed)$")


def _item_out(item) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "label": item.label,
        "detail": item.detail,
        "due": item.due,
        "urgency": item.urgency,
        "entity_kind": item.entity_kind,
        "entity_id": item.entity_id,
        "source_links": item.source_links or [],
        "status": item.status,
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        "resolved_by": item.resolved_by,
    }


def _record_out(
    db: Session, record: OffboardRecord, *, with_items: bool = False
) -> dict:
    visible_items = store.visible_items_for_record(db, record)
    out = {
        "id": record.id,
        "user_email": record.user_email,
        "user_name": record.user_name,
        "department": record.department,
        "successor_email": record.successor_email,
        "created_by": record.created_by,
        "offboarded_at": record.offboarded_at.isoformat(),
        "sweep_status": record.sweep_status,
        "status": record.status,
        "open_items": sum(1 for i in visible_items if i.status == "open"),
        "total_items": len(visible_items),
    }
    if with_items:
        from rubik_core.offboarding.sweep import _fallback_narrative, _render_dossier

        # Stored dossiers may contain the same stale CRM prose as their items.
        # The live-visible checklist is the only safe source for this response.
        meta = record.dossier_meta if isinstance(record.dossier_meta, dict) else {}
        google_ok = bool(meta.get("google_last_pass"))
        narrative = _fallback_narrative(record, visible_items, google_ok)
        out["dossier_md"] = _render_dossier(record, visible_items, narrative, google_ok)
        out["dossier_meta"] = {
            "item_count": len(visible_items),
            "google_last_pass": google_ok,
            "generated_at": meta.get("generated_at"),
        }
        out["sweep_detail"] = []
        out["items"] = [_item_out(i) for i in visible_items]
    return out


def _history_card_out(card: HomeCard, *, eos_enabled: bool) -> dict | None:
    from rubik_core.priorities.cached_visibility import render_cached_briefing
    from rubik_core.priorities.persistence import NEXT_WEEK_CATEGORY
    from rubik_core.priorities.privacy import is_private_gmail_bundle

    # A mixed run may contain a work-looking card whose model context included
    # the owner's personal Gmail. None of that run belongs in a successor's
    # history, including cards written before the current read path shipped.
    if is_private_gmail_bundle(card.metadata_json):
        return None

    # The stored Markdown predates today's pipeline assignments and may carry
    # both foreign CRM facts and EOS data. Rebuild from checked structured rows.
    content = render_cached_briefing(
        card.metadata_json,
        card.date,
        next_week=card.category == NEXT_WEEK_CATEGORY,
        eos_enabled=eos_enabled,
        compact=not eos_enabled,
    )
    if content is None:
        return None
    return {
        "id": card.id,
        "date": card.date,
        "category": card.category,
        "content_md": content,
        "created_at": card.created_at.isoformat() if card.created_at else None,
    }


def _can_view(user: UserProfile, record: OffboardRecord) -> bool:
    email = (user.email or "").strip().lower()
    return user.is_admin or user.sees_all_departments or record.successor_email == email


def _get_record_authorized(
    record_id: str, user: UserProfile, db: Session
) -> OffboardRecord:
    record = store.get_record(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="offboard record not found")
    if not _can_view(user, record):
        raise HTTPException(status_code=403, detail="not your handoff")
    return record


# ── The offboard action ──────────────────────────────────────────────────────


@router.post("/offboard", dependencies=[Depends(reject_support_background_work)])
async def offboard_user(
    body: OffboardBody,
    background_tasks: BackgroundTasks,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Offboard one user: durable record → assets to successor → roster delete
    → exit sweep on the worker.

    One transaction up to the roster delete, so a failure can't leave a
    half-offboarded person. Run this BEFORE IT suspends the Google account —
    the sweep's email/calendar last pass only works while the account is
    impersonable.
    """
    if body.run_sweep:
        require_automations_http()
    try:
        record = store.create_record(
            db,
            user_email=body.email,
            successor_email=body.successor_email,
            created_by=admin.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    departed = record.user_email

    # Docs go to the manager, never deleted (roster-reconciliation convention).
    # Case-insensitive match: assets.owner_email is stored verbatim.
    reassigned = (
        db.query(Asset)
        .filter(func.lower(Asset.owner_email) == departed)
        .update({Asset.owner_email: record.successor_email}, synchronize_session=False)
    )

    # The roster delete itself. Config rows (roles, groups, policies,
    # preferences, slack credentials, priority memory) cascade at the DB
    # level; history rows (home_cards, chat, feedback, recommendations)
    # deliberately survive.
    user_row = db.query(User).filter(func.lower(User.email) == departed).first()
    if user_row is not None:
        db.delete(user_row)

    # Cubie for Chrome keys. Already dead once the users row is gone (the key
    # resolves to nobody), but revoked here too so Settings and the audit trail
    # say why, and a mailbox later reused by someone new starts with none.
    from rubik_core.browser_extension.connections import revoke_all

    browsers_disconnected = revoke_all(db, departed, reason="offboarded")

    db.commit()

    # Files they attached in Ask Cubie are PRIVATE: never handed to the
    # successor (unlike the Library docs above). Removed, so a mailbox later
    # given to someone else starts with none. Best-effort: the counts are kept.
    from rubik_core.chat_files.service import purge_owner

    try:
        chat_files = await run_in_threadpool(purge_owner, db, departed)
    except Exception:  # noqa: BLE001 — never undo an offboarding over this
        logger.exception("offboard: private chat files not purged for %s", departed)
        chat_files = {"removed": 0, "failed": -1}

    record_audit(
        db,
        actor_email=admin.email,
        action="USER_OFFBOARD",
        target=departed,
        details={
            "record_id": record.id,
            "successor": record.successor_email,
            "assets_reassigned": int(reassigned or 0),
            "chat_files_removed": chat_files["removed"],
            "chat_files_failed": chat_files["failed"],
            "browsers_disconnected": int(browsers_disconnected or 0),
            "run_sweep": body.run_sweep,
        },
    )

    executor = "skipped"
    if body.run_sweep:
        executor, _ = submit_background_work(
            background_tasks,
            module="backend.jobs.offboard_sweep_job",
            args=["--record-id", record.id],
            fallback=lambda rid=record.id: _run_sweep_inline(rid),
            label=f"offboard-sweep {departed}",
        )
    else:
        record.sweep_status = "skipped"
        record.status = "closed"
        db.commit()

    return {
        "record": _record_out(db, record),
        "assets_reassigned": int(reassigned or 0),
        "sweep_executor": executor,
    }


def _run_sweep_inline(record_id: str) -> None:
    """BackgroundTask fallback for local dev / job-launch failure."""
    from rubik_core.offboarding.sweep import run_offboard_sweep

    run_offboard_sweep(record_id)


@router.post(
    "/records/{record_id}/resweep",
    dependencies=[Depends(reject_support_background_work)],
)
async def resweep(
    record_id: str,
    background_tasks: BackgroundTasks,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Re-run the exit sweep (idempotent — items dedup on kind+entity+label).

    For the 'we offboarded before IT suspended, but the sweep raced the
    suspension' case, or a plain failure."""
    require_automations_http()
    record = store.get_record(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="offboard record not found")
    record.sweep_status = "pending"
    db.commit()
    executor, _ = submit_background_work(
        background_tasks,
        module="backend.jobs.offboard_sweep_job",
        args=["--record-id", record.id],
        fallback=lambda rid=record.id: _run_sweep_inline(rid),
        label=f"offboard-resweep {record.user_email}",
    )
    return {"record_id": record.id, "sweep_executor": executor}


@router.get("/suggest-successor")
async def suggest_successor(
    email: str,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Default successor = the person's EOS-chart boss (may be null)."""
    return {"suggestion": store.suggest_successor(db, email)}


# ── Reading + working the handoff ────────────────────────────────────────────


@router.get("/records")
async def list_records(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """My inherited handoffs — or all of them for admins / org-wide exec."""
    if user.is_admin or user.sees_all_departments:
        records = store.all_records(db)
    else:
        records = store.records_for_successor(db, user.email)
    return {"records": [_record_out(db, r) for r in records]}


@router.get("/records/{record_id}")
async def get_record(
    record_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = _get_record_authorized(record_id, user, db)
    return {"record": _record_out(db, record, with_items=True)}


@router.post("/items/{item_id}/status")
async def set_item_status(
    item_id: str,
    body: ItemStatusBody,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from rubik_core.database.models import OffboardItem

    item = db.get(OffboardItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if store.is_eos_item(item) and not store.eos_enabled():
        raise HTTPException(status_code=404, detail="item not found")
    record = store.get_record(db, item.record_id)
    if record is None or not _can_view(user, record):
        raise HTTPException(status_code=403, detail="not your handoff")
    if item.id not in {row.id for row in store.visible_items_for_record(db, record)}:
        raise HTTPException(status_code=404, detail="item not found")
    item = store.set_item_status(db, item_id, body.status, actor=user.email)
    db.commit()
    return {"item": _item_out(item), "record_status": record.status}


@router.get("/departed/{email}/priorities")
async def departed_priorities(
    email: str,
    limit: int = 30,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read-only priorities history of a departed teammate.

    Gated to the successor on their offboard record, org-wide exec, or admin
    — and every read lands in the audit log, because this is someone else's
    (former) work surface."""
    departed = (email or "").strip().lower()
    record = store.record_for_departed(db, departed)
    if record is None:
        raise HTTPException(status_code=404, detail="no offboard record for that email")
    if not _can_view(user, record):
        raise HTTPException(status_code=403, detail="not your handoff")

    max_visible = max(1, min(limit, 90))
    cards = (
        db.query(HomeCard)
        .filter(
            HomeCard.user_email == departed,
            HomeCard.category.in_(["daily_briefing", "next_week_report"]),
        )
        .order_by(HomeCard.date.desc(), HomeCard.created_at.desc())
        .yield_per(100)
    )
    eos_enabled = await run_in_threadpool(store.eos_enabled)
    visible_cards = []
    for card in cards:
        item = _history_card_out(card, eos_enabled=eos_enabled)
        if item is not None:
            visible_cards.append(item)
            if len(visible_cards) >= max_visible:
                break
    record_audit(
        db,
        actor_email=user.email,
        action="DEPARTED_PRIORITIES_VIEW",
        target=departed,
        details={"record_id": record.id, "cards": len(visible_cards)},
    )
    return {
        "record": _record_out(db, record),
        "cards": visible_cards,
    }
