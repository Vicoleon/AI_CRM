"""Reads and writes over ``graph_entities`` / ``graph_edges``.

Plain SQLAlchemy, no graph database: a bounded ego traversal is one indexed
query per hop, and Phase 1 never goes deeper than two. At one company's scale (order
10^4–10^5 edges) that is a few milliseconds, and it keeps the graph inside the
database we already back up, migrate and permission.

Three properties this module is responsible for:

* **Identity is never invented here.** Keys arrive already normalized from
  ``rubik_core.graph.keys``; this module only stores them. A writer that
  formats its own key can split one person into two nodes, which no amount of
  clever traversal will fix afterwards.
* **Edges accumulate, they do not churn.** Re-observing an edge bumps
  ``last_seen_at`` (and clears ``invalidated_at``) instead of inserting a
  duplicate, so "we have seen this relationship since March" survives every
  re-sync. Disappearance is a soft ``invalidated_at``, not a DELETE.
* **Every write is its own transaction by default.** Graph writes are
  best-effort decoration hung off flows that matter more than they do (the
  transcript ACL, an X-Ray run). Sharing those callers' sessions would let a
  graph bug roll back an authoritative write, so callers pass ``db`` only when
  they genuinely want one transaction.

``ego()`` is deliberately UNFILTERED infrastructure. Feature code must apply the
reader gate from ``docs/customer_relationship_graph.md`` §6 —
``filter_transcripts_for_reader`` exists so no caller reimplements it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rubik_core.database.models import GraphEdge, GraphEntity
from rubik_core.graph.keys import parse_key

logger = logging.getLogger(__name__)

# A single traversal never returns more than this many edges, whatever the
# caller asks for. A hub node (an all-hands transcript, a company with hundreds
# of contacts) would otherwise pull an unbounded slice of the graph into a
# prompt — expensive, and useless once it exceeds what a reader can absorb.
MAX_EDGES = 500


@dataclass(frozen=True)
class EntityStub:
    """A node to upsert. ``key`` must already be normalized (graph.keys)."""

    key: str
    display_name: str | None = None
    attrs: dict[str, Any] | None = None


@dataclass(frozen=True)
class EdgeStub:
    """An edge to upsert, with the provenance that makes it auditable.

    ``observed_at`` is when the fact HAPPENED (the meeting date), which is not
    the same as when we recorded it — a briefing that says "you last met on the
    4th" is reading this, not ``first_seen_at``.
    """

    src_key: str
    dst_key: str
    rel: str
    source: str
    method: str = "deterministic"
    confidence: float = 1.0
    evidence_ref: str | None = None
    observed_at: datetime | None = None
    attrs: dict[str, Any] | None = None

    @property
    def natural_key(self) -> tuple[str, str, str, str]:
        """What ``uq_graph_edge`` considers one row."""
        return (self.src_key, self.dst_key, self.rel, self.source)


@dataclass(frozen=True)
class NodeView:
    """A node, detached from the session that loaded it."""

    key: str
    kind: str
    entity_id: str
    display_name: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeView:
    """An edge, detached from the session that loaded it.

    Reads return these rather than ORM rows on purpose: a caller that holds a
    live ``GraphEdge`` after its session closes gets a ``DetachedInstanceError``
    on attribute access, which would make every consumer's lifetime management
    the store's problem. A plain view is safe to pass into a prompt builder, a
    background task, or a response model.
    """

    src_key: str
    dst_key: str
    rel: str
    source: str
    method: str
    confidence: float
    evidence_ref: str | None = None
    observed_at: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    invalidated_at: datetime | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


def _node_view(row: GraphEntity) -> NodeView:
    return NodeView(
        key=row.entity_key,
        kind=row.kind,
        entity_id=row.entity_id,
        display_name=row.display_name,
        attrs=dict(row.attrs or {}),
    )


def _edge_view(row: GraphEdge) -> EdgeView:
    return EdgeView(
        src_key=row.src_key,
        dst_key=row.dst_key,
        rel=row.rel,
        source=row.source,
        method=row.method,
        confidence=row.confidence,
        evidence_ref=row.evidence_ref,
        observed_at=row.observed_at,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        invalidated_at=row.invalidated_at,
        attrs=dict(row.attrs or {}),
    )


@dataclass
class EgoGraph:
    """A bounded neighbourhood: the edges found, plus the nodes they touch."""

    nodes: dict[str, NodeView] = field(default_factory=dict)
    edges: list[EdgeView] = field(default_factory=list)
    truncated: bool = False

    def neighbor_keys(self, of_keys: Iterable[str]) -> set[str]:
        """Keys reachable in one hop from ``of_keys`` within this result."""
        anchors = set(of_keys)
        out: set[str] = set()
        for edge in self.edges:
            if edge.src_key in anchors:
                out.add(edge.dst_key)
            if edge.dst_key in anchors:
                out.add(edge.src_key)
        return out - anchors


@contextmanager
def _session(db: Session | None):
    """Use the caller's session, or own one (commit + close) if none is given.

    ``SessionLocal`` is resolved through the module at call time, never bound at
    import: several test suites ``importlib.reload`` the database modules, and a
    reference captured at import would keep writing to the old engine.
    """
    if db is not None:
        yield db
        return
    from rubik_core.database import db as db_module

    own = db_module.SessionLocal()
    try:
        yield own
        own.commit()
    finally:
        own.close()


def upsert_entities(entities: Iterable[EntityStub], db: Session | None = None) -> int:
    """Insert missing nodes; refresh ``last_seen_at`` (and any label) on the rest.

    Returns the number of nodes created. A blank ``display_name`` never
    overwrites a known one — writers see the world partially, and a HubSpot
    contact with a name should not be blanked by a later calendar sighting that
    only knew the address.
    """
    deduped: dict[str, EntityStub] = {}
    for stub in entities:
        if not stub or not stub.key:
            continue
        existing = deduped.get(stub.key)
        if existing is None:
            deduped[stub.key] = stub
        else:  # last non-empty label/attrs within a batch wins
            deduped[stub.key] = EntityStub(
                key=stub.key,
                display_name=stub.display_name or existing.display_name,
                attrs={**(existing.attrs or {}), **(stub.attrs or {})} or None,
            )
    if not deduped:
        return 0

    created = 0
    now = datetime.utcnow()
    with _session(db) as session:
        rows = session.execute(
            select(GraphEntity).where(GraphEntity.entity_key.in_(list(deduped)))
        )
        by_key = {row.entity_key: row for row in rows.scalars()}

        for key, stub in deduped.items():
            row = by_key.get(key)
            if row is None:
                kind, entity_id = parse_key(key)
                if not kind or not entity_id:
                    logger.warning("graph: skipping malformed entity key %r", key)
                    continue
                session.add(
                    GraphEntity(
                        entity_key=key,
                        kind=kind,
                        entity_id=entity_id,
                        display_name=stub.display_name,
                        attrs=stub.attrs or {},
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                created += 1
            else:
                row.last_seen_at = now
                if stub.display_name:
                    row.display_name = stub.display_name
                if stub.attrs:
                    row.attrs = {**(row.attrs or {}), **stub.attrs}
    return created


def upsert_edges(edges: Iterable[EdgeStub], db: Session | None = None) -> int:
    """Insert missing edges; re-observe the rest.

    Re-observation bumps ``last_seen_at``, keeps the HIGHER confidence (a
    deterministic sighting must not be downgraded by a later heuristic one), and
    clears ``invalidated_at`` — a relationship that comes back is the same
    relationship, not a new one.

    Nodes are NOT auto-created: call ``upsert_entities`` first. Edges carry a
    foreign key to ``graph_entities``, and silently conjuring endpoint nodes
    would hide the writer bug that produced a dangling key.
    """
    deduped: dict[tuple[str, str, str, str], EdgeStub] = {}
    for edge in edges:
        if not edge or not edge.src_key or not edge.dst_key or not edge.rel:
            continue
        deduped[edge.natural_key] = edge
    if not deduped:
        return 0

    created = 0
    now = datetime.utcnow()
    with _session(db) as session:
        src_keys = {k[0] for k in deduped}
        rows = session.execute(
            select(GraphEdge).where(GraphEdge.src_key.in_(list(src_keys)))
        )
        existing = {(r.src_key, r.dst_key, r.rel, r.source): r for r in rows.scalars()}

        for natural_key, stub in deduped.items():
            row = existing.get(natural_key)
            if row is None:
                session.add(
                    GraphEdge(
                        id=uuid.uuid4().hex,
                        src_key=stub.src_key,
                        dst_key=stub.dst_key,
                        rel=stub.rel,
                        source=stub.source,
                        method=stub.method,
                        confidence=stub.confidence,
                        evidence_ref=stub.evidence_ref,
                        observed_at=stub.observed_at,
                        first_seen_at=now,
                        last_seen_at=now,
                        attrs=stub.attrs or {},
                    )
                )
                created += 1
            else:
                row.last_seen_at = now
                row.invalidated_at = None
                if stub.confidence > (row.confidence or 0.0):
                    row.confidence = stub.confidence
                    row.method = stub.method
                if stub.observed_at:
                    row.observed_at = stub.observed_at
                if stub.evidence_ref:
                    row.evidence_ref = stub.evidence_ref
                if stub.attrs:
                    row.attrs = {**(row.attrs or {}), **stub.attrs}
    return created


def replace_edges(
    anchor_key: str,
    rel: str,
    source: str,
    other_keys: Sequence[str],
    *,
    anchor_side: str = "src",
    evidence_ref: str | None = None,
    observed_at: datetime | None = None,
    method: str = "deterministic",
    confidence: float = 1.0,
    db: Session | None = None,
) -> tuple[int, int]:
    """Make ``anchor_key``'s ``(rel, source)`` edge set exactly ``other_keys``.

    Set semantics, for sources that publish a complete list rather than
    increments — ``meeting_participants`` replaces a transcript's whole attendee
    set on every re-sync, so the graph must mirror removals too.

    ``anchor_side`` says which end is fixed: ``"dst"`` for attendance (many
    people → one transcript). Edges no longer present are soft-invalidated, not
    deleted: "these five attended, and Casey used to be listed" is a fact worth
    keeping, and a re-sync blip should not erase history.

    Returns ``(created, invalidated)``.
    """
    if anchor_side not in ("src", "dst"):
        raise ValueError(f"anchor_side must be 'src' or 'dst', got {anchor_side!r}")
    wanted = {k for k in other_keys if k}

    stubs = [
        EdgeStub(
            src_key=(anchor_key if anchor_side == "src" else other),
            dst_key=(other if anchor_side == "src" else anchor_key),
            rel=rel,
            source=source,
            method=method,
            confidence=confidence,
            evidence_ref=evidence_ref,
            observed_at=observed_at,
        )
        for other in sorted(wanted)
    ]

    now = datetime.utcnow()
    with _session(db) as session:
        created = upsert_edges(stubs, db=session)

        anchor_col = GraphEdge.src_key if anchor_side == "src" else GraphEdge.dst_key
        other_col = GraphEdge.dst_key if anchor_side == "src" else GraphEdge.src_key
        rows = session.execute(
            select(GraphEdge).where(
                anchor_col == anchor_key,
                GraphEdge.rel == rel,
                GraphEdge.source == source,
                GraphEdge.invalidated_at.is_(None),
            )
        )
        invalidated = 0
        for row in rows.scalars():
            if getattr(row, other_col.key) not in wanted:
                row.invalidated_at = now
                invalidated += 1
    return created, invalidated


def ego(
    keys: Sequence[str],
    rels: Sequence[str] | None = None,
    depth: int = 1,
    limit: int = 200,
    include_invalidated: bool = False,
    db: Session | None = None,
) -> EgoGraph:
    """Edges within ``depth`` hops of ``keys``, plus the nodes they touch.

    UNFILTERED: this is infrastructure, not an access decision. Anything derived
    from transcripts must be passed through ``filter_transcripts_for_reader``
    before it reaches a reader — see the module docstring.
    """
    result = EgoGraph()
    frontier = {k for k in keys if k}
    if not frontier or depth < 1:
        return result

    budget = max(0, min(limit, MAX_EDGES))
    seen_edge_ids: set[str] = set()
    visited: set[str] = set()

    with _session(db) as session:
        for _ in range(depth):
            if not frontier or len(result.edges) >= budget:
                break
            conditions = [
                or_(
                    GraphEdge.src_key.in_(list(frontier)),
                    GraphEdge.dst_key.in_(list(frontier)),
                )
            ]
            if rels:
                conditions.append(GraphEdge.rel.in_(list(rels)))
            if not include_invalidated:
                conditions.append(GraphEdge.invalidated_at.is_(None))

            rows = session.execute(
                select(GraphEdge)
                .where(*conditions)
                .order_by(GraphEdge.observed_at.desc().nullslast(), GraphEdge.id)
                .limit(budget - len(result.edges) + 1)
            )
            hop_rows = [e for e in rows.scalars() if e.id not in seen_edge_ids]
            if len(result.edges) + len(hop_rows) > budget:
                hop_rows = hop_rows[: budget - len(result.edges)]
                result.truncated = True

            for edge in hop_rows:
                seen_edge_ids.add(edge.id)
                result.edges.append(_edge_view(edge))

            # Expand only into nodes not already walked, so a cycle (two people
            # who share two meetings) cannot bounce the frontier back and forth.
            visited |= frontier
            frontier = result.neighbor_keys(frontier) - visited

        touched = {e.src_key for e in result.edges} | {e.dst_key for e in result.edges}
        touched |= {k for k in keys if k}
        if touched:
            nodes = session.execute(
                select(GraphEntity).where(GraphEntity.entity_key.in_(list(touched)))
            )
            result.nodes = {n.entity_key: _node_view(n) for n in nodes.scalars()}
    return result


def filter_transcripts_for_reader(reader_email: str, keys: Sequence[str]) -> list[str]:
    """Keep only the transcript keys ``reader_email`` actually attended.

    The graph knows every meeting; a reader may see only their own. This
    delegates to ``meeting_participants`` — the same authoritative ACL that
    gates transcript retrieval — rather than trusting anything stored here, so
    the graph cannot become a second, weaker permission model. Non-transcript
    keys pass through untouched; this function only speaks for transcripts.
    """
    from rubik_core.database.repository import SQLMeetingParticipantsRepository
    from rubik_core.graph.keys import KIND_TRANSCRIPT, transcript_key

    transcript_keys = [k for k in keys if parse_key(k)[0] == KIND_TRANSCRIPT]
    if not transcript_keys:
        return list(keys)

    allowed_doc_ids = set(
        SQLMeetingParticipantsRepository().list_doc_ids_for_user(reader_email)
    )
    allowed_keys = {transcript_key(d) for d in allowed_doc_ids}
    return [k for k in keys if parse_key(k)[0] != KIND_TRANSCRIPT or k in allowed_keys]
