"""Insights Phase 3: AI Spend, Knowledge & Content, Quality & Trust.

EOS Health shipped here and was removed (the EOS Exec Dashboard already
answers it); ``test_eos_health_is_gone`` keeps it out.

What this file pins, on ``scripts/seed_insights_demo.py``'s rows (the same
fixture as ``test_insights_router.py``):

1. **The contract shape** for all four dashboards across ranges, filtered and
   not. Quality's trust scorecard is a table of surfaces, not people, so it
   carries ``row_link: null`` and no person drill.
2. **Whole-company-only AI features are never named and never split.** Their
   key, their Admin label and anything like them never appear in any
   response; their dollars are in the company total (under "Other
   features") and in no department, person or department-filtered figure.
3. **Counts only.** The seed's sentinel, written into every title, description,
   file name, note, draft, answer and citation title, never comes back.
4. **Small groups are hidden** from viewers without the people view, and
   meeting transcripts over fewer than 3 people from everyone.
5. **EOS numbers are the EOS Dashboard's**, per team, with the department
   filter mapped onto teams by slug (case variants included), and the
   scorecard is read on Sunday-anchored weeks.
6. **Answers citing documents** count the citations list's length, in SQL.
"""

# The fixtures are imported from test_insights_router, so every test that
# takes one "redefines" it.
# ruff: noqa: F811
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from backend.tests.test_insights_router import (  # noqa: F401 — fixtures
    ADMIN,
    BASE,
    CEO,
    PHASE1,
    SALES_EXEC,
    SENTINEL,
    TODAY,
    _add,
    _assert_kpi,
    _utc,
    as_user,
    email,
    insights_db,
    people_view_for_admins_only,
)
from rubik_core.insights.catalog import METRICS

PHASE3 = ("ai-spend", "knowledge", "quality")
SECTION_KINDS = {"timeseries", "bar", "funnel", "matrix", "table"}
#: Never in any response: the whole-company-only features' keys and labels.
PRIVATE = ("smart_teams", "smart teams", "smart-teams", "work style", "st_profile")


def _fresh():
    from rubik_core.insights.cache import clear_cache

    clear_cache()


def _get(client, slug: str, query: str = "") -> dict:
    r = client.get(f"{BASE}/dashboards/{slug}?{query}")
    assert r.status_code == 200, r.text
    return r.json()


def _kpi(payload: dict, metric_id: str) -> dict:
    return next(k for k in payload["kpis"] if k["id"] == metric_id)


def _section(payload: dict, section_id: str) -> dict:
    return next(s for s in payload["sections"] if s["id"] == section_id)


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
    elif s["kind"] == "bar":
        for item in data["items"]:
            assert {"key", "label", "value"} <= set(item)
            if item["value"] is None:
                assert item.get("note"), (s["id"], item)
            elif data["unit"] == "pct":
                assert "numerator" in item and "denominator" in item
    elif s["kind"] == "funnel":
        steps = data["steps"]
        assert data["mode"] in ("sequential", "breakdown")
        if data["mode"] == "breakdown":
            assert steps[0]["value"] == sum(st["value"] for st in steps[1:])
    elif s["kind"] == "table":
        keys = {c["key"] for c in data["columns"]}
        for row in data["rows"]:
            assert set(row["cells"]) <= keys
        if data["row_link"] == "person":
            assert s["drill"] == "person"
            assert all("@" in row["key"] for row in data["rows"])
        else:
            assert data["row_link"] is None and s["drill"] is None


# ── 1. shape ─────────────────────────────────────────────────────────────────


def test_phase3_dashboards_are_listed_as_available(as_user):
    body = as_user(ADMIN).get(f"{BASE}/meta").json()
    available = {d["slug"] for d in body["dashboards"] if d["available"]}
    assert set(PHASE3) <= available and set(PHASE1) <= available


@pytest.mark.parametrize("slug", PHASE3)
@pytest.mark.parametrize(
    "query", ["range=7d", "range=30d", "range=90d", "range=ytd", "dept=sales"]
)
def test_every_phase3_dashboard_returns_the_contract_shape(as_user, slug, query):
    p = _get(as_user(ADMIN), slug, query)
    assert p["dashboard"] == slug and p["kpis"] and p["sections"]
    assert p["period"]["end"] == TODAY.isoformat()
    for k in p["kpis"]:
        _assert_kpi(k)
        m = METRICS[k["id"]]
        if m.unit == "pct":
            assert "numerator" in k and "denominator" in k, k["id"]
    n_buckets = {"range=7d": 7, "range=30d": 30, "dept=sales": 30}.get(query)
    if n_buckets:
        assert all(len(k["spark"]) == n_buckets for k in p["kpis"]), slug
    for s in p["sections"]:
        _assert_section(s)
    assert p["notes"]


def test_overview_shows_every_phase3_headline_with_a_link(as_user):
    p = _get(as_user(ADMIN), "overview")
    hrefs = {k["id"]: k.get("href") for k in p["kpis"]}
    assert hrefs["ai_spend_per_active_person"] == "/insights/ai-spend"
    assert hrefs["ai_spend_total"] == "/insights/ai-spend"
    assert not any(h == "/insights/eos" for h in hrefs.values())
    assert hrefs["documents_added"] == "/insights/knowledge"
    assert hrefs["useful_rating_rate"] == "/insights/quality"
    spend = _get(as_user(ADMIN), "ai-spend")
    assert (
        _kpi(p, "ai_spend_per_active_person")["value"]
        == _kpi(spend, "ai_spend_per_active_person")["value"]
    )


def test_the_person_profile_carries_the_phase3_cards(as_user):
    body = as_user(ADMIN).get(f"{BASE}/people/{email('Avery', 'Stone')}").json()
    cards = {c["dashboard"]: c for c in body["cards"]}
    assert {"ai-spend", "knowledge"} <= set(cards)
    assert "eos" not in cards
    assert all(len(k["spark"]) == 30 for c in body["cards"] for k in c["kpis"])
    # Transcripts are never counted against a person.
    assert "transcripts_ingested" not in {k["id"] for k in cards["knowledge"]["kpis"]}


# ── 2. whole-company-only AI features ────────────────────────────────────────


def _usage(feature: str, cost: float, who: str | None, day: date = TODAY):
    import uuid

    from rubik_core.database.models import LLMUsageEvent

    return LLMUsageEvent(
        id=str(uuid.uuid4()),
        timestamp=_utc(day, 14),
        feature=feature,
        model="gemini-pro",
        total_tokens=1000,
        cost_usd=cost,
        user_email=who,
    )


def test_private_features_count_in_the_company_total_only(as_user, insights_db):
    client = as_user(ADMIN)
    avery = email("Avery", "Stone")

    def view():
        _fresh()
        company = _get(client, "ai-spend", "range=7d")
        sales = _get(client, "ai-spend", "range=7d&dept=sales")
        person = client.get(f"{BASE}/people/{avery}?range=7d").json()
        return company, sales, person

    company, sales, person = view()
    _add(
        insights_db,
        _usage("smart_teams", 1000.0, avery),
        _usage("st_profile", 500.0, avery),
        _usage("smart_teams_guide", 250.0, None),
    )
    company2, sales2, person2 = view()

    # The company total and its "Other features" line take every dollar…
    total = _kpi(company, "ai_spend_total")["value"]
    assert _kpi(company2, "ai_spend_total")["value"] == pytest.approx(
        total + 1750, abs=0.02
    )
    other = {
        i["key"]: i["value"] for i in _section(company2, "by_feature")["data"]["items"]
    }
    before = {
        i["key"]: i["value"] for i in _section(company, "by_feature")["data"]["items"]
    }
    assert other["other"] == pytest.approx(before.get("other", 0) + 1750, abs=0.02)
    # …and no department, person or filtered figure takes any of them.
    assert (
        _kpi(sales2, "ai_spend_total")["value"]
        == _kpi(sales, "ai_spend_total")["value"]
    )
    by_dept = {
        i["key"]: i["value"]
        for i in _section(company2, "by_department")["data"]["items"]
    }
    by_dept_before = {
        i["key"]: i["value"]
        for i in _section(company, "by_department")["data"]["items"]
    }
    assert by_dept == by_dept_before
    people = {
        r["key"]: r["cells"] for r in _section(company2, "people")["data"]["rows"]
    }
    people_before = {
        r["key"]: r["cells"] for r in _section(company, "people")["data"]["rows"]
    }
    assert people[avery]["spend"] == people_before[avery]["spend"]
    card = next(c for c in person2["cards"] if c["dashboard"] == "ai-spend")
    card_before = next(c for c in person["cards"] if c["dashboard"] == "ai-spend")
    assert (
        _kpi(card, "ai_spend_total")["value"]
        == _kpi(card_before, "ai_spend_total")["value"]
    )
    # Overview's spend in a department view does not take them either.
    _fresh()
    ov = _get(client, "overview", "range=7d&dept=sales")
    assert _kpi(ov, "ai_spend_total")["value"] == _kpi(sales, "ai_spend_total")["value"]


def test_private_features_are_never_named_anywhere(as_user, insights_db):
    _add(insights_db, _usage("st_profile", 12.0, email("Blake", "Rivera")))
    client = as_user(ADMIN)
    bodies = [client.get(f"{BASE}/meta").text]
    for slug in (*PHASE3, "overview"):
        for query in ("range=7d", "range=90d", "dept=sales", "dept=engineering"):
            bodies.append(client.get(f"{BASE}/dashboards/{slug}?{query}").text)
    for who in (email("Avery", "Stone"), email("Noel", "Vance")):
        bodies.append(client.get(f"{BASE}/people/{who}").text)
    for text in bodies:
        low = text.lower()
        for word in PRIVATE:
            assert word not in low, word


# ── 3. counts only ───────────────────────────────────────────────────────────


def test_no_phase3_response_carries_seeded_content(as_user):
    client = as_user(ADMIN)
    bodies = []
    for slug in PHASE3:
        for query in ("range=7d", "range=90d", "dept=sales"):
            bodies.append(client.get(f"{BASE}/dashboards/{slug}?{query}").text)
    for who in (email("Avery", "Stone"), email("Indy", "Moss"), email("Rowan", "Ex")):
        bodies.append(client.get(f"{BASE}/people/{who}").text)
    for text in bodies:
        assert SENTINEL not in text
        json.loads(text)


def test_people_tables_need_the_people_view(as_user, insights_db):
    people_view_for_admins_only(insights_db)
    client = as_user(CEO)
    for slug in PHASE3:
        p = _get(client, slug)
        assert not [s for s in p["sections"] if s["drill"] == "person"], slug
        assert not [
            s for s in p["sections"] if s["kind"] == "table" and s["data"]["row_link"]
        ], slug
    # The admin still has them.
    assert [
        s for s in _get(as_user(ADMIN), "ai-spend")["sections"] if s["id"] == "people"
    ]


# ── 4. small groups ──────────────────────────────────────────────────────────


def _add_tiny_department(session_factory) -> None:
    """Two people in Finance, with an EOS to-do, a document, a transcript and
    some AI spend each."""
    import uuid

    from rubik_core.database.eos import Person, Todo, TodoStatus
    from rubik_core.database.models import Asset, Department, User

    rows: list = [Department(name="Finance")]
    for i, first in enumerate(("Ada", "Bea")):
        addr = email(first, "Fin")
        rows.append(
            User(email=addr, id=f"fin-{i}", name=f"{first} Fin", department="Finance")
        )
        rows.append(_usage("chat", 2.0, addr))
        rows.append(
            Asset(
                doc_id=uuid.uuid4().hex,
                owner_email=addr,
                topic="Pricing",
                status="approved",
                created_at=_utc(TODAY, 10),
            )
        )
        rows.append(
            Asset(
                doc_id=uuid.uuid4().hex,
                owner_email=addr,
                topic="Meeting Transcripts",
                status="approved",
                created_at=_utc(TODAY, 11),
            )
        )
    _add(session_factory, *rows)
    with session_factory() as db:
        person = Person(first_name="Ada", last_name="Fin", email=email("Ada", "Fin"))
        db.add(person)
        db.flush()
        db.add(
            Todo(
                owner_id=person.id,
                description="x",
                department_name="Finance",
                status=TodoStatus.DONE,
                created_at=_utc(TODAY, 9),
            )
        )
        db.commit()


def _item(payload: dict, section_id: str, key: str) -> dict:
    return next(
        i for i in _section(payload, section_id)["data"]["items"] if i["key"] == key
    )


HIDDEN = {
    "key": "finance",
    "label": "Finance",
    "value": None,
    "note": "Hidden: fewer than 3 people",
}


def test_departments_under_three_people_are_hidden_without_the_people_view(
    as_user, insights_db
):
    _add_tiny_department(insights_db)
    admin = as_user(ADMIN)
    # With the people view the numbers show, except transcripts (never).
    assert _item(_get(admin, "ai-spend"), "by_department", "finance")["value"] == 4.0
    assert (
        _item(_get(admin, "knowledge"), "documents_by_department", "finance")["value"]
        == 2
    )
    assert (
        _item(_get(admin, "knowledge"), "transcripts_by_department", "finance")
        == HIDDEN
    )

    people_view_for_admins_only(insights_db)
    ceo = as_user(CEO)
    _fresh()
    assert _item(_get(ceo, "ai-spend"), "by_department", "finance") == HIDDEN
    assert _item(_get(ceo, "knowledge"), "documents_by_department", "finance") == HIDDEN
    assert (
        _item(_get(ceo, "knowledge"), "transcripts_by_department", "finance") == HIDDEN
    )


# ── 5. EOS Health is gone ────────────────────────────────────────────────────


def test_eos_health_is_gone(as_user):
    """Removed: the EOS Exec Dashboard is where EOS health lives."""
    admin = as_user(ADMIN)
    meta = admin.get(f"{BASE}/meta").json()
    assert "eos" not in {d["slug"] for d in meta["dashboards"]}
    assert not any(m.startswith("eos_") for m in meta["metrics"])
    assert admin.get(f"{BASE}/dashboards/eos").status_code == 404


# ── 6. knowledge and quality ─────────────────────────────────────────────────


def test_answers_citing_documents_count_the_citations_list(as_user, insights_db):
    import uuid

    from rubik_core.database.models import ChatMessage, ChatSession

    client = as_user(ADMIN)
    before = _kpi(
        _get(client, "knowledge", "dept=sales&range=7d"),
        "answers_citing_documents_rate",
    )
    sid = str(uuid.uuid4())
    at = _utc(TODAY, 15)
    metas = (
        {"citations": [{"doc_id": "d1", "title": SENTINEL}, {"doc_id": "d2"}]},
        {"citations": []},
        {"citations": None},
        None,
    )
    _add(
        insights_db,
        ChatSession(
            id=sid,
            user_email=email("Avery", "Stone").upper(),
            title=SENTINEL,
            created_at=at,
        ),
        *[
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=sid,
                role="assistant",
                content=SENTINEL,
                metadata_json=meta,
                created_at=at + timedelta(seconds=i),
            )
            for i, meta in enumerate(metas)
        ],
        ChatMessage(
            id=str(uuid.uuid4()),
            session_id=sid,
            role="user",
            content=SENTINEL,
            metadata_json={"citations": [{"doc_id": "d3"}]},
            created_at=at,
        ),
    )
    _fresh()
    after_payload = _get(client, "knowledge", "dept=sales&range=7d")
    after = _kpi(after_payload, "answers_citing_documents_rate")
    assert after["denominator"] == before["denominator"] + 4
    assert after["numerator"] == before["numerator"] + 1
    assert SENTINEL not in json.dumps(after_payload)


def test_the_trust_scorecard_names_every_measure_with_its_denominator(as_user):
    p = _get(as_user(ADMIN), "quality")
    table = _section(p, "trust_scorecard")["data"]
    assert table["row_link"] is None
    keys = {r["key"] for r in table["rows"]}
    assert {
        "chat_useful",
        "chat_errors",
        "priorities_dismissed",
        "priorities_critic",
        "meeting_prep_rewrites",
        "proposals_redraft",
        "crm_sync_accepted",
        "loose_ends_noise",
    } <= keys
    for row in table["rows"]:
        cells = row["cells"]
        if cells["value"] is not None:
            assert cells["of"], row["key"]
            assert 0 <= cells["value"] <= 1
    # The scorecard and the KPIs agree.
    noise = next(r for r in table["rows"] if r["key"] == "loose_ends_noise")["cells"]
    assert noise["value"] == _kpi(p, "quality_loose_ends_noise_rate")["value"]
    errors = next(r for r in table["rows"] if r["key"] == "chat_errors")["cells"]
    ask = _get(as_user(ADMIN), "ask-cubie")
    assert errors["value"] == pytest.approx(
        1 - _kpi(ask, "chat_success_rate")["value"], abs=1e-3
    )
    # Dismissed cards are the AI Priorities dashboard's own number.
    prio = _get(as_user(ADMIN), "priorities")
    assert _kpi(p, "priorities_dismiss_rate") == _kpi(prio, "priorities_dismiss_rate")


def test_loose_ends_noise_is_dismissed_out_of_flagged(as_user, insights_db):
    from rubik_core.database.loop_closure import LoopItem

    client = as_user(ADMIN)
    before = _kpi(_get(client, "quality", "range=7d"), "quality_loose_ends_noise_rate")
    flagged = _utc(TODAY, 8)
    _add(
        insights_db,
        LoopItem(
            owner_email=email("Blake", "Rivera"),
            kind="unlogged",
            source="gmail",
            source_key="gmail:t-noise",
            status="dismissed",
            title=SENTINEL,
            opened_at=flagged,
            first_flagged_at=flagged,
            last_checked_at=flagged,
            dismissed_at=flagged,
            dismiss_reason="internal",
        ),
        LoopItem(
            owner_email=email("Blake", "Rivera"),
            kind="unlogged",
            source="gmail",
            source_key="gmail:t-real",
            status="open",
            title=SENTINEL,
            opened_at=flagged,
            first_flagged_at=flagged,
            last_checked_at=flagged,
        ),
    )
    _fresh()
    after = _kpi(_get(client, "quality", "range=7d"), "quality_loose_ends_noise_rate")
    assert after["denominator"] == before["denominator"] + 2
    assert after["numerator"] == before["numerator"] + 1
