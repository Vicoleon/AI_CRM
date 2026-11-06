"""Meeting transcript management API.

Surfaces the transcripts a user has access to (as owner OR participant) and
lets the owner / admin delete a transcript out of the corpus and ACL.

Access boundary: list/delete decisions go through the meeting_participants
junction table. Asset.allowed_emails is a denormalized cache used only by the
vector store metadata filter — never trusted on its own.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.dam.audit import log_action
from rubik_core.dam.registry import DAMRegistry
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import Asset, MeetingParticipant
from rubik_core.database.repository import SQLMeetingParticipantsRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/transcripts", tags=["transcripts"])

dam_registry = DAMRegistry()


def _ui_status(asset_status: str) -> str:
    if asset_status == "approved":
        return "Indexed"
    if asset_status == "deleted":
        return "Deleted"
    if asset_status == "failed":
        return "Failed"
    return "Pending"


class TranscriptRow(BaseModel):
    doc_id: str
    original_filename: str | None
    owner: str
    status: str
    is_owner: bool
    participant_count: int
    created_at: str
    updated_at: str


@router.get("/", response_model=dict[str, Any])
def list_transcripts(user: User = Depends(get_current_user)):
    """Return every transcript the caller may query, with ingestion status.

    Includes transcripts they own (as host) and meetings they attended. Only
    admins see everything — execs (org-wide or otherwise) are scoped to their
    own/attended meetings like any other user. Soft-deleted transcripts
    (status=deleted) are returned so the user sees the audit trail in the UI but
    cannot query them.
    """
    db: Session = SessionLocal()
    try:
        if user.is_admin:
            # Admins (and only admins) see every transcript, for moderation.
            assets = (
                db.query(Asset)
                .filter(Asset.topic == "Meeting Transcripts")
                .order_by(Asset.created_at.desc())
                .all()
            )
        else:
            participant_doc_ids = (
                db.query(MeetingParticipant.doc_id)
                .filter(MeetingParticipant.participant_email == user.email.lower())
                .subquery()
            )
            assets = (
                db.query(Asset)
                .filter(Asset.topic == "Meeting Transcripts")
                .filter(
                    (Asset.owner_email == user.email)
                    | (Asset.doc_id.in_(participant_doc_ids))
                )
                .order_by(Asset.created_at.desc())
                .all()
            )

        rows = []
        for a in assets:
            count = (
                db.query(MeetingParticipant)
                .filter(MeetingParticipant.doc_id == a.doc_id)
                .count()
            )
            rows.append(
                TranscriptRow(
                    doc_id=a.doc_id,
                    original_filename=a.original_filename,
                    owner=a.owner_email,
                    status=_ui_status(a.status),
                    is_owner=(a.owner_email == user.email),
                    participant_count=count,
                    created_at=a.created_at.isoformat() if a.created_at else "",
                    updated_at=a.updated_at.isoformat() if a.updated_at else "",
                ).model_dump()
            )
        return {"transcripts": rows, "total": len(rows)}
    finally:
        db.close()


@router.delete("/{meeting_id}")
def delete_transcript(meeting_id: str, user: User = Depends(get_current_user)):
    """Remove a transcript from the corpus and the participants table.

    Only the meeting owner (host) or an admin may delete. Other participants
    cannot — that would let one attendee strip access from all the others. The
    asset row is kept with status=deleted as a tombstone so transcript_sync
    will not re-ingest it next cycle.
    """
    meeting_id = meeting_id.lstrip("/")
    asset = dam_registry.get_asset(meeting_id)
    if not asset or asset.topic != "Meeting Transcripts":
        raise HTTPException(status_code=404, detail="Transcript not found")

    # ── ACCESS BOUNDARY ──────────────────────────────────────────────────
    # Only the owner or an admin can delete. We log every denial because a
    # participant attempting to delete is a meaningful access-violation signal.
    if asset.owner != user.email and not user.is_admin:
        logger.warning(
            "Transcript delete denied: actor=%s owner=%s doc_id=%s",
            user.email,
            asset.owner,
            meeting_id,
        )
        log_action(
            user.email,
            "transcript_delete",
            meeting_id,
            "denied",
            {"reason": "not_owner_or_admin"},
        )
        raise HTTPException(
            status_code=403,
            detail="Only the meeting host or an admin can delete this transcript",
        )
    # ─────────────────────────────────────────────────────────────────────

    rag_file_removed = None
    try:
        from rubik_core.config import AppMode, settings

        meetings_corpus = None
        if settings.APP_MODE == AppMode.GCP:
            from rubik_core.adapters.vertex_rag import VertexRagAdapter
            from rubik_core.tenancy.corpora import corpus_id
            from rubik_core.tenancy.storage import assert_own_uri

            assert_own_uri(asset.source_path)
            # SOC 2 H-15 — transcripts are ingested into company_meetings_corpus,
            # NOT the default corpus. Delete the one matching ragFile from THAT
            # corpus so the transcript's chunks actually stop being retrievable;
            # other files in the corpus are untouched.
            meetings_corpus = corpus_id("meetings")
            if meetings_corpus is None:
                raise RuntimeError("Meeting corpus is not bound to this workspace")
            adapter = VertexRagAdapter()
        else:
            from rubik_core.adapters.chroma import ChromaAdapter

            adapter = ChromaAdapter()
        if hasattr(adapter, "delete_document"):
            if meetings_corpus is not None:
                rag_file_removed = adapter.delete_document(
                    meeting_id,
                    source_path=asset.source_path,
                    corpus_id=meetings_corpus,
                )
            else:
                rag_file_removed = adapter.delete_document(
                    meeting_id, source_path=asset.source_path
                )
    except Exception as e:
        log_action(
            user.email, "transcript_delete", meeting_id, "failure", {"error": str(e)}
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to remove from vector store: {e}"
        )

    participants_repo = SQLMeetingParticipantsRepository()
    removed = participants_repo.delete_for_doc(meeting_id)

    dam_registry.update_asset(meeting_id, {"status": "deleted", "allowed_emails": []})

    log_action(
        user.email,
        "transcript_delete",
        meeting_id,
        "success",
        {"participants_removed": removed, "rag_file_removed": rag_file_removed},
    )
    return {
        "message": "Transcript deleted",
        "participants_removed": removed,
        "rag_file_removed": rag_file_removed,
    }
