"""The previous company's HubSpot portal leaves the code (spec §2).

Its won-stage ids, the deal stages the stage-lag detector compared against,
and its custom company and deal properties used to be literals in the code.
Every company's portal is its own, so each one is configuration now, empty by
default:

* won stages: HubSpot's built-in ``closedwon`` plus
  ``HUBSPOT_CLOSED_WON_STAGE_IDS``, bound into the BigQuery SQL as a query
  parameter by one helper (``rubik_core.retrieval.closed_won``), never
  interpolated;
* stage lag: ``COCKPIT_STAGE_LAG_STAGES``; with none set the detector finds
  nothing and never reads HubSpot;
* the company's own fields (rubik_core/retrieval/hubspot_properties.py,
  rubik_core/xray/properties.py): the deal product and turnaround fields,
  Customer X-Ray's renewal date, auto-renew, account status and CSM fields,
  and its extras. With none named, X-Ray reads HubSpot's built-ins only, the
  turn-times answer says it is not set up, and nothing reads a field by an
  old portal's name;
* Call Capture's company write fence: the vendor fields an admin configured
  (``CCS_VENDOR_FIELDS_DEFAULT`` / ``_EXTENDED``), never a built-in that drives
  HubSpot automation, never the X-Ray research link.

The removed ids and names are on the legacy term list
(``backend/tests/_legacy_brand.py``), so they cannot come back.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from rubik_core.config import settings
from rubik_core.tenancy.hubspot_config import (
    HubSpotConfigInvalid,
    HubSpotConnectorConfig,
)

#: HubSpot's built-in deal and company properties X-Ray may read by default.
HUBSPOT_BUILT_IN = {
    "dealname",
    "amount",
    "hs_acv",
    "dealstage",
    "pipeline",
    "createdate",
    "closedate",
    "hs_is_closed",
    "hs_is_closed_won",
    "hs_v2_date_entered_current_stage",
    "hubspot_owner_id",
    "hs_lastmodifieddate",
    "name",
    "domain",
    "notes_last_updated",
    "notes_last_contacted",
    "hs_last_sales_activity_timestamp",
    "hs_v2_latest_time_in_customer",
    "lifecyclestage",
}

#: A HubSpot custom stage id is a long number or a UUID.
_PORTAL_ID = re.compile(r"\b\d{7,}\b|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}")


@pytest.fixture
def hubspot_config(monkeypatch):
    """Change only this test's typed workspace connector configuration."""
    from rubik_core.retrieval import (
        closed_won,
        hubspot_api,
        hubspot_properties,
        hubspot_write,
    )
    from rubik_core.tenancy import hubspot_config as store
    from rubik_core.xray import properties as xray_properties

    current = HubSpotConnectorConfig()

    def get_config():
        return current

    for module in (
        store,
        closed_won,
        hubspot_api,
        hubspot_properties,
        hubspot_write,
        xray_properties,
    ):
        monkeypatch.setattr(module, "get_hubspot_config", get_config)

    def set_config(**changes):
        nonlocal current
        current = replace(current, **changes)
        return current

    return set_config


# ── Won stages ───────────────────────────────────────────────────────────────


def test_closed_won_is_the_built_in_stage_alone_by_default(hubspot_config):
    from rubik_core.retrieval import closed_won

    hubspot_config(closed_won_stage_ids=())

    condition = closed_won.closed_won_condition()
    assert "'closedwon'" in condition
    assert "@closed_won_stage_ids" in condition
    assert not _PORTAL_ID.search(condition)
    assert closed_won.closed_won_stage_ids() == ()
    (param,) = closed_won.closed_won_job_config().query_parameters
    assert param.name == "closed_won_stage_ids"
    assert list(param.values) == []


def test_a_configured_won_stage_is_bound_not_interpolated(hubspot_config):
    from rubik_core.retrieval import closed_won

    hubspot_config(closed_won_stage_ids=("4242", "won-x"))

    assert closed_won.closed_won_stage_ids() == ("4242", "won-x")
    condition = closed_won.closed_won_condition(alias="deal")
    assert "deal.dealstage IN UNNEST(@closed_won_stage_ids)" in condition
    assert "4242" not in condition  # bound as a parameter, never in the SQL
    (param,) = closed_won.closed_won_job_config().query_parameters
    assert list(param.values) == ["4242", "won-x"]


def test_the_crm_reports_bind_the_won_stages(hubspot_config):
    """The BigQuery won/open reports in chat run through the one helper."""
    from rubik_core.retrieval.hubspot import HubSpotIntentSQLGenerator, HubSpotRetriever

    hubspot_config(closed_won_stage_ids=("4242",))
    sql = HubSpotIntentSQLGenerator(project="proj").generate_sql(
        {"intent": "deals_won", "timeframe": {"start_date": "2026-01-01"}}
    )
    assert "UNNEST(@closed_won_stage_ids)" in sql
    assert not _PORTAL_ID.search(sql)

    retriever = HubSpotRetriever.__new__(HubSpotRetriever)
    retriever.bq = MagicMock()
    retriever.bq.query.return_value = []
    retriever._execute_and_format_sql(sql)
    config = retriever.bq.query.call_args.kwargs["job_config"]
    assert [list(p.values) for p in config.query_parameters] == [["4242"]]


# ── Stage lag ────────────────────────────────────────────────────────────────


def test_stage_lag_with_no_stages_configured_finds_nothing_and_reads_nothing(
    hubspot_config,
):
    from rubik_core.cockpit import stage_lag

    hubspot_config(stage_lag_stages="")
    hs = MagicMock()

    outcome = stage_lag.detect_outcome(MagicMock(), "D1", hs=hs)

    assert outcome == stage_lag.StageLagOutcome(None, "not_configured")
    assert "not_configured" in stage_lag.OUTCOME_REASONS
    hs.get_object.assert_not_called()
    assert set(stage_lag.SIGNAL_TO_STAGE.values()) == {None}


def test_the_stage_lag_sweep_reports_an_unconfigured_detector(
    monkeypatch, hubspot_config
):
    from backend.jobs import sales_kaizen

    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    hubspot_config(stage_lag_stages="")

    report = sales_kaizen.run_stage_lag(MagicMock())

    assert report["status"] == sales_kaizen.STATUS_DISABLED
    assert report["flag"] == "COCKPIT_STAGE_LAG_STAGES"


def test_configured_stages_give_the_order_and_the_signals(hubspot_config):
    from rubik_core.cockpit import stage_lag

    hubspot_config(
        stage_lag_stages="quote, drafting, sent=sow_sent, legal=moved_to_legal_review, "
        "signoff=pending_signature",
    )
    stages = stage_lag.configured_stages()

    assert stages.order == ("quote", "drafting", "sent", "legal", "signoff")
    assert stage_lag.SIGNAL_TO_STAGE["sow_sent"] == "sent"
    assert stage_lag.SIGNAL_TO_STAGE["moved_to_legal_review"] == "legal"
    assert stage_lag.SIGNAL_TO_STAGE["ops_involved"] is None
    assert stage_lag.SIGNAL_TO_STAGE["verbal_agreement"] is None
    # A verbal agreement lags only before the SOW Sent stage.
    assert stages.unmapped_boundary() == stages.index("drafting")
    assert stage_lag.signal_for_stage("legal") == "moved_to_legal_review"
    with pytest.raises(HubSpotConfigInvalid):
        hubspot_config(stage_lag_stages="stray=not_a_signal")


def _deal(stage: str) -> dict:
    return {
        "properties": {"pipeline": "default", "dealstage": stage, "hs_is_closed": ""}
    }


def test_configured_stages_drive_a_finding(monkeypatch, hubspot_config):
    """Evidence of legal review on a deal still at drafting is a lag."""
    from rubik_core.cockpit import stage_lag

    hubspot_config(
        stage_lag_stages="drafting,sent=sow_sent,legal=moved_to_legal_review"
    )
    monkeypatch.setattr(stage_lag, "_read_deal", lambda deal_id, hs: _deal("drafting"))
    evidence = stage_lag._Evidence(
        signal="moved_to_legal_review",
        stage_id=stage_lag.SIGNAL_TO_STAGE["moved_to_legal_review"],
        text="our counsel will send redlines",
        ref="e1",
        confidence=0.9,
        observed_at=None,
    )
    monkeypatch.setattr(
        stage_lag, "_gather_evidence", lambda db, deal_id, now: [evidence]
    )
    monkeypatch.setattr(stage_lag, "_is_suppressed_by_rejection", lambda *a: False)
    monkeypatch.setattr(stage_lag, "_existing", lambda *a: [])
    db = MagicMock()

    outcome = stage_lag.detect_outcome(db, "D1", hs=MagicMock())

    assert outcome.reason == "emitted"
    assert outcome.exception.evidence_stage_id == "legal"
    assert outcome.exception.hubspot_stage_id == "drafting"


# ── The company's own fields: none named by default ─────────────────────────


@pytest.fixture
def no_fields_named(hubspot_config):
    hubspot_config()


@pytest.fixture
def fields_named(hubspot_config):
    """A workspace that named a field for every role (fictional names)."""
    hubspot_config(
        deal_product_property="services_sold",
        deal_turnaround_property="delivery_time",
        xray_renewal_date_property="renews_on",
        xray_auto_renew_properties=("auto_renews", "evergreen"),
        xray_account_status_properties=("account_status",),
        xray_csm_owner_property="success_manager",
        xray_extra_deal_properties=("seats", "renews_on"),
        xray_extra_company_properties=("segment",),
    )


def test_a_named_field_must_be_an_internal_name(hubspot_config):
    """A typo never reaches an API body or a BigQuery column."""
    from rubik_core.retrieval import hubspot_properties

    with pytest.raises(HubSpotConfigInvalid):
        hubspot_config(
            xray_account_status_properties=("account_status", "x; DROP TABLE d")
        )
    with pytest.raises(HubSpotConfigInvalid):
        hubspot_config(deal_product_property="rd.x OR 1=1")
    assert hubspot_properties.deal_product_property() == ""


def test_xray_reads_only_hubspot_built_ins_by_default(no_fields_named):
    from rubik_core.xray import fetchers, properties

    assert set(fetchers.deal_properties()) <= HUBSPOT_BUILT_IN
    assert set(fetchers.company_properties()) <= HUBSPOT_BUILT_IN
    assert properties.search_properties() == ["name", "domain"]


def test_xray_asks_for_every_named_field_once(fields_named):
    from rubik_core.xray import fetchers

    deal = fetchers.deal_properties()
    company = fetchers.company_properties()

    for name in ("services_sold", "renews_on", "auto_renews", "evergreen", "seats"):
        assert deal.count(name) == 1, name
    for name in ("account_status", "success_manager", "segment"):
        assert company.count(name) == 1, name
    assert "delivery_time" not in deal  # only the turn-times answer reads it


class _FakeXrayClient:
    """Answers the X-Ray fetch with one company, one deal and an owner list."""

    base_url = "https://api.hubapi.example"

    def __init__(self, *a, **k):
        pass

    def search_objects(self, obj, query, props, limit=20):
        self.asked = list(props)
        return [{"id": "1", "properties": _COMPANY}]

    def owner_name_map(self):
        return {"77": "Robin Success"}

    def fully_owned_deal_associations(self, _object_type, ids):
        return {str(item_id): ["d1"] for item_id in ids}


_COMPANY = {
    "name": "Acme",
    "domain": "acme.example",
    "account_status": "Active",
    "success_manager": "77",
    "segment": "Mid-market",
}
_DEAL = {
    "dealname": "Acme renewal",
    "hs_is_closed": "false",
    "services_sold": "Support plan",
    "renews_on": "2030-01-31",
    "auto_renews": "false",
    "evergreen": "true",
    "seats": "40",
}


def _fake_bundle(monkeypatch):
    from rubik_core.graph import writers
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig
    from rubik_core.xray import fetchers

    monkeypatch.setattr(fetchers, "HubSpotAPIClient", _FakeXrayClient)
    monkeypatch.setattr(
        fetchers,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )
    monkeypatch.setattr(fetchers, "_associated_ids", lambda c, i, obj, **k: (["d1"], 1))

    def ownership_read(client, obj, ids, props):
        if obj == "deals":
            return [{"id": "d1", "properties": {"pipeline": "default"}}]
        return []

    monkeypatch.setattr(fetchers, "_raw_batch_read", ownership_read)

    def batch_read(client, obj, ids, props):
        record = {"companies": _COMPANY, "deals": _DEAL}.get(obj, {})
        return [
            {"id": "d1", "properties": {k: record[k] for k in props if k in record}}
        ]

    monkeypatch.setattr(fetchers, "_batch_read", batch_read)
    monkeypatch.setattr(writers, "record_company_bundle", lambda *a, **k: None)
    return fetchers.fetch_company_bundle("1")


def test_xray_reads_status_and_csm_from_the_named_fields(fields_named, monkeypatch):
    from rubik_core.xray import fetchers

    bundle = _fake_bundle(monkeypatch)
    (row,) = fetchers.search_companies("acme")

    assert bundle.client_status == "Active"
    assert bundle.csm == "Robin Success"
    assert row["client_status"] == "Active"
    assert row["csm"] == "Robin Success"


def test_with_no_fields_named_xray_shows_no_status_csm_or_renewal(
    no_fields_named, monkeypatch
):
    from rubik_core.xray import churn, fetchers, service

    bundle = _fake_bundle(monkeypatch)
    (row,) = fetchers.search_companies("acme")
    signals = service._build_signals(bundle, churn.score(bundle))

    assert bundle.client_status == "" and bundle.csm == ""
    assert row["client_status"] == "" and row["csm"] == ""
    assert signals["renewal_countdown_days"] is None
    assert signals["active_products"] == []


def test_the_xray_brief_shows_the_named_fields(fields_named, monkeypatch):
    from rubik_core.xray import churn, prompts, service

    bundle = _fake_bundle(monkeypatch)
    company = prompts.format_company_block(bundle)
    deals = prompts.format_deals_block(bundle)
    signals = service._build_signals(bundle, churn.score(bundle))
    renewal = next(
        f for f in churn.score(bundle).factors if f.name == "renewal_exposure"
    )

    assert "Client status: Active" in company
    assert "CSM: Robin Success" in company
    assert "segment: Mid-market" in company
    assert "| Support plan | renewal=2030-01-31 |" in deals
    assert "seats=40" in deals
    assert signals["renewal_countdown_days"] > 0
    assert "auto-renew=yes" in renewal.evidence  # any named auto-renew field


def test_the_xray_company_search_reads_built_ins_by_default(
    no_fields_named, monkeypatch
):
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig
    from rubik_core.xray import fetchers

    asked: list[list[str]] = []
    owners_read: list[bool] = []

    class FakeClient:
        def search_objects(self, obj, query, props, limit=20):
            asked.append(list(props))
            return [
                {"id": "1", "properties": {"name": "Acme", "domain": "acme.example"}}
            ]

        def owner_name_map(self):
            owners_read.append(True)
            return {}

    monkeypatch.setattr(fetchers, "HubSpotAPIClient", FakeClient)
    monkeypatch.setattr(
        fetchers,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )
    monkeypatch.setattr(fetchers, "_authorized_deals", lambda *_a: ["d1"])

    (row,) = fetchers.search_companies("acme")

    assert set(asked[0]) <= HUBSPOT_BUILT_IN
    assert row["name"] == "Acme"
    assert owners_read == []  # no CSM field named: no owner lookup


# ── Ask Cubie, AI Priorities and the CRM reports ─────────────────────────────


class _FakeSearchClient:
    def __init__(self, deals):
        self.deals = deals
        self.calls: list[tuple[list, list]] = []

    def search_all(self, obj, filters, props, sorts=None):
        self.calls.append((filters, list(props)))
        return self.deals


def test_turn_times_say_they_are_not_set_up_and_read_nothing(no_fields_named):
    from rubik_core.retrieval import hubspot_api

    client = _FakeSearchClient([])

    answer = hubspot_api._kpi_turn_times_by_product(client, (None, None, ""), None, "")

    assert answer == hubspot_api.TURN_TIMES_NOT_SET_UP
    assert client.calls == []


def test_turn_times_read_the_named_fields(fields_named):
    from rubik_core.retrieval import hubspot_api

    day_ms = 86_400_000
    client = _FakeSearchClient(
        [
            {
                "properties": {
                    "delivery_time": str(4 * day_ms),
                    "services_sold": "Audit",
                }
            },
            {
                "properties": {
                    "delivery_time": str(6 * day_ms),
                    "services_sold": "Audit",
                }
            },
        ]
    )

    answer = hubspot_api._kpi_turn_times_by_product(client, (None, None, ""), None, "")

    ((filters, props),) = client.calls
    assert {"propertyName": "delivery_time", "operator": "HAS_PROPERTY"} in filters
    assert props == ["delivery_time", "services_sold"]
    assert "- Audit: median **5d** (n=2)" in answer


def test_turn_times_without_a_product_field_group_every_deal(
    fields_named, hubspot_config
):
    from rubik_core.retrieval import hubspot_api

    hubspot_config(deal_product_property="")
    client = _FakeSearchClient([{"properties": {"delivery_time": str(86_400_000)}}])

    answer = hubspot_api._kpi_turn_times_by_product(client, (None, None, ""), None, "")

    assert client.calls[0][1] == ["delivery_time"]
    assert "- All won deals: median **1d** (n=1)" in answer


def test_the_planner_filters_on_a_product_not_a_field_name():
    from rubik_core.retrieval import hubspot_api

    _search, dropped = hubspot_api._modeled_filters(
        [{"field": "product", "value": "Audit"}]
    )
    assert dropped == ["product"]


def _live_deal() -> dict:
    """A quiet, stuck open deal, as the live HubSpot search returns it."""
    now = datetime.now(timezone.utc)
    return {
        "id": "d-1",
        "properties": {
            "hs_object_id": "d-1",
            "dealname": "Acme expansion",
            "dealstage": "s1",
            "services_sold": "Audit",
            "notes_last_contacted": (now - timedelta(days=24)).isoformat(),
            "hs_v2_date_entered_current_stage": (now - timedelta(days=40)).isoformat(),
        },
    }


def test_priorities_read_products_only_from_the_named_field(
    fields_named, hubspot_config
):
    from backend.signals.hubspot import _live_rows

    stale, stuck, at_risk = _live_rows([_live_deal()], "me@acme.example", "1", {})
    rows = stale + stuck + at_risk
    assert rows and all(r["products"] == "Audit" for r in rows)

    hubspot_config(deal_product_property="")
    stale, stuck, at_risk = _live_rows([_live_deal()], "me@acme.example", "1", {})
    assert all(r["products"] is None for r in stale + stuck + at_risk)


def test_the_stale_deal_report_reads_the_named_product_column(
    monkeypatch, hubspot_config
):
    from backend.agents import sales_hubspot

    monkeypatch.setattr(sales_hubspot, "require_workspace_crm_bigquery", lambda: None)
    monkeypatch.setattr(settings, "BIGQUERY_PROJECT_ID", "proj")
    seen: list[str] = []
    bq = MagicMock()
    bq.query.side_effect = lambda sql: seen.append(sql) or []

    hubspot_config(deal_product_property="")
    sales_hubspot.fetch_stale_open_deals(bq)
    hubspot_config(deal_product_property="services_sold")
    sales_hubspot.fetch_stale_open_deals(bq)

    assert "CAST(NULL AS STRING) AS products" in seen[0]
    assert "rd.services_sold AS products" in seen[1]


def test_the_crm_report_product_filter_uses_the_named_field(hubspot_config):
    from rubik_core.retrieval.hubspot import HubSpotIntentSQLGenerator

    generator = HubSpotIntentSQLGenerator(project="proj")
    filters = {"product_line": "Audit"}

    hubspot_config(deal_product_property="")
    assert generator.build_where_clause(filters) == "WHERE 1=1"
    hubspot_config(deal_product_property="services_sold")
    assert generator.build_where_clause(filters) == (
        "WHERE 1=1 AND rd.services_sold LIKE '%Audit%'"
    )


# ── The write fences carry no custom property by default ─────────────────────


def test_the_company_write_fence_is_empty_by_default(hubspot_config):
    from rubik_core.retrieval import hubspot_write

    hubspot_config(ccs_vendor_fields_default=(), ccs_vendor_fields_extended=())

    assert hubspot_write.company_writable() == frozenset()
    with pytest.raises(hubspot_write.HubSpotWriteRefused):
        hubspot_write.HubSpotWriteClient._property_gate(
            "company", {"anything": "x"}, hubspot_write.company_writable()
        )


def test_the_company_write_fence_is_the_configured_vendor_fields(hubspot_config):
    from rubik_core.retrieval import hubspot_write

    with pytest.raises(HubSpotConfigInvalid):
        hubspot_config(ccs_vendor_fields_default=("payroll_vendor", "lifecyclestage"))
    with pytest.raises(HubSpotConfigInvalid):
        hubspot_config(
            ccs_vendor_fields_default=(hubspot_write.RESEARCH_LINK_PROPERTY,)
        )
    hubspot_config(
        ccs_vendor_fields_default=("payroll_vendor",),
        ccs_vendor_fields_extended=("it_vendor",),
    )

    # Built-ins that drive HubSpot automation and X-Ray's own link never enter.
    assert hubspot_write.company_writable() == frozenset(
        {"payroll_vendor", "it_vendor"}
    )


def test_the_lead_fence_carries_no_custom_property():
    from rubik_core.retrieval import hubspot_write

    assert all(
        name.startswith("hs_") or name == "hubspot_owner_id"
        for name in hubspot_write.LEAD_WRITABLE
    )


def test_call_capture_titles_a_vendor_field_from_its_internal_name():
    """No per-portal label table: a field's heading comes from its name."""
    from backend.routers import calls

    assert calls._vendor_label("payroll_vendor") == "Payroll Vendor"
    assert not hasattr(calls, "_VENDOR_LABEL_OVERRIDES")
