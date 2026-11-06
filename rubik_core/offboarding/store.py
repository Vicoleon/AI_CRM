"""Persistence for offboard records and inherited items.

Every function takes an open session — callers own transaction scope. The
record is created BEFORE the users row is deleted (it snapshots name and
department from it); everything else here runs after the person is gone, so
nothing in this module may join back to ``users`` for the departed side.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from rubik_core.database.models import OffboardItem, OffboardRecord, User

logger = logging.getLogger(__name__)

ITEM_STATUSES = ("open", "done", "dismissed")
EOS_ITEM_KINDS = frozenset({"l10", "todo", "milestone", "rock", "scorecard"})


def eos_enabled() -> bool:
    from rubik_core.control.repository import module_enabled

    try:
        return module_enabled("eos")
    except Exception:  # noqa: BLE001 — failed control reads cannot expose EOS
        logger.warning("[offboard] EOS module lookup failed", exc_info=True)
        return False


def is_eos_item(item: OffboardItem) -> bool:
    return item.kind in EOS_ITEM_KINDS or (item.entity_kind or "").startswith("eos_")


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def create_record(
    db: Session,
    *,
    user_email: str,
    successor_email: str,
    created_by: str,
) -> OffboardRecord:
    """Snapshot the departing user into a durable offboard record.

    Raises ``ValueError`` when either side is invalid — the router turns those
    into 400s. The successor must be a live, different user; the departed user
    must still exist (this runs before the roster delete).
    """
    email = _norm(user_email)
    successor = _norm(successor_email)
    if email == successor:
        raise ValueError("successor must be a different person")

    user = db.get(User, email) or _user_ci(db, email)
    if user is None:
        raise ValueError(f"no users row for {email!r}")
    successor_row = db.get(User, successor) or _user_ci(db, successor)
    if successor_row is None:
        raise ValueError(f"successor {successor!r} is not an active user")

    record = OffboardRecord(
        id=str(uuid.uuid4()),
        user_email=email,
        user_name=user.name or email,
        department=user.department,
        successor_email=_norm(successor_row.email),
        created_by=_norm(created_by),
        offboarded_at=datetime.utcnow(),
        sweep_status="pending",
        status="active",
    )
    db.add(record)
    db.flush()
    return record


def _user_ci(db: Session, email: str) -> User | None:
    """Case-insensitive users lookup — users.email is stored verbatim from the
    Google token, so a lower-cased input may miss ``db.get``."""
    from sqlalchemy import func

    return db.query(User).filter(func.lower(User.email) == email).first()


def get_record(db: Session, record_id: str) -> OffboardRecord | None:
    return db.get(OffboardRecord, record_id)


def record_for_departed(db: Session, email: str) -> OffboardRecord | None:
    """Most recent record for a departed email (re-offboarding is idempotent-ish:
    the newest record is the live one)."""
    return (
        db.query(OffboardRecord)
        .filter(OffboardRecord.user_email == _norm(email))
        .order_by(OffboardRecord.offboarded_at.desc())
        .first()
    )


def records_for_successor(db: Session, successor_email: str) -> list[OffboardRecord]:
    return (
        db.query(OffboardRecord)
        .filter(OffboardRecord.successor_email == _norm(successor_email))
        .order_by(OffboardRecord.offboarded_at.desc())
        .all()
    )


def all_records(db: Session) -> list[OffboardRecord]:
    return db.query(OffboardRecord).order_by(OffboardRecord.offboarded_at.desc()).all()


def open_items_for_successor(
    db: Session, successor_email: str
) -> list[tuple[OffboardItem, OffboardRecord]]:
    """Every open inherited item this person owns, newest record first.

    Returns ``(item, record)`` pairs — the inherited priorities channel needs
    the departed person's name for attribution and the record id for links.
    """
    rows = (
        db.query(OffboardItem, OffboardRecord)
        .join(OffboardRecord, OffboardItem.record_id == OffboardRecord.id)
        .filter(
            OffboardRecord.successor_email == _norm(successor_email),
            OffboardItem.status == "open",
        )
        .order_by(OffboardRecord.offboarded_at.desc(), OffboardItem.created_at.asc())
        .all()
    )
    permitted: dict[str, set[str]] = {}
    for _, record in rows:
        if record.id not in permitted:
            permitted[record.id] = {
                item.id for item in visible_items_for_record(db, record)
            }
    return [
        (item, record)
        for item, record in rows
        if item.id in permitted.get(record.id, set())
    ]


def visible_items_for_record(db: Session, record: OffboardRecord) -> list[OffboardItem]:
    """Hide old CRM-derived handoff items unless live ownership still holds."""
    rows = list(record.items)
    include_eos = eos_enabled()
    deal_ids = {
        str(item.entity_id) for item in rows if item.kind == "deal" and item.entity_id
    }
    if deal_ids:
        from backend.routers.agents import visible_hubspot_deal_ids

        visible_deals = visible_hubspot_deal_ids(deal_ids)
    else:
        visible_deals = set()
    visible = []
    for item in rows:
        if is_eos_item(item) and not include_eos:
            continue
        # Old history commitments and old Google extras can contain CRM facts
        # copied into prose even when their metadata claims a Google source.
        if item.kind in {"commitment", "google_commitment"}:
            continue
        if item.kind == "google_signal_commitment" and (
            item.entity_kind not in {"thread", "event", "document"}
            or not item.entity_id
        ):
            continue
        if item.kind == "deal" or item.entity_kind == "deal":
            if not item.entity_id or str(item.entity_id) not in visible_deals:
                continue
        visible.append(item)
    return visible


def visible_inherited_item_ids(item_ids: set[str]) -> set[str]:
    """Live proof for already persisted inherited AI Priority cards."""
    if not item_ids:
        return set()
    from rubik_core.database.db import SessionLocal

    with SessionLocal() as db:
        rows = (
            db.query(OffboardItem, OffboardRecord)
            .join(OffboardRecord, OffboardItem.record_id == OffboardRecord.id)
            .filter(OffboardItem.id.in_(item_ids))
            .all()
        )
        permitted: dict[str, set[str]] = {}
        for _, record in rows:
            if record.id not in permitted:
                permitted[record.id] = {
                    item.id for item in visible_items_for_record(db, record)
                }
        return {
            item.id
            for item, record in rows
            if item.id in permitted.get(record.id, set())
        }


def set_item_status(
    db: Session, item_id: str, status: str, *, actor: str
) -> OffboardItem | None:
    """Mark one item done/dismissed (or re-open it) and roll the record status.

    The record flips to ``closed`` when its last open item resolves, and back
    to ``active`` if something is re-opened — that drives both the Handoffs
    page grouping and whether the inherited channel still has anything to say.
    """
    if status not in ITEM_STATUSES:
        raise ValueError(f"status must be one of {ITEM_STATUSES}")
    item = db.get(OffboardItem, item_id)
    if item is None:
        return None
    item.status = status
    if status == "open":
        item.resolved_at = None
        item.resolved_by = None
    else:
        item.resolved_at = datetime.utcnow()
        item.resolved_by = _norm(actor)
    db.flush()

    record = db.get(OffboardRecord, item.record_id)
    if record is not None:
        open_left = (
            db.query(OffboardItem)
            .filter(OffboardItem.record_id == record.id, OffboardItem.status == "open")
            .count()
        )
        record.status = "active" if open_left else "closed"
    db.flush()
    return item


def resolve_items_by_entity(
    db: Session, *, successor_email: str, entity_ids: list[str], status: str, actor: str
) -> int:
    """Write-back from the priorities feed: the successor dismissed/resolved an
    inherited priority, so close the matching offboard items too.

    ``entity_ids`` are OffboardItem ids (the inherited channel tags each signal
    with ``entity_kind="offboard_item"``, ``entity_id=item.id``). Returns how
    many items changed.
    """
    changed = 0
    visible_by_record: dict[str, set[str]] = {}
    for item_id in entity_ids:
        item = db.get(OffboardItem, str(item_id))
        if item is None or item.status != "open":
            continue
        record = db.get(OffboardRecord, item.record_id)
        if record is None or record.successor_email != _norm(successor_email):
            continue
        if record.id not in visible_by_record:
            visible_by_record[record.id] = {
                row.id for row in visible_items_for_record(db, record)
            }
        if item.id not in visible_by_record[record.id]:
            continue
        set_item_status(db, item.id, status, actor=actor)
        changed += 1
    return changed


def add_items(
    db: Session, record: OffboardRecord, items: list[dict]
) -> list[OffboardItem]:
    """Persist sweep output. Dedups within the record on (kind, entity_id,
    label) so a re-run sweep doesn't double the checklist."""
    existing = {
        (i.kind, i.entity_id or "", i.label)
        for i in db.query(OffboardItem).filter(OffboardItem.record_id == record.id)
    }
    created: list[OffboardItem] = []
    for raw in items:
        key = (
            raw.get("kind", ""),
            str(raw.get("entity_id") or ""),
            raw.get("label", ""),
        )
        if key in existing:
            continue
        existing.add(key)
        item = OffboardItem(
            id=str(uuid.uuid4()),
            record_id=record.id,
            kind=raw.get("kind") or "commitment",
            label=(raw.get("label") or "(untitled)")[:500],
            detail=raw.get("detail") or "",
            due=raw.get("due"),
            urgency=raw.get("urgency") or "medium",
            entity_kind=raw.get("entity_kind"),
            entity_id=str(raw["entity_id"]) if raw.get("entity_id") else None,
            source_links=[s for s in (raw.get("source_links") or []) if s][:6] or None,
            status="open",
            created_at=datetime.utcnow(),
        )
        db.add(item)
        created.append(item)
    db.flush()
    return created


def suggest_successor(db: Session, email: str) -> dict | None:
    """The departing person's EOS-chart boss — the default successor.

    Reads the Accountability Chart the same way the PTO handoff delegate pool
    does: my seat → ``reports_to_id`` → the person holding that seat. Returns
    ``{"email", "name"}`` or None (no EOS identity / vacant boss seat / boss
    has no live users row).
    """
    if not eos_enabled():
        return None
    try:
        from backend.routers.eos._authz import _seat_for_person, person_by_email
        from rubik_core.database.eos import Person
    except Exception:  # noqa: BLE001 — EOS module optional in some test rigs
        return None

    me = person_by_email(db, _norm(email))
    if me is None:
        return None
    seat = _seat_for_person(me, db)
    if seat is None or not getattr(seat, "reports_to_id", None):
        return None
    boss = (
        db.query(Person)
        .filter(Person.seat_id == seat.reports_to_id, Person.is_deleted.is_(False))
        .first()
    )
    if boss is None or not (boss.email or "").strip():
        return None
    boss_email = _norm(boss.email)
    if _user_ci(db, boss_email) is None:
        return None  # boss themselves departed — force an explicit pick
    return {"email": boss_email, "name": getattr(boss, "name", None) or boss_email}
