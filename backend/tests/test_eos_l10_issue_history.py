"""The IDS step's solved-issue lookback.

Emery ran a Marketing L10, the team solved issues in the room, and the IDS
tile still read "Solved this week: 0". ``get_meeting_items`` returns only
OPEN issues — deliberately, the IDS list is today's work — so a solved
issue left the client on the next poll and nothing in the meeting could
count it or show it. ``GET /meetings/{id}/issues/history`` is the missing
half: the team's solved issues over a trailing window, the Issues twin of
the Headlines step's ``/inputs/history``.

Pinned here: the window boundary, that ``/items`` stays open-only (this
endpoint's whole reason to exist), team scoping, and the NULL-``solved_at``
legacy row.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_l10_issue_history.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _as(email: str, department: str) -> None:
    now = "2026-09-17T00:00:00"
    app.dependency_overrides[get_current_user] = lambda: UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=["user"],
        department=department,
        is_exec=False,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


def _seed() -> dict:
    """One Marketing L10 plus issues at every interesting age / status."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Issue, IssueStatus, L10Meeting
    from rubik_core.database.models import Department

    db = SessionLocal()
    ids: dict = {}
    try:
        db.add_all([Department(name="Marketing"), Department(name="Sales")])
        meeting = L10Meeting(
            name="Marketing L10", department_name="Marketing", created_by="seed"
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        ids["meeting"] = str(meeting.id)

        now = datetime.utcnow()

        def _issue(title, *, dept="Marketing", status=IssueStatus.SOLVED, solved=None):
            return Issue(
                title=title,
                description=title,
                department_name=dept,
                status=status,
                solved_at=solved,
                created_by="seed",
            )

        rows = {
            # Solved in the room this week — what the tile was missing.
            "this_week": _issue("TMC booth follow-up", solved=now - timedelta(days=2)),
            "last_month": _issue("Webinar promo plan", solved=now - timedelta(days=45)),
            "last_quarter": _issue("Rebrand deck", solved=now - timedelta(days=200)),
            "still_open": _issue("Proposals process", status=IssueStatus.OPEN),
            # Another team's solved issue must never surface in this meeting.
            "other_team": _issue(
                "Pipeline hygiene", dept="Sales", solved=now - timedelta(days=2)
            ),
            # Pre-`solved_at` legacy row: solved, but with no date to window by.
            "no_stamp": _issue("Ancient solved issue", solved=None),
        }
        db.add_all(list(rows.values()))
        db.commit()
        for key, row in rows.items():
            db.refresh(row)
            ids[key] = str(row.id)
        return ids
    finally:
        db.close()


def _history(meeting_id: str, days: int | None = None):
    qs = "" if days is None else f"?days={days}"
    res = client.get(f"/api/eos/l10/meetings/{meeting_id}/issues/history{qs}")
    assert res.status_code == 200, res.text
    return res.json()


def test_window_bounds_what_the_meeting_sees():
    ids = _seed()
    _as("shay@acme.example", "Marketing")

    titles_30 = [i["title"] for i in _history(ids["meeting"], 30)]
    assert titles_30 == ["TMC booth follow-up"]

    # 60 and 90 both reach the 45-day-old solve; newest-solved first.
    assert [i["title"] for i in _history(ids["meeting"], 60)] == [
        "TMC booth follow-up",
        "Webinar promo plan",
    ]
    assert [i["title"] for i in _history(ids["meeting"], 90)] == [
        "TMC booth follow-up",
        "Webinar promo plan",
    ]

    # Default window is the widest the drawer offers.
    assert _history(ids["meeting"]) == _history(ids["meeting"], 90)


def test_excludes_open_other_teams_and_undated_rows():
    ids = _seed()
    _as("shay@acme.example", "Marketing")

    returned = {i["id"] for i in _history(ids["meeting"], 365)}
    assert ids["this_week"] in returned
    assert ids["last_quarter"] in returned  # inside a year
    assert ids["still_open"] not in returned  # open issues stay on the IDS list
    assert ids["other_team"] not in returned  # team scoping
    assert ids["no_stamp"] not in returned  # no solve date to window by


def test_a_bad_days_value_falls_back_to_the_default_window():
    ids = _seed()
    _as("shay@acme.example", "Marketing")

    # 0 / negative → the 90-day default rather than an empty or unbounded list.
    assert _history(ids["meeting"], 0) == _history(ids["meeting"], 90)
    assert _history(ids["meeting"], -5) == _history(ids["meeting"], 90)
    # Capped at a year, so a huge value can't drag the whole archive into a
    # live meeting's payload.
    assert len(_history(ids["meeting"], 100000)) == 3


def test_items_stays_open_only():
    """The contract the history endpoint exists to complement. If /items ever
    starts returning solved issues, the meeting would double-count them."""
    ids = _seed()
    _as("shay@acme.example", "Marketing")

    res = client.get(f"/api/eos/l10/meetings/{ids['meeting']}/items")
    assert res.status_code == 200, res.text
    statuses = {i["status"] for i in res.json()["issues"]}
    assert statuses == {"open"}
