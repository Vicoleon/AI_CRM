"""A shared HubSpot portal cannot write across workspace deal boundaries."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from rubik_core.retrieval import hubspot_api, hubspot_write
from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
from rubik_core.retrieval.hubspot_write import (
    HubSpotPipelineRefused,
    HubSpotWriteClient,
    HubSpotWriteRefused,
)
from rubik_core.tenancy.context import current_workspace, use_workspace
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

_ASSOCIATIONS = {
    "companies": {
        "co-a": ["deal-a"],
        "co-b": ["deal-b"],
        "1002": ["deal-b"],
        "co-none": [],
        "co-mixed": ["deal-a", "deal-b"],
        "co-unknown": ["deal-a", "deal-unknown"],
    },
    "contacts": {
        "ct-a": ["deal-a"],
        "ct-b": ["deal-b"],
        "ct-none": [],
        "ct-mixed": ["deal-a", "deal-b"],
        "ct-unknown": ["deal-a", "deal-unknown"],
    },
}
_DEAL_PIPELINES = {"deal-a": "alpha", "deal-b": "beta"}
_EMAIL = "ae@example.test"
_STAMP = int(datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture
def shared_portal(monkeypatch):
    """Real HubSpot read/write clients over one recorded in-memory portal."""
    configs = {
        slug: HubSpotConnectorConfig(
            pipeline_ids=(slug,) if slug != "blank" else (),
            ccs_vendor_fields_default=("supplier",),
        )
        for slug in ("alpha", "beta", "blank")
    }

    def config():
        return configs[current_workspace()]

    monkeypatch.setattr(hubspot_api, "get_hubspot_config", config)
    monkeypatch.setattr(hubspot_write, "get_hubspot_config", config)
    requests: list[httpx.Request] = []
    failed_associations: set[str] = set()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path.startswith("/crm/v4/objects/"):
            parts = path.split("/")
            from_type, record_id = parts[4:6]
            if from_type in failed_associations:
                return httpx.Response(400, json={"message": "unavailable"})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"toObjectId": deal_id}
                        for deal_id in _ASSOCIATIONS[from_type].get(record_id, [])
                    ]
                },
            )
        if path.startswith("/crm/v4/associations/") and path.endswith(
            "/deals/batch/read"
        ):
            from_type = path.split("/")[4]
            if from_type in failed_associations:
                return httpx.Response(400, json={"message": "unavailable"})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "from": {"id": item["id"]},
                            "to": [
                                {"toObjectId": deal_id}
                                for deal_id in _ASSOCIATIONS[from_type].get(
                                    item["id"], []
                                )
                            ],
                        }
                        for item in body["inputs"]
                    ]
                },
            )
        if path == "/crm/v3/objects/deals/batch/read":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": item["id"],
                            "properties": {"pipeline": _DEAL_PIPELINES[item["id"]]},
                        }
                        for item in body["inputs"]
                        if item["id"] in _DEAL_PIPELINES
                    ]
                },
            )
        if request.method == "GET" and path.startswith("/crm/v3/objects/"):
            object_type, object_id = path.split("/")[4:6]
            properties = {"name": "Private record"}
            if object_type == "deals":
                properties.update(pipeline=_DEAL_PIPELINES[object_id], amount="900000")
            return httpx.Response(200, json={"id": object_id, "properties": properties})
        if request.method == "POST" and path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        if request.method in ("PATCH", "POST"):
            return httpx.Response(200, json={"id": "created"})
        raise AssertionError(f"Unexpected HubSpot request: {request.method} {path}")

    real_client = httpx.Client
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs),
    )

    def writer() -> HubSpotWriteClient:
        read = HubSpotAPIClient(token="read", base_url="https://hubspot.test")
        read.owner_directory = lambda: {"77": {"email": _EMAIL}}
        return HubSpotWriteClient(
            token="write", read_client=read, base_url="https://hubspot.test"
        )

    return writer, requests, failed_associations


def _writes(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [
        request
        for request in requests
        if request.method in ("PATCH", "POST")
        and not request.url.path.endswith("/batch/read")
        and not request.url.path.endswith("/search")
    ]


def test_blank_pipeline_refuses_protected_reads_and_writes_without_http(shared_portal):
    writer, requests, _ = shared_portal
    with use_workspace("blank"):
        client = writer()
        with pytest.raises(HubSpotPipelineRefused):
            client.get_object("deals", "deal-a", ["amount"])
        with pytest.raises(HubSpotPipelineRefused):
            client.get_object("companies", "co-a", ["name"])
        with pytest.raises(HubSpotPipelineRefused):
            client.update_company("co-a", {"supplier": "x"})
        with pytest.raises(HubSpotPipelineRefused):
            client.create_call({"hs_timestamp": _STAMP}, company_id="co-a")
    assert requests == []


def test_deal_get_checks_live_pipeline_before_returning_properties(shared_portal):
    writer, requests, _ = shared_portal
    with use_workspace("alpha"):
        client = writer()
        assert client.get_object("deals", "deal-a", ["amount"])["id"] == "deal-a"
        with pytest.raises(HubSpotPipelineRefused):
            client.get_object("deals", "deal-b", ["amount"])
    with use_workspace("beta"):
        client = writer()
        assert client.get_object("deals", "deal-b", ["amount"])["id"] == "deal-b"
        with pytest.raises(HubSpotPipelineRefused):
            client.get_object("deals", "deal-a", ["amount"])
    deal_gets = [r for r in requests if r.method == "GET" and "/deals/" in r.url.path]
    assert len(deal_gets) == 4
    assert all("pipeline" in r.url.params["properties"] for r in deal_gets)


def test_company_and_contact_need_an_in_scope_deal_in_each_workspace(shared_portal):
    writer, requests, _ = shared_portal
    with use_workspace("alpha"):
        client = writer()
        assert client.get_object("contacts", "ct-a", ["name"])["id"] == "ct-a"
        assert client.update_company("co-a", {"supplier": "x"})["id"] == "created"
        for object_type, record_id in (
            ("companies", "co-b"),
            ("contacts", "ct-b"),
            ("companies", "co-none"),
            ("contacts", "ct-none"),
        ):
            with pytest.raises(HubSpotWriteRefused, match="no deal"):
                client.get_object(object_type, record_id, ["name"])
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.update_company("co-b", {"supplier": "x"})
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.update_contact("ct-b", {"firstname": "x"})
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.update_company_research_link(
                "1002", "https://cubie.example/research/1002"
            )
        alpha_requests = requests[:]
    with use_workspace("beta"):
        client = writer()
        assert client.get_object("contacts", "ct-b", ["name"])["id"] == "ct-b"
        assert client.update_company("co-b", {"supplier": "x"})["id"] == "created"
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.update_company("co-a", {"supplier": "x"})
    assert [r.url.path for r in _writes(requests)] == [
        "/crm/v3/objects/companies/co-a",
        "/crm/v3/objects/companies/co-b",
    ]
    assert not any(
        r.method == "GET"
        and r.url.path.endswith(("ct-b", "co-b", "co-none", "ct-none"))
        for r in alpha_requests
    )


def test_association_lookup_failure_never_patches_company(shared_portal):
    writer, requests, failed_associations = shared_portal
    failed_associations.add("companies")
    with use_workspace("alpha"):
        with pytest.raises(HubSpotWriteRefused, match="Could not verify"):
            writer().update_company("co-a", {"supplier": "x"})
    assert _writes(requests) == []


def test_mixed_or_unresolved_company_contact_never_reads_or_writes(shared_portal):
    writer, requests, _ = shared_portal
    with use_workspace("alpha"):
        client = writer()
        for kind, record_id in (
            ("companies", "co-mixed"),
            ("companies", "co-unknown"),
            ("contacts", "ct-mixed"),
            ("contacts", "ct-unknown"),
        ):
            with pytest.raises(HubSpotWriteRefused, match="no deal"):
                client.get_object(kind, record_id, ["name"])
            with pytest.raises(HubSpotWriteRefused, match="no deal"):
                if kind == "companies":
                    client.update_company(record_id, {"supplier": "x"})
                else:
                    client.update_contact(record_id, {"firstname": "x"})
    assert not any(
        request.method == "GET" and request.url.path.startswith("/crm/v3/objects/")
        for request in requests
    )
    assert _writes(requests) == []


def test_creates_refuse_foreign_association_targets_before_post(shared_portal):
    writer, requests, _ = shared_portal
    with use_workspace("alpha"):
        client = writer()
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.create_call({"hs_timestamp": _STAMP}, contact_id="ct-b")
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.create_task(
                {"hs_timestamp": _STAMP}, owner_email=_EMAIL, company_id="co-b"
            )
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.create_email(
                {"hs_timestamp": _STAMP, "hs_email_direction": "EMAIL"},
                owner_email=_EMAIL,
                contact_ids=["ct-b"],
            )
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.create_deal(
                {"dealname": "Foreign attachment", "pipeline": "alpha"},
                owner_email=_EMAIL,
                company_id="co-b",
            )
        with pytest.raises(HubSpotWriteRefused, match="no deal"):
            client.create_deal(
                {"dealname": "Foreign contact", "pipeline": "alpha"},
                owner_email=_EMAIL,
                company_id="co-a",
                contact_ids=["ct-b"],
            )
        with pytest.raises(HubSpotPipelineRefused):
            client.create_call({"hs_timestamp": _STAMP}, deal_id="deal-b")
        assert (
            client.create_call({"hs_timestamp": _STAMP}, contact_id="ct-a")["id"]
            == "created"
        )
    with use_workspace("beta"):
        client = writer()
        assert (
            client.create_deal(
                {"dealname": "Owned attachment", "pipeline": "beta"},
                owner_email=_EMAIL,
                company_id="co-b",
                contact_ids=["ct-b"],
            )["id"]
            == "created"
        )
    assert [r.url.path for r in _writes(requests)] == [
        "/crm/v3/objects/calls",
        "/crm/v3/objects/deals",
    ]
