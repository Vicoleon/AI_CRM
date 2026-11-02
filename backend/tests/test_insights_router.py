"""Insights HTTP API: gates, scope, contract shapes, privacy (README §1, §4).

What this file pins:

1. **WHETHER, then WHO, then HOW MUCH.** ``INSIGHTS_ENABLED`` off answers 404
   everywhere (and ``/users/me`` reports both keys false); a caller outside
   the ``insights`` roster gets 403; a ``dept`` outside the viewer's scope is
   403; a person outside it is 404.
2. **People need their own key.** Without ``insights_people`` the person page
   is 403 and every people table is silently left out of the dashboards.
3. **Every Phase-1 dashboard returns the contract shape** on seeded rows.
4. **Counts only.** A sentinel written into every free-text column the seed
   touches (questions, answers, titles, card text) never appears in any
   response.
5. **A person read is audited** as ``INSIGHTS_PERSON_VIEW``.

The data is ``scripts/seed_insights_demo.py``'s, on a throwaway SQLite file,
with the Workspace domain set to ``company.com`` so nothing here leans on the
real domain. Outside calls are patched by STRING target.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy, clear_cache, save_policy

TODAY = date(2026, 10, 8)
DOMAIN = "company.com"
SENTINEL = "SENTINEL-CONTENT-7f3a"
BASE = "/api/insights"
TODAY_TARGET = "rubik_core.insights.filters.today_local"
KILL_SWITCH = "backend.routers.insights.settings.INSIGHTS_ENABLED"

PHASE1 = ("overview", "adoption", "ask-cubie", "priorities")
PHASE2 = ("follow-through", "crm", "proposals")
PHASE3 = ("ai-spend", "knowledge", "quality")
SECTION_KINDS = {"timeseries", "bar", "funnel", "matrix", "table"}
UNITS = {"count", "pct", "usd", "ms", "days", "ratio", "score"}


def email(first: str, last: str) -> str:
    return f"{first}.{last}@{DOMAIN}".lower()


def profile(
    addr: str,
    *,
    department: str = "general",
    roles: list[str] | None = None,
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-10-08T00:00:00"
    return UserProfile(
        id=addr.split("@")[0],
        email=addr,
        name=addr.split("@")[0].replace(".", " ").title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at=now,
        updated_at=now,
    )


ADMIN = profile(
    email("Noel", "Vance"), department="Engineering", roles=["admin", "user"]
)
CEO = profile(
    email("Yael", "Hart"), department="Operations", is_exec=True, managed=["*"]
)
SALES_EXEC = profile(
    email("Avery", "Stone"), department="Sales", is_exec=True, managed=["Sales"]
)
MARKETER = profile(email("Indy", "Moss"), department="Marketing")


def load_seed_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_insights_demo.py"
    spec = importlib.util.spec_from_file_location("seed_insights_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def insights_db(tmp_path, monkeypatch, set_test_workspace_domains):
    """A throwaway SQLite file with the demo rows, the app's SessionLocal bound
    to it, today pinned, and every cache empty before and after."""
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database.models import Base
    from rubik_core.insights.cache import clear_cache as clear_insights_cache

    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'insights.db'}"
    )
    set_test_workspace_domains(DOMAIN)
    monkeypatch.setattr(settings, "INSIGHTS_ENABLED", True)
    from rubik_core.company.operations import OperationsConfig
    from rubik_core.insights import population

    monkeypatch.setattr(population, "get_operations_config", OperationsConfig)
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    clear_cache()
    clear_insights_cache()
    seed = load_seed_module()
    with dbmod.SessionLocal() as db:
        seed.seed(db, domain=DOMAIN, today=TODAY, sentinel=SENTINEL)
    with patch(TODAY_TARGET, return_value=TODAY):
        try:
            yield dbmod.SessionLocal
        finally:
            # The usage middleware buffered these test requests; drop them
            # rather than let the exit-time flush write them to another DB.
            from rubik_core.insights.activity import reset_buffer

            reset_buffer()
            clear_cache()
            clear_insights_cache()
            app.dependency_overrides.clear()
            engine.dispose()
            dbmod._engine = None


@pytest.fixture
def as_user():
    def _apply(user: UserProfile) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)

    yield _apply
    app.dependency_overrides.clear()


def people_view_for_admins_only(session_factory) -> None:
    with session_factory() as db:
        save_policy(
            db,
            "insights_people",
            FeaturePolicy(exec_allowed=False, admin_allowed=True),
            actor_email=ADMIN.email,
        )


# ── 1. gates ─────────────────────────────────────────────────────────────────


def test_kill_switch_answers_404_everywhere(as_user):
    client = as_user(ADMIN)
    with patch(KILL_SWITCH, False):
        assert client.get(f"{BASE}/meta").status_code == 404
        assert client.get(f"{BASE}/dashboards/overview").status_code == 404
        assert client.get(f"{BASE}/people/{SALES_EXEC.email}").status_code == 404
        assert client.post(f"{BASE}/dashboards/overview/summary").status_code == 404


def test_users_me_reports_both_keys_off_with_the_kill_switch(as_user, monkeypatch):
    from rubik_core.config import settings

    client = as_user(ADMIN)
    features = client.get("/api/users/me").json()["features"]
    assert features["insights"] is True and features["insights_people"] is True
    monkeypatch.setattr(settings, "INSIGHTS_ENABLED", False)
    features = client.get("/api/users/me").json()["features"]
    assert features["insights"] is False and features["insights_people"] is False


def test_outside_the_roster_is_403(as_user):
    client = as_user(MARKETER)
    for path in ("/meta", "/dashboards/adoption", f"/people/{ADMIN.email}"):
        r = client.get(BASE + path)
        assert r.status_code == 403, path
        assert "not switched on" in r.json()["detail"]


def test_unknown_and_unshipped_dashboards_are_404(as_user):
    from rubik_core.insights.dashboards import DASHBOARDS

    client = as_user(ADMIN)
    assert client.get(f"{BASE}/dashboards/nope").status_code == 404
    for d in DASHBOARDS:
        if not d.available:
            assert client.get(f"{BASE}/dashboards/{d.slug}").status_code == 404
            assert client.post(f"{BASE}/dashboards/{d.slug}/summary").status_code == 404


@pytest.mark.parametrize(
    "query",
    [
        "range=forever",
        "range=custom",
        "range=custom&start=2026-01-01&end=bad",
        "range=custom&start=2026-09-10&end=2026-09-01",
        "range=custom&start=2025-01-01&end=2026-10-01",
        "range=custom&start=0001-01-01&end=0001-01-10",
        "range=custom&start=9999-12-01&end=9999-12-31",
        "range=custom&start=1999-12-25&end=2000-01-05",
    ],
)
def test_bad_filters_are_400_with_a_sentence(as_user, query):
    r = as_user(ADMIN).get(f"{BASE}/dashboards/adoption?{query}")
    assert r.status_code == 400
    assert r.json()["detail"].endswith(".")


# ── 2. scope ─────────────────────────────────────────────────────────────────


def test_meta_lists_every_dashboard_the_catalog_and_the_viewers_departments(as_user):
    body = as_user(ADMIN).get(f"{BASE}/meta").json()
    assert body["all_departments"] is True
    assert body["can_view_people"] is True
    assert body["tz"] == "America/Chicago"
    assert body["ranges"] == ["7d", "30d", "90d", "qtd", "ytd", "custom"]
    slugs = [d["slug"] for d in body["dashboards"]]
    assert slugs == [
        "overview",
        "adoption",
        "ask-cubie",
        "priorities",
        "follow-through",
        "crm",
        "proposals",
        "ai-spend",
        "knowledge",
        "quality",
    ]
    assert [d["slug"] for d in body["dashboards"] if d["available"]] == list(
        PHASE1 + PHASE2 + PHASE3
    )
    depts = {d["slug"]: d for d in body["departments"]}
    assert set(depts) == {"sales", "marketing", "engineering", "operations"}
    assert depts["sales"] == {"slug": "sales", "label": "Sales", "people": 8}
    m = body["metrics"]["active_rate"]
    assert m["unit"] == "pct" and m["denominator"] and m["good_direction"] == "up"


def test_a_scoped_exec_sees_only_their_departments(as_user):
    client = as_user(SALES_EXEC)
    body = client.get(f"{BASE}/meta").json()
    assert body["all_departments"] is False
    assert [d["slug"] for d in body["departments"]] == ["sales"]
    payload = client.get(f"{BASE}/dashboards/adoption").json()
    assert payload["scope"] == {
        "departments": ["sales"],
        "labels": ["Sales"],
        "all": False,
        "people": 8,
    }
    r = client.get(f"{BASE}/dashboards/adoption?dept=sales,marketing")
    assert r.status_code == 403
    assert "marketing" in r.json()["detail"]
    assert client.get(f"{BASE}/dashboards/adoption?dept=Sales").status_code == 200


def test_an_all_departments_viewer_can_filter_to_any_department(as_user):
    for viewer in (ADMIN, CEO):
        payload = (
            as_user(viewer).get(f"{BASE}/dashboards/adoption?dept=marketing").json()
        )
        assert payload["scope"]["departments"] == ["marketing"]
        assert payload["scope"]["all"] is False
        assert payload["scope"]["people"] == 5
    whole = as_user(ADMIN).get(f"{BASE}/dashboards/adoption").json()
    assert whole["scope"]["all"] is True and whole["scope"]["people"] == 25


def test_a_person_outside_scope_is_404_and_inside_is_200(as_user):
    client = as_user(SALES_EXEC)
    assert client.get(f"{BASE}/people/{MARKETER.email}").status_code == 404
    assert client.get(f"{BASE}/people/nobody@{DOMAIN}").status_code == 404
    ok = client.get(f"{BASE}/people/{email('Blake', 'Rivera').upper()}")
    assert ok.status_code == 200
    assert ok.json()["person"]["email"] == email("Blake", "Rivera")


def test_excluded_emails_drop_out_of_every_count(as_user, monkeypatch):
    from rubik_core.company.operations import OperationsConfig
    from rubik_core.insights import population

    monkeypatch.setattr(
        population,
        "get_operations_config",
        lambda: OperationsConfig(
            insights_excluded_emails=(email("Blake", "Rivera").upper(),)
        ),
    )
    payload = as_user(ADMIN).get(f"{BASE}/dashboards/adoption?refresh=1").json()
    assert payload["scope"]["people"] == 24
    people = next(s for s in payload["sections"] if s["id"] == "people")
    assert email("Blake", "Rivera") not in {r["key"] for r in people["data"]["rows"]}
    assert (
        as_user(ADMIN).get(f"{BASE}/people/{email('Blake', 'Rivera')}").status_code
        == 404
    )


# ── 3. contract shapes ───────────────────────────────────────────────────────


def _assert_kpi(k: dict) -> None:
    assert set(k) >= {"id", "value", "prev_value", "spark"}
    assert isinstance(k["spark"], list)


def _assert_section(s: dict) -> None:
    assert s["kind"] in SECTION_KINDS
    assert s["width"] in ("full", "half", "third")
    assert s["drill"] in ("department", "person", None)
    assert s["title"] and s["id"]
    data = s["data"]
    if s["kind"] == "timeseries":
        assert data["chart"] in ("line", "bar") and isinstance(data["stacked"], bool)
        for series in data["series"]:
            assert len(series["values"]) == len(data["x"])
            assert {"key", "label", "unit", "values"} <= set(series)
    elif s["kind"] == "bar":
        assert "unit" in data
        for item in data["items"]:
            assert {"key", "label", "value"} <= set(item)
            if data["unit"] == "pct" and item["value"] is not None:
                assert "numerator" in item and "denominator" in item
    elif s["kind"] == "funnel":
        assert data["steps"] and all(
            {"key", "label", "value"} <= set(st) for st in data["steps"]
        )
    elif s["kind"] == "matrix":
        assert data["unit"] in ("pct", "count") and data["scale"] in (
            "sequential",
            "diverging",
        )
        assert len(data["cells"]) == len(data["rows"])
        for row in data["cells"]:
            assert len(row) == len(data["cols"])
            for c in row:
                assert c is None or "value" in c
    else:
        assert s["drill"] == "person" and data["row_link"] == "person"
        keys = {c["key"] for c in data["columns"]}
        for row in data["rows"]:
            assert set(row["cells"]) <= keys
            assert "@" in row["key"]


@pytest.mark.parametrize("slug", PHASE1)
@pytest.mark.parametrize("range_", ["7d", "30d", "90d", "ytd"])
def test_every_dashboard_returns_the_contract_shape(as_user, slug, range_):
    r = as_user(ADMIN).get(f"{BASE}/dashboards/{slug}?range={range_}")
    assert r.status_code == 200, r.text
    p = r.json()
    assert set(p) == {
        "dashboard",
        "title",
        "generated_at",
        "cached",
        "period",
        "scope",
        "kpis",
        "sections",
        "notes",
    }
    assert p["dashboard"] == slug and p["cached"] is False
    assert p["generated_at"].endswith("Z")
    assert set(p["period"]) == {
        "start",
        "end",
        "prev_start",
        "prev_end",
        "days",
        "bucket",
        "tz",
        "label",
    }
    assert p["period"]["end"] == TODAY.isoformat()
    assert p["period"]["bucket"] == ("day" if range_ in ("7d", "30d") else "week")
    assert p["kpis"] and p["sections"]
    for k in p["kpis"]:
        _assert_kpi(k)
    n_buckets = {"7d": 7, "30d": 30}.get(range_)
    if n_buckets:
        assert all(len(k["spark"]) == n_buckets for k in p["kpis"])
    for s in p["sections"]:
        _assert_section(s)


def test_the_numbers_are_the_seeded_ones(as_user):
    """Spot-check a few values that follow directly from the seed."""
    p = as_user(ADMIN).get(f"{BASE}/dashboards/adoption").json()
    kpis = {k["id"]: k for k in p["kpis"]}
    # Two of the 25 never use Cubie; one stopped three weeks ago (dormant).
    assert kpis["active_rate"]["denominator"] == 25
    assert kpis["active_people"]["value"] <= 23
    assert kpis["dormant_people"]["value"] >= 3
    dormant = next(s for s in p["sections"] if s["id"] == "dormant")
    keys = {r["key"] for r in dormant["data"]["rows"]}
    assert {
        email("Gray", "Holt"),
        email("Wren", "Ford"),
        email("Finley", "Ward"),
    } <= keys
    prio = as_user(ADMIN).get(f"{BASE}/dashboards/priorities").json()
    funnel = next(s for s in prio["sections"] if s["kind"] == "funnel")["data"]["steps"]
    assert funnel[0]["key"] == "surfaced"
    assert funnel[0]["value"] == sum(st["value"] for st in funnel[1:])
    assert any("tracked from Sep 18, 2026" in n for n in prio["notes"])


def test_the_departed_persons_history_counts_but_not_in_rates(as_user):
    """Rowan left 15 days ago; their questions still count in the 30-day
    volume, but the active rate's denominator is today's people only."""
    p = as_user(ADMIN).get(f"{BASE}/dashboards/ask-cubie?dept=sales").json()
    people = next(s for s in p["sections"] if s["id"] == "people")
    assert email("Rowan", "Ex") not in {r["key"] for r in people["data"]["rows"]}
    a = as_user(ADMIN).get(f"{BASE}/dashboards/adoption?dept=sales").json()
    rate = next(k for k in a["kpis"] if k["id"] == "active_rate")
    assert rate["denominator"] == 8
    profile_ = as_user(ADMIN).get(f"{BASE}/people/{email('Rowan', 'Ex')}").json()
    assert profile_["person"]["department"] == "sales"
    assert not any(f["has_access"] for f in profile_["features"])


def test_the_cache_serves_repeats_and_refresh_bypasses_it(as_user):
    client = as_user(ADMIN)
    first = client.get(f"{BASE}/dashboards/overview").json()
    second = client.get(f"{BASE}/dashboards/overview").json()
    assert first["cached"] is False and second["cached"] is True
    assert second["generated_at"] == first["generated_at"]
    # Another viewer never gets this viewer's cached payload.
    assert as_user(CEO).get(f"{BASE}/dashboards/overview").json()["cached"] is False


def test_refresh_is_throttled_for_a_fresh_entry(as_user, monkeypatch):
    """refresh=1 rebuilds, but not an entry younger than 30 seconds: a held
    Refresh button must not keep the database rebuilding the same numbers."""
    client = as_user(ADMIN)
    client.get(f"{BASE}/dashboards/overview")
    again = client.get(f"{BASE}/dashboards/overview?refresh=1").json()
    assert again["cached"] is True
    monkeypatch.setattr("rubik_core.insights.cache.MIN_REFRESH_AGE_SECONDS", 0)
    assert client.get(f"{BASE}/dashboards/overview?refresh=1").json()["cached"] is False


def test_the_activity_buffer_is_flushed_once_per_build_outside_the_session(as_user):
    """One flush per build, before the build session opens (a flush inside a
    load would check a second connection out of the pool); a cached answer
    and a cached person page flush nothing."""
    from unittest.mock import MagicMock

    flush = MagicMock(return_value=0)
    client = as_user(ADMIN)
    with patch("rubik_core.insights.activity.flush_activity", flush):
        client.get(f"{BASE}/dashboards/adoption")
        assert flush.call_count == 1
        client.get(f"{BASE}/dashboards/adoption")
        assert flush.call_count == 1
        target = email("Blake", "Rivera")
        assert client.get(f"{BASE}/people/{target}").status_code == 200
        assert client.get(f"{BASE}/people/{target}").status_code == 200
        assert flush.call_count == 2


# ── 4. people and privacy ────────────────────────────────────────────────────


def test_people_tables_are_left_out_without_the_people_view(as_user, insights_db):
    people_view_for_admins_only(insights_db)
    client = as_user(CEO)
    assert client.get(f"{BASE}/meta").json()["can_view_people"] is False
    for slug in PHASE1 + PHASE2 + PHASE3:
        p = client.get(f"{BASE}/dashboards/{slug}").json()
        # No table that names people (Quality's scorecard lists measures).
        assert not [
            s
            for s in p["sections"]
            if s["kind"] == "table" and s["data"]["row_link"] == "person"
        ], slug
        assert not [s for s in p["sections"] if s["drill"] == "person"], slug
    r = client.get(f"{BASE}/people/{SALES_EXEC.email}")
    assert r.status_code == 403
    # The admin still has both.
    p = as_user(ADMIN).get(f"{BASE}/dashboards/adoption").json()
    assert [s for s in p["sections"] if s["kind"] == "table"]


def test_a_person_read_is_audited_with_the_contract_shape(as_user, insights_db):
    from rubik_core.database.models import AuditLog

    target = email("Blake", "Rivera")
    r = as_user(ADMIN).get(f"{BASE}/people/{target}?range=7d")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "person",
        "period",
        "calendar",
        "kpis",
        "cards",
        "features",
        "notes",
    }
    assert set(body["person"]) == {
        "email",
        "name",
        "department",
        "department_label",
        "last_active",
    }
    assert body["person"]["department_label"] == "Sales"
    assert len(body["calendar"]) == 90
    assert body["calendar"][-1]["date"] == TODAY.isoformat()
    assert [c["dashboard"] for c in body["cards"]] == [
        "adoption",
        "ask-cubie",
        "priorities",
        "follow-through",
        "crm",
        "proposals",
        "ai-spend",
        "knowledge",
    ]
    assert all(len(k["spark"]) == 7 for c in body["cards"] for k in c["kpis"])
    for f in body["features"]:
        assert set(f) == {"key", "label", "has_access", "used"}
    with insights_db() as db:
        rows = (
            db.query(AuditLog).filter(AuditLog.action == "INSIGHTS_PERSON_VIEW").all()
        )
    assert len(rows) == 1
    assert rows[0].actor_email == ADMIN.email and rows[0].doc_id == target


def test_call_capture_and_smart_teams_never_appear_per_person(as_user):
    body = as_user(ADMIN).get(f"{BASE}/people/{email('Avery', 'Stone')}").json()
    keys = {f["key"] for f in body["features"]}
    assert not keys & {"sales_call_capture", "sales_meet_addon", "smart_teams"}
    p = as_user(ADMIN).get(f"{BASE}/dashboards/adoption").json()
    people = next(s for s in p["sections"] if s["id"] == "people")
    assert all("call" not in c["key"] for c in people["data"]["columns"])


def test_no_response_ever_carries_seeded_content(as_user):
    """The seed writes a sentinel into every question, answer, title, card and
    rating text. None of it may come back, from any endpoint."""
    client = as_user(ADMIN)
    bodies = [client.get(f"{BASE}/meta").text]
    for slug in PHASE1 + PHASE2 + PHASE3:
        for range_ in ("7d", "90d"):
            bodies.append(client.get(f"{BASE}/dashboards/{slug}?range={range_}").text)
    for who in (email("Avery", "Stone"), email("Noel", "Vance"), email("Rowan", "Ex")):
        bodies.append(client.get(f"{BASE}/people/{who}").text)
    for text in bodies:
        assert SENTINEL not in text
        json.loads(text)  # and every body is JSON


# ── 5. hardening ─────────────────────────────────────────────────────────────


def _add(session_factory, *rows) -> None:
    with session_factory() as db:
        db.add_all(rows)
        db.commit()


def _utc(day: date, hour: int = 15):
    from datetime import timedelta

    from rubik_core.insights.filters import local_midnight_utc

    return local_midnight_utc(day) + timedelta(hours=hour)


def test_smart_teams_actions_never_count_as_activity(as_user, insights_db):
    """Gray never uses Cubie. A Smart Teams upload in the audit log is their
    own advisory record, not a use of Cubie, so they stay never-active."""
    from rubik_core.database.models import AuditLog

    gray = email("Gray", "Holt")
    _add(
        insights_db,
        AuditLog(
            actor_email=gray,
            action="SMART_TEAMS_UPLOAD",
            status="success",
            timestamp=_utc(TODAY),
        ),
        AuditLog(
            actor_email=gray,
            action="SMART_TEAMS_PROFILE_VIEW",
            status="success",
            timestamp=_utc(TODAY),
        ),
    )
    client = as_user(ADMIN)
    body = client.get(f"{BASE}/people/{gray}").json()
    assert body["person"]["last_active"] is None
    assert sum(d["count"] for d in body["calendar"]) == 0
    adoption = client.get(f"{BASE}/dashboards/adoption").json()
    dormant = next(s for s in adoption["sections"] if s["id"] == "dormant")
    assert gray in {r["key"] for r in dormant["data"]["rows"]}


def test_call_capture_never_reaches_a_person_but_counts_for_the_department(
    as_user, insights_db, monkeypatch
):
    """Call Capture use is department-level only (Google Meet's terms). Gray's
    only activity here is call capture: their page, the people tables and
    "active" all ignore it, while the department matrix still counts it."""
    from rubik_core.config import settings
    from rubik_core.database.insights import ActivityDaily
    from rubik_core.database.models import AuditLog

    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    gray = email("Gray", "Holt")
    _add(
        insights_db,
        ActivityDaily(
            user_email=gray,
            day=TODAY,
            area="call_capture",
            requests=9,
            first_at=_utc(TODAY),
            last_at=_utc(TODAY),
        ),
        AuditLog(
            actor_email=gray,
            action="CALL_CAPTURE_STARTED",
            status="success",
            timestamp=_utc(TODAY),
        ),
    )
    with insights_db() as db:
        save_policy(
            db,
            "sales_call_capture",
            FeaturePolicy(
                departments=frozenset({"sales"}),
                admin_allowed=False,
                exec_allowed=False,
            ),
            actor_email=ADMIN.email,
        )
    client = as_user(ADMIN)
    body = client.get(f"{BASE}/people/{gray}").json()
    assert body["person"]["last_active"] is None
    assert sum(d["count"] for d in body["calendar"]) == 0
    assert next(k for k in body["kpis"] if k["id"] == "active_days")["value"] == 0
    adoption = client.get(f"{BASE}/dashboards/adoption").json()
    people = next(s for s in adoption["sections"] if s["id"] == "people")
    row = next(r for r in people["data"]["rows"] if r["key"] == gray)
    assert row["cells"]["active_days"] == 0 and row["cells"]["last_active"] is None
    matrix = next(s for s in adoption["sections"] if s["id"] == "feature_adoption")[
        "data"
    ]
    col = [c["key"] for c in matrix["cols"]].index("call_capture")
    sales = [r["key"] for r in matrix["rows"]].index("sales")
    cell = matrix["cells"][sales][col]
    assert (
        cell["value"] is not None
        and cell["numerator"] >= 1
        and cell["denominator"] == 8
    )


def test_call_capture_over_fewer_than_3_people_is_hidden_even_from_admins(
    as_user, insights_db, monkeypatch
):
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    with insights_db() as db:
        save_policy(
            db,
            "sales_call_capture",
            FeaturePolicy(
                extra_emails=frozenset({email("Gray", "Holt")}),
                admin_allowed=False,
                exec_allowed=False,
            ),
            actor_email=ADMIN.email,
        )
    adoption = as_user(ADMIN).get(f"{BASE}/dashboards/adoption").json()
    matrix = next(s for s in adoption["sections"] if s["id"] == "feature_adoption")[
        "data"
    ]
    col = [c["key"] for c in matrix["cols"]].index("call_capture")
    sales = [r["key"] for r in matrix["rows"]].index("sales")
    assert matrix["cells"][sales][col] == {
        "value": None,
        "note": "Hidden: fewer than 3 people",
    }


def test_small_cohorts_are_hidden_without_the_people_view(as_user, insights_db):
    """Gray's first use is this week: a cohort of one. Without the people view
    it would say what one named-by-elimination person did, so it is hidden."""
    from rubik_core.database.models import LoginEvent

    _add(
        insights_db, LoginEvent(user_email=email("Gray", "Holt"), timestamp=_utc(TODAY))
    )
    week = "2026-10-05"

    def cohort_row(payload):
        m = next(s for s in payload["sections"] if s["id"] == "retention_cohorts")[
            "data"
        ]
        i = [r["key"] for r in m["rows"]].index(week)
        return m["rows"][i], m["cells"][i]

    label, cells = cohort_row(as_user(ADMIN).get(f"{BASE}/dashboards/adoption").json())
    assert label["label"].endswith("(1)") and cells[0]["value"] == 1.0
    people_view_for_admins_only(insights_db)
    label, cells = cohort_row(as_user(CEO).get(f"{BASE}/dashboards/adoption").json())
    assert "(" not in label["label"]
    assert cells[0] == {"value": None, "note": "Hidden: fewer than 3 people"}


def test_a_feature_switched_off_is_nobodys(as_user, monkeypatch):
    """Eligibility honours the env kill switch: a switched-off feature is not
    'has access' for anyone and has no adoption column."""
    from rubik_core.config import settings

    client = as_user(ADMIN)
    target = email("Avery", "Stone")

    def view():
        from rubik_core.insights.cache import clear_cache as clear_insights_cache

        clear_insights_cache()
        person = client.get(f"{BASE}/people/{target}").json()
        adoption = client.get(f"{BASE}/dashboards/adoption").json()
        matrix = next(s for s in adoption["sections"] if s["id"] == "feature_adoption")
        return {f["key"] for f in person["features"]}, {
            c["key"] for c in matrix["data"]["cols"]
        }

    monkeypatch.setattr(settings, "CUBIE_CRM_SYNC_ENABLED", True)
    features, cols = view()
    assert "crm_sync" in features and "crm_sync" in cols
    monkeypatch.setattr(settings, "CUBIE_CRM_SYNC_ENABLED", False)
    features, cols = view()
    assert "crm_sync" not in features and "crm_sync" not in cols


def test_the_person_read_is_audited_first_and_fails_closed(as_user):
    """No audit row, no data: a failed write is a 503 and the profile is
    never computed."""
    from unittest.mock import MagicMock

    build = MagicMock()
    with (
        patch(
            "backend.routers.insights._record_person_view",
            side_effect=RuntimeError("audit table down"),
        ),
        patch("rubik_core.insights.service.person", build),
    ):
        r = as_user(ADMIN).get(f"{BASE}/people/{email('Blake', 'Rivera')}")
    assert r.status_code == 503
    assert "could not be recorded" in r.json()["detail"]
    assert "Blake" not in r.text
    build.assert_not_called()


def test_the_audit_names_the_real_reader_under_impersonation(insights_db):
    """``get_current_user`` stashes the REAL identity on request.state before
    impersonation masks it; the audit row names that person."""
    from rubik_core.database.models import AuditLog

    def as_ceo_seen_by_admin(request: Request) -> UserProfile:
        request.state.actor_email = ADMIN.email
        return CEO

    app.dependency_overrides[get_current_user] = as_ceo_seen_by_admin
    try:
        r = TestClient(app).get(f"{BASE}/people/{email('Blake', 'Rivera')}")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    with insights_db() as db:
        row = db.query(AuditLog).filter(AuditLog.action == "INSIGHTS_PERSON_VIEW").one()
    assert row.actor_email == ADMIN.email
    assert row.details["impersonating"] == CEO.email


def test_briefing_cards_are_cut_down_to_ids_and_kinds(insights_db):
    """Card titles, reasons and actions never survive the read loop."""
    from rubik_core.insights import service
    from rubik_core.insights.dashboards import priorities

    with insights_db() as db:
        ctx = service._context(
            db,
            ADMIN,
            dept=None,
            range_key="30d",
            start=None,
            end=None,
            can_view_people=True,
        )
        cards = priorities._cards(ctx)
    assert cards
    for _email, _day, _at, meta in cards:
        assert set(meta) == {"priorities", "last_critique_score"}
        for card in meta["priorities"]:
            assert set(card) <= set(priorities.CARD_KEYS)
            assert SENTINEL not in json.dumps(card)


@pytest.mark.parametrize(
    "flag_location", ["attrs", "card", "bundle", "malformed_attrs"]
)
def test_private_gmail_briefing_is_absent_from_shared_priorities(
    insights_db, flag_location
):
    from rubik_core.database.insights import PriorityEvent
    from rubik_core.database.models import HomeCard
    from rubik_core.insights import service
    from rubik_core.insights.dashboards import priorities

    with insights_db() as db:
        before_ctx = service._context(
            db,
            ADMIN,
            dept=None,
            range_key="30d",
            start=None,
            end=None,
            can_view_people=True,
        )
        before = priorities._cards(before_ctx)
        assert before
        owner, day, _created, visible = before[0]
        private_ids = {card["id"] for card in visible["priorities"]}
        card = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == owner, HomeCard.date == day.isoformat())
            .one()
        )
        meta = dict(card.metadata_json)
        meta["priorities"] = [dict(item) for item in meta["priorities"]]
        if flag_location == "attrs":
            meta["priorities"][0]["attrs"] = {"private_gmail_context": True}
        elif flag_location == "card":
            meta["priorities"][0]["personal_gmail"] = True
        elif flag_location == "malformed_attrs":
            meta["priorities"][0]["attrs"] = "unknown provenance"
        else:
            meta["private_gmail_context"] = True
        card.metadata_json = meta
        db.add(
            PriorityEvent(
                user_email=owner,
                kind="delivered_slack",
                actor="system",
                at=datetime.combine(day, time(12, 0)),
            )
        )
        db.commit()

        ctx = service._context(
            db,
            ADMIN,
            dept=None,
            range_key="30d",
            start=None,
            end=None,
            can_view_people=True,
        )
        after = priorities._cards(ctx)
        assert len(after) == len(before) - 1
        assert not any(email == owner and d == day for email, d, _at, _meta in after)
        assert (owner, day) in priorities._private_briefing_days(ctx)
        assert not private_ids.intersection(item.pid for item in priorities.items(ctx))
        _by_id, _by_entity, delivered = priorities._events(ctx)
        assert (owner, day) not in delivered


def test_private_priority_feedback_is_absent_from_shared_ratings(insights_db):
    from rubik_core.database.models import AssistantFeedback, HomeCard
    from rubik_core.insights import service
    from rubik_core.insights.dashboards import priorities, quality

    with insights_db() as db:
        ctx = service._context(
            db,
            ADMIN,
            dept=None,
            range_key="30d",
            start=None,
            end=None,
            can_view_people=True,
        )
        owner, day, _created, meta = priorities._cards(ctx)[0]
        private_id = meta["priorities"][0]["id"]
        card = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == owner, HomeCard.date == day.isoformat())
            .one()
        )
        stored = dict(card.metadata_json)
        stored["priorities"] = [dict(item) for item in stored["priorities"]]
        stored["priorities"][0]["attrs"] = {"private_gmail_context": True}
        card.metadata_json = stored
        db.commit()

        def context():
            return service._context(
                db,
                ADMIN,
                dept=None,
                range_key="30d",
                start=None,
                end=None,
                can_view_people=True,
            )

        public_ids = priorities._public_priority_ids(context())[owner]
        public_id = next(iter(public_ids))
        db.query(AssistantFeedback).filter(
            AssistantFeedback.user_email == owner,
            AssistantFeedback.surface == "priorities",
            AssistantFeedback.proposal_id.in_((private_id, public_id)),
        ).delete(synchronize_session=False)
        db.commit()
        before = priorities._rating_rows(context())
        quality_before = quality._ratings(context())
        for proposal_id, rating in ((private_id, "meh"), (public_id, "useful")):
            db.add(
                AssistantFeedback(
                    id=str(uuid.uuid4()),
                    user_email=owner,
                    surface="priorities",
                    proposal_id=proposal_id,
                    proposal_text="private text"
                    if proposal_id == private_id
                    else "work",
                    rating=rating,
                    created_at=datetime.combine(day, time(12, 0)),
                    updated_at=datetime.combine(day, time(12, 0)),
                )
            )
        db.commit()
        after = priorities._rating_rows(context())
        assert len(after) == len(before) + 1
        assert (owner, day, "useful") in after
        quality_after = quality._ratings(context())
        assert len(quality_after) == len(quality_before) + 1
        assert (owner, day, "priorities", "useful") in quality_after
