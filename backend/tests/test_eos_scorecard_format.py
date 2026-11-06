"""Leadership Scorecard format (zc22) and loading an Executive-team scorecard.

Covers the bits added to carry a leadership scorecard spreadsheet's format:
  - ``value_type`` + ``comparator`` round-trip through create / GET / grid.
  - exec-scope metrics (``is_exec=True``, NULL department) stay confidential —
    a regular IC can't read them, the Exec team can.
  - a fictional leadership scorecard loads every measurable × its weekly
    values, and loading the weeks again updates them in place.

Temp SQLite per test; no mocks.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers.eos import _authz
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_scorecard_format.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _profile(email, *, department="general", roles=None, is_exec=False, managed=None):
    now = "2026-06-29T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at=now,
        updated_at=now,
    )


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _make_owner(email="alex@x.example"):
    from rubik_core.database.eos import Person

    db = _session()
    try:
        p = Person(first_name="Alex", last_name="Rivera", email=email, created_by="t")
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


# ---- format fields round-trip --------------------------------------------


def test_value_type_and_comparator_round_trip():
    owner_id = _make_owner()
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "admin@x.example", roles=["admin"]
    )

    r = client.post(
        "/api/eos/scorecard/metrics",
        json={
            "owner_id": str(owner_id),
            "name": "Client Retention",
            "goal_description": "> 98%",
            "target_value": "0.98",
            "unit": "%",
            "value_type": "percent",
            "comparator": ">",
            "is_exec": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["value_type"] == "percent"
    assert body["comparator"] == ">"
    assert body["is_exec"] is True

    # GET reflects the stored format fields.
    got = client.get("/api/eos/scorecard/metrics").json()
    assert got[0]["value_type"] == "percent"
    assert got[0]["comparator"] == ">"


def test_value_type_defaults_to_number():
    owner_id = _make_owner()
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "admin@x.example", roles=["admin"]
    )
    r = client.post(
        "/api/eos/scorecard/metrics",
        json={"owner_id": str(owner_id), "name": "Headcount"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["value_type"] == "number"
    assert r.json()["comparator"] is None


def test_grid_includes_format_fields():
    owner_id = _make_owner()
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "admin@x.example", roles=["admin"]
    )
    mid = client.post(
        "/api/eos/scorecard/metrics",
        json={
            "owner_id": str(owner_id),
            "name": "Weekly Collected Revenue",
            "goal_description": ">158,000 usd",
            "target_value": "158000",
            "unit": "usd",
            "value_type": "currency",
            "comparator": ">",
            "is_exec": True,
        },
    ).json()["id"]
    client.post(
        "/api/eos/scorecard/entries",
        json={
            "metric_id": mid,
            "week_start_date": "2026-06-14T00:00:00",
            "value": "139032.46",
        },
    )
    grid = client.get("/api/eos/scorecard/grid", params={"is_exec": True}).json()
    m = next(x for x in grid["metrics"] if x["id"] == mid)
    assert m["value_type"] == "currency"
    assert m["comparator"] == ">"
    assert m["goal_description"] == ">158,000 usd"
    assert m["is_exec"] is True


# ---- confidentiality -------------------------------------------------------


def test_exec_scorecard_hidden_from_regular_user_visible_to_exec():
    owner_id = _make_owner()
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "admin@x.example", roles=["admin"]
    )
    mid = client.post(
        "/api/eos/scorecard/metrics",
        json={
            "owner_id": str(owner_id),
            "name": "Loss Revenue",
            "value_type": "currency",
            "comparator": "<",
            "target_value": "191000",
            "is_exec": True,
        },
    ).json()["id"]

    db = _session()
    try:
        # A regular IC in another department: cannot see the exec metric.
        ic = _profile("ic@x.example", department="Marketing")
        vis = _authz.visible_measurable_ids(ic, db)
        assert vis is not None
        assert __import__("uuid").UUID(mid) not in set(vis)

        # An Exec-team member: can.
        execu = _profile("exec@x.example", is_exec=True)
        evis = _authz.visible_measurable_ids(execu, db)
        # exec without managed='*' is restricted-but-sees-exec-rows
        assert evis is None or __import__("uuid").UUID(mid) in set(evis)
    finally:
        db.close()

    # Endpoint-level: the regular user's list excludes it.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "ic@x.example", department="Marketing"
    )
    listed = {m["id"] for m in client.get("/api/eos/scorecard/metrics").json()}
    assert mid not in listed


# ---- loading a leadership scorecard ----------------------------------------

#: A fictional leadership scorecard: one measurable per value type, each with
#: three weekly values. Percent values and targets are stored as fractions.
LEADERSHIP_SCORECARD = (
    {
        "name": "Client Retention",
        "value_type": "percent",
        "comparator": ">",
        "target_value": "0.98",
        "weeks": {"2026-06-07": "0.97", "2026-06-14": "0.99", "2026-06-21": "0.985"},
    },
    {
        "name": "Weekly Collected Revenue",
        "value_type": "currency",
        "comparator": ">",
        "target_value": "150000",
        "weeks": {
            "2026-06-07": "142000.50",
            "2026-06-14": "151200",
            "2026-06-21": "149875.25",
        },
    },
    {
        "name": "New Discovery Calls",
        "value_type": "number",
        "comparator": ">=",
        "target_value": "12",
        "weeks": {"2026-06-07": "10", "2026-06-14": "14", "2026-06-21": "12"},
    },
)


def _load_weeks(metric_ids: dict[str, str], *, bump: str = "") -> None:
    """Record every weekly value through the idempotent upsert."""
    for measurable in LEADERSHIP_SCORECARD:
        for week, value in measurable["weeks"].items():
            r = client.post(
                "/api/eos/scorecard/entries/upsert",
                json={
                    "metric_id": metric_ids[measurable["name"]],
                    "week_start_date": f"{week}T00:00:00",
                    "value": value + bump,
                },
            )
            assert r.status_code == 200, r.text


def test_a_leadership_scorecard_loads_and_reloads_without_duplicates():
    from rubik_core.database.eos import ScorecardEntry, ScorecardMetric

    owner_id = _make_owner()
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "admin@x.example", roles=["admin"]
    )
    metric_ids = {}
    for measurable in LEADERSHIP_SCORECARD:
        r = client.post(
            "/api/eos/scorecard/metrics",
            json={
                "owner_id": str(owner_id),
                "name": measurable["name"],
                "value_type": measurable["value_type"],
                "comparator": measurable["comparator"],
                "target_value": measurable["target_value"],
                "is_exec": True,
            },
        )
        assert r.status_code == 201, r.text
        metric_ids[measurable["name"]] = r.json()["id"]
    total_weeks = sum(len(m["weeks"]) for m in LEADERSHIP_SCORECARD)

    _load_weeks(metric_ids)
    _load_weeks(metric_ids, bump="1")  # a corrected re-load of the same weeks

    db = _session()
    try:
        metrics = (
            db.query(ScorecardMetric)
            .filter(ScorecardMetric.is_deleted.is_(False))
            .all()
        )
        assert len(metrics) == len(LEADERSHIP_SCORECARD)
        assert all(m.is_exec is True for m in metrics)
        assert all(m.department_name is None for m in metrics)
        retention = next(m for m in metrics if m.name == "Client Retention")
        assert retention.value_type == "percent"
        assert retention.comparator == ">"
        assert retention.target_value == "0.98"

        entries = (
            db.query(ScorecardEntry).filter(ScorecardEntry.is_deleted.is_(False)).all()
        )
        assert len(entries) == total_weeks
        calls_id = metric_ids["New Discovery Calls"]
        reloaded = {e.value for e in entries if str(e.metric_id) == calls_id}
        assert reloaded == {"101", "141", "121"}
    finally:
        db.close()
