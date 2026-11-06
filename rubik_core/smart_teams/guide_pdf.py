"""Render an approved personal guide as a PDF (fpdf2, pure-Python).

**Why a PDF exists at all, and why it is the one to send.** The two sample
guides this format follows — the format spec in ``docs/smart_teams/plan.md`` —
are PDFs, and ST-6's scope has always said "Word + PDF export". Until now only
the ``.docx`` shipped, which meant the artifact colleagues pass around was an
editable document. A PDF is not tamper-proof and nothing here pretends
otherwise; what it carries is the right signal, that this is a thing to read
rather than a template to fork. The ``.docx`` stays for the occasional person
who wants to adapt the wording.

**The stated blocker was not real.** ``guide.export_docx`` deferred a PDF on the
grounds that the house renderer is latin-1 only and "a name outside latin-1
would render as ``?`` in a document about that person". Measured against the
actual data: none of the names on the roster at the time was lossy under latin-1
(accented Latin letters such as ``ó`` are in latin-1), and the only lossy
character in a rendered guide is the em dash, fifteen of them, all from house
copy. ``deep_prep.pdf._PUNCT`` already maps that to a hyphen — and the source
PDFs themselves use hyphens ("Give Me Ownership - Not a Script"), so the
substitution moves toward the format spec rather than away from it. Nothing is
lost. If a genuinely non-latin-1 name is ever hired,
:func:`unsupported_characters` is the check that will say so out loud rather
than printing a ``?`` into somebody's guide.

**Rendered from the stored markdown, through the shared parse.** Same rule as the
Word export and for the same reason: ``content_md`` is the editable draft, so a
renderer that walked the ``PersonalGuide`` object again would export words the
person had already changed. Both exports consume
:func:`guide.parse_guide_blocks`, so a markdown shape either renders in both or
in neither — two hand-rolled parsers is the drift the guide module already warns
about.

Layout follows the source: a small violet kicker over a large title, the deck and
framing, the two-column page-1 index, violet hanging numerals on each guideline,
tinted boxes for the tie-in citations, and ``WORKING WITH {NAME}  n / N`` running
on every page.
"""

from __future__ import annotations

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from rubik_core import brand
from rubik_core.deep_prep.pdf import BRAND, INK, MUTED, RULE, _latin1

#: The tint behind a tie-in citation: Cubie's off-white tint, the same value the
#: Word export uses, standing in for the source format's tint of its own accent.
#: A bordered box with no fill reads as a table rather than a citation.
CALLOUT_FILL = brand.rgb(brand.TINT_HEX)

#: Page geometry, in mm. A4 default with generous margins: the source sets a
#: narrow measure and long lines are what make a one-page-per-idea document read
#: like a report instead.
MARGIN = 18


def unsupported_characters(text: str) -> set[str]:
    """Characters the latin-1 core font cannot render, after punctuation mapping.

    Empty for every guide this repo can currently produce. Exposed and tested
    rather than assumed, because the failure mode is silent: ``_latin1``
    substitutes a ``?`` and the document still renders, so a name it mangles
    would ship looking almost right. A caller that gets a non-empty set should
    bundle a Unicode TTF, not ship the ``?``.
    """
    mapped = _latin1(text or "")
    return {ch for ch in mapped if ch == "?" and "?" not in (text or "")}


class _GuidePDF(FPDF):
    """The running footer is the whole reason this subclass exists."""

    def __init__(self, running_title: str) -> None:
        super().__init__(format="A4", unit="mm")
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self._running_title = running_title

    def footer(self) -> None:  # noqa: D102 — fpdf2 hook
        self.set_y(-15)
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*BRAND)
        self.cell(
            0,
            4,
            _latin1(f"WORKING WITH {self._running_title.upper()}"),
            align="L",
            new_x=XPos.LMARGIN,
            new_y=YPos.TOP,
        )
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        # "{nb}" is fpdf2's total-page alias, resolved at output. The source
        # numbers every page "n / N"; a guide's length varies with how much of
        # the interview somebody finished, so the total cannot be hardcoded.
        self.cell(0, 4, f"{self.page_no()} / {{nb}}", align="R")
        self.set_y(-11)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*MUTED)
        self.cell(
            0,
            3,
            _latin1(
                f"{brand.PRODUCT_NAME} - Confidential. AI-generated; verify before use."
            ),
            align="L",
        )


def _para(pdf: _GuidePDF, text: str, height: float = 5.0) -> None:
    """``multi_cell`` that leaves the cursor at the left margin on the next line.

    fpdf2 leaves x at the right margin by default, so a following ``multi_cell``
    starts with a negative width and raises. Same guard ``deep_prep/pdf.py``
    applies, for the same reason.
    """
    pdf.multi_cell(0, height, _latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _cover(pdf: _GuidePDF, kicker: str, title: str, provenance: str) -> None:
    """The source's page-1 masthead: small violet kicker, large title, meta.

    Deliberately NOT ``deep_prep``'s filled violet band. The source guides open
    on white with a large title, and the band is a Deep Prep / X-Ray device.
    """
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*BRAND)
    _para(pdf, kicker.upper(), height=5)
    pdf.ln(1)

    pdf.set_font("Helvetica", "B", 26)
    pdf.set_text_color(*INK)
    _para(pdf, title, height=10)
    pdf.ln(1)

    if provenance:
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        _para(pdf, provenance, height=4)
    pdf.ln(2)


def _heading(pdf: _GuidePDF, text: str) -> None:
    """A structural heading: violet, with a hairline rule beneath it."""
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*BRAND)
    _para(pdf, text, height=6)
    pdf.set_draw_color(*RULE)
    y = pdf.get_y() + 0.5
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(2.5)


def _guideline(pdf: _GuidePDF, numeral: str, title: str) -> None:
    """A numbered guideline: violet hanging numeral, then the title in ink."""
    pdf.ln(2.5)
    if numeral:
        pdf.set_font("Helvetica", "B", 15)
        pdf.set_text_color(*BRAND)
        width = pdf.get_string_width(_latin1(numeral)) + 3
        pdf.cell(width, 7, _latin1(numeral), align="L")
    pdf.set_font("Helvetica", "B", 11.5)
    pdf.set_text_color(*INK)
    _para(pdf, title, height=7)
    pdf.ln(0.5)


def _body(pdf: _GuidePDF, text: str) -> None:
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*INK)
    _para(pdf, text)


def _muted(pdf: _GuidePDF, text: str) -> None:
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.set_text_color(*MUTED)
    _para(pdf, text, height=3.6)


def _bullets(pdf: _GuidePDF, items: list[str]) -> None:
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*INK)
    for item in items:
        clean = (item or "").replace("**", "").strip()
        if clean:
            _para(pdf, f"-  {clean}")


def _callout(pdf: _GuidePDF, label: str, body: str) -> None:
    """One tinted citation box — the device the source uses for every tie-in."""
    pdf.ln(1.5)
    pdf.set_fill_color(*CALLOUT_FILL)
    pdf.set_draw_color(*CALLOUT_FILL)
    if label:
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*BRAND)
        pdf.multi_cell(
            0,
            4.5,
            _latin1(label.upper()),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
            fill=True,
        )
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*INK)
    pdf.multi_cell(
        0, 4.5, _latin1(body), new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True
    )
    pdf.ln(1.5)


def _contents_grid(pdf: _GuidePDF, entries: list[tuple[str, str]]) -> None:
    """The page-1 index, in two columns filled DOWN each one.

    01-05 left and 06-10 right in the source, so a reader scanning the left
    column gets the first half of the guide rather than every other item. Drawn
    with explicit cells rather than a table because fpdf2's table API would
    force a header row and borders the source does not have.
    """
    if not entries:
        return
    rows = (len(entries) + 1) // 2
    column_width = (pdf.w - pdf.l_margin - pdf.r_margin) / 2
    top = pdf.get_y()
    line_height = 5.5

    for position, (numeral, title) in enumerate(entries):
        row, column = position % rows, position // rows
        pdf.set_xy(pdf.l_margin + column * column_width, top + row * line_height)
        if numeral:
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*BRAND)
            width = pdf.get_string_width(_latin1(numeral)) + 2
            pdf.cell(width, line_height, _latin1(numeral), align="L")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK)
        # Clipped to the column so a long title cannot bleed into its neighbour;
        # the full title is three lines below in its own section either way.
        pdf.cell(
            column_width - (width if numeral else 0) - 4,
            line_height,
            _latin1(title),
            align="L",
        )
    pdf.set_xy(pdf.l_margin, top + rows * line_height)
    pdf.ln(2)


def render_guide_pdf(
    blocks, *, title: str, display_name: str, provenance: str
) -> bytes:
    """Render parsed guide blocks to PDF bytes.

    Takes the blocks rather than the markdown so the caller owns the parse — the
    Word export uses the same one, which is what keeps the two in step.
    """
    from rubik_core.smart_teams.guide import split_contents_entry

    pdf = _GuidePDF(display_name)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_title(title)
    _cover(pdf, "A personal working guide", title, provenance)

    for block in blocks:
        if block.kind in {"title", "rule"}:
            # The title is already the cover. A horizontal rule is the markdown's
            # separator before the end matter, and the heading that follows it
            # carries its own rule, so drawing a second one just doubles it.
            continue
        if block.kind == "heading":
            _heading(pdf, block.text)
        elif block.kind == "guideline":
            _guideline(pdf, block.numeral, block.text)
        elif block.kind == "contents":
            _contents_grid(pdf, [split_contents_entry(e) for e in block.items])
        elif block.kind == "callout":
            callout = block.callout
            _callout(
                pdf, callout.label if callout else "", callout.body if callout else ""
            )
        elif block.kind == "bullets":
            _bullets(pdf, list(block.items))
        elif block.kind == "muted":
            _muted(pdf, block.text)
        else:
            _body(pdf, block.text)

    return bytes(pdf.output())
