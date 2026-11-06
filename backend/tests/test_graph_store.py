"""Reads and writes over the relationship graph.

The properties pinned here are the ones that decide whether the graph is worth
having at all:

* An edge is a FACT WITH HISTORY. Re-observing it bumps ``last_seen_at`` instead
  of inserting a twin, and a relationship that disappears is soft-invalidated
  rather than deleted — so "they attended until the re-sync dropped them" stays
  answerable.
* Confidence only ever goes UP. A later heuristic sighting must not quietly
  downgrade a deterministic one, or provenance stops meaning anything.
* Traversal is BOUNDED. A hub node must not be able to pull the whole graph into
  a prompt.
* ``filter_transcripts_for_reader`` defers to ``meeting_participants``. The graph
  knows every meeting; a reader may see only their own, and the graph must never
  become a second, weaker permission model.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from rubik_core.graph import keys as gk
from rubik_core.graph.store import (
    EdgeStub,
    EntityStub,
    ego,
    filter_transcripts_for_reader,
    replace_edges,
    upsert_edges,
    upsert_entities,
)

ALICE = gk.person_key("alice@acme.example")
BOB = gk.person_key("bob@acme.example")
CARLA = gk.person_key("carla@northwind.example")
DOC1 = gk.transcript_key("doc-1")
DOC2 = gk.transcript_key("doc-2")
ACME = gk.company_key("northwind.example")
DEAL = gk.deal_key("55")


def _wire_sqlite(tmp_path, monkeypatch):
    """Schema'd throwaway sqlite, rebound where SessionLocal is looked up."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/graph.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    return TestSession


def _edges(TestSession, **filters):
    from rubik_core.database.models import GraphEdge

    with TestSession() as s:
        q = s.query(GraphEdge)
        for key, value in filters.items():
            q = q.filter(getattr(GraphEdge, key) == value)
        return q.all()


def _entities(TestSession):
    from rubik_core.database.models import GraphEntity

    with TestSession() as s:
        return {e.entity_key: e for e in s.query(GraphEntity).all()}


# ── entities ─────────────────────────────────────────────────────────────────


def test_upsert_entities_creates_then_re_observes(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    assert upsert_entities([EntityStub(key=ALICE, display_name="Alice")]) == 1
    first = _entities(TestSession)[ALICE]
    seen_at, created_at = first.last_seen_at, first.first_seen_at

    # Re-observing is not a new node.
    assert upsert_entities([EntityStub(key=ALICE)]) == 0
    again = _entities(TestSession)[ALICE]
    assert again.first_seen_at == created_at
    assert again.last_seen_at >= seen_at
    assert again.kind == "person"
    assert again.entity_id == "alice@acme.example"


def test_a_later_sighting_never_blanks_a_known_label(tmp_path, monkeypatch):
    """Writers see the world partially: a calendar sighting that knows only an
    address must not erase the name HubSpot supplied."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    upsert_entities([EntityStub(key=ALICE, display_name="Alice Smith")])
    upsert_entities([EntityStub(key=ALICE, display_name=None)])

    assert _entities(TestSession)[ALICE].display_name == "Alice Smith"


def test_attrs_merge_rather_than_replace(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    upsert_entities([EntityStub(key=DEAL, attrs={"dealstage": "won"})])
    upsert_entities([EntityStub(key=DEAL, attrs={"amount": "1000"})])

    assert _entities(TestSession)[DEAL].attrs == {
        "dealstage": "won",
        "amount": "1000",
    }


def test_a_malformed_key_is_skipped_not_stored(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    assert upsert_entities([EntityStub(key="no-colon-here")]) == 0
    assert _entities(TestSession) == {}


# ── edges ────────────────────────────────────────────────────────────────────


def _attended(src, dst, **kw):
    return EdgeStub(
        src_key=src, dst_key=dst, rel="attended", source="meeting_participants", **kw
    )


def test_upsert_edges_creates_then_re_observes(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(key=ALICE), EntityStub(key=DOC1)])

    when = datetime(2026, 8, 4)
    assert upsert_edges([_attended(ALICE, DOC1, observed_at=when)]) == 1
    assert upsert_edges([_attended(ALICE, DOC1, observed_at=when)]) == 0

    rows = _edges(TestSession)
    assert len(rows) == 1
    assert rows[0].observed_at == when
    assert rows[0].method == "deterministic"
    assert rows[0].confidence == 1.0


def test_re_observation_revives_an_invalidated_edge(tmp_path, monkeypatch):
    """A relationship that comes back is the same relationship."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(key=ALICE), EntityStub(key=DOC1)])
    upsert_edges([_attended(ALICE, DOC1)])

    replace_edges(DOC1, "attended", "meeting_participants", [], anchor_side="dst")
    assert _edges(TestSession)[0].invalidated_at is not None

    upsert_edges([_attended(ALICE, DOC1)])
    rows = _edges(TestSession)
    assert len(rows) == 1, "revival must not create a second edge"
    assert rows[0].invalidated_at is None


def test_confidence_ratchets_up_and_never_down(tmp_path, monkeypatch):
    """A heuristic sighting must not downgrade a deterministic one."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(key=ALICE), EntityStub(key=ACME)])

    heuristic = EdgeStub(
        src_key=ALICE,
        dst_key=ACME,
        rel="member_of",
        source="s",
        method="heuristic",
        confidence=0.8,
    )
    deterministic = EdgeStub(
        src_key=ALICE,
        dst_key=ACME,
        rel="member_of",
        source="s",
        method="deterministic",
        confidence=1.0,
    )

    upsert_edges([heuristic])
    upsert_edges([deterministic])
    assert _edges(TestSession)[0].confidence == 1.0
    assert _edges(TestSession)[0].method == "deterministic"

    upsert_edges([heuristic])
    assert _edges(TestSession)[0].confidence == 1.0
    assert _edges(TestSession)[0].method == "deterministic"


def test_the_same_claim_from_two_sources_is_two_rows(tmp_path, monkeypatch):
    """Corroboration is information: collapsing sources would throw it away."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(key=ALICE), EntityStub(key=ACME)])

    upsert_edges(
        [
            EdgeStub(ALICE, ACME, "member_of", "hubspot_associations"),
            EdgeStub(ALICE, ACME, "member_of", "domain_heuristic", confidence=0.8),
        ]
    )
    assert len(_edges(TestSession, rel="member_of")) == 2


def test_duplicates_within_one_batch_collapse(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(key=ALICE), EntityStub(key=DOC1)])

    assert upsert_edges([_attended(ALICE, DOC1), _attended(ALICE, DOC1)]) == 1
    assert len(_edges(TestSession)) == 1


# ── replace_edges (set semantics) ────────────────────────────────────────────


def test_replace_edges_invalidates_what_is_gone_and_keeps_the_rest(
    tmp_path, monkeypatch
):
    """Mirrors upsert_participants: the source publishes a whole set each time."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(k) for k in (ALICE, BOB, CARLA, DOC1)])

    created, invalidated = replace_edges(
        DOC1, "attended", "meeting_participants", [ALICE, BOB], anchor_side="dst"
    )
    assert (created, invalidated) == (2, 0)

    # Bob drops off the re-synced attendee list; Carla appears.
    created, invalidated = replace_edges(
        DOC1, "attended", "meeting_participants", [ALICE, CARLA], anchor_side="dst"
    )
    assert (created, invalidated) == (1, 1)

    live = {e.src_key for e in _edges(TestSession) if e.invalidated_at is None}
    gone = {e.src_key for e in _edges(TestSession) if e.invalidated_at is not None}
    assert live == {ALICE, CARLA}
    assert gone == {BOB}, "a dropped attendee is soft-invalidated, never deleted"


def test_replace_edges_leaves_other_rels_and_sources_alone(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(k) for k in (ALICE, DOC1)])
    upsert_edges(
        [
            _attended(ALICE, DOC1),
            EdgeStub(ALICE, DOC1, "mentioned_in", "other_source"),
        ]
    )

    replace_edges(DOC1, "attended", "meeting_participants", [], anchor_side="dst")

    survivors = [e for e in _edges(TestSession) if e.invalidated_at is None]
    assert [e.rel for e in survivors] == ["mentioned_in"]


def test_replace_edges_rejects_a_bad_anchor_side():
    import pytest

    with pytest.raises(ValueError):
        replace_edges(DOC1, "attended", "s", [], anchor_side="sideways")


# ── traversal ────────────────────────────────────────────────────────────────


def _seed_small_graph():
    """alice --attended--> doc1 <--attended-- carla --member_of--> northwind"""
    upsert_entities([EntityStub(k) for k in (ALICE, BOB, CARLA, DOC1, DOC2, ACME)])
    upsert_edges(
        [
            _attended(ALICE, DOC1, observed_at=datetime(2026, 8, 4)),
            _attended(CARLA, DOC1, observed_at=datetime(2026, 8, 4)),
            _attended(BOB, DOC2, observed_at=datetime(2026, 6, 1)),
            EdgeStub(CARLA, ACME, "member_of", "domain_heuristic", confidence=0.8),
        ]
    )


def test_ego_depth_one_returns_only_direct_edges(tmp_path, monkeypatch):
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_small_graph()

    result = ego([ALICE], depth=1)

    assert {(e.src_key, e.dst_key) for e in result.edges} == {(ALICE, DOC1)}
    # The nodes touched come back too, so a caller can label without a second query.
    assert DOC1 in result.nodes and ALICE in result.nodes


def test_ego_depth_two_reaches_a_co_attendee(tmp_path, monkeypatch):
    """The question Phase 0 could not answer: who else was in that room?

    Hops are literal. Alice → doc1 is one; doc1 → Carla is two. Carla's employer
    is a third hop, which is what makes `depth` a real cost control rather than
    a suggestion — so it must NOT appear here.
    """
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_small_graph()

    result = ego([ALICE], depth=2)
    pairs = {(e.src_key, e.dst_key) for e in result.edges}

    assert (CARLA, DOC1) in pairs, "second hop must reach the co-attendee"
    assert (CARLA, ACME) not in pairs, "the co-attendee's employer is a third hop"
    assert (BOB, DOC2) not in pairs, "an unrelated meeting stays out"


def test_ego_depth_three_reaches_the_co_attendees_company(tmp_path, monkeypatch):
    """'Who do we know at Northwind, via meetings Alice was in' — the multi-hop
    question that motivated persisting the graph at all."""
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_small_graph()

    pairs = {(e.src_key, e.dst_key) for e in ego([ALICE], depth=3).edges}

    assert (CARLA, ACME) in pairs


def test_ego_filters_by_rel(tmp_path, monkeypatch):
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_small_graph()

    result = ego([CARLA], rels=["member_of"], depth=1)

    assert {e.rel for e in result.edges} == {"member_of"}


def test_ego_hides_invalidated_edges_unless_asked(tmp_path, monkeypatch):
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_small_graph()
    replace_edges(DOC1, "attended", "meeting_participants", [CARLA], anchor_side="dst")

    assert {e.src_key for e in ego([DOC1], rels=["attended"]).edges} == {CARLA}
    revived = ego([DOC1], rels=["attended"], include_invalidated=True)
    assert {e.src_key for e in revived.edges} == {ALICE, CARLA}


def test_ego_is_bounded_by_the_limit(tmp_path, monkeypatch):
    """A hub node must not be able to pull the whole graph into a prompt."""
    _wire_sqlite(tmp_path, monkeypatch)
    people = [gk.person_key(f"p{i}@acme.example") for i in range(12)]
    upsert_entities([EntityStub(k) for k in [*people, DOC1]])
    upsert_edges([_attended(p, DOC1) for p in people])

    result = ego([DOC1], depth=1, limit=5)

    assert len(result.edges) == 5
    assert result.truncated is True


def test_ego_on_an_unknown_key_is_empty_not_an_error(tmp_path, monkeypatch):
    _wire_sqlite(tmp_path, monkeypatch)
    _seed_small_graph()

    result = ego([gk.person_key("nobody@acme.example")], depth=2)

    assert result.edges == []


# ── the reader gate ──────────────────────────────────────────────────────────


def test_filter_transcripts_for_reader_defers_to_the_participants_acl(
    tmp_path, monkeypatch
):
    """The graph knows every meeting; the reader sees only the ones they were in."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    from datetime import datetime as dt

    from rubik_core.database.models import Asset
    from rubik_core.database.repository import SQLMeetingParticipantsRepository

    with TestSession() as s:
        for doc_id in ("doc-1", "doc-2"):
            s.add(
                Asset(
                    doc_id=doc_id,
                    owner_email="alice@acme.example",
                    status="approved",
                    topic="Meeting Transcripts",
                    created_at=dt(2026, 8, 4),
                    updated_at=dt(2026, 8, 4),
                )
            )
        s.commit()
    repo = SQLMeetingParticipantsRepository()
    repo.upsert_participants("doc-1", meeting_id="m1", emails=["alice@acme.example"])
    repo.upsert_participants("doc-2", meeting_id="m2", emails=["bob@acme.example"])

    kept = filter_transcripts_for_reader("alice@acme.example", [DOC1, DOC2])

    assert kept == [DOC1], "a meeting Alice was not in must not survive the filter"


def test_filter_transcripts_passes_non_transcript_keys_through(tmp_path, monkeypatch):
    """This function speaks only for transcripts; it must not silently drop
    company or person nodes it was never asked to judge."""
    _wire_sqlite(tmp_path, monkeypatch)

    kept = filter_transcripts_for_reader("alice@acme.example", [ALICE, ACME])

    assert kept == [ALICE, ACME]


def test_observed_at_is_the_meeting_date_not_the_write_time(tmp_path, monkeypatch):
    """ "You last met on the 4th" reads observed_at; first_seen_at is when we
    happened to record it, which for a backfill is months later."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    upsert_entities([EntityStub(k) for k in (ALICE, DOC1)])
    meeting_date = datetime.utcnow() - timedelta(days=90)

    upsert_edges([_attended(ALICE, DOC1, observed_at=meeting_date)])

    row = _edges(TestSession)[0]
    assert row.observed_at == meeting_date
    assert row.first_seen_at > meeting_date
