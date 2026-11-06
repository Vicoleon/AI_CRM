"""The Fathom fallback — a Fathom recording becomes a captured sales call.

The Fathom fallback and the ``fathom`` value in ``TRANSCRIPT_SOURCES`` have
existed since v1; the
extractor already relaxes its citation rule for Fathom entries. What was
missing was the writer: nothing turned a Fathom meeting into a
``call_sessions`` row with ``call_transcript_entries``, so a Fathom-recorded
call got RAG-ingested for Chat and then vanished from Today's Calls — no
wrap-up, no SOW clock, no Draft proposal — for an AE who records every call
with Fathom, that was the whole workflow.

:func:`attach_meeting` closes that gap for ONE Fathom meeting object (the shape
``FathomClient.list_meetings(include_transcript=True)`` returns), and it is
called from the Fathom sync — the ``meeting.completed`` webhook and the manual
``POST /fathom/ingest`` both run ``process_fathom_transcripts_background``,
which now calls this per meeting BEFORE its own "already RAG-ingested" skip,
so the backfill of recordings that predate this module is the same code path.

What it does, in order, and why each step stops where it stops:

1. **Host must be ours.** ``recorded_by.email`` on our Workspace domain. Fathom's
   list is account-wide; a recording made by a client-side Fathom licence that
   somehow appears is not our call to capture.
2. **At least one external attendee.** Internal meetings (an L10, a 1:1) are
   not sales calls; a session for them would page an AE about a wrap-up that
   has nothing to file and start a wrap-up clock nobody should be nudged on.
3. **Find the session or create it.** Replays key on
   ``call_sessions.fathom_recording_id`` (a UNIQUE column, because a
   found-or-created-by key must be a column — see ``calendar_event_id``). A
   recording with no row yet is matched to the host's existing session in the
   same time window (the calendar-bound session Cubie made for the meeting, or
   the ad-hoc one the Meet sweep found), else a new ``origin="fathom"`` row.
4. **Never write a second transcript.** If the matched session already holds
   entries from ANY source — the Meet path captured it — the recording is only
   linked, so the extractor never reads the same call twice. A suppressed
   (confidential) session gets no entries either, same as the Meet ingest.
5. **Bind the company** from the external attendees' email domains through
   ``domain_match`` — the same blocklists and the same "exactly one candidate
   or nothing" rule the calendar screen uses. Ambiguous stays NULL and the
   wrap-up asks, because a wrong binding writes to the wrong CRM record.
6. **Write the entries** with a deterministic ``external_id``
   (``fathom:<recording>:<position>``) so the table's UNIQUE makes a replay a
   no-op — the constraint the model docstring says Fathom must mint to use.
7. **Hand the session to the existing pipeline:** ``transcript_ready``, the
   ``wrapup`` commitment, one extraction — the exact three calls the Meet sweep
   makes, imported from it rather than re-implemented.

Fathom's RAG document, its DAM asset and its ``meeting_participants`` ACL row
are NOT written here: the sync that calls this already does that per meeting,
keyed on ``sha256("fathom_<id>")``. This module owns the call-capture side only.

Nothing here raises past :func:`attach_meeting`: every failure returns an
``outcome`` string and is logged, because the caller is a batch that must keep
going, and because "Fathom sync failed" must never cost the RAG ingest that
runs next to it.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rubik_core.call_capture import sweep as _sweep
from rubik_core.call_capture import transcript_ingest as _ingest
from rubik_core.call_capture.domain_match import (
    is_company_matchable,
    own_domains,
    resolve_domain_matches,
)
from rubik_core.call_capture.provenance import (
    ATTR_KEY as CRM_PROVENANCE_ATTR,
)
from rubik_core.call_capture.provenance import (
    session_source_visible,
    stamp_session_source,
)
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession, CallTranscriptEntry
from rubik_core.identifiers import FATHOM_SKIP_HOST_OUTSIDE

logger = logging.getLogger(__name__)

FATHOM_SOURCE = _ingest.FATHOM_SOURCE

#: ``CallSession.attrs`` key holding this module's own bookkeeping (title, the
#: share URL, what the last pass did). Reassigned, never mutated.
FATHOM_ATTRS_KEY = "fathom"

#: How far a Fathom recording's start may sit from an existing session's start
#: and still be "the same call". Calendar sessions are stamped from the event,
#: Fathom from when the bot joined; a late-joining bot or an early host is
#: minutes, not an hour. Widening this risks gluing two back-to-back calls with
#: the same client together, which is worse than a duplicate row.
MATCH_WINDOW = timedelta(minutes=45)

# Outcomes. A closed set so the sync can log a histogram and a test can pin
# each branch by name.
ATTACHED = "attached"
REPLAYED = "replayed"
LINKED_EXISTING_TRANSCRIPT = "linked_existing_transcript"
SUPPRESSED = "suppressed"
SKIPPED_NO_ID = "skipped_no_id"
SKIPPED_INTERNAL_ONLY = "skipped_internal_only"
SKIPPED_NO_TRANSCRIPT = "skipped_no_transcript"
SKIPPED_DISABLED = "skipped_disabled"
SKIPPED_TOO_OLD = "skipped_too_old"
FAILED = "failed"

#: A wrap-up clock is only opened for a recording this fresh. The Meet sweep
#: never sees a call older than its 24-hour lookback, so a Fathom backfill is
#: the one path that can meet a three-week-old recording — and opening a
#: ``wrapup`` commitment dated three weeks ago breaches on creation, lands on
#: the nudge worklist and escalates about a call everybody has moved past.
#: The transcript, the extraction and the proposal are still worth having for
#: such a call; the nag is not.
WRAPUP_CLOCK_MAX_AGE = timedelta(hours=48)


# ── Reading the Fathom object ────────────────────────────────────────────────


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def recording_id(meeting: dict) -> str | None:
    """Fathom's id for the recording, as a string, or None."""
    raw = meeting.get("id") or meeting.get("recording_id")
    return str(raw) if raw not in (None, "") else None


def host_email(meeting: dict) -> str:
    recorded_by = meeting.get("recorded_by")
    if isinstance(recorded_by, dict):
        return _fold(recorded_by.get("email"))
    # Legacy / webhook shapes.
    return _fold(meeting.get("host_email"))


def invitees(meeting: dict) -> list[dict]:
    out: list[dict] = []
    for raw in meeting.get("calendar_invitees") or meeting.get("attendees") or []:
        if isinstance(raw, dict) and _fold(raw.get("email")):
            out.append(
                {
                    "email": _fold(raw.get("email")),
                    "name": (raw.get("name") or "").strip(),
                    "is_external": raw.get("is_external"),
                }
            )
    return out


def external_attendees(meeting: dict, *, host: str) -> list[dict]:
    """Invitees outside verified control domains, regardless of Fathom's flag."""
    own = own_domains(host)
    out: list[dict] = []
    for person in invitees(meeting):
        if person["email"] == host:
            continue
        if _domain(person["email"]) not in own:
            out.append(person)
    return out


def _host_is_ours(db: Session, host: str) -> bool:
    """Is the recorder one of us?

    Only verified control domains count. ``own_domains(host)`` would be wrong
    here because it unions in the host's domain, making every recorder look
    like us. An unconfigured workspace must fail closed even if a users row
    happens to contain the address.
    """
    configured = own_domains()
    return bool(configured) and _domain(host) in configured


def _parse_instant(value: Any) -> datetime | None:
    """ISO-8601 (with or without offset) → naive UTC, the schema's shape."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?$")


def _offset_ms(value: Any, base: datetime | None) -> int | None:
    """A per-utterance offset from the start of the recording, or None.

    Fathom usually supplies none, sometimes a clock string
    (``"00:12:34"``), occasionally a full instant. All three are tolerated;
    anything else is None, which the coverage calculator already knows how to
    read as "Fathom, no offsets".
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Seconds when small, milliseconds when large — Fathom has used both.
        return int(value * 1000) if value < 100_000 else int(value)
    if not isinstance(value, str):
        return None
    m = _CLOCK.match(value.strip())
    if m:
        hours, minutes, seconds, frac = m.groups()
        ms = ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
        return ms + int((frac or "0").ljust(3, "0"))
    instant = _parse_instant(value)
    if instant is not None and base is not None:
        return max(0, int((instant - base).total_seconds() * 1000))
    return None


def _window(
    meeting: dict, *, last_offset_ms: int | None
) -> tuple[datetime | None, datetime | None]:
    """``(started_at, ended_at)`` for the session, best available source first."""
    started = (
        _parse_instant(meeting.get("recording_start_time"))
        or _parse_instant(meeting.get("scheduled_start_time"))
        or _parse_instant(meeting.get("started_at"))
        or _parse_instant(meeting.get("created_at"))
    )
    ended = _parse_instant(meeting.get("recording_end_time")) or _parse_instant(
        meeting.get("scheduled_end_time")
    )
    if ended is None and started is not None:
        duration = meeting.get("duration_seconds") or meeting.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            ended = started + timedelta(seconds=float(duration))
        elif last_offset_ms:
            ended = started + timedelta(milliseconds=last_offset_ms)
    return started, ended


def transcript_rows(meeting: dict) -> list[dict]:
    """Fathom's transcript, normalised to ``{speaker_name, speaker_email, text, offset_ms}``.

    Two shapes are tolerated because both have been seen: the flat
    ``{"speaker_name", "text"}`` this repo's client reads, and the API's nested
    ``{"speaker": {"display_name", "matched_calendar_invitee_email"}, "text",
    "timestamp"}``. Empty utterances are dropped — ``text`` is NOT NULL and an
    empty line can never be cited.
    """
    base = _parse_instant(meeting.get("recording_start_time"))
    by_name = {p["name"].lower(): p["email"] for p in invitees(meeting) if p["name"]}
    rows: list[dict] = []
    for raw in meeting.get("transcript") or []:
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            continue
        speaker = raw.get("speaker")
        if isinstance(speaker, dict):
            name = (speaker.get("display_name") or speaker.get("name") or "").strip()
            email = _fold(
                speaker.get("matched_calendar_invitee_email") or speaker.get("email")
            )
        else:
            name = (
                raw.get("speaker_name")
                or (speaker if isinstance(speaker, str) else "")
                or ""
            ).strip()
            email = _fold(raw.get("speaker_email"))
        if not email and name:
            email = by_name.get(name.lower(), "")
        rows.append(
            {
                "speaker_name": name or None,
                "speaker_email": email or None,
                "text": text,
                "offset_ms": _offset_ms(
                    raw.get("timestamp") or raw.get("start_time") or raw.get("start"),
                    base,
                ),
            }
        )
    return rows


# ── Session resolution ───────────────────────────────────────────────────────


def _by_recording(db: Session, rid: str) -> CallSession | None:
    return db.query(CallSession).filter(CallSession.fathom_recording_id == rid).first()


def _nearby_session(
    db: Session, host: str, started: datetime | None
) -> CallSession | None:
    """The host's own session for the same call, if Cubie already has one.

    A calendar-bound session (the AE clicked Start on Today's Calls) or an
    ad-hoc one the Meet sweep found. Nearest start wins; a session that already
    carries a Fathom recording is somebody else's call.
    """
    if started is None:
        return None
    lo, hi = started - MATCH_WINDOW, started + MATCH_WINDOW
    candidates = (
        db.query(CallSession)
        .filter(
            CallSession.ae_email == host,
            CallSession.fathom_recording_id.is_(None),
        )
        .all()
    )
    best: tuple[float, CallSession] | None = None
    for row in candidates:
        anchor = row.started_at or row.created_at
        if anchor is None or not (lo <= anchor <= hi):
            continue
        delta = abs((anchor - started).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, row)
    return best[1] if best else None


def _has_entries(db: Session, session_id: str) -> bool:
    return (
        db.query(CallTranscriptEntry.id)
        .filter(CallTranscriptEntry.call_session_id == session_id)
        .first()
        is not None
    )


def _next_sequence(db: Session, session_id: str) -> int:
    from sqlalchemy import func

    current = (
        db.query(func.max(CallTranscriptEntry.sequence))
        .filter(CallTranscriptEntry.call_session_id == session_id)
        .scalar()
    )
    return int(current or 0) + 1


def _bind_company(
    db: Session, session: CallSession, externals: list[dict], *, hubspot_client=None
) -> str | None:
    """Set ``hubspot_company_id`` when the attendee domains name exactly one company."""
    if session.hubspot_company_id:
        return session.hubspot_company_id
    own = own_domains(session.ae_email)
    domains = sorted(
        {
            _domain(p["email"])
            for p in externals
            if is_company_matchable(_domain(p["email"]), own=own)
        }
    )
    if not domains:
        return None
    try:
        matches = resolve_domain_matches(
            domains, ae_email=session.ae_email, client=hubspot_client
        )
    except Exception as exc:  # noqa: BLE001 — a binding is a nicety, the call is the point
        logger.warning("[ccs-fathom] company match failed for %s: %s", session.id, exc)
        return None
    companies = {
        m.company.hubspot_company_id: m.company for m in matches.values() if m.company
    }
    if len(companies) != 1:
        # Zero: nobody owns these domains. Several: two clients on one call, or
        # a partner — a human picks on the wrap-up, never list order.
        return None
    company = next(iter(companies.values()))
    from rubik_core.tenancy.crm_visibility import visible_source_ids

    owned, _, _ = visible_source_ids({company.hubspot_company_id}, set(), set())
    if company.hubspot_company_id not in owned:
        return None
    session.hubspot_company_id = company.hubspot_company_id
    session.attrs = {
        **(session.attrs or {}),
        "company_name": company.name,
        CRM_PROVENANCE_ATTR: stamp_session_source(
            session.hubspot_company_id,
            session.hubspot_contact_id,
            session.hubspot_deal_id,
        ),
    }
    return company.hubspot_company_id


# ── The attach ───────────────────────────────────────────────────────────────


def attach_meeting(
    db: Session,
    meeting: dict,
    *,
    hubspot_client=None,
    report: dict | None = None,
) -> dict:
    """Turn one Fathom meeting into a captured call. Never raises.

    Returns ``{"outcome", "recording_id", "session_id", "entries_written",
    "company_id"}``. ``report`` is the sweep's bucket dict; one is minted when
    the caller has none, so the wrap-up-commitment and extraction helpers can
    count into it.
    """
    rid = recording_id(meeting)
    out = {
        "outcome": FAILED,
        "recording_id": rid,
        "session_id": None,
        "entries_written": 0,
        "company_id": None,
    }
    if not settings.CCS_FATHOM_ENABLED:
        return {**out, "outcome": SKIPPED_DISABLED}
    if not rid:
        return {**out, "outcome": SKIPPED_NO_ID}

    host = host_email(meeting)
    if not host or not _host_is_ours(db, host):
        logger.info(
            "[ccs-fathom] recording %s skipped: host %r is outside the company",
            rid,
            host,
        )
        return {**out, "outcome": FATHOM_SKIP_HOST_OUTSIDE}

    externals = external_attendees(meeting, host=host)
    if not externals:
        logger.info("[ccs-fathom] recording %s skipped: no external attendee", rid)
        return {**out, "outcome": SKIPPED_INTERNAL_ONLY}

    rows = transcript_rows(meeting)
    if not rows:
        logger.info("[ccs-fathom] recording %s skipped: no transcript text", rid)
        return {**out, "outcome": SKIPPED_NO_TRANSCRIPT}

    last_offset = max(
        (r["offset_ms"] for r in rows if r["offset_ms"] is not None), default=None
    )
    started, ended = _window(meeting, last_offset_ms=last_offset)
    anchor = ended or started
    max_age = timedelta(days=max(0, int(settings.CCS_FATHOM_MAX_AGE_DAYS)))
    if anchor is not None and max_age and datetime.utcnow() - anchor > max_age:
        # A backfill reaches back across the whole account listing. A call from
        # last quarter gets no session: the proposal window has passed, the AE
        # has moved on, and an extraction of it is a PRO call spent on history.
        logger.info(
            "[ccs-fathom] recording %s skipped: ended %s, older than %s days",
            rid,
            anchor.isoformat(),
            settings.CCS_FATHOM_MAX_AGE_DAYS,
        )
        return {**out, "outcome": SKIPPED_TOO_OLD}
    report = report if report is not None else _sweep._new_report(host, False)

    try:
        session = _by_recording(db, rid)
        created = False
        if session is None:
            session = _nearby_session(db, host, started)
            if session is None:
                session = CallSession(
                    id=uuid.uuid4().hex,
                    ae_email=host,
                    capture_consent="granted",
                    status="ended",
                    origin="fathom",
                    started_at=started,
                    ended_at=ended,
                    attrs={CRM_PROVENANCE_ATTR: stamp_session_source(None, None, None)},
                )
                db.add(session)
                created = True
        if not session_source_visible(session):
            # A nearby or previously attached row may contain CRM facts from a
            # pipeline this workspace no longer owns. Do not even link it.
            return out
        if session.fathom_recording_id != rid:
            session.fathom_recording_id = rid
            session.attrs = {
                **(session.attrs or {}),
                FATHOM_ATTRS_KEY: {
                    "recording_id": rid,
                    "title": (
                        meeting.get("title") or meeting.get("meeting_title") or ""
                    ).strip()
                    or None,
                    "url": meeting.get("share_url") or meeting.get("url"),
                    "attached_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            # A session the Meet path never stamped learns its window from Fathom.
            if session.started_at is None and started is not None:
                session.started_at = started
            if session.ended_at is None and ended is not None:
                session.ended_at = ended
            try:
                db.commit()
            except IntegrityError:
                # Two syncs (webhook retry + manual) raced on the UNIQUE; the
                # loser adopts the winner's row and continues as a replay.
                db.rollback()
                session = _by_recording(db, rid)
                if session is None:
                    raise
                if not session_source_visible(session):
                    return out
                created = False
        out["session_id"] = session.id
        report["sessions_created" if created else "sessions_matched"] += 1

        if (session.capture_consent or "granted") == "suppressed":
            db.commit()
            return {**out, "outcome": SUPPRESSED}

        if not session_source_visible(session):
            return out

        out["company_id"] = _bind_company(
            db, session, externals, hubspot_client=hubspot_client
        )
        db.commit()

        already = _has_entries(db, session.id)
        fathom_rows_exist = (
            db.query(CallTranscriptEntry.id)
            .filter(
                CallTranscriptEntry.call_session_id == session.id,
                CallTranscriptEntry.source == FATHOM_SOURCE,
            )
            .first()
            is not None
        )
        if already and not fathom_rows_exist:
            # The Meet path already captured this call. Link, never double it.
            logger.info(
                "[ccs-fathom] recording %s linked to session %s which already has a transcript",
                rid,
                session.id,
            )
            return {**out, "outcome": LINKED_EXISTING_TRANSCRIPT}

        seq = _next_sequence(db, session.id)
        payload: list[dict] = []
        now = datetime.utcnow()
        for position, row in enumerate(rows, start=1):
            payload.append(
                {
                    "id": uuid.uuid4().hex,
                    "call_session_id": session.id,
                    "source": FATHOM_SOURCE,
                    # Deterministic, so the table's UNIQUE turns a replay into a no-op.
                    "external_id": f"fathom:{rid}:{position}",
                    "participant_key": None,
                    "speaker_name": row["speaker_name"],
                    "speaker_email": row["speaker_email"],
                    "text": row["text"],
                    "language_code": meeting.get("language") or "en-US",
                    "start_offset_ms": row["offset_ms"],
                    "end_offset_ms": None,
                    "sequence": seq,
                    "created_at": now,
                }
            )
            seq += 1
        written = 0 if fathom_rows_exist else _ingest._insert_ignore(db, payload)
        db.commit()
        out["entries_written"] = written
        report["entries_written"] += written

        entries = (
            db.query(CallTranscriptEntry)
            .filter(CallTranscriptEntry.call_session_id == session.id)
            .order_by(CallTranscriptEntry.sequence)
            .all()
        )
        coverage = _ingest.compute_coverage(
            entries, started_at=session.started_at, ended_at=session.ended_at
        )
        session.attrs = {**(session.attrs or {}), _ingest.COVERAGE_KEY: coverage}
        if session.status in ("scheduled", "in_progress", "ended", "no_transcript"):
            session.status = "transcript_ready"
        db.commit()

        # The same three hand-offs the Meet sweep makes, in the same order.
        fresh = (
            session.ended_at is not None
            and datetime.utcnow() - session.ended_at <= WRAPUP_CLOCK_MAX_AGE
        )
        if fresh:
            _sweep._open_wrapup_commitment(db, session, report)
        else:
            logger.info(
                "[ccs-fathom] recording %s: no wrap-up clock for a call that ended %s",
                rid,
                session.ended_at.isoformat()
                if session.ended_at
                else "at an unknown time",
            )
        if (
            written
            or session.status == "transcript_ready"
            or _sweep._extraction_is_stale(db, session)
        ):
            _sweep._run_extraction(db, session, report, dry_run=False)

        outcome = ATTACHED if written else REPLAYED
        logger.info(
            "[ccs-fathom] recording %s → session %s (%s): %s entries, company=%s, status=%s",
            rid,
            session.id,
            "created" if created else "matched",
            written,
            out["company_id"],
            session.status,
        )
        return {**out, "outcome": outcome}
    except Exception:  # noqa: BLE001 — the sync must keep going; see module docstring
        db.rollback()
        logger.exception("[ccs-fathom] recording %s could not be attached", rid)
        return {**out, "outcome": FAILED}


def attach_meetings(db: Session, meetings: list[dict], *, hubspot_client=None) -> dict:
    """Attach every meeting in a Fathom listing; returns an outcome histogram."""
    summary: dict[str, int] = {}
    for meeting in meetings or []:
        result = attach_meeting(db, meeting, hubspot_client=hubspot_client)
        summary[result["outcome"]] = summary.get(result["outcome"], 0) + 1
    return summary
