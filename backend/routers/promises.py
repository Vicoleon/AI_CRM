"""My To-Dos — the promises ledger API (``/api/todos``).

User-facing name "To-Dos", code name "promise" (see rubik_core/promises).
Gate, per the repo convention: the env kill switch answers 404 (there is no
such surface in this environment), the roster answers 403 (it exists and you
are not on it yet). Every mutation is scoped to the caller's OWN rows through
``store.get_owned`` — there is no admin or manager view in Phase 0.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user, require_eos_module
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.promises import OPEN_PROMISE_STATUSES, Promise
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.promises import parse as promise_parse
from rubik_core.promises import readthrough, store
from rubik_core.promises.priority_sync import sync_promise_priority

logger = logging.getLogger(__name__)


def require_todos(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Gate for the whole router — 404 when switched off, 403 when not on it."""
    if not settings.TODOS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows("todos", user):
        raise HTTPException(
            status_code=403,
            detail="My To-Dos is not turned on for you. Ask your admin.",
        )
    return user


router = APIRouter(
    prefix="/todos", tags=["todos"], dependencies=[Depends(require_todos)]
)


# ── Schemas ──────────────────────────────────────────────────────────────────


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class CreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    detail: str | None = None
    due_local: str | None = None  # YYYY-MM-DD, owner-local calendar day
    counterparty_kind: str | None = None
    counterparty_id: str | None = None
    counterparty_label: str | None = None
    object_kind: str | None = None
    object_id: str | None = None
    object_label: str | None = None
    source: str = "page"
    source_ref: dict[str, Any] | None = None


class PatchIn(BaseModel):
    title: str | None = None
    detail: str | None = None
    due_local: str | None = None
    clear_due: bool = False
    counterparty_label: str | None = None
    clear_counterparty: bool = False
    is_private: bool | None = None


class SnoozeIn(BaseModel):
    preset: str | None = None  # tomorrow | 3d | week
    until: str | None = None  # YYYY-MM-DD


class DropIn(BaseModel):
    reason: str | None = None


class FromPriorityIn(BaseModel):
    priority_id: str


class PromoteIn(BaseModel):
    department: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_date(value: str | None, field: str = "due_local") -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{field} must be YYYY-MM-DD"
        ) from None


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _out(row: Promise) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "detail": row.detail,
        "status": row.status,
        "promised_for": _iso(row.promised_for),
        "due_local": _iso(row.due_local),
        "timezone": row.timezone,
        "counterparty_kind": row.counterparty_kind,
        "counterparty_id": row.counterparty_id,
        "counterparty_label": row.counterparty_label,
        "object_kind": row.object_kind,
        "object_id": row.object_id,
        "object_label": row.object_label,
        "source": row.source,
        "source_ref": row.source_ref,
        "snooze_count": row.snooze_count or 0,
        "slipping_since": _iso(row.slipping_since),
        "is_private": bool(row.is_private),
        "eos_todo_id": row.eos_todo_id,
        "created_at": _iso(row.created_at),
        "kept_at": _iso(row.kept_at),
        "dropped_at": _iso(row.dropped_at),
    }


def _owned(db: Session, user: UserProfile, promise_id: str) -> Promise:
    row = store.get_owned(db, user.email, promise_id)
    if row is None:
        raise HTTPException(status_code=404, detail="That to-do is not on your list.")
    return row


def _today_for(db: Session, user: UserProfile) -> tuple[str, date]:
    tz = store.owner_timezone(db, user.email)
    return tz, store.local_today(tz)


def _transition(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _group_key(row: Promise) -> tuple[str, str, str | None]:
    if row.counterparty_label or row.counterparty_id:
        key = f"{row.counterparty_kind or 'person'}:{row.counterparty_id or row.counterparty_label}"
        return (
            key,
            row.counterparty_label or row.counterparty_id or "Someone",
            row.counterparty_kind,
        )
    return "__you__", "Just you", None


# ── Read ─────────────────────────────────────────────────────────────────────


@router.get("")
async def list_todos(
    user: UserProfile = Depends(get_current_user), db: Session = Depends(get_db)
):
    """The whole page in one read: suggestions, confirmations, what you owe
    grouped by who is waiting, the other ledgers, and this week's tally."""
    tz, today = _today_for(db, user)
    if store.refresh_slipping(db, user.email, today=today):
        db.commit()
    rows = store.list_for_owner(db, user.email)

    suggested = [_out(r) for r in rows if r.status == "suggested"]
    confirm = [_out(r) for r in rows if r.status == "evidence_found"]
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.status not in ("open", "slipping"):
            continue
        key, label, kind = _group_key(r)
        g = groups.setdefault(
            key, {"key": key, "label": label, "kind": kind, "items": []}
        )
        g["items"].append(_out(r))
    waiting = sorted(
        groups.values(),
        key=lambda g: (g["key"] == "__you__", -len(g["items"]), g["label"].lower()),
    )
    waiting_on_you = sum(1 for g in waiting if g["key"] != "__you__")

    from rubik_core.control.repository import module_enabled

    try:
        eos_enabled = await run_in_threadpool(module_enabled, "eos")
    except Exception:  # noqa: BLE001 — an unavailable sibling ledger stays empty
        logger.warning("promises: EOS module lookup failed", exc_info=True)
        eos_enabled = False
    eos_todos = (
        await run_in_threadpool(readthrough.eos_todos_for, db, user.email)
        if eos_enabled
        else []
    )
    commitments = await run_in_threadpool(readthrough.commitments_for, db, user.email)
    return {
        "today": today.isoformat(),
        "timezone": tz,
        "people_waiting": waiting_on_you,
        "suggested": suggested,
        "confirm": confirm,
        "waiting": waiting,
        "read_through": {"eos_todos": eos_todos, "commitments": commitments},
        "ledger": store.week_ledger(db, user.email, today=today),
        "recent": [
            _out(r)
            for r in rows
            if r.status in ("kept", "dropped")
            and (r.kept_at or r.dropped_at or r.updated_at)
            and (r.kept_at or r.dropped_at or r.updated_at)
            >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        ][:10],
    }


# ── Capture ──────────────────────────────────────────────────────────────────


@router.post("/parse")
async def parse_todo(
    body: ParseIn,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read a typed line into title / who / when. Writes nothing."""
    tz, today = _today_for(db, user)
    parsed = await run_in_threadpool(
        promise_parse.parse_todo_text, body.text, today=today, tz_name=tz
    )
    resolved = promise_parse.resolve_counterparty(db, parsed.get("counterparty_label"))
    if resolved:
        parsed.update(resolved)
    return {"parsed": parsed, "today": today.isoformat(), "timezone": tz}


def _create(db: Session, user: UserProfile, body: CreateIn) -> Promise:
    if body.source not in ("page", "chat", "priority_card", "slack_cmd"):
        raise HTTPException(status_code=422, detail="unknown source")
    tz, _ = _today_for(db, user)
    counterparty = {
        "counterparty_kind": body.counterparty_kind,
        "counterparty_id": body.counterparty_id,
        "counterparty_label": body.counterparty_label,
    }
    if body.counterparty_label and not body.counterparty_id:
        resolved = promise_parse.resolve_counterparty(db, body.counterparty_label)
        if resolved:
            counterparty = resolved
    if counterparty["counterparty_label"] and not counterparty["counterparty_kind"]:
        counterparty["counterparty_kind"] = "person"
    try:
        row = store.create_promise(
            db,
            owner_email=user.email,
            title=body.title,
            detail=body.detail,
            due_local=_parse_date(body.due_local),
            timezone=tz,
            object_kind=body.object_kind,
            object_id=body.object_id,
            object_label=body.object_label,
            source=body.source,
            source_ref=body.source_ref,
            actor=user.email,
            **counterparty,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.commit()
    db.refresh(row)
    return row


@router.post("", status_code=201)
async def create_todo(
    body: CreateIn,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {"todo": _out(_create(db, user, body))}


@router.post("/from-priority", status_code=201)
async def create_from_priority(
    body: FromPriorityIn,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """ "Add to my To-Dos" on a Homefeed card. Idempotent per card: adding the
    same card twice returns the existing promise with 200."""
    existing = store.find_by_priority(db, user.email, body.priority_id)
    if existing is not None:
        return {"todo": _out(existing), "created": False}

    from backend.routers.home import _find_priority_in_today

    p = await run_in_threadpool(_find_priority_in_today, user.email, body.priority_id)
    if p is None:
        raise HTTPException(
            status_code=404, detail="priority not found in today's bundle"
        )

    ek = (p.get("entity_kind") or "").lower()
    eid = p.get("entity_id")
    label = p.get("entity_label")
    attrs = p.get("attrs") or {}
    fields: dict[str, Any] = {}
    if ek in ("person", "company", "deal") and eid:
        fields.update(
            counterparty_kind=ek, counterparty_id=str(eid), counterparty_label=label
        )
        if ek == "deal":
            fields.update(object_kind="deal", object_id=str(eid), object_label=label)
    elif ek in ("thread", "document", "event", "transcript") and eid:
        fields.update(object_kind=ek, object_id=str(eid), object_label=label)
    due = attrs.get("due_date")
    body_in = CreateIn(
        title=(p.get("title") or "Untitled")[:300],
        detail=p.get("suggested_action") or p.get("why_it_matters"),
        due_local=due if isinstance(due, str) and len(due) >= 10 else None,
        source="priority_card",
        source_ref={
            "priority_id": body.priority_id,
            "entity_kind": ek or None,
            "entity_id": str(eid) if eid else None,
            "priority_kind": p.get("priority_kind"),
            "source_links": (p.get("source_links") or [])[:3],
        },
        **fields,
    )
    return {"todo": _out(_create(db, user, body_in)), "created": True}


# ── Edit ─────────────────────────────────────────────────────────────────────


@router.patch("/{promise_id}")
async def patch_todo(
    promise_id: str,
    body: PatchIn,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.detail is not None:
        fields["detail"] = body.detail or None
    if body.is_private is not None:
        fields["is_private"] = body.is_private
    if body.clear_counterparty:
        fields.update(
            counterparty_kind=None, counterparty_id=None, counterparty_label=None
        )
    elif body.counterparty_label is not None:
        resolved = promise_parse.resolve_counterparty(db, body.counterparty_label) or {
            "counterparty_kind": "person",
            "counterparty_id": None,
            "counterparty_label": body.counterparty_label.strip() or None,
        }
        fields.update(resolved)
    if fields:
        _transition(store.update_fields, db, row, actor=user.email, **fields)
    if body.clear_due:
        _transition(store.move_due, db, row, actor=user.email, due_local=None)
    elif body.due_local is not None:
        _transition(
            store.move_due,
            db,
            row,
            actor=user.email,
            due_local=_parse_date(body.due_local),
        )
    db.commit()
    db.refresh(row)
    return {"todo": _out(row)}


# ── Transitions ──────────────────────────────────────────────────────────────


def _closed(row: Promise, user: UserProfile, via: str) -> None:
    sync_promise_priority(promise_id=row.id, closed=True, actor=user.email, via=via)


@router.post("/{promise_id}/kept")
async def kept(
    promise_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    _transition(store.mark_kept, db, row, actor=user.email)
    db.commit()
    _closed(row, user, "kept")
    return {"todo": _out(row)}


@router.post("/{promise_id}/not-yet")
async def not_yet(
    promise_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    _transition(store.mark_not_yet, db, row, actor=user.email)
    db.commit()
    return {"todo": _out(row)}


@router.post("/{promise_id}/drop")
async def drop(
    promise_id: str,
    body: DropIn | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    _transition(
        store.drop, db, row, actor=user.email, reason=(body.reason if body else None)
    )
    db.commit()
    _closed(row, user, "dropped")
    return {"todo": _out(row)}


@router.post("/{promise_id}/reopen")
async def reopen(
    promise_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    _transition(store.reopen, db, row, actor=user.email)
    db.commit()
    sync_promise_priority(
        promise_id=row.id, closed=False, actor=user.email, via="reopened"
    )
    return {"todo": _out(row)}


@router.post("/{promise_id}/snooze")
async def snooze(
    promise_id: str,
    body: SnoozeIn,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    _, today = _today_for(db, user)
    if body.until:
        _transition(
            store.snooze,
            db,
            row,
            actor=user.email,
            until=_parse_date(body.until, "until"),
            today=today,
        )
    elif body.preset:
        _transition(
            store.snooze_preset,
            db,
            row,
            actor=user.email,
            preset=body.preset,
            today=today,
        )
    else:
        raise HTTPException(status_code=422, detail="pick a preset or a date")
    db.commit()
    return {"todo": _out(row)}


@router.post("/{promise_id}/accept")
async def accept(
    promise_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    _transition(store.accept, db, row, actor=user.email)
    db.commit()
    return {"todo": _out(row)}


@router.post("/{promise_id}/reject")
async def reject(
    promise_id: str,
    body: DropIn | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _owned(db, user, promise_id)
    _transition(
        store.reject, db, row, actor=user.email, reason=(body.reason if body else None)
    )
    db.commit()
    # A heard suggestion can already be on the Homefeed as "You said you
    # would…"; "that wasn't a promise" must take the card with it.
    _closed(row, user, "rejected")
    return {"todo": _out(row)}


@router.post("/{promise_id}/promote-eos", dependencies=[Depends(require_eos_module)])
async def promote_to_eos(
    promise_id: str,
    body: PromoteIn | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Make it a team To-Do: one ``eos_todos`` row, owned by the caller,
    linked back so either side completing completes both (Phase 3 wires the
    reverse direction; Phase 0 links and creates)."""
    row = _owned(db, user, promise_id)
    if row.status not in OPEN_PROMISE_STATUSES:
        raise HTTPException(
            status_code=409, detail="only an open to-do can join the team list"
        )
    if row.eos_todo_id:
        return {"todo": _out(row), "eos_todo_id": row.eos_todo_id, "created": False}

    from backend.models.eos.traction import truncate_todo_text
    from backend.routers.eos._authz import person_by_email
    from rubik_core.database.eos._base import TodoStatus
    from rubik_core.database.eos.todos import Todo

    person = person_by_email(db, user.email)
    if person is None:
        raise HTTPException(
            status_code=400,
            detail="You are not set up in EOS yet, so this can't join a team list.",
        )
    department = (body.department if body else None) or getattr(
        user, "department", None
    )
    todo = Todo(
        owner_id=person.id,
        description=truncate_todo_text(row.title),
        department_name=department,
        is_exec=False,
        due_date=datetime.combine(row.due_local, datetime.min.time())
        if row.due_local
        else None,
        status=TodoStatus.PENDING,
        created_by=user.email,
    )
    db.add(todo)
    db.flush()
    store.link_eos_todo(db, row, actor=user.email, eos_todo_id=str(todo.id))
    db.commit()
    db.refresh(row)
    return {"todo": _out(row), "eos_todo_id": str(todo.id), "created": True}
