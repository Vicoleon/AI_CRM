"""Long saved histories use one live CRM proof without admitting revoked rows."""

from __future__ import annotations

from rubik_core.meeting_prep.provenance import contexts_visible, stamp
from rubik_core.tenancy import crm_visibility


def _bundle(company_id: str):
    return {
        "crm_provenance": stamp(company_ids={company_id}, deal_ids=set()),
        "account_snapshot": {"companies": [{"hubspot_company_id": company_id}]},
        "signal_blocks": {},
    }


def test_version_histories_share_one_live_source_scan(monkeypatch):
    calls = []

    def visible(companies, contacts, deals):
        calls.append((companies, contacts, deals))
        return companies & {"co1"}, contacts, deals

    monkeypatch.setattr(crm_visibility, "visible_source_ids", visible)
    assert contexts_visible([_bundle("co1"), _bundle("co2"), {}]) == [
        True,
        False,
        False,
    ]
    assert calls == [({"co1", "co2"}, set(), set())]
