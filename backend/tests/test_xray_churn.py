"""UC-45 churn scorer — factors + bands on fixture accounts.

Pure-Python (no DB, no network). Asserts relative/structural properties rather
than brittle exact scores, plus the two design guarantees: skipped factors
renormalize (a single factor's points == the score) and missing evidence is
never scored as healthy.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig
from rubik_core.xray import churn
from rubik_core.xray import properties as xray_properties
from rubik_core.xray.schemas import CompanyBundle

NOW = datetime(2026, 7, 15)


@pytest.fixture(autouse=True)
def _named_fields(monkeypatch):
    """The renewal, auto-renew and status fields are the company's own, so the
    tests name theirs (neutral, fictional internal names)."""
    current = HubSpotConnectorConfig(
        xray_renewal_date_property="renews_on",
        xray_auto_renew_properties=("auto_renews", "evergreen"),
        xray_account_status_properties=("account_status",),
    )

    def get_config():
        return current

    monkeypatch.setattr(xray_properties, "get_hubspot_config", get_config)

    def configure(**changes):
        nonlocal current
        current = replace(current, **changes)

    return configure


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _calls(*days_ago: int) -> dict:
    return {
        "calls": [
            {"id": str(i), "hs_timestamp": _iso(d)} for i, d in enumerate(days_ago)
        ]
    }


def _healthy() -> CompanyBundle:
    b = CompanyBundle(company_id="1", company_name="Healthy Co")
    b.company = {"account_status": "Active - current"}
    b.engagements = _calls(2, 12, 22, 32, 42, 100, 130, 160)  # busy recently
    b.contacts = [
        {"id": str(i), "hs_last_sales_activity_timestamp": _iso(10)} for i in range(4)
    ]
    b.deals = [
        {
            "id": "d1",
            "hs_is_closed": "false",
            "amount": "50000",
            "renews_on": _iso(-300),  # 300 days out
            "auto_renews": "true",
            "hs_v2_date_entered_current_stage": _iso(10),
        }
    ]
    return b


def _ghosting() -> CompanyBundle:
    b = CompanyBundle(company_id="2", company_name="Ghost Co")
    b.company = {"account_status": "Active"}
    b.engagements = _calls(120, 150, 180, 210)  # nothing recent; ~30d cadence
    b.contacts = [{"id": "c1", "hs_last_sales_activity_timestamp": _iso(120)}]
    return b


def test_healthy_is_low_risk():
    r = churn.score(_healthy(), now=NOW)
    assert r.band == "Healthy"
    assert r.score < 40
    assert {f.name for f in r.factors} >= {"engagement_recency", "engagement_trend"}


def test_ghosting_scores_worse_than_healthy():
    healthy = churn.score(_healthy(), now=NOW)
    ghost = churn.score(_ghosting(), now=NOW)
    assert ghost.score > healthy.score
    recency = next(f for f in ghost.factors if f.name == "engagement_recency")
    assert recency.points >= 55  # well past its own cadence


def test_renewal_cliff_factor_is_high():
    b = CompanyBundle(company_id="3", company_name="Cliff Co")
    b.company = {"account_status": "Active"}
    b.engagements = _calls(40, 70)
    b.deals = [
        {
            "id": "d1",
            "hs_is_closed": "false",
            "renews_on": _iso(-20),  # 20 days away
            "auto_renews": "false",  # no auto-renew = the cliff
        }
    ]
    r = churn.score(b, now=NOW)
    renewal = next(f for f in r.factors if f.name == "renewal_exposure")
    assert renewal.points >= 80


def test_single_threaded_breadth_is_high():
    b = CompanyBundle(company_id="4", company_name="Solo Co")
    b.company = {"account_status": "Active"}
    b.engagements = _calls(10, 40)
    b.contacts = [{"id": "c1", "hs_last_sales_activity_timestamp": _iso(10)}]
    r = churn.score(b, now=NOW)
    breadth = next(f for f in r.factors if f.name == "relationship_breadth")
    assert breadth.points == 80


def test_terminated_status_and_renormalization():
    # Only the client_status factor has data → the score renormalizes to it.
    b = CompanyBundle(company_id="5", company_name="Gone Co")
    b.company = {"account_status": "Terminated - closed lost"}
    r = churn.score(b, now=NOW)
    assert len(r.factors) == 1
    assert r.factors[0].name == "client_status"
    assert r.factors[0].points == 90
    assert r.score == 90  # single factor → weight renormalizes to 1.0
    assert r.band == "At-risk"


def test_no_data_is_not_healthy_theater():
    r = churn.score(CompanyBundle(company_id="6"), now=NOW)
    assert r.factors == []
    assert r.score == 0  # nothing to score — explicitly empty, not a fake pass


def test_meeting_sentiment_is_deep_only_and_directional():
    b = _healthy()
    neg = churn.score(b, meeting_sentiment=-1.0, now=NOW)
    pos = churn.score(b, meeting_sentiment=1.0, now=NOW)
    assert neg.score > pos.score
    assert any(f.name == "meeting_sentiment" for f in neg.factors)
    # Fast run (no sentiment) omits the factor entirely.
    fast = churn.score(b, now=NOW)
    assert not any(f.name == "meeting_sentiment" for f in fast.factors)


def test_bands_boundaries():
    from rubik_core.xray.config import band_for_score

    assert band_for_score(0) == "Healthy"
    assert band_for_score(39) == "Healthy"
    assert band_for_score(40) == "Watch"
    assert band_for_score(69) == "Watch"
    assert band_for_score(70) == "At-risk"
    assert band_for_score(100) == "At-risk"


def test_with_no_fields_named_renewal_and_status_drop_out(_named_fields):
    """A fresh workspace names none of its own fields: the renewal and status
    factors are absent (never scored as healthy), whatever a record carries."""
    _named_fields(
        xray_renewal_date_property="",
        xray_auto_renew_properties=(),
        xray_account_status_properties=(),
    )

    r = churn.score(_healthy(), now=NOW)

    names = {f.name for f in r.factors}
    assert "renewal_exposure" not in names
    assert "client_status" not in names
    assert {"engagement_recency", "relationship_breadth"} <= names


def test_status_reads_every_named_field_in_order(_named_fields):
    _named_fields(xray_account_status_properties=("billing_state", "account_status"))
    b = CompanyBundle(company_id="7", company_name="Two Field Co")
    b.company = {"account_status": "Active", "billing_state": "Past due"}

    status = next(
        f for f in churn.score(b, now=NOW).factors if f.name == "client_status"
    )

    assert status.value == "Past due / Active"
    assert status.points == 70  # the at-risk wording wins over "Active"
