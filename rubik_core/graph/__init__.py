"""The customer relationship graph — who knows whom, and how we know it.

Cubie derived relationships per feature and threw them away: X-Ray joined on a
HubSpot ``company_id``, Meeting Prep matched email-domain strings, and both
reached transcripts by guessing a name into a semantic search. This package is
the shared identity layer underneath them — typed nodes, typed edges, and
provenance on every edge, so a fact can be traced to the source that asserted it.

Layout:

* ``keys`` — canonical node identity (``"{kind}:{id}"``) and ALL normalization.
  The node vocabulary is ``EntityKind``; there is deliberately no parallel enum.
* ``store`` — upserts, set-replace, and bounded ego traversal over the
  ``graph_entities`` / ``graph_edges`` tables.
* ``writers`` — fail-soft hooks that persist what host flows already fetched.
  Writers never make an external API call of their own.

Two rules hold everywhere (see ``docs/customer_relationship_graph.md`` §6):

1. **Metadata only.** Nodes and edges carry ids, titles, dates and provenance —
   never transcript text, email bodies, or document content. Content is always
   re-fetched through its existing access path.
2. **The graph is not an access path.** ``store.ego`` is unfiltered
   infrastructure; every feature read is anchored on the requesting user, and
   transcript-derived facts pass through
   ``store.filter_transcripts_for_reader`` first. A meeting you were not in
   never appears in your briefing, not even as a title.
"""

from __future__ import annotations

from rubik_core.graph.keys import (
    company_key,
    company_key_for_email,
    deal_key,
    entity_key,
    event_key,
    parse_key,
    person_key,
    transcript_key,
)

__all__ = [
    "company_key",
    "company_key_for_email",
    "deal_key",
    "entity_key",
    "event_key",
    "parse_key",
    "person_key",
    "transcript_key",
]
