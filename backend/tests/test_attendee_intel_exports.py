"""UC-47 — the researched attendees travel with the Word and PDF exports.

The exported doc is what a rep forwards to a colleague or reads on the way to the
meeting, so research that only exists on screen is research they don't have when
it matters. Both renderers walk the same style-tagged blocks
(`people/export.attendee_intel_blocks`) — the tests here cover the flattener's
content decisions once, then assert each renderer actually consumes it.
"""

from __future__ import annotations

from backend.routers.home import MeetingPrepResponse
from rubik_core.deep_prep.docx import render_meeting_prep_docx
from rubik_core.deep_prep.pdf import render_meeting_prep_pdf
from rubik_core.people.export import attendee_intel_blocks
from rubik_core.people.schemas import AttendeeIntel

EXTERNAL = "riley.fontaine@northwind.example"


def _intel(**overrides) -> dict:
    data = {
        "email": EXTERNAL,
        "name": "Riley Fontaine",
        "profile": {
            "name": "Riley Fontaine",
            "linkedin_url": "https://linkedin.example/in/riley-fontaine",
            "headline": "Account Manager, Cloud Infrastructure",
            "current_title": "Account Manager",
            "current_company": "Northwind",
            "tenure": "since 2023",
            "location": "Dallas, TX",
            "career_summary": "Moved from solutions engineering into account management.",
            "credentials": ["GCP Professional Architect"],
        },
        "activity": [
            {
                "kind": "post",
                "posted_at": "2026-07-02",
                "summary": "Argued most GCP overspend is idle non-prod, not compute.",
                "theme": "cloud cost",
                "url": "https://www.linkedin.com/posts/rs-1",
            }
        ],
        "persona": {
            "communication_style": "Short, numbers-first posts.",
            "decision_style": "ROI-first.",
            "priorities": ["cutting idle spend"],
            "posts_about": ["cloud cost", "FinOps"],
            "conversation_openers": ["Ask about the idle non-prod claim."],
            "avoid": ["Framing this as a compute problem."],
            "confidence": "high",
            "basis": "one post plus a profile",
        },
        "sources": ["https://linkedin.example/in/riley-fontaine"],
        "provider": "web_search",
    }
    data.update(overrides)
    return AttendeeIntel.from_dict(data).as_dict()


def _resp(**kw) -> MeetingPrepResponse:
    base = dict(
        title="Acme / Northwind — GCP review",
        executive_summary="A review of GCP spend.",
        is_sales_user=True,
        attendee_intel=[_intel()],
    )
    base.update(kw)
    return MeetingPrepResponse(**base)


# ── the flattener ────────────────────────────────────────────────────────────


def test_blocks_are_empty_without_research():
    """Empty means the renderers omit the heading entirely."""
    assert attendee_intel_blocks(None) == []
    assert attendee_intel_blocks([]) == []


def test_blocks_carry_position_activity_and_the_confidence_band():
    text = " ".join(t for _, t in attendee_intel_blocks([_intel()]))

    assert "Riley Fontaine — Account Manager @ Northwind" in text
    assert "idle non-prod" in text
    assert "https://www.linkedin.com/posts/rs-1" in text
    # The band is spelled out, not left as a raw "high" the reader must decode.
    assert "Well evidenced" in text
    assert "based on one post plus a profile" in text


def test_blocks_say_so_when_nothing_was_found():
    text = " ".join(
        t
        for _, t in attendee_intel_blocks(
            [_intel(profile={}, activity=[], persona={}, error="nothing found")]
        )
    )

    assert "No public professional presence found" in text
    assert "Nothing has been inferred" in text


def test_every_block_style_is_one_the_renderers_handle():
    """A new style added to the flattener without teaching the renderers would
    silently fall through to plain body text in both."""
    styles = {style for style, _ in attendee_intel_blocks([_intel()])}

    assert styles <= {"person", "muted", "label", "body", "bullet"}


# ── the renderers ────────────────────────────────────────────────────────────


def test_pdf_renders_with_attendee_intelligence():
    pdf = render_meeting_prep_pdf(
        _resp(), prepared_for="bellison@acme.example", meeting_time="Thu 10:00 AM"
    )

    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_pdf_without_research_is_still_valid():
    pdf = render_meeting_prep_pdf(
        _resp(attendee_intel=[]), prepared_for="bellison@acme.example"
    )

    assert pdf[:5] == b"%PDF-"


def test_docx_includes_the_researched_attendee():
    blob = render_meeting_prep_docx(
        _resp().model_dump(), prepared_for="bellison@acme.example"
    )

    assert blob[:2] == b"PK"  # docx is a zip

    import zipfile
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(blob)) as z:
        xml = z.read("word/document.xml").decode("utf-8")

    assert "Attendee intelligence" in xml
    assert "Account Manager" in xml
    assert "idle non-prod" in xml
    assert "Well evidenced" in xml


def test_docx_omits_the_section_without_research():
    blob = render_meeting_prep_docx(
        _resp(attendee_intel=[]).model_dump(), prepared_for="bellison@acme.example"
    )

    import zipfile
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(blob)) as z:
        xml = z.read("word/document.xml").decode("utf-8")

    assert "Attendee intelligence" not in xml


def test_the_response_model_rejects_a_malformed_intel_item():
    """The API boundary is where shape is enforced — so the renderers only ever
    see dicts, and a garbage item can't reach a reader's document."""
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        _resp(attendee_intel=[None])


def test_the_flattener_tolerates_pre_feature_and_empty_rows():
    """The docx path renders a persisted run's raw `response` dict, which may
    predate UC-47 or carry rows written by an older shape."""
    for raw in (None, [], [{}], [{"email": ""}]):
        assert attendee_intel_blocks(raw) == []


def test_docx_tolerates_a_non_list_intel_value_on_an_old_run_row():
    """`render_meeting_prep_docx` takes a plain dict off the runs table, so it has
    no pydantic guard in front of it."""
    payload = _resp().model_dump()
    payload["attendee_intel"] = "not a list"

    blob = render_meeting_prep_docx(payload, prepared_for="bellison@acme.example")

    assert blob[:2] == b"PK"
