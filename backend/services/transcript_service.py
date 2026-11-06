"""TranscriptService — signal-oriented access to meeting transcripts.

Mirrors `CalendarService` / `GmailService`: gives the UC-17 signal layer a
single call to list recent transcripts the rep participated in and to read
their raw text (capped) without each consumer touching the DB or GCS directly.

The authoritative ACL is the `meeting_participants` junction table — we query
through `SQLMeetingParticipantsRepository.list_doc_ids_for_user` and join to
`Asset` rows (topic="Meeting Transcripts").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Cap how much of a transcript we read into memory for keyword scanning.
# A 2-hour Meet transcript can run >200KB; 50KB covers the opening + most
# action-item summaries Fathom/Gemini tend to surface near the top.
MAX_TRANSCRIPT_BYTES = 50_000


@dataclass
class TranscriptRecord:
    doc_id: str
    meeting_id: str
    owner_email: str
    original_filename: str | None
    source_path: str | None
    created_at: datetime
    participants: list[str]
    is_owner: bool


class TranscriptService:
    def __init__(
        self,
        user_email: str,
        *,
        session_factory: Any | None = None,
        gcs_client: Any | None = None,
    ):
        self.user_email = (user_email or "").lower()
        self._session_factory = session_factory
        self._gcs_client = gcs_client

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from rubik_core.database.db import SessionLocal

        return SessionLocal()

    def list_transcripts_for_signals(
        self,
        *,
        since: datetime,
    ) -> list[TranscriptRecord]:
        """Transcripts created at or after `since` that the rep participated in.

        Joins `meeting_participants` (authoritative ACL) to `Asset` rows with
        topic="Meeting Transcripts". Owner-only transcripts are surfaced via
        `Asset.owner_email == user_email` as a fallback when no participant
        row exists (e.g. a personal recording with no attendees).
        """
        from rubik_core.database.models import Asset, MeetingParticipant

        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        db = self._session()
        try:
            participant_doc_ids = (
                db.query(MeetingParticipant.doc_id)
                .filter(MeetingParticipant.participant_email == self.user_email)
                .subquery()
            )
            assets = (
                db.query(Asset)
                .filter(Asset.topic == "Meeting Transcripts")
                .filter(Asset.status == "approved")
                .filter(Asset.created_at >= since.replace(tzinfo=None))
                .filter(
                    (Asset.owner_email == self.user_email)
                    | (Asset.doc_id.in_(participant_doc_ids))
                )
                .order_by(Asset.created_at.desc())
                .all()
            )

            records: list[TranscriptRecord] = []
            for a in assets:
                participants = [
                    r[0]
                    for r in db.query(MeetingParticipant.participant_email)
                    .filter(MeetingParticipant.doc_id == a.doc_id)
                    .all()
                ]
                meeting_id = ""
                first = (
                    db.query(MeetingParticipant.meeting_id)
                    .filter(MeetingParticipant.doc_id == a.doc_id)
                    .first()
                )
                if first:
                    meeting_id = first[0] or ""
                created = a.created_at or datetime.now(timezone.utc)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                records.append(
                    TranscriptRecord(
                        doc_id=a.doc_id,
                        meeting_id=meeting_id,
                        owner_email=a.owner_email,
                        original_filename=a.original_filename,
                        source_path=a.source_path,
                        created_at=created,
                        participants=participants,
                        is_owner=(a.owner_email or "").lower() == self.user_email,
                    )
                )
            return records
        finally:
            db.close()

    def read_transcript_text(
        self,
        record: TranscriptRecord,
        *,
        max_bytes: int = MAX_TRANSCRIPT_BYTES,
    ) -> str:
        """Return up to `max_bytes` of decoded text from the transcript's GCS
        object. Returns "" if unavailable; callers must tolerate that."""
        # One implementation of "read a transcript out of GCS", shared with the
        # L10 summary email (which reads the same objects from a Cloud Run Job,
        # where this service's per-user ACL scoping doesn't apply).
        from rubik_core.eos.l10_recording import read_transcript_text

        return read_transcript_text(
            record.source_path,
            max_bytes=max_bytes,
            gcs_client=self._gcs_client,
        )
