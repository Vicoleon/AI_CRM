"""HubSpot company/domain → deal-status lookup + its grounded prompt block.

Exercises HubSpotAPIClient.company_relationships_for_domains with _search and
_deals_for_company faked, so no HubSpot API is contacted.
"""

import pytest

from rubik_core.retrieval import hubspot_api
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig


@pytest.fixture(autouse=True)
def _workspace_pipeline(monkeypatch):
    monkeypatch.setattr(
        hubspot_api,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )


def _client():
    return hubspot_api.HubSpotAPIClient(token="test-token")  # bypasses settings


def test_active_client_with_won_and_open_deals(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "_search",
        lambda obj, body: {
            "results": [
                {
                    "id": "77",
                    "properties": {
                        "name": "Initech",
                        "domain": "initech.example",
                        "lifecyclestage": "customer",
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        c,
        "_deals_for_company",
        lambda cid, allowed: [
            {
                "id": "1",
                "props": {
                    "hs_is_closed_won": "true",
                    "hs_is_closed": "true",
                    "dealname": "Initech Rollout 2024",
                    "closedate": "2024-03-01T00:00:00Z",
                    "amount": "50000",
                },
            },
            {
                "id": "2",
                "props": {
                    "hs_is_closed_won": "false",
                    "hs_is_closed": "false",
                    "dealname": "Initech Rollout Renewal",
                    "amount": "45000",
                },
            },
        ],
    )
    rels = c.company_relationships_for_domains(
        ["initech.example"], allowed_pipelines=["default"]
    )
    r = rels[0]
    assert r["found"] is True
    assert r["company_name"] == "Initech"
    assert r["lifecyclestage"] == "customer"
    assert r["won_count"] == 1
    assert r["open_count"] == 1
    assert r["open_value"] == 45000.0
    assert r["last_won"] == {"name": "Initech Rollout 2024", "closedate": "2024-03-01"}

    block = hubspot_api.format_client_relationship_block(rels)
    assert "Initech" in block
    assert "won deal" in block
    assert "Initech Rollout 2024" in block
    assert "authoritative" in block  # tells the model to prefer this over inference


def test_company_not_found_is_stated_not_invented(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_search", lambda obj, body: {"results": []})
    rels = c.company_relationships_for_domains(["unknown-co.com"])
    assert rels[0]["found"] is False
    block = hubspot_api.format_client_relationship_block(rels)
    assert "no matching company found" in block


def test_lookup_failure_is_swallowed_per_domain(monkeypatch):
    c = _client()

    def boom(obj, body):
        raise RuntimeError("hubspot 500")

    monkeypatch.setattr(c, "_search", boom)
    rels = c.company_relationships_for_domains(["initech.example"])
    assert rels[0]["found"] is False
    assert rels[0].get("error") == "HubSpot lookup failed"


def test_company_with_only_foreign_deals_remains_private(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "_search",
        lambda *_: {
            "results": [
                {
                    "id": "foreign-company",
                    "properties": {
                        "name": "Foreign Client",
                        "domain": "shared.example",
                        "lifecyclestage": "customer",
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(c, "_deals_for_company", lambda *_: [])
    assert c.company_relationships_for_domains(["shared.example"]) == [
        {"domain": "shared.example", "found": False}
    ]


def test_empty_block_when_no_relationships():
    assert hubspot_api.format_client_relationship_block([]) == ""
