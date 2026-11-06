"""Render a proposal version as a PDF (fpdf2) or an editable Word file.

Layout: a masthead band with the company's name, "SERVICES PROPOSAL" over the
service title, PREPARED FOR / PREPARED BY side by side, then EXECUTIVE
SUMMARY, SCOPE OF WORK, DESIRED OUTCOMES & KEY DELIVERABLES, the chosen
template's write-up, PRICING & INVESTMENT with a standard-rate /
proposal-price table and a SAVE badge, notes, a closing line, and the
confidential footer with "Page n of N". Type is set small enough that a
typical discovery-call proposal fits one Letter page; a long one runs to two.

THE PRICING TABLE IS RENDERED FROM THE BRIEF, NOT THE PROSE. That is the last
link of the fence in ``brief.py``: whatever the model wrote, the only numbers a
prospect sees are the ones on the confirmed brief.

THE LETTERHEAD IS THE WORKSPACE'S. Name, legal name, address and website come
from the company profile through ``brand.letterhead``; with no profile saved
the masthead is a plain band and the footer reads "Confidential Proposal".
The caller passes the letterhead and the template in, so these functions stay
pure: no database, no network.

Same constraints as the house renderers (``deep_prep/pdf.py``,
``smart_teams/guide_pdf.py``): fpdf2's core Helvetica is latin-1, so every
string goes through ``_latin1`` (curly quotes and dashes become ASCII; nothing
raises). python-docx is Unicode and takes text as is. Both are pure-Python and
Cloud-Run-safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor
from fpdf import FPDF
from fpdf.enums import TableBordersLayout, XPos, YPos
from fpdf.fonts import FontFace

from rubik_core.deep_prep.pdf import INK, MUTED, RULE, _latin1
from rubik_core.proposals.brand import (
    BADGE_RGB,
    HEADING_RGB,
    TINT_RGB,
    Letterhead,
)
from rubik_core.proposals.brief import savings_label
from rubik_core.proposals.templates import Block, Template, printed_blocks

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_MARGIN = 16.0  # mm
_BAND_H = 15.0  # mm — the masthead band; content starts below it


@dataclass(frozen=True)
class _Theme:
    heading: tuple[int, int, int] = HEADING_RGB
    ink: tuple[int, int, int] = INK
    muted: tuple[int, int, int] = MUTED
    rule: tuple[int, int, int] = RULE
    tint: tuple[int, int, int] = TINT_RGB
    badge: tuple[int, int, int] = BADGE_RGB


_PLAIN = _Theme()


def _th(pdf: FPDF) -> _Theme:
    return getattr(pdf, "theme", _PLAIN)


def filename(brief: dict, version_no: int, ext: str) -> str:
    company = (brief.get("prepared_for") or {}).get("company") or "Prospect"
    slug = re.sub(r"[^A-Za-z0-9]+", "", company)[:32] or "Prospect"
    return f"{slug}_Proposal_v{version_no}.{ext}"


def _shows_standard_rate(lines: list[dict]) -> bool:
    """A standard-rate column only earns its place when some line's standard
    rate differs from what is being proposed (a list price the AE accepted
    as is would otherwise print twice)."""
    return any(
        (line.get("standard_rate") or "").strip()
        and (line.get("standard_rate") or "").strip()
        != (line.get("proposal_price") or "").strip()
        for line in lines
    )


def _clean(text: str | None) -> str:
    """Strip the light markdown a model emits; the renderer sets its own type."""
    s = (text or "").replace("**", "").replace("__", "")
    return s.strip()


# ── PDF ──────────────────────────────────────────────────────────────────────


class _ProposalPDF(FPDF):
    def __init__(self, *, ref_code: str, headline: str, letterhead: Letterhead):
        super().__init__(orientation="P", unit="mm", format="Letter")
        self._ref_code = ref_code
        self._headline = headline
        self._letterhead = letterhead
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages()

    def header(self):
        if self.page_no() == 1:
            # The masthead band. Drawn here so the auto page-break never puts a
            # continuation page under a second band.
            self.set_fill_color(*HEADING_RGB)
            self.rect(0, 0, self.w, _BAND_H, style="F")
            name = self._letterhead.display_name
            if name:
                self.set_xy(_MARGIN, 4.2)
                self.set_text_color(255, 255, 255)
                self.set_font("Helvetica", "B", 11)
                self.cell(0, 6, _latin1(name.upper()))
            self.set_y(_BAND_H + 6)
            return
        self.set_y(8)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, _latin1(f"{self._headline}  |  {self._ref_code}"), align="L")
        self.ln(8)

    def footer(self):
        self.set_y(-13)
        self.set_draw_color(*RULE)
        self.line(
            self.l_margin,
            self.get_y() - 1.5,
            self.w - self.r_margin,
            self.get_y() - 1.5,
        )
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(
            0,
            4,
            _latin1(self._letterhead.footer),
            align="L",
            new_x=XPos.LMARGIN,
            new_y=YPos.TOP,
        )
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")


def _bullet(pdf: FPDF, lead: str, text: str) -> None:
    """A round bullet, then "**lead** text" wrapped under itself."""
    indent = 5.0
    y = pdf.get_y()
    pdf.set_fill_color(*_th(pdf).ink)
    pdf.circle(x=pdf.l_margin + 1.8, y=y + 2.1, radius=0.6, style="F")
    # fpdf2 tables fill cells with the CURRENT fill color; never leave it dark.
    pdf.set_fill_color(255, 255, 255)
    pdf.set_x(pdf.l_margin + indent)
    lead = _latin1(lead).replace("*", "")
    text = _latin1(text).replace("*", "")
    pdf.multi_cell(
        pdf.epw - indent,
        4.3,
        f"**{lead}** {text}" if lead else text,
        markdown=True,
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.ln(0.7)


def _template_blocks(pdf: FPDF, blocks: list[Block]) -> None:
    """The template's write-up, in the company's words, after the outcomes."""
    for block in blocks:
        if block.kind == "heading":
            _section(pdf, block.text)
        elif block.kind == "bullet":
            pdf.set_font("Helvetica", "", 8.7)
            pdf.set_text_color(*_th(pdf).ink)
            _bullet(pdf, "", block.text)
        else:
            _body(pdf, block.text, size=8.7)


def _para(pdf: FPDF, text: str, h: float = 4.3, *, markdown: bool = False) -> None:
    pdf.multi_cell(
        0, h, _latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT, markdown=markdown
    )


def _section(pdf: FPDF, title: str) -> None:
    # Never strand a heading at the foot of a page.
    if pdf.get_y() > pdf.page_break_trigger - 22:
        pdf.add_page()
    pdf.ln(2.2)
    pdf.set_font("Helvetica", "B", 8.8)
    pdf.set_text_color(*_th(pdf).heading)
    _para(pdf, title.upper(), h=5)
    pdf.set_draw_color(*_th(pdf).rule)
    y = pdf.get_y() + 0.3
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(1.6)


def _body(pdf: FPDF, text: str, *, size: float = 8.9) -> None:
    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(*_th(pdf).ink)
    for para in [p for p in re.split(r"\n\s*\n", _clean(text)) if p.strip()]:
        _para(pdf, " ".join(para.split()))
        pdf.ln(1.2)


def _label(pdf: FPDF, text: str) -> None:
    pdf.set_font("Helvetica", "B", 6.8)
    pdf.set_text_color(*_th(pdf).muted)
    _para(pdf, text.upper(), h=3.6)


def _prepared_block(pdf: FPDF, brief: dict, letterhead: Letterhead) -> None:
    """PREPARED FOR | PREPARED BY, side by side, then the date/ref line."""
    pf = brief.get("prepared_for") or {}
    pb = brief.get("prepared_by") or {}
    top = pdf.get_y()
    col_w = pdf.epw / 2
    left_x, right_x = pdf.l_margin, pdf.l_margin + col_w

    def column(
        x: float, label: str, lines: list[tuple[str, str, float, tuple[int, int, int]]]
    ) -> float:
        pdf.set_xy(x, top)
        pdf.set_font("Helvetica", "B", 6.8)
        pdf.set_text_color(*_th(pdf).muted)
        pdf.cell(col_w - 4, 3.6, label, new_x=XPos.LEFT, new_y=YPos.NEXT)
        for text, style, size, color in lines:
            if not text:
                continue
            pdf.set_font("Helvetica", style, size)
            pdf.set_text_color(*color)
            pdf.set_x(x)
            pdf.multi_cell(
                col_w - 4, 4.0, _latin1(text), new_x=XPos.LEFT, new_y=YPos.NEXT
            )
        return pdf.get_y()

    y_left = column(
        left_x,
        "PREPARED FOR",
        [
            (pf.get("name") or "", "B", 9.5, _th(pdf).ink),
            (pf.get("title") or "", "", 8.5, _th(pdf).ink),
            (pf.get("company") or "", "", 8.5, _th(pdf).ink),
        ],
    )
    y_right = column(
        right_x,
        "PREPARED BY",
        [
            (letterhead.display_name, "B", 9.2, _th(pdf).ink),
            (
                " - ".join(p for p in (pb.get("name"), pb.get("title")) if p),
                "",
                8.5,
                _th(pdf).ink,
            ),
            (pb.get("email") or "", "", 8.0, _th(pdf).muted),
            (letterhead.contact_line, "", 7.5, _th(pdf).muted),
        ],
    )
    pdf.set_xy(pdf.l_margin, max(y_left, y_right) + 1.5)
    meta = "  |  ".join(
        p
        for p in (
            f"Date: {brief.get('date') or ''}",
            f"Ref: {brief.get('ref_code') or ''}",
        )
        if p.split(":")[1].strip()
    )
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*_th(pdf).muted)
    _para(pdf, meta, h=4)


def _pricing_table(pdf: FPDF, brief: dict) -> None:
    lines = brief.get("pricing") or []
    if not lines:
        return
    has_std = _shows_standard_rate(lines)
    has_saving = any(savings_label(line) for line in lines)
    headings = ["Service description"]
    widths = [0.0]
    aligns = ["LEFT"]
    if has_std:
        headings.append("Standard rate")
        widths.append(30)
        aligns.append("RIGHT")
    headings.append("Proposal price")
    widths.append(30)
    aligns.append("RIGHT")
    if has_saving:
        headings.append("")
        widths.append(26)
        aligns.append("CENTER")
    total_w = pdf.epw
    widths[0] = total_w - sum(widths[1:])

    pdf.set_font("Helvetica", "", 8.6)
    pdf.set_text_color(*_th(pdf).ink)
    heading_face = FontFace(
        emphasis="BOLD", color=_th(pdf).heading, fill_color=_th(pdf).tint, size_pt=7.2
    )
    with pdf.table(
        col_widths=tuple(widths),
        text_align=tuple(aligns),
        borders_layout=TableBordersLayout.HORIZONTAL_LINES,
        headings_style=heading_face,
        line_height=4.4,
        padding=(1.2, 1.5, 1.2, 1.5),
    ) as table:
        row = table.row()
        for h in headings:
            row.cell(_latin1(h.upper()))
        for line in lines:
            row = table.row()
            row.cell(_latin1(_clean(line.get("description"))))
            if has_std:
                row.cell(_latin1(line.get("standard_rate") or "-"))
            row.cell(
                _latin1(line.get("proposal_price") or "-"),
                style=FontFace(emphasis="BOLD", color=_th(pdf).ink),
            )
            if has_saving:
                badge = savings_label(line)
                row.cell(
                    _latin1(badge or ""),
                    style=FontFace(
                        emphasis="BOLD",
                        color=_th(pdf).heading,
                        fill_color=_th(pdf).badge if badge else None,
                        size_pt=7,
                    ),
                )
    pdf.ln(1.5)


def render_pdf(
    brief: dict,
    content: dict,
    *,
    letterhead: Letterhead | None = None,
    template: Template | None = None,
) -> bytes:
    """The PDF. ``letterhead`` defaults to the empty one; ``template`` is the
    brief's template, already looked up by the caller (``None`` = none)."""
    letterhead = letterhead or Letterhead()
    headline = (
        _clean(content.get("headline"))
        or brief.get("service_title")
        or "Services Proposal"
    )
    pdf = _ProposalPDF(
        ref_code=brief.get("ref_code") or "", headline=headline, letterhead=letterhead
    )
    pdf.add_page()

    # Title block.
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*_th(pdf).heading)
    _para(
        pdf,
        (_clean(content.get("document_title")) or "Services Proposal").upper(),
        h=4.5,
    )
    pdf.set_font("Helvetica", "B", 15.5)
    pdf.set_text_color(*_th(pdf).ink)
    _para(pdf, headline, h=7)
    pdf.ln(2.5)
    _prepared_block(pdf, brief, letterhead)

    if content.get("executive_summary"):
        _section(pdf, "Executive Summary")
        _body(pdf, content["executive_summary"])

    if content.get("scope"):
        _section(pdf, "Scope of Work")
        if content.get("scope_intro"):
            _body(pdf, content["scope_intro"])
        for n, item in enumerate(content["scope"], start=1):
            pdf.set_font("Helvetica", "B", 8.9)
            pdf.set_text_color(*_th(pdf).ink)
            _para(pdf, f"{n}. {_clean(item.get('heading'))}", h=4.4)
            if item.get("body"):
                pdf.set_font("Helvetica", "", 8.7)
                pdf.set_x(pdf.l_margin + 5)
                pdf.multi_cell(
                    0,
                    4.2,
                    _latin1(" ".join(_clean(item["body"]).split())),
                    new_x=XPos.LMARGIN,
                    new_y=YPos.NEXT,
                )
            pdf.ln(1.0)

    if content.get("outcomes"):
        _section(pdf, "Desired Outcomes & Key Deliverables")
        if content.get("outcomes_intro"):
            _body(pdf, content["outcomes_intro"])
        pdf.set_font("Helvetica", "", 8.7)
        pdf.set_text_color(*_th(pdf).ink)
        for item in content["outcomes"]:
            heading = _clean(item.get("heading")).replace("*", "")
            body = " ".join(_clean(item.get("body")).replace("*", "").split())
            _para(
                pdf,
                f"**{heading}:** {body}" if body else f"**{heading}**",
                h=4.2,
                markdown=True,
            )
            pdf.ln(0.9)

    # The template's write-up of the service, in the company's own words.
    if template is not None:
        _template_blocks(pdf, printed_blocks(template))

    if brief.get("pricing") or content.get("pricing_intro"):
        _section(pdf, "Pricing & Investment")
        if content.get("pricing_intro"):
            _body(pdf, content["pricing_intro"])
        _pricing_table(pdf, brief)

    notes = content.get("notes") or brief.get("notes") or []
    if notes:
        pdf.set_font("Helvetica", "B", 8.2)
        pdf.set_text_color(*_th(pdf).ink)
        _para(pdf, "Additional notes", h=4.4)
        pdf.set_font("Helvetica", "", 8.4)
        for note in notes:
            pdf.set_x(pdf.l_margin + 3)
            pdf.multi_cell(
                0,
                4.1,
                _latin1("- " + " ".join(_clean(note).split())),
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
        pdf.ln(1.0)

    if brief.get("timeline"):
        pdf.set_font("Helvetica", "B", 8.2)
        pdf.set_text_color(*_th(pdf).ink)
        _para(pdf, "Timing", h=4.4)
        _body(pdf, brief["timeline"], size=8.4)

    if content.get("closing"):
        pdf.ln(1.5)
        pdf.set_font("Helvetica", "I", 8.6)
        pdf.set_text_color(*_th(pdf).ink)
        _para(pdf, _clean(content["closing"]))

    return bytes(pdf.output())


# ── DOCX ─────────────────────────────────────────────────────────────────────

_D_HEADING = RGBColor(*HEADING_RGB)
_D_INK = RGBColor(0x1D, 0x1A, 0x1B)
_D_MUTED = RGBColor(0x6D, 0x64, 0x67)


def _run(paragraph, text: str, *, bold=False, italic=False, size=10.0, color=_D_INK):
    run = paragraph.add_run(text)
    run.font.bold = bold
    run.font.italic = italic
    run.font.size = Pt(size)
    run.font.color.rgb = color
    return run


def _d_heading(doc, text: str, color: RGBColor = _D_HEADING) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.keep_with_next = True
    _run(p, text.upper(), bold=True, size=9.5, color=color)


def _d_template_blocks(doc, blocks: list[Block], accent: RGBColor) -> None:
    """The template's write-up, in the company's own words."""
    for block in blocks:
        if block.kind == "heading":
            _d_heading(doc, block.text, accent)
        elif block.kind == "bullet":
            q = doc.add_paragraph(style="List Bullet")
            q.paragraph_format.space_after = Pt(2)
            _run(q, _clean(block.text), size=9.2)
        else:
            _d_body(doc, block.text)


def _d_body(doc, text: str, *, size=9.5, italic=False) -> None:
    for para in [p for p in re.split(r"\n\s*\n", _clean(text)) if p.strip()]:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(4)
        _run(p, " ".join(para.split()), size=size, italic=italic)


def render_docx(
    brief: dict,
    content: dict,
    *,
    letterhead: Letterhead | None = None,
    template: Template | None = None,
) -> bytes:
    """The Word file; same inputs as :func:`render_pdf`."""
    letterhead = letterhead or Letterhead()
    headline = (
        _clean(content.get("headline"))
        or brief.get("service_title")
        or "Services Proposal"
    )
    doc = Document()
    doc.core_properties.title = f"{headline} - Proposal"
    section = doc.sections[0]
    section.left_margin = section.right_margin = Inches(0.75)
    section.top_margin = section.bottom_margin = Inches(0.7)
    normal = doc.styles["Normal"]
    normal.font.size = Pt(9.5)
    normal.font.color.rgb = _D_INK

    accent = _D_HEADING
    if letterhead.display_name:
        band = doc.add_paragraph()
        _run(band, letterhead.display_name.upper(), bold=True, size=11, color=accent)
        band.paragraph_format.space_after = Pt(6)

    p = doc.add_paragraph()
    _run(
        p,
        (_clean(content.get("document_title")) or "Services Proposal").upper(),
        bold=True,
        size=8.5,
        color=accent,
    )
    p.paragraph_format.space_after = Pt(0)
    p = doc.add_paragraph()
    _run(p, headline, bold=True, size=16)
    p.paragraph_format.space_after = Pt(6)

    pf = brief.get("prepared_for") or {}
    pb = brief.get("prepared_by") or {}
    table = doc.add_table(rows=1, cols=2)
    left, right = table.rows[0].cells
    lp = left.paragraphs[0]
    _run(lp, "PREPARED FOR\n", bold=True, size=6.8, color=_D_MUTED)
    if pf.get("name"):
        _run(lp, pf["name"] + "\n", bold=True, size=9.5)
    if pf.get("title"):
        _run(lp, pf["title"] + "\n", size=8.5)
    if pf.get("company"):
        _run(lp, pf["company"], size=8.5)
    rp = right.paragraphs[0]
    _run(rp, "PREPARED BY\n", bold=True, size=6.8, color=_D_MUTED)
    if letterhead.display_name:
        _run(rp, letterhead.display_name + "\n", bold=True, size=9.5)
    by_line = " - ".join(x for x in (pb.get("name"), pb.get("title")) if x)
    if by_line:
        _run(rp, by_line + "\n", size=8.5)
    if pb.get("email"):
        _run(rp, pb["email"] + "\n", size=8, color=_D_MUTED)
    if letterhead.contact_line:
        _run(rp, letterhead.contact_line, size=7.5, color=_D_MUTED)

    meta = doc.add_paragraph()
    meta.paragraph_format.space_before = Pt(4)
    bits = [
        f"Date: {brief.get('date')}" if brief.get("date") else "",
        f"Ref: {brief.get('ref_code')}" if brief.get("ref_code") else "",
    ]
    _run(meta, "  |  ".join(b for b in bits if b), size=7.5, color=_D_MUTED)

    if content.get("executive_summary"):
        _d_heading(doc, "Executive Summary", accent)
        _d_body(doc, content["executive_summary"])

    if content.get("scope"):
        _d_heading(doc, "Scope of Work", accent)
        if content.get("scope_intro"):
            _d_body(doc, content["scope_intro"])
        for n, item in enumerate(content["scope"], start=1):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(1)
            _run(p, f"{n}. {_clean(item.get('heading'))}", bold=True, size=9.5)
            if item.get("body"):
                q = doc.add_paragraph()
                q.paragraph_format.left_indent = Inches(0.22)
                q.paragraph_format.space_after = Pt(4)
                _run(q, " ".join(_clean(item["body"]).split()), size=9.2)

    if content.get("outcomes"):
        _d_heading(doc, "Desired Outcomes & Key Deliverables", accent)
        if content.get("outcomes_intro"):
            _d_body(doc, content["outcomes_intro"])
        for item in content["outcomes"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(3)
            _run(p, _clean(item.get("heading")) + ": ", bold=True, size=9.2)
            _run(p, " ".join(_clean(item.get("body")).split()), size=9.2)

    if template is not None:
        _d_template_blocks(doc, printed_blocks(template), accent)

    if brief.get("pricing") or content.get("pricing_intro"):
        _d_heading(doc, "Pricing & Investment", accent)
        if content.get("pricing_intro"):
            _d_body(doc, content["pricing_intro"])
        lines = brief.get("pricing") or []
        if lines:
            has_std = _shows_standard_rate(lines)
            has_saving = any(savings_label(line) for line in lines)
            headers = (
                ["Service description"]
                + (["Standard rate"] if has_std else [])
                + ["Proposal price"]
                + ([""] if has_saving else [])
            )
            t = doc.add_table(rows=1, cols=len(headers))
            t.style = "Table Grid"
            for cell, text in zip(t.rows[0].cells, headers, strict=True):
                _run(
                    cell.paragraphs[0],
                    text.upper(),
                    bold=True,
                    size=7.2,
                    color=accent,
                )
            for line in lines:
                cells = t.add_row().cells
                i = 0
                _run(cells[i].paragraphs[0], _clean(line.get("description")), size=9)
                i += 1
                if has_std:
                    _run(
                        cells[i].paragraphs[0], line.get("standard_rate") or "-", size=9
                    )
                    cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    i += 1
                _run(
                    cells[i].paragraphs[0],
                    line.get("proposal_price") or "-",
                    bold=True,
                    size=9,
                )
                cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
                i += 1
                if has_saving:
                    _run(
                        cells[i].paragraphs[0],
                        savings_label(line) or "",
                        bold=True,
                        size=7.5,
                        color=accent,
                    )
                    cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

    notes = content.get("notes") or brief.get("notes") or []
    if notes:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(1)
        _run(p, "Additional notes", bold=True, size=8.8)
        for note in notes:
            q = doc.add_paragraph(style="List Bullet")
            _run(q, " ".join(_clean(note).split()), size=8.8)

    if brief.get("timeline"):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(4)
        _run(p, "Timing", bold=True, size=8.8)
        _d_body(doc, brief["timeline"], size=8.8)

    if content.get("closing"):
        _d_body(doc, content["closing"], size=9.2, italic=True)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _run(footer, letterhead.footer, size=7, color=_D_MUTED)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()
