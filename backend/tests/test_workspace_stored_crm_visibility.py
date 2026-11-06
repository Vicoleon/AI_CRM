"""Live proof requires every stored CRM source to remain workspace-owned."""

from __future__ import annotations

from types import SimpleNamespace

from rubik_core.tenancy import crm_visibility


def test_mixed_pipeline_source_sets_only_return_currently_owned_ids(monkeypatch):
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: SimpleNamespace(pipeline_ids=("allowed",)),
    )

    class Portal:
        def fully_owned_deal_associations(self, kind, ids):
            assert kind in ("companies", "contacts")
            return {value: ["d1"] for value in ids if value.endswith("1")}

        def batch_read(self, kind, ids, properties):
            assert kind == "deals" and properties == ["pipeline"]
            return [
                {
                    "id": value,
                    "properties": {"pipeline": "allowed" if value == "d1" else "other"},
                }
                for value in ids
            ]

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", Portal)
    assert crm_visibility.visible_source_ids(
        {"co1", "co2"}, {"ct1", "ct2"}, {"d1", "d2"}
    ) == ({"co1"}, {"ct1"}, {"d1"})


def test_unavailable_or_malformed_scope_has_no_visible_ids(monkeypatch):
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: SimpleNamespace(pipeline_ids=()),
    )
    assert crm_visibility.visible_source_ids({"co1"}, set(), set()) == (
        set(),
        set(),
        set(),
    )
    assert crm_visibility.visible_source_ids({"co/1"}, set(), set()) == (
        set(),
        set(),
        set(),
    )
