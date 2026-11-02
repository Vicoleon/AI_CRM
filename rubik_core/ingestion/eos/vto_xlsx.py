"""Excel ⇄ VTO conversion for the Exec uploader.

Builds the fill-in `.xlsx` template execs download, and parses a filled-in
one back into per-section content. Kept out of the router (per the
``rubik_core/`` vs ``backend/`` layering rule) and free of DB/session
imports so the build/parse logic can be unit-tested with just openpyxl.

Template shape — sheet ``VTO`` — ONE ROW PER BULLET/GOAL::

    Category | Section          | Content              | Confidential
    Vision   | Core Values      | Integrity             | No
    Traction | 3-Year Picture   | $10M revenue          | Yes
    Traction | 3-Year Picture   | Expand to new markets | No

Each row is a single bullet, tagged with the Section it belongs to (a
per-row dropdown) and whether it's Confidential (Exec-only) or open
(visible company-wide). A blank Confidential cell means open — the exec
marks the sensitive lines; everything else defaults to visible. Rows are
stored as ``list[{"text": str, "confidential": bool}]`` on
``eos_vto_sections.content`` — see ``Bullet``/``bullets_from_content``/
``bullets_to_content`` below, which are the single source of truth for that
shape so the VTO page, the RAG extractor, and the alignment provider don't
each reinvent it.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from rubik_core.brand import BRAND_HEX, PRODUCT_NAME
from rubik_core.company.profile import company_name_for_documents
from rubik_core.database.eos import VTOSectionType

# xlsx MIME type — used by the download endpoint's Content-Type.
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEMPLATE_FILENAME = f"{PRODUCT_NAME}_VTO_template.xlsx"

_SHEET_NAME = "VTO"
_HEADER = ("Category", "Section", "Content", "Confidential")

# Tolerant vocab for the Confidential cell. Anything outside these two sets
# is a hard parse error (see `_parse_confidential`) rather than a guess —
# it's a security-relevant flag, not a cosmetic label.
_OPEN_TOKENS = {"no", "n", "open", "false", "0"}
_CONFIDENTIAL_TOKENS = {"yes", "y", "confidential", "true", "1"}


class VtoParseError(ValueError):
    """An uploaded workbook isn't a usable VTO template. The router maps
    this to HTTP 400 with the message shown to the exec."""


@dataclass(frozen=True)
class Bullet:
    """One VTO line: its text, and whether it's Exec-only.

    This is the canonical shape of an item in ``eos_vto_sections.content``.
    Everything that reads or writes that JSON column should go through
    ``bullets_from_content``/``bullets_to_content`` rather than assuming
    ``list[str]`` — that was the shape before per-line confidentiality
    existed, and legacy rows in that shape still exist in the DB.
    """

    text: str
    confidential: bool = False


def bullets_from_content(content) -> list[Bullet]:
    """Normalize a ``VTOSection.content`` JSON blob into typed bullets.

    Current shape: ``list[{"text": str, "confidential": bool}]``.
    Legacy shape (bullets published before per-line confidentiality
    existed): ``list[str]`` — normalized to ``confidential=True``. Those
    bullets were published back when the whole VTO was Exec-only, so we
    don't silently expose them company-wide the moment this ships; they
    stay hidden from non-exec until republished through the new template.
    """
    if content is None:
        return []
    if not isinstance(content, list):
        # Stray scalar/dict content predates any list-based shape. Treat it
        # like legacy content: shown, but Exec-only until republished.
        text = str(content).strip()
        return [Bullet(text, True)] if text else []

    out: list[Bullet] = []
    for item in content:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            if text:
                out.append(Bullet(text, bool(item.get("confidential", False))))
        else:
            text = str(item).strip()
            if text:
                out.append(Bullet(text, True))  # legacy plain-string bullet
    return out


def bullets_to_content(bullets: list[Bullet]) -> list[dict]:
    """Inverse of ``bullets_from_content`` — plain, JSON-serializable dicts
    (a frozen ``Bullet`` dataclass can't be stored directly in a JSON
    column)."""
    return [{"text": b.text, "confidential": b.confidential} for b in bullets]


def _parse_confidential(cell_value, *, row_label: str) -> bool:
    """Parse the Confidential cell for one row. Blank means open (the exec
    marks sensitive rows; everything else defaults to visible). Anything
    outside the recognized Yes/No vocabulary is a hard error — guessing on
    a security flag is worse than making the exec fix a typo."""
    if cell_value is None:
        return False
    raw = str(cell_value).strip()
    if raw == "":
        return False
    norm = raw.lower()
    if norm in _OPEN_TOKENS:
        return False
    if norm in _CONFIDENTIAL_TOKENS:
        return True
    raise VtoParseError(
        f'Row "{row_label}": the Confidential column must be "Yes" or "No" '
        f'(got "{raw}"). Fix it and re-upload.'
    )


@dataclass(frozen=True)
class TemplateSection:
    category: str  # "Vision" | "Traction" — informational grouping only
    section_type: str  # VTOSectionType value, stored on eos_vto_sections
    label: str  # human label shown in the sheet + on the VTO page


# The six sections the VTO page renders (frontend VISION_TYPES + TRACTION_TYPES).
# Labels mirror extractor.py::_VTO_LABELS and the frontend labels; a test pins
# them together so they can't drift.
TEMPLATE_SECTIONS: list[TemplateSection] = [
    TemplateSection("Vision", VTOSectionType.VALUES.value, "Core Values"),
    TemplateSection("Vision", VTOSectionType.FOCUS.value, "Core Focus"),
    TemplateSection("Vision", VTOSectionType.TEN_YEAR_TARGET.value, "10-Year Target"),
    TemplateSection(
        "Traction", VTOSectionType.THREE_YEAR_PICTURE.value, "3-Year Picture"
    ),
    TemplateSection("Traction", VTOSectionType.ONE_YEAR_PLAN.value, "1-Year Plan"),
    TemplateSection(
        "Traction", VTOSectionType.QUARTERLY_ROCKS.value, "Quarterly Rocks"
    ),
]

# normalized label -> section_type, for tolerant matching on parse.
_LABEL_TO_TYPE: dict[str, str] = {
    s.label.strip().lower(): s.section_type for s in TEMPLATE_SECTIONS
}


@dataclass(frozen=True)
class ParsedVto:
    """Result of parsing a filled-in template."""

    sections: dict[str, list[Bullet]]  # section_type -> bullets (non-empty only)
    ignored_labels: list[str]  # unrecognized Section labels we skipped


def _content_lines(cell_value) -> list[str]:
    """Split a Content cell into stripped, non-empty lines. Normally one
    line = one row = one bullet (the current template); newline-separated
    lines pasted into a single cell are still tolerated — each inherits
    that row's Confidential value."""
    if cell_value is None:
        return []
    return [line.strip() for line in str(cell_value).splitlines() if line.strip()]


# Data-validation dropdowns are pre-applied down to this row so rows the
# exec inserts later (beyond the pre-filled scaffold) still get them.
_DROPDOWN_MAX_ROW = 300
_SCAFFOLD_ROWS_PER_SECTION = 3


def build_vto_template_workbook(*, company_name: str | None = None) -> bytes:
    """Generate the two-sheet (.xlsx) template execs fill in and upload.

    The heading and the workbook's title name the company
    (``company_name``, else the saved profile, else the product's name).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    company = (company_name or "").strip() or company_name_for_documents()
    heading = f"{company} — Vision/Traction Organizer (VTO) upload template"
    wb = Workbook()
    wb.properties.title = heading

    # -- Instructions sheet (first, so it opens here) ------------------
    info = wb.active
    info.title = "Instructions"
    info_lines = [
        heading,
        "",
        "1. Open the 'VTO' tab. Add ONE ROW PER GOAL/BULLET — if a section "
        "has 3 goals, that's 3 separate rows.",
        "2. For each row, choose its Section from the dropdown in column B, "
        "and type the goal in the Content column.",
        "3. Mark Confidential 'Yes' for sensitive lines only the Exec team "
        "should see. Leave it 'No' (or blank) for lines everyone in your company "
        "should see — open lines show up on the VTO page company-wide.",
        "4. Need more rows than are pre-filled? Insert a row and copy the "
        "dropdowns down. Don't rename the 'VTO' tab or change the Section "
        "labels.",
        "5. Leave a section out entirely to publish it empty.",
        f"6. Save the file, then upload it on the VTO page in {PRODUCT_NAME}.",
        "",
        "Each upload publishes a NEW active version of the VTO and archives the "
        "previous one. Only the Executive team (or admins) can upload.",
    ]
    for i, line in enumerate(info_lines, start=1):
        cell = info.cell(row=i, column=1, value=line)
        if i == 1:
            cell.font = Font(bold=True, size=14)
    info.column_dimensions["A"].width = 95

    # -- VTO sheet (the one they edit) ---------------------------------
    ws = wb.create_sheet(title=_SHEET_NAME)
    header_fill = PatternFill("solid", fgColor=BRAND_HEX.lstrip("#"))  # Cubie violet
    header_font = Font(bold=True, color="FFFFFF")
    for col, name in enumerate(_HEADER, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.fill = header_fill
        cell.font = header_font

    # Scaffold a few blank rows per section (Category+Section pre-filled)
    # so the one-row-per-bullet layout is obvious; the exec adds/removes
    # rows to match how many goals each section actually has.
    row_num = 2
    for sec in TEMPLATE_SECTIONS:
        for _ in range(_SCAFFOLD_ROWS_PER_SECTION):
            ws.cell(row=row_num, column=1, value=sec.category)
            ws.cell(row=row_num, column=2, value=sec.label)
            content = ws.cell(row=row_num, column=3, value="")
            content.alignment = Alignment(wrap_text=True, vertical="top")
            row_num += 1
    dropdown_last_row = max(row_num - 1, _DROPDOWN_MAX_ROW)

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 80
    ws.column_dimensions["D"].width = 14
    ws.freeze_panes = "A2"

    # Lock the Section column to the known labels so users can't invent
    # labels the parser will ignore. (No commas in labels → safe inline list.)
    labels = ",".join(s.label for s in TEMPLATE_SECTIONS)
    section_dv = DataValidation(type="list", formula1=f'"{labels}"', allow_blank=False)
    section_dv.add(f"B2:B{dropdown_last_row}")
    ws.add_data_validation(section_dv)

    # Confidential dropdown — unlike Section above, blank is a valid,
    # meaningful answer here ("open"), so allow_blank=True.
    confidential_dv = DataValidation(type="list", formula1='"No,Yes"', allow_blank=True)
    confidential_dv.add(f"D2:D{dropdown_last_row}")
    ws.add_data_validation(confidential_dv)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_vto_workbook(data: bytes) -> ParsedVto:
    """Parse a filled-in template into per-section bullet lists.

    Tolerant: finds the ``VTO`` sheet case-insensitively (falls back to the
    first sheet), locates the Section/Content/Confidential columns from a
    header row, maps each Section label case-insensitively, splits Content
    on newlines, and merges rows that repeat a section. Unknown Section
    labels are reported, not fatal — an unrecognized Confidential value IS
    fatal (see ``_parse_confidential``), since guessing on a security flag
    is worse than making the exec fix a typo.

    Raises ``VtoParseError`` if the bytes aren't a valid ``.xlsx``, the sheet
    is missing the Section/Content/Confidential headers, a Confidential cell
    has an unrecognized value, or no recognized section has any content.
    """
    import zipfile

    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, OSError) as e:
        raise VtoParseError(
            "That file isn't a valid .xlsx workbook. Download a fresh template "
            "and try again."
        ) from e

    try:
        ws = None
        for name in wb.sheetnames:
            if name.strip().lower() == _SHEET_NAME.lower():
                ws = wb[name]
                break
        if ws is None and wb.sheetnames:
            ws = wb[wb.sheetnames[0]]
        if ws is None:
            raise VtoParseError("The workbook has no sheets.")

        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()  # read_only mode holds a file handle until closed

    # Locate the header row (the one carrying both "Section" and "Content").
    section_col = content_col = confidential_col = None
    data_start = 0
    for idx, row in enumerate(rows):
        norm = [("" if v is None else str(v)).strip().lower() for v in row]
        if "section" in norm and "content" in norm:
            section_col = norm.index("section")
            content_col = norm.index("content")
            confidential_col = (
                norm.index("confidential") if "confidential" in norm else None
            )
            data_start = idx + 1
            break
    if section_col is None or content_col is None:
        raise VtoParseError(
            "Couldn't find the 'Section' and 'Content' columns. Use the "
            "provided template (fill in the 'VTO' tab)."
        )
    if confidential_col is None:
        raise VtoParseError(
            "Couldn't find the 'Confidential' column. Download a fresh "
            "template and try again."
        )

    sections: dict[str, list[Bullet]] = {}
    ignored: list[str] = []
    for row in rows[data_start:]:
        if not row or section_col >= len(row):
            continue
        raw_label = row[section_col]
        if raw_label is None or not str(raw_label).strip():
            continue
        label = str(raw_label).strip()
        section_type = _LABEL_TO_TYPE.get(label.lower())
        if section_type is None:
            if label not in ignored:
                ignored.append(label)
            continue
        content_val = row[content_col] if content_col < len(row) else None
        lines = _content_lines(content_val)
        if not lines:
            continue
        confidential_val = (
            row[confidential_col] if confidential_col < len(row) else None
        )
        confidential = _parse_confidential(
            confidential_val, row_label=f"{label}: {lines[0]}"
        )
        bullets = [Bullet(line, confidential) for line in lines]
        sections.setdefault(section_type, []).extend(bullets)

    if not sections:
        raise VtoParseError(
            "No VTO content found. Fill in at least one section's Content "
            "column, then upload again."
        )
    return ParsedVto(sections=sections, ignored_labels=ignored)
