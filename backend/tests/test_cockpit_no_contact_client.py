"""My Day's "No contact in 90 days" block reads the deal AND its client.

A deal's own "Last Contacted" only moves when the email or call is logged on
that deal. An email to someone at the client who is not on the deal is logged
on their company instead, so a deal can read "no contact in 82 days" when the
client was emailed 13 days earlier. The same deal-only reading fed this block.

What is pinned here:

* every deal that qualified before still qualifies, and its own number stays
  on the row;
* a deal whose client was contacted inside the 90 days says so, is titled for
  the deal rather than the client, and ranks after the deals whose whole
  client has gone quiet;
* only the deals that are quiet on their own cost a company read;
* a failed company read keeps today's list and says, in a code the client
  declares, what it did not count.

Nothing here talks to HubSpot: the client is faked at its factory and patched
by STRING target, because other suites ``importlib.reload()`` modules.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.routers import cockpit
from rubik_core.retrieval.hubspot_api import HubSpotAPIError
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

NOW = datetime.now(timezone.utc)
AE = "ae@acme.example"
_COCKPIT_TS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "cockpit.ts"
)


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


class _FakeHubSpot:
    """The four calls the stale list makes, and a record of them."""

    def __init__(self) -> None:
        self.deals: list[dict] = []
        self.companies: dict[str, dict] = {}
        self.links: dict[str, list[str]] = {}
        self.fail_company_read = False
        self.calls: list[tuple] = []

    def deal(self, rid: str, name: str, *, quiet: int | None) -> None:
        props = {
            "hs_object_id": rid,
            "dealname": name,
            "amount": "10000",
            "pipeline": "default",
        }
        if quiet is not None:
            props["notes_last_contacted"] = _ago(quiet)
        self.deals.append({"id": rid, "properties": props})

    def client(self, deal_id: str, company_id: str, name: str, *, quiet: int) -> None:
        self.companies[company_id] = {"name": name, "notes_last_contacted": _ago(quiet)}
        self.links.setdefault(deal_id, []).append(company_id)

    def search_all(self, object_type, filters, properties, sorts=None):
        self.calls.append(("search", object_type))
        return list(self.deals) if object_type == "deals" else []

    def batch_associations(self, from_type, to_type, ids, *, prefer_type_id=None):
        self.calls.append(("assoc", from_type, to_type, tuple(ids)))
        return {str(i): list(self.links.get(str(i), [])) for i in ids}

    def batch_read(self, object_type, ids, properties, *, properties_with_history=None):
        self.calls.append(("read", object_type, tuple(ids)))
        if object_type == "companies" and self.fail_company_read:
            raise HubSpotAPIError(
                "HubSpot transport error: Illegal header value "
                "b'Bearer pat-na1-SECRET-TOKEN\\n'"
            )
        return [
            {"id": i, "properties": self.companies[i]}
            for i in ids
            if i in self.companies
        ]


@pytest.fixture()
def hs(monkeypatch):
    fake = _FakeHubSpot()
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda *a, **k: fake
    )
    monkeypatch.setattr(
        "backend.signals.hubspot._cached_directories",
        lambda client: ({"77": {"email": AE, "name": "Parker"}}, {}),
    )
    monkeypatch.setattr(
        cockpit,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )
    return fake


def _crm():
    return cockpit._crm_context(
        owner_email=AE, deal_ids=[], company_ids=[], want_stale=True
    )


def test_a_deal_whose_client_heard_from_us_says_so_and_ranks_last(hs):
    """A deal quiet on its own record whose client heard from us lately."""
    hs.deal("1", "Contoso Ltd - Annual Review", quiet=120)
    hs.client("1", "co1", "Contoso Ltd", quiet=13)
    hs.deal("2", "Quiet Co - Rollout", quiet=100)

    crm = _crm()
    block = cockpit._no_contact_block(crm=crm)

    assert [i.id for i in block.items] == ["2", "1"]
    quiet, deal_only = block.items
    # The deal whose whole client is quiet reads exactly as it always has.
    assert quiet.title == "No contact in 90 days · Quiet Co - Rollout"
    assert quiet.subtitle == "100d since contact"
    assert quiet.age.label == "100d since contact"
    # The other is titled for the deal, with the deal's own number on the chip.
    assert deal_only.title == (
        "No contact logged on this deal in 90 days · Contoso Ltd - Annual Review"
    )
    assert deal_only.subtitle == (
        "Contoso Ltd was contacted 13 days ago, but no logged contact on this "
        "deal in 120 days"
    )
    assert deal_only.age.label == "120d on this deal"
    # Same one action: log the email on the deal.
    assert deal_only.primary_action.kind == "log_outreach"
    assert block.unavailable_reason is None


def test_a_client_quiet_past_ninety_days_too_counts_from_the_newer_date(hs):
    hs.deal("1", "Older on the deal", quiet=150)
    hs.client("1", "co1", "Beta Co", quiet=100)
    hs.deal("2", "Plain quiet", quiet=120)

    block = cockpit._no_contact_block(crm=_crm())

    # Longest silence first, counted from the newer of deal and client: the
    # 150-day deal's client heard from us 100 days ago, so it reads 100.
    assert [i.id for i in block.items] == ["2", "1"]
    row = block.items[1]
    assert row.title == "No contact in 90 days · Older on the deal"
    assert row.subtitle == (
        "No logged contact in 100 days (none on this deal itself in 150 days)"
    )
    assert row.age.label == "100d since contact"


def test_a_never_contacted_deal_still_leads_and_a_never_contacted_client_too(hs):
    hs.deal("1", "Nothing anywhere", quiet=None)
    hs.deal("2", "Nothing on the deal", quiet=None)
    hs.client("2", "co2", "Gamma Co", quiet=5)
    hs.deal("3", "Quiet", quiet=95)

    block = cockpit._no_contact_block(crm=_crm())

    assert [i.id for i in block.items] == ["1", "3", "2"]
    assert block.items[0].subtitle == "no logged contact on record"
    assert block.items[2].subtitle == (
        "Gamma Co was contacted 5 days ago, but this deal has no logged contact "
        "on record"
    )
    assert block.items[2].age.label == "none on this deal"


def test_an_older_client_date_changes_nothing(hs):
    hs.deal("1", "Deal is newer", quiet=95)
    hs.client("1", "co1", "Delta Co", quiet=200)

    row = cockpit._no_contact_block(crm=_crm()).items[0]

    assert row.title == "No contact in 90 days · Deal is newer"
    assert row.subtitle == "95d since contact"


def test_only_the_quiet_deals_cost_a_company_read(hs):
    hs.deal("fresh", "Fresh", quiet=10)
    hs.deal("stale", "Stale", quiet=120)

    _crm()

    assert [c for c in hs.calls if c[0] == "assoc"] == [
        ("assoc", "deals", "companies", ("stale",))
    ]


def test_no_quiet_deal_means_no_company_read_at_all(hs):
    hs.deal("fresh", "Fresh", quiet=10)

    crm = _crm()

    assert crm.stale_deals == []
    assert not [c for c in hs.calls if c[0] in ("assoc", "read")]


def test_a_failed_company_read_keeps_the_list_and_says_so(hs, caplog):
    hs.fail_company_read = True
    hs.deal("1", "Contoso Ltd - Annual Review", quiet=120)
    hs.client("1", "co1", "Contoso Ltd", quiet=13)
    hs.deal("2", "Quiet Co - Rollout", quiet=100)

    with caplog.at_level("WARNING"):
        crm = _crm()
    block = cockpit._no_contact_block(crm=crm)

    # Today's list, today's order (most days first), today's wording...
    assert [i.id for i in block.items] == ["1", "2"]
    assert block.items[0].subtitle == "120d since contact"
    # ...plus the reason the rows may be missing client contact.
    assert crm.stale_clients_unread and not crm.stale_unavailable
    assert block.unavailable_reason == "client_records_unavailable"
    # The failure is logged without the credential httpx quotes.
    assert "client company read failed" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_the_client_declares_the_new_block_code_with_its_own_sentence():
    """A code missing from BLOCK_UNAVAILABLE_REASONS falls back to the generic
    "could not load" line, which would tell the rep the rows are unreliable
    for the wrong reason."""
    from backend.tests.test_cockpit_transcript_fence import DECLARED_BLOCK_REASONS

    src = _COCKPIT_TS.read_text(encoding="utf-8")
    declared = src[
        src.index("export const BLOCK_UNAVAILABLE_REASONS") : src.index(
            "export type BlockUnavailableReason"
        )
    ]
    copy = src[src.index("export const BLOCK_UNAVAILABLE_COPY") :]
    assert "'client_records_unavailable'" in declared
    assert "client_records_unavailable:" in copy[: copy.index("};")]
    assert "client_records_unavailable" in DECLARED_BLOCK_REASONS
