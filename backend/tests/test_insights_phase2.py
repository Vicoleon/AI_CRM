"""Insights Phase 2: CRM & HubSpot, Proposals & Prep, Follow-through.

What this file pins, on ``scripts/seed_insights_demo.py``'s rows (Phase 2
appends ``_seed_phase2``):

1. **Contract shape.** Each new dashboard returns a DashboardPayload whose
   KPIs are in the catalog, with every rate carrying its x of y.
2. **Call Capture is department-level only.** A HubSpot write, a call script
   or a Cockpit commitment that came from a recorded call counts for the
   department, never in a people table or a person's card, and a department
   where fewer than 3 people use call capture has those figures withheld for
   every viewer, admins included.
3. **Counts only.** The sentinel the seed writes into every free-text column
   (loose end subjects, proposal prose, nudge and stage-lag evidence, call
   transcripts and request bodies, promise titles) never comes back.
4. **Small departments are hidden** from viewers without the people view.
5. **No leaderboard.** The Follow-through people table sorts A–Z by name.
6. **Wiring.** Overview shows each new dashboard's headline tiles and adds
   their columns to the department comparison; the person profile has a card
   per new dashboard.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.tests.test_insights_router import (  # noqa: F401 — fixtures
    ADMIN,
    BASE,
    CEO,
    DOMAIN,
    PHASE2,
    SENTINEL,
    TODAY,
    _assert_kpi,
    _assert_section,
    _utc,
    as_user,
    email,
    insights_db,
    people_view_for_admins_only,
)
from rubik_core.insights.cache import clear_cache as clear_insights_cache
from rubik_core.insights.catalog import METRICS
from rubik_core.insights.payload import HIDDEN_NOTE

#: KPIs that are a snapshot of "now": no previous value, no trend.
SNAPSHOT_KPIS = {"promises_slipping_now"}

#: Ids that come from recorded calls and must never reach a person.
CALL_DERIVED = {"cockpit_breach_rate"}


def _section(payload: dict, section_id: str) -> dict:
    return next(s for s in payload["sections"] if s["id"] == section_id)


def _kpi(payload: dict, metric_id: str) -> dict:
    return next(k for k in payload["kpis"] if k["id"] == metric_id)


def _add(session_factory, *rows) -> None:
    with session_factory() as db:
        db.add_all(rows)
        db.commit()


def _call(session_factory, ae: str, *, written: int = 1, script: bool = False) -> None:
    """One recorded call today for ``ae``, with ``written`` HubSpot writes
    and, optionally, a call script attached."""
    import uuid

    from rubik_core.database.call_capture import (
        CallExtraction,
        CallExtractionItem,
        CallHubspotWrite,
        CallSession,
    )
    from rubik_core.database.call_scripts import CallScript, CallSessionScript

    def uid():
        return uuid.uuid4().hex

    at = _utc(TODAY, 9)
    sid, ext = uid(), uid()
    rows = [
        CallSession(id=sid, ae_email=ae, status="wrapped", created_at=at),
        CallExtraction(
            id=ext,
            call_session_id=sid,
            model="m",
            prompt_version="v",
            schema_version="1",
            raw_output={"text": SENTINEL},
        ),
    ]
    for _ in range(written):
        item = uid()
        rows.append(
            CallExtractionItem(
                id=item,
                extraction_id=ext,
                call_session_id=sid,
                kind="task",
                evidence_text=SENTINEL,
                status="written",
                created_at=at,
            )
        )
        rows.append(
            CallHubspotWrite(
                id=uid(),
                extraction_item_id=item,
                call_session_id=sid,
                idempotency_key=uid(),
                method="POST",
                path="/crm/v3/objects/tasks",
                request_body={"x": SENTINEL},
                response_status=201,
                attempts=1,
                succeeded_at=at,
                created_at=at,
                updated_at=at,
            )
        )
    if script:
        sc = CallScript(id=uid(), product_key=f"p{uid()[:6]}", name="Demo")
        rows.append(sc)
        rows.append(
            CallSessionScript(
                id=uid(),
                call_session_id=sid,
                script_id=sc.id,
                product_key=sc.product_key,
                attached_at=at,
            )
        )
    _add(session_factory, *rows)


# ── 1. contract shape ────────────────────────────────────────────────────────


def test_meta_lists_the_phase2_dashboards_as_shipped(as_user):  # noqa: F811
    body = as_user(ADMIN).get(f"{BASE}/meta").json()
    available = {d["slug"] for d in body["dashboards"] if d["available"]}
    assert set(PHASE2) <= available
    for metric_id in (
        "crm_hubspot_updates",
        "loose_ends_noise_rate",
        "proposals_draft_rate",
        "cockpit_breach_rate",
    ):
        assert metric_id in body["metrics"]


@pytest.mark.parametrize("slug", PHASE2)
@pytest.mark.parametrize("range_", ["7d", "30d", "90d", "ytd"])
def test_every_phase2_dashboard_returns_the_contract_shape(as_user, slug, range_):  # noqa: F811
    r = as_user(ADMIN).get(f"{BASE}/dashboards/{slug}?range={range_}")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["dashboard"] == slug and p["kpis"] and p["sections"]
    n_buckets = {"7d": 7, "30d": 30}.get(range_)
    for k in p["kpis"]:
        _assert_kpi(k)
        assert k["id"] in METRICS, k["id"]
        if METRICS[k["id"]].unit == "pct":
            assert "numerator" in k and "denominator" in k, k["id"]
        if n_buckets and k["id"] not in SNAPSHOT_KPIS:
            assert len(k["spark"]) == n_buckets, k["id"]
    for s in p["sections"]:
        _assert_section(s)
    assert "smart team" not in r.text.lower()


def test_the_numbers_add_up(as_user):  # noqa: F811
    client = as_user(ADMIN)
    crm = client.get(f"{BASE}/dashboards/crm").json()
    steps = _section(crm, "crm_sync_outcomes")["data"]["steps"]
    assert steps[0]["value"] > 0
    assert steps[0]["value"] == sum(s["value"] for s in steps[1:])
    assert _section(crm, "crm_sync_outcomes")["data"]["mode"] == "breakdown"
    assert _kpi(crm, "crm_hubspot_updates")["value"] > 0
    total = sum(
        sum(series["values"])
        for series in _section(crm, "updates_trend")["data"]["series"]
    )
    assert total == _kpi(crm, "crm_hubspot_updates")["value"]
    by_dept = _section(crm, "updates_by_department")["data"]["items"]
    assert sum(i["value"] for i in by_dept) == total

    follow = client.get(f"{BASE}/dashboards/follow-through").json()
    steps = _section(follow, "loose_ends_outcomes")["data"]["steps"]
    assert steps[0]["value"] == _kpi(follow, "loose_ends_flagged")["value"] > 0
    assert steps[0]["value"] == sum(s["value"] for s in steps[1:])
    noise = _kpi(follow, "loose_ends_noise_rate")
    assert noise["denominator"] == steps[0]["value"]
    assert (
        noise["numerator"] == next(s for s in steps if s["key"] == "dismissed")["value"]
    )
    aging = _section(follow, "open_aging")["data"]["items"]
    assert [i["key"] for i in aging] == ["d0_2", "d3_6", "d7_13", "d14_plus"]

    props = client.get(f"{BASE}/dashboards/proposals").json()
    funnel = _section(props, "proposal_funnel")["data"]
    assert funnel["mode"] == "sequential"
    values = [s["value"] for s in funnel["steps"]]
    assert values == sorted(values, reverse=True) and values[0] > 0
    assert _kpi(props, "prep_briefs")["value"] > 0


def test_loose_ends_history_starts_where_the_data_does(as_user):  # noqa: F811
    """Loose Ends started 24 days before the pinned today (in the seed). A
    90-day view says so, and its previous period has no honest value."""
    p = as_user(ADMIN).get(f"{BASE}/dashboards/follow-through?range=90d").json()
    first = TODAY - timedelta(days=24)
    assert any(
        f"Loose Ends is recorded from {first:%b} {first.day}, {first.year}" in n
        for n in p["notes"]
    )
    assert _kpi(p, "loose_ends_flagged")["prev_value"] is None


# ── 2. call capture is department-level only ────────────────────────────────


def test_call_capture_writes_count_for_the_department_but_never_a_person(
    as_user,  # noqa: F811
    insights_db,  # noqa: F811
):
    """Gray never uses Cubie. A recorded call of theirs that wrote to HubSpot
    adds to Sales' totals, and to nothing with Gray's name on it."""
    gray = email("Gray", "Holt")
    client = as_user(ADMIN)
    before = client.get(f"{BASE}/dashboards/crm?dept=sales").json()
    _call(insights_db, gray, written=2)
    clear_insights_cache()
    after = client.get(f"{BASE}/dashboards/crm?dept=sales").json()
    assert (
        _kpi(after, "crm_hubspot_updates")["value"]
        == _kpi(before, "crm_hubspot_updates")["value"] + 2
    )
    people = _section(after, "people")["data"]
    assert not [c for c in people["columns"] if "call" in c["key"]]
    row = next(r for r in people["rows"] if r["key"] == gray)
    assert row["cells"]["updates"] == 0
    profile = client.get(f"{BASE}/people/{gray}").json()
    card = next(c for c in profile["cards"] if c["dashboard"] == "crm")
    assert (
        next(k for k in card["kpis"] if k["id"] == "crm_hubspot_updates")["value"] == 0
    )
    assert profile["person"]["last_active"] is None


def test_no_person_card_or_people_table_carries_call_derived_numbers(as_user):  # noqa: F811
    client = as_user(ADMIN)
    for who in (email("Avery", "Stone"), email("Blake", "Rivera")):
        body = client.get(f"{BASE}/people/{who}").json()
        ids = {k["id"] for c in body["cards"] for k in c["kpis"]}
        assert not ids & CALL_DERIVED
    for slug in PHASE2:
        p = client.get(f"{BASE}/dashboards/{slug}").json()
        for s in p["sections"]:
            if s["kind"] != "table":
                continue
            keys = {c["key"] for c in s["data"]["columns"]}
            assert not {
                k for k in keys if "call" in k or "script" in k or "commit" in k
            }


def test_a_call_group_of_one_is_hidden_from_everyone(as_user, insights_db):  # noqa: F811
    """One marketer records calls in the seed; a script on their call today
    is a department figure over one person, hidden even from an admin, and
    their call writes are left out of the totals."""
    from rubik_core.database.commitments import Commitment

    marketer = email("Jules", "Park")
    _call(insights_db, marketer, written=3, script=True)
    breach = _utc(TODAY - timedelta(days=1), 10)
    row = Commitment(
        id="c-marketing",
        kind="wrapup",
        hubspot_deal_id="deal-marketing",
        owner_email=marketer,
        opened_at=breach - timedelta(days=2),
        opened_source="call",
        sla_hours=8,
        breach_at=breach,
        breached_at=breach,
        state="breached",
    )
    row.refresh_live_scope_flags()
    _add(insights_db, row)

    client = as_user(ADMIN)
    props = client.get(f"{BASE}/dashboards/proposals").json()
    items = {
        i["key"]: i
        for i in _section(props, "call_scripts_by_department")["data"]["items"]
    }
    assert items["marketing"] == {
        "key": "marketing",
        "label": "Marketing",
        "value": None,
        "note": HIDDEN_NOTE,
    }
    assert items["sales"]["value"] is not None
    assert any("fewer than 3 people use call capture" in n for n in props["notes"])

    crm = client.get(f"{BASE}/dashboards/crm?dept=marketing").json()
    trend_keys = {s["key"] for s in _section(crm, "updates_trend")["data"]["series"]}
    assert "call_capture" not in trend_keys
    assert not _section(crm, "call_capture_outcomes")["data"]["items"]

    follow = client.get(f"{BASE}/dashboards/follow-through").json()
    by_dept = {
        i["key"]: i
        for i in _section(follow, "commitments_by_department")["data"]["items"]
    }
    assert by_dept["marketing"]["value"] is None
    assert by_dept["sales"]["value"] is not None
    only = client.get(f"{BASE}/dashboards/follow-through?dept=marketing").json()
    assert "cockpit_breach_rate" not in {k["id"] for k in only["kpis"]}


# ── 3. counts only ───────────────────────────────────────────────────────────


def test_no_phase2_response_carries_seeded_content(as_user):  # noqa: F811
    client = as_user(ADMIN)
    bodies = []
    for slug in (*PHASE2, "overview"):
        for range_ in ("7d", "90d"):
            bodies.append(client.get(f"{BASE}/dashboards/{slug}?range={range_}").text)
    for who in (email("Avery", "Stone"), email("Noel", "Vance"), email("Rowan", "Ex")):
        bodies.append(client.get(f"{BASE}/people/{who}").text)
    for text in bodies:
        assert SENTINEL not in text
        assert "client@example.com" not in text


def test_loose_end_rows_are_read_without_their_content(insights_db):  # noqa: F811
    from rubik_core.insights import service
    from rubik_core.insights.dashboards import follow_through

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
        rows = follow_through.loops(ctx)
    assert rows
    assert SENTINEL not in repr(rows)


# ── 4. small departments ─────────────────────────────────────────────────────


def test_small_departments_are_hidden_without_the_people_view(
    as_user,  # noqa: F811
    insights_db,  # noqa: F811
    monkeypatch,
):
    """Excluding three of Marketing's five people leaves a department of two:
    its bars and matrix row are hidden from a viewer without the people view,
    and shown to one with it."""
    from rubik_core.company.operations import OperationsConfig
    from rubik_core.insights import population

    monkeypatch.setattr(
        population,
        "get_operations_config",
        lambda: OperationsConfig(
            insights_excluded_emails=tuple(
                email(*n)
                for n in (("Kai", "Reed"), ("Logan", "Shaw"), ("Morgan", "Tate"))
            )
        ),
    )
    people_view_for_admins_only(insights_db)

    def marketing(payload, section_id):
        data = _section(payload, section_id)["data"]
        if "items" in data:
            return next(i for i in data["items"] if i["key"] == "marketing")
        row = [r["key"] for r in data["rows"]].index("marketing")
        return data["cells"][row][0]

    ceo = as_user(CEO)
    crm = ceo.get(f"{BASE}/dashboards/crm").json()
    assert marketing(crm, "updates_by_department") == {
        "key": "marketing",
        "label": "Marketing",
        "value": None,
        "note": HIDDEN_NOTE,
    }
    props = ceo.get(f"{BASE}/dashboards/proposals").json()
    assert marketing(props, "by_department") == {"value": None, "note": HIDDEN_NOTE}
    admin = as_user(ADMIN)
    crm = admin.get(f"{BASE}/dashboards/crm").json()
    assert marketing(crm, "updates_by_department")["value"] is not None
    props = admin.get(f"{BASE}/dashboards/proposals").json()
    assert marketing(props, "by_department")["value"] is not None


def test_people_tables_need_the_people_view(as_user, insights_db):  # noqa: F811
    people_view_for_admins_only(insights_db)
    for slug in PHASE2:
        p = as_user(CEO).get(f"{BASE}/dashboards/{slug}").json()
        assert not [s for s in p["sections"] if s["kind"] == "table"], slug
        assert not [s for s in p["sections"] if s["drill"] == "person"], slug


# ── 5. no leaderboard ────────────────────────────────────────────────────────


def test_follow_through_people_are_listed_alphabetically(as_user):  # noqa: F811
    p = as_user(ADMIN).get(f"{BASE}/dashboards/follow-through").json()
    people = _section(p, "people")["data"]
    assert people["default_sort"] == {"key": "name", "dir": "asc"}
    assert people["columns"][0]["key"] == "name"
    keys = {r["key"] for r in people["rows"]}
    # Everyone with a loose end or a promise is listed, and nobody in view
    # without either (and without access) is padded in.
    assert email("Avery", "Stone") in keys
    assert email("Gray", "Holt") in keys  # the scan flags their loose ends
    for row in people["rows"]:
        assert row["key"].endswith(f"@{DOMAIN}")


# ── 6. wiring ────────────────────────────────────────────────────────────────


def test_overview_shows_the_phase2_headlines_and_columns(as_user):  # noqa: F811
    p = as_user(ADMIN).get(f"{BASE}/dashboards/overview").json()
    by_id = {k["id"]: k for k in p["kpis"]}
    assert by_id["loose_ends_noise_rate"]["href"] == "/insights/follow-through"
    assert by_id["promises_kept_rate"]["href"] == "/insights/follow-through"
    assert by_id["crm_hubspot_updates"]["href"] == "/insights/crm"
    assert by_id["crm_sync_acceptance_rate"]["href"] == "/insights/crm"
    assert by_id["prep_briefs"]["href"] == "/insights/proposals"
    assert by_id["proposals_draft_rate"]["href"] == "/insights/proposals"
    m = _section(p, "department_comparison")["data"]
    cols = [c["key"] for c in m["cols"]]
    for key in (
        "loose_ends_noise_rate",
        "crm_sync_acceptance_rate",
        "proposals_draft_rate",
    ):
        assert key in cols
    sales = [r["key"] for r in m["rows"]].index("sales")
    for key in (
        "loose_ends_noise_rate",
        "crm_sync_acceptance_rate",
        "proposals_draft_rate",
    ):
        c = m["cells"][sales][cols.index(key)]
        assert c is not None and 0 <= c["value"] <= 1 and c["denominator"] > 0


def test_the_person_profile_has_a_card_per_phase2_dashboard(as_user):  # noqa: F811
    body = (
        as_user(ADMIN).get(f"{BASE}/people/{email('Blake', 'Rivera')}?range=7d").json()
    )
    cards = {c["dashboard"]: c for c in body["cards"]}
    assert set(PHASE2) <= set(cards)
    for slug in PHASE2:
        for k in cards[slug]["kpis"]:
            assert k["href"] == f"/insights/{slug}"
            assert len(k["spark"]) == 7
