"""UC-25 priority memory façade.

Thin upsert helpers around the ``priority_memory`` table. The
orchestrator hits these at run time (load → post-filter → record_seen);
the meeting-prep / outbound-email / task-create code paths call
``resolve(...)`` to auto-clear debts; the homefeed endpoints call
``dismiss`` / ``snooze`` / ``resolve_manual`` from the UI.

Every public helper is best-effort — a DB hiccup logs a warning and
returns ``None`` / ``[]`` rather than aborting upstream actions.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import PriorityMemory
from rubik_core.priorities.models import ChannelKind, EntityKind, PriorityKind

logger = logging.getLogger(__name__)


# State strings used in the ``state`` column. Plain strings (not an
# Enum) so the column stays cheap and a future state can be added
# without an Alembic alter.
STATE_ACTIVE = "active"
STATE_RESOLVED = "resolved"
STATE_DISMISSED = "dismissed"
STATE_SNOOZED = "snoozed"

VALID_STATES = (STATE_ACTIVE, STATE_RESOLVED, STATE_DISMISSED, STATE_SNOOZED)


@dataclass
class MemoryRow:
    id: str
    user_email: str
    entity_kind: str
    entity_id: str
    priority_kind: str
    state: str
    snoozed_until: str | None
    last_priority_id: str | None
    resolved_evidence: dict | None
    first_seen_at: str
    last_seen_at: str
    last_state_change_at: str
    last_state_change_by: str | None

    @classmethod
    def from_orm(cls, row: PriorityMemory) -> MemoryRow:
        return cls(
            id=row.id,
            user_email=row.user_email,
            entity_kind=row.entity_kind,
            entity_id=row.entity_id,
            priority_kind=row.priority_kind,
            state=row.state,
            snoozed_until=row.snoozed_until.isoformat() if row.snoozed_until else None,
            last_priority_id=row.last_priority_id,
            resolved_evidence=row.resolved_evidence,
            first_seen_at=row.first_seen_at.isoformat() if row.first_seen_at else "",
            last_seen_at=row.last_seen_at.isoformat() if row.last_seen_at else "",
            last_state_change_at=(
                row.last_state_change_at.isoformat() if row.last_state_change_at else ""
            ),
            last_state_change_by=row.last_state_change_by,
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.entity_kind, self.entity_id, self.priority_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_email": self.user_email,
            "entity_kind": self.entity_kind,
            "entity_id": self.entity_id,
            "priority_kind": self.priority_kind,
            "state": self.state,
            "snoozed_until": self.snoozed_until,
            "last_priority_id": self.last_priority_id,
            "resolved_evidence": self.resolved_evidence,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "last_state_change_at": self.last_state_change_at,
            "last_state_change_by": self.last_state_change_by,
        }


# ── internal helpers ────────────────────────────────────────────────────────


def _coerce_kind(value) -> str | None:
    """Accept either an enum or a string; return the value or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (EntityKind, PriorityKind)):
        return value.value
    return str(value)


def _find(
    db, user_email: str, entity_kind: str, entity_id: str, priority_kind: str
) -> PriorityMemory | None:
    return (
        db.query(PriorityMemory)
        .filter(PriorityMemory.user_email == user_email)
        .filter(PriorityMemory.entity_kind == entity_kind)
        .filter(PriorityMemory.entity_id == entity_id)
        .filter(PriorityMemory.priority_kind == priority_kind)
        .first()
    )


def _record_transition(
    kind: str,
    *,
    user_email: str,
    entity_kind: str | None,
    entity_id: str | None,
    priority_kind: str | None,
    actor: str | None,
    priority_id: str | None = None,
) -> None:
    """Append a state change to the Insights card history (``priority_events``).

    Called only AFTER the memory write has committed, in its own short session,
    so the history can never roll back, delay or fail the change it describes.
    Only real transitions are recorded: re-dismissing a dismissed card is not an
    event. Never raises.
    """
    try:
        from rubik_core.insights.priority_events import record_priority_event

        record_priority_event(
            user_email=user_email,
            kind=kind,
            priority_id=priority_id,
            entity_kind=entity_kind,
            entity_id=entity_id,
            priority_kind=priority_kind,
            actor=actor,
        )
    except Exception:  # noqa: BLE001 — telemetry never fails a memory action
        logger.warning(
            "priority_events %s not recorded for %s", kind, user_email, exc_info=True
        )


def _is_transition(
    row: PriorityMemory | None, new_state: str, priority_id: str | None
) -> bool:
    """Does writing ``new_state`` change what this card is, worth a history row?

    * An existing row moving to a different state: yes.
    * The same state again (a double click, a re-snooze): no.
    * No row yet: only when a card action names its card (``priority_id``).
      ``record_seen`` writes only a card's PRIMARY entity, so ✗ on a card that
      covers two To-Dos creates the second row, and that is a real action. A
      hook writing memory pre-emptively — meeting prep resolving both prep
      kinds for an event — creates rows for cards nobody was ever shown, and
      those are not events.
    """
    if row is None:
        return priority_id is not None
    return row.state != new_state


# The ``priority_events`` kind each state change records.
_EVENT_FOR_STATE = {
    STATE_RESOLVED: "resolved",
    STATE_DISMISSED: "dismissed",
    STATE_SNOOZED: "snoozed",
    STATE_ACTIVE: "reopened",
}


def _upsert_active(
    db,
    *,
    user_email: str,
    entity_kind: str,
    entity_id: str,
    priority_kind: str,
    last_priority_id: str | None,
    now: datetime,
    revived: list | None = None,
) -> PriorityMemory:
    """Mark a surfaced priority active. ``revived`` (optional) collects the rows
    whose snooze had run out and that this call brought back, so the caller can
    record them once its transaction has committed."""
    row = _find(db, user_email, entity_kind, entity_id, priority_kind)
    if row is None:
        row = PriorityMemory(
            id=str(uuid.uuid4()),
            user_email=user_email,
            entity_kind=entity_kind,
            entity_id=entity_id,
            priority_kind=priority_kind,
            state=STATE_ACTIVE,
            last_priority_id=last_priority_id,
            first_seen_at=now,
            last_seen_at=now,
            last_state_change_at=now,
            last_state_change_by="system",
        )
        db.add(row)
    else:
        row.last_seen_at = now
        if last_priority_id:
            row.last_priority_id = last_priority_id
        # Auto-revive snoozed rows whose deadline passed.
        if (
            row.state == STATE_SNOOZED
            and row.snoozed_until
            and row.snoozed_until <= now
        ):
            row.state = STATE_ACTIVE
            row.last_state_change_at = now
            row.last_state_change_by = "system"
            if revived is not None:
                revived.append(
                    (entity_kind, entity_id, priority_kind, row.last_priority_id)
                )
    return row


# ── public API ──────────────────────────────────────────────────────────────


def load_for_run(user_email: str) -> dict[tuple[str, str, str], MemoryRow]:
    """Return EVERY memory row a pipeline run needs, resolved rows included.

    Keyed by ``(entity_kind, entity_id, priority_kind)`` for direct
    lookup from the orchestrator's post-filter.

    Resolved rows used to be omitted here ("the decision-maker doesn't need
    to know about them"), and that omission was the bug behind the
    silent empty feed: generation couldn't see a resolved entity,
    so the decision-maker happily spent a slot re-proposing it under a fresh
    title, and then the READ-side filter (``home._apply_priority_memory``,
    which loads via :func:`load_all` and therefore DOES see resolved) deleted
    it again on the way out. The user got a shrinking — sometimes empty —
    feed while ``generation_status`` still said ``ok``, and nothing alerted.

    Generation and read must agree on what "resolved" means, so the run now
    sees resolved rows too: :func:`apply_post_filter` can enforce its
    documented ``resolved → DROP`` rule (previously unreachable, because the
    dict never contained a resolved row), and the decision-maker gets them in
    its "already handled" list so the slot goes to real work instead.
    """
    try:
        with SessionLocal() as db:
            rows = (
                db.query(PriorityMemory)
                .filter(PriorityMemory.user_email == user_email)
                .all()
            )
            out: dict[tuple[str, str, str], MemoryRow] = {}
            for r in rows:
                mr = MemoryRow.from_orm(r)
                out[mr.key] = mr
            return out
    except Exception:  # noqa: BLE001
        logger.warning("priority_memory load failed for %s", user_email, exc_info=True)
        return {}


def load_all(user_email: str) -> list[MemoryRow]:
    """Return EVERY memory row for a user, including resolved.

    Used by the admin debug endpoint and the read-path filter; the
    dict-shaped ``load_for_run`` is what the
    orchestrator uses on every run.
    """
    try:
        with SessionLocal() as db:
            rows = (
                db.query(PriorityMemory)
                .filter(PriorityMemory.user_email == user_email)
                .order_by(PriorityMemory.last_state_change_at.desc())
                .all()
            )
            return [MemoryRow.from_orm(r) for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning(
            "priority_memory load_all failed for %s", user_email, exc_info=True
        )
        return []


def load_inactive(user_email: str) -> list[MemoryRow]:
    """Return the user's dismissed + snoozed + resolved rows, newest first.

    Powers the "handled priorities" management view (AI Personalization tab),
    where the user can reopen something they hid. Only ``active`` rows are
    excluded. Returns ``[]`` on any failure.

    Resolved rows used to be excluded, which left a ✓ with NO undo anywhere in
    the product: resolve is the one DURABLE state, so a mis-click permanently
    blacklisted that entity and only a hand-written SQL update could bring it
    back. If the user can make a choice stick forever, they have to
    be able to see and reverse it.
    """
    try:
        with SessionLocal() as db:
            rows = (
                db.query(PriorityMemory)
                .filter(PriorityMemory.user_email == user_email)
                .filter(
                    PriorityMemory.state.in_(
                        (STATE_DISMISSED, STATE_SNOOZED, STATE_RESOLVED)
                    )
                )
                .order_by(PriorityMemory.last_state_change_at.desc())
                .all()
            )
            return [MemoryRow.from_orm(r) for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning(
            "priority_memory load_inactive failed for %s", user_email, exc_info=True
        )
        return []


def reopen(
    *,
    user_email: str,
    entity_kind,
    entity_id: str,
    priority_kind,
    actor: str,
    priority_id: str | None = None,
) -> MemoryRow | None:
    """User reopened a dismissed/snoozed priority — set it back to active.

    Clears any snooze. The item becomes eligible to resurface on the next
    priorities run (the decision-maker's "already handled" hint no longer
    excludes it).
    """
    return _set_state(
        user_email=user_email,
        entity_kind=entity_kind,
        entity_id=entity_id,
        priority_kind=priority_kind,
        new_state=STATE_ACTIVE,
        actor=actor,
        priority_id=priority_id,
    )


def record_seen(
    user_email: str,
    priorities: list,
    *,
    now: datetime | None = None,
) -> None:
    """Mark each surfaced priority as ``active`` in memory.

    Called by the orchestrator AFTER the post-filter, so we only record
    priorities that actually made it into the bundle. Idempotent.
    """
    now = now or datetime.utcnow()
    revived: list[tuple] = []
    try:
        with SessionLocal() as db:
            for p in priorities:
                if not getattr(p, "entity_kind", None) or not getattr(
                    p, "entity_id", None
                ):
                    continue
                if not getattr(p, "priority_kind", None):
                    continue
                _upsert_active(
                    db,
                    user_email=user_email,
                    entity_kind=_coerce_kind(p.entity_kind),
                    entity_id=str(p.entity_id),
                    priority_kind=_coerce_kind(p.priority_kind),
                    last_priority_id=p.id,
                    now=now,
                    revived=revived,
                )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "priority_memory record_seen failed for %s", user_email, exc_info=True
        )
        return
    # A snooze that ran out and brought its card back: the system reopened it.
    for ek, eid, pk, pid in revived:
        _record_transition(
            "reopened",
            user_email=user_email,
            entity_kind=ek,
            entity_id=eid,
            priority_kind=pk,
            actor="system",
            priority_id=pid,
        )


def resolve(
    *,
    user_email: str,
    entity_kind,
    entity_id: str,
    priority_kind,
    evidence: dict | None = None,
    actor: str = "system",
    priority_id: str | None = None,
) -> MemoryRow | None:
    """Auto-resolve hook — called from meeting-prep, outbound-email, etc.

    Upserts the row to state='resolved' with the evidence dict so the UI
    can show "✓ auto-resolved because <evidence>" if/when we surface that.
    Recorded in the card history as ``auto_resolved``: evidence did it.
    """
    return _resolve(
        user_email=user_email,
        entity_kind=entity_kind,
        entity_id=entity_id,
        priority_kind=priority_kind,
        evidence=evidence,
        actor=actor,
        priority_id=priority_id,
        event_kind="auto_resolved",
    )


def _resolve(
    *,
    user_email: str,
    entity_kind,
    entity_id: str,
    priority_kind,
    evidence: dict | None,
    actor: str,
    priority_id: str | None,
    event_kind: str,
) -> MemoryRow | None:
    ek = _coerce_kind(entity_kind)
    pk = _coerce_kind(priority_kind)
    if not ek or not pk or not entity_id:
        return None
    now = datetime.utcnow()
    try:
        with SessionLocal() as db:
            row = _find(db, user_email, ek, entity_id, pk)
            changed = _is_transition(row, STATE_RESOLVED, priority_id)
            if row is None:
                row = PriorityMemory(
                    id=str(uuid.uuid4()),
                    user_email=user_email,
                    entity_kind=ek,
                    entity_id=entity_id,
                    priority_kind=pk,
                    state=STATE_RESOLVED,
                    resolved_evidence=evidence,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_state_change_at=now,
                    last_state_change_by=actor,
                )
                db.add(row)
            else:
                row.state = STATE_RESOLVED
                row.resolved_evidence = evidence
                row.last_state_change_at = now
                row.last_state_change_by = actor
                row.last_seen_at = now
            db.commit()
            db.refresh(row)
            result = MemoryRow.from_orm(row)
    except Exception:  # noqa: BLE001
        logger.warning(
            "priority_memory resolve failed for %s / %s:%s / %s",
            user_email,
            ek,
            entity_id,
            pk,
            exc_info=True,
        )
        return None
    if changed:
        _record_transition(
            event_kind,
            user_email=user_email,
            entity_kind=ek,
            entity_id=entity_id,
            priority_kind=pk,
            actor=actor,
            priority_id=priority_id,
        )
    return result


def dismiss(
    *,
    user_email: str,
    entity_kind,
    entity_id: str,
    priority_kind,
    actor: str,
    priority_id: str | None = None,
) -> MemoryRow | None:
    return _set_state(
        user_email=user_email,
        entity_kind=entity_kind,
        entity_id=entity_id,
        priority_kind=priority_kind,
        new_state=STATE_DISMISSED,
        actor=actor,
        priority_id=priority_id,
    )


def snooze(
    *,
    user_email: str,
    entity_kind,
    entity_id: str,
    priority_kind,
    until: datetime,
    actor: str,
    priority_id: str | None = None,
) -> MemoryRow | None:
    return _set_state(
        user_email=user_email,
        entity_kind=entity_kind,
        entity_id=entity_id,
        priority_kind=priority_kind,
        new_state=STATE_SNOOZED,
        actor=actor,
        snoozed_until=until,
        priority_id=priority_id,
    )


def resolve_manual(
    *,
    user_email: str,
    entity_kind,
    entity_id: str,
    priority_kind,
    actor: str,
    evidence: dict | None = None,
    priority_id: str | None = None,
) -> MemoryRow | None:
    """User clicked ✓. Same as :func:`resolve` but actor is the user, and the
    card history records ``resolved`` rather than ``auto_resolved``."""
    return _resolve(
        user_email=user_email,
        entity_kind=entity_kind,
        entity_id=entity_id,
        priority_kind=priority_kind,
        actor=actor,
        evidence=evidence or {"kind": "manual_resolve"},
        priority_id=priority_id,
        event_kind="resolved",
    )


def _set_state(
    *,
    user_email: str,
    entity_kind,
    entity_id: str,
    priority_kind,
    new_state: str,
    actor: str,
    snoozed_until: datetime | None = None,
    priority_id: str | None = None,
) -> MemoryRow | None:
    if new_state not in VALID_STATES:
        return None
    ek = _coerce_kind(entity_kind)
    pk = _coerce_kind(priority_kind)
    if not ek or not pk or not entity_id:
        return None
    now = datetime.utcnow()
    try:
        with SessionLocal() as db:
            row = _find(db, user_email, ek, entity_id, pk)
            changed = _is_transition(row, new_state, priority_id)
            if row is None:
                row = PriorityMemory(
                    id=str(uuid.uuid4()),
                    user_email=user_email,
                    entity_kind=ek,
                    entity_id=entity_id,
                    priority_kind=pk,
                    state=new_state,
                    snoozed_until=snoozed_until,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_state_change_at=now,
                    last_state_change_by=actor,
                )
                db.add(row)
            else:
                row.state = new_state
                row.snoozed_until = (
                    snoozed_until if new_state == STATE_SNOOZED else None
                )
                row.last_state_change_at = now
                row.last_state_change_by = actor
                row.last_seen_at = now
            db.commit()
            db.refresh(row)
            result = MemoryRow.from_orm(row)
    except Exception:  # noqa: BLE001
        logger.warning(
            "priority_memory state-change failed for %s / %s:%s / %s",
            user_email,
            ek,
            entity_id,
            pk,
            exc_info=True,
        )
        return None
    if changed:
        _record_transition(
            _EVENT_FOR_STATE[new_state],
            user_email=user_email,
            entity_kind=ek,
            entity_id=entity_id,
            priority_kind=pk,
            actor=actor,
            priority_id=priority_id,
        )
    return result


def resolve_entity_everywhere(
    *,
    entity_kind,
    entity_id: str,
    priority_kind,
    evidence: dict | None = None,
    actor: str = "system",
) -> int:
    """Resolve the (entity, kind) priority for EVERY user who has it active.

    Used when an EOS entity is completed/solved: a manager's card for a
    direct report's item clears too, not just the owner's. Best-effort;
    returns the number of memory rows resolved.
    """
    ek = _coerce_kind(entity_kind)
    pk = _coerce_kind(priority_kind)
    if not ek or not pk or not entity_id:
        return 0
    now = datetime.utcnow()
    try:
        with SessionLocal() as db:
            rows = (
                db.query(PriorityMemory)
                .filter(
                    PriorityMemory.entity_kind == ek,
                    PriorityMemory.entity_id == str(entity_id),
                    PriorityMemory.priority_kind == pk,
                    PriorityMemory.state != STATE_RESOLVED,
                )
                .all()
            )
            holders = []
            for row in rows:
                row.state = STATE_RESOLVED
                row.resolved_evidence = evidence
                row.last_state_change_at = now
                row.last_state_change_by = actor
                row.last_seen_at = now
                holders.append(row.user_email)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "priority_memory resolve_entity_everywhere failed for %s:%s / %s",
            ek,
            entity_id,
            pk,
            exc_info=True,
        )
        return 0
    # Every holder's card was cleared by the EOS change, not by their own ✓.
    for holder in holders:
        _record_transition(
            "auto_resolved",
            user_email=holder,
            entity_kind=ek,
            entity_id=str(entity_id),
            priority_kind=pk,
            actor=actor,
        )
    return len(holders)


def reopen_entity_everywhere(
    *,
    entity_kind,
    entity_id: str,
    priority_kind,
    actor: str = "system",
) -> int:
    """Revive an auto-resolved (entity, kind) priority for every user.

    The inverse of :func:`resolve_entity_everywhere` — when a completed EOS
    entity is re-opened, flip rows we previously auto-resolved back to
    ``active`` so the item can resurface on the next run. Rows the user
    dismissed / snoozed themselves are left untouched. Best-effort.
    """
    ek = _coerce_kind(entity_kind)
    pk = _coerce_kind(priority_kind)
    if not ek or not pk or not entity_id:
        return 0
    now = datetime.utcnow()
    try:
        with SessionLocal() as db:
            rows = (
                db.query(PriorityMemory)
                .filter(
                    PriorityMemory.entity_kind == ek,
                    PriorityMemory.entity_id == str(entity_id),
                    PriorityMemory.priority_kind == pk,
                    PriorityMemory.state == STATE_RESOLVED,
                )
                .all()
            )
            revived: list[str] = []
            for row in rows:
                # Preserve a user's manual completion (✓): resolve_manual tags
                # evidence kind='manual_resolve'. Only the EOS completion hooks'
                # auto-resolutions should revive on an upstream reopen.
                ev = row.resolved_evidence or {}
                if isinstance(ev, dict) and ev.get("kind") == "manual_resolve":
                    continue
                row.state = STATE_ACTIVE
                row.resolved_evidence = None  # drop the stale "auto-resolved" note
                row.last_state_change_at = now
                row.last_state_change_by = actor
                row.last_seen_at = now
                revived.append(row.user_email)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "priority_memory reopen_entity_everywhere failed for %s:%s / %s",
            ek,
            entity_id,
            pk,
            exc_info=True,
        )
        return 0
    for holder in revived:
        _record_transition(
            "reopened",
            user_email=holder,
            entity_kind=ek,
            entity_id=str(entity_id),
            priority_kind=pk,
            actor=actor,
        )
    return len(revived)


# ── post-filter (orchestrator consumer) ──────────────────────────────────────


def _suppression_reason(
    priority,
    row: MemoryRow | None,
    *,
    now: datetime,
    signal_by_id: dict,
) -> str | None:
    """Why ONE of a card's entities is handled, or None if it still needs the
    user. Split out of :func:`apply_post_filter` because a card can cover
    several entities and each is judged on its own row."""
    if row is None:
        return None

    title = priority.title or "(unnamed)"

    if row.state == STATE_RESOLVED:
        return f"{title} → resolved on {row.last_state_change_at}"

    if row.state == STATE_SNOOZED:
        until_iso = row.snoozed_until
        if until_iso:
            try:
                until = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
                # Strip tzinfo for comparison if naive `now`.
                if until.tzinfo and now.tzinfo is None:
                    until = until.replace(tzinfo=None)
            except ValueError:
                until = None
            if until and until > now:
                return f"{title} → snoozed until {until_iso}"
        # snoozed_until is None or in the past → revive.
        return None

    if row.state == STATE_DISMISSED:
        if _has_new_evidence(priority, row, signal_by_id, now=now):
            return None
        return f"{title} → dismissed on {row.last_state_change_at}"

    # state == active or unknown → keep.
    return None


def apply_post_filter(
    priorities: list,
    memory: dict[tuple[str, str, str], MemoryRow],
    *,
    now: datetime | None = None,
    signal_by_id: dict | None = None,
) -> tuple[list, list[str]]:
    """Drop priorities the user has already handled.

    Returns ``(kept_priorities, dropped_reasons)``. The orchestrator
    appends the reasons into ``audit_trail`` so debugging "why didn't I
    see X?" is straightforward.

    Rules per priority:

      - ``resolved``           → DROP (durable judgment)
      - ``snoozed`` with future deadline → DROP
      - ``snoozed`` with past   deadline → KEEP (auto-revived)
      - ``dismissed``:
          - if a cited signal records something that HAPPENED after the
            dismissal → KEEP (new evidence resurfaces it; a meeting time or
            any future date does not count, see :func:`_has_new_evidence`)
          - else → DROP
      - no memory row OR ``active`` → KEEP

    A priority carrying ``covered_entity_ids`` (one card speaking for several
    sibling entities) is judged on ALL of them: it is dropped only when every
    covered entity says drop, so an unhandled or brand-new sibling still
    surfaces the card.

    A priority carrying ``series_entity_id`` is additionally checked against
    the SERIES key, and that check is deliberately NOT evidence-gated: every
    occurrence of a recurring meeting is newer than the dismissal that silenced
    the series, so running it through ``_has_new_evidence`` would resurface it
    next week — exactly the bug this exists to fix.
    """
    now = now or datetime.utcnow()
    signal_by_id = signal_by_id or {}
    kept: list = []
    drops: list[str] = []
    for p in priorities:
        if (
            not getattr(p, "entity_kind", None)
            or not getattr(p, "entity_id", None)
            or not getattr(p, "priority_kind", None)
        ):
            kept.append(p)
            continue
        ek = _coerce_kind(p.entity_kind)
        pk = _coerce_kind(p.priority_kind)

        # Series-level suppression, checked first: "never prep this meeting
        # again" outranks whatever this individual occurrence's row says.
        series_id = getattr(p, "series_entity_id", None)
        if series_id:
            series_row = memory.get((ek, str(series_id), pk))
            if series_row is not None and series_row.state in (
                STATE_DISMISSED,
                STATE_RESOLVED,
            ):
                drops.append(
                    f"{p.title or '(unnamed)'} → series {series_id} "
                    f"{series_row.state} on {series_row.last_state_change_at}"
                )
                continue

        # A card that aggregates several sibling entities ("two overdue To-Dos")
        # is only silenced once EVERY one of them is handled. Checking just the
        # primary let the card return the moment a later run cited a different
        # sibling first; checking ALL of them and requiring unanimity also keeps
        # the useful half of that behaviour — a genuinely NEW sibling has no
        # memory row, so it legitimately brings the card back.
        covered = getattr(p, "covered_entity_ids", None) or []
        entity_ids = [str(e) for e in covered if e]
        if str(p.entity_id) not in entity_ids:
            entity_ids.insert(0, str(p.entity_id))

        reasons = [
            _suppression_reason(
                p,
                memory.get((ek, eid, pk)),
                now=now,
                signal_by_id=signal_by_id,
            )
            for eid in entity_ids
        ]
        if all(r is not None for r in reasons):
            drops.append(reasons[0])
            continue

        kept.append(p)

    return kept, drops


# Slack for a source clock running slightly ahead of ours, so a message that
# landed during the run still counts as having arrived.
_EVIDENCE_CLOCK_SKEW = timedelta(minutes=5)

# Channels whose timestamp is a SCHEDULE, never an arrival. A calendar signal
# is stamped with the meeting's start: in the future until the meeting, then
# "now-ish" the moment it begins, and a bare date (read as midnight UTC) for an
# all-day event. None of those is something that happened to the item.
_SCHEDULE_CHANNELS = frozenset({ChannelKind.CALENDAR.value})


def _naive_utc(value: datetime) -> datetime:
    """``value`` as naive UTC, the form ``priority_memory`` timestamps take.

    An aware value is CONVERTED, not just stripped: ``12:00+05:00`` is 07:00
    UTC, and dropping the offset would put it five hours late.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _signal_time(sig) -> datetime | None:
    ts_str = getattr(sig, "timestamp", None)
    if not ts_str:
        return None
    try:
        return _naive_utc(datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")))
    except (ValueError, AttributeError, TypeError):
        return None


def _is_schedule(sig) -> bool:
    channel = getattr(sig, "channel", None)
    return getattr(channel, "value", channel) in _SCHEDULE_CHANNELS


def _has_new_evidence(
    priority, row: MemoryRow, signal_by_id: dict, *, now: datetime | None = None
) -> bool:
    """True when something the card cites HAPPENED after the dismissal, which
    is the only thing that should overrule a ✗.

    A cited signal counts when its timestamp is after the dismissal and no
    later than now. Two kinds of timestamp used to pass that test without
    being news, and each kept a dismissed card coming back:

    * **A schedule.** Calendar signals carry the meeting's start, which is in
      the future until the meeting is over. Any card citing an upcoming meeting
      therefore beat its own dismissal on every run, and "Dismiss just this
      one" on a prep card did nothing. Calendar signals never count.
    * **A future date of any kind.** A timestamp later than now has not
      arrived yet, whatever the channel.

    Every other cited signal still counts, not only the one the card is keyed
    to. The card's own signal is often the one that cannot move: EOS and
    My To-Do signals are stamped with their creation date, and the model keys a
    client conversation to the deal rather than the thread. Judging only that
    signal swallowed a real client reply and made those dismissals permanent.
    Sources must stamp a real activity time for this to hold, which is why the
    HubSpot channel no longer stamps "now" on a deal with no logged activity.
    """
    try:
        threshold = _naive_utc(datetime.fromisoformat(row.last_state_change_at))
    except ValueError:
        return True  # malformed timestamp → err on the side of resurfacing
    ceiling = _naive_utc(now or datetime.utcnow()) + _EVIDENCE_CLOCK_SKEW
    for sid in getattr(priority, "signal_ids", []) or []:
        sig = signal_by_id.get(sid)
        if sig is None or _is_schedule(sig):
            continue
        ts = _signal_time(sig)
        if ts is not None and threshold < ts <= ceiling:
            return True
    return False
