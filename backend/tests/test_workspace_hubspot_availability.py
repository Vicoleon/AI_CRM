"""Workspace CRM reads require that workspace's live HubSpot token."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock

from rubik_core.tenancy.context import use_workspace


def test_signal_source_does_not_use_mirror_when_workspace_token_is_blank(monkeypatch):
    from backend.signals import hubspot

    monkeypatch.setattr(hubspot, "workspace_secret", lambda *_: " \n ")
    source = hubspot.HubspotSignalSource()

    def unexpected_live(*_args, **_kwargs):
        raise AssertionError("Live HubSpot read was selected")

    monkeypatch.setattr(source, "_fetch_live", unexpected_live)
    mirror = Mock(side_effect=AssertionError("BigQuery mirror was selected"))
    monkeypatch.setattr(source, "_fetch_bigquery", mirror)
    assert source.fetch("user@example.com", since=datetime.now(timezone.utc)) == []
    mirror.assert_not_called()


def test_signal_source_stops_when_token_becomes_whitespace(monkeypatch):
    from backend.signals import hubspot

    token = {"value": "configured-token"}
    monkeypatch.setattr(hubspot, "workspace_secret", lambda *_: token["value"])
    source = hubspot.HubspotSignalSource()
    live = Mock(return_value=["live"])
    mirror = Mock(return_value=["mirror"])
    monkeypatch.setattr(source, "_fetch_live", live)
    monkeypatch.setattr(source, "_fetch_bigquery", mirror)
    since = datetime.now(timezone.utc)

    assert source.fetch("user@example.com", since=since) == ["live"]
    token["value"] = " \t\n "
    assert source.fetch("user@example.com", since=since) == []
    live.assert_called_once_with("user@example.com", since=since)
    mirror.assert_not_called()


def test_meeting_prep_treats_blank_workspace_token_as_unconfigured(monkeypatch):
    from rubik_core.meeting_prep import signals

    monkeypatch.setattr(signals, "workspace_secret", lambda *_: "\n\t")
    assert signals.crm_configured() is False


def test_chat_tools_do_not_open_live_hubspot_for_blank_workspace_token(
    monkeypatch,
):
    from rubik_core.adk_agent import tools
    from rubik_core.config import settings
    from rubik_core.retrieval import bq_data_agent, hubspot

    monkeypatch.setattr(tools, "workspace_secret", lambda *_: " \n")
    monkeypatch.setattr(settings, "USE_BQ_DATA_AGENT", True)

    class Mirror:
        def retrieve(self, _query):
            return "mirror answer", "SELECT 1"

    monkeypatch.setattr(hubspot, "HubSpotRetriever", Mirror)
    monkeypatch.setattr(
        bq_data_agent,
        "query_hubspot_via_agent",
        Mock(side_effect=AssertionError("BQ Data Agent was selected")),
    )
    agent = tools.AgentTools("user@example.com")
    result = asyncio.run(agent.search_hubspot_data("pipeline"))
    assert result["status"] == "error"
    assert "not configured" in result["error_message"]

    contact = asyncio.run(agent.find_hubspot_contact("Pat"))
    assert contact["status"] == "error"
    assert "not configured" in contact["error_message"]


def test_signal_directories_are_workspace_and_token_scoped():
    from backend.signals import hubspot

    hubspot._DIR_CACHE.clear()
    try:
        first = Mock(token="shared-token")
        first.owner_directory.return_value = {"1": {"email": "first@example.com"}}
        first.deal_stage_labels.return_value = {"s1": "First stage"}
        second = Mock(token="shared-token")
        second.owner_directory.return_value = {"2": {"email": "second@example.com"}}
        second.deal_stage_labels.return_value = {"s2": "Second stage"}

        with use_workspace("acme"):
            assert hubspot._cached_directories(first) == (
                first.owner_directory.return_value,
                first.deal_stage_labels.return_value,
            )
        with use_workspace("beta"):
            assert hubspot._cached_directories(second) == (
                second.owner_directory.return_value,
                second.deal_stage_labels.return_value,
            )
        with use_workspace("acme"):
            assert hubspot._cached_directories(first)[0] == {
                "1": {"email": "first@example.com"}
            }
            rotated = Mock(token="rotated-token")
            rotated.owner_directory.return_value = {"3": {"email": "new@example.com"}}
            rotated.deal_stage_labels.return_value = {"s3": "New stage"}
            assert hubspot._cached_directories(rotated)[0] == {
                "3": {"email": "new@example.com"}
            }

        first.owner_directory.assert_called_once()
        second.owner_directory.assert_called_once()
    finally:
        hubspot._DIR_CACHE.clear()
