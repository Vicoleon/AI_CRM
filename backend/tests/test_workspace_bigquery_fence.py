"""Legacy global CRM BigQuery settings cannot cross a workspace boundary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from rubik_core.tenancy.context import use_workspace


@pytest.mark.parametrize("slug", ["acme", "beta"])
def test_global_mirror_and_data_agent_stop_before_cloud_clients(monkeypatch, slug):
    from backend.agents import sales_hubspot
    from backend.signals.hubspot import HubspotSignalSource
    from rubik_core.adapters import bigquery as adapter_module
    from rubik_core.config import settings
    from rubik_core.retrieval import bq_data_agent

    monkeypatch.setattr(settings, "BIGQUERY_PROJECT_ID", "legacy-project")
    monkeypatch.setattr(settings, "BIGQUERY_DATASET", "hubspot_raw")
    monkeypatch.setattr(settings, "BQ_DATA_AGENT_RESOURCE", "legacy-agent")
    monkeypatch.setattr(settings, "USE_BQ_DATA_AGENT", True)
    bq_client = Mock(side_effect=AssertionError("BigQuery client was created"))
    adc = Mock(side_effect=AssertionError("ADC was requested"))
    agent_http = Mock(side_effect=AssertionError("Agent HTTP request was made"))
    monkeypatch.setattr(adapter_module.bigquery, "Client", bq_client)
    monkeypatch.setattr(bq_data_agent.google.auth, "default", adc)
    monkeypatch.setattr(bq_data_agent.httpx, "Client", agent_http)
    injected_bq = Mock()

    with use_workspace(slug):
        with pytest.raises(adapter_module.WorkspaceBigQueryUnavailable):
            adapter_module.BigQueryAdapter()
        orphaned_adapter = adapter_module.BigQueryAdapter.__new__(
            adapter_module.BigQueryAdapter
        )
        orphaned_adapter.client = injected_bq
        with pytest.raises(adapter_module.WorkspaceBigQueryUnavailable):
            orphaned_adapter.query("SELECT * FROM legacy-project.hubspot_raw.tbl_deals")
        with pytest.raises(bq_data_agent.BQDataAgentError, match="workspace-bound"):
            bq_data_agent.query_hubspot_via_agent("pipeline")
        assert sales_hubspot.fetch_stale_open_deals(injected_bq) == []
        assert (
            HubspotSignalSource(bq=injected_bq)._fetch_bigquery(
                "rep@example.com", since=datetime.now(timezone.utc)
            )
            == []
        )

    bq_client.assert_not_called()
    adc.assert_not_called()
    agent_http.assert_not_called()
    injected_bq.query.assert_not_called()


@pytest.mark.parametrize("slug", ["acme", "beta"])
def test_flagged_chat_uses_current_workspace_live_token_only(monkeypatch, slug):
    from rubik_core.adk_agent import tools
    from rubik_core.config import settings
    from rubik_core.retrieval import bq_data_agent, hubspot, hubspot_api
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(settings, "USE_BQ_DATA_AGENT", True)
    monkeypatch.setattr(
        tools, "workspace_secret", lambda *_: f"{current_workspace()}-token"
    )
    mirror = Mock(side_effect=AssertionError("Legacy mirror was selected"))
    agent = Mock(side_effect=AssertionError("BQ Data Agent was selected"))
    monkeypatch.setattr(hubspot, "HubSpotRetriever", mirror)
    monkeypatch.setattr(bq_data_agent, "query_hubspot_via_agent", agent)

    class LiveRetriever:
        def retrieve(self, _query, *, caller_email):
            assert caller_email == "rep@example.com"
            return f"{current_workspace()} live CRM", None

    monkeypatch.setattr(hubspot_api, "HubSpotAPIRetriever", LiveRetriever)
    with use_workspace(slug):
        result = asyncio.run(
            tools.AgentTools("rep@example.com").search_hubspot_data("pipeline")
        )

    assert result == {"status": "success", "report": f"{slug} live CRM"}
    mirror.assert_not_called()
    agent.assert_not_called()


@pytest.mark.parametrize("slug", ["acme", "beta"])
def test_flagged_chat_without_workspace_token_has_no_crm_data(monkeypatch, slug):
    from rubik_core.adk_agent import tools
    from rubik_core.config import settings
    from rubik_core.retrieval import bq_data_agent, hubspot, hubspot_api

    monkeypatch.setattr(settings, "USE_BQ_DATA_AGENT", True)
    monkeypatch.setattr(tools, "workspace_secret", lambda *_: None)
    mirror = Mock(side_effect=AssertionError("Legacy mirror was selected"))
    agent = Mock(side_effect=AssertionError("BQ Data Agent was selected"))
    live = Mock(side_effect=AssertionError("Live HubSpot was selected"))
    monkeypatch.setattr(hubspot, "HubSpotRetriever", mirror)
    monkeypatch.setattr(bq_data_agent, "query_hubspot_via_agent", agent)
    monkeypatch.setattr(hubspot_api, "HubSpotAPIRetriever", live)

    with use_workspace(slug):
        result = asyncio.run(
            tools.AgentTools("rep@example.com").search_hubspot_data("pipeline")
        )

    assert result == {
        "status": "error",
        "error_message": "HubSpot read token is not configured for this workspace",
    }
    mirror.assert_not_called()
    agent.assert_not_called()
    live.assert_not_called()


def test_mcp_crm_tools_use_live_workspace_token_only(monkeypatch):
    from rubik_core.mcp_servers import hubspot_server
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(
        "rubik_core.tenancy.secrets.workspace_secret",
        lambda *_: "token" if current_workspace() == "acme" else None,
    )

    class LiveRetriever:
        def retrieve(self, _query):
            return "acme live CRM", None

    class LiveClient:
        def deal_stage_labels(self):
            return {"s1": "Discovery"}

    monkeypatch.setattr(hubspot_api, "HubSpotAPIRetriever", LiveRetriever)
    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", LiveClient)

    with use_workspace("acme"):
        assert hubspot_server.search_hubspot_data("pipeline") == "acme live CRM"
        assert hubspot_server.get_deal_stages() == "Deal Stages:\n- Discovery (ID: s1)"
    with use_workspace("beta"):
        assert "not configured" in hubspot_server.search_hubspot_data("pipeline")
        assert "not configured" in hubspot_server.get_deal_stages()
