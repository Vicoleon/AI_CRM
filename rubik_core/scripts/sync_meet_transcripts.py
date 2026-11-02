import hashlib
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rubik_core.adapters.chroma import ChromaAdapter
from rubik_core.adapters.vertex_rag import VertexRagAdapter
from rubik_core.auth.workspace import WorkspaceService
from rubik_core.config import AppMode, settings
from rubik_core.dam.registry import AssetMetadata, DAMRegistry
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import User
from rubik_core.database.repository import (
    SQLMeetingParticipantsRepository,
    SQLUserRegistry,
)
from rubik_core.ingestion.processor import IngestionProcessor
from rubik_core.tenancy.corpora import corpus_id
from rubik_core.tenancy.storage import local_data_dir, object_name
from rubik_core.tools.drive import GoogleDriveClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_processor():
    if settings.APP_MODE == AppMode.GCP:
        adapter = VertexRagAdapter()
    else:
        adapter = ChromaAdapter()
    return IngestionProcessor(adapter)


def run_sync(user_emails: list[str] = None, days_ago: int = 7):
    """
    Synchronizes Google Meet transcripts for the specified users (or all users if None).
    Capped to files modified within the last `days_ago` days.
    """
    processor = get_processor()
    db = SessionLocal()
    dam_registry = DAMRegistry()
    participants_repo = SQLMeetingParticipantsRepository()
    user_registry = SQLUserRegistry()
    all_app_users = user_registry.list_users()

    try:
        # 1. Determine which users to sync
        if not user_emails:
            # Global sweep: fetch all domain users via Admin SDK (if DWD active)
            from rubik_core.tools._workspace_auth import workspace_dwd_subject

            admin_email = workspace_dwd_subject()
            ws = WorkspaceService(user_registry)
            creds = ws._get_credentials(
                admin_email,
                ["https://www.googleapis.com/auth/admin.directory.user.readonly"],
            )

            if creds:
                try:
                    from googleapiclient.discovery import build

                    service = build(
                        "admin",
                        "directory_v1",
                        credentials=creds,
                        cache_discovery=False,
                    )
                    results = (
                        service.users()
                        .list(customer="my_customer", maxResults=500)
                        .execute()
                    )
                    workspace_users = results.get("users", [])
                    user_emails = []
                    for u in workspace_users:
                        email = u.get("primaryEmail")
                        if email:
                            user_emails.append(email)
                            # Auto-register if in PROD
                            if settings.ENVIRONMENT.upper() == "PROD":
                                existing_user = user_registry.get_user(email)
                                if not existing_user:
                                    logger.info(
                                        f"Auto-registering domain user: {email}"
                                    )
                                    user_registry.register_user(
                                        email=email,
                                        name=u.get("name", {}).get(
                                            "fullName", "Workspace User"
                                        ),
                                        id=u.get("id", "workspace-id"),
                                        roles=["user"],
                                        picture=u.get("thumbnailPhotoUrl"),
                                    )
                    logger.info(
                        f"Admin SDK found {len(user_emails)} domain users for global sweep."
                    )
                except Exception as e:
                    logger.error(f"Failed to list domain users via Admin SDK: {e}")

            if not user_emails:
                # Fallback to local users
                local_users = (
                    db.query(User).filter(User.is_platform_support.is_(False)).all()
                )
                user_emails = [u.email for u in local_users]

        if not user_emails:
            logger.info("No users found to synchronize.")
            return

        # 2. Iterate and Sync
        for user_email in user_emails:
            logger.info(
                f"Starting Google Drive sync for: {user_email} (Cap: {days_ago} days)"
            )

            try:
                drive_client = GoogleDriveClient(user_email=user_email)
                if not drive_client.service:
                    continue

                transcripts = []
                # Check Meet Recordings folder
                folder_id = drive_client.get_meet_recordings_folder_id()
                if folder_id:
                    transcripts.extend(
                        drive_client.list_transcripts(
                            folder_id, limit=50, days_ago=days_ago
                        )
                    )

                # Search for 'Notes by Gemini' pattern
                pattern_matches = drive_client.search_transcripts_by_pattern(
                    "Notes by Gemini", limit=50, days_ago=days_ago
                )
                transcripts.extend(pattern_matches)

                # Deduplicate by ID
                seen_ids = set()
                unique_transcripts = []
                for t in transcripts:
                    if t["id"] not in seen_ids:
                        unique_transcripts.append(t)
                        seen_ids.add(t["id"])

                logger.info(
                    f"Found {len(unique_transcripts)} new/recent transcripts for {user_email}"
                )

                # Target Corpus (Vertex AI)
                target_corpus_id = None
                if settings.APP_MODE == AppMode.GCP:
                    target_corpus_id = corpus_id("meetings", create=True)

                for transcript in unique_transcripts:
                    file_id = transcript.get("id")
                    filename = transcript.get("name", "Unknown Meet Transcript")
                    doc_id = hashlib.sha256(file_id.encode()).hexdigest()

                    # Skip if already in DAM (and successful)
                    existing = dam_registry.get_asset(doc_id)
                    if existing and existing.status == "approved":
                        continue

                    logger.info(f"Ingesting transcript: {filename} ({file_id})")

                    try:
                        text = drive_client.export_document_text(
                            file_id, mime_type=transcript.get("mimeType")
                        )
                        if not text:
                            continue

                        # Extract participants for ACL
                        allowed_emails = {user_email.lower()}
                        regex_emails = (
                            drive_client.extract_participants_from_transcript(text)
                        )
                        allowed_emails.update([e.lower() for e in regex_emails])

                        # Cross-reference with known users
                        header_text = text[:3000].lower()
                        for u in all_app_users:
                            if u.name and u.name.lower() in header_text:
                                allowed_emails.add(u.email.lower())

                        allowed_emails_list = list(allowed_emails)
                        if settings.APP_MODE == AppMode.GCP:
                            source_uri = f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', f'{doc_id}.txt')}"
                        else:
                            stored = local_data_dir() / "rag" / f"{doc_id}.txt"
                            stored.parent.mkdir(parents=True, exist_ok=True)
                            stored.write_text(text)
                            source_uri = stored.as_uri()

                        # Register in DAM Registry
                        now = datetime.now(timezone.utc)
                        asset = AssetMetadata(
                            doc_id=doc_id,
                            owner=user_email,
                            topic="Meeting Transcripts",
                            # No department: transcripts are participant-gated
                            # (see has_document_access + search_past_meetings),
                            # not department-scoped. "Internal" was a phantom
                            # label.
                            department=None,
                            status="approved",
                            is_company_wide=False,
                            source_path=source_uri,
                            allowed_emails=allowed_emails_list,
                            original_filename=filename,
                            created_at=now,
                            updated_at=now,
                        )
                        dam_registry.register_asset(asset)

                        # Authoritative participant ACL — see transcript_sync.py
                        # and the MeetingParticipant docstring. Chat retrieval
                        # gates on this table, not Asset.allowed_emails, so the
                        # transcript is unqueryable without it.
                        participants_repo.upsert_participants(
                            doc_id, meeting_id=file_id, emails=allowed_emails_list
                        )

                        # Process File
                        with tempfile.TemporaryDirectory() as tmp_dir:
                            tmp_file = Path(tmp_dir) / f"{doc_id}.txt"
                            with open(tmp_file, "w") as f:
                                f.write(text)

                            # Format allowed_emails for retrieval filters (|email1|email2|)
                            allowed_emails_str = (
                                "|" + "|".join(allowed_emails_list) + "|"
                            )

                            manual_metadata = {
                                "owner": user_email,
                                "topic": "Meeting Transcripts",
                                "status": "approved",
                                "is_company_wide": False,
                                "allowed_emails": allowed_emails_str,
                                "original_filename": filename,
                            }

                            res = processor.process_file(
                                tmp_file,
                                manual_metadata=manual_metadata,
                                target_corpus_id=target_corpus_id,
                            )

                            if not res.get("result") or not res.get("result").success:
                                logger.error(f"Failed to ingest transcript {filename}")
                                dam_registry.update_asset(doc_id, {"status": "failed"})

                    except Exception as e:
                        logger.error(f"Error processing transcript {file_id}: {e}")
                        dam_registry.update_asset(doc_id, {"status": "failed"})

            except Exception as e:
                logger.error(f"Drive sync failed for {user_email}: {e}")

            # Update user cooldown
            u_db = db.query(User).filter(User.email == user_email).first()
            if u_db:
                u_db.last_ingest = datetime.now(timezone.utc)
                db.commit()

    finally:
        db.close()


if __name__ == "__main__":
    import argparse

    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    parser = argparse.ArgumentParser(
        description="Sync Google Meet transcripts to RAG corpus."
    )
    parser.add_argument(
        "--users",
        nargs="+",
        help="Specific user emails to sync. If omitted, runs for all domain users.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to look back for modified transcripts. Default is 7.",
    )
    args = parser.parse_args()

    run_sync(user_emails=args.users, days_ago=args.days)
