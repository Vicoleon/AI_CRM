"""Multi-hop questions the graph exists to answer, with the reader gate applied.

``store.ego`` is deliberately unfiltered infrastructure. This module is where
features shop instead: each function anchors on something the caller already has
access to, walks a bounded number of hops, and puts transcript results through
``filter_transcripts_for_reader`` before returning them — so a feature cannot
accidentally surface a meeting its reader was not in.

Metadata only, always. These return ids, titles and dates; the CONTENT of a
transcript is still fetched through its own access path afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from rubik_core.graph import keys as gk
from rubik_core.graph.store import (
    NodeView,
    ego,
    filter_transcripts_for_reader,
)

logger = logging.getLogger(__name__)

# How far back a company's meeting history is worth pulling by default. Two
# hops (company → people → meetings) over a long-standing client can reach
# hundreds of transcripts, and a brief cannot use them all.
DEFAULT_MEETING_LIMIT = 8


@dataclass(frozen=True)
class MeetingHit:
    """One meeting on record, as metadata the reader is allowed to see."""

    doc_id: str
    title: str | None
    when: datetime | None
    # Which of the company's people were in the room (lowercased emails).
    counterparts: list[str]

    @property
    def when_label(self) -> str:
        return self.when.strftime("%A, %B %d, %Y") if self.when else "date unknown"


def _person_keys_for_company(
    company_key: str, extra_contact_emails: list[str] | None = None
) -> tuple[set[str], dict[str, NodeView]]:
    """People linked to a company, from HubSpot associations OR email domain.

    Both edge types are used because they cover different gaps: HubSpot knows
    contacts nobody has met yet, while the domain heuristic knows everyone who
    has actually turned up to a meeting — including people never entered in the
    CRM, who are exactly the ones a name-matching search misses.
    """
    neighbourhood = ego([company_key], rels=["has_contact", "member_of"], depth=1)
    person_keys = {
        key
        for key in neighbourhood.neighbor_keys([company_key])
        if gk.parse_key(key)[0] == gk.KIND_PERSON
    }
    for email in extra_contact_emails or []:
        key = gk.person_key(email)
        if key:
            person_keys.add(key)
    return person_keys, neighbourhood.nodes


def company_meeting_history(
    company_domain: str | None,
    reader_email: str,
    *,
    limit: int = DEFAULT_MEETING_LIMIT,
    contact_emails: list[str] | None = None,
) -> list[MeetingHit]:
    """Meetings we have had with a company, resolved by identity not by name.

    The path is ``company → its people → meetings they attended``, which finds a
    transcript even when it never says the company's name out loud — the exact
    case a semantic search over ``"meetings with {company}"`` drops on the floor.

    ACL: results are filtered to transcripts ``reader_email`` personally
    attended, preserving the boundary the meetings corpus already enforces. A
    reader with no shared history gets an empty list, never someone else's
    meetings.
    """
    company_key = gk.company_key(company_domain)
    if not company_key or not reader_email:
        return []

    person_keys, _ = _person_keys_for_company(company_key, contact_emails)
    if not person_keys:
        return []

    # Hop two: what those people attended. Ask for more than `limit` because the
    # reader gate below will remove the meetings they were not part of.
    attendance = ego(
        sorted(person_keys),
        rels=["attended"],
        depth=1,
        limit=max(limit * 10, 50),
    )

    by_transcript: dict[str, dict] = {}
    for edge in attendance.edges:
        if edge.src_key not in person_keys:
            continue
        entry = by_transcript.setdefault(
            edge.dst_key, {"when": edge.observed_at, "counterparts": set()}
        )
        entry["counterparts"].add(gk.parse_key(edge.src_key)[1])
        if edge.observed_at and (
            entry["when"] is None or edge.observed_at > entry["when"]
        ):
            entry["when"] = edge.observed_at

    allowed = set(filter_transcripts_for_reader(reader_email, list(by_transcript)))
    hits = [
        MeetingHit(
            doc_id=gk.parse_key(key)[1],
            title=(
                attendance.nodes.get(key).display_name
                if key in attendance.nodes
                else None
            ),
            when=entry["when"],
            counterparts=sorted(entry["counterparts"]),
        )
        for key, entry in by_transcript.items()
        if key in allowed
    ]
    hits.sort(key=lambda h: h.when or datetime.min, reverse=True)
    return hits[: max(0, limit)]


def format_meeting_history_block(hits: list[MeetingHit], company_name: str) -> str:
    """Render meeting history for a prompt, or "" when there is none.

    Empty input returns an empty string so callers can follow the house rule:
    no block means no instruction referring to one, and a prompt identical to
    what it would have been without this feature.
    """
    if not hits:
        return ""
    lines = [
        f"MEETINGS ON RECORD WITH {company_name.upper() or 'THIS COMPANY'} "
        "(attendance records — treat as ground truth):"
    ]
    for hit in hits:
        who = ", ".join(hit.counterparts) or "attendees not recorded"
        lines.append(f'- {hit.when_label} — "{hit.title or hit.doc_id}" — with {who}')
    lines.append(
        "(These are meetings YOU attended, resolved from attendance records rather "
        "than from a text search, so the list is complete for you even where a "
        "transcript never names the company. Dates are ingest dates, normally "
        "within a day of the meeting.)"
    )
    return "\n".join(lines)
