"""Did it happen? — the second half of the accountability pass.

For each heard promise whose moment has come (its date has passed, or, with no
date, a few days have gone by), gather what the reader has DONE since they
said it — email they sent, Slack messages they posted, Drive files they
edited, EOS To-Dos they closed — and ask one Flash call, per person per
night, which promises that activity fulfils.

The verdict is stored on the row (``store.record_check``):

* ``kept`` — evidence found. The row moves toward "Looks done, confirm?" and
  the card stops calling it broken. It is NEVER marked kept: a wrong
  auto-close would hide a real broken promise, so that is always a person's
  tap (the inherited ``commitment_engine`` invariant).
* ``not_found`` / ``unclear`` — the card says so, and names what was checked,
  so "no sign it happened" is a statement with a denominator rather than an
  accusation.

Citations are validated in code: an evidence tag the model invents, or one
dated before the promise was made, cannot make a promise "kept".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: An undated promise is checked once this many days have passed.
UNDATED_GRACE_DAYS = 3
#: Promises older than this stop being chased: the card would be noise.
MAX_AGE_DAYS = 30
MAX_CHECKS_PER_RUN = 12
MAX_EVIDENCE_ITEMS = 80
EVIDENCE_CHARS = 220


@dataclass
class EvidenceItem:
    tag: str
    kind: str  # email | slack | drive | eos_todo
    at: datetime  # aware UTC
    text: str
    link: str | None = None


def said_at(promise: Any) -> datetime | None:
    raw = (promise.source_ref or {}).get("at")
    try:
        dt = datetime.fromisoformat(raw) if raw else promise.created_at
    except (TypeError, ValueError):
        dt = promise.created_at
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_due_for_check(promise: Any, *, today: date, now: datetime) -> bool:
    """The moment to ask "did it happen?" has come, and is not long gone."""
    when = said_at(promise)
    if when is None or (now - when) > timedelta(days=MAX_AGE_DAYS):
        return False
    if (promise.evidence or {}).get("verdict") == "kept":
        return False
    if promise.due_local is not None:
        return promise.due_local < today
    return (now - when) >= timedelta(days=UNDATED_GRACE_DAYS)


# ── Gathering ────────────────────────────────────────────────────────────────


def _clip(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:EVIDENCE_CHARS]


def email_evidence(
    user_email: str, threads: list[dict], *, since: datetime
) -> list[EvidenceItem]:
    from email.utils import getaddresses, parseaddr

    from backend.services.open_loops import _headers, _msg_datetime

    me = user_email.lower()
    out = []
    for t in threads or []:
        tid = t.get("id") or ""
        for m in t.get("messages") or []:
            h = _headers(m)
            _, sender = parseaddr(h.get("from", ""))
            when = _msg_datetime(m, h)
            if (sender or "").lower() != me or when is None or when < since:
                continue
            to = ", ".join(
                f"{n or a} <{a}>"
                for n, a in getaddresses([h.get("to", ""), h.get("cc", "")])
                if a
            )[:160]
            att = " [has attachment]" if _has_attachment(m) else ""
            out.append(
                EvidenceItem(
                    tag="",
                    kind="email",
                    at=when,
                    text=_clip(
                        f"sent to {to} · “{h.get('subject') or ''}”{att}: "
                        f"{m.get('snippet') or ''}"
                    ),
                    link=f"https://mail.google.com/mail/u/0/#all/{tid}"
                    if tid
                    else None,
                )
            )
    return out


def _has_attachment(msg: dict) -> bool:
    def walk(part: dict) -> bool:
        if (part.get("filename") or "").strip():
            return True
        return any(walk(p) for p in part.get("parts") or [])

    return walk(msg.get("payload") or {})


def slack_evidence(messages: list[Any], *, since: datetime) -> list[EvidenceItem]:
    out = []
    for m in messages or []:
        try:
            when = datetime.fromtimestamp(float(m.ts), tz=timezone.utc)
        except (TypeError, ValueError, AttributeError):
            continue
        if when < since:
            continue
        where = f"#{m.channel_name}" if m.channel_name else "a DM"
        out.append(
            EvidenceItem(
                tag="",
                kind="slack",
                at=when,
                text=_clip(f"posted in {where}: {m.text}"),
                link=m.permalink or m.app_link,
            )
        )
    return out


def drive_evidence(
    user_email: str, *, since: datetime, client: Any | None = None
) -> list[EvidenceItem]:
    if client is None:
        from rubik_core.tools.drive import GoogleDriveClient

        client = GoogleDriveClient(user_email=user_email)
    days = max(1, (datetime.now(timezone.utc) - since).days + 1)
    out = []
    for f in client.list_recent_files(days_back=days, max_results=40) or []:
        editor = ((f.get("lastModifyingUser") or {}).get("emailAddress") or "").lower()
        if editor and editor != user_email.lower():
            continue
        try:
            when = datetime.fromisoformat(
                (f.get("modifiedTime") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if when < since:
            continue
        shared = " (shared)" if f.get("shared") else ""
        out.append(
            EvidenceItem(
                tag="",
                kind="drive",
                at=when,
                text=_clip(f"edited Drive file “{f.get('name')}”{shared}"),
                link=f.get("webViewLink"),
            )
        )
    return out


def eos_evidence(user_email: str, *, since: datetime, db: Any) -> list[EvidenceItem]:
    from sqlalchemy import func

    from rubik_core.database.eos._base import TodoStatus
    from rubik_core.database.eos.people import Person
    from rubik_core.database.eos.todos import Todo

    person = (
        db.query(Person).filter(func.lower(Person.email) == user_email.lower()).first()
    )
    if person is None:
        return []
    rows = (
        db.query(Todo)
        .filter(
            Todo.owner_id == person.id,
            Todo.is_deleted.is_(False),
            Todo.updated_at >= since.replace(tzinfo=None),
        )
        .all()
    )
    out = []
    for t in rows:
        done = t.status == TodoStatus.DONE
        when = t.updated_at if done else t.created_at
        if when is None:
            continue
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        if when < since:
            continue
        verb = "completed EOS To-Do" if done else "created EOS To-Do"
        out.append(
            EvidenceItem(
                tag="", kind="eos_todo", at=when, text=_clip(f"{verb}: {t.description}")
            )
        )
    return out


def tag_pool(items: list[EvidenceItem]) -> list[EvidenceItem]:
    items = sorted(items, key=lambda e: e.at, reverse=True)[:MAX_EVIDENCE_ITEMS]
    for i, e in enumerate(items, 1):
        e.tag = f"E{i}"
    return items


# ── Judging ──────────────────────────────────────────────────────────────────


def build_judge_prompt(
    promises: list[Any], pool: list[EvidenceItem], *, reader_email: str
) -> str:
    plines = []
    for i, p in enumerate(promises, 1):
        ref = p.source_ref or {}
        when = said_at(p)
        plines.append(
            f"[P{i}] said {when.date().isoformat() if when else '?'}"
            + (f", due {p.due_local.isoformat()}" if p.due_local else "")
            + (f", for {p.counterparty_label}" if p.counterparty_label else "")
            + (f" <{p.counterparty_id}>" if p.counterparty_id else "")
            + f"\n  owes: {p.title}\n  their words: “{ref.get('quote') or ''}”"
        )
    elines = [
        f"[{e.tag}] {e.at.date().isoformat()} {e.kind}: {e.text}" for e in pool
    ] or ["(no activity found)"]
    return (
        f"You check whether {reader_email} kept promises they made. For each "
        "promise, look ONLY at the reader's own activity below, dated on or "
        "after the day the promise was made, and decide:\n"
        "- kept: an item clearly IS the promised thing being done (the email "
        "to that person with that deliverable, the file shared, the To-Do "
        "closed). Cite the item tag(s).\n"
        "- unclear: something related happened but you cannot tell it "
        "fulfilled the promise (an email to the right person about something "
        "else, a file edited but not sent).\n"
        "- not_found: nothing here does it.\n"
        "Be strict about `kept`: the reader will be told it looks done. "
        "Absence of evidence here is NOT proof it was not done (calls, "
        "in-person, other tools are invisible), so `not_found` only means "
        "this activity does not show it.\n\n"
        "## PROMISES\n" + "\n".join(plines) + "\n\n"
        "## READER ACTIVITY (newest first)\n" + "\n".join(elines) + "\n\n"
        "## OUTPUT (raw JSON only)\n"
        '{"checks": [{"promise": "P1", "verdict": "kept|unclear|not_found", '
        '"evidence": ["E3"], "note": "one short factual sentence"}]}'
    )


def parse_checks(
    payload: Any, promises: list[Any], pool: list[EvidenceItem]
) -> dict[str, dict]:
    """promise.id → {verdict, evidence: [EvidenceItem], note}. A `kept` that
    cites nothing real, or only items older than the promise, is downgraded to
    `unclear` — the model cannot close a promise on invented evidence."""
    by_tag = {e.tag: e for e in pool}
    by_ptag = {f"P{i}": p for i, p in enumerate(promises, 1)}
    out: dict[str, dict] = {}
    items = (payload or {}).get("checks") if isinstance(payload, dict) else None
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        p = by_ptag.get(str(it.get("promise") or "").strip().upper())
        if p is None:
            continue
        verdict = str(it.get("verdict") or "").strip().lower()
        if verdict not in ("kept", "unclear", "not_found"):
            continue
        when = said_at(p)
        cited = [
            by_tag[t]
            for t in (it.get("evidence") or [])
            if isinstance(t, str) and t in by_tag
        ]
        if when is not None:
            # Same-day is allowed: "I'll send it this afternoon" then did.
            floor = when - timedelta(hours=12)
            cited = [e for e in cited if e.at >= floor]
        if verdict == "kept" and not cited:
            verdict = "unclear"
        out[p.id] = {
            "verdict": verdict,
            "evidence": cited,
            "note": str(it.get("note") or "").strip()[:240],
        }
    return out


def checked_sources(available: dict[str, bool]) -> list[str]:
    """What the card may say was looked at — only lanes that were readable."""
    labels = {
        "email": "sent email",
        "slack": "Slack",
        "drive": "Drive",
        "eos_todo": "EOS To-Dos",
    }
    return [labels[k] for k in labels if available.get(k)]


def check(
    user_email: str,
    *,
    db: Any,
    invoke: Callable[[str], Any],
    deadline: Callable[[], bool],
    sent_threads: list[dict] | None,
    slack_messages: list[Any] | None,
    today: date,
    now: datetime | None = None,
    drive_client: Any | None = None,
) -> dict[str, int]:
    from rubik_core.promises import store

    now = now or datetime.now(timezone.utc)
    stats = {"checked": 0, "kept": 0, "not_found": 0, "unclear": 0}
    due = [
        p
        for p in store.list_unkept_candidates(db, user_email)
        if is_due_for_check(p, today=today, now=now)
    ][:MAX_CHECKS_PER_RUN]
    if not due or deadline():
        return stats

    since = min(said_at(p) for p in due) - timedelta(hours=12)
    available: dict[str, bool] = {}
    items: list[EvidenceItem] = []
    if sent_threads is not None:
        available["email"] = True
        items += email_evidence(user_email, sent_threads, since=since)
    if slack_messages is not None:
        available["slack"] = True
        items += slack_evidence(slack_messages, since=since)
    from rubik_core.control.repository import module_enabled

    try:
        eos_enabled = module_enabled("eos")
    except Exception:  # noqa: BLE001 — skip EOS when control cannot decide
        logger.warning("promises: EOS module lookup failed", exc_info=True)
        eos_enabled = False
    lanes = [
        ("drive", lambda: drive_evidence(user_email, since=since, client=drive_client))
    ]
    if eos_enabled:
        lanes.append(("eos_todo", lambda: eos_evidence(user_email, since=since, db=db)))
    for kind, fn in lanes:
        try:
            items += fn()
            available[kind] = True
        except Exception:  # noqa: BLE001 — an unreadable lane is just not checked
            logger.info("promises: %s evidence unavailable for %s", kind, user_email)
            if kind == "eos_todo":
                db.rollback()
    if not available:
        return stats  # nothing readable: claiming "no sign" would be a lie

    pool = tag_pool(items)
    payload = invoke(build_judge_prompt(due, pool, reader_email=user_email))
    if payload is None:
        return stats
    verdicts = parse_checks(payload, due, pool)
    looked_at = checked_sources(available)
    for p in due:
        v = verdicts.get(p.id)
        if v is None:
            continue
        ev = v["evidence"][0] if v["evidence"] else None
        store.record_check(
            db,
            p,
            verdict=v["verdict"],
            checked=looked_at,
            summary=v["note"],
            ref={
                "kind": ev.kind,
                "link": ev.link,
                "at": ev.at.isoformat(),
                "text": ev.text,
            }
            if ev
            else None,
        )
        stats["checked"] += 1
        stats[v["verdict"]] += 1
    db.commit()
    return stats
