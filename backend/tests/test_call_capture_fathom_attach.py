"""The Fathom fallback — a recording becomes a captured call, exactly once.

Regression cover for ``rubik_core/call_capture/fathom_attach.py``. What is
pinned, and why each would fail silently without a test:

1. **A Fathom recording of a client call becomes a session with a transcript**,
   status ``transcript_ready`` → ``extracted``, a ``wrapup`` commitment, and a
   company bound from the attendee domains. Every downstream surface — the
   wrap-up, the SOW clock, Draft proposal — reads these rows and nothing else.
2. **Replays write nothing twice.** The webhook retries and the manual ingest
   re-lists the same twenty meetings; the second pass must be a no-op on the
   entries AND must not mint a second session.
3. **A call Cubie already captured through Meet is linked, never doubled.**
   Two transcripts of one call would make the extractor read it twice.
4. **What is refused:** an internal-only meeting, a non-MQMM host, a recording
   with no transcript, a confidential session.
5. **Both Fathom transcript shapes are read** (flat ``speaker_name`` and nested
   ``speaker.display_name`` with a clock ``timestamp``), because the client in
   this repo reads one and the API documents the other.
6. **The Proposal Generator sees the result.** ``service.transcript_lines`` on a
   Fathom-sourced session returns cited lines — the whole reason this exists.

Offline: SQLite, the extractor patched by string target, HubSpot domain
matching patched on the module under test.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.call_capture import fathom_attach as fa
from rubik_core.call_capture.provenance import stamp_session_source
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession, CallTranscriptEntry
from rubik_core.database.commitments import Commitment
from rubik_core.database.models import Base, User
from rubik_core.retrieval.hubspot_api import CompanyMatch, DomainMatch

AE = "sage@acme.example"
# The fixture call ended two hours ago: inside every age window, so the
# age tests below are the only ones that move the clock.
from datetime import timedelta as _td  # noqa: E402

START = (datetime.utcnow() - _td(hours=2, minutes=32)).replace(microsecond=0)
END = START + _td(minutes=32)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.database.repository.SessionLocal", Session)
    s = Session()
    s.add(User(id="google-sub-ae", email=AE, name="Sage Lowe", department="Sales"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _flags(monkeypatch, set_test_workspace_domains):
    monkeypatch.setattr(settings, "CCS_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "CCS_FATHOM_ENABLED", True, raising=False)
    set_test_workspace_domains("acme.example")
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, deals),
    )


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    calls: list[str] = []

    def _fake_run_extraction(db, session):
        calls.append(session.id)
        return None

    monkeypatch.setattr(
        "rubik_core.call_capture.extraction.run_extraction", _fake_run_extraction
    )
    return calls


@pytest.fixture(autouse=True)
def _hubspot(monkeypatch):
    """Domain matching answers from a table, never from HubSpot."""
    table = {
        "globexholding.alt": DomainMatch(
            domain="globexholding.alt",
            candidates=(
                CompanyMatch(
                    hubspot_company_id="co-42",
                    name="Globex Holding Company",
                    domain="globexholding.alt",
                ),
            ),
        ),
        "acmecorp.alt": DomainMatch(
            domain="acmecorp.alt",
            candidates=(
                CompanyMatch(
                    hubspot_company_id="co-9", name="Acme", domain="acmecorp.alt"
                ),
            ),
        ),
    }

    def _resolve(domains, *, ae_email=None, client=None):
        return {d: table[d] for d in domains if d in table}

    monkeypatch.setattr(fa, "resolve_domain_matches", _resolve)
    return table


def _meeting(**overrides) -> dict:
    base = {
        "id": 77001,
        "title": "Globex Holding Company <> Acme — Contoso review",
        "share_url": "https://fathom.video/share/abc",
        "recording_start_time": START.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recording_end_time": END.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recorded_by": {
            "email": "Sage@acme.example",
            "name": "Sage Lowe",
            "team": "Sales",
        },
        "calendar_invitees": [
            {
                "email": "sage@acme.example",
                "name": "Sage Lowe",
                "is_external": False,
            },
            {
                "email": "riley@globexholding.alt",
                "name": "Riley Fontaine",
                "is_external": True,
            },
        ],
        "transcript": [
            {"speaker_name": "Sage Lowe", "text": "Thanks for joining."},
            {
                "speaker_name": "Riley Fontaine",
                "text": "We just want someone independent to look at Contoso.",
            },
            {"speaker_name": "Riley Fontaine", "text": ""},  # dropped: empty
            {
                "speaker_name": "Riley Fontaine",
                "text": "Can you send me something in writing?",
            },
        ],
    }
    base.update(overrides)
    return base


def _entries(db, session_id) -> list[CallTranscriptEntry]:
    return (
        db.query(CallTranscriptEntry)
        .filter(CallTranscriptEntry.call_session_id == session_id)
        .order_by(CallTranscriptEntry.sequence)
        .all()
    )


# ── 1. The happy path ────────────────────────────────────────────────────────


def test_a_fathom_client_call_becomes_a_captured_call(db, _no_llm):
    out = fa.attach_meeting(db, _meeting())
    assert out["outcome"] == fa.ATTACHED
    assert out["entries_written"] == 3
    assert out["company_id"] == "co-42"

    session = db.get(CallSession, out["session_id"])
    assert session.origin == "fathom"
    assert session.ae_email == AE  # folded
    assert session.fathom_recording_id == "77001"
    assert session.started_at == START
    assert session.ended_at == END
    assert session.hubspot_company_id == "co-42"
    assert session.attrs["company_name"] == "Globex Holding Company"
    assert session.attrs["fathom"]["title"].startswith("Globex Holding Company")
    # The extractor ran (and, patched, proposed nothing) → the wrap-up is ready.
    assert _no_llm == [session.id]
    assert session.status == "extracted"
    # Coverage is recorded in the Fathom-specific shape, not as "0% covered".
    assert session.attrs["transcript_coverage"]["reason"] == "no_offsets_fathom"

    rows = _entries(db, session.id)
    assert [r.sequence for r in rows] == [1, 2, 3]
    assert all(r.source == "fathom" for r in rows)
    assert rows[1].speaker_name == "Riley Fontaine"
    assert rows[1].speaker_email == "riley@globexholding.alt"  # matched by invitee name
    assert rows[0].external_id == "fathom:77001:1"

    wrapups = db.query(Commitment).filter(Commitment.kind == "wrapup").all()
    assert len(wrapups) == 1 and wrapups[0].owner_email == AE


def test_a_replay_writes_nothing_and_mints_no_second_session(db, _no_llm):
    first = fa.attach_meeting(db, _meeting())
    second = fa.attach_meeting(db, _meeting())
    assert second["outcome"] == fa.REPLAYED
    assert second["session_id"] == first["session_id"]
    assert second["entries_written"] == 0
    assert db.query(CallSession).count() == 1
    assert len(_entries(db, first["session_id"])) == 3
    # The extractor is not re-run on a replay of an already-extracted call.
    assert _no_llm == [first["session_id"]]


@pytest.mark.parametrize("prior_status", ["extracted", "wrapped"])
def test_late_fathom_entries_reextract_an_already_extracted_call(
    db, _no_llm, prior_status
):
    existing = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        origin="calendar",
        status=prior_status,
        wrapped_at=END if prior_status == "wrapped" else None,
        capture_consent="granted",
        started_at=START,
        ended_at=END,
        last_extracted_entry_count=0,
        attrs={"crm_provenance": stamp_session_source(None, None, None)},
    )
    db.add(existing)
    db.commit()

    out = fa.attach_meeting(db, _meeting())
    assert out["outcome"] == fa.ATTACHED
    assert out["session_id"] == existing.id
    assert _no_llm == [existing.id]
    db.refresh(existing)
    assert existing.status == prior_status
    if prior_status == "wrapped":
        assert existing.wrapped_at == END
    assert existing.last_extracted_entry_count == 3

    # Simulate a process killed after committing one new entry but before it
    # could reset the session's status. A replay writes nothing, yet notices
    # that this transcript is newer than the last completed extraction.
    db.add(
        CallTranscriptEntry(
            id=uuid.uuid4().hex,
            call_session_id=existing.id,
            source="fathom",
            external_id="fathom:77001:late",
            speaker_name="Client",
            text="One more agreed action.",
            sequence=4,
        )
    )
    db.commit()
    replay = fa.attach_meeting(db, _meeting())
    assert replay["outcome"] == fa.REPLAYED
    assert replay["entries_written"] == 0
    assert _no_llm == [existing.id, existing.id]
    db.refresh(existing)
    assert existing.last_extracted_entry_count == 4
    assert existing.status == prior_status
    if prior_status == "wrapped":
        assert existing.wrapped_at == END


# ── 2. Matching what Cubie already has ───────────────────────────────────────


def test_it_joins_the_calendar_session_the_app_already_made_for_the_meeting(db):
    existing = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        origin="calendar",
        status="no_transcript",  # arming failed; the Meet path heard nothing
        capture_consent="granted",
        calendar_event_id="evt-1",
        hubspot_company_id="co-42",
        started_at=START - _td(minutes=2),
        attrs={
            "company_name": "Globex Holding Company",
            "crm_provenance": stamp_session_source("co-42", None, None),
        },
    )
    db.add(existing)
    db.commit()

    out = fa.attach_meeting(db, _meeting())
    assert out["outcome"] == fa.ATTACHED
    assert out["session_id"] == existing.id
    assert db.query(CallSession).count() == 1
    db.refresh(existing)
    assert existing.fathom_recording_id == "77001"
    assert existing.status == "extracted"
    assert existing.ended_at == END  # learned from Fathom


def test_a_call_meet_already_captured_is_linked_not_doubled(db, _no_llm):
    existing = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        origin="calendar",
        status="extracted",
        capture_consent="granted",
        started_at=START + _td(minutes=1),
        ended_at=END - _td(minutes=2),
        attrs={"crm_provenance": stamp_session_source(None, None, None)},
    )
    db.add(existing)
    db.flush()
    db.add(
        CallTranscriptEntry(
            id=uuid.uuid4().hex,
            call_session_id=existing.id,
            source="meet",
            external_id="conferenceRecords/x/transcripts/y/entries/1",
            speaker_name="Sage Lowe",
            text="Thanks for joining.",
            sequence=1,
        )
    )
    db.commit()

    out = fa.attach_meeting(db, _meeting())
    assert out["outcome"] == fa.LINKED_EXISTING_TRANSCRIPT
    assert out["session_id"] == existing.id
    assert [r.source for r in _entries(db, existing.id)] == ["meet"]
    db.refresh(existing)
    assert existing.fathom_recording_id == "77001"
    assert _no_llm == []  # nothing new to read


def test_a_session_far_from_the_recording_is_not_the_same_call(db):
    other = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        origin="calendar",
        status="no_transcript",
        capture_consent="granted",
        started_at=START - _td(hours=6),  # six hours earlier
    )
    db.add(other)
    db.commit()
    out = fa.attach_meeting(db, _meeting())
    assert out["session_id"] != other.id
    assert db.query(CallSession).count() == 2


# ── 3. What is refused ───────────────────────────────────────────────────────


def test_internal_only_meetings_are_skipped(db):
    meeting = _meeting(
        calendar_invitees=[
            {"email": "sage@acme.example", "is_external": False},
            {"email": "ifoster@acme.example", "is_external": False},
        ]
    )
    assert fa.attach_meeting(db, meeting)["outcome"] == fa.SKIPPED_INTERNAL_ONLY
    assert db.query(CallSession).count() == 0


def test_a_recording_by_an_outside_host_is_skipped(db):
    meeting = _meeting(recorded_by={"email": "someone@otherfirm.com"})
    assert fa.attach_meeting(db, meeting)["outcome"] == fa.FATHOM_SKIP_HOST_OUTSIDE
    assert db.query(CallSession).count() == 0


def test_no_verified_domains_refuses_known_host(db, set_test_workspace_domains):
    set_test_workspace_domains()
    assert db.query(User).filter(User.email == AE).first() is not None
    assert fa._host_is_ours(db, AE) is False
    assert fa.attach_meeting(db, _meeting())["outcome"] == fa.FATHOM_SKIP_HOST_OUTSIDE
    assert db.query(CallSession).count() == 0


def test_secondary_domain_colleague_is_internal_even_if_fathom_marks_external(
    db, set_test_workspace_domains
):
    set_test_workspace_domains("acme.example", "subsidiary.alt")
    meeting = _meeting(
        calendar_invitees=[
            {"email": AE, "is_external": False},
            {"email": "teammate@subsidiary.alt", "is_external": True},
        ]
    )
    assert fa.external_attendees(meeting, host=AE) == []
    assert fa.attach_meeting(db, meeting)["outcome"] == fa.SKIPPED_INTERNAL_ONLY
    assert db.query(CallSession).count() == 0


def test_no_transcript_and_no_id_are_skipped(db):
    assert (
        fa.attach_meeting(db, _meeting(transcript=[]))["outcome"]
        == fa.SKIPPED_NO_TRANSCRIPT
    )
    assert fa.attach_meeting(db, _meeting(id=None))["outcome"] == fa.SKIPPED_NO_ID
    assert db.query(CallSession).count() == 0


def test_a_confidential_session_gets_linked_but_no_transcript(db, _no_llm):
    existing = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        origin="calendar",
        status="ended",
        capture_consent="suppressed",
        started_at=START,
        attrs={"crm_provenance": stamp_session_source(None, None, None)},
    )
    db.add(existing)
    db.commit()
    out = fa.attach_meeting(db, _meeting())
    assert out["outcome"] == fa.SUPPRESSED
    assert out["session_id"] == existing.id
    assert _entries(db, existing.id) == []
    assert _no_llm == []


@pytest.mark.parametrize("legacy", [False, True])
def test_existing_unowned_or_unproved_session_is_not_linked(
    db, _no_llm, monkeypatch, legacy
):
    existing = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        origin="calendar",
        status="ended",
        capture_consent="suppressed",
        hubspot_company_id="co-42",
        hubspot_deal_id="deal-outside",
        started_at=START,
        attrs={
            "company_name": "Former client",
            **(
                {}
                if legacy
                else {
                    "crm_provenance": stamp_session_source(
                        "co-42", None, "deal-outside"
                    )
                }
            ),
        },
    )
    db.add(existing)
    db.commit()
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, set()),
    )

    out = fa.attach_meeting(db, _meeting())
    db.refresh(existing)
    assert out["outcome"] == fa.FAILED
    assert out["session_id"] is None
    assert existing.fathom_recording_id is None
    assert _entries(db, existing.id) == []
    assert _no_llm == []


def test_unowned_domain_match_never_stamps_a_company_name(db, monkeypatch):
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (set(), contacts, deals),
    )

    out = fa.attach_meeting(db, _meeting())
    session = db.get(CallSession, out["session_id"])
    assert out["outcome"] == fa.ATTACHED
    assert out["company_id"] is None
    assert session.hubspot_company_id is None
    assert "company_name" not in session.attrs


def test_the_kill_switch_stops_it_before_anything_is_read(db, monkeypatch):
    monkeypatch.setattr(settings, "CCS_FATHOM_ENABLED", False, raising=False)
    assert fa.attach_meeting(db, _meeting())["outcome"] == fa.SKIPPED_DISABLED
    assert db.query(CallSession).count() == 0


# ── 4. Company binding ───────────────────────────────────────────────────────


def test_two_clients_on_one_call_leaves_the_company_for_the_wrap_up_to_ask(db):
    meeting = _meeting(
        calendar_invitees=[
            {"email": "sage@acme.example", "is_external": False},
            {"email": "riley@globexholding.alt", "is_external": True},
            {"email": "wile@acmecorp.alt", "is_external": True},
        ]
    )
    out = fa.attach_meeting(db, meeting)
    assert out["outcome"] == fa.ATTACHED
    assert out["company_id"] is None
    assert db.get(CallSession, out["session_id"]).hubspot_company_id is None


def test_a_personal_address_is_external_but_never_matched(db):
    meeting = _meeting(
        calendar_invitees=[
            {"email": "sage@acme.example"},
            {"email": "riley.f@gmail.com"},  # no is_external flag: domain rule decides
        ]
    )
    out = fa.attach_meeting(db, meeting)
    assert out["outcome"] == fa.ATTACHED  # still a client call
    assert out["company_id"] is None  # gmail.com is never a company


# ── 5. Transcript shapes ─────────────────────────────────────────────────────


def test_the_nested_api_shape_and_clock_timestamps_are_read(db):
    meeting = _meeting(
        transcript=[
            {
                "speaker": {
                    "display_name": "Riley Fontaine",
                    "matched_calendar_invitee_email": "Riley@GlobexHolding.alt",
                },
                "text": "We   need this  for our investors.",
                "timestamp": "00:01:05",
            },
            {
                "speaker": {"display_name": "Sage Lowe"},
                "text": "Understood.",
                "timestamp": "1:12:30.5",
            },
        ]
    )
    out = fa.attach_meeting(db, meeting)
    rows = _entries(db, out["session_id"])
    assert rows[0].text == "We need this for our investors."
    assert rows[0].speaker_email == "riley@globexholding.alt"
    assert rows[0].start_offset_ms == 65_000
    assert rows[1].start_offset_ms == (72 * 60 + 30) * 1000 + 500
    # With offsets present the coverage verdict is the ordinary one.
    session = db.get(CallSession, out["session_id"])
    assert session.attrs["transcript_coverage"]["reason"] != "no_offsets_fathom"


def test_the_window_falls_back_to_scheduled_times_and_duration(db):
    meeting = _meeting(
        recording_start_time=None,
        recording_end_time=None,
        scheduled_start_time=(START - _td(hours=7)).strftime("%Y-%m-%dT%H:%M:%S-07:00"),
        duration_seconds=1800,
    )
    out = fa.attach_meeting(db, meeting)
    session = db.get(CallSession, out["session_id"])
    assert session.started_at == START  # -07:00 folded into naive UTC
    assert session.ended_at == START + _td(minutes=30)


# ── 6. Downstream ────────────────────────────────────────────────────────────


def test_the_proposal_generator_reads_a_fathom_sourced_call(db):
    from rubik_core.proposals import service

    out = fa.attach_meeting(db, _meeting())
    lines = service.transcript_lines(db, out["session_id"])
    assert (
        lines[1]
        == "[2] Riley Fontaine: We just want someone independent to look at Contoso."
    )


def test_attach_meetings_reports_a_histogram(db):
    summary = fa.attach_meetings(
        db,
        [
            _meeting(),
            _meeting(),  # replay of the first
            _meeting(id=77002, transcript=[]),
            _meeting(id=77003, recorded_by={"email": "x@elsewhere.com"}),
        ],
    )
    assert summary == {
        fa.ATTACHED: 1,
        fa.REPLAYED: 1,
        fa.SKIPPED_NO_TRANSCRIPT: 1,
        fa.FATHOM_SKIP_HOST_OUTSIDE: 1,
    }


def test_the_sync_hook_is_wired_before_the_rag_skip():
    """``process_fathom_transcripts_background`` must attach BEFORE its
    already-ingested ``continue`` — otherwise backfill never happens."""
    import inspect

    from backend.routers import fathom as router

    src = inspect.getsource(router.process_fathom_transcripts_background)
    assert src.index("_attach_to_call_capture(meeting)") < src.index(
        "already ingested, skipping"
    )
    with patch("rubik_core.call_capture.fathom_attach.attach_meeting") as attach:
        attach.return_value = {"outcome": "attached", "recording_id": "1"}
        router._attach_to_call_capture({"id": 1})
        assert attach.called


# ── 7. Age rules ─────────────────────────────────────────────────────────────


def test_an_old_recording_is_not_attached_at_all(db, monkeypatch):
    monkeypatch.setattr(settings, "CCS_FATHOM_MAX_AGE_DAYS", 14, raising=False)
    meeting = _meeting(
        recording_start_time="2026-06-01T15:00:00Z",
        recording_end_time="2026-06-01T15:30:00Z",
    )
    assert fa.attach_meeting(db, meeting)["outcome"] == fa.SKIPPED_TOO_OLD
    assert db.query(CallSession).count() == 0


def test_a_recent_but_not_fresh_recording_gets_a_transcript_and_no_wrapup_clock(
    db, monkeypatch, _no_llm
):
    """Five days old: inside the attach window, outside the 48h clock window.
    The proposal is still worth drafting; nagging the AE about a wrap-up for
    a call everybody has moved past is not."""
    from datetime import timedelta

    monkeypatch.setattr(settings, "CCS_FATHOM_MAX_AGE_DAYS", 14, raising=False)
    start = datetime.utcnow() - timedelta(days=5)
    end = start + timedelta(minutes=30)
    meeting = _meeting(
        recording_start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        recording_end_time=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    out = fa.attach_meeting(db, meeting)
    assert out["outcome"] == fa.ATTACHED
    assert _no_llm == [out["session_id"]]  # extracted
    assert db.query(Commitment).filter(Commitment.kind == "wrapup").count() == 0


def test_a_fresh_recording_opens_the_wrapup_clock(db):
    from datetime import timedelta

    start = datetime.utcnow() - timedelta(hours=3)
    end = start + timedelta(minutes=30)
    meeting = _meeting(
        recording_start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        recording_end_time=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    fa.attach_meeting(db, meeting)
    assert db.query(Commitment).filter(Commitment.kind == "wrapup").count() == 1
