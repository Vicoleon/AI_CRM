"""UC-45 churn factor scoring — the per-factor point math + renormalization.

Complements test_xray_churn.py (which covers bands + presence). These pin the
actual points each branch emits, so a regression in the scoring curves — like
the HubSpot 'false'-string auto-renew bug — is caught.
"""

from __future__ import annotations

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
    monkeypatch.setattr(
        xray_properties,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(
            xray_renewal_date_property="renews_on",
            xray_auto_renew_properties=("auto_renews", "evergreen"),
            xray_account_status_properties=("account_status",),
        ),
    )


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _calls(*days_ago: int) -> dict:
    return {
        "calls": [
            {"id": str(i), "hs_timestamp": _iso(d)} for i, d in enumerate(days_ago)
        ]
    }


def _factor(bundle: CompanyBundle, name: str):
    return next(
        (f for f in churn.score(bundle, now=NOW).factors if f.name == name), None
    )


def test_engagement_trend_decline_points():
    # 1 activity in the last 90d vs 4 in the prior 90d → ratio 0.25 → 68 pts.
    b = CompanyBundle(company_id="1")
    b.engagements = _calls(10, 100, 120, 140, 160)
    f = _factor(b, "engagement_trend")
    assert f is not None and f.points == 68.0


def test_deal_momentum_stalled_points():
    b = CompanyBundle(company_id="2")
    b.deals = [
        {
            "id": "d1",
            "hs_is_closed": "false",
            "hs_v2_date_entered_current_stage": _iso(200),
        }
    ]
    f = _factor(b, "deal_momentum")
    assert f is not None and f.points == 85.0  # >120d in stage


def test_client_status_branches():
    def status_points(text: str) -> float:
        b = CompanyBundle(company_id="c")
        b.company = {"account_status": text}
        return _factor(b, "client_status").points

    assert status_points("Past Due — delinquent") == 70.0  # at-risk
    assert status_points("Active - current") == 12.0  # healthy
    assert status_points("Some Unmapped Label") == 40.0  # unknown → mild caution


def test_renewal_auto_renew_false_string_is_not_auto():
    # Regression: HubSpot sends the STRING "false"; bool("false") is True. A
    # non-auto-renewing account must get the +15 cliff bump, not be treated as
    # auto-renewing.
    b = CompanyBundle(company_id="3")
    b.deals = [
        {
            "id": "d1",
            "hs_is_closed": "false",
            "renews_on": _iso(-200),  # ~200 days out → base 10
            "evergreen": "false",
        }
    ]
    f = _factor(b, "renewal_exposure")
    assert f is not None and f.points == 25.0  # 10 base + 15 no-auto-renew cliff

    # And a genuine auto-renew stays at the low base.
    b.deals[0]["auto_renews"] = "true"
    f2 = _factor(b, "renewal_exposure")
    assert f2.points == 10.0


def test_renormalization_is_weighted_not_mean():
    # Exactly two factors present (recency via company last-activity so no
    # engagement_trend, + client_status). Weighted avg differs from a plain mean,
    # so this catches a divide-by-count regression.
    b = CompanyBundle(company_id="4")
    b.company = {
        "hs_last_sales_activity_timestamp": _iso(400),  # very stale → recency 95
        "account_status": "Active",  # healthy → 12
    }
    r = churn.score(b, now=NOW)
    names = {f.name for f in r.factors}
    assert names == {"engagement_recency", "client_status"}
    # (95*0.22 + 12*0.13) / (0.22 + 0.13) = 64.2 → 64  (a mean would be 54)
    assert r.score == 64


def test_cadence_relative_recency_differentiates_accounts():
    # Same 60 days dark, different account cadence → different recency risk.
    monthly = CompanyBundle(company_id="m")
    monthly.engagements = _calls(60, 90, 120, 150, 180)  # ~30d cadence
    quarterly = CompanyBundle(company_id="q")
    quarterly.engagements = _calls(60, 150, 240, 330)  # ~90d cadence
    m = _factor(monthly, "engagement_recency").points
    qf = _factor(quarterly, "engagement_recency").points
    assert m > qf  # 60d dark is riskier for a monthly-cadence account
