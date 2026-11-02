"""Tests for the Scorecard Excel template + uploader.

Two layers:

1. Pure build/parse unit tests on ``rubik_core.ingestion.eos.scorecard_xlsx``
   (no app, no DB) — the template's shape, the whole-percent ⇄ fraction
   conversion (including the percent-number-format trap that would otherwise
   store every value 100× off), and Sunday snapping.
2. Endpoint tests for ``GET /api/eos/scorecard/template`` and
   ``POST /api/eos/scorecard/upload``, which must enforce exactly the Scorecard
   page's own access rules: you can only see/pre-fill measurables you may read
   AND write, you can only update ones ``can_mutate`` allows, you can only
   create on a team you belong to (the Exec team being Exec-only), and you can
   only assign owners you may already assign. Plus: the upload is
   all-or-nothing, and a blank week cell never clears a value.
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.ingestion.eos.scorecard_xlsx import (
    COMPARATORS,
    FIXED_HEADERS,
    TEMPLATE_HEADERS,
    VALUE_TYPE_LABELS,
    XLSX_MEDIA_TYPE,
    RawCell,
    ScorecardParseError,
    build_scorecard_template_workbook,
    parse_scorecard_workbook,
    past_sundays,
    snap_to_sunday,
    storage_value,
    template_filename,
)

client = TestClient(app)

WEEKS = [date(2026, 8, 2), date(2026, 7, 26)]  # Sundays, newest first


# -- helpers ----------------------------------------------------------------


def _make_workbook(
    rows: list[dict],
    weeks: list[date] | None = None,
    *,
    sheet_name: str = "Scorecard",
    headers: list | None = None,
    percent_formatted: bool = False,
) -> bytes:
    """Build a filled-in template for parser/upload tests.

    Each row is a dict keyed by the fixed header names plus ``values`` (a
    ``{week: value}`` mapping).
    """
    from openpyxl import Workbook

    weeks = WEEKS if weeks is None else weeks
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(
        list(headers)
        if headers is not None
        else [*FIXED_HEADERS, *(w.isoformat() for w in weeks)]
    )
    for row in rows:
        values = row.get("values", {})
        ws.append(
            [*(row.get(h) for h in FIXED_HEADERS), *(values.get(w) for w in weeks)]
        )
        if percent_formatted:
            for i in range(len(weeks)):
                ws.cell(
                    row=ws.max_row, column=len(FIXED_HEADERS) + 1 + i
                ).number_format = "0.00%"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _profile(
    email: str,
    *,
    roles: list[str] | None = None,
    department: str = "general",
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-08-05T00:00:00"
    return UserProfile(
        id=email,
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        created_at=now,
        updated_at=now,
        is_exec=is_exec,
        managed_departments=managed or [],
    )


def _as_user(profile: UserProfile):
    app.dependency_overrides[get_current_user] = lambda: profile


# The cast of test users: department, exec flag, managed teams.
USERS = {
    "owner@example.com": dict(department="Sales"),
    "teammate@example.com": dict(department="Sales"),
    "manager@example.com": dict(department="Sales", managed=["Sales"]),
    "exec@example.com": dict(department="Compliance", is_exec=True),
    "outsider@example.com": dict(department="Compliance"),
}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_eos_scorecard_upload.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base, Department, User

    Base.metadata.create_all(bind=engine)

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person

    db = SessionLocal()
    try:
        for name in ("Sales", "Compliance"):
            db.add(Department(name=name))
        for email, spec in USERS.items():
            db.add(
                User(
                    email=email,
                    id=email,
                    name=email.split("@")[0].title(),
                    department=spec["department"],
                    is_exec=bool(spec.get("is_exec")),
                    managed_departments=spec.get("managed") or [],
                )
            )
            db.add(
                Person(
                    first_name=email.split("@")[0].title(),
                    last_name="Tester",
                    email=email,
                    created_by="test",
                )
            )
        db.commit()
    finally:
        db.close()

    yield
    app.dependency_overrides.clear()


def _person_id(email: str):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person

    db = SessionLocal()
    try:
        return db.query(Person).filter(Person.email == email).first().id
    finally:
        db.close()


def _seed_metric(
    name: str,
    owner_email: str,
    *,
    department: str | None = "Sales",
    is_exec: bool = False,
    value_type: str = "number",
    created_by: str = "seed@example.com",
    **kwargs,
):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import ScorecardMetric

    db = SessionLocal()
    try:
        metric = ScorecardMetric(
            owner_id=_person_id(owner_email),
            department_name=None if is_exec else department,
            is_exec=is_exec,
            name=name,
            value_type=value_type,
            created_by=created_by,
            **kwargs,
        )
        db.add(metric)
        db.commit()
        db.refresh(metric)
        return metric.id
    finally:
        db.close()


def _seed_entry(metric_id, week: date, value: str):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import ScorecardEntry

    db = SessionLocal()
    try:
        db.add(
            ScorecardEntry(
                metric_id=metric_id,
                week_start_date=datetime(week.year, week.month, week.day),
                value=value,
                created_by="seed@example.com",
            )
        )
        db.commit()
    finally:
        db.close()


def _metrics():
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import ScorecardMetric

    db = SessionLocal()
    try:
        return (
            db.query(ScorecardMetric)
            .filter(ScorecardMetric.is_deleted == False)  # noqa: E712
            .all()
        )
    finally:
        db.close()


def _entries(metric_id=None):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import ScorecardEntry

    db = SessionLocal()
    try:
        query = db.query(ScorecardEntry).filter(
            ScorecardEntry.is_deleted == False  # noqa: E712
        )
        if metric_id is not None:
            query = query.filter(ScorecardEntry.metric_id == metric_id)
        return query.all()
    finally:
        db.close()


def _post(data: bytes):
    return client.post(
        "/api/eos/scorecard/upload",
        files={"file": ("scorecard.xlsx", data, XLSX_MEDIA_TYPE)},
    )


# -- unit: units & weeks ----------------------------------------------------


def test_value_type_labels_and_comparators_match_the_stored_vocabulary():
    """The dropdown labels map onto the value_type values the DB column and the
    frontend's VALUE_TYPES / COMPARATORS use — drift here silently changes how
    a number is rendered (and, for percent, its magnitude)."""
    assert set(VALUE_TYPE_LABELS) == {"number", "currency", "percent"}
    assert COMPARATORS == (">", ">=", "<", "<=", "=")


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("50", "0.5"),  # 50 means 50%, not 5000%
        ("9.25", "0.0925"),  # and no 0.09250000000000001 float artifact
        ("94.95", "0.9495"),
        ("100", "1"),
        ("0.5", "0.005"),  # the flip side of one consistent rule
    ],
)
def test_percent_cells_are_read_as_whole_percents(typed, expected):
    cell = RawCell(text=typed, numeric=typed, percent_formatted=False)
    assert storage_value(cell, "percent") == expected


def test_percent_formatted_cell_is_already_a_fraction():
    """Excel shows "94.95%" and hands back 0.9495. Reading that as whole
    percents would store 0.009495 — a silent 100× error."""
    cell = RawCell(text="0.9495", numeric="0.9495", percent_formatted=True)
    assert storage_value(cell, "percent") == "0.9495"


def test_non_percent_and_non_numeric_values_pass_through():
    assert storage_value(RawCell("34300.0", "34300.0", False), "currency") == "34300"
    assert storage_value(RawCell("TBD", None, False), "number") == "TBD"
    assert storage_value(RawCell("TBD", None, False), "percent") == "TBD"


def test_snap_to_sunday_and_past_sundays():
    assert snap_to_sunday(date(2026, 8, 2)) == date(2026, 8, 2)  # already Sunday
    assert snap_to_sunday(date(2026, 8, 3)) == date(2026, 8, 2)  # Monday
    assert snap_to_sunday(date(2026, 8, 8)) == date(2026, 8, 2)  # Saturday
    weeks = past_sundays(3, today=date(2026, 8, 5))
    assert weeks == [date(2026, 8, 2), date(2026, 7, 26), date(2026, 7, 19)]
    assert all(w.weekday() == 6 for w in weeks)


def test_template_filename_names_the_team():
    assert template_filename("Sales") == "Cubie_Scorecard_Sales_template.xlsx"
    assert (
        template_filename("Executive View")
        == "Cubie_Scorecard_Executive_View_template.xlsx"
    )
    assert template_filename(None) == "Cubie_Scorecard_template.xlsx"


# -- unit: build ------------------------------------------------------------


def _built_template(**kwargs):
    defaults = dict(
        weeks=WEEKS,
        team_labels=["Sales"],
        owner_emails=["owner@example.com"],
        current_team_label="Sales",
        rows=5,
    )
    defaults.update(kwargs)
    return build_scorecard_template_workbook(**defaults)


def test_built_template_is_blank():
    """The template is a bulk-ENTRY sheet, not an export: header row, week
    columns and dropdowns, and no data. Pre-filling it made the download a read
    of scorecard content, which for an admin/exec put Exec/Leadership numbers in
    a file on someone's laptop."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(_built_template()))
    assert wb.sheetnames == ["Instructions", "Scorecard", "Lists"]
    # The dropdown vocabulary sheet is a mechanism, not content.
    assert wb["Lists"].sheet_state == "hidden"
    ws = wb["Scorecard"]
    assert [c.value for c in ws[1]] == [
        *TEMPLATE_HEADERS,
        *(w.isoformat() for w in WEEKS),
    ]
    # No Metric ID column: there's no row to identify, and no id to hand out.
    assert "Metric ID" not in TEMPLATE_HEADERS
    # Every cell below the header is empty.
    filled = [
        c.value
        for row in ws.iter_rows(min_row=2)
        for c in row
        if c.value not in (None, "")
    ]
    assert filled == []


def test_built_template_carries_no_scorecard_data_at_all():
    """Byte-level backstop for the leak this replaced: nothing about any
    measurable may appear anywhere in the file — not in the sheet, not in the
    shared-strings table, not in a stale cached value."""
    import zipfile

    data = _built_template()
    blob = b""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            blob += z.read(name)
    for secret in (b"Closed Revenue", b"Audit accuracy", b"34300", b"0.9495"):
        assert secret not in blob


def test_template_dropdowns_only_offer_what_the_caller_may_use():
    """The only caller-specific content in the file — and it's the same
    vocabulary the page's own pickers show."""
    from openpyxl import load_workbook

    lists = load_workbook(
        io.BytesIO(
            _built_template(
                team_labels=["Sales", "Executive View"],
                owner_emails=["owner@example.com", "teammate@example.com"],
            )
        )
    )["Lists"]
    assert [c.value for c in lists["A"][1:3]] == ["Sales", "Executive View"]
    assert [c.value for c in lists["B"][1:3]] == [
        "owner@example.com",
        "teammate@example.com",
    ]


# -- unit: parse ------------------------------------------------------------


def test_parse_reads_ids_teams_and_week_values():
    data = _make_workbook(
        [
            {
                "Metric ID": "33333333-3333-3333-3333-333333333333",
                "Owner Email": "owner@example.com",
                "Measurable": "Closed Revenue",
                "Team": "Sales",
                "Value Type": "Currency",
                "Compare": ">",
                "Goal": 33000,
                "values": {WEEKS[0]: 34300, WEEKS[1]: None},
            },
            {
                "Owner Email": "teammate@example.com",
                "Measurable": "New logos",
                "Team": "Sales",
                "values": {WEEKS[0]: 4},
            },
        ]
    )
    parsed = parse_scorecard_workbook(data)
    assert parsed.weeks == WEEKS
    assert parsed.ignored_columns == []
    first, second = parsed.rows
    assert first.metric_id == "33333333-3333-3333-3333-333333333333"
    assert first.value_type == "currency"
    assert list(first.values) == [WEEKS[0]]  # the blank cell isn't emitted
    assert storage_value(first.values[WEEKS[0]], "currency") == "34300"
    assert second.metric_id is None and second.value_type is None
    assert second.row_number == 3  # 1-based sheet row, for error messages


def test_parse_tolerates_sheet_case_column_order_and_date_forms():
    headers = ["measurable", "owner email", "team", "8/3/2026", "2026-07-26"]
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "scorecard"
    ws.append(headers)
    ws.append(["New logos", "owner@example.com", "Sales", 4, 3])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = parse_scorecard_workbook(buf.getvalue())
    # 8/3/2026 is a Monday — snapped back to its Sunday, or the page would
    # render the column as "—" forever.
    assert parsed.weeks == WEEKS
    assert {w.isoformat() for w in parsed.rows[0].values} == {
        "2026-08-02",
        "2026-07-26",
    }


def test_parse_reports_unrecognized_columns_without_failing():
    data = _make_workbook(
        [
            {
                "Measurable": "New logos",
                "Owner Email": "owner@example.com",
                "Team": "Sales",
            }
        ],
        headers=[*FIXED_HEADERS, "Notes", WEEKS[0].isoformat()],
    )
    parsed = parse_scorecard_workbook(data)
    assert parsed.ignored_columns == ["Notes"]


def test_parse_skips_blank_rows_but_raises_when_everything_is_blank():
    data = _make_workbook([{}, {}])
    with pytest.raises(ScorecardParseError, match="No measurables"):
        parse_scorecard_workbook(data)


def test_parse_requires_the_measurable_header():
    data = _make_workbook([{}], headers=["Owner Email", "Team"])
    with pytest.raises(ScorecardParseError, match="Measurable"):
        parse_scorecard_workbook(data)


def test_parse_rejects_garbage_bytes():
    with pytest.raises(ScorecardParseError):
        parse_scorecard_workbook(b"this is not a spreadsheet")


def test_parse_rejects_an_unrecognized_value_type_rather_than_guessing():
    """Value Type decides whether a number is read as whole percents, so a
    typo can't be guessed past — it would store the row 100× off."""
    data = _make_workbook(
        [
            {
                "Measurable": "Audit accuracy",
                "Owner Email": "owner@example.com",
                "Team": "Sales",
                "Value Type": "Percentage",
            }
        ]
    )
    with pytest.raises(ScorecardParseError, match="Value Type"):
        parse_scorecard_workbook(data)


@pytest.mark.parametrize("comparator", COMPARATORS)
def test_every_comparator_parses(comparator):
    """Including the bare "=" — the one value a spreadsheet could mistake for a
    formula and hand back blank."""
    data = _make_workbook(
        [
            {
                "Measurable": "New logos",
                "Owner Email": "owner@example.com",
                "Team": "Sales",
                "Compare": comparator,
            }
        ]
    )
    assert parse_scorecard_workbook(data).rows[0].comparator == comparator


def test_parse_rejects_an_unrecognized_comparator():
    data = _make_workbook(
        [
            {
                "Measurable": "New logos",
                "Owner Email": "owner@example.com",
                "Team": "Sales",
                "Compare": "at least",
            }
        ]
    )
    with pytest.raises(ScorecardParseError, match="Compare"):
        parse_scorecard_workbook(data)


# -- endpoint: template download -------------------------------------------


def test_template_download_is_blank_even_for_an_unrestricted_reader():
    """The regression this replaced: an admin/exec download came pre-filled with
    whatever the page was showing — which for them defaults to the Executive
    View, so the file carried Exec/Leadership numbers. The download must contain
    no scorecard content for ANY caller, however much they can read."""
    import zipfile

    mine = _seed_metric("Closed Revenue", "owner@example.com")
    _seed_entry(mine, WEEKS[0], "34300")
    _seed_metric("Exec-only number", "exec@example.com", is_exec=True)

    for profile in (
        _profile("owner@example.com", department="Sales"),
        _profile("exec@example.com", department="Compliance", is_exec=True),
        _profile("admin@example.com", roles=["admin", "user"], department="Sales"),
    ):
        _as_user(profile)
        r = client.get("/api/eos/scorecard/template")
        assert r.status_code == 200, r.text
        blob = b""
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for name in z.namelist():
                blob += z.read(name)
        for secret in (
            b"Closed Revenue",
            b"Exec-only number",
            b"34300",
            str(mine).encode(),
        ):
            assert secret not in blob, f"{profile.email} got {secret!r} in the template"


def test_template_download_has_only_headers_and_empty_rows():
    from openpyxl import load_workbook

    _seed_metric("Closed Revenue", "owner@example.com")
    _as_user(_profile("owner@example.com", department="Sales"))
    r = client.get("/api/eos/scorecard/template?weeks=4")
    assert r.status_code == 200, r.text
    assert "Cubie_Scorecard_Sales_template.xlsx" in r.headers["content-disposition"]

    ws = load_workbook(io.BytesIO(r.content))["Scorecard"]
    assert [c.value for c in ws[1]][: len(TEMPLATE_HEADERS)] == list(TEMPLATE_HEADERS)
    assert [
        c.value
        for row in ws.iter_rows(min_row=2)
        for c in row
        if c.value not in (None, "")
    ] == []


def test_template_download_rejects_a_team_you_arent_on():
    _as_user(_profile("owner@example.com", department="Sales"))
    assert client.get("/api/eos/scorecard/template?team=__exec__").status_code == 403
    assert client.get("/api/eos/scorecard/template?team=Compliance").status_code == 403


def test_exec_can_download_the_executive_view_template():
    from openpyxl import load_workbook

    _seed_metric("Exec-only number", "exec@example.com", is_exec=True)
    _as_user(_profile("exec@example.com", department="Compliance", is_exec=True))
    r = client.get("/api/eos/scorecard/template?team=__exec__")
    assert r.status_code == 200, r.text
    assert "Executive_View" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    # Executive View is offered in the Team dropdown for an exec…
    assert "Executive View" in [c.value for c in wb["Lists"]["A"]]
    # …and the sheet is still blank (no Exec numbers travel in the file).
    assert [
        c.value
        for row in wb["Scorecard"].iter_rows(min_row=2)
        for c in row
        if c.value not in (None, "")
    ] == []


def test_template_download_without_a_team_explains_itself():
    _as_user(_profile("nobody@example.com"))
    r = client.get("/api/eos/scorecard/template")
    assert r.status_code == 400
    assert "team" in r.json()["detail"].lower()


# -- endpoint: upload — the happy paths ------------------------------------


def test_owner_can_import_weekly_values_sunday_anchored():
    metric_id = _seed_metric(
        "Closed Revenue", "owner@example.com", value_type="currency"
    )
    _as_user(_profile("owner@example.com", department="Sales"))

    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Owner Email": "owner@example.com",
                    "Measurable": "Closed Revenue",
                    "Team": "Sales",
                    "Value Type": "Currency",
                    "values": {WEEKS[0]: 34300, WEEKS[1]: 31200},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["entries_created"], body["metrics_created"]) == (2, 0)

    by_week = {e.week_start_date.date(): e.value for e in _entries(metric_id)}
    assert by_week == {WEEKS[0]: "34300", WEEKS[1]: "31200"}
    # Sunday-anchored or the page renders every cell as "—".
    assert all(w.weekday() == 6 for w in by_week)


def test_import_creates_a_new_measurable_on_your_own_team():
    _as_user(_profile("manager@example.com", department="Sales", managed=["Sales"]))
    r = _post(
        _make_workbook(
            [
                {
                    "Owner Email": "teammate@example.com",
                    "Measurable": "New logos",
                    "Team": "Sales",
                    "Value Type": "Number",
                    "Compare": ">=",
                    "Goal": 3,
                    "Goal Label": ">= 3 / week",
                    "Description": "Signed new customers",
                    "values": {WEEKS[0]: 4},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    assert r.json()["metrics_created"] == 1

    (metric,) = [m for m in _metrics() if m.name == "New logos"]
    assert (metric.department_name, metric.is_exec) == ("Sales", False)
    assert (metric.owner_id, metric.value_type) == (
        _person_id("teammate@example.com"),
        "number",
    )
    assert (metric.comparator, metric.target_value) == (">=", "3")
    assert metric.goal_description == ">= 3 / week"
    assert [e.value for e in _entries(metric.id)] == ["4"]


def test_percent_values_are_imported_as_whole_percents():
    metric_id = _seed_metric(
        "Audit accuracy", "owner@example.com", value_type="percent"
    )
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Measurable": "Audit accuracy",
                    "Value Type": "Percent",
                    "Goal": 98,
                    "values": {WEEKS[0]: 94.95},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    assert [e.value for e in _entries(metric_id)] == ["0.9495"]
    assert [m.target_value for m in _metrics() if m.id == metric_id] == ["0.98"]


def test_percent_formatted_cells_are_not_divided_twice():
    """A workbook whose percent column carries Excel's percent format already
    holds the fraction. Without the number-format check this row would land at
    0.009495 — the 100× bug this guard exists for."""
    metric_id = _seed_metric(
        "Audit accuracy", "owner@example.com", value_type="percent"
    )
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Measurable": "Audit accuracy",
                    "Value Type": "Percent",
                    "values": {WEEKS[0]: 0.9495},
                }
            ],
            percent_formatted=True,
        )
    )
    assert r.status_code == 201, r.text
    assert [e.value for e in _entries(metric_id)] == ["0.9495"]


def test_blank_week_cell_leaves_an_existing_value_alone():
    metric_id = _seed_metric("Closed Revenue", "owner@example.com")
    _seed_entry(metric_id, WEEKS[1], "31200")
    _as_user(_profile("owner@example.com", department="Sales"))

    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Measurable": "Closed Revenue",
                    "values": {WEEKS[0]: 34300},  # WEEKS[1] left blank
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    assert {e.week_start_date.date(): e.value for e in _entries(metric_id)} == {
        WEEKS[0]: "34300",
        WEEKS[1]: "31200",
    }


def test_reimporting_the_same_week_updates_in_place():
    metric_id = _seed_metric("Closed Revenue", "owner@example.com")
    _seed_entry(metric_id, WEEKS[0], "34300")
    _as_user(_profile("owner@example.com", department="Sales"))

    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Measurable": "Closed Revenue",
                    "values": {WEEKS[0]: 35000},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    assert r.json()["entries_updated"] == 1
    assert [(e.value) for e in _entries(metric_id)] == ["35000"]


def test_download_fill_in_upload_is_the_whole_loop():
    """End-to-end on the real user path: download the blank template (with its
    dropdowns and hidden lists sheet), type a row for an existing measurable,
    upload it back. The row matches on name + owner + team, so it updates in
    place instead of adding a duplicate."""
    from openpyxl import load_workbook

    metric_id = _seed_metric(
        "Audit accuracy", "owner@example.com", value_type="percent", target_value="0.98"
    )
    _seed_entry(metric_id, WEEKS[1], "0.9495")
    _as_user(_profile("owner@example.com", department="Sales"))

    downloaded = client.get("/api/eos/scorecard/template?weeks=4")
    assert downloaded.status_code == 200, downloaded.text
    wb = load_workbook(io.BytesIO(downloaded.content))
    ws = wb["Scorecard"]
    header = [c.value for c in ws[1]]

    def col(name: str) -> int:
        return header.index(name) + 1

    this_week = past_sundays(1)[0]
    ws.cell(row=2, column=col("Owner Email"), value="owner@example.com")
    ws.cell(row=2, column=col("Measurable"), value="Audit accuracy")
    ws.cell(row=2, column=col("Team"), value="Sales")
    ws.cell(row=2, column=col("Value Type"), value="Percent")
    # Typed the way the grid is typed: whole percents.
    ws.cell(row=2, column=col(this_week.isoformat()), value=99.5)
    buf = io.BytesIO()
    wb.save(buf)

    r = _post(buf.getvalue())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["metrics_created"] == 0  # matched the existing measurable
    assert body["metrics_created_names"] == []
    by_week = {e.week_start_date.date(): e.value for e in _entries(metric_id)}
    assert by_week[this_week] == "0.995"
    assert by_week[WEEKS[1]] == "0.9495"  # untouched column, unchanged value


def test_a_misspelled_name_is_reported_as_an_addition():
    """With a blank template there's no Metric ID to match on, so a typo makes a
    NEW measurable. The response must name what it added, or the user finds a
    near-duplicate row on the page next week instead."""
    _seed_metric("Closed Revenue", "owner@example.com")
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Owner Email": "owner@example.com",
                    "Measurable": "Closed Revenu",  # typo
                    "Team": "Sales",
                    "values": {WEEKS[0]: 34300},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    assert r.json()["metrics_created_names"] == ["Closed Revenu"]


def test_a_hand_typed_row_updates_the_existing_measurable_instead_of_duplicating():
    metric_id = _seed_metric("Closed Revenue", "owner@example.com")
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Owner Email": "owner@example.com",
                    "Measurable": "Closed Revenue",  # no Metric ID
                    "Team": "Sales",
                    "values": {WEEKS[0]: 34300},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    assert r.json()["metrics_created"] == 0
    assert any("already exists" in w for w in r.json()["warnings"])
    assert len([m for m in _metrics() if m.name == "Closed Revenue"]) == 1
    assert [e.metric_id for e in _entries()] == [metric_id]


# -- endpoint: upload — the access rules -----------------------------------


def test_cannot_import_into_a_measurable_you_cannot_edit():
    """A teammate's measurable is readable on the page but not editable, and an
    import mustn't be the way around that."""
    metric_id = _seed_metric("Teammate metric", "teammate@example.com")
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Measurable": "Teammate metric",
                    "values": {WEEKS[0]: 999},
                }
            ]
        )
    )
    assert r.status_code == 400
    assert "permission" in " ".join(r.json()["detail"]["errors"]).lower()
    assert _entries(metric_id) == []


def test_cannot_import_into_an_exec_measurable_you_cannot_see():
    """The response must not distinguish "confidential" from "deleted" — the
    same reason the by-id read gate answers 404."""
    metric_id = _seed_metric("Exec-only number", "exec@example.com", is_exec=True)
    _as_user(_profile("manager@example.com", department="Sales", managed=["Sales"]))
    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Measurable": "Exec-only number",
                    "values": {WEEKS[0]: 999},
                }
            ]
        )
    )
    assert r.status_code == 400
    errors = " ".join(r.json()["detail"]["errors"])
    assert "no longer exists" in errors and "access" in errors
    assert _entries(metric_id) == []


def test_owner_of_an_exec_measurable_can_still_import_their_own_number():
    """A non-exec department head who reports one number on the confidential
    Leadership Scorecard keeps maintaining it — same carve-out ``can_mutate``
    makes for the owner."""
    metric_id = _seed_metric("Their exec number", "owner@example.com", is_exec=True)
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Measurable": "Their exec number",
                    "values": {WEEKS[0]: 12},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    assert [e.value for e in _entries(metric_id)] == ["12"]


def test_cannot_create_a_measurable_on_a_team_you_arent_on():
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Owner Email": "owner@example.com",
                    "Measurable": "Someone else's metric",
                    "Team": "Compliance",
                    "values": {WEEKS[0]: 1},
                }
            ]
        )
    )
    assert r.status_code == 400
    assert "Team" in " ".join(r.json()["detail"]["errors"])
    assert _metrics() == []


def test_non_exec_cannot_create_an_executive_view_measurable():
    _as_user(_profile("manager@example.com", department="Sales", managed=["Sales"]))
    r = _post(
        _make_workbook(
            [
                {
                    "Owner Email": "manager@example.com",
                    "Measurable": "Company EBITDA",
                    "Team": "Executive View",
                    "values": {WEEKS[0]: 1},
                }
            ]
        )
    )
    assert r.status_code == 400
    assert _metrics() == []


def test_exec_can_create_an_executive_view_measurable():
    _as_user(_profile("exec@example.com", department="Compliance", is_exec=True))
    r = _post(
        _make_workbook(
            [
                {
                    "Owner Email": "exec@example.com",
                    "Measurable": "Company EBITDA",
                    "Team": "Executive View",
                    "values": {WEEKS[0]: 1},
                }
            ]
        )
    )
    assert r.status_code == 201, r.text
    (metric,) = [m for m in _metrics() if m.name == "Company EBITDA"]
    assert (metric.is_exec, metric.department_name) == (True, None)


def test_cannot_assign_a_measurable_to_someone_outside_your_roster():
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(
        _make_workbook(
            [
                {
                    "Owner Email": "outsider@example.com",  # other department
                    "Measurable": "New logos",
                    "Team": "Sales",
                    "values": {WEEKS[0]: 1},
                }
            ]
        )
    )
    assert r.status_code == 400
    assert "outsider@example.com" in " ".join(r.json()["detail"]["errors"])
    assert _metrics() == []


def test_reassigning_an_owner_needs_an_assignable_owner_but_keeping_one_doesnt():
    """The pre-filled owner may sit outside the caller's roster (a
    cross-department measurable), so an unchanged owner must not be rejected —
    only a *change* is checked."""
    metric_id = _seed_metric(
        "Cross-team metric", "outsider@example.com", created_by="owner@example.com"
    )
    _as_user(_profile("owner@example.com", department="Sales"))

    unchanged = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Owner Email": "outsider@example.com",
                    "Measurable": "Cross-team metric",
                    "values": {WEEKS[0]: 7},
                }
            ]
        )
    )
    assert unchanged.status_code == 201, unchanged.text

    changed = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Owner Email": "teammate@example.com",
                    "Measurable": "Cross-team metric",
                }
            ]
        )
    )
    assert changed.status_code == 201, changed.text  # teammate IS assignable

    _as_user(_profile("manager@example.com", department="Sales", managed=["Sales"]))
    to_outsider = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(metric_id),
                    "Owner Email": "outsider@example.com",
                    "Measurable": "Cross-team metric",
                }
            ]
        )
    )
    assert to_outsider.status_code == 400
    assert "outsider@example.com" in " ".join(to_outsider.json()["detail"]["errors"])


def test_one_bad_row_rejects_the_whole_upload():
    """All-or-nothing: a permission error or typo must never leave a
    half-imported scorecard behind."""
    mine = _seed_metric("Closed Revenue", "owner@example.com")
    theirs = _seed_metric("Teammate metric", "teammate@example.com")
    _as_user(_profile("owner@example.com", department="Sales"))

    r = _post(
        _make_workbook(
            [
                {
                    "Metric ID": str(mine),
                    "Measurable": "Closed Revenue",
                    "values": {WEEKS[0]: 34300},
                },
                {
                    "Metric ID": str(theirs),
                    "Measurable": "Teammate metric",
                    "values": {WEEKS[0]: 999},
                },
            ]
        )
    )
    assert r.status_code == 400
    assert len(r.json()["detail"]["errors"]) == 1
    # The *good* row didn't land either.
    assert _entries() == []


def test_duplicate_rows_for_the_same_measurable_are_rejected():
    metric_id = _seed_metric("Closed Revenue", "owner@example.com")
    _as_user(_profile("owner@example.com", department="Sales"))
    row = {
        "Metric ID": str(metric_id),
        "Measurable": "Closed Revenue",
        "values": {WEEKS[0]: 1},
    }
    r = _post(_make_workbook([row, dict(row, values={WEEKS[0]: 2})]))
    assert r.status_code == 400
    assert "one row per measurable" in " ".join(r.json()["detail"]["errors"]).lower()
    assert _entries() == []


def test_upload_rejects_a_non_xlsx_file():
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(b"not a spreadsheet")
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)


def test_a_new_row_without_an_owner_is_reported_not_guessed():
    _as_user(_profile("owner@example.com", department="Sales"))
    r = _post(_make_workbook([{"Measurable": "New logos", "Team": "Sales"}]))
    assert r.status_code == 400
    assert "Owner Email" in " ".join(r.json()["detail"]["errors"])
    assert _metrics() == []
