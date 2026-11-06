"""Fail-soft hooks that persist relationships the host flow already fetched.

Two rules every writer here obeys:

1. **No new external calls.** A writer only records what its caller already had
   in hand. If a fact needs another API round-trip, it is not written — the
   graph is a by-product of work that was happening anyway, never a reason for
   extra latency or quota.
2. **Never break the host.** Each entry point swallows its own exceptions and
   logs. These hooks hang off flows that matter far more than they do (the
   transcript ACL, an X-Ray run), and a graph bug must never cost a user their
   permissions row or their briefing.

Writers also run in their OWN database session rather than joining the caller's
transaction: sharing it would let a failure here roll back the authoritative
write it was decorating.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from rubik_core.graph import keys as gk
from rubik_core.graph.store import (
    EdgeStub,
    EntityStub,
    replace_edges,
    upsert_edges,
    upsert_entities,
)

logger = logging.getLogger(__name__)

SOURCE_MEETING_PARTICIPANTS = "meeting_participants"
SOURCE_HUBSPOT = "hubspot_associations"
SOURCE_DOMAIN_HEURISTIC = "domain_heuristic"

REL_ATTENDED = "attended"
REL_MEMBER_OF = "member_of"
REL_HAS_CONTACT = "has_contact"
REL_HAS_DEAL = "has_deal"
REL_OWNED_BY = "owned_by"

# An email domain implies employment, but only probably: shared inboxes,
# contractors on a client domain and personal addresses all break it. Recorded
# as a heuristic so a reader can tell it apart from a HubSpot association.
DOMAIN_MEMBERSHIP_CONFIDENCE = 0.8


def _graph_writes_enabled() -> bool:
    from rubik_core.config import settings

    return bool(getattr(settings, "CUBIE_GRAPH_WRITES_ENABLED", False))


def membership_edges(person_keys_by_email: dict[str, str]) -> list[EdgeStub]:
    """person → company edges implied by each address's domain.

    Consumer mail hosts are dropped by ``keys.company_key`` — a company node for
    gmail.com would merge every unrelated freelancer into one organization.
    """
    edges: list[EdgeStub] = []
    for email, person_key in person_keys_by_email.items():
        company_key = gk.company_key_for_email(email)
        if company_key:
            edges.append(
                EdgeStub(
                    src_key=person_key,
                    dst_key=company_key,
                    rel=REL_MEMBER_OF,
                    source=SOURCE_DOMAIN_HEURISTIC,
                    method="heuristic",
                    confidence=DOMAIN_MEMBERSHIP_CONFIDENCE,
                )
            )
    return edges


def record_attendance(
    doc_id: str,
    meeting_id: str,
    participant_emails: list[str],
    *,
    title: str | None = None,
    observed_at: datetime | None = None,
) -> None:
    """Record who attended one transcript, as ``person --attended--> transcript``.

    Called from ``SQLMeetingParticipantsRepository.upsert_participants`` — the
    one choke point every ingest path (Fathom, Meet sync, backfill) already goes
    through, so the graph cannot drift from the ACL by someone adding a fifth
    ingest route.

    Set semantics, mirroring the ACL: ``upsert_participants`` replaces a
    transcript's whole participant set, so attendees dropped by a re-sync are
    soft-invalidated here rather than left behind as phantom attendance.

    ``observed_at`` should be the meeting date. Callers that have the ``Asset``
    row pass its ``created_at`` (ingest time, the same meeting-date proxy the
    rest of the app uses); when omitted this looks it up rather than guessing.
    """
    if not _graph_writes_enabled() or not doc_id:
        return
    try:
        transcript_key = gk.transcript_key(doc_id)
        if not transcript_key:
            return

        person_keys_by_email: dict[str, str] = {}
        for email in participant_emails or []:
            normalized = gk.normalize_email(email)
            person_key = gk.person_key(normalized)
            if normalized and person_key:
                person_keys_by_email[normalized] = person_key

        if observed_at is None or title is None:
            resolved_title, resolved_when = _transcript_metadata(doc_id)
            title = title or resolved_title
            observed_at = observed_at or resolved_when

        upsert_entities(
            [
                EntityStub(
                    key=transcript_key,
                    display_name=title,
                    attrs={"meeting_id": meeting_id} if meeting_id else None,
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
        )

        created, invalidated = replace_edges(
            transcript_key,
            REL_ATTENDED,
            SOURCE_MEETING_PARTICIPANTS,
            list(person_keys_by_email.values()),
            anchor_side="dst",
            evidence_ref=doc_id,
            observed_at=observed_at,
        )

        upsert_edges(membership_edges(person_keys_by_email))

        logger.debug(
            "graph: transcript %s attendance +%d edges, %d invalidated",
            doc_id,
            created,
            invalidated,
        )
    except Exception:  # noqa: BLE001 — must never break the participant ACL write
        logger.warning("graph: attendance write failed for %s", doc_id, exc_info=True)


def _transcript_metadata(doc_id: str) -> tuple[str | None, datetime | None]:
    """(title, meeting-date proxy) for a transcript, or (None, None).

    ``Asset.created_at`` is the app-wide stand-in for the meeting date —
    transcripts ingest right after the meeting ends and there is no separate
    meeting-date column (see ``list_recent_doc_ids_for_user``).
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import Asset

        with SessionLocal() as session:
            row = (
                session.query(Asset.original_filename, Asset.created_at)
                .filter(Asset.doc_id == doc_id)
                .first()
            )
        return (row[0], row[1]) if row else (None, None)
    except Exception:  # noqa: BLE001 — a missing label must not lose the edge
        logger.debug("graph: transcript metadata lookup failed", exc_info=True)
        return (None, None)


def record_company_bundle(
    company_id: str,
    company_name: str | None,
    company_domain: str | None,
    deals: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    *,
    owner_emails: dict[str, str] | None = None,
) -> None:
    """Record a HubSpot company's contacts and deals from an X-Ray bundle.

    Lazy-warmed rather than swept: whenever an X-Ray run pulls a company, the
    associations it already fetched are persisted. No extra HubSpot call is made
    for this, which is why ``owner_emails`` is passed in — X-Ray holds an
    id→NAME map, and ``owned_by`` is written only when the caller happens to
    have real addresses (``HubSpotAPIClient.owner_directory``). A missing owner
    email means no edge, never a guessed one.

    The company node is keyed on its DOMAIN (``EntityKind.COMPANY``'s documented
    convention), not the HubSpot id, so it lines up with the company nodes the
    domain heuristic and calendar produce. Without a domain there is nothing to
    key on and the whole bundle is skipped — a company node keyed on a HubSpot
    id would be a second identity for the same organization.
    """
    if not _graph_writes_enabled():
        return
    try:
        company_key = gk.company_key(company_domain)
        if not company_key:
            logger.debug(
                "graph: company %s has no usable domain; skipping bundle", company_id
            )
            return

        entities: list[EntityStub] = [
            EntityStub(
                key=company_key,
                display_name=company_name or None,
                attrs={"hubspot_company_id": str(company_id)} if company_id else None,
            )
        ]
        edges: list[EdgeStub] = []

        for contact in contacts or []:
            email = gk.normalize_email(contact.get("email"))
            person_key = gk.person_key(email)
            if not person_key:
                continue
            name = " ".join(
                part
                for part in (contact.get("firstname"), contact.get("lastname"))
                if part
            ).strip()
            entities.append(
                EntityStub(
                    key=person_key,
                    display_name=name or None,
                    attrs={"jobtitle": contact["jobtitle"]}
                    if contact.get("jobtitle")
                    else None,
                )
            )
            edges.append(
                EdgeStub(
                    src_key=company_key,
                    dst_key=person_key,
                    rel=REL_HAS_CONTACT,
                    source=SOURCE_HUBSPOT,
                    evidence_ref=str(contact.get("id") or ""),
                )
            )
            # A HubSpot association is stronger evidence of employment than the
            # address domain, so this membership edge is deterministic and will
            # win the confidence comparison against the heuristic one.
            edges.append(
                EdgeStub(
                    src_key=person_key,
                    dst_key=company_key,
                    rel=REL_MEMBER_OF,
                    source=SOURCE_HUBSPOT,
                    evidence_ref=str(contact.get("id") or ""),
                )
            )

        for deal in deals or []:
            deal_key = gk.deal_key(deal.get("id"))
            if not deal_key:
                continue
            entities.append(
                EntityStub(
                    key=deal_key,
                    display_name=deal.get("dealname") or None,
                    attrs={
                        k: deal[k]
                        for k in ("dealstage", "pipeline", "amount")
                        if deal.get(k)
                    }
                    or None,
                )
            )
            edges.append(
                EdgeStub(
                    src_key=company_key,
                    dst_key=deal_key,
                    rel=REL_HAS_DEAL,
                    source=SOURCE_HUBSPOT,
                    evidence_ref=str(deal.get("id") or ""),
                )
            )
            owner_email = gk.normalize_email(
                (owner_emails or {}).get(str(deal.get("hubspot_owner_id") or ""))
            )
            owner_key = gk.person_key(owner_email)
            if owner_key:
                entities.append(EntityStub(key=owner_key))
                edges.append(
                    EdgeStub(
                        src_key=deal_key,
                        dst_key=owner_key,
                        rel=REL_OWNED_BY,
                        source=SOURCE_HUBSPOT,
                        evidence_ref=str(deal.get("id") or ""),
                    )
                )

        upsert_entities(entities)
        upsert_edges(edges)
        logger.debug(
            "graph: company %s bundle → %d entities, %d edges",
            company_key,
            len(entities),
            len(edges),
        )
    except Exception:  # noqa: BLE001 — must never break an X-Ray run
        logger.warning(
            "graph: company bundle write failed for %s", company_id, exc_info=True
        )
