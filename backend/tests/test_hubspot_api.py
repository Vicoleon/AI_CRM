"""Unit tests for the live HubSpot REST client's full-text search + the chat
contact-lookup tool.

Guards the fix for "I couldn't find a contact record for Arden Webb" — the
record existed in HubSpot but the app had no by-name contact lookup
(`search_hubspot_data` is scoped to pipeline analytics only). `find_hubspot_contact`
adds one, using HubSpot's top-level `query` full-text search so a NAME or an
EMAIL both resolve the same person (the legacy path searched firstname/lastname
only, which is exactly why an email-bearing contact was missed).
"""

import asyncio
from zoneinfo import ZoneInfo

import pytest

from rubik_core.adk_agent import tools as agent_tools_module
from rubik_core.adk_agent.tools import AgentTools
from rubik_core.retrieval import hubspot_api
from rubik_core.retrieval.hubspot_api import (
    CONTACT_SEARCH_PROPERTIES,
    HubSpotAPIClient,
)
from rubik_core.tenancy import hubspot_config as connector_config
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig


@pytest.fixture(autouse=True)
def _workspace_pipelines(monkeypatch):
    def configured():
        return HubSpotConnectorConfig(pipeline_ids=("default",))

    monkeypatch.setattr(connector_config, "get_hubspot_config", configured)
    monkeypatch.setattr(hubspot_api, "get_hubspot_config", configured)


def test_search_objects_uses_full_text_query_body(monkeypatch):
    """search_objects must post HubSpot's top-level `query` (cross-property
    full-text search that spans name + email), NOT a name-only filterGroup."""
    captured = {}

    def fake_search(self, object_type, body):
        captured["object_type"] = object_type
        captured["body"] = body
        return {"results": [{"id": "1", "properties": {"email": "k@x.example"}}]}

    monkeypatch.setattr(HubSpotAPIClient, "_search", fake_search)

    client = HubSpotAPIClient(token="tok")
    results = client.search_objects(
        "contacts", "Arden Webb", CONTACT_SEARCH_PROPERTIES, limit=5
    )

    assert captured["object_type"] == "contacts"
    assert captured["body"]["query"] == "Arden Webb"
    assert "filterGroups" not in captured["body"]
    assert captured["body"]["limit"] == 5
    assert captured["body"]["properties"] == CONTACT_SEARCH_PROPERTIES
    assert results[0]["properties"]["email"] == "k@x.example"


def test_default_client_reads_workspace_token_not_process_token(monkeypatch):
    seen = []
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "process-token")
    monkeypatch.setattr(
        hubspot_api,
        "workspace_secret",
        lambda connector, field: seen.append((connector, field)) or "workspace-token",
        raising=False,
    )

    client = HubSpotAPIClient()

    assert client.token == "workspace-token"
    assert seen == [("hubspot", "read_token")]


def test_find_hubspot_contact_formats_matches(monkeypatch):
    monkeypatch.setattr(
        hubspot_api, "workspace_secret", lambda *_: "tok", raising=False
    )
    monkeypatch.setattr(agent_tools_module, "workspace_secret", lambda *_: "tok")

    def fake_search_objects(self, object_type, query, properties, limit=10):
        return [
            {
                "id": "own-contact",
                "properties": {
                    "firstname": "Arden",
                    "lastname": "Webb",
                    "email": "arden.webb@example.com",
                    "company": "Example Industries",
                },
            },
            {
                "id": "foreign-contact",
                "properties": {"firstname": "Foreign", "email": "foreign@example.com"},
            },
        ]

    monkeypatch.setattr(
        hubspot_api.HubSpotAPIClient, "search_objects", fake_search_objects
    )
    monkeypatch.setattr(
        hubspot_api.HubSpotAPIClient,
        "fully_owned_deal_associations",
        lambda self, from_type, ids: {
            "own-contact": ["own-deal"],
            "foreign-contact": [],
        },
    )

    tools = AgentTools(user_email="me@acme.example")
    result = asyncio.run(tools.find_hubspot_contact("Arden Webb"))

    assert result["status"] == "success"
    assert "Arden Webb" in result["report"]
    assert "arden.webb@example.com" in result["report"]
    assert "Example Industries" in result["report"]
    assert "foreign@example.com" not in result["report"]


def test_find_hubspot_contact_reports_no_match(monkeypatch):
    monkeypatch.setattr(
        hubspot_api, "workspace_secret", lambda *_: "tok", raising=False
    )
    monkeypatch.setattr(agent_tools_module, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(
        hubspot_api.HubSpotAPIClient,
        "search_objects",
        lambda self, *a, **k: [],
    )
    tools = AgentTools(user_email="me@acme.example")
    result = asyncio.run(tools.find_hubspot_contact("Nobody Here"))

    assert result["status"] == "success"
    assert "No HubSpot contact matched" in result["report"]


def test_find_hubspot_contact_requires_token(monkeypatch):
    """No token → a clean error, never a crash."""
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: None, raising=False)
    monkeypatch.setattr(agent_tools_module, "workspace_secret", lambda *_: None)
    tools = AgentTools(user_email="me@acme.example")
    result = asyncio.run(tools.find_hubspot_contact("Arden Webb"))

    assert result["status"] == "error"


def test_find_hubspot_contact_without_pipelines_never_constructs_client(monkeypatch):
    monkeypatch.setattr(connector_config, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(
        agent_tools_module,
        "workspace_secret",
        lambda *_: (_ for _ in ()).throw(AssertionError("read token requested")),
    )
    monkeypatch.setattr(
        hubspot_api,
        "HubSpotAPIClient",
        lambda: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    result = asyncio.run(
        AgentTools(user_email="me@acme.example").find_hubspot_contact("Pat")
    )
    assert result["status"] == "error"
    assert "not configured" in result["error_message"]


# ── The business time zone the KPI windows are cut in ───────────────────────


def test_kpi_windows_use_the_workspace_time_zone(monkeypatch):
    from rubik_core.tenancy import workspace_time

    monkeypatch.setattr(
        workspace_time, "workspace_time_zone", lambda: ZoneInfo("Asia/Tokyo")
    )
    assert str(hubspot_api._tz()) == "Asia/Tokyo"


def test_kpi_windows_refuse_unavailable_workspace_time_zone(monkeypatch):
    from rubik_core.tenancy import workspace_time

    def unavailable():
        raise workspace_time.WorkspaceTimeUnavailable("control unavailable")

    monkeypatch.setattr(workspace_time, "workspace_time_zone", unavailable)
    with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
        hubspot_api._tz()
