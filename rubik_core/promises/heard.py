"""Hearing promises — the first half of the accountability pass.

Reads what a person SAID in the last two weeks (meeting transcripts they were
in, the closing notes of their L10s, the email they sent, their Slack
messages) and writes each first-person promise to the ledger as a
``suggested`` row: what they said they would do, for whom, by when, and the
exact words. ``rubik_core/promises/evidence.py`` later checks whether it
happened; the ``promises`` priorities channel turns the unkept ones into
"You said you would…" cards.

Rules this module keeps:

* **Their own words only.** The prompt asks for promises made BY the reader,
  never ones made to them or by someone else in the room, and every promise
  must carry a verbatim quote. A promise with no quote is dropped.
* **Only meetings they were in.** Transcripts come from
  ``TranscriptService.list_transcripts_for_signals`` — the
  ``meeting_participants`` ACL — so nobody is told about a promise made in a
  meeting they could not open.
* **Each source is read once.** ``promise_scans`` remembers every transcript,
  L10 session, email and Slack message already read, so the model never sees
  the same thing twice and a rejected suggestion is never re-heard.
* **Below the confidence bar, nothing is written.** A card that accuses
  someone of breaking a promise they never made is worse than a missed one.

Pure helpers (prompt building, output parsing) are separated from the I/O so
they are unit-testable without Google, Slack or a model.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: How far back the listener reads. Long enough that the first night has
#: something useful to say, short enough that a promise is still fresh.
LOOKBACK_DAYS = 14
#: Model confidence below which a heard promise is not written at all.
MIN_CONFIDENCE = 0.7
#: Per-night caps, so one person's backlog cannot eat the batch's time budget.
MAX_TRANSCRIPTS_PER_RUN = 6
MAX_L10_PER_RUN = 3
MAX_EMAILS_PER_RUN = 30
MAX_SLACK_PER_RUN = 40
#: How much of one transcript reaches the model. Flash reads this comfortably;
#: the old Meet extractor read 1,500 characters, which is why promises made
#: after the first few minutes of a call were never heard.
TRANSCRIPT_CHARS = 60_000
MESSAGE_CHARS = 600


@dataclass
class HeardSource:
    """One thing the reader said, ready for the model."""

    key: str  # promise_scans.source_key
    source: str  # heard_transcript | heard_email | heard_slack
    entity_kind: str  # transcript | l10_session | email | slack
    entity_id: str
    title: str
    said_at: datetime  # aware UTC
    text: str
    link: str | None = None
    where: str = ""  # "the Acme renewal call", "email to Sam", "#sales"
    # email → name, for resolving "I'll send it to you" to a person.
    people: dict[str, str] = field(default_factory=dict)
    object_kind: str | None = None
    object_id: str | None = None
    # The TranscriptRecord a transcript source was listed from; its text is
    # read only once the source is known to be unscanned.
    record: Any = None


@dataclass
class HeardPromise:
    source: HeardSource
    title: str
    quote: str
    confidence: float
    due: date | None = None
    counterparty_email: str | None = None
    counterparty_label: str | None = None


# ── Prompting ────────────────────────────────────────────────────────────────


def _rules(reader_name: str, reader_email: str) -> str:
    return (
        f"The READER is {reader_name or reader_email} <{reader_email}>.\n\n"
        "Find every promise the READER personally made: a clear first-person "
        "commitment to do a specific thing, usually for someone else "
        '("I\'ll send you the deck by Friday", "let me get you those numbers '
        'tomorrow", "I\'ll follow up with legal and circle back").\n\n'
        "NOT a promise:\n"
        "- something someone ELSE committed to, or a promise made TO the reader\n"
        '- vague intentions with no deliverable ("we should catch up", '
        '"I\'ll think about it", "let\'s stay in touch")\n'
        '- something the reader did in the same breath ("I just sent it")\n'
        '- team plans with no owner ("we will review next quarter")\n'
        '- pleasantries and sign-offs ("talk soon", "I\'ll be there")\n\n'
        "For each promise return:\n"
        "- quote: the reader's words, VERBATIM from the text, at most 200 "
        "characters. Never paraphrase. If you cannot quote it, leave it out.\n"
        "- title: what they owe, as a short imperative, at most 80 characters "
        '("Send Sam the revised SOW")\n'
        "- counterparty_name / counterparty_email: who is waiting on it, when "
        "the text says (use an email only if it appears in PEOPLE), else null\n"
        "- due: YYYY-MM-DD when a date or day was stated, resolved against the "
        'date the reader said it ("Friday" = the next Friday after that date; '
        '"tomorrow" = the day after; "end of week" = that Friday), else null\n'
        "- confidence: 0.0-1.0 that this is a real promise BY the reader\n"
    )


def build_transcript_prompt(
    src: HeardSource, *, reader_name: str, reader_email: str
) -> str:
    people = "\n".join(f"- {n} <{e}>" for e, n in src.people.items()) or "(unknown)"
    return (
        "You read one meeting record and extract the promises one person made "
        "in it. Speaker lines may look like `Name: text` or `[Name]: text`; "
        "meeting notes may say `Name will …`, which counts when Name is the "
        "READER.\n\n" + _rules(reader_name, reader_email) + "\n## MEETING\n"
        f"title: {src.title}\n"
        f"date the reader said it: {src.said_at.date().isoformat()}\n"
        f"PEOPLE:\n{people}\n\n"
        f"## TEXT\n{src.text[:TRANSCRIPT_CHARS]}\n\n"
        "## OUTPUT (raw JSON only)\n"
        '{"promises": [{"quote": "...", "title": "...", "counterparty_name": '
        'null, "counterparty_email": null, "due": null, "confidence": 0.0}]}\n'
        'Return {"promises": []} when there are none — that is the usual answer.'
    )


def build_messages_prompt(
    sources: list[HeardSource], *, reader_name: str, reader_email: str
) -> str:
    lines = []
    for i, s in enumerate(sources, 1):
        people = ", ".join(f"{n} <{e}>" for e, n in s.people.items())
        lines.append(
            f"[M{i}] {s.said_at.date().isoformat()} · {s.where}"
            + (f" · PEOPLE: {people}" if people else "")
            + f"\n{s.text[:MESSAGE_CHARS]}"
        )
    return (
        "You read messages the READER wrote (email they sent, Slack messages "
        "they posted) and extract the promises in them. Every message below "
        "was written BY the reader.\n\n"
        + _rules(reader_name, reader_email)
        + "- message: the [M#] tag the promise is in\n\n"
        "## MESSAGES\n" + "\n\n".join(lines) + "\n\n"
        "## OUTPUT (raw JSON only)\n"
        '{"promises": [{"message": "M1", "quote": "...", "title": "...", '
        '"counterparty_name": null, "counterparty_email": null, "due": null, '
        '"confidence": 0.0}]}\n'
        'Return {"promises": []} when there are none — that is the usual answer.'
    )


# ── Parsing (pure) ───────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _quote_in_text(quote: str, text: str) -> bool:
    """The quote must really be in the source. Whitespace and case are
    forgiven (transcripts wrap lines); invention is not."""
    q = _norm(quote).strip(" .\"'“”")
    if len(q) < 8:
        return False
    hay = _norm(text)
    if q in hay:
        return True
    # Allow the model to have trimmed the middle of a long quote with "…".
    parts = [p.strip() for p in re.split(r"\.\.\.|…", q) if len(p.strip()) >= 8]
    return bool(parts) and all(p in hay for p in parts)


def _parse_due(raw: Any, said_on: date) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        d = date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None
    # A due date before the promise was made is a misread; a year out is not a
    # promise anyone is tracking day to day.
    if d < said_on or d > said_on + timedelta(days=366):
        return None
    return d


def parse_promises(
    payload: Any,
    sources: list[HeardSource],
    *,
    min_confidence: float = MIN_CONFIDENCE,
) -> list[HeardPromise]:
    """Model output → validated promises. One source → items need no tag;
    several → each item names its ``message`` tag (M1, M2, …)."""
    items = (payload or {}).get("promises") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    by_tag = {f"M{i}": s for i, s in enumerate(sources, 1)}
    out: list[HeardPromise] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if len(sources) == 1:
            src = sources[0]
        else:
            src = by_tag.get(str(it.get("message") or "").strip().upper())
            if src is None:
                continue
        try:
            conf = float(it.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        quote = str(it.get("quote") or "").strip()
        title = str(it.get("title") or "").strip()
        if conf < min_confidence or not title or not _quote_in_text(quote, src.text):
            continue
        email = str(it.get("counterparty_email") or "").strip().lower() or None
        if email and email not in src.people:
            email = None  # only emails we handed it — never one it made up
        label = str(it.get("counterparty_name") or "").strip() or None
        if email and not label:
            label = src.people.get(email) or email
        out.append(
            HeardPromise(
                source=src,
                title=title[:120],
                quote=quote[:300],
                confidence=round(conf, 2),
                due=_parse_due(it.get("due"), src.said_at.date()),
                counterparty_email=email,
                counterparty_label=label[:120] if label else None,
            )
        )
    return out


# ── Gathering (I/O) ──────────────────────────────────────────────────────────


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{25,}$")


def _transcript_link(meeting_id: str) -> str | None:
    """A Drive file id gets its Drive link; Fathom ids and Meet conference
    records have no stable link the reader can open."""
    mid = (meeting_id or "").strip()
    if _DRIVE_ID.match(mid) and not mid.isdigit():
        return f"https://drive.google.com/file/d/{mid}/view"
    return None


def _people_names(db: Any, emails: list[str]) -> dict[str, str]:
    from sqlalchemy import func

    from rubik_core.database.models import User

    folded = sorted({(e or "").strip().lower() for e in emails if e})
    if not folded:
        return {}
    names = {
        (u.email or "").lower(): (u.name or u.email)
        for u in db.query(User)
        .filter(func.lower(User.email).in_(folded), User.is_platform_support.is_(False))
        .all()
    }
    return {e: names.get(e, e) for e in folded}


def gather_transcripts(
    user_email: str, *, since: datetime, db: Any, service: Any | None = None
) -> list[HeardSource]:
    if service is None:
        from backend.services.transcript_service import TranscriptService

        service = TranscriptService(user_email)
    out: list[HeardSource] = []
    for rec in service.list_transcripts_for_signals(since=since):
        title = (rec.original_filename or "Meeting").strip()
        people = _people_names(db, [p for p in rec.participants if p != user_email])
        out.append(
            HeardSource(
                key=f"transcript:{rec.doc_id}",
                source="heard_transcript",
                entity_kind="transcript",
                entity_id=rec.doc_id,
                title=title[:160],
                said_at=_aware(rec.created_at),
                text="",  # read lazily — only for sources not yet scanned
                link=_transcript_link(rec.meeting_id),
                where=f"the meeting “{title[:80]}”",
                people=people,
                object_kind="transcript",
                object_id=rec.doc_id,
                record=rec,
            )
        )
    return out


def read_transcript(src: HeardSource, service: Any | None = None) -> str:
    rec = src.record
    if rec is None:
        return ""
    if service is None:
        from backend.services.transcript_service import TranscriptService

        service = TranscriptService(rec.owner_email or "")
    return service.read_transcript_text(rec, max_bytes=TRANSCRIPT_CHARS * 2) or ""


def gather_l10_sessions(
    user_email: str, *, since: datetime, db: Any
) -> list[HeardSource]:
    """Closing notes of L10s the reader attended. The recording itself, when
    there is one, already arrives as a transcript."""
    from sqlalchemy import func

    from rubik_core.database.eos.l10 import L10Attendance, L10Meeting, L10Session
    from rubik_core.database.eos.people import Person

    person = (
        db.query(Person).filter(func.lower(Person.email) == user_email.lower()).first()
    )
    if person is None:
        return []
    rows = (
        db.query(L10Session, L10Meeting)
        .join(L10Meeting, L10Meeting.id == L10Session.meeting_id)
        .join(L10Attendance, L10Attendance.session_id == L10Session.id)
        .filter(
            L10Attendance.person_id == person.id,
            L10Attendance.attended.is_(True),
            L10Session.actual_end.isnot(None),
            L10Session.actual_end >= since.replace(tzinfo=None),
            L10Session.notes.isnot(None),
        )
        .order_by(L10Session.actual_end.desc())
        .all()
    )
    out = []
    for sess, meeting in rows:
        notes = (sess.notes or "").strip()
        if len(notes) < 40:
            continue
        out.append(
            HeardSource(
                key=f"l10:{sess.id}",
                source="heard_transcript",
                entity_kind="l10_session",
                entity_id=str(sess.id),
                title=f"{meeting.name} L10",
                said_at=_aware(sess.actual_end),
                text=notes,
                link=f"/eos/meetings/{meeting.id}",
                where=f"the {meeting.name} L10",
                people=_people_names(
                    db,
                    [e for e in (meeting.roster_emails or []) if e != user_email],
                ),
            )
        )
    return out


def gather_sent_email(
    user_email: str,
    *,
    since: datetime,
    client: Any | None = None,
    threads: list[dict] | None = None,
) -> list[HeardSource]:
    """One source per message the reader SENT. ``threads`` lets the caller
    reuse a fetch (the evidence check reads the same Sent lane)."""
    from email.utils import getaddresses, parseaddr

    from backend.services.open_loops import _headers, _msg_datetime

    me = user_email.lower()
    if threads is None:
        threads = fetch_sent_threads(user_email, days=LOOKBACK_DAYS, client=client)
    out: list[HeardSource] = []
    for t in threads:
        tid = t.get("id") or ""
        for m in t.get("messages") or []:
            h = _headers(m)
            _, sender = parseaddr(h.get("from", ""))
            when = _msg_datetime(m, h)
            if (sender or "").lower() != me or when is None or when < since:
                continue
            people = {
                addr.lower(): (name or addr)
                for name, addr in getaddresses([h.get("to", ""), h.get("cc", "")])
                if addr and addr.lower() != me
            }
            subject = (h.get("subject") or "(no subject)").strip()
            to_label = ", ".join(list(people.values())[:2]) or "someone"
            snippet = re.sub(r"\s+", " ", m.get("snippet") or "").strip()
            if not snippet:
                continue
            out.append(
                HeardSource(
                    key=f"gmail:{m.get('id') or tid}",
                    source="heard_email",
                    entity_kind="email",
                    entity_id=m.get("id") or tid,
                    title=subject[:160],
                    said_at=when,
                    text=f"Subject: {subject}\n{snippet}",
                    link=f"https://mail.google.com/mail/u/0/#all/{tid}"
                    if tid
                    else None,
                    where=f"your email to {to_label} (“{subject[:60]}”)",
                    people=people,
                    object_kind="thread",
                    object_id=tid or None,
                )
            )
    out.sort(key=lambda s: s.said_at, reverse=True)
    return out


def fetch_sent_threads(
    user_email: str, *, days: int, client: Any | None = None, max_results: int = 50
) -> list[dict]:
    if client is None:
        from rubik_core.tools.gmail import GmailClient

        client = GmailClient(user_email=user_email)
    return (
        client.search_threads(
            query=f"in:sent newer_than:{days}d -in:chats", max_results=max_results
        )
        or []
    )


def fetch_own_slack(
    user_email: str, *, since: datetime, svc: Any | None = None, limit: int = 60
) -> list[Any]:
    """The reader's own Slack messages since ``since``, newest first. Empty
    when Slack is not connected or the token lacks search scope."""
    if svc is None:
        from backend.services.slack_service import SlackService

        svc = SlackService(user_email)
    if not svc.is_connected:
        return []
    uid = svc.slack_user_id()
    if not uid:
        return []
    after = (since - timedelta(days=1)).date().isoformat()
    return (
        svc.search_messages(
            f"from:<@{uid}> after:{after}", limit=limit, sort="timestamp"
        )
        or []
    )


def gather_slack(messages: list[Any], *, since: datetime) -> list[HeardSource]:
    out = []
    for m in messages:
        try:
            when = datetime.fromtimestamp(float(m.ts), tz=timezone.utc)
        except (TypeError, ValueError, AttributeError):
            continue
        text = re.sub(r"\s+", " ", m.text or "").strip()
        if when < since or len(text) < 12:
            continue
        chan = m.channel_name or "a DM"
        where = f"Slack #{chan}" if m.channel_name else "a Slack DM"
        out.append(
            HeardSource(
                key=f"slack:{m.channel_id}:{m.ts}",
                source="heard_slack",
                entity_kind="slack",
                entity_id=f"{m.channel_id}:{m.ts}",
                title=text[:80],
                said_at=when,
                text=text,
                link=m.permalink or m.app_link,
                where=where,
            )
        )
    return out


# ── Writing ──────────────────────────────────────────────────────────────────


def write_heard(db: Any, user_email: str, promises: list[HeardPromise]) -> int:
    """Suggested rows, de-duplicated against everything already heard."""
    from rubik_core.promises import store

    written = 0
    tz = store.owner_timezone(db, user_email)
    for p in promises:
        said_on = p.source.said_at.date()
        if store.find_similar_heard(
            db,
            user_email,
            title=p.title,
            counterparty_id=p.counterparty_email,
            said_on=said_on,
        ):
            continue
        store.create_promise(
            db,
            owner_email=user_email,
            title=p.title,
            source=p.source.source,
            actor=store.SYSTEM_ACTOR,
            status="suggested",
            due_local=p.due,
            timezone=tz,
            counterparty_kind="person" if p.counterparty_email else None,
            counterparty_id=p.counterparty_email,
            counterparty_label=p.counterparty_label,
            object_kind=p.source.object_kind,
            object_id=p.source.object_id,
            object_label=p.source.title,
            confidence=p.confidence,
            source_ref={
                "entity_kind": p.source.entity_kind,
                "entity_id": p.source.entity_id,
                "quote": p.quote,
                "at": p.source.said_at.astimezone(timezone.utc)
                .replace(tzinfo=None)
                .isoformat(timespec="seconds"),
                "link": p.source.link,
                "where": p.source.where,
            },
        )
        written += 1
    return written


def hear(
    user_email: str,
    *,
    reader_name: str,
    db: Any,
    invoke: Callable[[str], Any],
    deadline: Callable[[], bool],
    sent_threads: list[dict] | None,
    slack_messages: list[Any] | None,
    now: datetime | None = None,
    transcript_service: Any | None = None,
) -> dict[str, int]:
    """Read every not-yet-read source and write what was promised.

    ``invoke(prompt) -> dict`` is the model call; ``deadline()`` is True once
    the pass's time budget is spent. Every source read is marked scanned in
    the same transaction as the promises it produced, so a crash mid-pass
    re-reads it next night rather than losing it.
    """
    from rubik_core.promises import store

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    stats = {"sources_read": 0, "heard": 0, "written": 0}

    def _unscanned(sources: list[HeardSource]) -> list[HeardSource]:
        done = store.scanned_keys(db, user_email, [s.key for s in sources])
        return [s for s in sources if s.key not in done]

    def _commit(sources: list[HeardSource], found: list[HeardPromise]) -> None:
        stats["sources_read"] += len(sources)
        stats["heard"] += len(found)
        stats["written"] += write_heard(db, user_email, found)
        for s in sources:
            store.mark_scanned(
                db,
                user_email,
                s.key,
                found=sum(1 for p in found if p.source is s),
            )
        db.commit()

    # 1. Meetings — one call each, newest first.
    try:
        meetings = _unscanned(
            gather_transcripts(
                user_email, since=since, db=db, service=transcript_service
            )
        )[:MAX_TRANSCRIPTS_PER_RUN]
    except Exception:  # noqa: BLE001 — one source failing never stops the rest
        logger.warning(
            "promises: transcript gather failed for %s", user_email, exc_info=True
        )
        meetings = []
    for src in meetings:
        if deadline():
            break
        src.text = read_transcript(src, transcript_service)
        if len(src.text) < 200:
            _commit([src], [])
            continue
        payload = invoke(
            build_transcript_prompt(
                src, reader_name=reader_name, reader_email=user_email
            )
        )
        if payload is None:
            continue  # model failed — leave unscanned, retry next night
        _commit([src], parse_promises(payload, [src]))

    # 2. L10 closing notes — only while this workspace's EOS module is on.
    from rubik_core.control.repository import module_enabled

    try:
        eos_enabled = module_enabled("eos")
    except Exception:  # noqa: BLE001 — skip EOS when control cannot decide
        logger.warning("promises: EOS module lookup failed", exc_info=True)
        eos_enabled = False
    l10s = []
    if eos_enabled:
        try:
            l10s = _unscanned(gather_l10_sessions(user_email, since=since, db=db))[
                :MAX_L10_PER_RUN
            ]
        except Exception:  # noqa: BLE001
            logger.warning(
                "promises: L10 gather failed for %s", user_email, exc_info=True
            )
            db.rollback()
    for src in l10s:
        if deadline():
            break
        payload = invoke(
            build_transcript_prompt(
                src, reader_name=reader_name, reader_email=user_email
            )
        )
        if payload is None:
            continue
        _commit([src], parse_promises(payload, [src]))

    # 3+4. Sent email and Slack — one batched call per lane.
    lanes: list[list[HeardSource]] = []
    if sent_threads is not None:
        try:
            lanes.append(
                _unscanned(
                    gather_sent_email(user_email, since=since, threads=sent_threads)
                )[:MAX_EMAILS_PER_RUN]
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "promises: email gather failed for %s", user_email, exc_info=True
            )
    if slack_messages is not None:
        lanes.append(
            _unscanned(gather_slack(slack_messages, since=since))[:MAX_SLACK_PER_RUN]
        )
    for batch in lanes:
        if not batch or deadline():
            continue
        payload = invoke(
            build_messages_prompt(
                batch, reader_name=reader_name, reader_email=user_email
            )
        )
        if payload is None:
            continue
        _commit(batch, parse_promises(payload, batch))

    return stats
