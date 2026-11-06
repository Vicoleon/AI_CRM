"""Deep Prep PDF renderer — produces a valid PDF and survives messy LLM text."""

import json

from backend.routers.home import MeetingPrepResponse
from rubik_core.deep_prep.pdf import (
    _BAND_H,
    _masthead,
    _PrepPDF,
    _stakeholders,
    render_meeting_prep_pdf,
)


def _resp(**kw) -> MeetingPrepResponse:
    base = dict(
        title="Initech Rollout SOW — Working Session",  # em dash on purpose
        executive_summary="The team is preparing the SOW for Initech's rollout "
        "engagement. **No** prior-bid outcome is stated in the records.",
        meeting_objective="Align on scope, hours, and pricing.",
        discovery_questions=["What's changed since last quarter?", "Who signs off?"],
        next_steps=["- Draft the SOW", "Send pricing"],
        source_evidence=["Transcript — Monday, July 7, 2025", "Email from Parker"],
    )
    base.update(kw)
    return MeetingPrepResponse(**base)


def test_renders_a_valid_pdf():
    pdf = render_meeting_prep_pdf(
        _resp(), prepared_for="pjensen@acme.example", meeting_time="Wed 10:00 AM"
    )
    assert isinstance(pdf, bytes)
    assert pdf[:5] == b"%PDF-"  # magic bytes
    assert len(pdf) > 1000


def test_unicode_and_markdown_do_not_crash():
    # Smart quotes, arrows, bullets, headers — the model emits all of these.
    messy = _resp(
        title="Deal “Review” → Q3",
        executive_summary="## Heading\n- point one\n- point two\n**bold** and — dash",
        client_profile="Café notes: naïve façade — 100% ✓",
    )
    pdf = render_meeting_prep_pdf(messy, prepared_for="bellison@acme.example")
    assert pdf[:5] == b"%PDF-"


def test_internal_relabeling_path_renders():
    r = _resp(is_internal=True, client_profile="History with this team.")
    pdf = render_meeting_prep_pdf(r, prepared_for="bellison@acme.example")
    assert pdf[:5] == b"%PDF-"


def test_empty_sources_still_renders():
    pdf = render_meeting_prep_pdf(
        _resp(source_evidence=[]), prepared_for="bellison@acme.example"
    )
    assert pdf[:5] == b"%PDF-"


def test_stakeholder_map_json_is_not_rendered_raw():
    # Regression: stakeholder_map is a JSON-encoded array, not prose. It used to
    # run through the plain-text prose path and dump raw JSON into the PDF.
    sm = json.dumps(
        [
            {
                "name": "Marlo Ingram",
                "email": "mingram@acme.example",
                "role": "Operations/Rollout Lead",
                "context": "Managing UAT sessions and collecting feedback.",
            },
            {
                "name": "Indigo Foster",
                "email": "ifoster@acme.example",
                "role": "Strategy/Decision-Maker",
                "context": "Drives strategic initiatives and budget decisions.",
            },
        ]
    )
    pdf = render_meeting_prep_pdf(
        _resp(is_internal=True, stakeholder_map=sm),
        prepared_for="bellison@acme.example",
    )
    assert pdf[:5] == b"%PDF-"
    # The verbatim JSON structure must not appear in the PDF stream.
    assert b'"role":' not in pdf
    assert b'{"name"' not in pdf


def test_stakeholders_helper_falls_back_on_non_json():
    pdf = _PrepPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    # Not valid JSON → helper reports it did not render structured entries.
    assert _stakeholders(pdf, "just a plain sentence") is False
    # Valid JSON array of stakeholders → renders and reports True.
    ok = _stakeholders(pdf, json.dumps([{"name": "A", "role": "R", "context": "C"}]))
    assert ok is True


def test_masthead_title_starts_below_the_band():
    # Regression: the title used to start at y~19, putting INK text on the
    # BRAND band. The title (one 8mm line) must sit entirely below the band.
    pdf = _PrepPDF(orientation="P", unit="mm", format="A4")
    pdf._doc_title = "t"
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    _masthead(pdf, "DEEP PREP", "EOS Tool by Systems Team")
    assert pdf.get_y() - 8 >= _BAND_H  # cursor is one title line past its top
