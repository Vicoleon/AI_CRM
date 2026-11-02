"""Loop Closure cannot report another workspace's shared-portal CRM records."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from rubik_core.loop_closure import hubspot_index
from rubik_core.tenancy.context import current_workspace, use_workspace
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class SharedPortal:
    MAX_RECORDS = 5000

    def __init__(self):
        self.calls: list[tuple] = []
        self.deals = {
            "da": {
                "pipeline": "pipe-a",
                "dealname": "A deal",
                "hubspot_owner_id": "o1",
            },
            "db": {
                "pipeline": "pipe-b",
                "dealname": "B deal",
                "hubspot_owner_id": "o1",
            },
        }
        self.associations = {
            ("contacts", "companies"): {"ca": ["coa"], "cb": ["cob"], "cm": ["coa"]},
            ("contacts", "deals"): {"ca": ["da"], "cb": ["db"], "cm": ["da", "db"]},
            ("companies", "deals"): {"coa": ["da"], "cob": ["db"]},
            ("deals", "companies"): {"da": ["coa"], "db": ["cob"]},
            ("tasks", "companies"): {"ta": ["coa"], "tb": ["cob"], "tm": ["coa"]},
            ("tasks", "deals"): {"ta": ["da"], "tb": ["db"], "tm": ["da", "db"]},
            ("emails", "contacts"): {"ea": ["ca"], "eb": ["cb"], "em": ["ca"]},
            ("emails", "companies"): {"ea": ["coa"], "eb": ["cob"], "em": ["coa"]},
            ("emails", "deals"): {"ea": ["da"], "eb": ["db"], "em": ["da", "db"]},
        }

    def owner_directory(self):
        self.calls.append(("owners",))
        return {"o1": {"email": "rep@example.com"}}

    def search_all(self, object_type, filters, properties, sorts=None):
        self.calls.append(("search", object_type))
        if object_type == "contacts":
            return [
                {
                    "id": "ca",
                    "properties": {"email": "a@client.example", "firstname": "A"},
                },
                {
                    "id": "cb",
                    "properties": {"email": "b@client.example", "firstname": "B"},
                },
                {
                    "id": "cm",
                    "properties": {"email": "m@client.example", "firstname": "Mixed"},
                },
            ]
        if object_type == "deals":
            return [
                {"id": deal_id, "properties": props}
                for deal_id, props in self.deals.items()
            ]
        if object_type == "tasks":
            return [
                {
                    "id": task_id,
                    "properties": {
                        "hs_task_subject": f"{task_id} confidential",
                        "hs_timestamp": (NOW - timedelta(days=5)).isoformat(),
                        "hs_task_status": "NOT_STARTED",
                        "hubspot_owner_id": "o1",
                    },
                }
                for task_id in ("ta", "tb", "tm")
            ]
        if object_type == "emails":
            return [
                {"id": activity_id, "properties": {"hs_timestamp": NOW.isoformat()}}
                for activity_id in ("ea", "eb", "em")
            ]
        return []

    def batch_associations(self, from_type, to_type, ids, **_kwargs):
        self.calls.append(("association", from_type, to_type, tuple(ids)))
        table = self.associations.get((from_type, to_type), {})
        if to_type == "deals":
            allowed = hubspot_index._allowed_pipelines()
            return {
                record_id: [
                    deal_id
                    for deal_id in table.get(record_id, [])
                    if self.deals[deal_id]["pipeline"] in allowed
                ]
                for record_id in ids
            }
        return {record_id: table.get(record_id, []) for record_id in ids}

    def fully_owned_deal_associations(self, object_type, ids):
        self.calls.append(("full-association", object_type, tuple(ids)))
        raw = self.associations.get((object_type, "deals"), {})
        allowed = hubspot_index._allowed_pipelines()
        return {
            record_id: raw[record_id]
            for record_id in ids
            if raw.get(record_id)
            and all(
                self.deals.get(deal_id, {}).get("pipeline") in allowed
                for deal_id in raw[record_id]
            )
        }

    def batch_read(self, object_type, ids, properties, **_kwargs):
        self.calls.append(("batch", object_type, tuple(ids)))
        if object_type == "deals":
            return [
                {"id": record_id, "properties": self.deals[record_id]}
                for record_id in ids
                if record_id in self.deals
            ]
        if object_type == "companies":
            return [
                {
                    "id": record_id,
                    "properties": {
                        "name": f"{record_id} secret",
                        "domain": "client.example",
                    },
                }
                for record_id in ids
            ]
        if object_type == "tasks":
            return [
                {
                    "id": record_id,
                    "properties": {
                        "hs_task_status": "NOT_STARTED",
                        "hubspot_owner_id": "o1",
                    },
                }
                for record_id in ids
            ]
        return []

    def companies_for_domains(self, _domains):
        return {}


def _build(client: SharedPortal):
    return hubspot_index.build_index(
        client,
        owner_emails=["rep@example.com"],
        addresses=["a@client.example", "b@client.example", "m@client.example"],
        window_start=NOW - timedelta(days=1),
        now=NOW + timedelta(hours=1),
        today=date(2026, 10, 30),
        task_grace_days=3,
        max_age_days=90,
        open_task_ids=["ta", "tb", "tm"],
        retry=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    )


def test_shared_portal_index_only_keeps_current_workspace_records(monkeypatch):
    from rubik_core.tenancy import hubspot_config

    configs = {
        "acme": HubSpotConnectorConfig(pipeline_ids=("pipe-a",)),
        "bravo": HubSpotConnectorConfig(pipeline_ids=("pipe-b",)),
    }
    monkeypatch.setattr(
        hubspot_config, "get_hubspot_config", lambda: configs[current_workspace()]
    )
    monkeypatch.setattr(hubspot_index, "_business_tz", lambda: timezone.utc)
    monkeypatch.setattr(
        hubspot_index,
        "_strict_company_ids",
        lambda ids: set(ids).intersection(
            {"coa"} if current_workspace() == "acme" else {"cob"}
        ),
    )

    for slug, contact, company, deal, task, activity in (
        ("acme", "a@client.example", "coa", "da", "ta", "ea"),
        ("bravo", "b@client.example", "cob", "db", "tb", "eb"),
    ):
        with use_workspace(slug):
            client = SharedPortal()
            index = _build(client)
            assert index.status == "partial"
            assert set(index.contacts) == {contact}
            assert set(index.companies) == {company}
            assert set(index.deals) == {deal}
            assert [item.id for item in index.tasks["o1"]] == [task]
            assert set(index.task_states) == {task}
            assert set(index.engagements) == {
                f"contact:{'ca' if slug == 'acme' else 'cb'}"
            }
            assert [eng.id for eng in next(iter(index.engagements.values()))] == [
                activity
            ]
            assert "m@client.example" not in index.contacts
            assert "tm" not in index.task_states
            assert all(
                "em" not in [eng.id for eng in rows]
                for rows in index.engagements.values()
            )
            assert index.evidence_may_be_missing(NOW - timedelta(hours=1))
            assert ("batch", "tasks", (task,)) in client.calls


def test_empty_pipeline_refuses_before_any_portal_call(monkeypatch):
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(hubspot_config, "get_hubspot_config", HubSpotConnectorConfig)
    client = SharedPortal()
    with use_workspace("acme"):
        index = _build(client)
    assert index.status == "unavailable"
    assert client.calls == []


def test_mixed_company_fields_never_enter_the_index(monkeypatch):
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("pipe-a",)),
    )
    monkeypatch.setattr(hubspot_index, "_business_tz", lambda: timezone.utc)
    # X-Ray's complete live proof rejects this company because one of its
    # deals is in the other workspace's pipeline.
    monkeypatch.setattr(hubspot_index, "_strict_company_ids", lambda _ids: set())
    client = SharedPortal()
    with use_workspace("acme"):
        index = _build(client)
    assert index.status == "partial"
    assert index.companies == {}
    assert index.domain_company == {}
    assert ("batch", "companies", ("coa",)) not in client.calls
    assert ("batch", "companies", ("cob",)) not in client.calls
