"""The one writer of the ``promises`` ledger.

Every transition here appends a ``promise_events`` row. Nothing else in the
codebase may UPDATE a promise without going through this module — the weekly
kept-rate and the pilot's acceptance-rate kill rule are computed from the event
log, and an un-logged write makes both lie.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.database.promises import (
    OPEN_PROMISE_STATUSES,
    PROMISE_SOURCES,
    Promise,
    PromiseEvent,
    PromiseScan,
)

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system"

#: Snooze presets the page, Chat and Slack all agree on, in days.
SNOOZE_PRESETS: dict[str, int] = {"tomorrow": 1, "3d": 3, "week": 7}


def _now() -> datetime:
    return datetime.utcnow()


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


# ── Timezone / "today" ───────────────────────────────────────────────────────


def owner_timezone(db: Session, owner_email: str) -> str:
    """The owner's saved Settings → General timezone, else the org default.

    Folded on both sides: ``user_preferences.user_email`` mirrors ``users.email``
    which is stored verbatim from the Google token.
    """
    from rubik_core.database.models import UserPreference

    try:
        saved = (
            db.query(UserPreference.timezone)
            .filter(func.lower(UserPreference.user_email) == _fold(owner_email))
            .scalar()
        )
        if (saved or "").strip():
            return saved.strip()
    except Exception:  # noqa: BLE001 — a tz lookup must never break a write
        logger.debug("promises: tz lookup failed for %s", owner_email, exc_info=True)
    from rubik_core.priorities.temporal import resolve_tz

    return resolve_tz(None)[1]


def local_today(tz_name: str, now_utc: datetime | None = None) -> date:
    """The calendar day in ``tz_name`` right now — never ``date.today()``."""
    from zoneinfo import ZoneInfo

    from rubik_core.priorities.temporal import resolve_tz

    tz, _ = resolve_tz(tz_name)
    now = now_utc or _now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(tz).date()


# ── Events ───────────────────────────────────────────────────────────────────


def record_event(
    db: Session,
    promise: Promise,
    kind: str,
    *,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> PromiseEvent:
    ev = PromiseEvent(
        promise_id=promise.id,
        at=_now(),
        actor=actor or SYSTEM_ACTOR,
        kind=kind,
        payload=payload or None,
    )
    db.add(ev)
    promise.updated_at = _now()
    return ev


# ── Create / read ────────────────────────────────────────────────────────────


def create_promise(
    db: Session,
    *,
    owner_email: str,
    title: str,
    source: str,
    actor: str,
    detail: str | None = None,
    due_local: date | None = None,
    timezone: str | None = None,
    counterparty_kind: str | None = None,
    counterparty_id: str | None = None,
    counterparty_label: str | None = None,
    object_kind: str | None = None,
    object_id: str | None = None,
    object_label: str | None = None,
    source_ref: dict[str, Any] | None = None,
    confidence: float | None = None,
    status: str = "open",
) -> Promise:
    """Append a promise. ``promised_for`` is stamped from ``due_local`` once and
    never moves; ``due_local`` is the working date."""
    if source not in PROMISE_SOURCES:
        raise ValueError(f"unknown promise source {source!r}")
    title = (title or "").strip()
    if not title:
        raise ValueError("a to-do needs a title")
    if status not in ("open", "suggested"):
        raise ValueError("a new promise starts open or suggested")
    row = Promise(
        owner_email=owner_email,
        title=title[:300],
        detail=(detail or "").strip() or None,
        status=status,
        promised_for=due_local,
        due_local=due_local,
        timezone=timezone or owner_timezone(db, owner_email),
        counterparty_kind=counterparty_kind,
        counterparty_id=counterparty_id,
        counterparty_label=(counterparty_label or "").strip() or None,
        object_kind=object_kind,
        object_id=object_id,
        object_label=(object_label or "").strip() or None,
        source=source,
        source_ref=source_ref or None,
        confidence=confidence,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.flush()
    record_event(
        db,
        row,
        "suggested" if status == "suggested" else "created",
        actor=actor,
        payload={
            "source": source,
            "due_local": due_local.isoformat() if due_local else None,
        },
    )
    return row


def get_owned(db: Session, owner_email: str, promise_id: str) -> Promise | None:
    """The row, only if ``owner_email`` owns it — the IDOR guard every
    mutation goes through."""
    return (
        db.query(Promise)
        .filter(Promise.id == promise_id, Promise.owner_email == _fold(owner_email))
        .first()
    )


def list_for_owner(
    db: Session, owner_email: str, *, statuses: tuple[str, ...] | None = None
) -> list[Promise]:
    q = db.query(Promise).filter(Promise.owner_email == _fold(owner_email))
    if statuses:
        q = q.filter(Promise.status.in_(statuses))
    # Dated first (soonest first), undated after, newest undated first.
    rows = q.all()
    rows.sort(
        key=lambda r: (
            r.due_local is None,
            r.due_local or date.max,
            -(r.created_at or _now()).timestamp(),
        )
    )
    return rows


def list_open(db: Session, owner_email: str) -> list[Promise]:
    return list_for_owner(db, owner_email, statuses=OPEN_PROMISE_STATUSES)


def find_by_priority(db: Session, owner_email: str, priority_id: str) -> Promise | None:
    """A promise already made from this priority card, if any — so "Add to my
    To-Dos" is idempotent per card."""
    for row in list_for_owner(db, owner_email):
        ref = row.source_ref or {}
        if row.source == "priority_card" and ref.get("priority_id") == priority_id:
            return row
    return None


# ── Transitions ──────────────────────────────────────────────────────────────


def _require_status(promise: Promise, allowed: tuple[str, ...], verb: str) -> None:
    if promise.status not in allowed:
        raise ValueError(f"cannot {verb} a to-do that is {promise.status}")


def accept(db: Session, promise: Promise, *, actor: str) -> Promise:
    _require_status(promise, ("suggested",), "accept")
    promise.status = "open"
    record_event(db, promise, "accepted", actor=actor)
    return promise


def reject(
    db: Session, promise: Promise, *, actor: str, reason: str | None = None
) -> Promise:
    _require_status(promise, ("suggested",), "reject")
    promise.status = "dropped"
    promise.dropped_at = _now()
    record_event(db, promise, "rejected", actor=actor, payload={"reason": reason})
    return promise


def mark_kept(
    db: Session, promise: Promise, *, actor: str, via: str = "manual"
) -> Promise:
    _require_status(promise, OPEN_PROMISE_STATUSES, "keep")
    promise.status = "kept"
    promise.kept_at = _now()
    record_event(db, promise, "kept_confirmed", actor=actor, payload={"via": via})
    return promise


def mark_not_yet(db: Session, promise: Promise, *, actor: str) -> Promise:
    """The owner says the evidence was wrong — back to open, evidence kept
    for the record."""
    _require_status(promise, ("evidence_found",), "deny")
    promise.status = "open"
    record_event(
        db, promise, "kept_denied", actor=actor, payload={"evidence": promise.evidence}
    )
    return promise


def drop(
    db: Session, promise: Promise, *, actor: str, reason: str | None = None
) -> Promise:
    _require_status(promise, OPEN_PROMISE_STATUSES, "drop")
    promise.status = "dropped"
    promise.dropped_at = _now()
    record_event(
        db,
        promise,
        "dropped",
        actor=actor,
        payload={"reason": (reason or "").strip() or None},
    )
    return promise


def reopen(db: Session, promise: Promise, *, actor: str) -> Promise:
    _require_status(promise, ("kept", "dropped"), "reopen")
    promise.status = "open"
    promise.kept_at = None
    promise.dropped_at = None
    record_event(db, promise, "reopened", actor=actor)
    return promise


def snooze(
    db: Session, promise: Promise, *, actor: str, until: date, today: date
) -> Promise:
    """Move the working date. Counts toward the renegotiation rule (Phase 2)."""
    _require_status(promise, OPEN_PROMISE_STATUSES, "snooze")
    if until < today:
        raise ValueError("a snooze has to land on today or later")
    before = promise.due_local
    promise.due_local = until
    promise.snooze_count = (promise.snooze_count or 0) + 1
    if promise.status == "slipping":
        promise.status = "open"
    record_event(
        db,
        promise,
        "snoozed",
        actor=actor,
        payload={
            "from": before.isoformat() if before else None,
            "to": until.isoformat(),
            "snooze_count": promise.snooze_count,
        },
    )
    return promise


def snooze_preset(
    db: Session, promise: Promise, *, actor: str, preset: str, today: date
) -> Promise:
    days = SNOOZE_PRESETS.get(preset)
    if days is None:
        raise ValueError(f"unknown snooze preset {preset!r}")
    # From the later of today and the current date, so "next week" on an
    # overdue item does not land in the past.
    base = max(today, promise.due_local or today)
    return snooze(
        db, promise, actor=actor, until=base + timedelta(days=days), today=today
    )


_EDITABLE = (
    "title",
    "detail",
    "counterparty_kind",
    "counterparty_id",
    "counterparty_label",
    "object_kind",
    "object_id",
    "object_label",
    "is_private",
)


def update_fields(
    db: Session, promise: Promise, *, actor: str, **fields: Any
) -> Promise:
    """Edit the descriptive fields. A due-date change is its own transition
    (``move_due``) because it is the one edit the ledger cares about."""
    changed: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in _EDITABLE:
            raise ValueError(f"{key} is not editable")
        if key == "title":
            value = (value or "").strip()[:300]
            if not value:
                raise ValueError("a to-do needs a title")
        if getattr(promise, key) != value:
            changed[key] = value
            setattr(promise, key, value)
    if changed:
        kind = "edited"
        if set(changed) == {"is_private"}:
            kind = "made_private" if changed["is_private"] else "made_shared"
        record_event(
            db, promise, kind, actor=actor, payload={"fields": sorted(changed)}
        )
    return promise


def move_due(
    db: Session, promise: Promise, *, actor: str, due_local: date | None
) -> Promise:
    _require_status(promise, OPEN_PROMISE_STATUSES, "re-date")
    before = promise.due_local
    if before == due_local:
        return promise
    promise.due_local = due_local
    if promise.promised_for is None and due_local is not None:
        promise.promised_for = due_local
    record_event(
        db,
        promise,
        "due_moved",
        actor=actor,
        payload={
            "from": before.isoformat() if before else None,
            "to": due_local.isoformat() if due_local else None,
        },
    )
    return promise


def link_eos_todo(
    db: Session, promise: Promise, *, actor: str, eos_todo_id: str
) -> Promise:
    promise.eos_todo_id = eos_todo_id
    record_event(
        db, promise, "promoted_eos", actor=actor, payload={"eos_todo_id": eos_todo_id}
    )
    return promise


# ── Clock ────────────────────────────────────────────────────────────────────


def refresh_slipping(db: Session, owner_email: str, *, today: date) -> int:
    """Open promises whose date has passed become ``slipping``. Deterministic,
    idempotent, cheap — called on every list read so the page never shows an
    overdue item as merely open. Returns how many rows moved."""
    moved = 0
    rows = (
        db.query(Promise)
        .filter(
            Promise.owner_email == _fold(owner_email),
            Promise.status == "open",
            Promise.due_local.isnot(None),
            Promise.due_local < today,
        )
        .all()
    )
    for row in rows:
        row.status = "slipping"
        row.slipping_since = row.slipping_since or _now()
        record_event(
            db,
            row,
            "slipping",
            actor=SYSTEM_ACTOR,
            payload={"due_local": row.due_local.isoformat()},
        )
        moved += 1
    return moved


# ── Heard promises (the accountability pass) ────────────────────────────────

#: The ``heard_*`` sources: promises Cubie heard rather than ones typed in.
HEARD_SOURCES: tuple[str, ...] = ("heard_transcript", "heard_email", "heard_slack")

#: Statuses a heard promise can still be "not kept" in. ``suggested`` is here
#: on purpose: most people on AI Priorities have no My To-Dos page to accept a
#: suggestion on, and a promise is just as broken whether or not anyone
#: clicked Accept.
UNKEPT_CANDIDATE_STATUSES: tuple[str, ...] = ("suggested", "open", "slipping")

_STOPWORDS = frozenset(
    "a an the to of for and or on in with by me my i you your we our it this "
    "that send get over back up out follow".split()
)


def _title_tokens(title: str) -> set[str]:
    import re

    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def find_similar_heard(
    db: Session,
    owner_email: str,
    *,
    title: str,
    counterparty_id: str | None,
    said_on: date,
    window_days: int = 3,
) -> Promise | None:
    """An existing promise that is the same one heard again — in another
    source, or another meeting a day later. Any status counts: a suggestion the
    owner rejected must not come back because it was also said in an email.

    Same counterparty (or either side unknown), said within ``window_days``,
    and titles that share most of their meaningful words."""
    want = _title_tokens(title)
    if not want:
        return None
    lo = datetime.combine(said_on - timedelta(days=window_days), datetime.min.time())
    hi = datetime.combine(
        said_on + timedelta(days=window_days + 1), datetime.min.time()
    )
    cp = _fold(counterparty_id) if counterparty_id else None
    rows = (
        db.query(Promise)
        .filter(
            Promise.owner_email == _fold(owner_email),
            Promise.created_at >= lo - timedelta(days=1),
        )
        .all()
    )
    for row in rows:
        ref = row.source_ref or {}
        at_raw = ref.get("at")
        try:
            at = datetime.fromisoformat(at_raw) if at_raw else row.created_at
        except (TypeError, ValueError):
            at = row.created_at
        if at is not None and at.tzinfo is not None:
            at = at.replace(tzinfo=None)
        if at is None or not (lo <= at < hi):
            continue
        if cp and row.counterparty_id and _fold(row.counterparty_id) != cp:
            continue
        have = _title_tokens(row.title)
        if not have:
            continue
        overlap = len(want & have) / len(want | have)
        if overlap >= 0.5:
            return row
    return None


def list_unkept_candidates(db: Session, owner_email: str) -> list[Promise]:
    """Heard promises that have not been kept, dropped or found done."""
    return (
        db.query(Promise)
        .filter(
            Promise.owner_email == _fold(owner_email),
            Promise.source.in_(HEARD_SOURCES),
            Promise.status.in_(UNKEPT_CANDIDATE_STATUSES),
        )
        .order_by(Promise.created_at.desc())
        .all()
    )


def record_check(
    db: Session,
    promise: Promise,
    *,
    verdict: str,
    checked: list[str],
    summary: str | None = None,
    ref: dict[str, Any] | None = None,
    judge: str = "llm",
) -> Promise:
    """Store the result of the nightly "did this happen?" check.

    ``verdict="kept"`` is evidence: an open row moves to ``evidence_found``
    (the "Looks done, confirm?" bucket) and an ``evidence_found`` event is
    logged. A ``suggested`` row keeps its status — evidence is not acceptance —
    but carries the evidence, so the card stops calling it broken. It never
    reaches ``kept``: that is always a person's tap.

    Any other verdict (``not_found`` / ``unclear``) only refreshes the note the
    card quotes ("Cubie checked your sent mail and Slack…"). It is not a
    transition, so it logs no event; the kept-rate reads transitions only.
    """
    if verdict not in ("kept", "not_found", "unclear"):
        raise ValueError(f"unknown check verdict {verdict!r}")
    previous = (promise.evidence or {}).get("verdict")
    promise.evidence = {
        "verdict": verdict,
        "checked": list(checked),
        "checked_at": _now().isoformat(timespec="seconds"),
        "summary": (summary or "").strip()[:300] or None,
        "ref": ref or None,
        "judge": judge,
    }
    promise.updated_at = _now()
    if verdict == "kept" and previous != "kept":
        if promise.status in ("open", "slipping"):
            promise.status = "evidence_found"
        record_event(
            db,
            promise,
            "evidence_found",
            actor=SYSTEM_ACTOR,
            payload={"ref": ref, "summary": summary, "judge": judge},
        )
    return promise


def keep_from_card(db: Session, promise: Promise, *, actor: str) -> bool:
    """✓ on a priority card: the owner says it is done. A heard suggestion is
    accepted first, so the event log shows both what was heard and that it was
    kept. Returns False when the row is not in a keepable state."""
    if promise.status == "suggested":
        accept(db, promise, actor=actor)
    if promise.status not in OPEN_PROMISE_STATUSES:
        return False
    mark_kept(db, promise, actor=actor, via="priority_card")
    return True


def scanned_keys(db: Session, owner_email: str, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = (
        db.query(PromiseScan.source_key)
        .filter(
            PromiseScan.owner_email == _fold(owner_email),
            PromiseScan.source_key.in_(keys),
        )
        .all()
    )
    return {r[0] for r in rows}


def mark_scanned(db: Session, owner_email: str, key: str, *, found: int) -> None:
    db.add(
        PromiseScan(
            owner_email=owner_email, source_key=key, scanned_at=_now(), found=found
        )
    )


# ── Ledger ───────────────────────────────────────────────────────────────────


def week_ledger(db: Session, owner_email: str, *, today: date) -> dict[str, int]:
    """This week's tally (Monday through today, owner-local dates)."""
    week_start = today - timedelta(days=today.weekday())
    start_dt = datetime.combine(week_start, datetime.min.time())
    owner = _fold(owner_email)
    kept = (
        db.query(func.count(Promise.id))
        .filter(
            Promise.owner_email == owner,
            Promise.status == "kept",
            Promise.kept_at >= start_dt,
        )
        .scalar()
        or 0
    )
    dropped = (
        db.query(func.count(Promise.id))
        .filter(
            Promise.owner_email == owner,
            Promise.status == "dropped",
            Promise.dropped_at >= start_dt,
        )
        .scalar()
        or 0
    )
    open_count = (
        db.query(func.count(Promise.id))
        .filter(Promise.owner_email == owner, Promise.status.in_(OPEN_PROMISE_STATUSES))
        .scalar()
        or 0
    )
    slipping = (
        db.query(func.count(Promise.id))
        .filter(Promise.owner_email == owner, Promise.status == "slipping")
        .scalar()
        or 0
    )
    return {
        "week_start": week_start.isoformat(),
        "kept": int(kept),
        "dropped": int(dropped),
        "open": int(open_count),
        "slipping": int(slipping),
    }
