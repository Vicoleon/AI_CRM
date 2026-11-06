"""Generated downloads carry the Cubie brand and none of the old one.

Review Focus 4: a file someone downloads keeps whatever it says long after the
rebrand, so the download names and the brand marks inside the files are pinned
here. The Scorecard and VTO upload templates are named ``Cubie_…``; the XLSX
templates, the Meeting Prep PDF and Word exports and the Smart Teams working
guide (Word, PDF and the copy filed for retrieval) name Cubie, use its
colours, and match no banned term (``LEGACY_PATTERN``), in their visible text
or in the colours and metadata stored with it.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from backend.routers.home import MeetingPrepResponse
from backend.services.l10_summary_email import render_html
from backend.tests._legacy_brand import LEGACY_PATTERN
from backend.tests._pdf import pdf_text
from rubik_core import brand
from rubik_core.deep_prep.docx import render_meeting_prep_docx
from rubik_core.deep_prep.pdf import render_meeting_prep_pdf
from rubik_core.ingestion.eos.scorecard_xlsx import (
    build_scorecard_template_workbook,
    template_filename,
)
from rubik_core.ingestion.eos.vto_xlsx import (
    TEMPLATE_FILENAME as VTO_TEMPLATE_FILENAME,
)
from rubik_core.ingestion.eos.vto_xlsx import build_vto_template_workbook
from rubik_core.smart_teams import guide


def _legacy_hits(text: str) -> list[str]:
    return [match.group() for match in LEGACY_PATTERN.finditer(text)]


def _zip_text(data: bytes) -> str:
    """Every XML part of an Office file, as one string."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith((".xml", ".rels"))
        )


# -- download names ---------------------------------------------------------


@pytest.mark.parametrize("team_label", [None, "", "  ", "Sales", "Executive View"])
def test_scorecard_template_filename_is_cubie(team_label):
    name = template_filename(team_label)
    assert name.startswith("Cubie_Scorecard_")
    assert name.endswith("_template.xlsx")
    assert _legacy_hits(name) == []


def test_scorecard_template_filename_names_the_team():
    assert template_filename("Executive View") == (
        "Cubie_Scorecard_Executive_View_template.xlsx"
    )
    assert template_filename(None) == "Cubie_Scorecard_template.xlsx"


def test_vto_template_filename_is_cubie():
    assert VTO_TEMPLATE_FILENAME == "Cubie_VTO_template.xlsx"
    assert _legacy_hits(VTO_TEMPLATE_FILENAME) == []


# -- what the files say ----------------------------------------------------


def _scorecard_template() -> bytes:
    return build_scorecard_template_workbook(
        weeks=[date(2026, 8, 2), date(2026, 7, 26)],
        team_labels=["Sales"],
        owner_emails=["ana@acme.example"],
        current_team_label="Sales",
    )


@pytest.mark.parametrize(
    "build",
    [_scorecard_template, build_vto_template_workbook],
    ids=["scorecard", "vto"],
)
def test_xlsx_templates_carry_cubie_and_no_old_brand(build):
    text = _zip_text(build())
    assert _legacy_hits(text) == []
    assert brand.PRODUCT_NAME in text
    # The header row is filled with Cubie violet (openpyxl stores ARGB).
    assert brand.BRAND_HEX.lstrip("#") in text.upper()


def test_meeting_prep_word_export_carries_cubie_and_no_old_brand():
    data = render_meeting_prep_docx(
        {"title": "Kickoff with Globex", "executive_summary": "Scope and pricing."},
        prepared_for="ana@acme.example",
    )
    text = _zip_text(data)
    assert _legacy_hits(text) == []
    assert brand.PRODUCT_NAME.upper() in text  # the masthead kicker
    assert f"{brand.PRODUCT_NAME} — Confidential" in text  # the footer
    assert brand.BRAND_HEX.lstrip("#") in text.upper()  # the kicker colour


def test_meeting_prep_pdf_carries_cubie_and_no_old_brand():
    data = render_meeting_prep_pdf(
        MeetingPrepResponse(title="Kickoff with Globex", executive_summary="Scope."),
        prepared_for="ana@acme.example",
    )
    text = pdf_text(data)
    assert _legacy_hits(text) == []
    assert brand.PRODUCT_NAME.upper() in text  # the masthead kicker
    assert f"{brand.PRODUCT_NAME} - Confidential" in text  # the footer


@pytest.mark.parametrize("recording_title", [None, "L10 recording"])
def test_l10_recap_email_carries_cubie_and_no_old_brand(recording_title):
    html = render_html(
        meeting_name="Weekly L10",
        when="Tuesday",
        summary="## Wins\n- Shipped",
        recording_title=recording_title,
    )
    assert _legacy_hits(html) == []
    assert f"Written by {brand.PRODUCT_NAME}" in html
    assert brand.BRAND_HEX in html.upper()


# -- the Smart Teams working guide --------------------------------------------

GUIDE_OWNER = "pat@acme.example"


def _working_guide():
    """A guide for someone who has not finished the interview: the shortfall
    document, whose framing, body and provenance are all authored copy."""
    from rubik_core.database.smart_teams import SmartTeamsDocument, SmartTeamsProfile

    built = guide.build_guide(
        SmartTeamsProfile(user_email=GUIDE_OWNER, version=1), display_name="Pat Doe"
    )
    subject_key, version_key = guide.cache_keys_for(GUIDE_OWNER, 1)
    return SmartTeamsDocument(
        id="guide-1",
        kind=guide.KIND_PERSONAL,
        subject_emails=[GUIDE_OWNER],
        subject_key=subject_key,
        profile_versions={GUIDE_OWNER: 1},
        profile_version_key=version_key,
        status="ready",
        title=built.title,
        content_md=guide.render_markdown(built),
    )


def test_the_working_guide_provenance_names_cubie():
    provenance = guide.PROVENANCE_TEMPLATE.format(name="Pat Doe", version=1)
    assert brand.PRODUCT_NAME in provenance
    assert _legacy_hits(provenance) == []


@pytest.mark.parametrize(
    "export, read",
    [(guide.export_docx, _zip_text), (guide.export_pdf, pdf_text)],
    ids=["word", "pdf"],
)
def test_working_guide_exports_carry_cubie_and_no_old_brand(export, read):
    data, filename = export(_working_guide(), display_name="Pat Doe")
    text = read(data)
    assert _legacy_hits(text) == []
    assert _legacy_hits(filename) == []
    assert brand.PRODUCT_NAME in text


def test_the_working_guide_filed_for_retrieval_carries_no_old_brand():
    text = guide.rag_document_text(_working_guide(), display_name="Pat Doe")
    assert _legacy_hits(text) == []
