"""A shared HubSpot portal must not expose another workspace's X-Ray company."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user, require_sales_or_admin
from backend.main import app
from backend.routers import xray as xray_router
from rubik_core.auth.registry import UserProfile
from rubik_core.retrieval import hubspot_properties
from rubik_core.tenancy.context import current_workspace, use_workspace
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig
from rubik_core.xray import fetchers
from rubik_core.xray import properties as xray_properties


@pytest.fixture
def shared_portal(monkeypatch):
    configs = {
        "acme": HubSpotConnectorConfig(pipeline_ids=("acme-pipeline",)),
        "beta": HubSpotConnectorConfig(pipeline_ids=("beta-pipeline",)),
    }

    def getter():
        return configs[current_workspace()]

    for module in (fetchers, xray_properties, hubspot_properties):
        monkeypatch.setattr(module, "get_hubspot_config", getter)

    calls = defaultdict(list)
    company_deals = {"1": ["d1"], "2": ["d2"], "3": ["d1", "d2"], "4": []}
    deal_pipeline = {"d1": "acme-pipeline", "d2": "beta-pipeline"}

    class Client:
        def __init__(self):
            calls["client"].append(True)

        def search_objects(self, obj, query, props, limit=10):
            calls["search"].append((obj, query))
            return [
                {"id": company_id, "properties": {"name": f"Company {company_id}"}}
                for company_id in company_deals
            ]

    def associations(_client, company_id, obj, **_kwargs):
        calls["associations"].append((company_id, obj))
        ids = company_deals.get(str(company_id), []) if obj == "deals" else []
        return ids, len(ids)

    def records(_client, obj, ids, props):
        calls["read"].append((obj, tuple(ids), tuple(props)))
        if obj == "deals":
            return [
                {
                    "id": deal_id,
                    "properties": {
                        "pipeline": deal_pipeline[deal_id],
                        "dealname": f"Deal {deal_id}",
                    },
                }
                for deal_id in ids
            ]
        if obj == "companies":
            return [
                {"id": company_id, "properties": {"name": f"Company {company_id}"}}
                for company_id in ids
            ]
        return []

    monkeypatch.setattr(fetchers, "HubSpotAPIClient", Client)
    monkeypatch.setattr(fetchers, "_associated_ids", associations)
    monkeypatch.setattr(fetchers, "_raw_batch_read", records)
    monkeypatch.setattr(
        "rubik_core.graph.writers.record_company_bundle", lambda *_a, **_kw: None
    )
    return calls, configs


def test_empty_pipeline_config_makes_zero_hubspot_calls(monkeypatch):
    monkeypatch.setattr(fetchers, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(
        fetchers,
        "HubSpotAPIClient",
        lambda: pytest.fail("HubSpot client must not be created"),
    )

    assert fetchers.visible_company_ids(["1"]) == set()
    assert fetchers.search_companies("Acme") == []
    bundle = fetchers.fetch_company_bundle("1")

    assert not bundle.company and not bundle.deals and not bundle.engagements
    assert bundle.errors == ["Workspace deal pipelines are not configured."]


def test_two_workspaces_see_only_their_wholly_owned_companies(shared_portal):
    calls, _configs = shared_portal

    with use_workspace("acme"):
        assert fetchers.visible_company_ids(["1", "2", "3", "4"]) == {"1"}
        assert [row["company_id"] for row in fetchers.search_companies("Company")] == [
            "1"
        ]
        own = fetchers.fetch_company_bundle("1")
        foreign = fetchers.fetch_company_bundle("2")
        acme_reads = list(calls["read"])
    with use_workspace("beta"):
        assert fetchers.visible_company_ids(["1", "2", "3", "4"]) == {"2"}
        assert [row["company_id"] for row in fetchers.search_companies("Company")] == [
            "2"
        ]
        beta = fetchers.fetch_company_bundle("2")

    assert own.company_name == "Company 1"
    assert [deal["id"] for deal in own.deals] == ["d1"]
    assert foreign.company == {} and foreign.deals == []
    assert beta.company_name == "Company 2"
    assert [deal["id"] for deal in beta.deals] == ["d2"]
    assert not any(
        obj == "companies" and ids == ("2",) for obj, ids, _props in acme_reads
    )


def test_mixed_and_no_deal_companies_are_unavailable_before_profile_read(
    shared_portal,
):
    calls, _configs = shared_portal

    with use_workspace("acme"):
        mixed = fetchers.fetch_company_bundle("3")
        prospect = fetchers.fetch_company_bundle("4")

    for bundle in (mixed, prospect):
        assert not bundle.company and not bundle.deals and not bundle.contacts
        assert bundle.errors == ["Company is not available in this workspace."]
    assert all(obj != "companies" for obj, _ids, _props in calls["read"])


def test_owned_company_does_not_expose_mixed_contact_or_activity(
    shared_portal, monkeypatch
):
    calls, _configs = shared_portal
    base_client = fetchers.HubSpotAPIClient
    original_associations = fetchers._associated_ids
    original_records = fetchers._raw_batch_read

    class Client(base_client):
        def fully_owned_deal_associations(self, object_type, ids):
            allowed = {"contacts": {"c1"}, "emails": {"e1"}}.get(object_type, set())
            return {item_id: ["d1"] for item_id in ids if item_id in allowed}

    def associations(client, company_id, obj, **kwargs):
        if obj == "contacts":
            return ["c1", "c2"], 2
        if obj == "emails":
            return ["e1", "e2"], 2
        return original_associations(client, company_id, obj, **kwargs)

    def records(client, obj, ids, props):
        if obj in ("contacts", "emails"):
            calls["read"].append((obj, tuple(ids), tuple(props)))
            return [
                {"id": item_id, "properties": {"email": f"{item_id}@example.com"}}
                for item_id in ids
            ]
        return original_records(client, obj, ids, props)

    monkeypatch.setattr(fetchers, "HubSpotAPIClient", Client)
    monkeypatch.setattr(fetchers, "_associated_ids", associations)
    monkeypatch.setattr(fetchers, "_raw_batch_read", records)

    with use_workspace("acme"):
        bundle = fetchers.fetch_company_bundle("1")

    assert [row["id"] for row in bundle.contacts] == ["c1"]
    assert [row["id"] for row in bundle.engagements["emails"]] == ["e1"]
    assert all("c2" not in ids and "e2" not in ids for _obj, ids, _p in calls["read"])
    assert any("Some contacts could not be checked" in gap for gap in bundle.errors)
    assert any("Some emails could not be checked" in gap for gap in bundle.errors)


def test_unresolved_deal_pipeline_denies_company_before_profile_read(
    shared_portal, monkeypatch
):
    calls, _configs = shared_portal
    original = fetchers._raw_batch_read

    def missing_pipeline(client, obj, ids, props):
        if obj == "deals":
            return [{"id": deal_id, "properties": {}} for deal_id in ids]
        return original(client, obj, ids, props)

    monkeypatch.setattr(fetchers, "_raw_batch_read", missing_pipeline)
    with use_workspace("acme"):
        bundle = fetchers.fetch_company_bundle("1")

    assert bundle.company == {} and bundle.deals == []
    assert all(obj != "companies" for obj, _ids, _props in calls["read"])


def test_control_outage_makes_zero_hubspot_calls(monkeypatch):
    def unavailable():
        raise RuntimeError("control DB unavailable")

    monkeypatch.setattr(fetchers, "get_hubspot_config", unavailable)
    monkeypatch.setattr(
        fetchers,
        "HubSpotAPIClient",
        lambda: pytest.fail("HubSpot client must not be created"),
    )

    assert fetchers.search_companies("Acme") == []
    bundle = fetchers.fetch_company_bundle("1")
    assert bundle.company == {} and bundle.deals == []


def test_ownership_change_mid_fetch_drops_whole_bundle(shared_portal, monkeypatch):
    calls, _configs = shared_portal
    original = fetchers._authorized_deals
    attempts = 0

    def moving_company(client, company_id, allowed):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise fetchers.HubSpotAPIError("association changed")
        return original(client, company_id, allowed)

    monkeypatch.setattr(fetchers, "_authorized_deals", moving_company)
    with use_workspace("acme"):
        bundle = fetchers.fetch_company_bundle("1")

    assert attempts == 2
    assert not bundle.company and not bundle.deals and not bundle.contacts
    assert bundle.errors == ["Company is not available in this workspace."]
    assert any(obj == "companies" for obj, _ids, _props in calls["read"])


def test_incomplete_association_scan_refuses_ownership(monkeypatch):
    class Client:
        base_url = "https://api.hubapi.test"

        def _headers(self):
            return {}

    monkeypatch.setattr(
        fetchers,
        "_with_backoff",
        lambda _fn, *_args: {
            "results": [{"toObjectId": "d1"}],
            "paging": {"next": {"after": "more"}},
        },
    )

    with pytest.raises(fetchers.HubSpotAPIError, match="incomplete"):
        fetchers._associated_ids(Client(), "1", "deals", max_pages=2)


@pytest.mark.parametrize(
    "bad_page",
    [
        {"results": None},
        {"results": [], "paging": {"next": {}}},
        {"results": [{"missing": "id"}]},
    ],
)
def test_malformed_second_association_page_cannot_authorize_company(
    monkeypatch, bad_page
):
    class Client:
        base_url = "https://api.hubapi.test"

        def _headers(self):
            return {}

    pages = iter(
        [
            {"results": [{"toObjectId": "d1"}], "paging": {"next": {"after": "more"}}},
            bad_page,
        ]
    )
    monkeypatch.setattr(fetchers, "_with_backoff", lambda _fn, *_args: next(pages))
    monkeypatch.setattr(
        fetchers,
        "_raw_batch_read",
        lambda *_args: pytest.fail("a partial association scan must not read deals"),
    )

    with pytest.raises(fetchers.HubSpotAPIError, match="association"):
        fetchers._authorized_deals(Client(), "1", frozenset({"acme-pipeline"}))


@pytest.fixture
def denied_xray_client(monkeypatch):
    user = UserProfile(
        id="u1",
        email="admin@acme.example",
        name="Admin",
        department="Sales",
        roles=["admin"],
        created_at="2026-10-01T00:00:00Z",
        updated_at="2026-10-01T00:00:00Z",
    )
    old = {
        dependency: app.dependency_overrides.get(dependency)
        for dependency in (get_current_user, require_sales_or_admin)
    }
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_sales_or_admin] = lambda: user
    monkeypatch.setattr(fetchers, "company_available", lambda _id: False)
    monkeypatch.setattr(fetchers, "visible_company_ids", lambda _ids: set())
    try:
        yield TestClient(app)
    finally:
        for dependency, previous in old.items():
            if previous is None:
                app.dependency_overrides.pop(dependency, None)
            else:
                app.dependency_overrides[dependency] = previous


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/xray/1"),
        ("get", "/api/xray/1/history"),
        ("get", "/api/xray/1/timeline"),
        ("get", "/api/xray/runs/snap-1"),
        ("get", "/api/xray/snapshots/snap-1/docx"),
        ("post", "/api/xray/snapshots/snap-1/save-to-assets"),
        ("post", "/api/xray/1/fast"),
        ("post", "/api/xray/1/deep"),
    ],
)
def test_stored_or_new_xray_denied_after_pipeline_narrowing(
    denied_xray_client, monkeypatch, method, path
):
    monkeypatch.setattr(
        xray_router,
        "_load_snapshot_dict",
        lambda *_a, **_kw: {"company_id": "1", "status": "ready"},
    )
    monkeypatch.setattr(
        xray_router,
        "_load_ready_snapshot",
        lambda *_a: SimpleNamespace(company_id="1"),
    )
    monkeypatch.setattr(
        xray_router.service,
        "run_fast",
        lambda *_a, **_kw: pytest.fail("foreign X-Ray generation must not run"),
    )
    monkeypatch.setattr(
        xray_router.service,
        "create_pending_deep",
        lambda *_a, **_kw: pytest.fail("foreign deep run must not be queued"),
    )

    result = getattr(denied_xray_client, method)(path)

    assert result.status_code == 404
    assert result.json()["detail"] == "X-Ray customer not found."


def test_recent_snapshots_filter_currently_foreign_companies(
    denied_xray_client, monkeypatch
):
    old = SimpleNamespace(
        company_id="1",
        company_name="Foreign customer",
        company_domain="foreign.example",
        signals={},
        id="snap-1",
        kind="fast",
        version=1,
        created_at=datetime(2026, 10, 1),
    )
    monkeypatch.setattr(
        xray_router.persistence, "recent_ready", lambda *_a, **_kw: [old]
    )

    result = denied_xray_client.get("/api/xray/recent")

    assert result.status_code == 200
    assert result.json() == []
