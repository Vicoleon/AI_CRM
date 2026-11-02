"""Proposal Generator — the workspace's own templates and catalog pricing.

What this suite pins (spec §4.2.5):

1. **No templates is the default, and it works.** The brief prompt offers no
   template, a key the model invents anyway is cleared, the document is built
   from the AI-written sections only, and no price reaches the prose: a
   sentence of model prose that states a figure is dropped.
2. **One template plus its catalog rows prices the proposal.** The rows become
   pricing lines on the brief, the priced table prints them, and the
   template's body prints in the admin's words — the writer sees its headings
   only.
3. **The letterhead is the company profile.** Empty profile: a plain masthead
   and "Confidential Proposal", never "None". Saved profile: its name.
4. **Templates are admin data.** Admin-only writes, a key that survives a
   rename, an inactive template that the brief form does not offer.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.tests._pdf import pdf_tj_strings
from rubik_core.company import catalog
from rubik_core.company.profile import CompanyProfile
from rubik_core.database.call_capture import CallSession, CallTranscriptEntry
from rubik_core.database.db import get_db
from rubik_core.database.models import Base
from rubik_core.database.proposals import Proposal, ProposalVersion
from rubik_core.database.schemas import UserProfile
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT
from rubik_core.proposals import brief as B
from rubik_core.proposals import prompts, render, service, templates
from rubik_core.proposals.brand import Letterhead, letterhead

AE = "rep@acme.example"
client = TestClient(app)

_MONEY = re.compile(r"\$\s?\d")

CATALOG_CSV = (
    "name,description,category,price,unit,url\n"
    "Nightly office clean,Every weeknight,Cleaning,1200,month,\n"
    "Deep carpet clean,Twice a year,Cleaning,450,,\n"
    "Window wash,,Cleaning,,,\n"
)
BODY = (
    "## How the service works\n"
    "Our crews arrive after hours and leave the floor spotless.\n\n"
    "## What is included\n"
    "- Desks and common areas\n"
    "- Waste and recycling\n"
)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(service, "visible_source_ids", lambda *sources: sources)
    # The proposal tests provide synthetic Call Capture rows. Stand in for the
    # call's live CRM proof while still honoring a later visibility revocation.
    monkeypatch.setattr(
        service,
        "session_source_visible",
        lambda session: service._sources_visible(service._ids_from_columns(session)),
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def _call(db) -> CallSession:
    s = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        hubspot_company_id="co-1",
        capture_consent="granted",
        status="extracted",
        origin=CALL_ORIGIN_ASSISTANT,
        started_at=datetime.utcnow() - timedelta(hours=2),
        ended_at=datetime.utcnow() - timedelta(hours=1),
        attrs={"company_name": "Globex"},
    )
    db.add(s)
    db.flush()
    db.add(
        CallTranscriptEntry(
            id=uuid.uuid4().hex,
            call_session_id=s.id,
            source="fathom",
            speaker_name="Pat Doe",
            text="We need the offices cleaned every night before the audit.",
            sequence=1,
        )
    )
    db.commit()
    return s


class _Recorder:
    """Stands in for the model: returns canned answers, keeps the prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system, prompt, *, model_id, temperature):
        self.calls.append((system, prompt))
        return self.answers.pop(0)


_BRIEF = {
    "service_title": "Nightly Office Cleaning",
    "prepared_for": {"name": "Pat Doe", "title": "", "company": "Globex"},
    "scope_items": [{"id": "s1", "title": "Nightly clean", "detail": "Every night."}],
    "desired_outcomes": [],
    "pricing": [],
}
_CONTENT = {
    "executive_summary": (
        "Globex needs spotless offices before its audit. "
        "The service costs $1,200 a month. We start next week."
    ),
    "scope": [{"id": "s1", "heading": "Nightly clean", "body": "Every weeknight."}],
    "pricing_intro": "The investment is set out below.",
}


def _pdf_text(data: bytes) -> str:
    """The words on the PDF's pages, whitespace collapsed (shared reader:
    ``backend/tests/_pdf.py``)."""
    return " ".join(" ".join(pdf_tj_strings(data)).split())


def _make_template(db, **overrides) -> templates.Template:
    rows, errors = catalog.parse_catalog_csv(CATALOG_CSV)
    assert errors == []
    catalog.replace_catalog(db, rows)
    payload = {
        "name": "Office Cleaning",
        "body_md": BODY,
        "catalog_item_names": ["Nightly office clean", "Deep carpet clean"],
        **overrides,
    }
    return templates.create_template(db, payload)


# ── 1. No templates ──────────────────────────────────────────────────────────


def test_no_templates_builds_from_ai_sections_only_and_keeps_prices_out_of_prose(db):
    call = _call(db)
    recorder = _Recorder({**_BRIEF, "template_key": "invented"}, _CONTENT)
    with (
        patch("rubik_core.proposals.service._llm_json", recorder),
        patch("rubik_core.proposals.service.hubspot_context", return_value={}),
    ):
        p = service.start_from_call(db, call, actor_email=AE)
        assert p.brief["template"] == {"key": ""}
        assert p.brief["pricing"] == []
        service.save_brief(db, p, p.brief, actor_email=AE)
        version = service.generate(db, p, actor_email=AE)

    brief_system, _ = recorder.calls[0]
    assert "no proposal templates" in brief_system
    assert "TEMPLATE PAGES" not in recorder.calls[1][1]
    # The priced sentence is gone, the rest of the paragraph stays.
    summary = version.content["executive_summary"]
    assert (
        summary == "Globex needs spotless offices before its audit. We start next week."
    )
    for text in (summary, version.content["pricing_intro"]):
        assert not _MONEY.search(text)

    pdf = _pdf_text(render.render_pdf(version.brief, version.content))
    assert "EXECUTIVE SUMMARY" in pdf
    assert not _MONEY.search(pdf)
    assert "HOW THE SERVICE WORKS" not in pdf


def test_a_figure_in_a_reworded_note_falls_back_to_the_brief_note():
    brief, _ = B.normalize_brief(
        {"service_title": "x", "scope_items": [], "notes": ["Travel billed at cost."]}
    )
    content, fence = B.normalize_content(
        {"notes": ["Travel is billed at $90 an hour."]}, brief
    )
    assert content["notes"] == ["Travel billed at cost."]
    _, fence = B.normalize_content({"closing": "Sign by Friday. Save $500."}, brief)
    assert fence["priced_sentences"] == 1


# ── 2. One template plus catalog rows ────────────────────────────────────────


def test_a_template_with_catalog_rows_prices_the_proposal_from_the_rows(db):
    tpl = _make_template(db)
    call = _call(db)
    recorder = _Recorder({**_BRIEF, "template_key": tpl.key}, _CONTENT)
    with (
        patch("rubik_core.proposals.service._llm_json", recorder),
        patch("rubik_core.proposals.service.hubspot_context", return_value={}),
    ):
        p = service.start_from_call(db, call, actor_email=AE)
        service.save_brief(db, p, p.brief, actor_email=AE)
        version = service.generate(db, p, actor_email=AE)

    assert f'"{tpl.key}": Office Cleaning' in recorder.calls[0][0]
    assert p.brief["template"] == {"key": tpl.key}
    assert [
        (line["description"], line["proposal_price"]) for line in p.brief["pricing"]
    ] == [("Nightly office clean", "$1,200/month"), ("Deep carpet clean", "$450")]
    # The writer gets the template's headings, never its words.
    draft_prompt = recorder.calls[1][1]
    assert "- How the service works" in draft_prompt
    assert "spotless" not in draft_prompt.split("TEMPLATE PAGES", 1)[1]

    looks = {"template": templates.get(db, tpl.key)}
    pdf = _pdf_text(render.render_pdf(version.brief, version.content, **looks))
    assert "$1,200/month" in pdf and "$450" in pdf
    assert "HOW THE SERVICE WORKS" in pdf and "Waste and recycling" in pdf
    doc = Document(BytesIO(render.render_docx(version.brief, version.content, **looks)))
    words = " ".join(p.text for p in doc.paragraphs)
    assert "HOW THE SERVICE WORKS" in words and "Desks and common areas" in words
    cells = " ".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
    assert "$1,200/month" in cells


def test_a_price_spoken_on_the_call_is_not_overwritten_by_the_list_price(db):
    tpl = _make_template(db)
    brief = {
        **_BRIEF,
        "template_key": tpl.key,
        "pricing": [
            {
                "id": "p1",
                "description": "Nightly office clean",
                "proposal_price": "$999",
            }
        ],
    }
    with (
        patch("rubik_core.proposals.service._llm_json", _Recorder(brief)),
        patch("rubik_core.proposals.service.hubspot_context", return_value={}),
    ):
        p = service.start_from_call(db, _call(db), actor_email=AE)
    lines = {line["description"]: line["proposal_price"] for line in p.brief["pricing"]}
    assert lines == {"Nightly office clean": "$999", "Deep carpet clean": "$450"}
    assert len({line["id"] for line in p.brief["pricing"]}) == 2


def test_an_inactive_template_is_not_offered_and_its_key_is_cleared(db):
    tpl = _make_template(db, active=False)
    assert templates.list_templates(db, active_only=True) == []
    assert templates.get(db, tpl.key) is None
    with (
        patch(
            "rubik_core.proposals.service._llm_json",
            _Recorder({**_BRIEF, "template_key": tpl.key}),
        ),
        patch("rubik_core.proposals.service.hubspot_context", return_value={}),
    ):
        p = service.start_from_call(db, _call(db), actor_email=AE)
    assert p.brief["template"] == {"key": ""} and p.brief["pricing"] == []


def test_retiring_a_template_keeps_its_write_up_on_proposals_that_carry_it(
    db, monkeypatch
):
    """Unticking "Offer it on new proposals" is the non-destructive retire.

    New proposals stop being offered the template, but one already drafted
    with it re-downloads with the write-up and regenerates with its TEMPLATE
    PAGES. Only Delete takes the write-up off existing proposals.
    """
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "PROPOSALS_ENABLED", True)
    tpl = _make_template(db)
    recorder = _Recorder({**_BRIEF, "template_key": tpl.key}, _CONTENT, _CONTENT)
    with (
        patch("rubik_core.proposals.service._llm_json", recorder),
        patch("rubik_core.proposals.service.hubspot_context", return_value={}),
    ):
        p = service.start_from_call(db, _call(db), actor_email=AE)
        service.save_brief(db, p, p.brief, actor_email=AE)
        first = service.generate(db, p, actor_email=AE)
        retired = templates.update_template(db, tpl.id, {"active": False})
        regenerated = service.generate(db, p, actor_email=AE)

    assert retired is not None and retired.active is False
    assert templates.get(db, tpl.key) is None  # no longer offered
    regenerate_prompt = recorder.calls[2][1]
    assert "TEMPLATE PAGES" in regenerate_prompt
    assert "- How the service works" in regenerate_prompt

    def downloaded(version_no: int, fmt: str) -> str:
        res = client.get(
            f"/api/proposals/{p.id}/versions/{version_no}/download",
            params={"format": fmt},
        )
        assert res.status_code == 200, res.text
        if fmt == "pdf":
            return _pdf_text(res.content)
        return " ".join(par.text for par in Document(BytesIO(res.content)).paragraphs)

    _as(AE, ["user"])
    with patch(
        "backend.routers.proposals.user_can_access_proposals", return_value=True
    ):
        for version in (first, regenerated):
            assert "HOW THE SERVICE WORKS" in downloaded(version.version_no, "pdf")
            assert "Desks and common areas" in downloaded(version.version_no, "docx")
        templates.delete_template(db, tpl.id)
        assert "HOW THE SERVICE WORKS" not in downloaded(first.version_no, "pdf")


def test_body_blocks_parse_headings_bullets_and_paragraphs():
    blocks = templates.body_blocks("Intro line\ncontinues.\n\n# Scope\n* one\n2. two\n")
    assert [(b.kind, b.text) for b in blocks] == [
        ("paragraph", "Intro line continues."),
        ("heading", "Scope"),
        ("bullet", "one"),
        ("bullet", "two"),
    ]
    tpl = templates.Template(1, "k", "Named", "Just text.", (), True)
    assert templates.printed_blocks(tpl)[0] == templates.Block("heading", "Named")


def test_normalize_template_keeps_only_a_key_shaped_value():
    assert B.normalize_template(None) == {"key": ""}
    assert B.normalize_template("office_cleaning") == {"key": "office_cleaning"}
    assert B.normalize_template({"key": "Not A Key!", "sections": ["x"]}) == {"key": ""}
    old = {"key": "office_cleaning", "sections": ["overview"]}
    assert B.normalize_template(old) == {"key": "office_cleaning"}


# ── 3. The letterhead ────────────────────────────────────────────────────────


def _rendered(brief_overrides=None, **looks) -> tuple[str, str]:
    brief, _ = B.normalize_brief({**_BRIEF, **(brief_overrides or {})})
    content, _ = B.normalize_content(_CONTENT, brief)
    pdf = _pdf_text(render.render_pdf(brief, content, **looks))
    doc = Document(BytesIO(render.render_docx(brief, content, **looks)))
    return pdf, doc.sections[0].footer.paragraphs[0].text


def test_an_empty_profile_prints_a_neutral_letterhead():
    pdf, footer = _rendered(letterhead=letterhead(CompanyProfile()))
    assert "Confidential Proposal" in pdf and footer == "Confidential Proposal"
    assert "None" not in pdf


def test_a_saved_profile_is_the_letterhead():
    head = letterhead(
        CompanyProfile(
            name="Acme Corp",
            legal_name="Acme Corporation LLC",
            address="1 Main St\nSpringfield",
            website="acme.example",
        )
    )
    assert head == Letterhead(
        name="Acme Corp",
        legal_name="Acme Corporation LLC",
        address="1 Main St\nSpringfield",
        website="acme.example",
    )
    pdf, footer = _rendered(letterhead=head)
    assert "ACME CORPORATION LLC" in pdf
    assert "1 Main St, Springfield | acme.example" in pdf
    assert footer == "Acme Corporation LLC | Confidential Proposal"
    assert render.filename({"prepared_for": {"company": "Globex"}}, 2, "pdf") == (
        "Globex_Proposal_v2.pdf"
    )


# ── 4. Admin data ────────────────────────────────────────────────────────────


def _as(email: str, roles: list[str]) -> None:
    app.dependency_overrides[get_current_user] = lambda: UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department="Sales",
        roles=roles,
        created_at="2026-10-28T00:00:00",
        updated_at="2026-10-28T00:00:00",
    )


def test_template_writes_are_admin_only(db):
    _as(AE, ["user"])
    res = client.post("/api/company/proposal-templates", json={"name": "Hijack"})
    assert res.status_code == 403
    assert templates.list_templates(db) == []


def test_an_admin_creates_renames_and_deletes_a_template(db):
    _as("admin@acme.example", ["admin", "user"])
    res = client.post(
        "/api/company/proposal-templates",
        json={"name": "Office Cleaning", "body_md": BODY},
    )
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["key"] == "office_cleaning" and created["active"] is True
    renamed = client.put(
        f"/api/company/proposal-templates/{created['id']}",
        json={"name": "Office Care", "catalog_item_names": ["Window wash"]},
    ).json()
    assert renamed["key"] == "office_cleaning" and renamed["name"] == "Office Care"
    assert renamed["body_md"] == BODY.strip()
    blank = client.put(
        f"/api/company/proposal-templates/{created['id']}", json={"name": " "}
    )
    assert blank.status_code == 400
    listing = client.get("/api/company/proposal-templates").json()["templates"]
    assert [t["name"] for t in listing] == ["Office Care"]
    gone = client.delete(f"/api/company/proposal-templates/{created['id']}")
    assert gone.status_code == 200
    assert (
        client.delete(f"/api/company/proposal-templates/{created['id']}").status_code
        == 404
    )


def test_the_brief_form_listing_offers_active_templates_with_list_prices(
    db, monkeypatch
):
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "PROPOSALS_ENABLED", True)
    tpl = _make_template(db)
    _make_template(db, name="Retired", active=False)
    _as(AE, ["user"])
    with patch(
        "backend.routers.proposals.user_can_access_proposals", return_value=True
    ):
        res = client.get("/api/proposals/templates")
    assert res.status_code == 200, res.text
    listing = res.json()["templates"]
    assert [t["key"] for t in listing] == [tpl.key]
    assert listing[0]["headings"] == ["How the service works", "What is included"]
    assert listing[0]["price_options"] == [
        {"description": "Nightly office clean", "standard_rate": "$1,200/month"},
        {"description": "Deep carpet clean", "standard_rate": "$450"},
    ]


def test_revoked_company_blocks_saved_proposal_routes_and_download(db, monkeypatch):
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "PROPOSALS_ENABLED", True)
    call = _call(db)
    proposal = Proposal(
        id=uuid.uuid4().hex,
        owner_email=AE,
        call_session_id=call.id,
        hubspot_company_id=call.hubspot_company_id,
        company_name="Globex",
        title="Private proposal",
        status="drafted",
        brief=_BRIEF,
        brief_source={
            "_crm_provenance": {
                "schema_version": 1,
                "workspace": "test",
                "company_id": "co-1",
                "contact_id": None,
                "deal_ids": [],
                "unprovable": False,
            }
        },
    )
    db.add(proposal)
    db.add(
        ProposalVersion(
            id=uuid.uuid4().hex,
            proposal_id=proposal.id,
            version_no=1,
            brief=_BRIEF,
            content=_CONTENT,
            created_by=AE,
        )
    )
    db.commit()
    monkeypatch.setattr(
        service,
        "visible_source_ids",
        lambda companies, contacts, deals: (set(), contacts, deals),
    )
    _as(AE, ["user"])
    with patch(
        "backend.routers.proposals.user_can_access_proposals", return_value=True
    ):
        listing = client.get("/api/proposals")
        assert listing.status_code == 200
        assert listing.json()["proposals"] == []
        paths = (
            ("get", f"/api/proposals/{proposal.id}", None),
            ("get", f"/api/proposals/{proposal.id}/versions/1/download", None),
            ("get", f"/api/proposals/for-call/{call.id}", None),
            ("post", f"/api/proposals/from-call/{call.id}", None),
            ("put", f"/api/proposals/{proposal.id}/brief", {"brief": _BRIEF}),
            ("post", f"/api/proposals/{proposal.id}/generate", None),
            (
                "post",
                f"/api/proposals/{proposal.id}/revise",
                {"instruction": "shorter"},
            ),
            ("post", f"/api/proposals/{proposal.id}/archive", None),
        )
        for method, path, payload in paths:
            response = (
                getattr(client, method)(path, json=payload)
                if payload
                else getattr(client, method)(path)
            )
            assert response.status_code == 404, (method, path, response.text)
            assert "Globex" not in response.text


def test_prompts_list_templates_by_key_and_name():
    tpl = templates.Template(7, "office_cleaning", "Office Cleaning", BODY, (), True)
    system = prompts.brief_system(CompanyProfile(), [tpl])
    assert '- "office_cleaning": Office Cleaning' in system
    assert '"template_key"' in prompts.BRIEF_CONTRACT
