"""A shared HubSpot portal cannot expose another workspace's deals."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from rubik_core.retrieval import hubspot_api
from rubik_core.retrieval.hubspot_api import HubSpotAPIClient, HubSpotAPIError
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig


def _set_pipelines(monkeypatch, *pipelines: str) -> None:
    monkeypatch.setattr(
        hubspot_api,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=pipelines),
    )


def test_unconfigured_workspace_makes_no_deal_request(monkeypatch):
    _set_pipelines(monkeypatch)
    client = HubSpotAPIClient(token="test-token")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("deal request escaped the pipeline gate")

    monkeypatch.setattr(client, "_search", forbidden)
    monkeypatch.setattr(hubspot_api.httpx, "Client", forbidden)
    for read in (
        lambda: client.count("deals", []),
        lambda: client.search_all("deals", [], ["dealname"]),
        lambda: client.search_all_groups("deals", [[]], ["dealname"]),
        lambda: client.search_objects("deals", "Acme", ["dealname"]),
        lambda: client.batch_read("deals", ["d1"], ["dealname"]),
        lambda: client.batch_read("contacts", ["c1"], ["email"]),
        lambda: client.batch_read("companies", ["c1"], ["name"]),
        lambda: client.batch_associations("deals", "contacts", ["d1"]),
        lambda: client.batch_associations("companies", "deals", ["c1"]),
        lambda: client._deals_for_company("c1", set()),
        lambda: client.search_owned_objects("contacts", "Pat", ["email"]),
        lambda: client.search_all_owned("contacts", [], ["email"]),
        lambda: client.search_all_owned("emails", [], ["hs_timestamp"]),
        lambda: client.fully_owned_deal_associations("emails", ["e1"]),
    ):
        with pytest.raises(HubSpotAPIError, match="pipelines are not configured"):
            read()
    domain = client.companies_for_domains(["foreign.example"])["foreign.example"]
    assert domain.candidates == () and domain.error
    assert (
        client.company_relationships_for_domains(["foreign.example"])[0]["found"]
        is False
    )


def test_deal_search_uses_server_filter_and_discards_foreign_rows(monkeypatch):
    _set_pipelines(monkeypatch, "own")
    client = HubSpotAPIClient(token="test-token")
    bodies = []

    def search(object_type, body):
        assert object_type == "deals"
        bodies.append(body)
        return {
            "results": [
                {"id": "a", "properties": {"pipeline": "own", "dealname": "Mine"}},
                {"id": "b", "properties": {"pipeline": "foreign", "dealname": "Other"}},
                {"id": "c", "properties": {"dealname": "Unknown"}},
            ]
        }

    monkeypatch.setattr(client, "_search", search)
    for rows in (
        client.search_all("deals", [], ["dealname"]),
        client.search_all_groups("deals", [[]], ["dealname"]),
        client.search_all_groups("deals", [], ["dealname"]),
        client.search_objects("deals", "Mine", ["dealname"]),
    ):
        assert [row["id"] for row in rows] == ["a"]
    for body in bodies:
        assert "pipeline" in body["properties"]
        assert all(
            {"propertyName": "pipeline", "operator": "IN", "values": ["own"]}
            in group["filters"]
            for group in body["filterGroups"]
        )


def test_by_id_deal_read_requires_own_pipeline(monkeypatch):
    _set_pipelines(monkeypatch, "own")
    client = HubSpotAPIClient(token="test-token")
    bodies = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"id": "a", "properties": {"pipeline": "own"}},
                    {"id": "b", "properties": {"pipeline": "foreign"}},
                    {"id": "c", "properties": {}},
                ]
            }

    class FakeHTTP:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, *, headers, json):
            assert headers["Authorization"] == "Bearer test-token"
            bodies.append(json)
            return Response()

    monkeypatch.setattr(hubspot_api.httpx, "Client", FakeHTTP)
    rows = client.batch_read("deals", ["a", "b", "c"], ["dealname"])
    assert [row["id"] for row in rows] == ["a"]
    assert "pipeline" in bodies[0]["properties"]


def test_by_id_contact_and_company_read_requires_full_ownership(monkeypatch):
    _set_pipelines(monkeypatch, "own")
    client = HubSpotAPIClient(token="test-token")
    seen: list[tuple[str, list[str]]] = []

    def prove(object_type, ids):
        seen.append((object_type, list(ids)))
        return {"a": ["deal-a"]} if "a" in ids else {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"id": "a", "properties": {"name": "Owned"}}]}

    class FakeHTTP:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, headers, json):
            assert headers["Authorization"] == "Bearer test-token"
            assert [item["id"] for item in json["inputs"]] == ["a"]
            assert "/batch/read" in url
            return Response()

    monkeypatch.setattr(client, "fully_owned_deal_associations", prove)
    monkeypatch.setattr(hubspot_api.httpx, "Client", FakeHTTP)
    for object_type in ("contacts", "companies"):
        assert [
            row["id"]
            for row in client.batch_read(object_type, ["a", "mixed"], ["name"])
        ] == ["a"]
        assert client.batch_read(object_type, ["mixed"], ["name"]) == []
    assert seen == [
        ("contacts", ["a", "mixed"]),
        ("contacts", ["mixed"]),
        ("companies", ["a", "mixed"]),
        ("companies", ["mixed"]),
    ]


def test_chat_refuses_to_construct_client_without_pipelines(monkeypatch):
    _set_pipelines(monkeypatch)
    monkeypatch.setattr(
        hubspot_api,
        "HubSpotAPIClient",
        lambda: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    retriever = hubspot_api.HubSpotAPIRetriever.__new__(hubspot_api.HubSpotAPIRetriever)
    retriever.model_id = "test-model"
    block, debug = retriever.retrieve("show our deals")
    assert "choose this workspace's HubSpot deal pipelines" in block
    assert debug is None


def test_shared_portal_companies_and_contacts_follow_workspace_deals(monkeypatch):
    client = HubSpotAPIClient(token="test-token")

    def search(object_type, _body):
        if object_type == "companies":
            return {
                "total": 2,
                "results": [
                    {
                        "id": "company-a",
                        "properties": {"domain": "shared.example", "name": "Company A"},
                    },
                    {
                        "id": "company-b",
                        "properties": {"domain": "shared.example", "name": "Company B"},
                    },
                    {
                        "id": "company-mixed",
                        "properties": {
                            "domain": "shared.example",
                            "name": "Shared Company",
                        },
                    },
                ],
            }
        if object_type == "contacts":
            return {
                "results": [
                    {
                        "id": "contact-a",
                        "properties": {
                            "firstname": "Alice",
                            "email": "a@shared.example",
                        },
                    },
                    {
                        "id": "contact-b",
                        "properties": {"firstname": "Bob", "email": "b@shared.example"},
                    },
                    {
                        "id": "contact-mixed",
                        "properties": {
                            "firstname": "Shared",
                            "email": "shared@shared.example",
                        },
                    },
                ]
            }
        if object_type == "meetings":
            return {
                "results": [
                    {
                        "id": "meeting-a",
                        "properties": {"hs_meeting_outcome": "COMPLETED"},
                    },
                    {
                        "id": "meeting-b",
                        "properties": {"hs_meeting_outcome": "COMPLETED"},
                    },
                    {
                        "id": "meeting-mixed",
                        "properties": {"hs_meeting_outcome": "COMPLETED"},
                    },
                    {
                        "id": "meeting-a-b-contact",
                        "properties": {"hs_meeting_outcome": "COMPLETED"},
                    },
                    {
                        "id": "meeting-a-b-company",
                        "properties": {"hs_meeting_outcome": "COMPLETED"},
                    },
                ]
            }
        raise AssertionError("Unexpected search")

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeHTTP:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, headers, params):
            assert headers["Authorization"] == "Bearer test-token"
            source = url.split("/objects/")[1].split("/")[0]
            record_id = url.split("/objects/")[1].split("/")[1]
            to_type = url.rsplit("/", 1)[-1]
            assert source in ("companies", "contacts", "meetings")
            assert params["limit"] == 500
            if to_type == "contacts":
                return Response(
                    {
                        "results": [
                            {"toObjectId": "contact-b"}
                            for _ in range(record_id == "meeting-a-b-contact")
                        ]
                    }
                )
            if to_type == "companies":
                return Response(
                    {
                        "results": [
                            {"toObjectId": "company-b"}
                            for _ in range(record_id == "meeting-a-b-company")
                        ]
                    }
                )
            assert to_type == "deals"
            if record_id.endswith("-mixed"):
                ids = ["deal-a", "deal-b"]
            elif record_id.startswith("meeting-a-b-"):
                ids = ["deal-a"]
            else:
                ids = ["deal-a" if record_id.endswith("-a") else "deal-b"]
            return Response({"results": [{"toObjectId": deal_id} for deal_id in ids]})

        def post(self, url, *, headers, json):
            assert headers["Authorization"] == "Bearer test-token"
            if "/associations/companies/deals/" in url:
                source = "company"
            elif "/associations/contacts/deals/" in url:
                source = "contact"
            elif "/associations/meetings/deals/" in url:
                source = "meeting"
            elif "/objects/deals/batch/read" in url:
                return Response(
                    {
                        "results": [
                            {"id": "deal-a", "properties": {"pipeline": "pipeline-a"}},
                            {"id": "deal-b", "properties": {"pipeline": "pipeline-b"}},
                        ]
                    }
                )
            else:
                raise AssertionError(f"Unexpected HubSpot URL: {url}")
            return Response(
                {
                    "results": [
                        {
                            "from": {"id": f"{source}-a"},
                            "to": [{"toObjectId": "deal-a"}],
                        },
                        {
                            "from": {"id": f"{source}-b"},
                            "to": [{"toObjectId": "deal-b"}],
                        },
                    ]
                }
            )

    monkeypatch.setattr(client, "_search", search)
    monkeypatch.setattr(hubspot_api.httpx, "Client", FakeHTTP)
    for pipeline, suffix in (("pipeline-a", "a"), ("pipeline-b", "b")):
        _set_pipelines(monkeypatch, pipeline)
        companies = client.companies_for_domains(["shared.example"])
        assert [
            c.hubspot_company_id for c in companies["shared.example"].candidates
        ] == [f"company-{suffix}"]
        contacts = client.search_owned_objects(
            "contacts", "shared", ["firstname", "email"]
        )
        assert [c["id"] for c in contacts] == [f"contact-{suffix}"]
        meetings = client.search_all_owned("meetings", [], ["hs_meeting_outcome"])
        assert [m["id"] for m in meetings] == [f"meeting-{suffix}"]


@pytest.mark.parametrize("object_type", ["contacts", "emails"])
def test_full_ownership_proof_reads_later_association_pages(monkeypatch, object_type):
    _set_pipelines(monkeypatch, "pipeline-a")
    client = HubSpotAPIClient(token="test-token")
    pages: list[str | None] = []

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

    class FakeHTTP:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, *, headers, params):
            assert headers["Authorization"] == "Bearer test-token"
            pages.append(params.get("after"))
            if params.get("after"):
                return Response({"results": [{"toObjectId": "deal-b"}]})
            return Response(
                {
                    "results": [{"toObjectId": "deal-a"}],
                    "paging": {"next": {"after": "page-2"}},
                }
            )

    monkeypatch.setattr(hubspot_api.httpx, "Client", FakeHTTP)
    monkeypatch.setattr(
        client,
        "batch_read",
        lambda _kind, _ids, _props: [
            {"id": "deal-a", "properties": {"pipeline": "pipeline-a"}}
        ],
    )
    assert client.fully_owned_deal_associations(object_type, ["record-mixed"]) == {}
    assert pages == [None, "page-2"]


def test_full_ownership_proof_rejects_broken_paging(monkeypatch):
    _set_pipelines(monkeypatch, "pipeline-a")
    client = HubSpotAPIClient(token="test-token")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [{"toObjectId": "deal-a"}],
                "paging": {"next": {"after": "same-cursor"}},
            }

    class FakeHTTP:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(hubspot_api.httpx, "Client", FakeHTTP)
    monkeypatch.setattr(
        client,
        "batch_read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deal data read before complete association proof")
        ),
    )
    with pytest.raises(HubSpotAPIError, match="paging is incomplete"):
        client.fully_owned_deal_associations("companies", ["company-a"])


def test_workspace_metrics_and_cold_scan_do_not_use_portal_wide_counts():
    class OwnedClient:
        def count(self, *_args, **_kwargs):
            raise AssertionError("portal-wide count escaped")

        def search_all(self, *_args, **_kwargs):
            raise AssertionError("unscoped sweep escaped")

        def search_all_owned(self, object_type, *_args):
            if object_type == "meetings":
                return [
                    {"id": "m1", "properties": {"hs_meeting_outcome": "COMPLETED"}},
                    {"id": "m2", "properties": {}},
                ]
            if object_type == "contacts":
                return [{"id": "c1", "properties": {"firstname": "Alice"}}]
            return []

    client = OwnedClient()
    window = (1, 2, "today")
    assert "2 booked" in hubspot_api._kpi_meetings(client, window, None, "")
    assert "1 completed" in hubspot_api._kpi_meetings(client, window, None, "")
    assert "**1**" in hubspot_api._kpi_new_contacts(client, window, None, "")
    assert "**2**" in hubspot_api._kpi_activities_logged(client, window, None, "")

    retriever = hubspot_api.HubSpotAPIRetriever.__new__(hubspot_api.HubSpotAPIRetriever)
    target = hubspot_api.ResolvedTarget(
        "company", True, "ok", set_object_type="companies"
    )
    block = retriever._recency_cold_scan(
        client, {}, window, target, "", {"pipeline-a"}, {}
    )
    assert "None found" in block


def test_cold_scan_reads_workspace_time_zone_once_for_all_rows(monkeypatch):
    calls = []

    def time_zone():
        calls.append(1)
        return ZoneInfo("UTC")

    class Client:
        def search_all_owned(self, *_args):
            return [
                {
                    "id": str(index),
                    "properties": {
                        "firstname": "Person",
                        "notes_last_contacted": "2026-10-01T00:00:00Z",
                    },
                }
                for index in range(20)
            ]

    monkeypatch.setattr(hubspot_api, "_tz", time_zone)
    retriever = hubspot_api.HubSpotAPIRetriever.__new__(hubspot_api.HubSpotAPIRetriever)
    target = hubspot_api.ResolvedTarget(
        "contact", True, "ok", set_object_type="contacts"
    )
    retriever._recency_cold_scan(
        Client(), {}, (1, 2, "today"), target, "", {"pipeline-a"}, {}
    )
    assert len(calls) == 1
