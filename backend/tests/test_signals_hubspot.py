"""UC-17.1 — Signal dataclass + HubSpot SignalSource.

Two layers:
  1. `Signal` dataclass — construction, serialization, source validation.
  2. The retired mirror mapper — reshapes rows into Signals, drops deprecated
     stages, and honors severity thresholds.

BigQuery is mocked end-to-end. We never hit a real client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# ─── Signal dataclass ─────────────────────────────────────────────────────────


def test_signal_construction_and_roundtrip_via_to_dict():
    from backend.signals import Signal

    occurred = datetime(2026, 4, 12, 14, 30, tzinfo=timezone.utc)
    sig = Signal(
        user_email="rep1@acme.example",
        source="hubspot",
        signal_type="stale_lead",
        summary="Deal 'Acme Q2' stale for 28 days",
        occurred_at=occurred,
        entity_refs={"deal_id": "d-100", "company_name": "Acme"},
        severity_hint="high",
        payload={"days_since_last_activity": 28},
    )
    d = sig.to_dict()
    assert d["source"] == "hubspot"
    assert d["signal_type"] == "stale_lead"
    assert d["user_email"] == "rep1@acme.example"
    assert d["entity_refs"] == {"deal_id": "d-100", "company_name": "Acme"}
    assert d["severity_hint"] == "high"
    assert d["payload"]["days_since_last_activity"] == 28
    assert d["occurred_at"] == occurred.isoformat()
    assert d["signal_id"] == sig.signal_id


def test_signal_rejects_unknown_source():
    from backend.signals import Signal

    with pytest.raises(ValueError, match="source"):
        Signal(
            user_email="x@y.com",
            source="zapier",  # not in KNOWN_SOURCES
            signal_type="whatever",
            summary="…",
            occurred_at=datetime.now(timezone.utc),
        )


def test_signal_rejects_unknown_severity():
    from backend.signals import Signal

    with pytest.raises(ValueError, match="severity"):
        Signal(
            user_email="x@y.com",
            source="hubspot",
            signal_type="stale_lead",
            summary="…",
            occurred_at=datetime.now(timezone.utc),
            severity_hint="catastrophic",
        )


def test_signal_id_is_unique_by_default():
    from backend.signals import Signal

    a = Signal(
        user_email="x@y.com",
        source="hubspot",
        signal_type="t",
        summary="s",
        occurred_at=datetime.now(timezone.utc),
    )
    b = Signal(
        user_email="x@y.com",
        source="hubspot",
        signal_type="t",
        summary="s",
        occurred_at=datetime.now(timezone.utc),
    )
    assert a.signal_id != b.signal_id


# ─── HubSpot SignalSource ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pin_bigquery_project(monkeypatch):
    """Exercise the dormant row mapper with injected rows and a project ID.

    `sales_hubspot.fetch_*` checks `_project()` before building SQL. These
    tests inject a mock `bq` and call `_fetch_bigquery` directly, while the
    production path remains disabled and `fetch()` uses live HubSpot only.

    Pin a project so these unit tests exercise row→Signal reshaping
    deterministically, regardless of ambient env.
    """
    from backend.agents import sales_hubspot
    from backend.signals import hubspot
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "BIGQUERY_PROJECT_ID", "test-project")
    # Keep the dormant mapper's row handling covered with injected test rows.
    # Production entry points and BigQuery adapters remain fail closed.
    monkeypatch.setattr(sales_hubspot, "require_workspace_crm_bigquery", lambda: None)
    monkeypatch.setattr(hubspot, "require_workspace_crm_bigquery", lambda: None)


def _bq_mock(stale_rows=None, stage_rows=None, triggers=None):
    bq = MagicMock()

    def fake_query(sql):
        if "fct_activities" in sql and "last_activity" in sql:
            return stale_rows or []
        if "fct_deal_stage_history" in sql:
            return stage_rows or []
        if "market_triggers" in sql:
            return triggers or []
        return []

    bq.query.side_effect = fake_query
    return bq


def test_hubspot_source_maps_stale_deal_row_to_signal():
    from backend.signals.hubspot import HubspotSignalSource

    last_date = (datetime.utcnow() - timedelta(days=25)).date()
    stale_rows = [
        {
            "deal_id": "deal-100",
            "deal_name": "Acme Q2 expansion",
            "owner_email": "rep1@acme.example",
            "company_name": "Acme Corp",
            "last_activity_date": last_date,
            "days_since_last_activity": 25,
            "dealstage": "qualifiedtobuy",
            "amount": 12000,
            "products": "Platform Basic",
        }
    ]

    src = HubspotSignalSource(bq=_bq_mock(stale_rows=stale_rows))
    sigs = src._fetch_bigquery("rep1@acme.example", since=datetime.now(timezone.utc))

    stale = [s for s in sigs if s.signal_type == "stale_lead"]
    assert len(stale) == 1
    s = stale[0]
    assert s.source == "hubspot"
    assert s.user_email == "rep1@acme.example"
    assert s.entity_refs["deal_id"] == "deal-100"
    assert s.entity_refs["company_name"] == "Acme Corp"
    assert "Acme Q2 expansion" in s.summary
    assert "25 days" in s.summary
    assert s.severity_hint == "medium"  # 22-30d window
    assert s.payload["days_since_last_activity"] == 25
    assert s.payload["dealstage"] == "qualifiedtobuy"


def test_hubspot_source_drops_deprecated_stage_deals(caplog):
    from backend.signals.hubspot import HubspotSignalSource

    last_date = (datetime.utcnow() - timedelta(days=40)).date()
    stale_rows = [
        {
            "deal_id": "deal-zombie",
            "deal_name": "Old Co — legacy",
            "owner_email": "rep1@acme.example",
            "company_name": "Old Co",
            "last_activity_date": last_date,
            "days_since_last_activity": 40,
            "dealstage": "Hold (DO NOT USE)",
            "amount": 0,
            "products": None,
        }
    ]

    src = HubspotSignalSource(bq=_bq_mock(stale_rows=stale_rows))
    with caplog.at_level("WARNING"):
        sigs = src._fetch_bigquery(
            "rep1@acme.example", since=datetime.now(timezone.utc)
        )

    assert [s for s in sigs if s.signal_type == "stale_lead"] == []
    assert any("deprecated stage" in rec.message for rec in caplog.records)


def test_hubspot_source_filters_by_user_email():
    from backend.signals.hubspot import HubspotSignalSource

    last_date = (datetime.utcnow() - timedelta(days=25)).date()
    stale_rows = [
        {
            "deal_id": "d1",
            "deal_name": "A",
            "owner_email": "rep1@acme.example",
            "company_name": "A Co",
            "last_activity_date": last_date,
            "days_since_last_activity": 25,
            "dealstage": "qualifiedtobuy",
            "amount": 1,
            "products": "",
        },
        {
            "deal_id": "d2",
            "deal_name": "B",
            "owner_email": "rep2@acme.example",
            "company_name": "B Co",
            "last_activity_date": last_date,
            "days_since_last_activity": 25,
            "dealstage": "qualifiedtobuy",
            "amount": 1,
            "products": "",
        },
    ]
    src = HubspotSignalSource(bq=_bq_mock(stale_rows=stale_rows))
    sigs = src._fetch_bigquery("rep1@acme.example", since=datetime.now(timezone.utc))
    assert {s.entity_refs.get("deal_id") for s in sigs} == {"d1"}


@pytest.mark.parametrize(
    "days,expected",
    [
        (22, "medium"),
        (28, "medium"),
        (31, "high"),
        (60, "high"),
    ],
)
def test_stale_lead_severity_thresholds(days, expected):
    from backend.signals.hubspot import HubspotSignalSource

    last_date = (datetime.utcnow() - timedelta(days=days)).date()
    stale_rows = [
        {
            "deal_id": f"d-{days}",
            "deal_name": "Test",
            "owner_email": "rep1@acme.example",
            "company_name": "Test Co",
            "last_activity_date": last_date,
            "days_since_last_activity": days,
            "dealstage": "qualifiedtobuy",
            "amount": 1,
            "products": "",
        }
    ]
    src = HubspotSignalSource(bq=_bq_mock(stale_rows=stale_rows))
    sigs = src._fetch_bigquery("rep1@acme.example", since=datetime.now(timezone.utc))
    stale = [s for s in sigs if s.signal_type == "stale_lead"]
    assert len(stale) == 1
    assert stale[0].severity_hint == expected


def test_hubspot_source_emits_stage_stuck_with_severity():
    from backend.signals.hubspot import HubspotSignalSource

    entered = (datetime.utcnow() - timedelta(days=35)).date()
    stage_rows = [
        {
            "deal_id": "deal-stuck",
            "deal_name": "Stuck Deal",
            "owner_email": "rep1@acme.example",
            "company_name": "Stuck Co",
            "stage_label": "Proposal Sent",
            "stage_entered_ts": entered,
            "days_in_stage": 35,
            "amount": 5000,
        }
    ]
    src = HubspotSignalSource(bq=_bq_mock(stage_rows=stage_rows))
    sigs = src._fetch_bigquery("rep1@acme.example", since=datetime.now(timezone.utc))
    stuck = [s for s in sigs if s.signal_type == "stage_stuck"]
    assert len(stuck) == 1
    s = stuck[0]
    assert s.severity_hint == "high"  # > 30d
    assert "Proposal Sent" in s.summary
    assert s.entity_refs["deal_id"] == "deal-stuck"
    assert s.payload["days_in_stage"] == 35


def test_hubspot_source_drops_stage_stuck_in_deprecated_stage():
    from backend.signals.hubspot import HubspotSignalSource

    entered = (datetime.utcnow() - timedelta(days=35)).date()
    stage_rows = [
        {
            "deal_id": "deal-stuck",
            "deal_name": "Stuck",
            "owner_email": "rep1@acme.example",
            "company_name": "Stuck Co",
            "stage_label": "Hold (DO NOT USE)",
            "stage_entered_ts": entered,
            "days_in_stage": 35,
            "amount": 0,
        }
    ]
    src = HubspotSignalSource(bq=_bq_mock(stage_rows=stage_rows))
    sigs = src._fetch_bigquery("rep1@acme.example", since=datetime.now(timezone.utc))
    assert [s for s in sigs if s.signal_type == "stage_stuck"] == []


def test_signal_source_protocol_is_satisfied_by_hubspot():
    from backend.signals import SignalSource
    from backend.signals.hubspot import HubspotSignalSource

    src = HubspotSignalSource(bq=MagicMock())
    assert isinstance(src, SignalSource)


def test_fetch_live_restricts_deals_to_allowlisted_pipelines(monkeypatch):
    """The live deal query must carry a pipeline IN-filter so a client's pipeline
    (Initech Partners) hosted inside the company's own HubSpot portal can't leak
    into its priorities — without it an Initech deal surfaces as a reader's "only
    open deal"."""
    from backend.signals import hubspot as hs_mod
    from backend.signals.hubspot import HubspotSignalSource
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def search_all(self, object_type, filters, properties, sorts=None):
            captured["filters"] = filters
            return []  # no deals — we only assert the query, not the mapping

    monkeypatch.setattr(hs_mod, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(
        hs_mod,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default", "9100002")),
    )
    # _fetch_live imports HubSpotAPIClient from this module at call time.
    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", FakeClient)
    monkeypatch.setattr(
        hs_mod,
        "_cached_directories",
        lambda client: (
            {"77": {"email": "emery@acme.example", "name": "Emery"}},
            {},
        ),
    )

    src = HubspotSignalSource(bq=MagicMock())
    src.fetch("emery@acme.example", since=datetime.now(timezone.utc))

    pipeline_filters = [
        f for f in captured["filters"] if f["propertyName"] == "pipeline"
    ]
    assert pipeline_filters == [
        {"propertyName": "pipeline", "operator": "IN", "values": ["default", "9100002"]}
    ]


def test_fetch_live_no_deal_query_when_allowlist_empty(monkeypatch):
    """An unconfigured workspace never queries a shared portal's deals."""
    from backend.signals import hubspot as hs_mod
    from backend.signals.hubspot import HubspotSignalSource
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def search_all(self, object_type, filters, properties, sorts=None):
            captured["filters"] = filters
            return []

    monkeypatch.setattr(hs_mod, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(hs_mod, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", FakeClient)
    monkeypatch.setattr(
        hs_mod,
        "_cached_directories",
        lambda client: (
            {"77": {"email": "emery@acme.example", "name": "Emery"}},
            {},
        ),
    )

    src = HubspotSignalSource(bq=MagicMock())
    src.fetch("emery@acme.example", since=datetime.now(timezone.utc))

    assert "filters" not in captured
