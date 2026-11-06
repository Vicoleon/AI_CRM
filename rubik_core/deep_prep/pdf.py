"""Render a Meeting-Prep brief into a branded Cubie PDF (fpdf2, pure-Python).

No system deps (fpdf2 is pure-Python) so it runs on Cloud Run unchanged. Uses
the built-in Helvetica core font, so all text is sanitized to latin-1 first
(smart quotes / em dashes / bullets → ASCII) — that avoids bundling a Unicode
TTF while never raising on the curly punctuation LLMs love.

Takes the same ``MeetingPrepResponse`` the on-screen brief produces, so the
emailed PDF and the in-app view stay in lockstep. When the council lands and
produces richer section data, add an overload here — the delivery/job layers
don't change.

The masthead and the footer carry the workspace's company name
(``company_name_for_documents``: the company profile's name, else the product's
name). A caller may pass ``company_name``; otherwise the renderer reads the
saved profile, which never raises.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from rubik_core import brand
from rubik_core.company.profile import company_name_for_documents
from rubik_core.deep_prep.models import DeepPrepResult

if TYPE_CHECKING:
    from backend.routers.home import MeetingPrepResponse

# Cubie's colours, from ``rubik_core.brand``: violet for the masthead band and
# headings, ink for body text. MUTED and RULE are neutral greys, not brand.
BRAND = brand.rgb(brand.BRAND_HEX)
INK = brand.rgb(brand.INK_HEX)
MUTED = (109, 100, 103)
RULE = (227, 220, 218)

# Masthead band height (mm). The title must start below this line — INK text on
# the BRAND band is unreadable.
_BAND_H = 24
_TITLE_TOP = _BAND_H + 6

_PUNCT = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "•": "-",  # bullet
    "…": "...",
    " ": " ",  # nbsp
    "‑": "-",  # non-breaking hyphen
    "→": "->",
}


def _latin1(text: str) -> str:
    """Make text safe for the latin-1 core font; never raise on stray glyphs."""
    if not text:
        return ""
    for bad, good in _PUNCT.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def _strip_md(line: str) -> str:
    """Flatten the light markdown the model emits into plain text for a cell."""
    s = line.strip()
    while s.startswith("#"):
        s = s[1:].strip()
    s = s.replace("**", "").replace("__", "")
    if s.startswith(("- ", "* ")):
        s = s[2:]
    return s


# Field render order + labels. Internal meetings relabel a few, matching the
# frontend (MeetingPrep.tsx getLabel) so the PDF reads the same as the app.
_PROSE_FIELDS = [
    ("meeting_objective", "Meeting objective", "Meeting objective"),
    ("client_profile", "Client profile", "Relationship context"),
    ("recent_interactions", "Recent interactions", "Recent interactions"),
    ("past_meeting_context", "Past meeting context", "Past meeting context"),
    ("product_fit_analysis", "Product fit analysis", "Project / status context"),
    ("recommended_positioning", "Recommended positioning", "Communication style"),
    ("likely_objections", "Likely objections", "Potential friction"),
]
# stakeholder_map is rendered separately via _stakeholders() — it is a
# JSON-encoded array, not prose, so it can't go through the _body() path.
_STAKEHOLDER_LABELS = ("Stakeholders", "Roles & dynamics")
_LIST_FIELDS = [
    ("discovery_questions", "Discovery questions", "Key questions"),
    ("suggested_agenda", "Suggested agenda", "Suggested agenda"),
    ("talking_points", "Talking points", "Talking points"),
    # Internal/1:1 briefs only in practice — the renderers skip empty lists, so
    # an external brief that never populates it simply omits the heading.
    ("open_items_you_owe", "Open items you owe", "What you owe them"),
    ("next_steps", "Next steps", "Next steps"),
]


class _PrepPDF(FPDF):
    #: Whose document this is: the masthead kicker and the footer name it.
    _company = brand.PRODUCT_NAME

    def header(self):
        if self.page_no() == 1:
            return
        self.set_y(8)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 5, _latin1(self._doc_title), align="L")
        self.ln(8)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(
            0,
            4,
            _latin1(
                f"{self._company} - Confidential. AI-generated; verify before use."
            ),
            align="L",
            new_x=XPos.LMARGIN,
            new_y=YPos.TOP,
        )
        self.cell(0, 4, f"{self.page_no()}", align="R")


def _para(pdf: _PrepPDF, text: str, h: float = 5) -> None:
    """multi_cell that always returns the cursor to the left margin + next line.

    This fpdf2 leaves x at the right margin by default, so a naive second
    multi_cell starts with negative width and raises; pinning new_x/new_y avoids
    that entirely.
    """
    pdf.multi_cell(0, h, _latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _heading(pdf: _PrepPDF, text: str) -> None:
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*BRAND)
    _para(pdf, text, h=6)
    pdf.set_draw_color(*RULE)
    y = pdf.get_y() + 1
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(2)


def _body(pdf: _PrepPDF, text: str) -> None:
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*INK)
    for raw in (text or "").splitlines():
        line = _strip_md(raw)
        if not line:
            pdf.ln(2)
            continue
        _para(pdf, line)


def _bullets(pdf: _PrepPDF, items: list[str]) -> None:
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*INK)
    for item in items:
        clean = _strip_md(str(item))
        if clean:
            _para(pdf, f"-  {clean}")


def _stakeholders(pdf: _PrepPDF, raw: str) -> bool:
    """Render the JSON-encoded stakeholder_map as readable entries.

    Returns True if it rendered structured stakeholders. Falls back to False
    (so the caller can print the raw string as prose) if the value isn't the
    expected JSON array of {name, email, role, context} objects.
    """
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
            header = f"{header} - {role}"

        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*INK)
        _para(pdf, header)

        if email and email != name:
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(*MUTED)
            _para(pdf, email)

        if context:
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*INK)
            _para(pdf, context)

        pdf.ln(2)
        rendered = True

    return rendered


def _styled_blocks(pdf: _PrepPDF, blocks: list[tuple[str, str]]) -> None:
    """Render ``people.export``-style lines (person, label, muted, bullet, body)."""
    for style, text in blocks:
        if style == "person":
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 10.5)
            pdf.set_text_color(*INK)
            _para(pdf, text)
        elif style == "label":
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*MUTED)
            _para(pdf, text)
        elif style == "muted":
            pdf.set_font("Helvetica", "I", 8.5)
            pdf.set_text_color(*MUTED)
            _para(pdf, text, h=4)
        elif style == "bullet":
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*INK)
            _para(pdf, f"-  {_strip_md(text)}")
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*INK)
            _para(pdf, _strip_md(text))


def _attendee_intel(pdf: _PrepPDF, raw: Any) -> None:
    """UC-47 Attendee Intelligence. No-op when nothing was researched."""
    from rubik_core.people.export import attendee_intel_blocks

    blocks = attendee_intel_blocks(raw if isinstance(raw, list) else None)
    if not blocks:
        return

    _heading(pdf, "Attendee intelligence")
    _styled_blocks(pdf, blocks)


def _masthead(pdf: _PrepPDF, kind: str, title: str) -> None:
    pdf.set_fill_color(*BRAND)
    pdf.rect(0, 0, pdf.w, _BAND_H, style="F")
    pdf.set_xy(pdf.l_margin, 7)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 5, _latin1(f"{pdf._company.upper()}  -  {kind}"))
    pdf.set_xy(pdf.l_margin, _TITLE_TOP)
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*INK)
    _para(pdf, title, h=8)


def _new_pdf(title: str, company_name: str | None) -> _PrepPDF:
    """A blank A4 brief carrying ``title`` and whose company it is."""
    pdf = _PrepPDF(orientation="P", unit="mm", format="A4")
    pdf._doc_title = title
    pdf._company = (company_name or "").strip() or company_name_for_documents()
    return pdf


def render_meeting_prep_pdf(
    resp: MeetingPrepResponse,
    *,
    prepared_for: str,
    meeting_time: str = "",
    company_name: str | None = None,
) -> bytes:
    """Return PDF bytes for a MeetingPrepResponse. Never hits the network."""
    internal = bool(getattr(resp, "is_internal", False))
    title = resp.title or "Meeting Prep"

    pdf = _new_pdf(title, company_name)
    pdf.set_title(title)  # /Title metadata → PDF viewers suggest this on Save-As
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()

    kind = (
        "SALES BATTLE BRIEF"
        if getattr(resp, "is_sales_user", False) and not internal
        else "MEETING PREP"
    )
    _masthead(pdf, kind, title)

    meta = f"Prepared for {prepared_for}"
    if meeting_time:
        meta += f"   |   {meeting_time}"
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    _para(pdf, meta)
    pdf.ln(2)

    # Executive summary — the one thing they read.
    if (resp.executive_summary or "").strip():
        _heading(pdf, "Executive summary")
        _body(pdf, resp.executive_summary)

    for key, ext_label, int_label in _PROSE_FIELDS:
        val = (getattr(resp, key, "") or "").strip()
        if val:
            _heading(pdf, int_label if internal else ext_label)
            _body(pdf, val)

    sm = (getattr(resp, "stakeholder_map", "") or "").strip()
    if sm and sm not in ("[]", "{}"):
        ext_label, int_label = _STAKEHOLDER_LABELS
        _heading(pdf, int_label if internal else ext_label)
        if not _stakeholders(pdf, sm):
            # Legacy / non-JSON value — fall back to plain prose.
            _body(pdf, sm)

    # UC-47 — the researched external attendees. The PDF is what gets read in the
    # car on the way to the meeting, so the research has to travel with it.
    _attendee_intel(pdf, getattr(resp, "attendee_intel", None))

    for key, ext_label, int_label in _LIST_FIELDS:
        items = [i for i in (getattr(resp, key, []) or []) if str(i).strip()]
        if items:
            _heading(pdf, int_label if internal else ext_label)
            _bullets(pdf, items)

    sources = [s for s in (resp.source_evidence or []) if str(s).strip()]
    if sources:
        _heading(pdf, "Sources")
        _bullets(pdf, sources)
    else:
        _heading(pdf, "Sources")
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*MUTED)
        _para(pdf, "No specific sources were cited for this brief.")

    out = pdf.output()  # fpdf2 >=2.8 returns a bytearray
    return bytes(out)


def render_deep_prep_pdf(
    result: DeepPrepResult,
    *,
    prepared_for: str,
    meeting_time: str = "",
    company_name: str | None = None,
) -> bytes:
    """Render a council-produced DeepPrepResult (variable sections)."""
    title = result.title or "Deep Meeting Prep"
    pdf = _new_pdf(title, company_name)
    pdf.set_title(title)  # /Title metadata → PDF viewers suggest this on Save-As
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()

    kind = (
        "SALES DEEP PREP"
        if result.is_sales_user and not result.is_internal
        else "DEEP PREP"
    )
    _masthead(pdf, kind, title)

    meta = f"Prepared for {prepared_for}"
    if meeting_time:
        meta += f"   |   {meeting_time}"
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    _para(pdf, meta)
    pdf.ln(1)

    if (result.headline or "").strip():
        _heading(pdf, "In brief")
        _body(pdf, result.headline)

    for sec in result.sections:
        _heading(pdf, sec.heading)
        _body(pdf, sec.body)
        srcs = [s for s in (sec.sources or []) if str(s).strip()]
        if srcs:
            pdf.ln(1)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*MUTED)
            _para(pdf, "Sources: " + "; ".join(srcs))

    if result.data_gaps:
        _heading(pdf, "What we don't know")
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*MUTED)
        _para(pdf, "Called out honestly rather than guessed:")
        _bullets(pdf, result.data_gaps)

    if result.open_questions:
        _heading(pdf, "Open questions")
        _bullets(pdf, result.open_questions)

    if result.all_sources:
        _heading(pdf, "All sources")
        _bullets(pdf, result.all_sources)

    return bytes(pdf.output())
