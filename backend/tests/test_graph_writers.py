"""The hooks that fill the graph from flows that were happening anyway.

The most important test in this file is
`test_a_broken_graph_writer_still_leaves_the_acl_correct`. The attendance writer
hangs off `upsert_participants`, which maintains the authoritative transcript
ACL — the table that decides who may read a meeting. A decoration on that write
must be incapable of damaging it: no exception may escape, and the participant
rows must be identical whether the graph write succeeded, failed, or was
switched off.

The rest pin the writers' honesty: they record only what the caller already
fetched (no owner edge without a real owner address), they mirror removals
rather than accumulating phantom attendance, and a domain that says nothing
about employment never becomes a company.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from rubik_core.graph import keys as gk

HOST = "host@acme.example"
GUEST = "carla@northwindholding.alt"
FREELANCER = "someone@gmail.com"


def _wire_sqlite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/w.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    return TestSession


def _participants_repo():
    """Resolved at call time — other suites importlib.reload this module."""
    from rubik_core.database import repository

    return repository.SQLMeetingParticipantsRepository()


def _seed_transcript(TestSession, doc_id="doc-1", when=datetime(2026, 8, 4)):
    from rubik_core.database.models import Asset

    with TestSession() as s:
        s.add(
            Asset(
                doc_id=doc_id,
                owner_email=HOST,
                status="approved",
                topic="Meeting Transcripts",
                original_filename="Acme / Northwind QBR",
                created_at=when,
                updated_at=when,
            )
        )
        s.commit()


def _edges(TestSession, rel=None, live_only=True):
    from rubik_core.database.models import GraphEdge

    with TestSession() as s:
        q = s.query(GraphEdge)
        if rel:
            q = q.filter(GraphEdge.rel == rel)
        if live_only:
            q = q.filter(GraphEdge.invalidated_at.is_(None))
        return q.all()


# ── attendance, through the real choke point ─────────────────────────────────


def test_upserting_participants_records_attendance_in_the_graph(tmp_path, monkeypatch):
    """Written through upsert_participants itself — the single choke point every
    ingest path already uses, so a fifth ingest route cannot bypass the graph."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession)

    _participants_repo().upsert_participants(
        "doc-1", meeting_id="m-1", emails=[HOST, GUEST]
    )

    attended = _edges(TestSession, rel="attended")
    assert {e.src_key for e in attended} == {gk.person_key(HOST), gk.person_key(GUEST)}
    assert {e.dst_key for e in attended} == {gk.transcript_key("doc-1")}
    edge = attended[0]
    assert edge.source == "meeting_participants"
    assert edge.method == "deterministic"
    assert edge.confidence == 1.0
    assert edge.evidence_ref == "doc-1"
    # The meeting date, taken from the Asset — not the moment we wrote the row.
    assert edge.observed_at == datetime(2026, 8, 4)


def test_the_transcript_node_carries_its_title_not_its_content(tmp_path, monkeypatch):
    """Metadata only: a title and an id, never a line of the transcript."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession)

    _participants_repo().upsert_participants("doc-1", meeting_id="m-1", emails=[HOST])

    from rubik_core.database.models import GraphEntity

    with TestSession() as s:
        node = s.get(GraphEntity, gk.transcript_key("doc-1"))
    assert node.display_name == "Acme / Northwind QBR"
    assert node.attrs == {"meeting_id": "m-1"}


def test_a_removed_attendee_is_invalidated_not_deleted(tmp_path, monkeypatch):
    """The ACL replaces the whole set on re-sync; the graph mirrors that, but
    keeps 'they used to be listed' rather than erasing it."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession)
    repo = _participants_repo()

    repo.upsert_participants("doc-1", meeting_id="m-1", emails=[HOST, GUEST])
    repo.upsert_participants("doc-1", meeting_id="m-1", emails=[HOST])

    live = _edges(TestSession, rel="attended")
    every = _edges(TestSession, rel="attended", live_only=False)
    assert {e.src_key for e in live} == {gk.person_key(HOST)}
    assert len(every) == 2, "the dropped attendee's edge is retained, invalidated"


def test_attendance_also_places_people_at_their_companies(tmp_path, monkeypatch):
    """Without this the graph is bipartite people↔meetings and no question about
    a company can be answered from transcripts alone."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession)

    _participants_repo().upsert_participants(
        "doc-1", meeting_id="m-1", emails=[HOST, GUEST, FREELANCER]
    )

    member_of = _edges(TestSession, rel="member_of")
    assert {(e.src_key, e.dst_key) for e in member_of} == {
        (gk.person_key(HOST), gk.company_key("acme.example")),
        (gk.person_key(GUEST), gk.company_key("northwindholding.alt")),
    }
    assert all(e.method == "heuristic" for e in member_of)
    assert all(e.confidence == 0.8 for e in member_of), (
        "a domain implies employment only probably; it must not read as certain"
    )


def test_a_consumer_mail_host_never_becomes_a_company(tmp_path, monkeypatch):
    """gmail.com as a company node would merge every unrelated freelancer into
    one fictitious organization."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession)

    _participants_repo().upsert_participants(
        "doc-1", meeting_id="m-1", emails=[FREELANCER]
    )

    from rubik_core.database.models import GraphEntity

    with TestSession() as s:
        companies = s.query(GraphEntity).filter(GraphEntity.kind == "company").all()
    assert companies == []


# ── the writer must never endanger the ACL ───────────────────────────────────


def test_a_broken_graph_writer_still_leaves_the_acl_correct(tmp_path, monkeypatch):
    """The load-bearing safety property: meeting_participants is the ACL that
    decides who may read a transcript. A graph bug must not cost anyone their
    access row, and must not raise into the ingest path."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession)

    with patch(
        "rubik_core.graph.writers.upsert_entities",
        side_effect=RuntimeError("graph table on fire"),
    ):
        _participants_repo().upsert_participants(
            "doc-1", meeting_id="m-1", emails=[HOST, GUEST]
        )

    assert sorted(_participants_repo().list_participants_for_doc("doc-1")) == sorted(
        [HOST, GUEST]
    )
    assert _edges(TestSession) == []


def test_the_kill_switch_stops_graph_writes_but_not_the_acl(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession)

    with patch("rubik_core.config.settings.CUBIE_GRAPH_WRITES_ENABLED", False):
        _participants_repo().upsert_participants(
            "doc-1", meeting_id="m-1", emails=[HOST]
        )

    assert _participants_repo().list_participants_for_doc("doc-1") == [HOST]
    assert _edges(TestSession) == []


# ── HubSpot bundle write-through ─────────────────────────────────────────────


def _record_bundle(**overrides):
    from rubik_core.graph.writers import record_company_bundle

    kwargs = {
        "company_id": "999",
        "company_name": "Northwind",
        "company_domain": "northwindholding.alt",
        "deals": [
            {"id": "55", "dealname": "Renewal", "hubspot_owner_id": "7", "amount": "1"}
        ],
        "contacts": [
            {"id": "11", "email": GUEST, "firstname": "Carla", "lastname": "Ruiz"}
        ],
    }
    kwargs.update(overrides)
    positional = (
        kwargs.pop("company_id"),
        kwargs.pop("company_name"),
        kwargs.pop("company_domain"),
        kwargs.pop("deals"),
        kwargs.pop("contacts"),
    )
    record_company_bundle(*positional, **kwargs)


def test_a_company_bundle_writes_contacts_and_deals(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    _record_bundle()

    pairs = {(e.rel, e.src_key, e.dst_key) for e in _edges(TestSession)}
    assert (
        "has_contact",
        gk.company_key("northwindholding.alt"),
        gk.person_key(GUEST),
    ) in pairs
    assert (
        "has_deal",
        gk.company_key("northwindholding.alt"),
        gk.deal_key("55"),
    ) in pairs
    # A HubSpot association is stronger evidence than the address domain.
    membership = [e for e in _edges(TestSession, rel="member_of")]
    assert all(e.method == "deterministic" for e in membership)


def test_no_owner_edge_without_a_real_owner_address(tmp_path, monkeypatch):
    """X-Ray holds an id→NAME map, not emails. Rather than guess an address the
    writer omits the edge — a wrong person on a deal is worse than no person."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    _record_bundle()
    assert _edges(TestSession, rel="owned_by") == []

    _record_bundle(owner_emails={"7": "rep@acme.example"})
    owned = _edges(TestSession, rel="owned_by")
    assert [(e.src_key, e.dst_key) for e in owned] == [
        (gk.deal_key("55"), gk.person_key("rep@acme.example"))
    ]


def test_a_company_without_a_domain_is_skipped_entirely(tmp_path, monkeypatch):
    """COMPANY nodes are keyed on domain. Falling back to the HubSpot id would
    create a second identity for an organization the graph already knows."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)

    _record_bundle(company_domain="")

    assert _edges(TestSession) == []


def test_the_bundle_write_never_breaks_an_xray_run(tmp_path, monkeypatch):
    _wire_sqlite(tmp_path, monkeypatch)

    with patch(
        "rubik_core.graph.writers.upsert_entities", side_effect=RuntimeError("boom")
    ):
        _record_bundle()  # must not raise


# ── backfill ─────────────────────────────────────────────────────────────────


def test_backfill_rebuilds_attendance_and_is_idempotent(tmp_path, monkeypatch):
    """Without the sweep the graph only knows meetings ingested after deploy."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession, "doc-1")
    _seed_transcript(TestSession, "doc-2", when=datetime(2026, 6, 1))

    # Populate the ACL with the graph writer disabled, simulating history that
    # predates the feature.
    with patch("rubik_core.config.settings.CUBIE_GRAPH_WRITES_ENABLED", False):
        repo = _participants_repo()
        repo.upsert_participants("doc-1", meeting_id="m-1", emails=[HOST, GUEST])
        repo.upsert_participants("doc-2", meeting_id="m-2", emails=[HOST])
    assert _edges(TestSession) == []

    from rubik_core.graph.backfill import backfill_attendance

    stats = backfill_attendance()
    assert stats["transcripts"] == 2
    assert stats["participants"] == 3
    assert len(_edges(TestSession, rel="attended")) == 3

    # Re-running re-observes rather than duplicating.
    again = backfill_attendance()
    assert again["edges_created"] == 0
    assert len(_edges(TestSession, rel="attended")) == 3


def test_backfill_respects_the_limit(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(TestSession, "doc-1")
    _seed_transcript(TestSession, "doc-2", when=datetime(2026, 6, 1))
    with patch("rubik_core.config.settings.CUBIE_GRAPH_WRITES_ENABLED", False):
        repo = _participants_repo()
        repo.upsert_participants("doc-1", meeting_id="m-1", emails=[HOST])
        repo.upsert_participants("doc-2", meeting_id="m-2", emails=[HOST])

    from rubik_core.graph.backfill import backfill_attendance

    assert backfill_attendance(limit=1)["transcripts"] == 1
