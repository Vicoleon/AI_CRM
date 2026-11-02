"""Deal defer — take a deal off a rep's lists until a follow-up date.

The one place every surface asks "is this deal deferred?". The table and the
reasons it is shaped the way it is live in ``rubik_core/database/deal_defers.py``;
the endpoints (and the HubSpot task write-through) in
``backend/routers/deal_defer.py``.

**Who hides it and who does not.** Rep-facing lists drop a deferred deal: My
Day's deal blocks, the daily-rhythm checks (on My Day and in Ask Cubie, for the
rep's own deals), and the morning priorities card. Manager-facing views never
drop it — the Sales Cockpit Team Roll-up and the weekly close-date roll-up list
every deferred deal with its owner, follow-up date and note instead, so a defer
cannot be used to hide a bad deal from the person who reviews the pipeline.

**Nothing runs on the follow-up date.** "Active" is computed at read time
(``follow_up_on`` after the reader's today), so a deal comes back on its date
even if every job in the building is down.

**Reads fail OPEN.** If the defers cannot be read, every deal is shown. Showing
a deal the rep deferred is an annoyance; hiding a deal nobody deferred is the
silent failure the cockpit exists to prevent.

Arithmetic only: no model call, so nothing here adds to Admin → AI Cost.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: The longest a deal may be deferred. Reps asked for "a few months"; past six
#: a deal is not deferred, it is lost or mis-staged, and that is a stage
#: conversation (sales leadership still owes the hold/defer criteria).
MAX_DEFER_DAYS = 180

#: A note is a reason for the manager and the body of a HubSpot task, not an
#: essay. HubSpot caps a task body far above this; the manager roll-up shows
#: the first line or so.
NOTE_MAX_CHARS = 1000


class DeferInvalid(ValueError):
    """A request that breaks a defer rule. The message is shown to the user."""


@dataclass(frozen=True)
class DeferInfo:
    """A defer as the surfaces read it — detached from any session."""

    id: str
    deal_id: str
    deal_name: str | None
    owner_email: str
    deferred_by: str
    follow_up_on: date
    note: str
    created_at: datetime
    hubspot_task_status: str
    hubspot_task_id: str | None
    hubspot_task_error: str | None

    @classmethod
    def from_row(cls, row) -> DeferInfo:
        return cls(
            id=row.id,
            deal_id=str(row.hubspot_deal_id),
            deal_name=row.deal_name,
            owner_email=row.owner_email,
            deferred_by=row.deferred_by,
            follow_up_on=row.follow_up_on,
            note=row.note,
            created_at=row.created_at,
            hubspot_task_status=row.hubspot_task_status or "pending",
            hubspot_task_id=row.hubspot_task_id,
            hubspot_task_error=row.hubspot_task_error,
        )


# ── Today ────────────────────────────────────────────────────────────────────


def reader_today(email: str | None) -> date:
    """The reader's local calendar day (their saved timezone, else the org's).

    A follow-up date is a day on the rep's calendar, so "has it arrived" is
    asked in the reader's timezone: a Pacific rep's deal must not come back at
    6 p.m. the evening before because UTC rolled over.
    """
    from rubik_core.priorities.temporal import resolve_user_now

    return resolve_user_now(email).now_local.date()


def org_today() -> date:
    """Today in the org's default timezone — for views that read many owners."""
    return reader_today(None)


def local_day(email: str | None, instant: datetime) -> date:
    """The calendar day a naive-UTC instant fell on for ``email``.

    "Deferred on" is shown to the rep, their manager and in the HubSpot task:
    a defer made at 8 p.m. Central is the 15th there, not the 16th UTC.
    """
    from rubik_core.priorities.temporal import resolve_user_now

    return resolve_user_now(email, now_utc=instant).now_local.date()


# ── Validation ───────────────────────────────────────────────────────────────


def validate(follow_up_on: date | None, note: str | None, *, today: date) -> str:
    """Check a defer request. Returns the cleaned note; raises ``DeferInvalid``.

    The follow-up date must be after today (a defer to today would hide the
    deal for no time at all) and no more than ``MAX_DEFER_DAYS`` out. The note
    is required: it is the reason the manager reads and the body of the
    HubSpot task.
    """
    if follow_up_on is None:
        raise DeferInvalid("Pick a follow-up date.")
    if follow_up_on <= today:
        raise DeferInvalid("The follow-up date has to be in the future.")
    latest = today + timedelta(days=MAX_DEFER_DAYS)
    if follow_up_on > latest:
        raise DeferInvalid(
            f"A deal can be deferred for at most {MAX_DEFER_DAYS} days "
            f"(until {latest.isoformat()})."
        )
    cleaned = (note or "").strip()
    if not cleaned:
        raise DeferInvalid(
            "Add a note: why is this deal on hold, and what happens next?"
        )
    if len(cleaned) > NOTE_MAX_CHARS:
        raise DeferInvalid(f"Keep the note under {NOTE_MAX_CHARS} characters.")
    return cleaned


# ── Reads ────────────────────────────────────────────────────────────────────


def _open_query(db: Session, deal_ids: Iterable[str] | None):
    from rubik_core.database.deal_defers import DealDefer

    query = db.query(DealDefer).filter(DealDefer.ended_at.is_(None))
    if deal_ids is not None:
        ids = sorted({str(d) for d in deal_ids if d})
        if not ids:
            return None
        query = query.filter(DealDefer.hubspot_deal_id.in_(ids))
    return query


def active_defers(
    db: Session,
    *,
    today: date,
    deal_ids: Iterable[str] | None = None,
    owner_emails: Iterable[str] | None = None,
) -> list[DeferInfo]:
    """Open defers whose follow-up date has not arrived, soonest first.

    ``deal_ids`` / ``owner_emails`` narrow the read; ``None`` means all. Raises
    on a database error — callers that must not fail use :func:`active_by_deal`.
    """
    from rubik_core.database.deal_defers import DealDefer

    query = _open_query(db, deal_ids)
    if query is None:
        return []
    query = query.filter(DealDefer.follow_up_on > today)
    if owner_emails is not None:
        owners = sorted({(e or "").strip().lower() for e in owner_emails if e})
        if not owners:
            return []
        query = query.filter(DealDefer.owner_email.in_(owners))
    rows = query.order_by(DealDefer.follow_up_on.asc(), DealDefer.created_at.asc())
    return [DeferInfo.from_row(r) for r in rows.all()]


def active_by_deal(
    deal_ids: Iterable[str] | None,
    *,
    reader: str | None = None,
    today: date | None = None,
    db: Session | None = None,
) -> dict[str, DeferInfo]:
    """{deal_id: defer} for the deals among ``deal_ids`` that are deferred now.

    Never raises: a failed read logs and returns ``{}`` (fail open — see the
    module docstring). ``today`` defaults to ``reader``'s local day.

    On a caller's session (``db``) the read runs in a SAVEPOINT. Swallowing the
    error is not enough on Postgres: a failed statement poisons the whole
    transaction, and the page's next query (My Day's wrap-ups, the digest's
    next read) would fail with it — turning "show every deal" into a 500.
    """
    if deal_ids is not None:
        deal_ids = [str(d) for d in deal_ids if d]
        if not deal_ids:
            return {}
    try:
        day = today or reader_today(reader)
        if db is not None:
            with db.begin_nested():
                found = active_defers(db, today=day, deal_ids=deal_ids)
        else:
            from rubik_core.database.db import SessionLocal

            with SessionLocal() as own:
                found = active_defers(own, today=day, deal_ids=deal_ids)
    except Exception:  # noqa: BLE001 — a filter must never take a page down
        logger.warning(
            "[deal-defer] could not read defers; showing every deal", exc_info=True
        )
        return {}
    return {d.deal_id: d for d in found}


def deferred_deal_ids(
    deal_ids: Iterable[str] | None,
    *,
    reader: str | None = None,
    today: date | None = None,
    db: Session | None = None,
) -> set[str]:
    """The ids among ``deal_ids`` deferred right now. Never raises."""
    return set(active_by_deal(deal_ids, reader=reader, today=today, db=db))


# ── The daily-rhythm checks ──────────────────────────────────────────────────


def hidden_note(count: int) -> str:
    """The sentence a list carries when it left deferred deals out."""
    if count == 1:
        return "1 deal you deferred is left out until its follow-up date."
    return f"{count} deals you deferred are left out until their follow-up dates."


def apply_to_check(result, defers: dict[str, DeferInfo], *, hide: bool):
    """A daily-rhythm ``CheckResult`` with ``defers`` applied. Never mutates.

    ``hide`` (the rep's own deals): deferred deal rows are dropped and a note
    says how many. Otherwise (a leader's team view): every row stays, and a
    deferred one says so, with its date and note — the same rule as the
    manager roll-ups. The input is the five-minute cached object, which is why
    this returns a copy rather than editing it.
    """
    if not defers or not getattr(result, "rows", None):
        return result
    rows = []
    hidden = 0
    for row in result.rows:
        info = defers.get(str(row.record_id)) if row.object_type == "deal" else None
        if info is None:
            rows.append(row)
        elif hide:
            hidden += 1
        else:
            rows.append(
                dataclasses.replace(
                    row,
                    reasons=[
                        *row.reasons,
                        f"deferred until {info.follow_up_on.isoformat()}: {info.note}",
                    ],
                )
            )
    if hide and not hidden:
        return result
    notes = list(result.notes)
    if hidden:
        notes.append(hidden_note(hidden))
    return dataclasses.replace(result, rows=rows, notes=notes)


# ── Writes ───────────────────────────────────────────────────────────────────


def _open_row(db: Session, deal_id: str):
    from rubik_core.database.deal_defers import DealDefer

    return (
        db.query(DealDefer)
        .filter(DealDefer.hubspot_deal_id == deal_id, DealDefer.ended_at.is_(None))
        .first()
    )


def defer(
    db: Session,
    *,
    deal_id: str,
    deal_name: str | None,
    owner_email: str,
    deferred_by: str,
    follow_up_on: date,
    note: str,
    today: date,
):
    """Defer a deal. Returns ``(row, outcome)``; commits.

    ``outcome`` is ``created`` (a new defer), ``unchanged`` (the same date and
    note are already in force — a double-click or a retry, which must reuse the
    row so the HubSpot task keys the same) or ``replaced`` (a new date or note:
    the old defer is closed and a new one opened, so the history stays).

    The caller has already run :func:`validate`. The partial UNIQUE index on
    open defers is the arbiter when two requests race: the loser re-reads.
    """
    from rubik_core.database.deal_defers import DealDefer

    deal_id = str(deal_id)
    now = datetime.utcnow()
    for _attempt in range(2):
        current = _open_row(db, deal_id)
        outcome = "created"
        if current is not None:
            if current.follow_up_on <= today:
                # Due already: the deal came back on its date. Close the row so
                # the new defer can open (one open row per deal).
                current.ended_at = now
                current.ended_by = deferred_by
                current.ended_reason = "expired"
            elif current.follow_up_on == follow_up_on and current.note == note:
                return current, "unchanged"
            else:
                current.ended_at = now
                current.ended_by = deferred_by
                current.ended_reason = "replaced"
                outcome = "replaced"
            # Close the old row before the new one exists, so the open-defer
            # index never sees two.
            db.flush()
        row = DealDefer(
            hubspot_deal_id=deal_id,
            deal_name=(deal_name or "").strip() or None,
            owner_email=owner_email,
            deferred_by=deferred_by,
            follow_up_on=follow_up_on,
            note=note,
            created_at=now,
            updated_at=now,
            hubspot_task_status="pending",
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Another request opened a defer for this deal between our read
            # and our insert. Re-read it and decide again.
            db.rollback()
            continue
        return row, outcome
    current = _open_row(db, deal_id)
    if current is None:  # pragma: no cover — the index said a row exists
        raise RuntimeError("could not open a defer for this deal")
    return current, "unchanged"


def undefer(db: Session, *, deal_id: str, by: str):
    """End the open defer on a deal, if any. Returns the row or ``None``; commits.

    A due row (follow-up date already arrived) is closed too: Undefer on a
    deal that already came back is a no-op for the lists and tidies the row.
    """
    row = _open_row(db, str(deal_id))
    if row is None:
        return None
    row.ended_at = datetime.utcnow()
    row.ended_by = by
    row.ended_reason = "undeferred"
    db.commit()
    return row
