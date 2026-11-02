"""The Insights metric catalog is the one source of every KPI's meaning.

* Every KPI id any shipped dashboard (and the person profile) emits, on
  seeded data across several ranges and with and without a department filter,
  exists in the catalog.
* Every ``pct`` KPI carries a numerator and a denominator — the house rule
  that no percentage is shown without what it is out of — and so does every
  ``pct`` bar item and matrix cell that has a value.
* Every catalog entry is complete, and every rate names its denominator.
* The AI summary's cost category has its admin label.
"""

from __future__ import annotations

import pytest

from backend.tests.test_insights_router import (  # noqa: F401 — fixtures
    ADMIN,
    BASE,
    as_user,
    email,
    insights_db,
)
from rubik_core.insights.catalog import METRICS, catalog_dict
from rubik_core.insights.dashboards import available

#: Every dashboard that has shipped.
SHIPPED = tuple(d.slug for d in available())


def _walk_kpis(payload: dict):
    yield from payload.get("kpis") or []
    for card in payload.get("cards") or []:
        yield from card.get("kpis") or []


def _assert_kpi_follows_the_catalog(k: dict, where: str) -> None:
    assert k["id"] in METRICS, f"{where}: {k['id']} is not in the catalog"
    if METRICS[k["id"]].unit == "pct":
        assert "numerator" in k and "denominator" in k, (
            f"{where}: {k['id']} has no x of y"
        )
        if k["value"] is not None:
            assert 0 <= k["value"] <= 1.5, f"{where}: {k['id']} is not a 0–1 rate"


@pytest.mark.parametrize(
    "query", ["range=7d", "range=30d", "range=90d", "range=ytd", "dept=sales"]
)
def test_every_emitted_kpi_is_in_the_catalog_with_its_denominator(as_user, query):  # noqa: F811
    client = as_user(ADMIN)
    seen: set[str] = set()
    for slug in SHIPPED:
        payload = client.get(f"{BASE}/dashboards/{slug}?{query}").json()
        for k in _walk_kpis(payload):
            _assert_kpi_follows_the_catalog(k, f"{slug}?{query}")
            seen.add(k["id"])
        for s in payload["sections"]:
            data = s["data"]
            if s["kind"] == "bar" and data["unit"] == "pct":
                for item in data["items"]:
                    if item["value"] is not None:
                        assert "numerator" in item and "denominator" in item, s["id"]
            if s["kind"] == "matrix" and data["unit"] == "pct":
                for row in data["cells"]:
                    for c in row:
                        if c is not None:
                            assert "numerator" in c and "denominator" in c, s["id"]
    person = client.get(f"{BASE}/people/{email('Avery', 'Stone')}?{query}").json()
    for k in _walk_kpis(person):
        _assert_kpi_follows_the_catalog(k, "person")
        seen.add(k["id"])
    assert seen, "no KPIs were emitted at all"


def test_every_catalog_metric_is_emitted_somewhere(as_user):  # noqa: F811
    """A catalog entry nothing emits is a definition with no number behind it."""
    client = as_user(ADMIN)
    seen = set()
    for slug in SHIPPED:
        seen |= {
            k["id"] for k in client.get(f"{BASE}/dashboards/{slug}").json()["kpis"]
        }
    person = client.get(f"{BASE}/people/{email('Avery', 'Stone')}").json()
    seen |= {k["id"] for k in _walk_kpis(person)}
    assert set(METRICS) == seen


def test_every_definition_is_complete_and_every_rate_names_its_denominator():
    for m in METRICS.values():
        assert m.label and len(m.definition) > 30, m.id
        assert m.unit in ("count", "pct", "usd", "ms", "days", "ratio", "score"), m.id
        assert m.good_direction in ("up", "down", "neutral"), m.id
        if m.unit == "pct":
            assert m.denominator, f"{m.id} is a rate with no denominator"
        if m.part_unit is not None:
            # The parts of a share are in a real unit, never another share.
            assert m.part_unit in ("count", "usd", "ms", "days", "ratio"), m.id
            assert m.unit == "pct", f"{m.id} has a part unit but is not a share"
        # Business language: no table or column names.
        for jargon in ("_logs", "_events", "metadata", "user_email", "SQL"):
            assert jargon not in m.definition, (m.id, jargon)
    assert (
        catalog_dict()["active_rate"]["denominator"]
        == "people in the selected departments"
    )


def test_the_summary_cost_category_has_its_admin_label():
    from backend.routers.admin import _FEATURE_LABELS
    from rubik_core.insights.summarize import COST_FEATURE

    assert COST_FEATURE == "insights_summary"
    assert _FEATURE_LABELS[COST_FEATURE] == "Insights summaries"
