"""One-shot sweep that fills the graph from records that predate it.

The incremental writers only fire on new activity, so without this the graph
would start empty and stay thin for months — every transcript already ingested
would be invisible to it. This walks ``meeting_participants`` (joined to
``assets`` for the title and meeting-date proxy) and writes the same
``attended`` / ``member_of`` edges the live writer produces.

Safe to re-run: writes go through the store's upsert path, so a second sweep
re-observes existing edges (bumping ``last_seen_at``) instead of duplicating
them. Runs as a ``cubie-worker`` task — never in-request, since Cloud Run
throttles CPU after the response and a partial sweep is worse than none.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from rubik_core.graph import keys as gk
from rubik_core.graph.store import (
    EntityStub,
    replace_edges,
    upsert_edges,
    upsert_entities,
)
from rubik_core.graph.writers import (
    REL_ATTENDED,
    SOURCE_MEETING_PARTICIPANTS,
    membership_edges,
)

logger = logging.getLogger(__name__)

# Transcripts per transaction. Small enough that a failure loses little work and
# the job stays interruptible; large enough that we are not paying per-row
# round-trip costs on a few thousand meetings.
DEFAULT_BATCH_SIZE = 100


def backfill_attendance(
    *, limit: int | None = None, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict[str, int]:
    """Rebuild ``attended`` edges for every transcript already on record.

    Returns counters: transcripts seen, entities created, edges created.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import Asset, MeetingParticipant

    stats: dict[str, int] = {
        "transcripts": 0,
        "participants": 0,
        "entities_created": 0,
        "edges_created": 0,
        "skipped_no_asset": 0,
    }

    with SessionLocal() as session:
        query = (
            session.query(
                MeetingParticipant.doc_id,
                MeetingParticipant.participant_email,
                MeetingParticipant.meeting_id,
                Asset.original_filename,
                Asset.created_at,
            )
            .join(Asset, Asset.doc_id == MeetingParticipant.doc_id)
            .filter(Asset.status == "approved")
            .order_by(Asset.created_at.desc())
        )
        rows = query.all()

    by_doc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"emails": [], "meeting_id": "", "title": None, "when": None}
    )
    for doc_id, email, meeting_id, title, created_at in rows:
        entry = by_doc[doc_id]
        entry["emails"].append(email)
        entry["meeting_id"] = entry["meeting_id"] or (meeting_id or "")
        entry["title"] = entry["title"] or title
        entry["when"] = entry["when"] or created_at

    doc_ids = list(by_doc)
    if limit is not None:
        doc_ids = doc_ids[: max(0, limit)]

    for start in range(0, len(doc_ids), max(1, batch_size)):
        chunk = doc_ids[start : start + max(1, batch_size)]
        for doc_id in chunk:
            entry = by_doc[doc_id]
            transcript_key = gk.transcript_key(doc_id)
            if not transcript_key:
                stats["skipped_no_asset"] += 1
                continue

            person_keys_by_email: dict[str, str] = {}
            for email in entry["emails"]:
                normalized = gk.normalize_email(email)
                person_key = gk.person_key(normalized)
                if normalized and person_key:
                    person_keys_by_email[normalized] = person_key

            entities = [
                EntityStub(
                    key=transcript_key,
                    display_name=entry["title"],
                    attrs={"meeting_id": entry["meeting_id"]}
                    if entry["meeting_id"]
                    else None,
                ),
                *(EntityStub(key=k) for k in person_keys_by_email.values()),
                *(
                    EntityStub(key=ck)
                    for ck in {
                        gk.company_key_for_email(e) for e in person_keys_by_email
                    }
                    if ck
                ),
            ]
            stats["entities_created"] += upsert_entities(entities)

            created, _ = replace_edges(
                transcript_key,
                REL_ATTENDED,
                SOURCE_MEETING_PARTICIPANTS,
                list(person_keys_by_email.values()),
                anchor_side="dst",
                evidence_ref=doc_id,
                observed_at=entry["when"],
            )
            stats["edges_created"] += created
            stats["edges_created"] += upsert_edges(
                membership_edges(person_keys_by_email)
            )
            stats["transcripts"] += 1
            stats["participants"] += len(person_keys_by_email)

        logger.info(
            "graph backfill: %d/%d transcripts", stats["transcripts"], len(doc_ids)
        )

    return stats
