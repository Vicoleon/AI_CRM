"""Proposal Generator — the fence, the flow, and the gates.

What this suite pins, in the order the AE meets it:

1. **The brief only keeps what the call established.** An outcome whose quote
   is not in the transcript is dropped from the model's extraction (Sage's
   rule: Desired Outcomes are goals the prospect actually stated), and kept
   but unmarked when the AE wrote it themselves.
2. **The content cannot grow the scope.** A scope or outcome entry with an id
   the brief does not have is discarded; a missing one is filled from the
   brief's own words; a different number of notes resets to the brief's.
3. **Pricing is printed from the brief.** The PDF and the Word file carry the
   brief's figures whatever the prose said, and a SAVE badge appears only when
   a standard total exceeds the proposal total on the same line.
4. **The flow is idempotent and append-only.** A second start on the same call
   returns the same proposal; generate refuses before the brief is confirmed;
   every revision is a new version and the earlier one is untouched.
5. **Feature Access is the roster and the kill switch is the switch.** The
   registry carries the key, the client knows it, the AI-cost label exists.

Nothing here calls Vertex or HubSpot: the model is a recorder patched onto
``rubik_core.proposals.service._llm_json`` by string target, HubSpot context
is patched to a constant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from docx import Document
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.tests._legacy_brand import LEGACY_PATTERN
from backend.tests._pdf import pdf_tj_strings
from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.call_capture.provenance import ATTR_KEY, stamp_session_source
from rubik_core.database.call_capture import CallSession, CallTranscriptEntry
from rubik_core.database.models import Base
from rubik_core.database.proposals import Proposal, ProposalVersion
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT
from rubik_core.proposals import brief as B
from rubik_core.proposals import prompts, render, service
from rubik_core.tenancy import crm_visibility

AE = "sage@acme.example"

TRANSCRIPT = [
    ("Sage Lowe", "Thanks for joining. What are you hoping to get out of a review?"),
    (
        "Riley Fontaine",
        "Honestly we just want someone independent to look at Contoso.",
    ),
    (
        "Riley Fontaine",
        "Our investors keep asking for a report and we have nothing to hand them.",
    ),
    (
        "Sage Lowe",
        "Our coordinated review is normally sixty-five hundred, for you it would be fifty-five hundred.",
    ),
    ("Riley Fontaine", "Can you send me something in writing this week?"),
]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(service, "visible_source_ids", lambda *sources: sources)
    monkeypatch.setattr(
        crm_visibility,
        "visible_source_ids",
        lambda *sources: service.visible_source_ids(*sources),
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_call(db, *, with_transcript: bool = True) -> CallSession:
    session = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        hubspot_company_id="co-1",
        hubspot_contact_id="ct-1",
        hubspot_deal_id="dl-1",
        capture_consent="granted",
        status="extracted",
        origin=CALL_ORIGIN_ASSISTANT,
        # Relative, not a fixed date: recent_captured_calls() only lists the
        # last 14 days, so a hard-coded call date ages out two weeks later.
        started_at=datetime.utcnow() - timedelta(days=2, minutes=30),
        ended_at=datetime.utcnow() - timedelta(days=2),
        attrs={
            "company_name": "Globex Holding Company",
            ATTR_KEY: stamp_session_source("co-1", "ct-1", "dl-1"),
        },
    )
    db.add(session)
    db.flush()
    if with_transcript:
        for i, (speaker, text) in enumerate(TRANSCRIPT, start=1):
            db.add(
                CallTranscriptEntry(
                    id=uuid.uuid4().hex,
                    call_session_id=session.id,
                    source="fathom",
                    speaker_name=speaker,
                    text=text,
                    sequence=i,
                )
            )
    db.commit()
    return session


MODEL_BRIEF = {
    "service_title": "Operational Review",
    "engagement_summary": "Independent review of Contoso for Globex Holding Company.",
    "prepared_for": {"name": "", "title": "", "company": ""},
    "scope_items": [
        {
            "id": "s1",
            "title": "Coordinated onsite review",
            "detail": "Governance, controls, day-to-day operations.",
            "confirmed": True,
        },
    ],
    "desired_outcomes": [
        {
            "id": "o1",
            "outcome": "An independent look at Contoso",
            "quote": "we just want someone independent to look at Contoso",
            "entry_sequence": 2,
            "confirmed": True,
        },
        {
            "id": "o2",
            "outcome": "A report for the investors",
            "quote": "our investors keep asking for a report",
            "entry_sequence": 3,
            "confirmed": True,
        },
        # Invented — the words are not in the call.
        {
            "id": "o3",
            "outcome": "Faster invoice processing",
            "quote": "we need invoicing to be faster",
            "entry_sequence": 9,
            "confirmed": True,
        },
    ],
    "pricing": [
        {
            "id": "p1",
            "description": "Coordinated onsite review and report",
            "standard_rate": "$6,500",
            "proposal_price": "$5,500",
            "confirmed": True,
        },
    ],
    "notes": ["Travel is shared among participating clients."],
    "timeline": "",
    "open_questions": ["Confirm the optional site-level testing."],
}

MODEL_CONTENT = {
    "document_title": "Services Proposal",
    "headline": "Operational Review",
    "executive_summary": "Acme Consulting is pleased to submit this proposal to Globex Holding Company.",
    "scope_intro": "Acme Consulting will review:",
    "scope": [
        {
            "id": "s1",
            "heading": "Coordinated Onsite Review",
            "body": "Governance, operational controls and day-to-day operations.",
        },
        {
            "id": "s7",
            "heading": "Free site-level testing",
            "body": "The model added this.",
        },
    ],
    "outcomes_intro": "This engagement gives Globex Holding Company:",
    "outcomes": [
        {
            "id": "o1",
            "heading": "Independent Risk Assessment",
            "body": "An unbiased evaluation of Contoso.",
        },
        {
            "id": "o2",
            "heading": "Investor-Ready Report",
            "body": "A written report the investors accept.",
        },
    ],
    "pricing_intro": "Pricing reflects the shared-cost model. The fee is $4,000.",
    "notes": ["Travel is divided equally.", "A second note the brief never had."],
    "closing": "Acme Consulting welcomes the chance to discuss timing.",
}


class _Recorder:
    """Stands in for ``service._llm_json``; answers from a queue and records prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, system, prompt, *, model_id, temperature):
        self.calls.append({"system": system, "prompt": prompt, "model": model_id})
        if not self.answers:
            raise service.ModelFailed("no answer queued")
        return self.answers.pop(0)


def test_selected_proposal_provider_uses_json_system_and_temperature(monkeypatch):
    route = ModelRoute("acme", "proposal_generator", "meta", "muse-spark-1.3", "client")
    seen = []

    def resolve(feature, capability, *, default_model):
        assert (feature, capability, default_model) == (
            "proposal_generator",
            "json",
            "gemini-default",
        )
        return route

    def generate(selected, prompt, **kwargs):
        seen.append((selected, prompt, kwargs))
        return SimpleNamespace(text='{"ok":true}')

    monkeypatch.setattr(service, "resolve_model_route", resolve)
    monkeypatch.setattr(service, "generate_text_on_route", generate)
    monkeypatch.setattr(
        service,
        "get_genai_client",
        lambda: pytest.fail("selected provider fell back to managed Vertex"),
    )
    raw = service._llm_json(
        "system guidance", "customer prompt", model_id="gemini-default", temperature=0.3
    )
    assert raw == {"ok": True}
    assert service._model_label(raw, "gemini-default") == "meta:muse-spark-1.3"
    assert seen == [
        (
            route,
            "customer prompt",
            {
                "json_mode": True,
                "system_instruction": "system guidance",
                "temperature": 0.3,
            },
        )
    ]


def test_selected_proposal_failure_does_not_expose_model_text(monkeypatch, caplog):
    secret = "PRIVATE CUSTOMER PROPOSAL"
    route = ModelRoute("acme", "proposal_generator", "meta", "muse-spark-1.3", "client")
    monkeypatch.setattr(service, "resolve_model_route", lambda *a, **kw: route)

    def fail(*args, **kwargs):
        raise ProviderRequestError(secret)

    monkeypatch.setattr(service, "generate_text_on_route", fail)
    monkeypatch.setattr(
        service,
        "get_genai_client",
        lambda: pytest.fail("selected provider fell back to managed Vertex"),
    )
    with pytest.raises(service.SelectedProviderFailed) as error:
        service._llm_json(
            "system", "prompt", model_id="gemini-default", temperature=0.2
        )
    assert secret not in str(error.value)
    assert secret not in caplog.text

    monkeypatch.setattr(
        service,
        "generate_text_on_route",
        lambda *a, **kw: SimpleNamespace(text=secret),
    )
    with pytest.raises(service.SelectedProviderFailed) as malformed:
        service._llm_json(
            "system", "prompt", model_id="gemini-default", temperature=0.2
        )
    assert secret not in str(malformed.value)
    assert secret not in caplog.text


def test_managed_proposal_route_uses_selected_model(monkeypatch):
    route = ModelRoute(
        "acme", "proposal_generator", "vertex_managed", "gemini-2.5-pro", "cubie"
    )
    seen = []
    monkeypatch.setattr(service, "resolve_model_route", lambda *a, **kw: route)
    monkeypatch.setattr(
        service,
        "get_genai_client",
        lambda: SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: (
                    seen.append(kw) or SimpleNamespace(text='{"ok":true}')
                )
            )
        ),
    )
    raw = service._llm_json("system", "prompt", model_id="default", temperature=0.2)
    assert seen[0]["model"] == "gemini-2.5-pro"
    assert seen[0]["config"].response_mime_type == "application/json"
    assert seen[0]["config"].system_instruction == "system"
    assert seen[0]["config"].temperature == 0.2
    assert service._model_label(raw, "default") == "gemini-2.5-pro"


def test_managed_proposal_route_change_refuses_vertex_spend(monkeypatch):
    managed = ModelRoute(
        "acme", "proposal_generator", "vertex_managed", "gemini-2.5-pro", "cubie"
    )
    selected = ModelRoute(
        "acme", "proposal_generator", "meta", "muse-spark-1.3", "client"
    )
    routes = iter((managed, selected))
    monkeypatch.setattr(service, "resolve_model_route", lambda *a, **kw: next(routes))
    monkeypatch.setattr(
        service,
        "get_genai_client",
        lambda: pytest.fail("route changed before managed Vertex call"),
    )

    with pytest.raises(service.SelectedProviderFailed, match="route is unavailable"):
        service._llm_json("system", "prompt", model_id="default", temperature=0.2)


def test_selected_proposal_error_maps_to_safe_admin_guidance():
    from backend.routers.proposals import _map_error

    selected = _map_error(service.SelectedProviderFailed("PRIVATE CUSTOMER PROPOSAL"))
    assert selected.status_code == 502
    assert "admin" in selected.detail
    assert "PRIVATE CUSTOMER PROPOSAL" not in selected.detail
    managed = _map_error(service.ModelFailed("PRIVATE CUSTOMER PROPOSAL"))
    assert managed.status_code == 502
    assert "PRIVATE CUSTOMER PROPOSAL" not in managed.detail


HUBSPOT = {
    "company": {"id": "co-1", "name": "Globex Holding Company"},
    "contact": {
        "id": "ct-1",
        "name": "Riley Fontaine",
        "title": "Operations Manager",
        "email": "a@globex.example",
    },
    "deal": {"id": "dl-1", "dealname": "GHC - Contoso review"},
}


# ── 1. The brief keeps what the call established ────────────────────────────


def test_extraction_drops_an_outcome_whose_quote_is_not_in_the_transcript():
    text = "\n".join(f"[{i}] {s}: {t}" for i, (s, t) in enumerate(TRANSCRIPT, start=1))
    brief, dropped = B.normalize_brief(MODEL_BRIEF, transcript_text=text)
    ids = [o["id"] for o in brief["desired_outcomes"]]
    assert ids == ["o1", "o2"]
    assert any(d["reason"] == B.DROP_QUOTE_NOT_IN_TRANSCRIPT for d in dropped)
    # Punctuation and case do not defeat a real quote.
    assert B.quote_in_transcript("Our investors keep asking for a report!", text)


def test_an_ae_authored_outcome_is_kept_but_not_marked_as_heard():
    text = "[1] A: nothing relevant here at all"
    brief, dropped = B.normalize_brief(
        MODEL_BRIEF, transcript_text=text, keep_unquoted_outcomes=True
    )
    assert [o["id"] for o in brief["desired_outcomes"]] == ["o1", "o2", "o3"]
    assert all(
        o["quote"] == "" and o["confirmed"] is False for o in brief["desired_outcomes"]
    )
    assert not dropped


def test_a_pricing_line_with_no_price_is_never_confirmed_and_caps_hold():
    raw = {
        "service_title": "X",
        "pricing": [{"description": "Review", "proposal_price": "", "confirmed": True}],
        "scope_items": [{"title": f"Item {i}"} for i in range(12)],
    }
    brief, dropped = B.normalize_brief(raw)
    assert brief["pricing"][0]["confirmed"] is False
    assert len(brief["scope_items"]) == B.MAX_SCOPE_ITEMS
    assert (
        sum(d["reason"] == B.DROP_OVER_CAP for d in dropped) == 12 - B.MAX_SCOPE_ITEMS
    )
    # Ids are minted when missing and unique.
    assert len({s["id"] for s in brief["scope_items"]}) == B.MAX_SCOPE_ITEMS


# ── 2. The content cannot grow the scope ────────────────────────────────────


def test_content_is_fenced_to_the_brief():
    brief, _ = B.normalize_brief(
        MODEL_BRIEF, transcript_text="\n".join(t for _, t in TRANSCRIPT)
    )
    content, fence = B.normalize_content(MODEL_CONTENT, brief)
    assert [s["id"] for s in content["scope"]] == ["s1"]
    assert fence["dropped_scope"] == ["s7"]
    assert [o["id"] for o in content["outcomes"]] == ["o1", "o2"]
    # Two notes offered, one in the brief: the brief's words win.
    assert content["notes"] == brief["notes"]
    assert fence["notes_reset"] is True


def test_a_missing_scope_body_is_filled_from_the_brief_not_left_blank():
    brief, _ = B.normalize_brief(MODEL_BRIEF)
    content, fence = B.normalize_content({"scope": [], "outcomes": []}, brief)
    assert content["scope"][0]["heading"] == "Coordinated onsite review"
    assert content["scope"][0]["body"] == "Governance, controls, day-to-day operations."
    assert fence["filled_scope"] == ["s1"]
    assert [o["heading"] for o in content["outcomes"]] == [
        o["outcome"] for o in brief["desired_outcomes"]
    ]


# ── 3. Pricing is printed from the brief ────────────────────────────────────


def test_savings_badge_only_for_a_real_total_saving():
    assert (
        B.savings_label({"standard_rate": "$6,500", "proposal_price": "$5,500"})
        == "SAVE $1,000"
    )
    assert (
        B.savings_label({"standard_rate": "$5,500", "proposal_price": "$5,500"}) is None
    )
    assert B.savings_label({"standard_rate": "", "proposal_price": "$5,500"}) is None
    assert (
        B.savings_label({"standard_rate": "$250/hr", "proposal_price": "$225/hr"})
        is None
    )
    assert B.money_value("45 to 60 hours at $225 per hour") == 45.0


@pytest.mark.parametrize(
    ("standard", "proposal"),
    [
        ("$50/seat", "$40/seat"),
        ("$50 / user", "$40 / user"),
        ("$12/unit", "$10/unit"),
        ("$900/month", "$800/month"),
        ("$9,000/yr", "$8,000/yr"),
    ],
)
def test_a_per_unit_rate_is_never_a_saving(standard: str, proposal: str):
    """A per-seat, per-user, per-unit or per-period rate beside a lower one is
    a cheaper rate, not a total saved, so it gets no SAVE badge."""
    line = {"standard_rate": standard, "proposal_price": proposal}
    assert B.savings_label(line) is None


def _pdf_text(data: bytes) -> str:
    """The literal strings fpdf2 wrote, one per line, with every content stream
    inflated (shared reader: ``backend/tests/_pdf.py``).

    No pypdf in the test environment, and none needed: fpdf2 emits each cell
    as one ``(text) Tj`` operator, so the words a prospect would read are
    recoverable with zlib alone.
    """
    return "\n".join(pdf_tj_strings(data))


def test_pdf_and_docx_print_the_brief_price_not_the_prose_price():
    brief, _ = B.normalize_brief(MODEL_BRIEF)
    content, _ = B.normalize_content(MODEL_CONTENT, brief)
    pdf = render.render_pdf(brief, content)
    assert pdf[:4] == b"%PDF"
    text = _pdf_text(pdf)
    assert "$5,500" in text and "$6,500" in text and "SAVE $1,000" in text
    # The prose said $4,000 in pricing_intro — that sentence is prose and
    # prints as prose; the TABLE is what a prospect reads, and it is the brief's.
    assert "Service description".upper() in text.upper()
    assert "Free site-level testing" not in text  # fenced out upstream
    assert "Confidential Proposal" in text

    docx = Document(BytesIO(render.render_docx(brief, content)))
    cells = [c.text for t in docx.tables for r in t.rows for c in r.cells]
    assert "$5,500" in cells and "$6,500" in cells and "SAVE $1,000" in cells


def test_pdf_survives_curly_punctuation_and_non_latin1():
    brief, _ = B.normalize_brief(
        {**MODEL_BRIEF, "service_title": "Review — “quoted” • 日本"}
    )
    content, _ = B.normalize_content(MODEL_CONTENT, brief)
    assert render.render_pdf(brief, content)[:4] == b"%PDF"
    assert render.filename(brief, 3, "pdf") == "Prospect_Proposal_v3.pdf"


# ── 4. The flow ──────────────────────────────────────────────────────────────


def test_start_is_idempotent_and_fills_identity_from_hubspot(db):
    call = _seed_call(db)
    recorder = _Recorder(MODEL_BRIEF, MODEL_BRIEF)
    with (
        patch("rubik_core.proposals.service._llm_json", recorder),
        patch("rubik_core.proposals.service.hubspot_context", return_value=HUBSPOT),
    ):
        first = service.start_from_call(db, call, actor_email=AE)
        second = service.start_from_call(db, call, actor_email=AE)
    assert first.id == second.id
    assert len(recorder.calls) == 1, "the model is asked once per call"
    assert first.status == "brief" and first.brief_confirmed_at is None
    assert first.company_name == "Globex Holding Company"
    pf = first.brief["prepared_for"]
    assert pf == {
        "name": "Riley Fontaine",
        "title": "Operations Manager",
        "company": "Globex Holding Company",
    }
    assert first.brief["prepared_by"]["email"] == AE
    assert first.brief["ref_code"].startswith("P-GHC-2026-")
    # The invented outcome did not survive the transcript check.
    assert [o["id"] for o in first.brief["desired_outcomes"]] == ["o1", "o2"]
    # The raw answer is kept for tuning, untouched.
    assert len(first.brief_source["desired_outcomes"]) == 3
    # The transcript, not a summary, went to the model, with sequence numbers.
    assert "[2] Riley Fontaine:" in recorder.calls[0]["prompt"]


def test_proposal_and_version_record_selected_provider_model(db):
    call = _seed_call(db)
    recorder = _Recorder(
        service._ModelJSON(MODEL_BRIEF, model_label="meta:muse-spark-1.3"),
        service._ModelJSON(MODEL_CONTENT, model_label="meta:muse-spark-1.3"),
    )
    with (
        patch("rubik_core.proposals.service._llm_json", recorder),
        patch("rubik_core.proposals.service.hubspot_context", return_value=HUBSPOT),
    ):
        proposal = service.start_from_call(db, call, actor_email=AE)
        assert proposal.model == "meta:muse-spark-1.3"
        service.save_brief(db, proposal, proposal.brief, actor_email=AE)
        version = service.generate(db, proposal, actor_email=AE)
    assert version.model.startswith("meta:muse-spark-1.3 / ")


def test_start_refuses_a_call_with_no_transcript(db):
    call = _seed_call(db, with_transcript=False)
    with pytest.raises(service.NoTranscript):
        service.start_from_call(db, call, actor_email=AE)
    assert db.query(Proposal).count() == 0


def test_generate_requires_a_confirmed_brief_then_appends_versions(db):
    call = _seed_call(db)
    revised_brief = {
        **MODEL_BRIEF,
        "pricing": [{**MODEL_BRIEF["pricing"][0], "proposal_price": "$5,000"}],
    }
    recorder = _Recorder(
        MODEL_BRIEF,
        MODEL_CONTENT,
        {
            "brief": revised_brief,
            "content": MODEL_CONTENT,
            "change_summary": "Price changed to $5,000.",
        },
    )
    with (
        patch("rubik_core.proposals.service._llm_json", recorder),
        patch("rubik_core.proposals.service.hubspot_context", return_value=HUBSPOT),
    ):
        proposal = service.start_from_call(db, call, actor_email=AE)
        with pytest.raises(service.BriefNotConfirmed):
            service.generate(db, proposal, actor_email=AE)
        with pytest.raises(service.NothingToRevise):
            service.revise(db, proposal, "make it shorter", actor_email=AE)

        edited = dict(proposal.brief)
        edited["prepared_by"] = {
            "name": "Mallory",
            "title": "CEO",
            "email": "mallory@example.com",
        }
        service.save_brief(db, proposal, edited, actor_email=AE)
        assert proposal.brief_confirmed_at is not None
        # Provenance is the system's: the AE cannot rename who it is from.
        assert proposal.brief["prepared_by"]["email"] == AE

        v1 = service.generate(db, proposal, actor_email=AE)
        assert v1.version_no == 1 and v1.instruction is None
        assert proposal.status == "drafted"
        assert [s["id"] for s in v1.content["scope"]] == ["s1"]  # fenced

        v2 = service.revise(db, proposal, "change the price to 5,000", actor_email=AE)
        assert v2.version_no == 2
        assert v2.instruction == "change the price to 5,000"
        assert v2.change_summary == "Price changed to $5,000."
        assert v2.brief["pricing"][0]["proposal_price"] == "$5,000"
        # Append-only: v1 still says what it said.
        db.refresh(v1)
        assert v1.brief["pricing"][0]["proposal_price"] == "$5,500"
        # The proposal's current brief follows the latest version.
        assert proposal.brief["pricing"][0]["proposal_price"] == "$5,000"

    versions = db.query(ProposalVersion).filter_by(proposal_id=proposal.id).all()
    assert sorted(v.version_no for v in versions) == [1, 2]
    assert all(v.model.endswith(f"/ {prompts.PROMPT_VERSION}") for v in versions)
    # The revise prompt carried the AE's instruction and the current document.
    assert "INSTRUCTION FROM THE AE" in recorder.calls[2]["prompt"]


def test_a_model_failure_is_one_typed_error_and_writes_nothing(db):
    call = _seed_call(db)
    with (
        patch("rubik_core.proposals.service._llm_json", _Recorder()),
        patch("rubik_core.proposals.service.hubspot_context", return_value=HUBSPOT),
    ):
        with pytest.raises(service.ModelFailed):
            service.start_from_call(db, call, actor_email=AE)
    assert db.query(Proposal).count() == 0


def test_revoked_company_hides_saved_proposal_and_call_before_reprompt(db, monkeypatch):
    call = _seed_call(db)
    recorder = _Recorder(MODEL_BRIEF)
    with (
        patch("rubik_core.proposals.service._llm_json", recorder),
        patch("rubik_core.proposals.service.hubspot_context", return_value=HUBSPOT),
    ):
        proposal = service.start_from_call(db, call, actor_email=AE)
    assert service.proposal_visible(proposal)

    def revoked_company(companies, contacts, deals):
        return set(), contacts, deals

    monkeypatch.setattr(service, "visible_source_ids", revoked_company)
    assert service.list_proposals(db, actor_email=AE) == []
    assert service.recent_captured_calls(db, actor_email=AE) == []
    assert not service.proposal_visible(proposal)
    with pytest.raises(service.SourceAccessChanged):
        service.start_from_call(db, call, actor_email=AE)
    with pytest.raises(service.SourceAccessChanged):
        service.save_brief(db, proposal, proposal.brief, actor_email=AE)
    with pytest.raises(service.SourceAccessChanged):
        service.generate(db, proposal, actor_email=AE)
    with pytest.raises(service.SourceAccessChanged):
        service.revise(db, proposal, "change it", actor_email=AE)
    assert len(recorder.calls) == 1


def test_legacy_call_without_source_stamp_cannot_feed_a_proposal(db):
    call = _seed_call(db)
    call.attrs = {"company_name": "Globex Holding Company"}
    db.commit()

    assert service.recent_captured_calls(db, actor_email=AE) == []
    with pytest.raises(service.SourceAccessChanged):
        service.start_from_call(db, call, actor_email=AE)


def test_legacy_proposal_without_source_stamp_is_hidden_even_with_live_ids(db):
    call = _seed_call(db)
    row = Proposal(
        id=uuid.uuid4().hex,
        owner_email=AE,
        call_session_id=call.id,
        hubspot_company_id=call.hubspot_company_id,
        hubspot_contact_id=call.hubspot_contact_id,
        hubspot_deal_id=call.hubspot_deal_id,
        title="Review",
        status="brief",
        brief={"prepared_for": {"name": "Riley", "company": "Globex"}},
        brief_source={"prepared_for": {"company": "Globex"}},
    )
    db.add(row)
    db.commit()
    assert service.list_proposals(db, actor_email=AE) == []
    assert not service.proposal_visible(row)
    with pytest.raises(service.SourceAccessChanged):
        service.save_brief(db, row, row.brief, actor_email=AE)


def test_mixed_pipeline_proposals_only_list_the_live_company(db, monkeypatch):
    for company_id in ("co-1", "co-2"):
        db.add(
            Proposal(
                id=uuid.uuid4().hex,
                owner_email=AE,
                hubspot_company_id=company_id,
                company_name=f"Private {company_id}",
                title="Review",
                status="brief",
                brief={"prepared_for": {"company": f"Private {company_id}"}},
                brief_source={
                    "_crm_provenance": {
                        "schema_version": 1,
                        "workspace": "test",
                        "company_id": company_id,
                        "contact_id": None,
                        "deal_ids": [],
                        "unprovable": False,
                    }
                },
            )
        )
    db.commit()
    monkeypatch.setattr(
        service,
        "visible_source_ids",
        lambda companies, contacts, deals: (companies & {"co-1"}, contacts, deals),
    )
    rows = service.list_proposals(db, actor_email=AE)
    assert [row.hubspot_company_id for row in rows] == ["co-1"]


def test_proposal_list_scans_past_revoked_rows_to_fill_visible_limit(db, monkeypatch):
    now = datetime.utcnow()
    for company_id, updated_at in (
        ("co-1", now - timedelta(days=1)),
        ("co-2", now),
    ):
        db.add(
            Proposal(
                id=uuid.uuid4().hex,
                owner_email=AE,
                hubspot_company_id=company_id,
                company_name=company_id,
                title="Review",
                status="brief",
                brief_source={
                    "_crm_provenance": {
                        "schema_version": 1,
                        "workspace": "test",
                        "company_id": company_id,
                        "contact_id": None,
                        "deal_ids": [],
                        "unprovable": False,
                    }
                },
                updated_at=updated_at,
            )
        )
    db.commit()
    monkeypatch.setattr(
        service,
        "visible_source_ids",
        lambda companies, contacts, deals: (companies & {"co-1"}, contacts, deals),
    )
    assert [
        row.hubspot_company_id
        for row in service.list_proposals(db, actor_email=AE, limit=1)
    ] == ["co-1"]


def test_captured_call_list_scans_past_revoked_rows(db, monkeypatch):
    older = _seed_call(db, with_transcript=False)
    newer = _seed_call(db, with_transcript=False)
    newer.hubspot_company_id = "co-2"
    newer.ended_at = datetime.utcnow()
    db.commit()
    monkeypatch.setattr(
        service,
        "visible_source_ids",
        lambda companies, contacts, deals: (companies & {"co-1"}, contacts, deals),
    )
    rows = service.recent_captured_calls(db, actor_email=AE, limit=1)
    assert [row["call_session_id"] for row in rows] == [older.id]


# ── 5. Registration ──────────────────────────────────────────────────────────


def test_feature_is_registered_gated_and_costed():
    from backend.auth import user_can_access_proposals
    from backend.routers.admin import _FEATURE_LABELS
    from rubik_core.features import feature_by_key
    from rubik_core.proposals import COST_FEATURE

    spec = feature_by_key("proposal_generator")
    assert spec.kill_switch == "PROPOSALS_ENABLED"
    assert spec.group == "Sales"
    assert not LEGACY_PATTERN.search(spec.description + spec.notes)
    assert _FEATURE_LABELS[COST_FEATURE] == "Proposal Generator"
    assert callable(user_can_access_proposals)


def test_prompts_carry_the_untrusted_clause_and_the_fence_language():
    from rubik_core.company.profile import CompanyProfile
    from rubik_core.proposals import prompts

    profile = CompanyProfile()
    assert "UNTRUSTED" in prompts.brief_system(profile, [])
    assert "Never invent a rate" in prompts.brief_system(profile, [])
    assert "Do NOT add a service" in prompts.draft_system(profile)
    assert "NEVER write a price" in prompts.draft_system(profile)
    assert "Change only what the instruction asks" in prompts.revise_system(profile)
    p = prompts.brief_prompt(
        transcript_lines=["[1] A: hi"], hubspot={}, ae={"name": "x", "email": "x@y"}
    )
    assert "[1] A: hi" in p and '"desired_outcomes"' in p


# ── 6. Entry by Fathom recording (the Sales Coach hand-off) ──────────────────


def _fathom_meeting(rid="55001", *, host=AE, external=True):
    from datetime import timedelta

    start = (datetime.utcnow() - timedelta(hours=1)).replace(microsecond=0)
    return {
        "id": rid,
        "title": "Discovery call",
        "recording_start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recording_end_time": (start + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "recorded_by": {"email": host, "name": "Sage Lowe"},
        "calendar_invitees": [
            {"email": host, "is_external": False},
            {
                "email": "riley@globex.example" if external else "riley@acme.example",
                "is_external": external,
            },
        ],
        "transcript": [{"speaker_name": s, "text": t} for s, t in TRANSCRIPT],
    }


@pytest.fixture
def _fathom_env(monkeypatch, set_test_workspace_domains):
    from rubik_core.call_capture import fathom_attach as fa
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "CCS_FATHOM_ENABLED", True, raising=False)
    set_test_workspace_domains("acme.example")
    monkeypatch.setattr(fa, "resolve_domain_matches", lambda domains, **kw: {})
    monkeypatch.setattr(
        "rubik_core.call_capture.extraction.run_extraction", lambda db, session: None
    )


def test_from_fathom_attaches_on_demand_then_starts_the_proposal(db, _fathom_env):
    fetched: list[str] = []

    def fetch(rid):
        fetched.append(rid)
        return _fathom_meeting(rid)

    with (
        patch("rubik_core.proposals.service._llm_json", _Recorder(MODEL_BRIEF)),
        patch("rubik_core.proposals.service.hubspot_context", return_value=HUBSPOT),
    ):
        session = service.session_for_fathom(
            db, "55001", actor_email=AE, fetch_meeting=fetch
        )
        proposal = service.start_from_call(db, session, actor_email=AE)
    assert fetched == ["55001"]
    assert session.fathom_recording_id == "55001" and session.origin == "fathom"
    assert proposal.call_session_id == session.id
    # Second time: found in Cubie, Fathom is not asked again.
    again = service.session_for_fathom(db, "55001", actor_email=AE, fetch_meeting=fetch)
    assert again.id == session.id and fetched == ["55001"]


def test_from_fathom_refuses_the_right_way(db, _fathom_env):
    with pytest.raises(service.RecordingNotFound):
        service.session_for_fathom(
            db, "nope", actor_email=AE, fetch_meeting=lambda rid: None
        )
    with pytest.raises(service.CouldNotAttach) as exc:
        service.session_for_fathom(
            db,
            "55002",
            actor_email=AE,
            fetch_meeting=lambda rid: _fathom_meeting(rid, external=False),
        )
    assert "no external attendee" in str(exc.value)
    # Somebody else's recording: attached (it is a real client call) but not theirs.
    with pytest.raises(service.NotYourCall):
        service.session_for_fathom(
            db,
            "55003",
            actor_email="other@acme.example",
            fetch_meeting=lambda rid: _fathom_meeting(rid),
        )
    # An admin may open it for support.
    admin_view = service.session_for_fathom(
        db,
        "55003",
        actor_email="admin@acme.example",
        is_admin=True,
        fetch_meeting=lambda rid: None,
    )
    assert admin_view.fathom_recording_id == "55003"


# ── 7. The lists behind the Proposals page ──────────────────────────────────


def test_lists_are_scoped_to_the_ae_unless_admin_asks_for_all(db):
    mine = _seed_call(db)
    theirs = _seed_call(db)
    theirs.ae_email = "other@acme.example"
    theirs.attrs = {
        "company_name": "Acme",
        "fathom": {"title": "Acme <> Initech"},
        ATTR_KEY: stamp_session_source("co-1", "ct-1", "dl-1"),
    }
    theirs.origin = "fathom"
    db.commit()
    for call in (mine, theirs):
        db.add(
            Proposal(
                id=uuid.uuid4().hex,
                owner_email=call.ae_email,
                call_session_id=call.id,
                title="Review",
                status="brief",
                brief={"prepared_for": {"name": "A", "company": "X"}},
                brief_source={
                    "_crm_provenance": {
                        "schema_version": 1,
                        "workspace": "test",
                        "company_id": None,
                        "contact_id": None,
                        "deal_ids": [],
                        "unprovable": False,
                    }
                },
            )
        )
    db.commit()

    own = service.list_proposals(db, actor_email=AE)
    assert [p.owner_email for p in own] == [AE]
    everyone = service.list_proposals(db, actor_email=AE, include_all=True)
    assert len(everyone) == 2

    calls = service.recent_captured_calls(db, actor_email=AE)
    assert [c["call_session_id"] for c in calls] == [mine.id]
    assert calls[0]["proposal_id"] is not None
    all_calls = service.recent_captured_calls(db, actor_email=AE, include_all=True)
    assert len(all_calls) == 2
    fathom_row = next(c for c in all_calls if c["origin"] == "fathom")
    assert (
        fathom_row["title"] == "Acme <> Initech"
        and fathom_row["company_name"] == "Acme"
    )


def test_captured_calls_leave_out_confidential_and_old_and_untranscribed(db):
    from datetime import timedelta

    ok = _seed_call(db)
    suppressed = _seed_call(db)
    suppressed.capture_consent = "suppressed"
    old = _seed_call(db)
    old.ended_at = datetime.utcnow() - timedelta(days=40)
    silent = _seed_call(db)
    silent.status = "no_transcript"
    db.commit()
    ids = {
        c["call_session_id"] for c in service.recent_captured_calls(db, actor_email=AE)
    }
    assert ids == {ok.id}
