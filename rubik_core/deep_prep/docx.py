"""Render Meeting-Prep briefs into an editable, Cubie-branded Word document.

Why Word next to PDF: the PDF is the polished read-only deliverable; the .docx
is the *editable* twin a user tweaks before forwarding to a teammate ahead of a
client call. Opens in both Microsoft Word and Google Docs (File → Open), so one
format covers both editors without a Drive API integration.

Mirrors ``deep_prep/pdf.py`` deliberately: same field order/labels (imported
from there so the two can never drift), same masthead/footer language. Rendering
is intentionally plain — styled headings and paragraphs, no drawn shapes — since
the whole point of this artifact is easy editing, not visual fidelity.

python-docx is pure-Python (lxml only), Cloud-Run-safe. Never hits the network;
the kicker and footer name the workspace's company the way the PDF does
(``company_name``, else the saved profile, else the product's name). Unlike the
PDF path there is no latin-1 constraint — docx is Unicode — so text passes
through unsanitized; only the light-markdown flattening is shared.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from rubik_core.brand import BRAND_HEX, INK_HEX, rgb
from rubik_core.company.profile import company_name_for_documents

# Same field order + internal/external relabeling as the PDF — import, don't copy.
from rubik_core.deep_prep.pdf import (
    _LIST_FIELDS,
    _PROSE_FIELDS,
    _STAKEHOLDER_LABELS,
    _strip_md,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Cubie violet for the kicker and headings, ink for body text (rubik_core.brand).
BRAND = RGBColor(*rgb(BRAND_HEX))
INK = RGBColor(*rgb(INK_HEX))
MUTED = RGBColor(0x6D, 0x64, 0x67)


def _docx_bytes(doc: Document) -> bytes:
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _new_doc(
    kind: str, title: str, meta: str, company_name: str | None = None
) -> Document:
    """A fresh document with the shared masthead + confidential footer."""
    company = (company_name or "").strip() or company_name_for_documents()
    doc = Document()
    doc.core_properties.title = title  # what Word/Docs suggest on Save-As

    normal = doc.styles["Normal"]
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = INK

    # Masthead: brand line, big title, muted meta — text-only twin of the PDF band.
    kicker = doc.add_paragraph()
    run = kicker.add_run(f"{company.upper()}  —  {kind}")
    run.font.bold = True
    run.font.size = Pt(9)
    run.font.color.rgb = BRAND
    kicker.paragraph_format.space_after = Pt(2)

    head = doc.add_paragraph()
    run = head.add_run(title)
    run.font.bold = True
    run.font.size = Pt(20)
    run.font.color.rgb = INK
    head.paragraph_format.space_after = Pt(2)

    if meta:
        m = doc.add_paragraph()
        run = m.add_run(meta)
        run.font.size = Pt(9)
        run.font.color.rgb = MUTED

    footer_p = doc.sections[0].footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = footer_p.add_run(
        f"{company} — Confidential. AI-generated; verify before use."
    )
    run.font.size = Pt(7.5)
    run.font.color.rgb = MUTED
    return doc


def _heading(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(3)
    run = p.add_run(text)
    run.font.bold = True
    run.font.size = Pt(13)
    run.font.color.rgb = BRAND


def _body(doc: Document, text: str) -> None:
    for raw in (text or "").splitlines():
        line = _strip_md(raw)
        if not line:
            continue
        doc.add_paragraph(line)


def _bullets(doc: Document, items: list[Any]) -> None:
    for item in items:
        clean = _strip_md(str(item))
        if clean:
            doc.add_paragraph(clean, style="List Bullet")


def _muted_para(doc: Document, text: str, *, italic: bool = True) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.size = Pt(9)
    run.font.italic = italic
    run.font.color.rgb = MUTED


def _stakeholders(doc: Document, raw: str) -> bool:
    """Structured stakeholder entries; False → caller falls back to prose."""
    try:
        people = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(people, list):
        return False

    rendered = False
    for person in people:
        if not isinstance(person, dict):
            continue
        name = str(person.get("name") or "").strip()
        role = str(person.get("role") or "").strip()
        email = str(person.get("email") or "").strip()
        context = str(person.get("context") or "").strip()

        header = name or email or "Stakeholder"
        if role:
            header = f"{header} — {role}"
        p = doc.add_paragraph()
        p.add_run(header).font.bold = True
        if email and email != name:
            _muted_para(doc, email)
        if context:
            doc.add_paragraph(context)
        rendered = True
    return rendered


def _attendee_intel(doc: Document, raw: Any) -> None:
    """UC-47 Attendee Intelligence. No-op when nothing was researched."""
    from rubik_core.people.export import attendee_intel_blocks

    blocks = attendee_intel_blocks(raw if isinstance(raw, list) else None)
    if not blocks:
        return

    _heading(doc, "Attendee intelligence")
    _styled_blocks(doc, blocks)


def _styled_blocks(doc: Document, blocks: list[tuple[str, str]]) -> None:
    """Render ``people.export``-style lines (person, label, muted, bullet, body)."""
    for style, text in blocks:
        if style == "person":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            run = p.add_run(text)
            run.font.bold = True
            run.font.size = Pt(11)
        elif style == "label":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            run = p.add_run(text)
            run.font.bold = True
            run.font.size = Pt(9.5)
            run.font.color.rgb = MUTED
        elif style == "muted":
            _muted_para(doc, text, italic=False)
        elif style == "bullet":
            doc.add_paragraph(_strip_md(text), style="List Bullet")
        else:
            doc.add_paragraph(_strip_md(text))


def render_meeting_prep_docx(
    resp: dict[str, Any],
    *,
    prepared_for: str,
    meeting_time: str = "",
    company_name: str | None = None,
) -> bytes:
    """Word twin of ``render_meeting_prep_pdf`` — takes the persisted run's
    ``response`` dict (a ``MeetingPrepResponse`` dump), not the model, so both
    the live request path and the runs-table path share one signature."""
    internal = bool(resp.get("is_internal"))
    title = resp.get("title") or "Meeting Prep"
    kind = (
        "SALES BATTLE BRIEF"
        if resp.get("is_sales_user") and not internal
        else "MEETING PREP"
    )
    meta = f"Prepared for {prepared_for}"
    if meeting_time:
        meta += f"   |   {meeting_time}"
    doc = _new_doc(kind, title, meta, company_name)

    if (resp.get("executive_summary") or "").strip():
        _heading(doc, "Executive summary")
        _body(doc, resp["executive_summary"])

    for key, ext_label, int_label in _PROSE_FIELDS:
        val = (resp.get(key) or "").strip()
        if val:
            _heading(doc, int_label if internal else ext_label)
            _body(doc, val)

    sm = (resp.get("stakeholder_map") or "").strip()
    if sm and sm not in ("[]", "{}"):
        ext_label, int_label = _STAKEHOLDER_LABELS
        _heading(doc, int_label if internal else ext_label)
        if not _stakeholders(doc, sm):
            _body(doc, sm)

    # UC-47 — the researched external attendees. The exported doc is what a rep
    # forwards or reads in the car, so the research has to travel with it.
    _attendee_intel(doc, resp.get("attendee_intel"))

    for key, ext_label, int_label in _LIST_FIELDS:
        items = [i for i in (resp.get(key) or []) if str(i).strip()]
        if items:
            _heading(doc, int_label if internal else ext_label)
            _bullets(doc, items)

    sources = [s for s in (resp.get("source_evidence") or []) if str(s).strip()]
    _heading(doc, "Sources")
    if sources:
        _bullets(doc, sources)
    else:
        _muted_para(doc, "No specific sources were cited for this brief.")

    return _docx_bytes(doc)


def render_deep_prep_docx(
    result: dict[str, Any],
    *,
    prepared_for: str,
    meeting_time: str = "",
    company_name: str | None = None,
) -> bytes:
    """Word twin of ``render_deep_prep_pdf`` — takes a ``DeepPrepResult`` dict
    (the ``response`` payload persisted by council-mode runs)."""
    title = result.get("title") or "Deep Meeting Prep"
    kind = (
        "SALES DEEP PREP"
        if result.get("is_sales_user") and not result.get("is_internal")
        else "DEEP PREP"
    )
    meta = f"Prepared for {prepared_for}"
    if meeting_time:
        meta += f"   |   {meeting_time}"
    doc = _new_doc(kind, title, meta, company_name)

    if (result.get("headline") or "").strip():
        _heading(doc, "In brief")
        _body(doc, result["headline"])

    for sec in result.get("sections") or []:
        sec = sec if isinstance(sec, dict) else {}
        _heading(doc, str(sec.get("heading") or "Section"))
        _body(doc, str(sec.get("body") or ""))
        srcs = [s for s in (sec.get("sources") or []) if str(s).strip()]
        if srcs:
            _muted_para(doc, "Sources: " + "; ".join(str(s) for s in srcs))

    if result.get("data_gaps"):
        _heading(doc, "What we don't know")
        _muted_para(doc, "Called out honestly rather than guessed:")
        _bullets(doc, result["data_gaps"])

    if result.get("open_questions"):
        _heading(doc, "Open questions")
        _bullets(doc, result["open_questions"])

    if result.get("all_sources"):
        _heading(doc, "All sources")
        _bullets(doc, result["all_sources"])

    return _docx_bytes(doc)
