"""Find and load the meetings a person attended, as cite-able transcripts.

ACCESS BOUNDARY: the ``meeting_participants`` table, exactly as
``search_past_meetings`` uses it. Every lookup here starts from the rows for
the signed-in person, so a meeting they were not part of cannot be named,
listed or read — execs and admins included.

The text is the stored copy of the Google transcript Doc (the asset's
``source_path``, written by ``transcript_sync``). When that copy cannot be
read, the Doc itself is exported from Drive AS the person, so Drive's own
sharing still applies. A Call Capture call swaps in its stored per-line
entries, which carry exact times.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from rubik_core.config import settings
from rubik_core.meeting_skills.normalize import (
    SOURCE_CALL_CAPTURE,
    NormalizedMeeting,
    normalize,
    with_exact_entries,
)

logger = logging.getLogger(__name__)

_DOC_ID = re.compile(r"^[0-9a-f]{64}$")
_RECENT = re.compile(
    r"^\s*(?:my\s+)?(?:last|latest|most\s+recent|previous|today'?s?|recent)?"
    r"\s*(?:meeting|call|one)?\s*$",
    re.IGNORECASE,
)
_GOOGLE_DOC = "application/vnd.google-apps.document"


@dataclass(frozen=True)
class MeetingRef:
    doc_id: str
    title: str
    when: datetime | None
    source_path: str | None


def _session():
    from rubik_core.database import db as _db

    return _db.SessionLocal()


def _ref(asset) -> MeetingRef:
    return MeetingRef(
        doc_id=asset.doc_id,
        title=asset.original_filename or "Untitled meeting",
        when=asset.created_at,
        source_path=asset.source_path,
    )


def _attended(db, user_email: str):
    """Approved transcripts ``user_email`` attended, newest first (a query)."""
    from rubik_core.database.models import Asset, MeetingParticipant

    return (
        db.query(Asset)
        .join(MeetingParticipant, MeetingParticipant.doc_id == Asset.doc_id)
        .filter(MeetingParticipant.participant_email == user_email.strip().lower())
        .filter(Asset.status == "approved")
        .order_by(Asset.created_at.desc())
    )


def _title_tokens(text: str) -> set[str]:
    stop = {
        "the",
        "a",
        "an",
        "my",
        "our",
        "with",
        "and",
        "meeting",
        "call",
        "sync",
        "of",
        "for",
    }
    return {
        t
        for t in re.split(r"[^a-z0-9]+", (text or "").lower())
        if len(t) > 1 and t not in stop
    }


def find_meetings(
    user_email: str,
    meeting: str = "",
    *,
    days: int = 0,
    limit: int = 1,
) -> list[MeetingRef]:
    """The meetings ``meeting`` names among those ``user_email`` attended.

    * a transcript id → that meeting;
    * ``days`` > 0 → every meeting in the last ``days`` days, newest first,
      narrowed to titles sharing a word with ``meeting`` when one is given;
    * blank or "my last meeting" → the most recent one;
    * otherwise the best title match, newest first.
    """
    if not user_email:
        return []
    limit = max(1, min(limit, settings.MEETING_SKILLS_MAX_MEETINGS))
    query_text = (meeting or "").strip()
    with _session() as db:
        attended = _attended(db, user_email)
        if _DOC_ID.match(query_text.lower()):
            from rubik_core.database.models import Asset

            asset = attended.filter(Asset.doc_id == query_text.lower()).first()
            return [_ref(asset)] if asset else []
        if days and days > 0:
            from rubik_core.database.models import Asset

            cutoff = datetime.utcnow() - timedelta(days=days)
            rows = attended.filter(Asset.created_at >= cutoff).limit(200).all()
            wanted = _title_tokens(query_text)
            if wanted:
                rows = [
                    a for a in rows if wanted & _title_tokens(a.original_filename or "")
                ]
            return [_ref(a) for a in rows[:limit]]
        if not query_text or _RECENT.match(query_text):
            return [_ref(a) for a in attended.limit(limit).all()]
        wanted = _title_tokens(query_text)
        if not wanted:
            return []
        scored = []
        for asset in attended.limit(500).all():
            have = _title_tokens(asset.original_filename or "")
            overlap = len(wanted & have)
            if overlap:
                scored.append(
                    (overlap / len(wanted), asset.created_at or datetime.min, asset)
                )
        if not scored:
            return []
        best = max(s[0] for s in scored)
        if best < 0.5:
            return []
        keep = sorted(
            (s for s in scored if s[0] >= best), key=lambda s: s[1], reverse=True
        )
        return [_ref(a) for _, _, a in keep[:limit]]


def _participants(db, doc_id: str) -> tuple[list[str], str | None]:
    from rubik_core.database.models import MeetingParticipant

    rows = (
        db.query(MeetingParticipant).filter(MeetingParticipant.doc_id == doc_id).all()
    )
    meeting_id = rows[0].meeting_id if rows else None
    return [r.participant_email for r in rows], meeting_id


def _stored_text(source_path: str | None, max_chars: int) -> str:
    from rubik_core.adk_agent.tools import _read_transcript_text

    return _read_transcript_text(source_path, max_chars=max_chars)


def _drive_text(user_email: str, file_id: str, max_chars: int) -> str:
    """The transcript Doc exported from Drive as ``user_email``, or ""."""
    try:
        from rubik_core.tools.drive import GoogleDriveClient

        drive = GoogleDriveClient(user_email=user_email)
        if not drive.service:
            return ""
        return (
            drive.export_document_text(
                file_id, mime_type=_GOOGLE_DOC, max_chars=max_chars
            )
            or ""
        )
    except Exception:  # noqa: BLE001 — a Drive miss is "no text", not a crash
        logger.info(
            "[meeting-skills] Drive export failed for %s", file_id, exc_info=True
        )
        return ""


def _exact_entries(db, conference_record: str) -> list[tuple[str, str, int | None]]:
    from rubik_core.database.call_capture import CallSession, CallTranscriptEntry

    session = (
        db.query(CallSession)
        .filter(CallSession.conference_record == conference_record)
        .first()
    )
    if session is None:
        return []
    entries = (
        db.query(CallTranscriptEntry)
        .filter(CallTranscriptEntry.call_session_id == session.id)
        .order_by(CallTranscriptEntry.sequence)
        .all()
    )
    return [(e.speaker_name, e.text, e.start_offset_ms) for e in entries]


def load_meeting(user_email: str, ref: MeetingRef) -> NormalizedMeeting | None:
    """``ref`` as a normalized meeting, or None when ``user_email`` may not read
    it or no text can be found."""
    max_chars = settings.MEETING_SKILLS_MAX_TRANSCRIPT_CHARS
    with _session() as db:
        attendees, meeting_id = _participants(db, ref.doc_id)
        if user_email.strip().lower() not in attendees:
            # The boundary, re-checked at read time: a ref is just an id.
            return None
        text = _stored_text(ref.source_path, max_chars)
        if not text and meeting_id and not meeting_id.startswith("conferenceRecords/"):
            # The stored copy is missing: read the Google Doc itself, as them.
            text = _drive_text(user_email, meeting_id, max_chars)
        if not text:
            return None
        meeting = normalize(
            doc_id=ref.doc_id,
            title=ref.title,
            text=text,
            attendees=attendees,
            started_at=ref.when,
            truncated=len(text) >= max_chars,
        )
        if meeting.source == SOURCE_CALL_CAPTURE and meeting.conference_record:
            try:
                entries = _exact_entries(db, meeting.conference_record)
            except Exception:  # noqa: BLE001 — the flattened text still works
                logger.info("[meeting-skills] exact entries unavailable", exc_info=True)
                entries = []
            if entries:
                meeting = with_exact_entries(meeting, entries)
    return meeting
