"""The CCS polling sweep — what it refuses to ingest, and what it never loses.

Regression cover for ``rubik_core/call_capture/sweep.py`` and the completion
bookkeeping in ``rubik_core/call_capture/transcript_ingest.py``. Three defects
are pinned here, each of which fails silently in production — the sweep reports
success in all three, which is what makes them expensive:

1. **A live call must never be ingested.** A conference record with no
   ``endTime`` is a call in progress and its transcript is a growing prefix.
   Ingesting at minute 20 of a 50-minute call stores the first 20 minutes, marks
   the session ``transcript_ready``, extracts from the fragment — and every
   later pass sees the work done. The SOW ask in the last half hour is never
   read, and Google deletes the source after 30 days.
2. **"Has entries" is not "was ingested."** The three ingest writes are ordered
   and the first commits before the other two start, so a Vertex import that
   times out leaves a full citation index, no corpus document and no ACL row.
   Gating recovery on entry rows reports that call as ``already_ingested``
   for ever, with ``records_failed: 0``.
3. **Somebody has to open the ``wrapup`` commitment.** The soft block is
   wired end to end downstream — aging, breach, Harper's
   worklist, escalation — and is worth exactly nothing without a producer.

Everything runs offline on SQLite: the Meet client is a fixture, and the DAM
registry, the participants repository and the RAG processor are patched at their
string targets (they are imported lazily inside the function under test, so an
object-target patch would miss).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.call_capture import sweep as ccs_sweep
from rubik_core.call_capture import transcript_ingest
from rubik_core.config import settings
from rubik_core.database.call_capture import (
    CallExtraction,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.database.commitments import Commitment
from rubik_core.database.models import Base, Role, User
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT

# Imported HERE, at collection time, on purpose — do not move this into a
# fixture or let it be reached lazily.
#
# `transcript_sync` binds `DAMRegistry`, `SQLMeetingParticipantsRepository` and
# `SessionLocal` with `from ... import X` at ITS import time. The ingest path
# this suite exercises imports it lazily, so without this line the module is
# first imported in the middle of a test, WHILE the `corpus` fakes are
# installed — and it permanently captures the fakes. monkeypatch then restores
# only the modules it patched (`dam.registry`, `database.repository`), never
# the module that copied the value out of them, so the fakes leak out of this
# file forever. That is what broke six attendance-gating tests in
# `test_meet_transcripts.py` when it ran after this suite: each passed alone
# and failed in the suite, because `run_sync` was writing the ACL into a stale
# in-memory session that had gone out of scope.
#
# Importing first means it captures the REAL objects, and `corpus` then patches
# its attributes explicitly below, which monkeypatch does restore.
from rubik_core.scripts import transcript_sync as _transcript_sync  # noqa: E402

AE = "ae@acme.example"
COMPANY = "7701"
DEAL = "8801"

RECORD = "conferenceRecords/rec-1"
SPACE = "spaces/space-1"
TRANSCRIPT = "conferenceRecords/rec-1/transcripts/t-1"

#: The line the AE is paid to act on, and the one a mid-call ingest loses: it is
#: said at minute 44 of a 50-minute call.
SOW_ASK = "Send us a SOW for the platform onboarding engagement by Friday please."


def _utc(*args) -> datetime:
    """A naive-UTC instant, which is the only datetime shape this schema holds."""
    return datetime(*args)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # mandatory for :memory:
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # Patched by string target: several suites importlib.reload() the db module
    # mid-run, which leaves an object-target patch on a stale module attribute.
    #
    # ALL THREE names, because each module that did `from ... import SessionLocal`
    # holds its own reference and patching the `db` module alone does not reach
    # any of them. `repository.py:13` is one such module, and the transcript
    # ingest this suite exercises reaches the participants ACL through it, so a
    # single patch left that write pointing somewhere this fixture does not own.
    # `test_meet_transcripts._wire_sqlite` rebinds the same set for the same
    # reason — follow it rather than inventing a narrower version.
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.database.repository.SessionLocal", Session)
    if hasattr(_transcript_sync, "SessionLocal"):
        monkeypatch.setattr(_transcript_sync, "SessionLocal", Session)
    s = Session()
    # Who is swept is the Sales Call Capture roster in Feature Access; it ships
    # open to everyone, so the department here is arbitrary.
    s.add(User(id="google-sub-ae", email=AE, name="Test AE", department="Systems"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_feature_cache():
    """The roster cache is process-wide; an override written in one test must
    not outlive the in-memory database it was written to."""
    from rubik_core.features import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "CCS_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", True, raising=False)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """The extractor is one Gemini PRO call. Never in a unit test."""
    calls: list[str] = []

    def _fake_run_extraction(db, session):
        calls.append(session.id)
        return None  # "the extractor ran and proposed nothing"

    monkeypatch.setattr(
        "rubik_core.call_capture.extraction.run_extraction", _fake_run_extraction
    )
    return calls


@pytest.fixture
def corpus(monkeypatch):
    """Fakes for the DAM asset, the participant ACL and the RAG import.

    ``state["mode"]`` drives write 2: ``"ok"``, ``"fail"`` (the processor returns
    an unsuccessful result — a rejected import) or ``"raise"`` (it throws, which
    is what a Vertex timeout actually looks like).
    """
    state: dict = {"mode": "ok", "assets": {}, "participants": [], "processed": []}

    class _Registry:
        def get_asset(self, doc_id):
            row = state["assets"].get(doc_id)
            return SimpleNamespace(**row) if row else None

        def register_asset(self, asset):
            state["assets"][asset.doc_id] = {
                "doc_id": asset.doc_id,
                "status": asset.status,
                "allowed_emails": list(asset.allowed_emails or []),
            }

        def update_asset(self, doc_id, fields):
            state["assets"].setdefault(doc_id, {"doc_id": doc_id}).update(fields)

    class _Participants:
        def upsert_participants(self, doc_id, *, meeting_id, emails):
            state["participants"].append((doc_id, meeting_id, tuple(emails)))

    class _Processor:
        def process_file(self, path, manual_metadata=None, target_corpus_id=None):
            state["processed"].append(path.name)
            if state["mode"] == "raise":
                raise TimeoutError("Vertex RAG import timed out")
            return {
                "result": SimpleNamespace(
                    success=state["mode"] == "ok", error="import rejected"
                )
            }

    monkeypatch.setattr("rubik_core.dam.registry.DAMRegistry", _Registry)
    monkeypatch.setattr(
        "rubik_core.database.repository.SQLMeetingParticipantsRepository", _Participants
    )
    # And on transcript_sync's OWN copies. It captured these by value at import
    # time (see the module-level import above), so patching the source modules
    # alone does not reach the code path under test — and these setattrs, unlike
    # an import-time capture, are restored at teardown.
    monkeypatch.setattr(_transcript_sync, "DAMRegistry", _Registry)
    monkeypatch.setattr(
        _transcript_sync, "SQLMeetingParticipantsRepository", _Participants
    )
    monkeypatch.setattr(
        "rubik_core.scripts.transcript_sync._build_processor", lambda: _Processor()
    )
    monkeypatch.setattr(
        "rubik_core.scripts.transcript_sync._resolve_meetings_corpus_id",
        lambda: "meetings-corpus",
    )
    return state


class FakeMeetClient:
    """The three read methods the sweep and the ingest use, plus a call log."""

    def __init__(self, records, transcripts=None, entries=None):
        self.service = object()  # not None: a real client would have a service
        self.records = records
        self.transcripts = transcripts or {}
        self.entries = entries or {}
        self.listed_transcripts: list[str] = []
        self.listed_entries: list[str] = []

    def list_conference_records(self, start_after=None):
        return list(self.records)

    def list_transcripts(self, conference_record):
        self.listed_transcripts.append(conference_record)
        return list(self.transcripts.get(conference_record, []))

    def list_transcript_entries(self, transcript_name):
        self.listed_entries.append(transcript_name)
        return list(self.entries.get(transcript_name, []))

    def list_participants(self, conference_record):
        return []


def _install(monkeypatch, client: FakeMeetClient) -> FakeMeetClient:
    monkeypatch.setattr(
        ccs_sweep, "GoogleMeetClient", lambda user_email=None, **kw: client
    )
    return client


def _record(*, ended: str | None) -> dict:
    """One conferenceRecords.list row. ``endTime`` absent means STILL LIVE."""
    row = {
        "name": RECORD,
        "space": {"name": SPACE, "meetingCode": "abc-defg-hij"},
        "startTime": "2026-08-26T15:00:00Z",
    }
    if ended:
        row["endTime"] = ended
    return row


def _entries(count: int, *, tail: str | None = None) -> list[dict]:
    """A transcript long enough to clear the 500-char corpus floor."""
    rows = []
    for i in range(count):
        rows.append(
            {
                "name": f"{TRANSCRIPT}/entries/e-{i}",
                "participant": None,
                "text": (
                    f"Line {i}: we walked through the onboarding scope, the "
                    "training cadence and what the rollout calendar "
                    "looks like for the next two quarters."
                ),
                "languageCode": "en-US",
                "startTime": f"2026-08-26T15:{i:02d}:00Z",
                "endTime": f"2026-08-26T15:{i:02d}:30Z",
            }
        )
    if tail:
        rows.append(
            {
                "name": f"{TRANSCRIPT}/entries/e-tail",
                "participant": None,
                "text": tail,
                "languageCode": "en-US",
                "startTime": "2026-08-26T15:44:00Z",
                "endTime": "2026-08-26T15:44:20Z",
            }
        )
    return rows


def _session(db, **overrides) -> CallSession:
    row = CallSession(
        id=uuid.uuid4().hex,
        ae_email=overrides.pop("ae_email", AE),
        origin=CALL_ORIGIN_ASSISTANT,
        status=overrides.pop("status", "in_progress"),
        capture_consent=overrides.pop("capture_consent", "granted"),
        hubspot_company_id=overrides.pop("hubspot_company_id", COMPANY),
        hubspot_deal_id=overrides.pop("hubspot_deal_id", DEAL),
        meet_space_name=overrides.pop("meet_space_name", SPACE),
        conference_record=overrides.pop("conference_record", RECORD),
        started_at=overrides.pop("started_at", _utc(2026, 8, 26, 15, 0)),
        ended_at=overrides.pop("ended_at", None),
        **overrides,
    )
    db.add(row)
    db.commit()
    return row


def _sweep(db, **kw) -> dict:
    return ccs_sweep.sweep_user(db, AE, lookback_hours=24, **kw)


def _wrapups(db) -> list[Commitment]:
    return db.query(Commitment).filter(Commitment.kind == "wrapup").all()


def _extraction_report() -> dict[str, int]:
    return {
        "extractions_run": 0,
        "extractions_empty": 0,
        "extractions_failed": 0,
        "extractions_deferred": 0,
    }


def test_second_session_cannot_spend_on_the_same_call(db, monkeypatch):
    call = _session(db, status="transcript_ready")
    first = _extraction_report()
    second = _extraction_report()
    model_calls = []

    def model(_db, session):
        model_calls.append(session.id)
        # The first worker has committed its claim before model I/O. Another
        # database session represents a sweep or AE retry on another instance.
        contender = sessionmaker(bind=db.get_bind())()
        try:
            same_call = contender.get(CallSession, call.id)
            ccs_sweep._run_extraction(contender, same_call, second, False)
        finally:
            contender.close()
        return None

    monkeypatch.setattr("rubik_core.call_capture.extraction.run_extraction", model)
    ccs_sweep._run_extraction(db, call, first, False)

    assert model_calls == [call.id]
    assert first["extractions_empty"] == 1
    assert second["extractions_deferred"] == 1
    assert call.status == "extracted"
    assert call.extraction_claimed_until is None


def test_unexpired_claim_defers_and_expired_claim_recovers(db, _no_llm):
    call = _session(db, status="transcript_ready")
    call.extraction_claimed_until = datetime.utcnow() + timedelta(minutes=10)
    db.commit()
    deferred = _extraction_report()
    ccs_sweep._run_extraction(db, call, deferred, False)
    assert deferred["extractions_deferred"] == 1
    assert _no_llm == []

    call.extraction_claimed_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    recovered = _extraction_report()
    ccs_sweep._run_extraction(db, call, recovered, False)
    assert recovered["extractions_empty"] == 1
    assert _no_llm == [call.id]
    assert call.extraction_claimed_until is None


def test_manual_retry_cooldown_is_atomic_and_sweep_remains_eligible(db, monkeypatch):
    call = _session(db, status="transcript_ready")
    model_calls = []

    def failed(_db, session):
        model_calls.append(session.id)
        raise RuntimeError("model refused")

    monkeypatch.setattr("rubik_core.call_capture.extraction.run_extraction", failed)
    initial = _extraction_report()
    ccs_sweep._run_extraction(db, call, initial, False, manual_retry=True)
    assert initial["extractions_failed"] == 1
    assert call.extraction_manual_retry_after > datetime.utcnow()
    assert call.extraction_claimed_until is None

    double_click = _extraction_report()
    ccs_sweep._run_extraction(db, call, double_click, False, manual_retry=True)
    assert double_click["extractions_deferred"] == 1
    assert model_calls == [call.id]

    scheduled = _extraction_report()
    ccs_sweep._run_extraction(db, call, scheduled, False)
    assert scheduled["extractions_failed"] == 1
    assert model_calls == [call.id, call.id]


def test_new_entries_during_analysis_discard_stale_output(db, monkeypatch):
    call = _session(db, status="transcript_ready")
    db.add(
        CallTranscriptEntry(
            id=uuid.uuid4().hex,
            call_session_id=call.id,
            source="meet",
            external_id="entry-initial",
            speaker_name="Client",
            text="Initial request.",
            sequence=1,
        )
    )
    db.commit()
    produced_ids = []

    def stale_model(worker_db, session):
        contender = sessionmaker(bind=db.get_bind())()
        try:
            contender.add(
                CallTranscriptEntry(
                    id=uuid.uuid4().hex,
                    call_session_id=session.id,
                    source="meet",
                    external_id="entry-late",
                    speaker_name="Client",
                    text="One more request.",
                    sequence=2,
                )
            )
            contender.commit()
        finally:
            contender.close()
        extraction = CallExtraction(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            model="stale-model",
            prompt_version="test",
            schema_version="1",
            raw_output={},
        )
        produced_ids.append(extraction.id)
        worker_db.add(extraction)
        worker_db.flush()
        return extraction

    monkeypatch.setattr(
        "rubik_core.call_capture.extraction.run_extraction", stale_model
    )
    stale = _extraction_report()
    ccs_sweep._run_extraction(db, call, stale, False)

    assert stale["extractions_failed"] == 1
    assert call.status == "transcript_ready"
    assert call.extraction_claimed_until is None
    assert db.query(CallExtraction).filter_by(id=produced_ids[0]).count() == 0

    monkeypatch.setattr(
        "rubik_core.call_capture.extraction.run_extraction",
        lambda _db, _call: None,
    )
    recovered = _extraction_report()
    ccs_sweep._run_extraction(db, call, recovered, False)
    assert recovered["extractions_empty"] == 1
    assert call.last_extracted_entry_count == 2
    assert call.status == "extracted"


def test_claim_heartbeat_renews_only_its_owner(db):
    call = _session(db, status="transcript_ready")
    claim = ccs_sweep._claim_extraction(db, call, manual_retry=False)
    assert claim is not None
    claim_id, _ = claim
    first_deadline = call.extraction_claimed_until

    other_db = sessionmaker(bind=db.get_bind())()
    try:
        assert ccs_sweep._renew_extraction_claim(other_db, call.id, claim_id)
        assert not ccs_sweep._renew_extraction_claim(other_db, call.id, "wrong-owner")
    finally:
        other_db.close()
    db.refresh(call)
    assert call.extraction_claimed_until >= first_deadline
    assert call.extraction_claim_id == claim_id
    assert ccs_sweep._release_extraction_claim(db, call, claim_id)
    db.commit()


# ═══ 1. FINDING 3 — a live call is never ingested ════════════════════════════


def test_a_live_conference_is_not_even_listed_for_transcripts(db, monkeypatch, corpus):
    """The 10:20 sweep of a call that started at 10:00 must do nothing.

    Not "ingest carefully" — nothing. The transcript of a live conference is a
    prefix, and a stored prefix is indistinguishable from a stored call
    afterwards, so the only safe read is no read at all.
    """
    session = _session(db)
    client = _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended=None)],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "STARTED"}]},
            entries={TRANSCRIPT: _entries(20)},
        ),
    )

    report = _sweep(db)

    assert report["still_live"] == 1
    assert report["entries_written"] == 0
    assert report["transcripts_seen"] == 0
    # The API call itself never happens: the endTime check comes first.
    assert client.listed_transcripts == []
    assert client.listed_entries == []
    assert db.query(CallTranscriptEntry).count() == 0
    db.refresh(session)
    assert session.status == "in_progress"
    # And nothing was extracted from a fragment.
    assert report["extractions_run"] == report["extractions_empty"] == 0


def test_the_pass_after_the_call_ends_ingests_the_WHOLE_call(db, monkeypatch, corpus):
    """The defect, end to end: sweep mid-call, then sweep after it ends.

    The second pass must land every utterance including the one said at minute
    44. Under the old gate the first pass wrote the prefix, and the second saw
    ``has_entries`` and skipped — losing the SOW ask permanently.
    """
    _session(db)
    client = _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended=None)],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "STARTED"}]},
            entries={TRANSCRIPT: _entries(20)},  # the first 20 minutes
        ),
    )

    first = _sweep(db)
    assert first["still_live"] == 1
    assert db.query(CallTranscriptEntry).count() == 0

    # The call ends. Meet finalises the transcript and publishes all of it.
    client.records = [_record(ended="2026-08-26T15:50:00Z")]
    client.transcripts = {
        RECORD: [{"name": TRANSCRIPT, "state": "FILE_GENERATED"}],
    }
    client.entries = {TRANSCRIPT: _entries(20, tail=SOW_ASK)}

    second = _sweep(db)

    assert second["still_live"] == 0
    assert second["entries_written"] == 21
    stored = {e.text for e in db.query(CallTranscriptEntry).all()}
    assert SOW_ASK in stored, "the last half hour of the call was lost"


def test_a_transcription_still_running_is_not_ingested_mid_flight(
    db, monkeypatch, corpus
):
    """The conference ended but the transcript has not stopped growing.

    Same failure shape as a live conference, one layer down: the host toggled
    transcription and Meet has not closed the session out. Waiting costs one
    pass; reading costs the tail of the call.
    """
    _session(db)
    client = _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended="2026-08-26T15:50:00Z")],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "STARTED"}]},
            entries={TRANSCRIPT: _entries(20)},
        ),
    )

    report = _sweep(db)

    assert report["transcripts_in_progress"] == 1
    assert report["awaiting_transcript"] == 1
    assert report["transcripts_seen"] == 0
    assert client.listed_entries == []
    assert db.query(CallTranscriptEntry).count() == 0

    client.transcripts = {RECORD: [{"name": TRANSCRIPT, "state": "ENDED"}]}
    client.entries = {TRANSCRIPT: _entries(20, tail=SOW_ASK)}
    after = _sweep(db)
    assert after["entries_written"] == 21


@pytest.mark.parametrize(
    "transcript,settled",
    [
        ({"name": TRANSCRIPT, "state": "ENDED"}, True),
        ({"name": TRANSCRIPT, "state": "FILE_GENERATED"}, True),
        ({"name": TRANSCRIPT, "state": "STARTED"}, False),
        # No state field: Meet stamps endTime when transcription stops, so it
        # is the fallback answer — and "neither" means "assume still running".
        ({"name": TRANSCRIPT, "endTime": "2026-08-26T15:50:00Z"}, True),
        ({"name": TRANSCRIPT}, False),
        ({"name": TRANSCRIPT, "state": "STATE_UNSPECIFIED"}, False),
    ],
)
def test_only_a_finished_transcript_counts_as_settled(transcript, settled):
    assert ccs_sweep._is_settled(transcript) is settled


# ═══ 2. FINDING 2 — a failed corpus import strands the call ══════════════════


def _ended_client(monkeypatch, entries=None) -> FakeMeetClient:
    return _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended="2026-08-26T15:50:00Z")],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "ENDED"}]},
            entries={TRANSCRIPT: entries or _entries(20, tail=SOW_ASK)},
        ),
    )


def test_a_failed_corpus_import_is_named_and_then_recovered(db, monkeypatch, corpus):
    """The whole of finding 2, in one flow.

    Pass 1: the entries commit, the Vertex import is rejected. The call is in
    the citation index and invisible to Chat, RAG and the graph. That must NOT
    read as ``already_ingested`` — the operator would see a clean report over a
    lost call. Pass 2: the corpus is healthy again and the sweep re-attempts,
    without duplicating a single entry row.
    """
    session = _session(db)
    corpus["mode"] = "fail"
    _ended_client(monkeypatch)

    first = _sweep(db)

    assert first["entries_written"] == 21
    assert first["ingest_incomplete"] == 1
    assert first["already_ingested"] == 0
    assert first["records_failed"] == 0
    db.refresh(session)
    assert not transcript_ingest.is_complete(session, TRANSCRIPT)
    # The asset row exists and is marked failed — which is NOT terminal, and is
    # what lets the next pass re-register the same doc_id.
    assert [a["status"] for a in corpus["assets"].values()] == ["failed"]

    corpus["mode"] = "ok"
    second = _sweep(db)

    assert second["ingest_incomplete"] == 0
    assert second["entries_written"] == 0, "a recovery must not duplicate entries"
    assert db.query(CallTranscriptEntry).count() == 21
    assert [a["status"] for a in corpus["assets"].values()] == ["approved"]
    # Write 3 — without it the transcript is invisible in Chat even to the AE.
    assert corpus["participants"], "upsert_participants never ran"
    doc_id, meeting_id, emails = corpus["participants"][-1]
    assert meeting_id == RECORD
    assert AE in emails

    third = _sweep(db)
    assert third["already_ingested"] == 1
    assert third["ingest_incomplete"] == 0
    assert len(corpus["processed"]) == 2, "an approved asset must not re-import"


def test_a_raising_import_leaves_the_call_retryable_and_still_wraps_up(
    db, monkeypatch, corpus, _no_llm
):
    """A Vertex timeout throws; it does not return a tidy failure result.

    The entries are already committed, so the AE's wrap-up and the extractor can
    still run off them — losing those too would turn one missing corpus document
    into a missing wrap-up. The call is reported stranded, not failed.
    """
    _session(db)
    corpus["mode"] = "raise"
    _ended_client(monkeypatch)

    report = _sweep(db)

    assert report["ingest_incomplete"] == 1
    assert report["records_failed"] == 0
    assert report["already_ingested"] == 0
    assert _no_llm, "the extractor must still run off the stored entries"
    assert _wrapups(db), "the AE still owes a wrap-up for an ingested call"

    corpus["mode"] = "ok"
    assert _sweep(db)["ingest_incomplete"] == 0


def test_the_corpus_retry_is_capped_but_the_call_never_looks_ingested(
    db, monkeypatch, corpus, _no_llm
):
    """A permanently broken corpus must not buy an import every 15 minutes.

    The cap bounds the spend. It does NOT launder the call into
    ``already_ingested``: every pass after the cap still reports it stranded,
    because that is the only way anyone finds out.
    """
    _session(db)
    corpus["mode"] = "fail"
    _ended_client(monkeypatch)

    reports = [_sweep(db) for _ in range(transcript_ingest.MAX_ASSET_ATTEMPTS + 3)]

    assert len(corpus["processed"]) == transcript_ingest.MAX_ASSET_ATTEMPTS
    assert all(r["ingest_incomplete"] == 1 for r in reports)
    assert all(r["already_ingested"] == 0 for r in reports)
    # And exactly one extraction, on the pass that actually wrote entries.
    assert len(_no_llm) == 1


def test_entries_alone_do_not_prove_an_ingest(db, corpus):
    """The predicate the recovery gate stands on, stated directly."""
    session = _session(db)
    db.add(
        CallTranscriptEntry(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            source=transcript_ingest.MEET_SOURCE,
            external_id=f"{TRANSCRIPT}/entries/e-0",
            text="We are committed to the onboarding scope.",
            sequence=1,
        )
    )
    db.commit()

    assert not transcript_ingest.is_complete(session, TRANSCRIPT)
    assert transcript_ingest.needs_ingest(session, TRANSCRIPT)

    transcript_ingest.mark_outcome(db, session, TRANSCRIPT, "ingested")
    assert transcript_ingest.is_complete(session, TRANSCRIPT)
    assert not transcript_ingest.needs_ingest(session, TRANSCRIPT)


# ═══ 3. FINDING 5 — the wrap-up commitment has a producer ════════════════════


def test_a_captured_call_opens_the_wrapup_commitment(db, monkeypatch, corpus):
    """The wrap-up clock opens at the polled design's ``conference.v2.ended``.

    This is the soft block: without this row the wrap-up never ages, never
    breaches onto Harper's worklist and never escalates.
    """
    session = _session(db)
    _ended_client(monkeypatch)

    report = _sweep(db)

    assert report["wrapups_opened"] == 1
    rows = _wrapups(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.owner_email == AE
    assert row.hubspot_company_id == COMPANY
    assert row.hubspot_deal_id == DEAL
    assert row.opened_source == "call"
    assert row.opened_evidence_ref == session.id
    assert row.sla_hours == ccs_sweep.WRAPUP_SLA_HOURS == 8
    # The clock starts when the call ENDED, not when the sweep noticed it.
    assert row.opened_at == _utc(2026, 8, 26, 15, 50)
    assert row.breach_at is not None, "a wrap-up that cannot breach is not a clock"
    assert row.state == "open"


def test_a_silent_call_still_owes_a_wrapup(db, monkeypatch, corpus):
    """No transcript is not an error, and the AE still logs it.

    Transcription off, a 40-second call, a confidential one: the wrap-up opens
    empty and the obligation is identical.
    """
    ended = datetime.utcnow() - timedelta(minutes=45)
    _session(db, status="ended", ended_at=ended)
    _install(
        monkeypatch,
        FakeMeetClient(records=[], transcripts={}, entries={}),
    )

    report = _sweep(db)

    assert report["no_transcript_marked"] == 1
    assert report["wrapups_opened"] == 1
    row = db.query(CallSession).one()
    assert row.status == "no_transcript"
    assert len(_wrapups(db)) == 1
    assert _wrapups(db)[0].opened_at == row.ended_at


def test_a_suppressed_call_owes_a_wrapup_too(db, monkeypatch, corpus):
    """The content is never read; the obligation to log the call remains."""
    ended = datetime.utcnow() - timedelta(minutes=45)
    _session(db, status="ended", ended_at=ended, capture_consent="suppressed")
    client = _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended="2026-08-26T15:50:00Z")],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "ENDED"}]},
            entries={TRANSCRIPT: _entries(20)},
        ),
    )

    report = _sweep(db)

    assert report["suppressed"] == 1
    assert client.listed_transcripts == [], "a suppressed call is never read"
    assert db.query(CallTranscriptEntry).count() == 0
    assert len(_wrapups(db)) == 1


def test_opening_the_wrapup_is_idempotent_across_passes(db, monkeypatch, corpus):
    """The sweep re-reads a 24-hour lookback every 15 minutes.

    ~96 passes over one call must leave ONE commitment, or every AE's My Day
    fills with copies of a single obligation.
    """
    _session(db)
    _ended_client(monkeypatch)

    reports = [_sweep(db) for _ in range(4)]

    assert len(_wrapups(db)) == 1
    assert reports[0]["wrapups_opened"] == 1
    assert [r["wrapups_existing"] for r in reports[1:]] == [1, 1, 1]
    assert sum(r["wrapups_opened"] for r in reports[1:]) == 0


def test_no_wrapup_is_opened_for_a_call_the_ae_already_wrapped(db, monkeypatch, corpus):
    """A discharged obligation must not be re-opened by a later pass.

    The sweep re-reads the same conference record for a whole day, and a fresh
    commitment for an already-wrapped call would age towards a breach nobody
    can clear.
    """
    _session(db, status="wrapped", wrapped_at=datetime.utcnow())
    _ended_client(monkeypatch)

    report = _sweep(db)

    assert report["wrapups_opened"] == 0
    assert report["wrapups_existing"] == 0
    assert _wrapups(db) == []


def test_an_adhoc_call_with_no_crm_scope_still_gets_exactly_one_wrapup(
    db, monkeypatch, corpus
):
    """The sweep leaves ``hubspot_company_id`` NULL on a discovered call.

    Both partial unique constraints are NULL on such a commitment, so the
    database permits unlimited duplicates and the dedupe falls to
    ``opened_evidence_ref`` — which is why the session id is passed as the
    evidence ref and must stay that.
    """
    _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended="2026-08-26T15:50:00Z")],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "ENDED"}]},
            entries={TRANSCRIPT: _entries(20, tail=SOW_ASK)},
        ),
    )

    for _ in range(3):
        _sweep(db)

    created = db.query(CallSession).one()
    assert created.origin == "adhoc"
    assert created.hubspot_company_id is None
    rows = _wrapups(db)
    assert len(rows) == 1
    assert rows[0].opened_evidence_ref == created.id
    assert rows[0].hubspot_company_id is None


def test_a_cockpit_failure_does_not_cost_the_ingest(db, monkeypatch, corpus):
    """The transcript is already stored; a commitment failure is not the call's.

    Contained into its own bucket rather than raised, or one broken cockpit
    write would turn every ingested call in the window into ``records_failed``.
    """
    _session(db)
    _ended_client(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("commitments table is unavailable")

    monkeypatch.setattr("rubik_core.cockpit.commitment_engine.open_commitment", _boom)

    report = _sweep(db)

    assert report["wrapups_failed"] == 1
    assert report["wrapups_opened"] == 0
    assert report["records_failed"] == 0
    assert report["entries_written"] == 21
    assert db.query(CallTranscriptEntry).count() == 21


# ═══ 4. The aggregate report keeps the new buckets ═══════════════════════════


def test_sweep_all_aggregates_the_named_buckets(db, monkeypatch, corpus):
    """A bucket missing from the aggregate is a bucket nobody reads.

    ``sweep_all`` sums a hard-coded tuple of keys, so a new per-user bucket that
    is not added there disappears in the report the scheduled job actually logs.
    """
    _session(db)
    corpus["mode"] = "fail"
    _ended_client(monkeypatch)

    report = ccs_sweep.sweep_all(db, lookback_hours=24)

    for key in (
        "ingest_incomplete",
        "transcripts_in_progress",
        "wrapups_opened",
        "wrapups_existing",
        "wrapups_failed",
    ):
        assert key in report, f"{key} is missing from the aggregate report"
    assert report["users"] == 1
    assert report["users_swept"] == 1
    assert report["ingest_incomplete"] == 1
    assert report["wrapups_opened"] == 1
    assert report["per_user"][0]["ingest_incomplete"] == 1


def test_a_dry_run_writes_nothing_and_opens_no_commitment(db, monkeypatch, corpus):
    """Dry run is a report, and a commitment is a write like any other."""
    _session(db)
    _ended_client(monkeypatch)

    report = _sweep(db, dry_run=True)

    assert report["would_ingest"] == 1
    assert report["entries_written"] == 0
    assert db.query(CallTranscriptEntry).count() == 0
    assert _wrapups(db) == []
    assert corpus["processed"] == []


def test_the_timestamps_the_sweep_stamps_are_naive_utc(db, monkeypatch, corpus):
    """Every DateTime column in this repo is naive and holds UTC.

    Meet returns RFC3339 with an offset; an aware value stored here would make
    every later comparison raise or silently compare wrong.
    """
    session = _session(db)
    _ended_client(monkeypatch)

    _sweep(db)

    db.refresh(session)
    assert session.ended_at is not None
    assert session.ended_at.tzinfo is None
    assert session.ended_at == datetime(2026, 8, 26, 15, 50).replace(tzinfo=None)
    commitment = _wrapups(db)[0]
    assert commitment.opened_at.tzinfo is None
    assert commitment.opened_at == datetime(
        2026, 8, 26, 15, 50, tzinfo=timezone.utc
    ).replace(tzinfo=None)


# ═══ 5. Coverage — a host-stopped transcript must not look complete ══════════
#
# `artifactConfig.autoTranscriptionGeneration` sets a DEFAULT, not a lock: a
# host or co-host can stop transcription mid-call from the Meet UI, and the Meet
# API's three write methods cannot stop, start or even observe it. Afterwards
# the truncated transcript is indistinguishable from a complete one — it reports
# ENDED, the sweep correctly ingests it, and the AE gets a normal-looking
# wrap-up built from part of the call. People reach for that button exactly
# where the SOW ask lives: when the call turns to pricing, a competitor or a
# complaint.
#
# So ingest compares the last utterance offset against the conference's own
# clock. Every test below the first two is about a way of computing a FALSE
# truncation, because a banner that cries wolf is one the AE stops reading —
# and then the real one costs a deal.

CALL_START = _utc(2026, 8, 26, 15, 0)
CALL_END = _utc(2026, 8, 26, 15, 50)  # 3_000_000 ms of call
MINUTE = 60_000


def _tail_at(rows: list[dict], *, start: str, end: str | None) -> list[dict]:
    """Append one last utterance at an explicit point in the call."""
    row = {
        "name": f"{TRANSCRIPT}/entries/e-tail",
        "participant": None,
        "text": SOW_ASK,
        "languageCode": "en-US",
        "startTime": start,
    }
    if end:
        row["endTime"] = end
    return rows + [row]


def _row(end_ms, *, start_ms=None, source=transcript_ingest.MEET_SOURCE):
    """One stored entry, reduced to what coverage actually reads."""
    return SimpleNamespace(
        source=source, start_offset_ms=start_ms, end_offset_ms=end_ms
    )


def _coverage_of(db, session) -> dict | None:
    db.refresh(session)
    return transcript_ingest.coverage(session)


def test_a_transcript_that_runs_to_the_end_of_the_call_is_not_truncated(
    db, monkeypatch, corpus
):
    """The normal call: nobody touched the button, so nothing is said about it.

    The banner has to be rare to mean anything, and this is the shape it must
    stay silent on.
    """
    session = _session(db)
    _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended="2026-08-26T15:50:00Z")],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "ENDED"}]},
            entries={
                TRANSCRIPT: _tail_at(
                    _entries(20),
                    start="2026-08-26T15:49:00Z",
                    end="2026-08-26T15:49:30Z",
                )
            },
        ),
    )

    _sweep(db)

    cov = _coverage_of(db, session)
    assert cov is not None, "every ingested call records what it heard"
    assert cov["truncated"] is False
    assert cov["reason"] == transcript_ingest.COVERAGE_COVERED
    assert cov["call_ms"] == 50 * MINUTE
    assert cov["covered_ms"] == 49 * MINUTE + 30_000
    assert cov["ratio"] == 0.99
    assert cov["gap_ms"] == 30_000


def test_a_transcript_stopped_at_minute_20_of_a_50_minute_call_is_truncated(
    db, monkeypatch, corpus
):
    """The defect this whole verdict exists for.

    Transcription stops at minute 20; Meet reports the transcript ENDED and the
    sweep ingests it, correctly, in full. Without coverage the AE reads a
    wrap-up about the first 20 minutes with nothing to say the model never heard
    the other thirty — the half of the call the pricing conversation was in.
    """
    session = _session(db)
    _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended="2026-08-26T15:50:00Z")],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "ENDED"}]},
            # Last utterance ends at 15:19:30. The call ran to 15:50.
            entries={TRANSCRIPT: _entries(20)},
        ),
    )

    report = _sweep(db)

    assert report["entries_written"] == 20, "the partial transcript is still ingested"
    cov = _coverage_of(db, session)
    assert cov["truncated"] is True
    assert cov["reason"] == transcript_ingest.COVERAGE_TRUNCATED
    assert cov["covered_ms"] == 19 * MINUTE + 30_000
    assert cov["gap_ms"] == 30 * MINUTE + 30_000, "half the call carries no transcript"
    assert cov["ratio"] == 0.39


def test_a_short_call_ending_slightly_early_is_not_truncated(monkeypatch):
    """The gap floor, and the reason `truncated` needs BOTH conditions.

    A four-minute call whose last word lands 40 seconds before the conference
    record closes is 83% covered — under the ratio — and is a perfectly normal
    call ending in goodbyes. Flagging it teaches the AE that the banner means
    nothing.
    """
    cov = transcript_ingest.compute_coverage(
        [_row(200_000)], started_at=CALL_START, ended_at=_utc(2026, 8, 26, 15, 4)
    )

    assert cov["ratio"] == 0.8333 < settings.CCS_COVERAGE_MIN_RATIO
    assert cov["gap_ms"] == 40_000 <= settings.CCS_COVERAGE_MIN_GAP_SECONDS * 1000
    assert cov["truncated"] is False
    assert cov["reason"] == transcript_ingest.COVERAGE_SHORT_GAP

    # And the mirror image: a huge gap that is still a high ratio (a two-hour
    # call whose last five minutes are silence) is not truncated either.
    long_call = transcript_ingest.compute_coverage(
        [_row(115 * MINUTE)],
        started_at=CALL_START,
        ended_at=_utc(2026, 8, 26, 17, 0),
    )
    assert long_call["gap_ms"] == 5 * MINUTE
    assert long_call["truncated"] is False
    assert long_call["reason"] == transcript_ingest.COVERAGE_WITHIN_RATIO


def test_untimed_fathom_entries_are_never_read_as_a_truncated_call():
    """Fathom entries usually carry no per-utterance timestamps.

    Taken naively their coverage computes as zero and EVERY Fathom call — the
    entire fallback capture path — is flagged truncated. An untimed utterance
    could have been said anywhere in the call, so the honest verdict is that
    coverage is unknown, not that it is nothing.
    """
    cov = transcript_ingest.compute_coverage(
        [_row(None, source=transcript_ingest.FATHOM_SOURCE) for _ in range(3)],
        started_at=CALL_START,
        ended_at=CALL_END,
    )

    assert cov["truncated"] is False
    assert cov["reason"] == transcript_ingest.COVERAGE_FATHOM_NO_OFFSETS
    assert cov["ratio"] is None and cov["gap_ms"] is None
    assert cov["call_ms"] == 50 * MINUTE, "the call length is still known"

    # A Meet transcript that happens to carry no offsets at all lands in the
    # same place, under its own code.
    meet = transcript_ingest.compute_coverage(
        [_row(None), _row(None)], started_at=CALL_START, ended_at=CALL_END
    )
    assert meet["truncated"] is False
    assert meet["reason"] == transcript_ingest.COVERAGE_NO_OFFSETS


def test_coverage_reads_every_source_stored_against_the_session(db):
    """The offsets query spans sources, which is what makes the guard reachable.

    A Meet transcript with a Fathom capture alongside it: judging only the Meet
    rows would flag the call on the Meet transcript's own short window while
    Fathom holds the rest of the call, untimed.
    """
    session = _session(db, started_at=CALL_START, ended_at=CALL_END)
    for i in range(2):
        db.add(
            CallTranscriptEntry(
                id=uuid.uuid4().hex,
                call_session_id=session.id,
                source=transcript_ingest.MEET_SOURCE,
                external_id=f"{TRANSCRIPT}/entries/e-{i}",
                text="We walked through the onboarding scope.",
                start_offset_ms=i * MINUTE,
                end_offset_ms=i * MINUTE + 30_000,
                sequence=i + 1,
            )
        )
    db.add(
        CallTranscriptEntry(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            source=transcript_ingest.FATHOM_SOURCE,
            external_id=None,
            text=SOW_ASK,
            sequence=3,
        )
    )
    db.commit()

    rows = transcript_ingest._coverage_rows(db, session.id)
    assert len(rows) == 3, "coverage must see the Fathom rows too"
    cov = transcript_ingest.compute_coverage(
        rows, started_at=session.started_at, ended_at=session.ended_at
    )
    assert cov["truncated"] is False
    assert cov["reason"] == transcript_ingest.COVERAGE_FATHOM_NO_OFFSETS


def test_a_missing_call_window_is_unknown_not_truncated():
    """No `started_at` or no `ended_at` means there is no length to divide by.

    Unknown is shown as unknown and never as zero — the same rule the commitment
    clock follows. `covered_ms` survives, because how much WAS heard is still a
    fact worth keeping.
    """
    entries = [_row(20 * MINUTE)]

    for started, ended in (
        (None, CALL_END),
        (CALL_START, None),
        (None, None),
        # Stamps that disagree about which way time runs: a length, but not one.
        (CALL_END, CALL_START),
        (CALL_START, CALL_START),
    ):
        cov = transcript_ingest.compute_coverage(
            entries, started_at=started, ended_at=ended
        )
        assert cov["truncated"] is False, (started, ended)
        assert cov["reason"] == transcript_ingest.COVERAGE_CALL_WINDOW_UNKNOWN
        assert cov["call_ms"] is None
        assert cov["ratio"] is None and cov["gap_ms"] is None
        assert cov["covered_ms"] == 20 * MINUTE


def test_clock_skew_clamps_instead_of_inventing_a_negative_gap():
    """Meet's offsets and our own stamps are two clocks; they disagree by seconds.

    An utterance that lands after the conference record closes must cap at full
    coverage, never produce a ratio above 1.0 or a gap below 0 for a reader to
    render as "-1 minutes missing".
    """
    cov = transcript_ingest.compute_coverage(
        [_row(50 * MINUTE + 12_000)], started_at=CALL_START, ended_at=CALL_END
    )

    assert cov["covered_ms"] == cov["call_ms"] == 50 * MINUTE
    assert cov["ratio"] == 1.0
    assert cov["gap_ms"] == 0
    assert cov["truncated"] is False
    assert cov["reason"] == transcript_ingest.COVERAGE_CLOCK_SKEW


def test_coverage_uses_the_last_KNOWN_offset_not_the_last_entry():
    """Meet does not stamp every entry, and the tail is the likeliest to be bare.

    Reading the last row's `end_offset_ms` would see None on such a transcript
    and report a call as uncovered that was covered to the minute — a false
    banner on a complete call. The furthest known offset is the answer, and a
    start-only entry counts (a start is never later than its own end).
    """
    cov = transcript_ingest.compute_coverage(
        [
            _row(10 * MINUTE),
            _row(None, start_ms=48 * MINUTE),  # spoken at 48:00, never closed
            _row(None),  # stamped with nothing at all
        ],
        started_at=CALL_START,
        ended_at=CALL_END,
    )

    assert cov["covered_ms"] == 48 * MINUTE
    assert cov["truncated"] is False
    assert cov["reason"] == transcript_ingest.COVERAGE_COVERED


def test_the_coverage_verdict_survives_a_re_ingest_replay(db, monkeypatch, corpus):
    """A stranded call is re-ingested every pass; the verdict must ride through.

    `attrs` holds the ingest marks, the coverage verdict and whatever else the
    sweep recorded, and every write REASSIGNS it — a writer that mutated the
    dict in place would emit no UPDATE at all, and one that rebuilt it from
    scratch would drop the other keys. Both failures are silent.
    """
    session = _session(db)
    session.attrs = {"company_name": "Acme Supply"}
    db.commit()
    corpus["mode"] = "fail"
    _ended_client(monkeypatch)

    first = _sweep(db)
    assert first["ingest_incomplete"] == 1
    after_first = _coverage_of(db, session)
    assert after_first is not None
    assert session.attrs["company_name"] == "Acme Supply"
    assert transcript_ingest.attempts(session, TRANSCRIPT) == 1

    corpus["mode"] = "ok"
    second = _sweep(db)

    assert second["entries_written"] == 0, "a replay writes no entries"
    assert transcript_ingest.is_complete(session, TRANSCRIPT)
    after_second = _coverage_of(db, session)
    assert after_second == after_first, "the same call, so the same verdict"
    assert session.attrs["company_name"] == "Acme Supply"

    # A third pass does not ingest at all — and must not disturb the verdict.
    third = _sweep(db)
    assert third["already_ingested"] == 1
    assert _coverage_of(db, session) == after_second


def test_a_call_with_no_transcript_gets_no_competing_verdict(db, monkeypatch, corpus):
    """`no_entries` is its own state, already handled and already visible.

    A "0% covered" verdict next to it would be a second, louder way of saying
    the same thing — and a reader that trusted it would report a call that was
    never transcribed as one somebody truncated.
    """
    session = _session(db)
    _install(
        monkeypatch,
        FakeMeetClient(
            records=[_record(ended="2026-08-26T15:50:00Z")],
            transcripts={RECORD: [{"name": TRANSCRIPT, "state": "ENDED"}]},
            # A settled transcript with nothing quotable in it.
            entries={TRANSCRIPT: [dict(e, text="   ") for e in _entries(3)]},
        ),
    )

    _sweep(db)

    db.refresh(session)
    assert transcript_ingest.outcomes(session)[TRANSCRIPT]["outcome"] == "no_entries"
    assert transcript_ingest.coverage(session) is None


def test_a_suppressed_call_never_gets_a_coverage_verdict(db, monkeypatch, corpus):
    """A confidential call is never read, so there is nothing to measure.

    Unknown, not "0% covered": the AE turned capture off on purpose and that is
    not a truncated call.
    """
    session = _session(db, capture_consent="suppressed")
    _ended_client(monkeypatch)

    report = _sweep(db)

    assert report["suppressed"] == 1
    db.refresh(session)
    assert transcript_ingest.coverage(session) is None


# ═══ WHO IS SWEPT — the Feature Access roster, not a department ══════════════


def test_the_sweep_reads_the_call_capture_roster_not_the_sales_department(db):
    """An AE outside the roster cannot open the wrap-up the sweep would build
    for them, so their calls are not read — even with sessions on record.
    Widening the roster on the admin page widens the sweep with no deploy.

    Call capture ships open to everyone; the roster is narrowed to Systems
    here first, as an admin would."""
    from rubik_core.features import FeaturePolicy, save_policy

    save_policy(
        db,
        ccs_sweep.CCS_FEATURE_KEY,
        FeaturePolicy(departments=frozenset({"systems"}), admin_allowed=True),
        actor_email="admin@acme.example",
    )
    admin = Role(name="admin")
    db.add_all(
        [
            User(
                id="sub-sales",
                email="rep@acme.example",
                name="Rep",
                department="Sales",
            ),
            User(
                id="sub-sys",
                email="sys@acme.example",
                name="Sys",
                department="Systems",
            ),
            User(
                id="sub-admin",
                email="admin@acme.example",
                name="Admin",
                department="Marketing",
                roles=[admin],
            ),
        ]
    )
    db.commit()
    # A Sales rep with a Fathom-created session is still not on the roster.
    _session(
        db,
        ae_email="rep@acme.example",
        meet_space_name="spaces/rep",
        conference_record=None,
    )
    # A session whose owner has left: counted, never swept.
    _session(
        db,
        ae_email="gone@acme.example",
        meet_space_name="spaces/gone",
        conference_record=None,
    )

    emails, departed = ccs_sweep._ae_emails(db)
    assert emails == ["admin@acme.example", AE, "sys@acme.example"]
    assert departed == 1

    save_policy(
        db,
        ccs_sweep.CCS_FEATURE_KEY,
        FeaturePolicy(departments=frozenset({"systems", "sales"}), admin_allowed=True),
        actor_email="bellison@acme.example",
    )
    emails, _ = ccs_sweep._ae_emails(db)
    assert "rep@acme.example" in emails


def test_a_second_conference_in_a_space_that_already_has_a_session_gets_its_own(
    db, monkeypatch, corpus
):
    """The next occurrence of a recurring series (or a second AE on the call)
    lands in a space another session already holds. ``meet_space_name`` is
    UNIQUE, so storing it again lost the new session and failed every pass."""
    first = _session(db)  # bound to RECORD, holds SPACE
    second_record = {
        "name": "conferenceRecords/rec-2",
        "space": {"name": SPACE, "meetingCode": "abc-defg-hij"},
        "startTime": "2026-09-02T15:00:00Z",
        "endTime": "2026-09-02T15:50:00Z",
    }
    _install(monkeypatch, FakeMeetClient([second_record]))

    report = _sweep(db)

    assert report["records_failed"] == 0
    assert report["sessions_created"] == 1
    new = (
        db.query(CallSession)
        .filter(CallSession.conference_record == "conferenceRecords/rec-2")
        .one()
    )
    assert new.id != first.id
    assert new.meet_space_name is None  # the space stays with the first session
    assert new.meet_meeting_code == "abc-defg-hij"
    db.refresh(first)
    assert first.meet_space_name == SPACE and first.conference_record == RECORD
