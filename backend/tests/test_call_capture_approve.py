"""Sales Call Capture — the five invariants of the wrap-up screen's endpoints.

Regression cover for ``backend/routers/calls.py`` only. Four of the five are ways
``POST /calls/{id}/wrapup/approve`` could keep a promise the UI already made; the
fifth is the one thing ``GET /calls/{id}/wrapup`` must never imply:

1. **"Approving twice writes once" survives CONCURRENCY.** The ledger claim
   is an INSERT arbitrated by the UNIQUE constraint, plus a conditional UPDATE
   for the genuinely retryable case — never a read of ``succeeded_at``
   followed by a decision, which is a race the AE loses by opening a second
   tab and the client pays for with two call engagements on their CRM record.
2. **The wrap-up timing is written ONCE** (it decides whether this gets
   used). The endpoint deliberately supports a multi-visit
   approval; a second visit at 16:30 must not rewrite a 65-second wrap-up as a
   seven-hour one and poison the median of ten calls.
3. **A confidential call generates NO artifact**, recording included. The
   suppressed-consent modal promises "no transcript is captured, nothing is
   extracted... this cannot be undone", and ``MEET_AUTO_RECORD_DEFAULT`` is a
   documented, flippable knob.
4. **The clocks are opened and closed by the humans who own them.** A written
   lead starts the ``lead_touch`` clock (without which Harper's
   worklist ships two-thirds empty), and a completed wrap-up STOPS the
   ``wrapup`` clock the sweep started when the call ended — an aging clock
   nobody can stop is worse than no clock, because it fills the worklist with
   work that is already done.
5. **A PARTIAL transcript never renders as a complete one.** A host can stop
   transcription mid-call from the Meet UI and no Meet API method can prevent,
   restart or report it, so the only defence is measuring what the transcript
   covered and saying so — and saying nothing at all about the calls that
   predate the measurement, rather than reassuring anyone about them.

Nothing here talks to HubSpot, Meet or Vertex. The write client is a recorder
patched onto ``backend.routers.calls.HubSpotWriteClient``; Meet is patched at
``rubik_core.tools.meet.GoogleMeetClient`` (a string target, because the router
imports it inside the handler and because sibling suites ``importlib.reload()``
modules mid-run). ``backend/tests/test_call_capture_hubspot_writes.py`` owns the
write client's own gates and is not re-tested here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.routers import calls as calls_router
from backend.routers.calls import (
    StartCallRequest,
    WrapupApproveRequest,
    WrapupDecision,
    _apply_wrapup,
    _reserve_write,
    _stamp_failure,
    _stamp_success,
    start_call,
)
from rubik_core.call_capture.review import failed_analysis_marker_exists
from rubik_core.cockpit import commitment_engine
from rubik_core.config import settings
from rubik_core.database.call_capture import (
    CallExtraction,
    CallExtractionItem,
    CallHubspotWrite,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.database.commitments import Commitment
from rubik_core.database.models import AuditLog, Base
from rubik_core.database.schemas import UserProfile
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT

AE = "parker@acme.example"
COMPANY = "co-42"
DEAL = "deal-77"
CONTACT = "contact-9"
QUOTE = "Send me the SOW by Friday and put Arden on the intro."

#: The wrap-up the AE actually did, in the wall-clock the metric is measured in.
T_OPENED = datetime(2026, 8, 27, 9, 0, 0)
T_FIRST_APPROVE = T_OPENED + timedelta(seconds=65)  # a passing data point
T_SECOND_VISIT = T_OPENED + timedelta(hours=7, minutes=30)  # after lunch


# ── Harness ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # mandatory for :memory:
    )
    # models.py imports the call_capture / commitments modules on its last line,
    # so importing Base is enough to register call_* and commitment_*.
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    """Capture on, so each test exercises the rule and not the master switch."""
    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", True)
    monkeypatch.setattr(settings, "CCS_LEADS_ENABLED", False)
    monkeypatch.setattr(settings, "MEET_AUTO_RECORD_DEFAULT", False)
    monkeypatch.setattr(settings, "MEET_AUTO_SMART_NOTES", True)
    monkeypatch.setattr("backend.auth.user_can_access_cockpit", lambda _user: True)
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, deals),
    )


class _FakeWriter:
    """Records every HubSpot mutation the router would make.

    ``on_create_call`` is the concurrency hook: it fires INSIDE the blocking
    call, which is exactly where a second tab's request arrives in the race
    "approving twice writes once" is about.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.leads: list[dict] = []
        self.tasks: list[dict] = []
        self.companies: list[tuple[str, dict]] = []
        self.on_create_call = None
        self.lead_error: Exception | None = None

    # reads
    def resolve_owner_id(self, email: str) -> str:  # noqa: ARG002
        return "77"

    def get_object(self, object_type, object_id, properties=None):  # noqa: ARG002
        return {"id": object_id, "properties": {p: None for p in (properties or [])}}

    # writes
    def create_call(self, properties, **_kw):
        self.calls.append(properties)
        hook, self.on_create_call = self.on_create_call, None
        if hook is not None:
            hook()
        return {"id": f"hs-call-{len(self.calls)}"}

    def update_company(self, company_id, properties):
        self.companies.append((company_id, properties))
        return {"id": company_id}

    def create_lead(self, properties, **_kw):
        if self.lead_error is not None:
            raise self.lead_error
        self.leads.append(properties)
        return {"id": f"hs-lead-{len(self.leads)}"}

    def create_task(self, properties, **_kw):
        self.tasks.append(properties)
        return {"id": f"hs-task-{len(self.tasks)}"}


@pytest.fixture
def writer(monkeypatch) -> _FakeWriter:
    fake = _FakeWriter()
    monkeypatch.setattr(calls_router, "HubSpotWriteClient", lambda *a, **kw: fake)
    return fake


def _freeze_router_clock(monkeypatch, instant: datetime) -> None:
    """Pin ``datetime.utcnow()`` inside the router module.

    Subclass + string target, per the repo convention (commit ``b8558df``):
    there is no freezegun in ``requirements.txt``, and ``_apply_wrapup`` takes
    no ``now=`` argument, so this is its only injection point. A subclass keeps
    ``datetime.strptime`` and arithmetic working for everything else in the
    module.
    """

    class _FrozenDatetime(datetime):
        @classmethod
        def utcnow(cls) -> datetime:
            return instant

    monkeypatch.setattr("backend.routers.calls.datetime", _FrozenDatetime)


# ── Row builders ──────────────────────────────────────────────────────────────


def _session(
    db,
    *,
    ae: str = AE,
    company: str | None = COMPANY,
    deal: str | None = DEAL,
    consent: str = "granted",
    wrap_opened_at: datetime | None = None,
) -> CallSession:
    from rubik_core.call_capture.provenance import stamp_session_source

    row = CallSession(
        id=uuid.uuid4().hex,
        ae_email=ae,
        hubspot_company_id=company,
        hubspot_contact_id=CONTACT,
        hubspot_deal_id=deal,
        capture_consent=consent,
        status="extracted",
        origin=CALL_ORIGIN_ASSISTANT,
        started_at=T_OPENED - timedelta(hours=1),
        ended_at=T_OPENED - timedelta(minutes=30),
        wrap_opened_at=wrap_opened_at,
        attrs={
            "company_name": "Acme Supply",
            "crm_provenance": stamp_session_source(company, CONTACT, deal),
        },
    )
    db.add(row)
    db.commit()
    return row


def _item(
    db,
    session: CallSession,
    *,
    kind: str = "stage_signal",
    proposed: str = "sow_sent",
    payload: dict | None = None,
) -> CallExtractionItem:
    extraction = (
        db.query(CallExtraction)
        .filter(CallExtraction.call_session_id == session.id)
        .first()
    )
    if extraction is None:
        extraction = CallExtraction(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            model="gemini-test",
            prompt_version="ccs-extract-v1",
            schema_version="1",
            raw_output={},
        )
        db.add(extraction)
        db.commit()
    entry_id = uuid.uuid4().hex
    db.add(
        CallTranscriptEntry(
            id=entry_id,
            call_session_id=session.id,
            source="meet",
            external_id=f"ext-{entry_id[:6]}",
            speaker_name="Client",
            text=QUOTE,
            sequence=1,
        )
    )
    item = CallExtractionItem(
        id=uuid.uuid4().hex,
        extraction_id=extraction.id,
        call_session_id=session.id,
        kind=kind,
        proposed_value=proposed,
        payload=payload,
        evidence_text=QUOTE,
        evidence_entry_id=entry_id,
        confidence=0.9,
        status="proposed",
    )
    db.add(item)
    db.commit()
    return item


def _approve(*item_ids: str, nothing_here: bool = False) -> WrapupApproveRequest:
    return WrapupApproveRequest(
        decisions=[WrapupDecision(item_id=i, accept=True) for i in item_ids],
        nothing_here=nothing_here,
    )


def _open_wrapup_clock(db, session: CallSession) -> Commitment:
    """The commitment the sweep opens at ``conference.v2.ended``."""
    commitment = commitment_engine.open_commitment(
        db,
        kind="wrapup",
        owner_email=session.ae_email,
        company_id=session.hubspot_company_id,
        deal_id=session.hubspot_deal_id,
        opened_at=session.ended_at,
        source="call",
        evidence_ref=session.id,
        sla_hours=8,
    )
    db.commit()
    return commitment


def _wrapup_audits(db) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "CALL_WRAPUP_APPROVED")
        .order_by(AuditLog.id.asc())
        .all()
    )


# ── Finding 10 — "approve twice writes once" under concurrency (AC 6) ─────────


def test_a_second_approval_arriving_mid_write_does_not_log_the_call_twice(db, writer):
    """The race the AE creates with two tabs, or one impatient retry.

    Request A reserves the key and blocks inside ``create_call``. Request B
    arrives while it is blocked, finds ``succeeded_at`` still NULL, and — under
    a read-modify-write reservation — concludes the write is retryable. The
    client's CRM then carries two call engagements for one call.
    """
    session = _session(db)
    second: dict = {}

    def _second_tab():
        second["result"] = _apply_wrapup(db, session, _approve(nothing_here=True), AE)

    writer.on_create_call = _second_tab

    first = _apply_wrapup(db, session, _approve(nothing_here=True), AE)

    assert len(writer.calls) == 1, (
        f"one call was logged to HubSpot {len(writer.calls)} times — the "
        "reservation is a read-modify-write, not a claim"
    )
    assert first["call_logged"]["status"] == "written"
    assert second["result"]["call_logged"]["status"] == "already_written"
    ledger = db.query(CallHubspotWrite).all()
    assert len(ledger) == 1
    assert ledger[0].succeeded_at is not None


def test_an_in_flight_reservation_is_not_handed_to_a_second_caller(db):
    """No outcome recorded yet means somebody is still working. Hands off."""
    session = _session(db)
    kwargs = dict(
        key="k-inflight",
        session_id=session.id,
        item_id=None,
        method="POST",
        path="/crm/v3/objects/calls",
        body={"hs_call_title": "one"},
    )

    first = _reserve_write(db, **kwargs)
    assert first is not None

    assert _reserve_write(db, **kwargs) is None
    held = (
        db.query(CallHubspotWrite)
        .filter(CallHubspotWrite.idempotency_key == "k-inflight")
        .one()
    )
    assert held.attempts == 1, "a refused claim must not even count as an attempt"


def test_a_recorded_failure_is_reclaimable_and_counted(db):
    """The retryable case — behind a conditional UPDATE, not behind a read."""
    session = _session(db)
    kwargs = dict(
        key="k-failed",
        session_id=session.id,
        item_id=None,
        method="POST",
        path="/crm/v3/objects/calls",
        body={"hs_call_title": "one"},
    )
    row = _reserve_write(db, **kwargs)
    _stamp_failure(db, [row], "HubSpot said no")

    retried = _reserve_write(db, **kwargs)

    assert retried is not None
    assert retried.attempts == 2
    assert retried.succeeded_at is None
    assert retried.response_body is None, (
        "the retry is in flight again; leaving the old error would make this "
        "very row look reclaimable to a third caller"
    )


def test_a_succeeded_key_is_never_reclaimed(db):
    """Approving twice writes once, in its simplest form."""
    session = _session(db)
    kwargs = dict(
        key="k-done",
        session_id=session.id,
        item_id=None,
        method="POST",
        path="/crm/v3/objects/calls",
        body={"hs_call_title": "one"},
    )
    row = _reserve_write(db, **kwargs)
    _stamp_success(db, [row], {"id": "hs-1"})

    assert _reserve_write(db, **kwargs) is None


def test_an_abandoned_claim_is_reclaimable_once_the_lease_expires(db):
    """A process that died must not hold a key forever.

    Cloud Run caps a request at 300s, so a claim older than the lease belongs to
    a dead process rather than a slow one — and a call that can never be logged
    is its own failure.
    """
    session = _session(db)
    kwargs = dict(
        key="k-abandoned",
        session_id=session.id,
        item_id=None,
        method="POST",
        path="/crm/v3/objects/calls",
        body={"hs_call_title": "one"},
    )
    row = _reserve_write(db, **kwargs)
    row.updated_at = datetime.utcnow() - calls_router._WRITE_CLAIM_LEASE
    row.updated_at -= timedelta(minutes=1)
    db.commit()

    reclaimed = _reserve_write(db, **kwargs)

    assert reclaimed is not None
    assert reclaimed.attempts == 2


# ── Finding 7 — the AC-10 timing is written once ──────────────────────────────


def test_a_second_visit_does_not_rewrite_the_wrap_up_timing(db, writer, monkeypatch):
    """Parker decides three items at 09:01 and the rest at 16:30.

    The wrap-up timing is the MEDIAN of ten real calls. Re-stamping ``wrapped_at``
    and ``wrap_elapsed_ms_server`` on the second visit turns this 65-second
    wrap-up into a 27,000,000 ms outlier that describes nothing anybody did.
    """
    session = _session(db, wrap_opened_at=T_OPENED)
    first_item = _item(db, session)
    second_item = _item(db, session)

    _freeze_router_clock(monkeypatch, T_FIRST_APPROVE)
    _apply_wrapup(db, session, _approve(first_item.id), AE)
    db.refresh(session)
    assert session.wrapped_at == T_FIRST_APPROVE
    assert session.attrs["wrap_elapsed_ms_server"] == 65_000

    _freeze_router_clock(monkeypatch, T_SECOND_VISIT)
    _apply_wrapup(db, session, _approve(second_item.id), AE)
    db.refresh(session)

    assert session.wrapped_at == T_FIRST_APPROVE, "the second visit re-stamped it"
    assert session.attrs["wrap_elapsed_ms_server"] == 65_000
    assert session.status == "wrapped"
    # And the item the AE decided on the second visit was still decided.
    assert db.get(CallExtractionItem, second_item.id).status == "accepted"


def test_a_later_visit_is_visible_in_the_audit_trail(db, writer, monkeypatch):
    """Write-once columns lose the revisit, so the audit row has to carry it."""
    session = _session(db, wrap_opened_at=T_OPENED)
    first_item = _item(db, session)
    second_item = _item(db, session)

    _freeze_router_clock(monkeypatch, T_FIRST_APPROVE)
    _apply_wrapup(db, session, _approve(first_item.id), AE)
    _freeze_router_clock(monkeypatch, T_SECOND_VISIT)
    _apply_wrapup(db, session, _approve(second_item.id), AE)

    audits = _wrapup_audits(db)
    assert len(audits) == 2
    assert audits[0].details["first_visit"] is True
    assert audits[1].details["first_visit"] is False
    assert audits[0].details["wrapped_at"] == audits[1].details["wrapped_at"]


def test_the_first_wrap_up_still_stamps_everything(db, writer, monkeypatch):
    """The guard must not cost the measurement it protects."""
    session = _session(db, wrap_opened_at=T_OPENED)
    item = _item(db, session)

    _freeze_router_clock(monkeypatch, T_FIRST_APPROVE)
    result = _apply_wrapup(
        db,
        session,
        WrapupApproveRequest(
            decisions=[WrapupDecision(item_id=item.id, accept=True)],
            client_elapsed_ms=64_200,
        ),
        AE,
    )

    db.refresh(session)
    assert result["wrapped_at"] is not None
    assert session.attrs["wrap_elapsed_ms_client"] == 64_200
    assert session.attrs["wrap_elapsed_ms_server"] == 65_000


# ── Finding 8 — a confidential call generates no artifact at all ──────────────


def _profile(
    email: str = AE,
    *,
    department: str = "Sales",
    managed: list[str] | None = None,
) -> UserProfile:
    """A caller. ``department`` is carried for realism and decides nothing here.

    Every surface this file reaches ships open to everyone, and
    ``cockpit_transcript_policy`` resolves from the role flags, so any plain
    caller in any department reads "own": the text of their own calls and of
    nobody else's. The coverage tests below vary WHO calls (the AE who hosted
    the call, a supervisor who did not), never the department.
    """
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name="Parker",
        department=department,
        roles=["user"],
        managed_departments=managed or [],
        created_at="2026-08-27T00:00:00",
        updated_at="2026-08-27T00:00:00",
    )


class _RecordingMeet:
    """Captures the ``create_space`` kwargs the router asks Meet for."""

    seen: dict = {}

    def __init__(self, email: str):
        self.email = email

    def create_space(self, **kwargs):
        type(self).seen = dict(kwargs)
        return {
            "name": "spaces/abc",
            "meetingCode": "abc-defg-hij",
            "meetingUri": "https://meet.google.com/abc-defg-hij",
            "config": {},
        }


@pytest.fixture
def meet(monkeypatch) -> type[_RecordingMeet]:
    _RecordingMeet.seen = {}
    monkeypatch.setattr("rubik_core.tools.meet.GoogleMeetClient", _RecordingMeet)
    monkeypatch.setattr(calls_router, "_company_name", lambda _cid: "Acme Supply")
    return _RecordingMeet


def test_a_confidential_call_is_not_recorded_either(db, meet, monkeypatch):
    """The modal says "nothing is captured. This cannot be undone." Keep it.

    ``MEET_AUTO_RECORD_DEFAULT`` is documented and flippable, so an ungated
    recording is one config change away from recording the exact call an AE
    ticked Confidential on.
    """
    monkeypatch.setattr(settings, "MEET_AUTO_RECORD_DEFAULT", True)

    asyncio.run(
        start_call(
            StartCallRequest(hubspot_company_id=COMPANY, capture_consent="suppressed"),
            user=_profile(),
            db=db,
        )
    )

    assert meet.seen["auto_record"] is False, "a confidential call was recorded"
    assert meet.seen["auto_transcribe"] is False
    assert meet.seen["auto_smart_notes"] is False
    assert db.query(CallSession).one().capture_consent == "suppressed"


def test_the_recording_knob_still_works_on_a_consented_call(db, meet, monkeypatch):
    """The gate is consent, not a hard-off — otherwise the knob is a lie too."""
    monkeypatch.setattr(settings, "MEET_AUTO_RECORD_DEFAULT", True)

    asyncio.run(
        start_call(
            StartCallRequest(hubspot_company_id=COMPANY, capture_consent="granted"),
            user=_profile(),
            db=db,
        )
    )

    assert meet.seen["auto_record"] is True
    assert meet.seen["auto_transcribe"] is True


# ── Finding 11 — a written lead starts the first-touch clock ─────────────────


def _lead_touch(db) -> Commitment | None:
    return db.query(Commitment).filter(Commitment.kind == "lead_touch").first()


def test_a_written_lead_opens_the_first_touch_clock(db, writer, monkeypatch):
    """Without it Harper's worklist ships two-thirds empty."""
    monkeypatch.setattr(settings, "CCS_LEADS_ENABLED", True)
    session = _session(db)
    item = _item(db, session, kind="lead", payload={"lead_name": "Arden at Acme"})

    result = _apply_wrapup(db, session, _approve(item.id), AE)

    assert result["results"][0]["status"] == "written"
    clock = _lead_touch(db)
    assert clock is not None, "the lead landed in HubSpot with no first-touch clock"
    assert clock.owner_email == AE
    assert clock.hubspot_company_id == COMPANY
    assert clock.opened_source == "call"
    assert clock.opened_evidence_ref == item.evidence_entry_id
    assert clock.state == "open"
    assert clock.opened_at is not None and clock.breach_at is not None


def test_no_lead_write_means_no_first_touch_clock(db, writer, monkeypatch):
    """A refused lead has nothing to be late on."""
    from rubik_core.retrieval.hubspot_write import HubSpotWriteRefused

    monkeypatch.setattr(settings, "CCS_LEADS_ENABLED", True)
    writer.lead_error = HubSpotWriteRefused("lead writes are disabled")
    session = _session(db)
    item = _item(db, session, kind="lead", payload={"lead_name": "Arden at Acme"})

    result = _apply_wrapup(db, session, _approve(item.id), AE)

    assert result["results"][0]["status"] == "write_failed"
    assert _lead_touch(db) is None


def test_a_clock_that_cannot_open_does_not_undo_the_lead(db, writer, monkeypatch):
    """The lead is in the client's CRM. A commitments failure cannot revoke it."""
    monkeypatch.setattr(settings, "CCS_LEADS_ENABLED", True)

    def _boom(*_a, **_kw):
        raise RuntimeError("commitments table is unhappy")

    monkeypatch.setattr("rubik_core.cockpit.commitment_engine.open_commitment", _boom)
    session = _session(db)
    item = _item(db, session, kind="lead", payload={"lead_name": "Arden at Acme"})

    result = _apply_wrapup(db, session, _approve(item.id), AE)

    assert result["results"][0]["status"] == "written"
    assert db.get(CallExtractionItem, item.id).status == "written"
    assert _lead_touch(db) is None


# ── Finding 5 (second half) — the wrap-up clock is stopped by the wrap-up ─────


def test_completing_the_wrap_up_closes_the_wrap_up_commitment(db, writer):
    """An aging clock nobody can stop is worse than no clock."""
    session = _session(db)
    clock = _open_wrapup_clock(db, session)
    item = _item(db, session)

    _apply_wrapup(db, session, _approve(item.id), AE)

    db.refresh(clock)
    assert clock.closed_at is not None, "the wrap-up clock is still running"
    assert clock.state == "closed"
    # The only two close sources are manual and superseded, and completing the
    # review is a human action.
    assert clock.closed_source == "manual"
    assert clock.closed_evidence_ref == session.id
    assert _wrapup_audits(db)[-1].details["wrapup_commitment_closed"] is True


def test_nothing_here_closes_the_wrap_up_commitment_too(db, writer):
    """ "Nothing here" is a COMPLETED wrap-up, not a skipped one."""
    session = _session(db)
    clock = _open_wrapup_clock(db, session)

    result = _apply_wrapup(db, session, _approve(nothing_here=True), AE)

    db.refresh(clock)
    assert result["nothing_here"] is True
    assert clock.closed_at is not None
    assert clock.state == "closed"


def test_a_wrap_up_with_no_clock_still_wraps(db, writer):
    """The sweep's opener is best-effort; a missing clock is not an error."""
    session = _session(db)
    item = _item(db, session)

    result = _apply_wrapup(db, session, _approve(item.id), AE)

    db.refresh(session)
    assert session.status == "wrapped"
    assert result["results"][0]["status"] == "recorded"
    assert _wrapup_audits(db)[-1].details["wrapup_commitment_closed"] is False


def test_the_wrap_up_clock_closes_even_when_the_call_log_fails(db, writer):
    """The AE did the review. A HubSpot outage does not keep the clock running."""
    from rubik_core.retrieval.hubspot_write import HubSpotWriteError

    session = _session(db)
    clock = _open_wrapup_clock(db, session)
    item = _item(db, session)

    def _explode(*_a, **_kw):
        raise HubSpotWriteError("502 from HubSpot")

    writer.create_call = _explode

    result = _apply_wrapup(db, session, _approve(item.id), AE)

    db.refresh(clock)
    assert result["call_logged"]["status"] == "write_failed"
    assert clock.closed_at is not None


# ── Finding 12 — a host-stopped transcript must not look complete ─────────────
#
# ``artifactConfig.autoTranscriptionGeneration`` sets a DEFAULT, not a lock:
# Google's documentation says hosts and co-hosts can still override it during the
# meeting, and the entire Meet API offers three write methods (``spaces.create``,
# ``spaces.patch``, ``spaces.endActiveConference``) — none of which stops or
# starts transcription on a running conference. So an AE can switch it off
# mid-call and Cubie can neither prevent it nor notice it happening.
#
# What arrives afterwards is indistinguishable from a complete transcript at the
# API level: the conference reports ENDED, the sweep ingests it correctly, and
# the AE gets a wrap-up that looks entirely normal, built from part of the call.
# That is the failure this feature spends the rest of its code avoiding — a
# partial result presented as a complete one — and it lands in the worst possible
# place, because transcription gets stopped mid-call when the conversation turns
# to pricing, a competitor or a complaint, which is exactly where the SOW ask is.
#
# The router cannot detect it. It can only report what the transcript covered,
# which is what ``attrs["transcript_coverage"]`` carries and what these tests fix
# in place.

#: Any department: the transcript fence is decided by role, not by team.
CALLER_DEPARTMENT = "Sales"
SUPERVISOR = "harper@acme.example"

#: ``_session`` builds a 30-minute call (``started_at``/``ended_at``, one hour to
#: thirty minutes before the wrap-up), so every figure below is derived from it
#: rather than invented — the minutes on the payload have to agree with
#: ``duration_minutes`` on the same payload or the screen contradicts itself.
CALL_MS = 30 * 60 * 1000

#: The host stopped transcription at minute 17 of a 30-minute call. Ratio 0.57
#: and a 13-minute hole: both halves of the rule, which is the only combination
#: that flags.
STOPPED_AT_17 = {
    "covered_ms": 17 * 60 * 1000,
    "call_ms": CALL_MS,
    "ratio": (17 * 60 * 1000) / CALL_MS,
    "gap_ms": 13 * 60 * 1000,
    "truncated": True,
    "reason": "truncated",
}

#: A normal call. The last utterance lands 30 seconds before the conference
#: record closes because people say goodbye and hang up.
RAN_TO_THE_END = {
    "covered_ms": CALL_MS - 30_000,
    "call_ms": CALL_MS,
    "ratio": (CALL_MS - 30_000) / CALL_MS,
    "gap_ms": 30_000,
    "truncated": False,
    "reason": "covered",
}


def _with_coverage(db, session: CallSession, coverage: dict | None) -> CallSession:
    """Write ``attrs["transcript_coverage"]`` the way ingest does.

    ``attrs`` is REASSIGNED, never mutated: SQLAlchemy does not track in-place
    changes to a JSON column, so ``session.attrs["x"] = y`` emits no UPDATE and
    the test would pass against a row that never changed.
    ``transcript_ingest.mark_outcome`` reassigns for the same reason.
    """
    attrs = dict(session.attrs or {})
    if coverage is None:
        attrs.pop("transcript_coverage", None)
    else:
        attrs["transcript_coverage"] = coverage
    session.attrs = attrs
    db.commit()
    return session


def _wrapup(db, session: CallSession, user: UserProfile) -> dict:
    return asyncio.run(calls_router.get_wrapup(session.id, user=user, db=db))


def test_revoked_deal_hides_saved_call_and_blocks_approval(db, writer, monkeypatch):
    from rubik_core.call_capture.provenance import stamp_session_source

    revoked = _session(db)
    _item(db, revoked)
    unbound = _session(db, company=None, deal=None)
    unbound.hubspot_contact_id = None
    unbound.attrs = {"crm_provenance": stamp_session_source(None, None, None)}
    db.commit()
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, set()),
    )
    user = _profile()

    pending = asyncio.run(calls_router.pending_wrapups(user=user, db=db))
    assert [s["call_session_id"] for s in pending["sessions"]] == [unbound.id]
    for action in (
        lambda: _wrapup(db, revoked, user),
        lambda: asyncio.run(
            calls_router.approve_wrapup(
                revoked.id,
                WrapupApproveRequest(nothing_here=True),
                user=user,
                db=db,
            )
        ),
        lambda: _apply_wrapup(db, revoked, WrapupApproveRequest(nothing_here=True), AE),
    ):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 404
    assert writer.calls == []
    assert writer.companies == []


def test_legacy_saved_call_without_source_proof_is_hidden(db):
    session = _session(db)
    _item(db, session)
    session.attrs = {"company_name": "Former client"}
    db.commit()

    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert pending["sessions"] == []
    with pytest.raises(HTTPException) as error:
        _wrapup(db, session, _profile())
    assert error.value.status_code == 404


def test_pending_scans_past_a_full_page_of_unprovable_calls(db):
    for _ in range(50):
        legacy = _session(db)
        legacy.ended_at = T_OPENED - timedelta(hours=2)
        legacy.attrs = {"company_name": "Former client"}
    valid = _session(db)
    valid.ended_at = T_OPENED
    db.commit()

    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert [row["call_session_id"] for row in pending["sessions"]] == [valid.id]


def test_saved_call_with_mismatched_scope_is_hidden(db):
    session = _session(db)
    session.hubspot_deal_id = "deal-changed-without-restamp"
    db.commit()

    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert pending["sessions"] == []


def test_unresolved_call_with_empty_server_stamp_remains_visible(db):
    from rubik_core.call_capture.provenance import stamp_session_source

    session = _session(db, company=None, deal=None)
    session.hubspot_contact_id = None
    session.attrs = {"crm_provenance": stamp_session_source(None, None, None)}
    db.commit()

    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert [s["call_session_id"] for s in pending["sessions"]] == [session.id]
    assert pending["sessions"][0]["company_name"] is None
    assert _wrapup(db, session, _profile())["session"]["call_session_id"] == session.id


def test_late_suggestions_on_wrapped_call_return_to_ae_worklists(
    db, writer, monkeypatch
):
    from backend.routers import cockpit

    session = _session(db)
    session.status = "wrapped"
    session.wrapped_at = T_FIRST_APPROVE
    session.nothing_here = True
    db.commit()
    item = _item(db, session)

    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert [row["call_session_id"] for row in pending["sessions"]] == [session.id]
    assert pending["sessions"][0]["status"] == "wrapped"
    wrapup = _wrapup(db, session, _profile())
    assert wrapup["session"]["nothing_here"] is False
    assert wrapup["groups"][0]["items"][0]["id"] == item.id
    assert session.wrapped_at == T_FIRST_APPROVE

    block = cockpit._wrapups_block(
        db,
        AE,
        now=T_FIRST_APPROVE + timedelta(days=1),
        crm=cockpit._Crm(),
        commitments=[],
        placeholders={},
    )
    assert [row.id for row in block.items] == [session.id]
    assert "new suggestions" in block.items[0].subtitle

    _apply_wrapup(db, session, _approve(item.id), AE)
    assert session.wrapped_at == T_FIRST_APPROVE
    assert (
        asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))["sessions"]
        == []
    )


def test_old_extraction_proposals_do_not_reopen_wrapped_call(db):
    session = _session(db)
    session.status = "wrapped"
    session.wrapped_at = T_FIRST_APPROVE
    _item(db, session)
    db.add(
        CallExtraction(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            model="gemini-test",
            prompt_version="ccs-extract-v1",
            schema_version="1",
            raw_output={},
            created_at=datetime.utcnow() + timedelta(days=1),
        )
    )
    db.commit()

    assert (
        asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))["sessions"]
        == []
    )


def test_wrapped_failed_analysis_returns_for_owner_retry(db, writer, monkeypatch):
    from backend.routers import cockpit

    session = _session(db)
    prior = _item(db, session)
    prior.status = "rejected"
    session.status = "wrapped"
    session.wrapped_at = T_FIRST_APPROVE
    session.nothing_here = True
    session.last_extracted_entry_count = 1
    session.attrs = {
        **session.attrs,
        "extraction_retry": {"failed_at": "2026-10-30T09:00:00Z"},
    }
    db.add(
        CallTranscriptEntry(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            source="fathom",
            external_id=f"late-{session.id}",
            speaker_name="Client",
            text="Additional transcript arrived later.",
            sequence=2,
        )
    )
    unrelated = _session(db)
    unrelated.status = "wrapped"
    unrelated.wrapped_at = T_FIRST_APPROVE
    db.commit()

    marker_query = select(CallSession.id).where(failed_analysis_marker_exists())
    assert "->>" in str(marker_query.compile(dialect=postgresql.dialect()))
    assert db.execute(marker_query).scalars().all() == [session.id]

    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert [row["call_session_id"] for row in pending["sessions"]] == [session.id]
    assert pending["sessions"][0]["extraction"]["status"] == "retrying"
    wrapup = _wrapup(db, session, _profile())
    assert wrapup["groups"] == []
    assert wrapup["extraction"]["can_retry"] is True
    assert wrapup["session"]["nothing_here"] is False
    assert session.wrap_opened_at is None
    block = cockpit._wrapups_block(
        db,
        AE,
        now=T_FIRST_APPROVE + timedelta(days=1),
        crm=cockpit._Crm(),
        commitments=[],
        placeholders={},
    )
    assert [row.id for row in block.items] == [session.id]
    assert "retrying" in block.items[0].subtitle

    attempts = []

    def recovered(_db, call):
        attempts.append(call.id)
        return None

    monkeypatch.setattr("rubik_core.call_capture.extraction.run_extraction", recovered)
    response = asyncio.run(
        calls_router.retry_wrapup_extraction(session.id, user=_profile(), db=db)
    )
    assert response["extraction"]["status"] == "complete"
    assert attempts == [session.id]
    assert session.status == "wrapped"
    assert session.wrapped_at == T_FIRST_APPROVE
    assert session.last_extracted_entry_count == 2
    assert "extraction_retry" not in (session.attrs or {})
    assert db.execute(marker_query).scalars().all() == []
    assert (
        asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))["sessions"]
        == []
    )
    assert writer.calls == []

    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(
            calls_router.retry_wrapup_extraction(
                session.id, user=_profile("someone-else@acme.example"), db=db
            )
        )
    assert forbidden.value.status_code == 403


def test_old_failed_hubspot_write_survives_new_empty_extraction(db, writer):
    from backend.routers import cockpit
    from rubik_core.retrieval.hubspot_write import HubSpotWriteError

    session = _session(db)
    item = _item(db, session, kind="task", payload={"subject": "Follow up"})

    def failed_task(*_args, **_kwargs):
        raise HubSpotWriteError("HubSpot temporarily unavailable")

    writer.create_task = failed_task
    first = _apply_wrapup(db, session, _approve(item.id), AE)
    assert first["results"][0]["status"] == "write_failed"
    first_wrapped_at = session.wrapped_at
    db.add(
        CallExtraction(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            model="gemini-test",
            prompt_version="ccs-extract-v1",
            schema_version="1",
            raw_output={},
            created_at=datetime.utcnow() + timedelta(days=1),
        )
    )
    db.commit()

    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert [row["call_session_id"] for row in pending["sessions"]] == [session.id]
    payload = _wrapup(db, session, _profile())
    assert [row["id"] for group in payload["groups"] for row in group["items"]] == [
        item.id
    ]
    block = cockpit._wrapups_block(
        db,
        AE,
        now=T_FIRST_APPROVE + timedelta(days=1),
        crm=cockpit._Crm(),
        commitments=[],
        placeholders={},
    )
    assert [row.id for row in block.items] == [session.id]

    def recovered_task(properties, **_kwargs):
        writer.tasks.append(properties)
        return {"id": "hs-task-retried"}

    writer.create_task = recovered_task
    second = _apply_wrapup(db, session, _approve(item.id), AE)
    assert second["results"][0]["status"] == "written"
    assert session.wrapped_at == first_wrapped_at
    assert len(writer.calls) == 1
    assert len(writer.tasks) == 1
    assert (
        asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))["sessions"]
        == []
    )


def test_failed_analysis_stays_visible_and_cannot_be_approved(
    db, writer, monkeypatch, caplog
):
    from rubik_core.call_capture import sweep

    session = _session(db)
    _item(db, session)  # A previous run must not show stale proposals on retry.
    session.status = "transcript_ready"
    db.commit()
    secret = "PRIVATE_CALL_QUOTE_8cb7e6"

    def broken_provider(_db, _session):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        "rubik_core.call_capture.extraction.run_extraction", broken_provider
    )
    report = {
        "extractions_run": 0,
        "extractions_empty": 0,
        "extractions_failed": 0,
        "extractions_deferred": 0,
    }
    sweep._run_extraction(db, session, report, False)

    assert report["extractions_failed"] == 1
    assert session.status == "transcript_ready"
    assert set(session.attrs["extraction_retry"]) == {"failed_at"}
    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert [row["call_session_id"] for row in pending["sessions"]] == [session.id]
    assert pending["sessions"][0]["extraction"]["status"] == "retrying"
    payload = _wrapup(db, session, _profile())
    assert payload["groups"] == []
    assert payload["extraction"]["status"] == "retrying"
    assert payload["extraction"]["can_retry"] is True
    assert session.wrap_opened_at is None
    assert secret not in caplog.text
    assert secret not in str(session.attrs)
    assert secret not in str(pending)
    assert secret not in str(payload)

    with pytest.raises(HTTPException) as blocked:
        _apply_wrapup(db, session, _approve(nothing_here=True), AE)
    assert blocked.value.status_code == 409
    assert db.query(CallHubspotWrite).count() == 0
    assert writer.calls == []


def test_ae_can_retry_failed_analysis_without_a_hubspot_write(db, monkeypatch):
    session = _session(db)
    session.status = "transcript_ready"
    session.attrs = {
        **session.attrs,
        "extraction_retry": {"failed_at": "2026-08-27T09:00:00Z"},
    }
    db.commit()
    attempts = []

    def recovered(_db, call):
        attempts.append(call.id)
        return None

    monkeypatch.setattr("rubik_core.call_capture.extraction.run_extraction", recovered)
    response = asyncio.run(
        calls_router.retry_wrapup_extraction(session.id, user=_profile(), db=db)
    )

    assert response["extraction"]["status"] == "complete"
    assert attempts == [session.id]
    assert session.status == "extracted"
    assert "extraction_retry" not in (session.attrs or {})
    assert db.query(CallHubspotWrite).count() == 0

    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(
            calls_router.retry_wrapup_extraction(
                session.id, user=_profile("someone-else@acme.example"), db=db
            )
        )
    assert forbidden.value.status_code == 403


def test_ae_retry_returns_safe_conflict_during_cooldown(db, monkeypatch):
    session = _session(db)
    session.status = "transcript_ready"
    session.attrs = {
        **session.attrs,
        "extraction_retry": {"failed_at": "2026-08-27T09:00:00Z"},
    }
    db.commit()
    attempts = []

    def broken(_db, call):
        attempts.append(call.id)
        raise RuntimeError("PRIVATE_TRANSCRIPT_7c40")

    monkeypatch.setattr("rubik_core.call_capture.extraction.run_extraction", broken)
    first = asyncio.run(
        calls_router.retry_wrapup_extraction(session.id, user=_profile(), db=db)
    )
    assert first["extraction"]["status"] == "retrying"

    with pytest.raises(HTTPException) as conflict:
        asyncio.run(
            calls_router.retry_wrapup_extraction(session.id, user=_profile(), db=db)
        )
    assert conflict.value.status_code == 409
    assert (
        conflict.value.detail == "Analysis is already running or was retried recently"
    )
    assert "PRIVATE_TRANSCRIPT" not in conflict.value.detail
    assert attempts == [session.id]


def test_partial_analysis_counts_are_visible_without_raw_output(db):
    session = _session(db)
    _item(db, session)
    extraction = db.query(CallExtraction).filter_by(call_session_id=session.id).one()
    secret = "PRIVATE_CALL_QUOTE_29bc4f"
    extraction.raw_output = {
        "chunks_total": 3,
        "chunks_failed": 1,
        "chunks": [{"error": secret}],
    }
    db.commit()

    payload = _wrapup(db, session, _profile())
    assert payload["extraction"] == {
        "status": "partial",
        "chunks_failed": 1,
        "chunks_total": 3,
        "can_retry": False,
    }
    assert payload["groups"], "usable proposals remain reviewable"
    assert secret not in str(payload)
    pending = asyncio.run(calls_router.pending_wrapups(user=_profile(), db=db))
    assert pending["sessions"][0]["extraction"]["status"] == "partial"
    assert secret not in str(pending)


def _coverage(db, session: CallSession, user: UserProfile | None = None) -> dict | None:
    payload = _wrapup(db, session, user or _profile(department=CALLER_DEPARTMENT))
    return payload["session"]["transcript_coverage"]


def test_a_host_stopped_transcript_is_flagged_on_the_wrap_up(db):
    """17 minutes of a 30-minute call. The AE has to be told."""
    session = _with_coverage(db, _session(db), STOPPED_AT_17)
    _item(db, session)

    coverage = _coverage(db, session)

    assert coverage is not None, "a partial capture rendered as no information"
    assert coverage["truncated"] is True
    assert coverage["covered_minutes"] == 17.0
    assert coverage["call_minutes"] == 30.0
    assert coverage["gap_minutes"] == 13.0
    assert coverage["reason"] == "truncated"


def test_a_complete_transcript_is_not_flagged(db):
    """The goodbye gap is not a truncation, and must not be dressed up as one.

    A detector that fires on a call that ran to the end teaches the AE to ignore
    the banner, at which point the real one costs nothing to miss — the same
    reasoning that keeps the stage-lag threshold where it is.
    """
    session = _with_coverage(db, _session(db), RAN_TO_THE_END)
    _item(db, session)

    coverage = _coverage(db, session)

    assert coverage is not None
    assert coverage["truncated"] is False
    assert coverage["covered_minutes"] == 29.5
    assert coverage["gap_minutes"] == 0.5
    assert coverage["reason"] == "covered"


def test_a_session_with_no_coverage_key_reports_nothing_not_full_coverage(db):
    """Every call recorded before this shipped is in this state.

    ``null`` and ``truncated: false`` are different claims and the screen says
    different things about them: one is "we did not measure this", the other is
    "we measured this and it was fine". Defaulting the unmeasured ones to the
    reassuring answer would invent a guarantee for the entire back catalogue.
    """
    session = _session(db)
    _item(db, session)
    assert "transcript_coverage" not in (session.attrs or {})

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["session"]["transcript_coverage"] is None
    # The key is still present — the client distinguishes null from absent, and
    # the rest of the header is unaffected by the missing measurement.
    assert "transcript_coverage" in payload["session"]
    assert payload["session"]["duration_minutes"] == 30.0


def test_a_call_that_captured_nothing_is_zero_covered_not_unknown(db):
    """0.0 minutes is a measurement. It must not collapse into "no information".

    ``duration_minutes`` folds a falsy value to ``None`` one line above, so this
    is the exact mistake the serializer is one ``if`` away from making — and the
    session it would erase is the worst one: a call whose transcript stops at
    the first utterance. ``compute_coverage`` reaches this state with a known
    offset of zero, so it is ``truncated`` and not one of the unknown codes.
    """
    session = _with_coverage(
        db,
        _session(db),
        {
            "covered_ms": 0,
            "call_ms": CALL_MS,
            "ratio": 0.0,
            "gap_ms": CALL_MS,
            "truncated": True,
            "reason": "truncated",
        },
    )

    coverage = _coverage(db, session)

    assert coverage is not None, "a call that captured nothing reported as unknown"
    assert coverage["covered_minutes"] == 0.0
    assert coverage["truncated"] is True
    assert coverage["gap_minutes"] == 30.0


def test_coverage_without_a_call_duration_still_reports_what_was_captured(db):
    """An unknowable denominator is not a reason to withhold the numerator."""
    session = _with_coverage(
        db,
        _session(db),
        {
            "covered_ms": 8 * 60 * 1000,
            "call_ms": None,
            "ratio": None,
            "gap_ms": None,
            "truncated": False,
            "reason": "no_call_duration",
        },
    )

    coverage = _coverage(db, session)

    assert coverage == {
        "covered_minutes": 8.0,
        "call_minutes": None,
        "gap_minutes": None,
        "truncated": False,
        "reason": "no_call_duration",
    }


def test_the_supervisor_is_told_the_call_was_partly_captured_and_no_more(
    db, monkeypatch
):
    """The two halves of this cut in opposite directions, and both are the point.

    Coverage is METADATA — three durations and a code — so it is NOT gated on
    ``user_can_read_transcript_text``. Harper's worklist exists to chase work
    that has gone quiet, and "half of this call was never captured" is precisely
    the operational fact they are chasing; gating it would hide a data-quality
    problem from the one person whose job is finding it.

    The transcript itself stays fenced in the same breath (the access table's "no
    exception, including deals they are working"). The supervisor is admitted to
    the ROW here by a team scope — ``_authz.email_scope`` reads the EOS chart,
    which ``backend/tests/test_cockpit_transcript_fence.py`` owns and this suite
    has no tables for — and denied the TEXT by their transcript policy, which is
    the combination that proves the two decisions are independent.
    """
    monkeypatch.setattr(
        calls_router, "_team_emails", lambda _db, _user: frozenset({AE})
    )
    session = _with_coverage(db, _session(db), STOPPED_AT_17)
    _item(db, session)

    payload = _wrapup(db, session, _profile(SUPERVISOR, department=CALLER_DEPARTMENT))

    assert payload["session"]["can_read_transcript"] is False
    assert payload["session"]["transcript_coverage"]["truncated"] is True
    assert payload["session"]["transcript_coverage"]["covered_minutes"] == 17.0

    served = [item for group in payload["groups"] for item in group["items"]]
    assert served, "the fixture served no items, so the redaction proves nothing"
    assert all(item["evidence_text"] is None for item in served)
    assert all(item["evidence_context"] == [] for item in served)
    assert QUOTE not in json.dumps(payload, default=str)


def test_the_ae_gets_the_same_coverage_block_and_the_quotes_too(db):
    """The other side of the same assertion: coverage is not a consolation prize.

    The supervisor and the AE see the identical coverage block. If it were gated
    it would differ here, and the test above would pass for the wrong reason.
    """
    session = _with_coverage(db, _session(db), STOPPED_AT_17)
    _item(db, session)

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["session"]["can_read_transcript"] is True
    assert payload["session"]["transcript_coverage"] == {
        "covered_minutes": 17.0,
        "call_minutes": 30.0,
        "gap_minutes": 13.0,
        "truncated": True,
        "reason": "truncated",
    }
    served = [item for group in payload["groups"] for item in group["items"]]
    assert served[0]["evidence_text"] == QUOTE


def test_the_reason_code_cannot_carry_a_word_of_the_transcript(db, monkeypatch):
    """The one field on this block that is a string, held to a closed set.

    Coverage rides out past the transcript fence by design, so ``reason`` is the
    hole a future "helpful" ingest change would open: one sentence of context —
    "stopped after: <quote>" — and the supervisor is reading the call. Serving
    only ``COVERAGE_REASONS`` makes that impossible rather than unlikely, and
    costs a label when a code is unrecognized.
    """
    monkeypatch.setattr(
        calls_router, "_team_emails", lambda _db, _user: frozenset({AE})
    )
    session = _with_coverage(
        db, _session(db), {**STOPPED_AT_17, "reason": f"host stopped after: {QUOTE}"}
    )
    _item(db, session)

    payload = _wrapup(db, session, _profile(SUPERVISOR, department=CALLER_DEPARTMENT))

    coverage = payload["session"]["transcript_coverage"]
    assert coverage["reason"] in calls_router.COVERAGE_REASONS or (
        coverage["reason"] == "unknown"
    ), "the router served a reason code outside its own vocabulary"
    assert coverage["reason"] == "unknown"
    assert QUOTE not in json.dumps(payload, default=str)
    # The measurement itself survives the unrecognized label — the AE still gets
    # the warning, which is the part that matters.
    assert coverage["truncated"] is True


@pytest.mark.parametrize("coverage", [STOPPED_AT_17, RAN_TO_THE_END])
def test_every_reason_this_router_serves_is_in_the_closed_set(db, coverage):
    """Sweeps the fixtures rather than the one hostile case above."""
    session = _with_coverage(db, _session(db), coverage)

    served = _coverage(db, session)

    assert served["reason"] in calls_router.COVERAGE_REASONS
    assert served["reason"].replace("_", "").isalnum(), (
        "a reason code with punctuation or whitespace is prose, not a code"
    )


def test_the_router_admits_every_code_the_writer_can_actually_produce():
    """The closed set must not close over the PRODUCER. It nearly did.

    A whitelist and its writer are two lists, and two lists drift. The failure is
    silent by construction: an unrecognized code degrades to ``unknown``, the
    banner still renders, nothing 500s, and the reason on every Fathom call —
    ``fathom_entries_untimed``, described by the writer as the common case — is
    quietly replaced along with a warning per read. This is the assertion that
    catches it, and it is why ``COVERAGE_REASONS`` is BUILT from these constants
    rather than retyped beside them.
    """
    from rubik_core.call_capture import transcript_ingest

    produced = {
        value
        for name, value in vars(transcript_ingest).items()
        if name.startswith("COVERAGE_") and isinstance(value, str)
    }
    produced.discard(transcript_ingest.COVERAGE_KEY)  # the attrs key, not a code

    assert produced, "no coverage constants found — did the writer rename them?"
    assert produced <= calls_router.COVERAGE_REASONS, (
        "the writer emits reason codes the router would flatten to 'unknown': "
        f"{sorted(produced - calls_router.COVERAGE_REASONS)}"
    )


# ══ The gaps block on GET /wrapup ═════════════════════════════════════════════
#
# The reason the checklist feature exists at all: extraction can
# only find what was SAID, so a perfect extractor over an incomplete call still
# produces an incomplete HubSpot record — and the AE cannot tell, because the
# wrap-up looks clean. These two keys are what make it not look clean.
#
# Both failures they guard are silent. A merged coverage number is wrong for
# every product it merges and reads as authoritative; a 500 from an un-migrated
# script table would take the whole wrap-up down with the overlay.


def _script(db, session, *, product_key, name, facts, attach=True):
    """Author an approved script and attach it, the way POST /start does.

    The facts are a sample script's facts — a test fixture,
    not seeded content. Nothing in the shipped code authors a product script:
    that is Indigo's and Harper's, through the form.
    """
    from rubik_core.database.call_scripts import (
        CallScript,
        CallSessionScript,
        ScriptItem,
    )

    script = CallScript(
        id=uuid.uuid4().hex,
        product_key=product_key,
        name=name,
        version=1,
        is_active=True,
        approved_by="harper@acme.example",
        approved_at=datetime(2026, 8, 20, 9, 0, 0),
        source="manual",
    )
    db.add(script)
    for sequence, (fact_key, prompt_text, required, target) in enumerate(facts, 1):
        db.add(
            ScriptItem(
                id=uuid.uuid4().hex,
                script_id=script.id,
                sequence=sequence,
                prompt_text=prompt_text,
                fact_key=fact_key,
                hubspot_target=target,
                required=required,
            )
        )
    if attach:
        db.add(
            CallSessionScript(
                id=uuid.uuid4().hex,
                call_session_id=session.id,
                script_id=script.id,
                product_key=product_key,
            )
        )
    db.commit()
    return script


PLAN = (
    ("hours_per_site", "How many hours per site?", True, None),
    ("rate", "What rate did we quote?", True, None),
    ("commencement_date", "When would it start?", False, None),
)


def _with_script_facts(db, session, facts: list[dict]) -> None:
    """Put validated script facts on the session's extraction, as the run does."""
    extraction = (
        db.query(CallExtraction)
        .filter(CallExtraction.call_session_id == session.id)
        .first()
    )
    extraction.raw_output = {
        **(extraction.raw_output or {}),
        "script_products": ["implementation_plan"],
        "script_coverage": facts,
    }
    db.commit()


def test_a_call_with_no_checklist_reports_empty_gaps_not_a_missing_key(db):
    """Every call in the product is in this state until Harper and Indigo
    have authored content — the tables ship EMPTY. The keys are always present so
    the screen never has to branch on their existence."""
    session = _session(db)
    _item(db, session)

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["script_coverage"] == []
    assert payload["unscripted_products"] == []


def test_a_required_fact_nobody_discussed_comes_back_as_the_question_to_ask(db):
    """The acceptance criterion, at the router. ``rate`` was never discussed, so
    the AE reads "What rate did we quote?" — not a bare key, and not silence."""
    session = _session(db)
    _item(db, session)
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=PLAN,
    )
    _with_script_facts(
        db,
        session,
        [
            {
                "fact_key": "hours_per_site",
                "product_key": "implementation_plan",
                "value": "60",
                "evidence_text": QUOTE,
                "evidence_entry_id": None,
                "confidence": 0.9,
            }
        ],
    )

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    section = payload["script_coverage"][0]
    assert section["product_key"] == "implementation_plan"
    assert section["name"] == "Implementation Plan"
    assert section["missing_required"] == ["rate"]
    by_key = {row["fact_key"]: row for row in section["items"]}
    assert by_key["hours_per_site"]["found"] is True
    assert by_key["hours_per_site"]["value"] == "60"
    assert by_key["rate"]["found"] is False
    assert by_key["rate"]["prompt_text"] == "What rate did we quote?"
    # Optional gaps are shown but never demanded.
    assert by_key["commencement_date"]["found"] is False
    assert "commencement_date" not in section["missing_required"]


def test_coverage_is_reported_per_product_and_never_merged(db):
    """AEs failing to split leads into multiple products is the failure
    this workstream exists to fix. One merged percentage
    across two products is wrong for both and hides which one has the gap."""
    session = _session(db)
    _item(db, session)
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=PLAN,
    )
    _script(
        db,
        session,
        product_key="staff_training",
        name="Staff Training",
        facts=(("trainee_count", "How many trainees?", True, None),),
    )

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert [s["product_key"] for s in payload["script_coverage"]] == [
        "implementation_plan",
        "staff_training",
    ]
    assert payload["script_coverage"][0]["missing_required"] == [
        "hours_per_site",
        "rate",
    ]
    assert payload["script_coverage"][1]["missing_required"] == ["trainee_count"]


def test_a_product_discussed_with_no_checklist_loaded_is_offered_as_a_split(db):
    """The sentence the multi-product design exists for: "staff training
    was discussed but this call is only scripted for the implementation plan —
    split it into its own lead?" Lead↔deal is 1:1, and AEs generally do not
    split. This turns the step they skip into one click."""
    session = _session(db)
    _item(
        db,
        session,
        kind="lead",
        proposed="Acme - Staff Training",
        payload={"product": "staff_training"},
    )
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=PLAN,
    )

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["unscripted_products"] == ["staff_training"]


def test_a_product_the_call_was_scripted_for_is_not_reported_as_unscripted(db):
    session = _session(db)
    _item(
        db,
        session,
        kind="lead",
        proposed="Acme - Implementation Plan",
        payload={"product": "implementation_plan"},
    )
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=PLAN,
    )

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["unscripted_products"] == []


def test_a_rejected_proposal_never_ticks_a_checklist_box(db):
    """The AE saying in as many words that a proposal is wrong is the one signal
    that outranks the model. Reporting the fact as covered anyway would overrule
    the only person who listened to the call."""
    session = _session(db)
    item = _item(db, session)
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=(
            (
                "supplier",
                "Who do they use for payroll?",
                True,
                "company.supplier",
            ),
        ),
    )
    item.target_property = "supplier"
    item.status = "rejected"
    db.commit()

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["script_coverage"][0]["missing_required"] == ["supplier"]


def test_an_accepted_fact_still_counts_as_covered(db):
    """The mirror of the test above, and the reason coverage is not filtered to
    ``CLAIMABLE_ITEM_STATUSES``: a fact the AE already approved was still
    established by the call, and re-reading the wrap-up must not grow a gap."""
    session = _session(db)
    item = _item(db, session)
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=(
            (
                "supplier",
                "Who do they use for payroll?",
                True,
                "company.supplier",
            ),
        ),
    )
    item.target_property = "supplier"
    item.status = "written"
    db.commit()

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["script_coverage"][0]["missing_required"] == []
    assert payload["script_coverage"][0]["items"][0]["found"] is True


def test_a_broken_script_table_costs_the_gaps_and_never_the_wrap_up(db, monkeypatch):
    """``scripts.py`` deliberately does not swallow database errors, on the
    grounds that the caller who knows the screen still has to render is the one
    that should. This is that caller."""
    session = _session(db)
    _item(db, session)
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=PLAN,
    )

    def _boom(*_a, **_kw):
        raise RuntimeError("no such table: call_script_items")

    monkeypatch.setattr("rubik_core.call_capture.scripts.coverage_for", _boom)

    payload = _wrapup(db, session, _profile(department=CALLER_DEPARTMENT))

    assert payload["script_coverage"] == []
    assert payload["unscripted_products"] == []
    assert payload["groups"], "the proposals went down with the overlay"


def test_the_supervisor_sees_the_gaps_without_a_word_of_the_transcript(db, monkeypatch):
    """The gaps block is authored by Harper plus a value and an id — no quote.
    It has to stay that way: a supervisor reads the coverage and no transcript
    text at all, and a leak here would go around the field-level
    fence every other quote passes."""
    monkeypatch.setattr(
        calls_router, "_team_emails", lambda _db, _user: frozenset({AE})
    )
    session = _session(db)
    _item(db, session)
    _script(
        db,
        session,
        product_key="implementation_plan",
        name="Implementation Plan",
        facts=PLAN,
    )
    _with_script_facts(
        db,
        session,
        [
            {
                "fact_key": "hours_per_site",
                "product_key": "implementation_plan",
                "value": "60",
                "evidence_text": QUOTE,
                "evidence_entry_id": None,
                "confidence": 0.9,
            }
        ],
    )

    payload = _wrapup(db, session, _profile(SUPERVISOR, department=CALLER_DEPARTMENT))

    assert payload["session"]["can_read_transcript"] is False
    assert QUOTE not in json.dumps(payload, default=str)
    section = payload["script_coverage"][0]
    assert section["items"][0]["value"] == "60"
    assert section["missing_required"] == ["rate"]
