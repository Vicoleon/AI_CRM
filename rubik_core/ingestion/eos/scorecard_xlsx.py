"""Excel ⇄ Scorecard conversion for the team uploader.

Same recipe as the VTO uploader (``vto_xlsx.py``): build the fill-in ``.xlsx``
a team downloads, and parse a filled-in one back into rows the router applies.
Kept out of the router (per the ``rubik_core/`` vs ``backend/`` layering rule)
and free of DB/session imports, so build/parse can be unit-tested with just
openpyxl.

Template shape — sheet ``Scorecard`` — ONE ROW PER MEASURABLE, one column per
week (newest week first, matching the on-screen grid)::

    Owner Email | Measurable | Team  | Value Type | Compare | Goal  | ... | 2026-08-02 | 2026-07-26
    jd@…        | Closed $   | Sales | Currency   | >       | 33000 |     | 34300      | 31200
    jd@…        | New logos  | Sales | Number     | >=      | 3     |     | 4          |

The generated template is **empty** — header row, week columns and dropdowns,
no scorecard content. It's a bulk-entry sheet, not an export: a template that
pre-filled the caller's measurables made the download a read of scorecard data,
which for an admin or exec meant the file carried Exec/Leadership rows. The only
caller-specific things in the file now are the Team and Owner dropdown lists,
both access-scoped to what the page's own pickers show.

A row is matched to an existing measurable by (team, name, owner) — case
insensitively — and updated in place; otherwise the measurable is created. A
blank cell is left alone, never cleared: a bulk import must not silently wipe a
number somebody entered on the page.

Units. Storage is canonical — percent as a FRACTION (0.9495), currency/number
as a plain number — but nobody types a fraction, on the page or here: percent
cells are read as whole percents ("94.95" → 0.9495), mirroring
``fromEditableScorecardValue`` in ``frontend/src/pages/eos/_utils.ts``. A cell
Excel has already number-formatted as a percent is *already* a fraction, so it
is stored as-is — without that check a percent-formatted workbook would land
100× too small. See ``storage_value``.

Weeks. A scorecard week is identified by its **Sunday**: the page builds its
columns as Sundays and matches entries by exact ``YYYY-MM-DD``, so any other
weekday renders as "—" even though the row exists. Every date header is
snapped back to its Sunday on parse (``snap_to_sunday``).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from rubik_core.brand import BRAND_HEX, INK_HEX, PRODUCT_NAME
from rubik_core.company.profile import company_name_for_documents

# xlsx MIME type — used by the download endpoint's Content-Type.
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_SHEET_NAME = "Scorecard"
_LISTS_SHEET = "Lists"

# Fixed (non-week) columns, in template order. Everything to the right of these
# is a week column.
COL_METRIC_ID = "Metric ID"
COL_OWNER = "Owner Email"
COL_MEASURABLE = "Measurable"
COL_TEAM = "Team"
COL_VALUE_TYPE = "Value Type"
COL_COMPARE = "Compare"
COL_GOAL = "Goal"
COL_GOAL_LABEL = "Goal Label"
COL_DESCRIPTION = "Description"

FIXED_HEADERS: tuple[str, ...] = (
    COL_METRIC_ID,
    COL_OWNER,
    COL_MEASURABLE,
    COL_TEAM,
    COL_VALUE_TYPE,
    COL_COMPARE,
    COL_GOAL,
    COL_GOAL_LABEL,
    COL_DESCRIPTION,
)

# value_type as stored ⇄ the label shown in the sheet's dropdown. Mirrors
# VALUE_TYPES in frontend/src/pages/eos/_utils.ts; a test pins them together.
VALUE_TYPE_LABELS: dict[str, str] = {
    "number": "Number",
    "currency": "Currency",
    "percent": "Percent",
}
_LABEL_TO_VALUE_TYPE = {label.lower(): vt for vt, label in VALUE_TYPE_LABELS.items()}
# Accept the stored form typed directly, too ("percent" as well as "Percent").
_LABEL_TO_VALUE_TYPE.update({vt: vt for vt in VALUE_TYPE_LABELS})

COMPARATORS: tuple[str, ...] = (">", ">=", "<", "<=", "=")

# Data-validation dropdowns are applied down to this row so rows the user adds
# past the pre-formatted block still get them.
_DROPDOWN_MAX_ROW = 400

_NUMERIC_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class ScorecardParseError(ValueError):
    """An uploaded workbook isn't a usable Scorecard template. The router maps
    this to HTTP 400 with the message shown to the user."""


# -- value units ------------------------------------------------------------


def format_decimal(d: Decimal) -> str:
    """Shortest exact plain-decimal text for ``d`` — "34300", not "3.43E+4"."""
    d = d.normalize()
    if d == 0:
        return "0"
    exponent = d.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        d = d.quantize(Decimal(1))
    return format(d, "f")


def _numeric_text(raw: str) -> str | None:
    """The numeric text inside a cell, tolerating the ``$``/``,``/``%``/spaces
    a human might type. ``None`` when it isn't a number (e.g. "TBD") — such a
    value is stored verbatim rather than zeroed, same as the page."""
    stripped = raw.strip().translate(str.maketrans("", "", "$,% \t "))
    if not stripped or not _NUMERIC_RE.match(stripped):
        return None
    return stripped


def shift_decimal(numeric: str, places: int) -> str:
    """Shift ``numeric`` by ``places`` powers of ten, exactly. The JS twin
    (``shiftDecimal``) exists for the same reason: a plain ``/ 100`` leaves
    binary-float artifacts (0.09250000000000001) that we would then *store* as
    the canonical value."""
    try:
        return format_decimal(Decimal(numeric).scaleb(places))
    except InvalidOperation:
        return numeric


@dataclass(frozen=True)
class RawCell:
    """One value cell as it came out of the workbook, before we know which
    ``value_type`` governs it.

    The parser can't do the percent conversion itself: when a row updates an
    existing measurable and leaves ``Value Type`` blank, the governing type is
    the one already stored in the DB. So the parser hands back the raw shape
    and the router calls ``storage_value`` once it knows the effective type.
    """

    text: str  # trimmed textual form; never "" (blank cells aren't emitted)
    numeric: str | None  # numeric text if it parses, else None
    percent_formatted: bool  # the cell carried an Excel percent number format


def storage_value(cell: RawCell, value_type: str | None) -> str:
    """The canonical string to STORE for one filled-in cell.

    * non-percent measurable → the number as typed (or the raw text for a
      non-numeric value like "TBD").
    * percent measurable → the typed number read as whole percents
      ("94.95" → "0.9495"), matching every other percent input in the app.
    * percent measurable whose cell Excel already formats as a percent → the
      underlying value IS the fraction, so it's stored unchanged. (Excel shows
      "94.95%" and hands us 0.9495; treating that as whole percents would
      store 0.009495 — a silent 100× error.)
    """
    if cell.numeric is None:
        return cell.text
    if value_type != "percent" or cell.percent_formatted:
        return format_decimal(Decimal(cell.numeric))
    return shift_decimal(cell.numeric, -2)


# -- weeks ------------------------------------------------------------------


def snap_to_sunday(d: date) -> date:
    """The Sunday that starts ``d``'s scorecard week (a no-op for a Sunday).

    Non-Sunday week starts are the single most common scorecard import bug:
    the page renders its columns as Sundays and matches on the exact date, so
    a Monday-anchored entry exists in the DB but shows as "—" forever.
    """
    return d - timedelta(days=(d.weekday() + 1) % 7)


def past_sundays(n: int, *, today: date | None = None) -> list[date]:
    """The last ``n`` scorecard weeks, newest first — the same axis
    ``getPastWeeks`` builds on the page."""
    start = snap_to_sunday(today or date.today())
    return [start - timedelta(days=7 * i) for i in range(max(n, 1))]


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y")


def _as_date(value) -> date | None:
    """Read a week-column header as a date. Excel hands back a ``datetime`` for
    a real date cell; a workbook saved from Sheets (or retyped by hand) can
    hand back a string, so the common written forms are accepted too."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = ("" if value is None else str(value)).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# -- parsed shapes ----------------------------------------------------------


@dataclass(frozen=True)
class ParsedRow:
    """One measurable's row, as filled in. Every field is optional except the
    row number: a row that updates an existing measurable may leave everything
    but a week cell blank."""

    row_number: int  # 1-based sheet row, so errors can name it
    metric_id: str | None  # blank → create a new measurable
    owner_email: str | None
    name: str | None
    team_label: str | None
    value_type: str | None  # canonical ("percent"), not the sheet's label
    comparator: str | None
    goal: RawCell | None  # target_value, in the same units as a week cell
    goal_description: str | None
    description: str | None
    values: dict[date, RawCell]  # Sunday → filled-in cell (blanks omitted)


@dataclass(frozen=True)
class ParsedScorecard:
    rows: list[ParsedRow]
    weeks: list[date] = field(default_factory=list)  # Sundays seen, newest first
    ignored_columns: list[str] = field(default_factory=list)


# -- template build ---------------------------------------------------------


def template_filename(team_label: str | None) -> str:
    """``Cubie_Scorecard_Sales_template.xlsx`` — the team is in the filename so
    a user who exports two teams doesn't overwrite one with the other."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (team_label or "").strip()).strip("_")
    return (
        f"{PRODUCT_NAME}_Scorecard_{slug}_template.xlsx"
        if slug
        else f"{PRODUCT_NAME}_Scorecard_template.xlsx"
    )


_INSTRUCTIONS = [
    "Scorecard — upload template",
    "",
    "1. Open the 'Scorecard' tab. ONE ROW PER MEASURABLE; one column per week. "
    "The sheet is blank — you fill it in.",
    "2. For each row: pick the Owner Email and the Team from the dropdowns, type "
    "the Measurable name, then type each week's number in the dated columns. "
    "The newest week is on the left.",
    "3. Value Type / Compare / Goal are optional — set them to have the grid "
    "render the number as currency or a percent and colour it against a goal.",
    "4. A BLANK cell always means 'leave as is'. Blank never deletes anything — "
    "clear a value on the Scorecard page instead.",
    "5. If a Measurable name + Owner + Team already exists, its weekly numbers "
    "are updated in place; otherwise the measurable is created. So spell an "
    "existing name exactly as it appears on the page, or you'll get a second "
    "measurable alongside it (the import tells you which ones it added).",
    "6. Don't rename the 'Scorecard' tab and don't change the header row.",
    "7. Percent measurables are typed as WHOLE PERCENTS: enter 94.95 for 94.95%, "
    "50 for 50% — the same as typing into the grid.",
    f"8. Save the file, then upload it on the Scorecard page in {PRODUCT_NAME}.",
    "",
    "Who can change what: the 'Team' and 'Owner Email' dropdowns list only the "
    "teams you may add measurables to and the people you may assign, and you may "
    "only update measurables you can already edit on the Scorecard page (yours, "
    "your team's if you lead it, or all of them for admins/Exec). Anything else "
    "is rejected — the whole upload is applied only if every row passes, so "
    "nothing lands half-done.",
]

# Columns written into the generated template. ``Metric ID`` is deliberately
# NOT among them: the sheet ships empty, so there's no id to carry, and a
# blank template means a download exposes no scorecard content at all — not
# even to a reader who could see it on the page. The parser still recognizes
# the column (see ``FIXED_HEADERS``) so a workbook that has one from anywhere
# else keeps working.
TEMPLATE_HEADERS: tuple[str, ...] = tuple(
    h for h in FIXED_HEADERS if h != COL_METRIC_ID
)

# Blank rows offered on the generated sheet.
_TEMPLATE_ROWS = 40


def build_scorecard_template_workbook(
    *,
    weeks: list[date],
    team_labels: list[str],
    owner_emails: list[str],
    current_team_label: str | None = None,
    rows: int = _TEMPLATE_ROWS,
    company_name: str | None = None,
) -> bytes:
    """Generate the blank fill-in ``.xlsx`` for bulk-entering weekly numbers.

    The sheet carries **no scorecard data** — only the header row, the week
    columns (``weeks``, Sundays newest-first) and empty rows with dropdowns.
    That's deliberate: a template that pre-filled the caller's measurables made
    the download a read of scorecard content, which for an admin or exec meant
    the file contained Exec/Leadership rows. Now the only caller-specific things
    in the file are the two dropdown vocabularies, which are themselves
    access-scoped: ``team_labels`` are the teams the caller may publish to and
    ``owner_emails`` the people they may assign — the same lists the page's own
    pickers show them.

    The heading and the workbook's title name the company (``company_name``,
    else the saved profile, else the product's name).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    company = (company_name or "").strip() or company_name_for_documents()
    heading = f"{company} — {_INSTRUCTIONS[0]}"
    wb = Workbook()
    wb.properties.title = heading

    # -- Instructions sheet (first, so it opens here) -------------------
    info = wb.active
    info.title = "Instructions"
    for i, line in enumerate([heading, *_INSTRUCTIONS[1:]], start=1):
        cell = info.cell(row=i, column=1, value=line)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        if i == 1:
            cell.font = Font(bold=True, size=14)
    if current_team_label:
        info.cell(
            row=len(_INSTRUCTIONS) + 2,
            column=1,
            value=f"This template was generated for: {current_team_label}",
        ).font = Font(italic=True)
    info.column_dimensions["A"].width = 100

    # -- Scorecard sheet (the one they edit) ---------------------------
    ws = wb.create_sheet(title=_SHEET_NAME)
    # Cubie violet over the fixed columns, ink over the week columns: both
    # carry white header text at well over 4.5:1.
    header_fill = PatternFill("solid", fgColor=BRAND_HEX.lstrip("#"))
    week_fill = PatternFill("solid", fgColor=INK_HEX.lstrip("#"))
    header_font = Font(bold=True, color="FFFFFF")

    n_fixed = len(TEMPLATE_HEADERS)
    headers = [*TEMPLATE_HEADERS, *(w.isoformat() for w in weeks)]
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.fill = header_fill if col <= n_fixed else week_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    last_row = max(1 + max(rows, 1), _DROPDOWN_MAX_ROW)

    widths = {
        COL_OWNER: 28,
        COL_MEASURABLE: 34,
        COL_TEAM: 18,
        COL_VALUE_TYPE: 12,
        COL_COMPARE: 10,
        COL_GOAL: 12,
        COL_GOAL_LABEL: 20,
        COL_DESCRIPTION: 40,
    }
    for i, header in enumerate(TEMPLATE_HEADERS):
        ws.column_dimensions[get_column_letter(i + 1)].width = widths.get(header, 16)
    for i in range(len(weeks)):
        ws.column_dimensions[get_column_letter(n_fixed + 1 + i)].width = 12
    # Keep the identifying columns on screen while the weeks scroll — the same
    # frozen-columns behaviour the page has.
    ws.freeze_panes = ws.cell(row=2, column=n_fixed + 1).coordinate

    # -- Lists sheet (dropdown vocabularies; hidden) -------------------
    lists = wb.create_sheet(title=_LISTS_SHEET)
    lists.cell(row=1, column=1, value="Teams")
    lists.cell(row=1, column=2, value="Owners")
    lists.cell(row=1, column=3, value="Value Types")
    lists.cell(row=1, column=4, value="Compare")
    for i, label in enumerate(team_labels, start=2):
        lists.cell(row=i, column=1, value=label)
    for i, email in enumerate(owner_emails, start=2):
        lists.cell(row=i, column=2, value=email)
    for i, label in enumerate(VALUE_TYPE_LABELS.values(), start=2):
        lists.cell(row=i, column=3, value=label)
    for i, comparator in enumerate(COMPARATORS, start=2):
        lists.cell(row=i, column=4, value=comparator)
    lists.sheet_state = "hidden"

    def _dv(list_col: str, count: int, target_col: str) -> None:
        """Range-backed dropdown (an inline list caps out at 255 chars, and the
        owner roster blows past that)."""
        if count <= 0:
            return
        dv = DataValidation(
            type="list",
            formula1=f"={_LISTS_SHEET}!${list_col}$2:${list_col}${count + 1}",
            allow_blank=True,
        )
        dv.add(f"{target_col}2:{target_col}{last_row}")
        ws.add_data_validation(dv)

    def _col(header: str) -> str:
        """Sheet column letter for a fixed header — derived, so the dropdowns
        can't drift out of alignment when the column order changes."""
        return get_column_letter(TEMPLATE_HEADERS.index(header) + 1)

    _dv("B", len(owner_emails), _col(COL_OWNER))
    _dv("A", len(team_labels), _col(COL_TEAM))
    _dv("C", len(VALUE_TYPE_LABELS), _col(COL_VALUE_TYPE))
    _dv("D", len(COMPARATORS), _col(COL_COMPARE))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# -- parse ------------------------------------------------------------------


def _cell_text(value) -> str:
    """Trimmed textual form of a cell value. Numbers keep their exact decimal
    form (Excel hands back 34300.0 for "34300")."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        try:
            return format_decimal(Decimal(str(value)))
        except InvalidOperation:
            return str(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _is_percent_formatted(cell) -> bool:
    """Does this cell carry an Excel percent number format? Its value is then
    already a fraction (0.9495 displayed as "94.95%")."""
    fmt = getattr(cell, "number_format", None)
    return isinstance(fmt, str) and "%" in fmt


def _raw_cell(cell) -> RawCell | None:
    text = _cell_text(getattr(cell, "value", None))
    if not text:
        return None
    return RawCell(
        text=text,
        numeric=_numeric_text(text),
        percent_formatted=_is_percent_formatted(cell),
    )


def _parse_choice(
    raw: str, vocabulary: dict[str, str], *, column: str, row_number: int
) -> str:
    """Map one dropdown cell to its canonical value. An unrecognized value is a
    hard error, never a guess: ``Value Type`` decides whether a number is read
    as whole percents, so guessing it wrong stores every value in the row 100×
    off."""
    key = raw.strip().lower()
    if key in vocabulary:
        return vocabulary[key]
    allowed = ", ".join(sorted(set(vocabulary.values())))
    raise ScorecardParseError(
        f'Row {row_number}: "{raw}" isn\'t a valid {column}. '
        f"Use one of: {allowed}. Fix it and re-upload."
    )


def parse_scorecard_workbook(data: bytes) -> ParsedScorecard:
    """Parse a filled-in template into per-measurable rows.

    Tolerant about layout: finds the ``Scorecard`` sheet case-insensitively
    (falls back to the first sheet), locates the header row by its
    ``Measurable`` column, matches the fixed column names case-insensitively in
    any order, and treats every remaining column whose header reads as a date
    as a week (snapped to its Sunday). Unrecognized columns are reported, not
    fatal.

    Raises ``ScorecardParseError`` when the bytes aren't a valid ``.xlsx``, the
    header row is missing, a ``Value Type`` / ``Compare`` cell has an
    unrecognized value, or no row carries any content.
    """
    import zipfile

    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        # Not read_only: we need each cell's number_format to tell an already-
        # fractional percent cell from a whole-percent one, and read-only rows
        # don't reliably carry style information.
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, OSError, KeyError) as e:
        raise ScorecardParseError(
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
            raise ScorecardParseError("The workbook has no sheets.")
        rows = list(ws.iter_rows())
    finally:
        wb.close()

    fixed_lookup = {h.lower(): h for h in FIXED_HEADERS}
    header_row_idx: int | None = None
    columns: dict[str, int] = {}  # fixed header -> column index
    week_columns: list[tuple[int, date]] = []
    ignored: list[str] = []

    for idx, row in enumerate(rows):
        labels = [_cell_text(c.value).lower() for c in row]
        if COL_MEASURABLE.lower() not in labels:
            continue
        header_row_idx = idx
        for col_idx, cell in enumerate(row):
            label = _cell_text(cell.value)
            if not label:
                continue
            fixed = fixed_lookup.get(label.lower())
            if fixed is not None:
                columns.setdefault(fixed, col_idx)
                continue
            week = _as_date(cell.value)
            if week is not None:
                week_columns.append((col_idx, snap_to_sunday(week)))
            elif label not in ignored:
                ignored.append(label)
        break

    if header_row_idx is None:
        raise ScorecardParseError(
            f"Couldn't find the '{COL_MEASURABLE}' header. Use the provided "
            f"template (fill in the '{_SHEET_NAME}' tab)."
        )

    def _text(row, header: str) -> str:
        col = columns.get(header)
        if col is None or col >= len(row):
            return ""
        return _cell_text(row[col].value)

    parsed_rows: list[ParsedRow] = []
    for offset, row in enumerate(rows[header_row_idx + 1 :], start=1):
        row_number = header_row_idx + 1 + offset  # 1-based sheet row
        values: dict[date, RawCell] = {}
        for col_idx, week in week_columns:
            if col_idx >= len(row):
                continue
            cell = _raw_cell(row[col_idx])
            if cell is not None:
                # Two columns snapping to the same week: the rightmost (older
                # in template order) wins only if the newer one is blank.
                values.setdefault(week, cell)

        metric_id = _text(row, COL_METRIC_ID)
        owner_email = _text(row, COL_OWNER)
        name = _text(row, COL_MEASURABLE)
        team_label = _text(row, COL_TEAM)
        value_type_raw = _text(row, COL_VALUE_TYPE)
        comparator_raw = _text(row, COL_COMPARE)
        goal_label = _text(row, COL_GOAL_LABEL)
        description = _text(row, COL_DESCRIPTION)
        goal_col = columns.get(COL_GOAL)
        goal = (
            _raw_cell(row[goal_col])
            if goal_col is not None and goal_col < len(row)
            else None
        )

        if not any(
            [
                metric_id,
                owner_email,
                name,
                team_label,
                value_type_raw,
                comparator_raw,
                goal_label,
                description,
                goal,
                values,
            ]
        ):
            continue  # entirely blank row (the scaffold rows nobody filled in)

        value_type = (
            _parse_choice(
                value_type_raw,
                _LABEL_TO_VALUE_TYPE,
                column=COL_VALUE_TYPE,
                row_number=row_number,
            )
            if value_type_raw
            else None
        )
        comparator = (
            _parse_choice(
                comparator_raw,
                {c: c for c in COMPARATORS},
                column=COL_COMPARE,
                row_number=row_number,
            )
            if comparator_raw
            else None
        )

        parsed_rows.append(
            ParsedRow(
                row_number=row_number,
                metric_id=metric_id or None,
                owner_email=owner_email or None,
                name=name or None,
                team_label=team_label or None,
                value_type=value_type,
                comparator=comparator,
                goal=goal,
                goal_description=goal_label or None,
                description=description or None,
                values=values,
            )
        )

    if not parsed_rows:
        raise ScorecardParseError(
            "No measurables found. Fill in at least one row on the "
            f"'{_SHEET_NAME}' tab, then upload again."
        )

    weeks = sorted({w for _, w in week_columns}, reverse=True)
    return ParsedScorecard(rows=parsed_rows, weeks=weeks, ignored_columns=ignored)
