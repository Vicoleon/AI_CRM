"""The ONLY writer of ``loop_items``, ``loop_mutes`` and ``loop_judgements``.

Every state change a loose end can go through is a function here, so the
rules that make the list trustworthy are enforced in one place:

* **A dismissed item is never re-opened or re-flagged.** ``apply_candidate``
  finds the dismissed row by its key and leaves it alone.
* **Only two closed items re-open**: an unlogged email thread, and only for a
  NEW unlogged AE email after it was closed (``detect.reopen_after``); and a
  todo closed ``rescheduled`` whose new date has now passed its grace days too.
  Everything else that closed stays closed; a new unanswered message is its
  own new item.
* **The scan never overwrites a click.** ``close_if_open`` closes with a
  conditional UPDATE (``WHERE status = 'open'``), so a dismissal or a "Log it
  in HubSpot" committed while the scan was reading is kept.
* **Closure is detected.** The scan closes with a detected reason; the AE's
  only manual exit is ``dismiss`` ("Not a real miss", with a reason, counted
  as noise). The one click that closes is ``mark_logged_from_assistant``, after
  the AE's emails it closes on are actually in HubSpot.
* **Evidence is ids and timestamps.** Nothing here copies a snippet, a body or
  a quote into ``close_evidence`` or ``detail``.

Callers commit (the router per request, the scan per AE), so a failure part
way through one AE never leaves half its list written.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from rubik_core.database.loop_closure import LoopItem, LoopJudgement, LoopMute
from rubik_core.identifiers import (
    CRM_LOGGED_DETAIL_KEY,
    LOOP_CLOSE_LOGGED_FROM_ASSISTANT,
)
from rubik_core.loop_closure.constants import (
    DISMISS_REASONS,
    MUTE_SCOPES,
    PUBLIC_EMAIL_DOMAINS,
)

logger = logging.getLogger(__name__)

#: Kinds a mute applies to: the ones keyed on an email counterparty.
MUTABLE_KINDS: tuple[str, ...] = ("unlogged", "open_loop")


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _naive(dt: datetime | None) -> datetime | None:
    """Aware → naive UTC, the shape every DateTime column holds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _now(now: datetime | None) -> datetime:
    return _naive(now) if now is not None else datetime.utcnow()


def _iso(dt: datetime) -> str:
    return _naive(dt).isoformat(timespec="seconds")


# ── Reads ────────────────────────────────────────────────────────────────────


def get_owned(
    db: Session, owner_email: str, item_id: str, *, for_update: bool = False
) -> LoopItem | None:
    """The owner's item, or None — the same None for someone else's id as for
    an id that does not exist, so ids cannot be probed.

    ``for_update`` locks the row until the request's transaction ends (``SELECT
    … FOR UPDATE`` on Postgres; SQLite has no row locks and ignores it) and
    re-reads it fresh, so a second "Log it in HubSpot" waits for the first and
    then sees the item already closed instead of logging the emails again.
    """
    if not item_id:
        return None
    query = db.query(LoopItem).filter(
        LoopItem.id == str(item_id), LoopItem.owner_email == _fold(owner_email)
    )
    if for_update:
        query = query.populate_existing().with_for_update()
    return query.first()


def items_for(
    db: Session, owner_email: str, *, refresh: bool = False
) -> dict[tuple[str, str], LoopItem]:
    """Every item the owner has, any status, keyed ``(kind, source_key)``.

    ``refresh`` re-reads rows already in the session, so a click committed by
    another request since they were first loaded (a dismissal during the scan's
    HubSpot read) is what the caller sees.
    """
    query = db.query(LoopItem).filter(LoopItem.owner_email == _fold(owner_email))
    if refresh:
        query = query.populate_existing()
    rows = query.all()
    return {(r.kind, r.source_key): r for r in rows}


def open_items(db: Session, owner_email: str) -> list[LoopItem]:
    return (
        db.query(LoopItem)
        .filter(LoopItem.owner_email == _fold(owner_email), LoopItem.status == "open")
        .order_by(LoopItem.opened_at)
        .all()
    )


def mutes_for(db: Session, owner_email: str) -> list[LoopMute]:
    return (
        db.query(LoopMute)
        .filter(LoopMute.owner_email == _fold(owner_email))
        .order_by(LoopMute.created_at)
        .all()
    )


# ── Scan writes ──────────────────────────────────────────────────────────────


def _fill(item: LoopItem, cand: Any) -> None:
    """Copy a candidate's descriptive fields onto a row (create or re-open)."""
    item.source = cand.source
    item.title = (cand.title or "")[:300]
    item.headline = (cand.headline or None) and cand.headline[:300]
    item.counterparty_email = cand.counterparty_email
    item.counterparty_label = (cand.counterparty_label or None) and str(
        cand.counterparty_label
    )[:200]
    item.company_label = (cand.company_label or None) and str(cand.company_label)[:200]
    item.hubspot_contact_id = cand.hubspot_contact_id
    item.hubspot_company_id = cand.hubspot_company_id
    item.hubspot_deal_id = cand.hubspot_deal_id
    item.thread_id = cand.thread_id
    item.message_id = cand.message_id
    item.link = (cand.link or None) and str(cand.link)[:500]
    item.detail = dict(cand.detail or {}) or None


def create_item(db: Session, owner_email: str, cand: Any, *, now: datetime) -> LoopItem:
    stamp = _now(now)
    item = LoopItem(
        owner_email=owner_email,
        kind=cand.kind,
        source_key=cand.source_key,
        status="open",
        opened_at=_naive(cand.opened_at),
        first_flagged_at=stamp,
        last_checked_at=stamp,
        reopened_count=0,
    )
    _fill(item, cand)
    db.add(item)
    return item


def apply_candidate(
    db: Session,
    owner_email: str,
    cand: Any,
    existing: LoopItem | None,
    *,
    now: datetime,
    reopen_after: datetime | None = None,
) -> tuple[str, LoopItem | None]:
    """Put one candidate on the list. Returns ``(outcome, item)`` where outcome
    is ``new`` | ``reopened`` | ``still_open`` | ``dismissed`` | ``closed``.

    ``reopen_after`` (from ``detect.reopen_after``) applies only to a closed
    unlogged email thread: it re-opens when the candidate carries an unlogged
    AE email sent after that instant, with ``opened_at`` moved to the earliest
    such email and ``reopened_count`` incremented.
    """
    if existing is None:
        return "new", create_item(db, owner_email, cand, now=now)
    if existing.status == "dismissed":
        return "dismissed", existing
    if existing.status == "open":
        existing.last_checked_at = _now(now)
        # Labels HubSpot learned since (a contact created, a company linked).
        if cand.company_label and not existing.company_label:
            existing.company_label = str(cand.company_label)[:200]
        if cand.counterparty_label and not existing.counterparty_label:
            existing.counterparty_label = str(cand.counterparty_label)[:200]
        crm_name = (cand.detail or {}).get("crm_contact_name")
        detail = existing.detail if isinstance(existing.detail, dict) else {}
        if crm_name and not detail.get("crm_contact_name"):
            existing.detail = {**detail, "crm_contact_name": crm_name}
        return "still_open", existing
    # Rescheduled, and now past its NEW date's grace days: the same promise or
    # task is owed again, so the row comes back with the new date as its age.
    if existing.kind == "todo" and existing.close_reason == "rescheduled":
        _fill(existing, cand)
        existing.status = "open"
        existing.opened_at = _naive(cand.opened_at)
        existing.closed_at = None
        existing.close_reason = None
        existing.close_evidence = None
        existing.last_checked_at = _now(now)
        existing.reopened_count = int(existing.reopened_count or 0) + 1
        return "reopened", existing
    # Closed: only an unlogged email thread with a NEW unlogged AE email.
    if existing.kind == "unlogged" and existing.source == "gmail" and reopen_after:
        newer = sorted(
            t for t in (cand.event_times or ()) if _aware(t) > _aware(reopen_after)
        )
        if newer:
            _fill(existing, cand)
            existing.status = "open"
            existing.opened_at = _naive(newer[0])
            existing.closed_at = None
            existing.close_reason = None
            existing.close_evidence = None
            existing.last_checked_at = _now(now)
            existing.reopened_count = int(existing.reopened_count or 0) + 1
            return "reopened", existing
    return "closed", existing


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def touch(item: LoopItem, *, now: datetime) -> None:
    item.last_checked_at = _now(now)


def close_if_open(
    db: Session,
    item: LoopItem,
    *,
    reason: str,
    evidence: dict | None,
    now: datetime,
) -> bool:
    """The scan's close: a conditional UPDATE that only lands on a row that is
    STILL open in the database. False (and the row re-read) when another
    request changed it since the scan loaded it — an AE who pressed "Not a
    real miss" or "Log it in HubSpot" while the scan was reading HubSpot keeps
    that outcome, and a dismissed item is never turned into a closed one that
    could re-open.
    """
    from rubik_core.loop_closure.constants import CLOSE_REASONS

    if reason not in CLOSE_REASONS:
        raise ValueError(f"unknown close reason {reason!r}")
    stamp = _now(now)
    values = {
        "status": "closed",
        "closed_at": stamp,
        "close_reason": reason,
        "close_evidence": dict(evidence or {}) or None,
        "last_checked_at": stamp,
    }
    changed = (
        db.query(LoopItem)
        .filter(LoopItem.id == item.id, LoopItem.status == "open")
        .update({**values, "updated_at": datetime.utcnow()}, synchronize_session=False)
    )
    if not changed:
        db.refresh(item)
        return False
    for key, value in values.items():
        set_committed_value(item, key, value)
    return True


def close_item(
    item: LoopItem, *, reason: str, evidence: dict | None, now: datetime
) -> LoopItem:
    """Close with a DETECTED reason (or ``aged_out``). Ids + timestamp only."""
    from rubik_core.loop_closure.constants import CLOSE_REASONS

    if reason not in CLOSE_REASONS:
        raise ValueError(f"unknown close reason {reason!r}")
    stamp = _now(now)
    item.status = "closed"
    item.closed_at = stamp
    item.close_reason = reason
    item.close_evidence = dict(evidence or {}) or None
    item.last_checked_at = stamp
    return item


# ── Judge memory ─────────────────────────────────────────────────────────────


def judged_keys(db: Session, owner_email: str) -> set[tuple[str, str]]:
    """``(kind, source_key)`` of every candidate the judge already declined."""
    rows = (
        db.query(LoopJudgement.kind, LoopJudgement.source_key)
        .filter(LoopJudgement.owner_email == _fold(owner_email))
        .all()
    )
    return {(k, key) for k, key in rows}


def record_declined(
    db: Session, owner_email: str, kind: str, source_key: str, *, now: datetime
) -> None:
    """Remember that the judge declined this candidate, so it is not sent
    again (a new client message is a new key and is judged afresh)."""
    db.add(
        LoopJudgement(
            owner_email=owner_email,
            kind=kind,
            source_key=source_key,
            verdict="declined",
            judged_at=_now(now),
        )
    )


def prune_judgements(db: Session, owner_email: str, *, before: datetime) -> int:
    """Forget declines older than ``before`` — the candidate can no longer come
    back once its message has left the lookback window."""
    return (
        db.query(LoopJudgement)
        .filter(
            LoopJudgement.owner_email == _fold(owner_email),
            LoopJudgement.judged_at < _now(before),
        )
        .delete(synchronize_session=False)
    )


# ── Click writes (the router) ────────────────────────────────────────────────


def _mute_pattern(item: LoopItem, scope: str) -> str:
    address = _fold(item.counterparty_email)
    if not address or "@" not in address:
        raise ValueError(
            "There is no sender address on this loose end, so there is nothing "
            "to mute. Dismiss it without muting."
        )
    if scope == "sender":
        return address
    domain = address.rsplit("@", 1)[1]
    if domain in PUBLIC_EMAIL_DOMAINS:
        raise ValueError(
            f"{domain} is a personal email provider, so muting the whole domain "
            "would hide every client who writes from it. Mute just this sender."
        )
    return f"@{domain}"


def _matches(address: str | None, pattern: str) -> bool:
    addr = _fold(address)
    if not addr:
        return False
    return addr == pattern or (pattern.startswith("@") and addr.endswith(pattern))


def dismiss(
    db: Session,
    item: LoopItem,
    *,
    reason: str,
    actor: str,
    mute: str | None = None,
    now: datetime | None = None,
) -> LoopItem:
    """ "Not a real miss" — the one manual exit, always with a reason.

    With ``mute`` (``"sender"`` | ``"domain"``) the counterparty — or their
    whole domain — is not flagged again for this AE, and the AE's other open
    email items from it are dismissed with the same reason (they are the same
    false flag). Raises ``ValueError`` with a sentence the AE can read for a
    bad reason, a bad mute, or an item that is no longer open.
    """
    if reason not in DISMISS_REASONS:
        raise ValueError("Pick one of the reasons offered for 'Not a real miss'.")
    if mute is not None and mute not in MUTE_SCOPES:
        raise ValueError("A mute is either this sender or their whole domain.")
    if item.status != "open":
        raise ValueError("This loose end is already closed.")
    stamp = _now(now)
    who = _fold(actor) or None

    pattern = _mute_pattern(item, mute) if mute else None

    def _dismiss_one(row: LoopItem) -> None:
        row.status = "dismissed"
        row.dismissed_at = stamp
        row.dismiss_reason = reason
        row.dismissed_by = who
        row.last_checked_at = stamp

    _dismiss_one(item)
    if pattern is None:
        return item

    owner = item.owner_email
    exists = (
        db.query(LoopMute)
        .filter(LoopMute.owner_email == owner, LoopMute.pattern == pattern)
        .first()
    )
    if exists is None:
        db.add(
            LoopMute(
                owner_email=owner, pattern=pattern, created_by=who, created_at=stamp
            )
        )
        # Visible to the next read in this transaction (sessions here do not
        # autoflush), so a second dismissal cannot add the same mute twice.
        db.flush()
    siblings = (
        db.query(LoopItem)
        .filter(
            LoopItem.owner_email == owner,
            LoopItem.status == "open",
            LoopItem.kind.in_(MUTABLE_KINDS),
            LoopItem.id != item.id,
        )
        .all()
    )
    for row in siblings:
        if _matches(row.counterparty_email, pattern):
            _dismiss_one(row)
    return item


def remove_mute(db: Session, owner_email: str, mute_id: str) -> bool:
    """Delete one of the owner's mutes. False (→ 404) for anyone else's id."""
    row = (
        db.query(LoopMute)
        .filter(LoopMute.id == str(mute_id), LoopMute.owner_email == _fold(owner_email))
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    return True


def remember_logged_emails(item: LoopItem, logged: dict[str, str]) -> None:
    """Add ``{gmail message id: HubSpot email id}`` to an OPEN item's detail.

    For emails "Log it in HubSpot" wrote while the item stays open: a click
    that failed partway, or one batch of a long thread. HubSpot's search can
    take a minute to show a new email, so the next preview and the next click
    read these (``crm_log.logged_by_cubie``) rather than trusting the probe
    alone. Ids only. The dict is replaced, never mutated in place, so the
    JSON column sees the change. Raises ``ValueError`` on a closed item.
    """
    if item.status != "open":
        raise ValueError("This loose end is already closed.")
    current = getattr(item, "detail", None)
    detail = dict(current) if isinstance(current, dict) else {}
    stored = detail.get(CRM_LOGGED_DETAIL_KEY)
    merged = dict(stored) if isinstance(stored, dict) else {}
    merged.update({str(m): str(h) for m, h in (logged or {}).items() if m and h})
    if not merged:
        return
    detail[CRM_LOGGED_DETAIL_KEY] = merged
    item.detail = detail


def mark_logged_from_assistant(
    db: Session,
    item: LoopItem,
    *,
    email_ids: list[str],
    message_ids: list[str],
    actor: str,
    now: datetime | None = None,
) -> LoopItem:
    """Close an unlogged item after "Log it in HubSpot" logged the AE's emails.

    Called only once every requested email is in HubSpot (created now, or
    found already there). ``close_evidence`` carries ids only — the HubSpot
    email engagement ids and the Gmail message ids, never a subject or a body —
    plus the time and the record URL derived from the item's own HubSpot ids,
    so a second click can answer with the first result instead of writing
    again. ``actor`` is accepted for symmetry with ``dismiss`` and not stored:
    only the owner can log their own item. Raises ``ValueError`` when the item
    is not an open unlogged item.
    """
    if item.kind != "unlogged":
        raise ValueError("Only a 'Not in HubSpot' loose end can be logged.")
    if item.status != "open":
        raise ValueError("This loose end is already closed.")
    from rubik_core.loop_closure.crm_log import hubspot_url_for

    stamp = _now(now)
    evidence: dict[str, Any] = {
        "type": "emails",
        "ids": [str(i) for i in email_ids or [] if i],
        "message_ids": [str(m) for m in message_ids or [] if m],
        "at": _iso(stamp),
    }
    url = hubspot_url_for(item)
    if url:
        evidence["hubspot_url"] = url
    return close_item(
        item, reason=LOOP_CLOSE_LOGGED_FROM_ASSISTANT, evidence=evidence, now=stamp
    )
