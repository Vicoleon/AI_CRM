"""Generated documents carry the workspace's company, never "None" (spec §4.1).

The company profile (Admin → Company setup) feeds the documents Cubie makes for
a workspace: the proposal letterhead, and here the Deep Prep PDF and Word
exports, the L10 recap email and the Scorecard and VTO upload templates. Each
names the company in its header and footer: the profile's name or legal name,
and the product's name until an admin saves one (Review Focus 1: never "None",
never an empty name before "— Confidential").
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core import brand
from rubik_core.company import profile as profile_mod
from rubik_core.company.profile import (
    CompanyProfile,
    company_name_for_documents,
    get_company_profile,
    save_company_profile,
)

ACME = CompanyProfile(name="Acme Corp", legal_name="Acme Corporation LLC")
LEGAL_ONLY = CompanyProfile(legal_name="Globex Holdings Ltd")
EMPTY = CompanyProfile()

# fpdf ends a stream with a bare "\n"; a trailing "\r" is the data's own last
# byte, so it must not be eaten as part of a "\r\n" separator.
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\nendstream", re.DOTALL)
_BAD = re.compile(r"\bNone\b|^\s*[—-] Confidential|\{company\}", re.MULTILINE)


def _pdf_text(data: bytes) -> str:
    parts = [data.decode("latin-1")]
    for raw in _PDF_STREAM.findall(data):
        try:
            parts.append(zlib.decompress(raw).decode("latin-1"))
        except zlib.error:
            continue
    return "\n".join(parts)


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith(".xml")
        )


def _xlsx_text(data: bytes) -> str:
    return _docx_text(data)


@pytest.fixture
def saved(monkeypatch):
    """Answer the profile lookup the renderers make when no name is passed."""

    def use(profile: CompanyProfile) -> None:
        monkeypatch.setattr(profile_mod, "get_company_profile", lambda db=None: profile)

    return use


# ── the name a document carries ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "profile, expected",
    [
        (ACME, "Acme Corp"),
        (LEGAL_ONLY, "Globex Holdings Ltd"),
        (EMPTY, brand.PRODUCT_NAME),
    ],
    ids=["name", "legal-name", "empty"],
)
def test_the_document_name_falls_back_to_the_product(profile, expected):
    assert company_name_for_documents(profile) == expected


def test_the_document_name_reads_the_saved_profile():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        assert company_name_for_documents(get_company_profile(db)) == "Cubie"
        save_company_profile(db, ACME)
        assert company_name_for_documents(get_company_profile(db)) == "Acme Corp"
    finally:
        db.close()
        engine.dispose()


# ── Deep Prep, PDF and Word ──────────────────────────────────────────────────


def _meeting_prep_pdf(**kwargs) -> str:
    from backend.routers.home import MeetingPrepResponse
    from rubik_core.deep_prep.pdf import render_meeting_prep_pdf

    resp = MeetingPrepResponse(title="Kickoff with Globex", executive_summary="Scope.")
    return _pdf_text(
        render_meeting_prep_pdf(resp, prepared_for="ana@acme.example", **kwargs)
    )


def _deep_prep_pdf(**kwargs) -> str:
    from rubik_core.deep_prep.models import DeepPrepResult, DeepPrepSection
    from rubik_core.deep_prep.pdf import render_deep_prep_pdf

    result = DeepPrepResult(
        title="Renewal with Initech",
        sections=[DeepPrepSection("Where things stand", "Body.", [])],
    )
    return _pdf_text(
        render_deep_prep_pdf(result, prepared_for="ana@acme.example", **kwargs)
    )


@pytest.mark.parametrize("render", [_meeting_prep_pdf, _deep_prep_pdf])
def test_the_pdf_names_a_saved_company(render, saved):
    saved(ACME)
    text = render()
    assert "ACME CORP  -  " in text  # the masthead kicker
    assert "Acme Corp - Confidential" in text  # the footer
    assert not _BAD.search(text)


@pytest.mark.parametrize("render", [_meeting_prep_pdf, _deep_prep_pdf])
def test_the_pdf_names_the_product_with_no_profile(render, saved):
    saved(EMPTY)
    text = render()
    assert f"{brand.PRODUCT_NAME.upper()}  -  " in text
    assert f"{brand.PRODUCT_NAME} - Confidential" in text
    assert not _BAD.search(text)


def test_a_passed_company_name_wins_over_the_profile(saved):
    saved(EMPTY)
    assert "Globex - Confidential" in _meeting_prep_pdf(company_name="Globex")


@pytest.mark.parametrize("deep", [False, True], ids=["meeting-prep", "deep-prep"])
@pytest.mark.parametrize(
    "profile, name", [(ACME, "Acme Corp"), (EMPTY, brand.PRODUCT_NAME)]
)
def test_the_word_export_names_the_company(saved, deep, profile, name):
    from rubik_core.deep_prep.docx import (
        render_deep_prep_docx,
        render_meeting_prep_docx,
    )

    saved(profile)
    if deep:
        data = render_deep_prep_docx(
            {"title": "Renewal with Initech", "sections": []}, prepared_for="a@x"
        )
    else:
        data = render_meeting_prep_docx({"title": "Kickoff"}, prepared_for="a@x")
    text = _docx_text(data)
    assert f"{name.upper()}  —  " in text
    assert f"{name} — Confidential" in text
    assert not _BAD.search(text)


# ── the L10 recap email ──────────────────────────────────────────────────────


@pytest.mark.parametrize("recording_title", [None, "L10 recording"])
@pytest.mark.parametrize(
    "profile, name", [(ACME, "Acme Corp"), (EMPTY, brand.PRODUCT_NAME)]
)
def test_the_l10_recap_names_the_company(saved, recording_title, profile, name):
    from backend.services.l10_summary_email import render_html, render_text

    saved(profile)
    args = dict(
        meeting_name="Weekly L10",
        when="Tuesday",
        summary="## Wins\n- Shipped",
        recording_title=recording_title,
    )
    html = render_html(**args)
    text = render_text(**args)
    assert f"{name} · L10 recap" in html
    assert text.startswith(f"{name} · L10 recap\nWeekly L10\n")
    assert not _BAD.search(html) and not _BAD.search(text)


def test_the_l10_recap_escapes_the_company_name():
    from backend.services.l10_summary_email import render_html

    html = render_html(
        meeting_name="L10",
        when="Tuesday",
        summary="",
        recording_title=None,
        company_name="<b>Acme</b>",
    )
    assert "&lt;b&gt;Acme&lt;/b&gt; · L10 recap" in html
    assert "<b>Acme</b>" not in html


# ── the Scorecard and VTO upload templates ───────────────────────────────────


def _scorecard(**kwargs) -> bytes:
    from datetime import date

    from rubik_core.ingestion.eos.scorecard_xlsx import (
        build_scorecard_template_workbook,
    )

    return build_scorecard_template_workbook(
        weeks=[date(2026, 8, 2)],
        team_labels=["Sales"],
        owner_emails=["ana@acme.example"],
        **kwargs,
    )


def _vto(**kwargs) -> bytes:
    from rubik_core.ingestion.eos.vto_xlsx import build_vto_template_workbook

    return build_vto_template_workbook(**kwargs)


@pytest.mark.parametrize("build", [_scorecard, _vto], ids=["scorecard", "vto"])
@pytest.mark.parametrize(
    "profile, name", [(ACME, "Acme Corp"), (EMPTY, brand.PRODUCT_NAME)]
)
def test_the_templates_name_the_company(saved, build, profile, name):
    from openpyxl import load_workbook

    saved(profile)
    data = build()
    workbook = load_workbook(io.BytesIO(data))
    heading = workbook["Instructions"]["A1"].value
    assert heading.startswith(f"{name} — ")
    assert workbook.properties.title == heading
    assert not _BAD.search(_xlsx_text(data))


@pytest.mark.parametrize("build", [_scorecard, _vto], ids=["scorecard", "vto"])
def test_a_template_takes_the_company_name_it_is_given(saved, build):
    from openpyxl import load_workbook

    saved(EMPTY)
    workbook = load_workbook(io.BytesIO(build(company_name="Initech")))
    assert workbook["Instructions"]["A1"].value.startswith("Initech — ")
