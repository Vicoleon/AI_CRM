"""Workspace pipeline boundaries for HubSpot deal readers."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.routers import cockpit, home
from backend.signals import hubspot as signals_hubspot
from rubik_core.crm_sync import hubspot_context
from rubik_core.crm_sync import service as crm_sync_service
from rubik_core.loop_closure import hubspot_index
from rubik_core.scorecard_sync import hubspot_green
from rubik_core.tenancy.context import current_workspace, use_workspace
from rubik_core.tenancy.hubspot_config import (
    HubSpotConfigUnavailable,
    HubSpotConnectorConfig,
)


def test_live_signals_refuse_deals_without_workspace_pipelines(monkeypatch):
    from rubik_core.retrieval import hubspot_api

    monkeypatch.setattr(signals_hubspot, "workspace_secret", lambda *_: "token")
    monkeypatch.setattr(
        signals_hubspot, "get_hubspot_config", lambda: HubSpotConnectorConfig()
    )

    def no_client(*_args, **_kwargs):
        raise AssertionError("HubSpot client must not be constructed")

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", no_client)
    source = signals_hubspot.HubspotSignalSource()
    assert source.fetch("rep@example.com", since=datetime.now(timezone.utc)) == []


def test_cockpit_deal_labels_are_scoped_server_and_client_side(monkeypatch):
    from rubik_core.retrieval import hubspot_api

    monkeypatch.setattr(
        cockpit,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("workspace-a",)),
    )
    searches = []

    class Client:
        def search_all(self, object_type, filters, properties):
            searches.append((object_type, filters, properties))
            return [
                {
                    "id": "own",
                    "properties": {
                        "dealname": "Own deal",
                        "amount": "100",
                        "pipeline": "workspace-a",
                    },
                },
                {
                    "id": "foreign",
                    "properties": {
                        "dealname": "Foreign deal",
                        "amount": "900",
                        "pipeline": "workspace-b",
                    },
                },
            ]

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", Client)
    result = cockpit._crm_context(
        owner_email=None,
        deal_ids=["own", "foreign"],
        company_ids=[],
    )
    assert result.deal_names == {"own": "Own deal"}
    assert result.deal_amounts == {"own": 100.0}
    assert searches[0][0] == "deals"
    assert {
        "propertyName": "pipeline",
        "operator": "IN",
        "values": ["workspace-a"],
    } in searches[0][1]
    assert "pipeline" in searches[0][2]


def test_cockpit_empty_pipeline_skips_hubspot(monkeypatch):
    from rubik_core.retrieval import hubspot_api

    monkeypatch.setattr(cockpit, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(
        hubspot_api,
        "HubSpotAPIClient",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected HubSpot client")),
    )
    result = cockpit._crm_context(
        owner_email=None, deal_ids=["foreign"], company_ids=[]
    )
    assert result.available is False


def test_cockpit_company_labels_require_live_full_company_ownership(monkeypatch):
    from rubik_core.retrieval import hubspot_api
    from rubik_core.xray import fetchers

    monkeypatch.setattr(
        cockpit,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("workspace-a",)),
    )
    monkeypatch.setattr(
        fetchers, "visible_company_ids", lambda ids: {"own"}.intersection(ids)
    )
    searches = []

    class Client:
        def search_all(self, object_type, filters, properties):
            searches.append((object_type, filters))
            if object_type != "companies":
                return []
            return [
                {"id": "own", "properties": {"name": "Acme"}},
                {"id": "foreign", "properties": {"name": "Client B"}},
            ]

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", Client)
    result = cockpit._crm_context(
        owner_email=None, deal_ids=[], company_ids=["own", "foreign"]
    )
    assert result.company_names == {"own": "Acme"}
    assert result.partial is True
    assert searches[0][0] == "companies"
    assert searches[0][1][0]["values"] == ["own"]
    assert cockpit._company_label(result, "foreign") is None

    stale_session = SimpleNamespace(
        hubspot_company_id="foreign",
        hubspot_deal_id=None,
        attrs={"company_name": "Client B"},
    )
    assert cockpit._session_label(result, stale_session) == "no CRM record yet"


def test_cockpit_deal_labels_and_links_follow_selected_workspace(monkeypatch):
    from rubik_core.retrieval import hubspot_api

    configs = {
        "alpha": HubSpotConnectorConfig(pipeline_ids=("alpha-pipe",), portal_id="101"),
        "beta": HubSpotConnectorConfig(pipeline_ids=("beta-pipe",)),
    }
    monkeypatch.setattr(
        cockpit, "get_hubspot_config", lambda: configs[current_workspace()]
    )

    class Client:
        def search_all(self, object_type, filters, properties):
            assert object_type == "deals"
            return [
                {
                    "id": "a1",
                    "properties": {"dealname": "Alpha", "pipeline": "alpha-pipe"},
                },
                {
                    "id": "b1",
                    "properties": {"dealname": "Beta", "pipeline": "beta-pipe"},
                },
            ]

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", Client)
    with use_workspace("alpha"):
        alpha = cockpit._crm_context(
            owner_email=None, deal_ids=["a1", "b1"], company_ids=[]
        )
        assert alpha.deal_names == {"a1": "Alpha"}
        assert cockpit._hubspot_deal_url("a1") == (
            "https://app.hubspot.com/contacts/101/deal/a1"
        )
    with use_workspace("beta"):
        beta = cockpit._crm_context(
            owner_email=None, deal_ids=["a1", "b1"], company_ids=[]
        )
        assert beta.deal_names == {"b1": "Beta"}
        assert cockpit._hubspot_deal_url("b1") is None


def test_cockpit_link_refuses_unavailable_workspace_config(monkeypatch):
    def unavailable():
        raise HubSpotConfigUnavailable("control store unavailable")

    monkeypatch.setattr(cockpit, "get_hubspot_config", unavailable)
    assert cockpit._hubspot_deal_url("a1") is None


def test_crm_sync_context_empty_pipeline_skips_deals(monkeypatch):
    monkeypatch.setattr(hubspot_context, "get_hubspot_config", HubSpotConnectorConfig)

    class Client:
        def search_all(self, *_args, **_kwargs):
            raise AssertionError("unexpected HubSpot deal search")

    assert (
        hubspot_context.fetch_deals_for_companies(
            Client(), ["company"], ae_owner_id=None, stage_labels={}, owner_names={}
        )
        == []
    )


def test_crm_sync_scan_empty_pipeline_fails_before_external_reads(monkeypatch):
    monkeypatch.setattr(crm_sync_service, "get_hubspot_config", HubSpotConnectorConfig)
    finished = MagicMock()
    monkeypatch.setattr(crm_sync_service.run_state, "mark_finished", finished)
    transcripts = MagicMock()
    hubspot = MagicMock()
    stats = crm_sync_service.run_scan(
        "run-1",
        "rep@example.com",
        transcript_service=transcripts,
        hubspot_client=hubspot,
    )
    assert stats["transcripts_scanned"] == 0
    assert transcripts.mock_calls == []
    assert hubspot.mock_calls == []
    assert finished.call_args.kwargs["succeeded"] is False


def test_scorecard_sync_empty_pipeline_skips_hubspot(monkeypatch):
    config = HubSpotConnectorConfig(
        scorecard_sync_department="Sales",
        scorecard_sync_metrics={"Revenue": "company_won_amount"},
    )
    monkeypatch.setattr(hubspot_green, "get_hubspot_config", lambda: config)

    class Client:
        def owner_directory(self):
            raise AssertionError("unexpected HubSpot owner lookup")

    report = hubspot_green.sync_sales_green_metrics(db=None, client=Client())
    assert report.note == hubspot_green.NOT_CONFIGURED


def test_loop_closure_empty_pipeline_marks_index_unavailable(monkeypatch):
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(hubspot_config, "get_hubspot_config", HubSpotConnectorConfig)

    class Client:
        def owner_directory(self):
            raise AssertionError("unexpected HubSpot owner lookup")

    now = datetime.now(timezone.utc)
    result = hubspot_index.build_index(
        Client(),
        owner_emails=["rep@example.com"],
        addresses=[],
        window_start=now,
        now=now,
        today=date.today(),
        task_grace_days=3,
        max_age_days=90,
    )
    assert result.status == "unavailable"


def test_home_relationships_empty_pipeline_skips_hubspot(monkeypatch):
    from rubik_core.company import domain
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(hubspot_config, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(domain, "workspace_domains", lambda: {"internal.example"})
    monkeypatch.setattr(
        hubspot_api,
        "HubSpotAPIClient",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected HubSpot client")),
    )
    result = home._fetch_crm_relationships(
        "rep@internal.example", {"attendees": [{"email": "client@example.com"}]}
    )
    assert result == ""
