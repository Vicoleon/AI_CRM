"""Background sync of Google Meet transcripts → RAG corpus.

Designed to run every 15 minutes via Cloud Scheduler. For each domain user,
it finds new Meet/Gemini transcripts in their Drive, ingests the new ones into
the shared meeting corpus, and writes one row per attendee into the
``meeting_participants`` junction table.

Idempotency:
- A transcript is keyed by Google Drive file_id (sha256 → doc_id). If the asset
  already exists with status ``approved`` or ``deleted``, we skip it. ``deleted``
  is a tombstone — once a user removes a transcript, the sync must never
  re-ingest it.

Run manually:
    python transcript_sync.py --users alice@acme.example bob@acme.example
    python transcript_sync.py --days 1
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rubik_core.adapters.chroma import ChromaAdapter
from rubik_core.adapters.vertex_rag import VertexRagAdapter
from rubik_core.auth.workspace import WorkspaceService
from rubik_core.automation import automations_enabled, require_automations_enabled
from rubik_core.config import AppMode, settings
from rubik_core.dam.registry import AssetMetadata, DAMRegistry
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import Asset, User
from rubik_core.database.repository import (
    SQLMeetingParticipantsRepository,
    SQLUserRegistry,
    is_placeholder_email,
)
from rubik_core.eos.l10_recording import (
    MIN_TRANSCRIPT_CHARS,
    title_hint_from_filename,
)
from rubik_core.ingestion.processor import IngestionProcessor
from rubik_core.tenancy.corpora import corpus_id
from rubik_core.tenancy.storage import local_data_dir, object_name
from rubik_core.tools.calendar import GoogleCalendarClient
from rubik_core.tools.drive import GoogleDriveClient
from rubik_core.tools.meet import GoogleMeetClient

# "Notes by Gemini" transcript files are named "<title> - <date> - Notes by
# Gemini"; strip the trailing boilerplate + date so we can match the calendar
# event by its real title. The implementation lives in
# ``rubik_core.eos.l10_recording`` because the L10 summary email matches a
# transcript to its meeting the same way, and the two must never drift apart.
_title_hint_from_filename = title_hint_from_filename


def _to_datetime(value) -> datetime | None:
    """Coerce an RFC3339 string / datetime into a datetime (None on failure)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _calendar_attendee_emails(calendar, filename, created_at) -> list[str]:
    """Authoritative attendee emails for a transcript, via the calendar invite.

    Best-effort: returns ``[]`` when no calendar client is available or nothing
    matches, so the caller can always union the result in without guarding.
    """
    if calendar is None:
        return []
    try:
        return calendar.find_meeting_attendee_emails(
            _title_hint_from_filename(filename),
            around_time=_to_datetime(created_at),
        )
    except Exception:
        logger.exception("Calendar attendee lookup failed for %r", filename)
        return []


def _calendar_client_for(user_email: str):
    """Build a calendar client for ``user_email``; None if creds unavailable."""
    try:
        cal = GoogleCalendarClient(user_email=user_email)
        return cal if cal.service else None
    except Exception:
        logger.exception("Could not build calendar client for %s", user_email)
        return None


def _meet_client_for(user_email: str):
    """Build a Google Meet client for ``user_email``; None if unavailable."""
    try:
        m = GoogleMeetClient(user_email=user_email)
        return m if m.service else None
    except Exception:
        logger.exception("Could not build Meet client for %s", user_email)
        return None


def _resolve_joiner_emails(joiners: list[dict], all_app_users) -> set[str]:
    """Map Meet signed-in participants to the company's app-user emails.

    Meet gives ``users/{id}`` + displayName, not an email. Match the id against
    ``User.id`` (the Google ``sub`` stored at login) first, then fall back to a
    case-insensitive display-name match. External / non-app participants don't
    resolve — correctly, they have no Cubie account to grant.
    """
    by_id = {u.id: u.email.lower() for u in all_app_users if getattr(u, "id", None)}
    by_name = {
        (u.name or "").strip().lower(): u.email.lower()
        for u in all_app_users
        if getattr(u, "name", None)
    }
    out: set[str] = set()
    for j in joiners:
        uid = (j.get("user_id") or "").rsplit("/", 1)[-1]
        if uid and uid in by_id:
            out.add(by_id[uid])
            continue
        nm = (j.get("display_name") or "").strip().lower()
        if nm and nm in by_name:
            out.add(by_name[nm])
    return out


def _attending_emails(
    calendar, meet, filename, created_at, all_app_users
) -> set[str] | None:
    """Company emails of people who ACTUALLY JOINED the meeting, or ``None`` when
    attendance can't be determined (caller falls back to the invite list).

    Correlates the transcript to its Meet conference via the calendar event's
    meeting code, reads the conference's signed-in participants, and resolves them
    to app-user emails. Returns ``None`` (not empty) on any miss — no calendar
    event, no Meet code, no participants, or nobody resolved — so the caller keeps
    the invite-based behavior rather than granting no one.
    """
    if calendar is None or meet is None:
        return None
    event = calendar.find_meeting_event(
        _title_hint_from_filename(filename), around_time=_to_datetime(created_at)
    )
    if not event:
        return None
    code = GoogleCalendarClient.extract_meeting_code(event)
    if not code:
        return None
    joiners = meet.list_signed_in_participants(
        code, around_time=_to_datetime(created_at)
    )
    if not joiners:
        return None
    return _resolve_joiner_emails(joiners, all_app_users) or None


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("transcript_sync")


def _build_processor() -> IngestionProcessor:
    if settings.APP_MODE == AppMode.GCP:
        return IngestionProcessor(VertexRagAdapter())
    return IngestionProcessor(ChromaAdapter())


def _resolve_meetings_corpus_id() -> str | None:
    if settings.APP_MODE != AppMode.GCP:
        return None
    return corpus_id("meetings", create=True)


def _resolve_user_emails(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    db = SessionLocal()
    try:
        registry = SQLUserRegistry()
        from rubik_core.tools._workspace_auth import workspace_dwd_subject

        admin_email = workspace_dwd_subject()
        ws = WorkspaceService(registry)
        creds = ws._get_credentials(
            admin_email,
            ["https://www.googleapis.com/auth/admin.directory.user.readonly"],
        )
        if creds:
            try:
                from googleapiclient.discovery import build

                service = build(
                    "admin", "directory_v1", credentials=creds, cache_discovery=False
                )
                results = (
                    service.users()
                    .list(customer="my_customer", maxResults=500)
                    .execute()
                )
                emails = []
                for u in results.get("users", []):
                    email = u.get("primaryEmail")
                    if email:
                        emails.append(email)
                        if (
                            settings.ENVIRONMENT.upper() == "PROD"
                            and not registry.get_user(email)
                        ):
                            registry.register_user(
                                email=email,
                                name=u.get("name", {}).get(
                                    "fullName", "Workspace User"
                                ),
                                id=u.get("id", "workspace-id"),
                                roles=["user"],
                                picture=u.get("thumbnailPhotoUrl"),
                            )
                if emails:
                    return emails
            except Exception as e:
                logger.warning(
                    "Admin SDK directory listing failed, falling back to local users: %s",
                    e,
                )
        return [
            u.email
            for u in db.query(User).filter(User.is_platform_support.is_(False)).all()
        ]
    finally:
        db.close()


def _sync_user(
    user_email: str,
    days_ago: int,
    processor: IngestionProcessor,
    target_corpus_id: str | None,
    dam_registry: DAMRegistry,
    participants_repo: SQLMeetingParticipantsRepository,
    all_app_users,
) -> int:
    drive = GoogleDriveClient(user_email=user_email)
    if not drive.service:
        logger.info("Drive client unavailable for %s, skipping.", user_email)
        return 0

    transcripts: list[dict] = []
    folder_id = drive.get_meet_recordings_folder_id()
    if folder_id:
        transcripts.extend(
            drive.list_transcripts(folder_id, limit=50, days_ago=days_ago)
        )
    transcripts.extend(
        drive.search_transcripts_by_pattern(
            "Notes by Gemini", limit=50, days_ago=days_ago
        )
    )

    seen_ids: set[str] = set()
    unique: list[dict] = []
    for t in transcripts:
        tid = t.get("id")
        if tid and tid not in seen_ids:
            seen_ids.add(tid)
            unique.append(t)

    logger.info("Found %d candidate transcripts for %s", len(unique), user_email)
    ingested = 0

    # Authoritative attendee source. Built once per user (DWD impersonation is
    # not free) and only when there's something to ingest.
    calendar = _calendar_client_for(user_email) if unique else None
    meet = _meet_client_for(user_email) if unique else None

    for transcript in unique:
        file_id = transcript.get("id")
        filename = transcript.get("name", "Unknown Meet Transcript")
        if not file_id:
            continue
        doc_id = hashlib.sha256(file_id.encode()).hexdigest()

        existing = dam_registry.get_asset(doc_id)
        if existing and existing.status in ("approved", "deleted"):
            # 'deleted' tombstones must NOT be re-ingested — the user removed access on purpose.
            continue

        try:
            text = drive.export_document_text(
                file_id, mime_type=transcript.get("mimeType")
            )
            # ── NOT-READY-YET GATE ──────────────────────────────────────────
            # Google creates the "Notes by Gemini" doc as an EMPTY SHELL the
            # moment the meeting ends, then fills it in over the next few
            # minutes (observed: 3 to 46). This sync runs every 15 minutes, so
            # a pass regularly lands in that gap. Skipping is the correct
            # response — no asset is registered, so the next pass re-exports it
            # once Gemini has finished.
            #
            # An empty Google Doc exports as a bare UTF-8 BOM, and `"\ufeff"` is
            # TRUTHY, so the original `if not text` never fired for exactly the
            # case it was written to catch. The shell was ingested, marked
            # `approved`, and skipped forever after by the status check above —
            # leaving a 3-byte transcript that is invisible in Chat and unusable
            # by the L10 recap email. Up to a third of a week's transcripts can
            # be poisoned this way, including an L10 whose real notes run to
            # 17,000 characters in Drive.
            #
            # Hence: strip the BOM, then require real content before ingesting.
            text = (text or "").lstrip("\ufeff")
            if len(text.strip()) < MIN_TRANSCRIPT_CHARS:
                logger.info(
                    "Transcript %s (%s) has only %d chars — Gemini has probably "
                    "not finished writing it. Skipping; the next pass retries.",
                    filename,
                    file_id,
                    len(text.strip()),
                )
                continue

            allowed_emails = {user_email.lower()}
            allowed_emails.update(
                e.lower() for e in drive.extract_participants_from_transcript(text)
            )

            header_text = text[:3000].lower()
            for u in all_app_users:
                if u.name and u.name.lower() in header_text:
                    allowed_emails.add(u.email.lower())

            # Prefer ACTUAL Meet attendance (who joined) over the calendar invite,
            # which includes no-shows — an invited-but-absent person shouldn't be
            # able to read the notes. Fall back to the invite list only when Meet
            # attendance can't be determined, so real attendees are never lost.
            attended = _attending_emails(
                calendar, meet, filename, transcript.get("createdTime"), all_app_users
            )
            if attended is not None:
                allowed_emails.update(attended)
            else:
                allowed_emails.update(
                    e.lower()
                    for e in _calendar_attendee_emails(
                        calendar, filename, transcript.get("createdTime")
                    )
                )

            # Drop scraped placeholder/reserved-domain addresses so they never
            # reach the ACL or the allowed_emails cache.
            allowed_emails_list = sorted(
                e for e in allowed_emails if not is_placeholder_email(e)
            )

            now = datetime.now(timezone.utc)
            if settings.APP_MODE == AppMode.GCP:
                source_uri = f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', f'{doc_id}.txt')}"
            else:
                stored = local_data_dir() / "rag" / f"{doc_id}.txt"
                stored.parent.mkdir(parents=True, exist_ok=True)
                stored.write_text(text)
                source_uri = stored.as_uri()
            asset = AssetMetadata(
                doc_id=doc_id,
                owner=user_email,
                topic="Meeting Transcripts",
                # No department: transcripts are participant-gated (see
                # has_document_access + search_past_meetings), not
                # department-scoped. "Internal" was a phantom label.
                department=None,
                status="pending",
                is_company_wide=False,
                source_path=source_uri,
                allowed_emails=allowed_emails_list,
                original_filename=filename,
                created_at=now,
                updated_at=now,
            )
            dam_registry.register_asset(asset)

            # ── ACCESS BOUNDARY ─────────────────────────────────────────────
            # Authoritative participant ACL. Every email here is granted query
            # access to this transcript and nothing more. Do not write here for
            # any non-meeting asset.
            participants_repo.upsert_participants(
                doc_id, meeting_id=file_id, emails=allowed_emails_list
            )
            # ────────────────────────────────────────────────────────────────

            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir) / f"{doc_id}.txt"
                tmp_path.write_text(text)
                allowed_emails_str = "|" + "|".join(allowed_emails_list) + "|"
                manual_metadata = {
                    "owner": user_email,
                    "topic": "Meeting Transcripts",
                    "status": "approved",
                    "is_company_wide": False,
                    "allowed_emails": allowed_emails_str,
                    "original_filename": filename,
                }
                res = processor.process_file(
                    tmp_path,
                    manual_metadata=manual_metadata,
                    target_corpus_id=target_corpus_id,
                )
                result = res.get("result")
                if not result or not result.success:
                    err = result.error if result else "unknown error"
                    logger.error("Ingest failed for %s: %s", filename, err)
                    dam_registry.update_asset(doc_id, {"status": "failed"})
                else:
                    dam_registry.update_asset(doc_id, {"status": "approved"})
                    ingested += 1
                    # Meeting routines ("after every client call, …"): queues
                    # rows only, never raises, no-op when the feature is off.
                    from rubik_core.meeting_skills.routines import on_transcript_ready

                    on_transcript_ready(doc_id)
        except Exception as e:
            logger.exception(
                "Error processing transcript %s for %s: %s", file_id, user_email, e
            )
            dam_registry.update_asset(doc_id, {"status": "failed"})

    return ingested


def run_sync(user_emails: list[str] | None = None, days_ago: int = 1) -> dict:
    """Sync new transcripts for the given users (or every domain user)."""
    require_automations_enabled()
    processor = _build_processor()
    dam_registry = DAMRegistry()
    participants_repo = SQLMeetingParticipantsRepository()
    user_registry = SQLUserRegistry()
    all_app_users = user_registry.list_users()

    target_corpus_id = _resolve_meetings_corpus_id()
    emails = _resolve_user_emails(user_emails)
    if not emails:
        logger.info("No users to sync.")
        return {"users": 0, "ingested": 0}

    total_ingested = 0
    db = SessionLocal()
    try:
        for email in emails:
            try:
                count = _sync_user(
                    email,
                    days_ago,
                    processor,
                    target_corpus_id,
                    dam_registry,
                    participants_repo,
                    all_app_users,
                )
                total_ingested += count
                u = db.query(User).filter(User.email == email).first()
                if u:
                    u.last_ingest = datetime.now(timezone.utc)
                    db.commit()
            except Exception as e:
                logger.exception("Sync failed for %s: %s", email, e)
    finally:
        db.close()

    logger.info(
        "transcript_sync done: users=%d ingested=%d", len(emails), total_ingested
    )
    return {"users": len(emails), "ingested": total_ingested}


def backfill_participants(
    user_emails: list[str] | None = None, dry_run: bool = False
) -> dict:
    """Reconcile already-ingested transcript ACLs to ACTUAL Meet attendance.

    The historical sync seeded the ACL from the calendar INVITE, so invited-but-
    absent people can still read meetings they never joined. This re-derives each
    ACL from real Meet attendance and REVOKES app users who didn't attend.

    Conservative, so it can't lock out real attendees:
      * Only reconciles a transcript when actual attendance can be determined
        (calendar event → Meet code → ``conferenceRecords.participants``). When it
        can't, the ACL is left as-is (only placeholder junk is stripped) and the
        transcript is counted under ``undetermined``.
      * Only APP USERS are ever revoked — an app user in the ACL who is NOT a Meet
        joiner is dropped (the invited-but-absent case). The owner is always kept,
        and external / non-app scraped emails are kept (no Cubie account to abuse,
        and re-verifying them would mean re-reading the doc).
      * Meet joiners missing from the ACL are added (repairs old silent-attendee gaps).

    ``dry_run=True`` computes and returns the full plan WITHOUT writing anything —
    inspect ``changes`` to see exactly who would be revoked/added per transcript.
    Idempotent; best-effort per transcript. Optionally scoped to ``user_emails``.

    Returns ``{"scanned","updated","participants_added","participants_removed",
    "undetermined","dry_run","changes"}``.
    """
    dam_registry = DAMRegistry()
    participants_repo = SQLMeetingParticipantsRepository()
    all_app_users = SQLUserRegistry().list_users()
    app_user_emails = {
        u.email.lower() for u in all_app_users if getattr(u, "email", None)
    }
    scope = {e.strip().lower() for e in user_emails} if user_emails else None

    db = SessionLocal()
    try:
        assets = (
            db.query(Asset)
            .filter(Asset.topic == "Meeting Transcripts")
            .filter(Asset.status == "approved")
            .all()
        )
    finally:
        db.close()

    calendars: dict[str, object | None] = {}
    meets: dict[str, object | None] = {}

    def _cal_for(owner: str):
        if owner not in calendars:
            calendars[owner] = _calendar_client_for(owner)
        return calendars[owner]

    def _meet_for(owner: str):
        if owner not in meets:
            meets[owner] = _meet_client_for(owner)
        return meets[owner]

    scanned = updated = added_n = removed_n = undetermined = skipped = 0
    changes: list[dict] = []
    for asset in assets:
        owner = (asset.owner_email or "").strip().lower()
        if scope is not None and owner not in scope:
            continue
        scanned += 1
        if scanned % 20 == 0:
            logger.info(
                "backfill progress: %d scanned, %d updated, %d revoked so far",
                scanned,
                updated,
                removed_n,
            )
        if owner and owner not in app_user_emails:
            # Not a real Cubie user (e.g. an impersonation artifact like
            # impersonated_user@sales.local) — no calendar/Meet to check, and a
            # DWD client build for it fails slowly. Skip fast; leave its ACL as-is.
            skipped += 1
            continue
        try:
            existing = {
                e.strip().lower()
                for e in participants_repo.list_participants_for_doc(asset.doc_id)
            }
            cache = {e.strip().lower() for e in (asset.allowed_emails or [])}
            attending = _attending_emails(
                _cal_for(owner),
                _meet_for(owner),
                asset.original_filename,
                asset.created_at,
                all_app_users,
            )

            if attending is None:
                # Attendance couldn't be determined — do NOT revoke (would risk
                # locking out real attendees); only strip placeholder junk.
                undetermined += 1
                desired = {
                    e
                    for e in (existing | cache)
                    if e and "@" in e and not is_placeholder_email(e)
                }
            else:
                # Attendance known → keep owner + actual joiners + external
                # (non-app) emails; DROP app users who didn't join.
                desired = {
                    e
                    for e in (existing | cache | set(attending) | {owner})
                    if e
                    and "@" in e
                    and not is_placeholder_email(e)
                    and (
                        e == owner
                        or e not in app_user_emails  # external / scraped — harmless
                        or e in attending  # app user who actually joined
                    )
                }

            desired_list = sorted(desired)
            revoked = sorted(existing - desired)
            added = sorted(desired - existing)
            needs_acl = desired != existing
            needs_cache = sorted(cache) != desired_list
            if not needs_acl and not needs_cache:
                continue

            if revoked or added:
                changes.append(
                    {
                        "doc_id": asset.doc_id,
                        "filename": asset.original_filename,
                        "owner": owner,
                        "revoke": revoked,
                        "add": added,
                    }
                )

            if not dry_run:
                if needs_acl:
                    meeting_id = (
                        participants_repo.meeting_id_for_doc(asset.doc_id)
                        or asset.doc_id
                    )
                    participants_repo.upsert_participants(
                        asset.doc_id, meeting_id=meeting_id, emails=desired_list
                    )
                if needs_cache:
                    # Keep the denormalized cache in sync with the authoritative ACL.
                    dam_registry.update_asset(
                        asset.doc_id, {"allowed_emails": desired_list}
                    )
            updated += 1
            added_n += len(desired - existing)
            removed_n += len(existing - desired)
        except Exception:
            logger.exception("Backfill failed for transcript %s", asset.doc_id)

    logger.info(
        "transcript participant backfill %sdone: scanned=%d updated=%d added=%d "
        "removed=%d undetermined=%d skipped=%d",
        "(dry-run) " if dry_run else "",
        scanned,
        updated,
        added_n,
        removed_n,
        undetermined,
        skipped,
    )
    return {
        "scanned": scanned,
        "updated": updated,
        "participants_added": added_n,
        "participants_removed": removed_n,
        "undetermined": undetermined,
        "skipped": skipped,
        "dry_run": dry_run,
        "changes": changes,
    }


def main() -> None:
    if not automations_enabled():
        logger.info(
            "Automatic processing is paused; transcript sync exits without work"
        )
        return
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    parser = argparse.ArgumentParser(
        description="Sync Google Meet transcripts into the RAG corpus."
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Reconcile the participant ACL for already-ingested transcripts to "
        "actual Meet attendance: revoke app users who didn't join and add any "
        "missing joiners. Does not ingest new transcripts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --backfill: compute and print the plan (who would be "
        "revoked/added per transcript) WITHOUT writing anything.",
    )
    parser.add_argument(
        "--users",
        nargs="+",
        help="Specific user emails to sync (default: all domain users).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Look back this many days for modified transcripts (default 1, matches a 15-min cadence with overlap).",
    )
    args = parser.parse_args()
    if args.backfill:
        result = backfill_participants(user_emails=args.users, dry_run=args.dry_run)
        prefix = "[DRY RUN] " if result["dry_run"] else ""
        print(
            f"{prefix}backfill: scanned={result['scanned']} "
            f"updated={result['updated']} added={result['participants_added']} "
            f"removed={result['participants_removed']} "
            f"undetermined={result['undetermined']} skipped={result['skipped']}"
        )
        for c in result["changes"]:
            print(f"  {c['filename']} (owner {c['owner']})")
            if c["revoke"]:
                print(f"    - REVOKE: {', '.join(c['revoke'])}")
            if c["add"]:
                print(f"    + ADD:    {', '.join(c['add'])}")
    else:
        run_sync(user_emails=args.users, days_ago=args.days)


if __name__ == "__main__":
    main()
