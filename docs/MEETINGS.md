# Meeting Transcripts — Access Model

This document describes how Cubie ingests Google Meet / Fathom transcripts and
who is allowed to query them. Read this before changing anything in
`transcript_sync.py`, `backend/routers/transcripts.py`, the `MeetingParticipant`
SQL model, or `AgentTools.search_past_meetings`.

## TL;DR

> **A user can only query a meeting they actually attended.**
>
> - The authoritative ACL is the `meeting_participants` SQL table.
> - `Asset.allowed_emails` is a denormalized cache used only by the vector
>   store metadata filter; never trust it on its own.
> - Deleting a transcript leaves a tombstone (`status="deleted"`) so the
>   sync job will not re-ingest it.

## Components

| Component | Path | Purpose |
| --- | --- | --- |
| `transcript_sync.py` | `rubik_core/scripts/` | Cloud Scheduler entry point (invoked via `POST /api/admin/transcript-sync`). Pulls new Meet/Gemini transcripts every 15 minutes and ingests them. |
| `MeetingParticipant` | `rubik_core/database/models.py` | Junction table `(doc_id, participant_email)` — the source of truth. |
| `SQLMeetingParticipantsRepository` | `rubik_core/database/repository.py` | Read/write helper for the junction table. |
| `transcripts` router | `backend/routers/transcripts.py` | `GET /api/transcripts/`, `DELETE /api/transcripts/{meeting_id}`. |
| `AgentTools.search_past_meetings` | `rubik_core/adk_agent/tools.py` | RAG retrieval gated by the participants table. |
| Transcripts tab | `frontend/src/pages/Assets.tsx` | UI list with Pending / Indexed / Deleted / Failed status and delete button. |

## Lifecycle

1. **Sync.** Cloud Scheduler invokes `transcript_sync.py` every 15 minutes
   (`--days 1` window, with overlap to be safe). For each domain user the
   script reads their Drive `Meet Recordings` folder and any `Notes by Gemini`
   docs.
2. **Skip if seen.** A transcript's `doc_id` is `sha256(drive_file_id)`. If an
   `Asset` row already exists with status `approved` or `deleted`, the
   transcript is skipped. The `deleted` tombstone is what guarantees a
   user-removed transcript is never re-ingested.
3. **Ingest.** A new transcript is written to GCS, registered in `assets`
   (status `pending` → `approved` on success or `failed` on error), and the
   participant set is upserted into `meeting_participants`. Participants are
   harvested by:
   - regex-extracting emails from the transcript body,
   - cross-referencing user names found in the transcript header against the
     `users` table,
   - always including the host (the user the script is running as).
4. **Query.** `search_past_meetings` first looks up the caller's accessible
   `doc_id` set from `meeting_participants`. If empty, it returns nothing. If
   non-empty, it queries the meeting RAG corpus, then re-checks every returned
   chunk's `doc_id` against the accessible set. This double check is defense
   in depth — a stale vector-store metadata filter cannot leak a transcript.
5. **Delete.** `DELETE /api/transcripts/{meeting_id}` (owner or admin only):
   - removes file/import from the vector store,
   - removes every row in `meeting_participants` for that `doc_id`,
   - marks the asset `status="deleted"` (tombstone, not hard delete).

## Authorization rules

Implemented in `backend/routers/transcripts.py` and `search_past_meetings`:

| Action | Who is allowed |
| --- | --- |
| Query a transcript via chat | A row in `meeting_participants` exists for `(doc_id, caller.email)`. |
| List transcripts in DAM UI | Owner OR participant. Admins see everything. |
| Delete a transcript | Owner OR admin. **Never** a non-owning participant — that would let one attendee strip access from everyone else. |

Every denied access is logged via `dam.audit.log_action` with action
`transcript_delete` and status `denied`. Look for these in the audit log when
investigating suspected unauthorized access.

## When in doubt: deny

`SQLMeetingParticipantsRepository.has_access` returns `False` for missing
inputs, and `search_past_meetings` short-circuits to "no past meetings on
record" when the caller has zero participant rows. There is no department or
group fallback — meeting transcripts are deliberately excluded from the
legacy department-match in `rubik_core/auth/access.py`. Do not add a fallback
without reading this doc and updating it.

## Adding a new ingestion source

If you wire up a new transcript source (e.g., Zoom, Teams), it must:

1. Write the asset with `topic="Meeting Transcripts"` and `status="pending"`.
2. Call `SQLMeetingParticipantsRepository.upsert_participants(doc_id, meeting_id, emails)`
   with the full attendee list **before** the asset becomes queryable.
3. Refuse to re-ingest if `Asset.status == "deleted"`.

Tests for the access boundary live in `backend/tests/test_meet_transcripts.py`.
Add a test for any new source.
