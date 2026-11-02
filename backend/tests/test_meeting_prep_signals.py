"""Meeting Prep v2 — the newer signals a brief is built from.

What this asserts about each new source (rubik_core/meeting_prep/signals.py):

* HubSpot account record: an ambiguous domain is reported, never resolved to
  one of its companies; consumer mail hosts never reach HubSpot; a deal in the
  client's own pipeline (the portal also hosts Initech) never reaches the brief;
  the deal and company last-contact dates sit side by side.
* Customer X-Ray: present only for readers ``customer_xray`` admits.
* Open to-dos: the reader's own ledger, filtered to the people on the invite.
* Last meeting's notes: read only where the reader attended (or owns it), from
  the shared meetings that fit the invite, and only their notes — never
  Gemini's footer, a transcript tab or a notes-less transcript's dialogue.
* Dates are the reader's calendar dates; "Built from" counts what was used.
* Every block — and every prompt clause pointing at it — exists only when the
  source found something, so a run with none of the new data produces a prompt
  byte-identical to the one before (the UC-47 discipline).
* Kill switches stop the source before it is called.
* The client card and the "Built from" strip reach the response from the
  records, on the success path and on the failure path.

Every external client is faked; patches use string targets.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.routers.home import (
    collect_meeting_prep_context,
    generate_meeting_prep_analysis,
)
from rubik_core.meeting_prep import signals
from rubik_core.retrieval.hubspot_api import CompanyMatch, DomainMatch
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

USER = "bellison@acme.example"
COLLEAGUE = "ifoster@acme.example"
CLIENT = "pat.doe@acmeindustries.alt"
CLIENT_2 = "sam.roe@acmeindustries.alt"
PERSONAL = "someone@gmail.com"
AMBIGUOUS = "lee@betabank.com"

ALLOWED_PIPELINES = ["default", "9100002"]
ACME_ID = "101"


@pytest.fixture(autouse=True)
def _fresh_directory_cache():
    signals._DIR_CACHE.clear()
    yield
    signals._DIR_CACHE.clear()


def test_crm_directory_cache_is_workspace_and_token_scoped():
    from rubik_core.tenancy.context import use_workspace

    first = MagicMock(token="shared-token")
    first.deal_stage_labels.return_value = {"s1": "First stage"}
    first.owner_name_map.return_value = {"1": "First owner"}
    second = MagicMock(token="shared-token")
    second.deal_stage_labels.return_value = {"s2": "Second stage"}
    second.owner_name_map.return_value = {"2": "Second owner"}

    with use_workspace("acme"):
        assert signals._directories(first) == (
            {"s1": "First stage"},
            {"1": "First owner"},
        )
    with use_workspace("beta"):
        assert signals._directories(second) == (
            {"s2": "Second stage"},
            {"2": "Second owner"},
        )
    with use_workspace("acme"):
        assert signals._directories(first) == (
            {"s1": "First stage"},
            {"1": "First owner"},
        )
        rotated = MagicMock(token="rotated-token")
        rotated.deal_stage_labels.return_value = {"s3": "Rotated stage"}
        rotated.owner_name_map.return_value = {"3": "Rotated owner"}
        assert signals._directories(rotated) == (
            {"s3": "Rotated stage"},
            {"3": "Rotated owner"},
        )

    first.deal_stage_labels.assert_called_once()
    second.deal_stage_labels.assert_called_once()


# ── fakes ────────────────────────────────────────────────────────────────────


def _acme_match() -> DomainMatch:
    return DomainMatch(
        domain="acmeindustries.alt",
        candidates=(
            CompanyMatch(
                hubspot_company_id=ACME_ID,
                name="Acme Industries",
                domain="acmeindustries.alt",
                lifecyclestage="customer",
            ),
        ),
    )


def _beta_match() -> DomainMatch:
    return DomainMatch(
        domain="betabank.com",
        candidates=(
            CompanyMatch(
                hubspot_company_id="201", name="Beta Bank", domain="betabank.com"
            ),
            CompanyMatch(
                hubspot_company_id="202", name="Beta Logistics", domain="betabank.com"
            ),
        ),
    )


def _deal(deal_id, name, *, pipeline="default", stage="s2", amount="45000",
          close="2026-11-15T00:00:00Z", closed="false", won="false",
          contacted="2026-09-20T14:00:00Z", owner="o1"):  # fmt: skip
    return {
        "id": deal_id,
        "properties": {
            "dealname": name,
            "pipeline": pipeline,
            "dealstage": stage,
            "amount": amount,
            "closedate": close,
            "hubspot_owner_id": owner,
            "hs_is_closed": closed,
            "hs_is_closed_won": won,
            "notes_last_contacted": contacted,
        },
    }


class FakeHubSpot:
    """Records every deal search so a test can prove what was (not) asked."""

    def __init__(self, deals_by_company=None):
        self.deal_searches: list[list[dict]] = []
        self.deals_by_company = deals_by_company or {
            ACME_ID: [
                _deal("d1", "Acme — Platform Renewal 2027"),
                _deal(
                    "d2",
                    "Acme — Data review",
                    close="2026-03-01T00:00:00Z",
                    closed="true",
                    won="true",
                    contacted="2026-03-02T00:00:00Z",
                ),
            ]
        }

    def fully_owned_deal_associations(self, object_type, ids):
        assert object_type == "companies"
        return {
            cid: [str(deal["id"]) for deal in self.deals_by_company[cid]]
            for cid in ids
            if cid in self.deals_by_company
            and self.deals_by_company[cid]
            and all(
                deal["properties"]["pipeline"] in ALLOWED_PIPELINES
                for deal in self.deals_by_company[cid]
            )
        }

    def deal_stage_labels(self):
        return {"s2": "Proposal Sent"}

    def owner_name_map(self):
        return {"o1": "Jane Rep"}

    def batch_read(self, object_type, ids, properties, **kwargs):
        assert object_type == "companies"
        return [
            {
                "id": cid,
                "properties": {
                    "name": "Acme Industries",
                    "lifecyclestage": "customer",
                    "notes_last_contacted": "2026-09-28T10:00:00Z",
                },
            }
            for cid in ids
        ]

    def search_all(self, object_type, filters, properties, sorts=None):
        assert object_type == "deals"
        self.deal_searches.append(filters)
        company = next(
            f["value"] for f in filters if f["propertyName"] == "associations.company"
        )
        return self.deals_by_company.get(company, [])


def _event(*attendees: str) -> dict:
    return {
        "id": "evt-1",
        "summary": "Northwind / Acme — renewal",
        "start": {"dateTime": "2026-10-07T15:00:00Z"},
        "end": {"dateTime": "2026-10-07T16:00:00Z"},
        "attendees": [
            {"email": e, "display_name": n}
            for e, n in [(a, _NAMES.get(a, a)) for a in attendees]
        ],
        "description": "",
        "attachments": [],
    }


_NAMES = {
    CLIENT: "Pat Doe",
    CLIENT_2: "Sam Roe",
    COLLEAGUE: "Indigo Foster",
    USER: "Blair Ellison",
}


def _reader(department="sales"):
    return SimpleNamespace(
        email=USER, department=department, roles=["user"], is_exec=False
    )


def _promise(pid, title, *, kind="person", cid=None, label=None, private=False,
             due=date(2026, 10, 3), status="open"):  # fmt: skip
    return SimpleNamespace(
        id=pid,
        title=title,
        status=status,
        due_local=due,
        counterparty_kind=kind,
        counterparty_id=cid,
        counterparty_label=label,
        is_private=private,
    )


def _xray_snapshot():
    return SimpleNamespace(
        company_id=ACME_ID,
        company_name="Acme Industries",
        version=3,
        created_at=datetime(2026, 9, 12, 14, 0),
        sections={
            "executive_summary": "Steady client; renewal due in Q1.",
            "relationship_health": {"narrative_md": "Weekly cadence with ops."},
            "opportunities": [
                {"title": "Data cleanup", "why_now": "New ERP migration in Q4."}
            ],
        },
        signals={"churn": {"score": 42, "band": "medium"}},
    )


def _collect(
    event: dict,
    *,
    reader=None,
    matches=None,
    client=None,
    allow=frozenset({"customer_xray", "todos"}),
    xray=True,
    promises=(),
    time_off=(),
    bundle=None,
    shared=(),
    loader=None,
    overrides=None,
    emails=(),
    today=(date(2026, 10, 1), "America/New_York"),
):
    """collect_meeting_prep_context with every source faked. Returns
    (ctx, resolve spy, client, snapshot_index spy)."""
    tools = MagicMock()

    async def _no_meetings(**kwargs):
        return {"status": "error"}

    tools.search_past_meetings = _no_meetings

    async def _no_research(attendees, **kwargs):
        return SimpleNamespace(
            intel=[], skipped_attendees=[], errors=[], as_dicts=lambda: []
        )

    client = client or FakeHubSpot()
    if matches is None:
        matches = {"acmeindustries.alt": _acme_match(), "betabank.com": _beta_match()}

    resolve_spy = MagicMock(
        side_effect=lambda domains, **kw: {
            d: matches[d] for d in domains if d in matches
        }
    )
    snapshot_index = MagicMock(return_value={ACME_ID: _xray_snapshot()} if xray else {})
    hubspot_token = (overrides or {}).get("hubspot_read_token", "test-token")
    settings_patches = {
        "TODOS_ENABLED": True,
        "PROMISE_ACCOUNTABILITY_ENABLED": False,
        **{
            key: value
            for key, value in (overrides or {}).items()
            if key != "hubspot_read_token"
        },
    }
    stack = [
        patch(
            "rubik_core.tenancy.hubspot_config.get_hubspot_config",
            return_value=HubSpotConnectorConfig(
                pipeline_ids=tuple(ALLOWED_PIPELINES), portal_id="9100001"
            ),
        ),
        patch(
            "rubik_core.sales.rhythm_checks.get_hubspot_config",
            return_value=HubSpotConnectorConfig(
                pipeline_ids=tuple(ALLOWED_PIPELINES), portal_id="9100001"
            ),
        ),
        patch(
            "rubik_core.meeting_prep.signals.workspace_secret",
            side_effect=lambda connector, field: (
                hubspot_token
                if (connector, field) == ("hubspot", "read_token")
                else None
            ),
        ),
        patch("rubik_core.adk_agent.tools.AgentTools", return_value=tools),
        patch("backend.routers.home._resolve_attendee_names", lambda a: None),
        patch(
            "backend.routers.home._fetch_emails_with_people", return_value=list(emails)
        ),
        patch("backend.routers.home._fetch_emails_for_domains", return_value=[]),
        patch(
            "rubik_core.retrieval.product_catalog.get_product_catalog",
            return_value=MagicMock(search_products=MagicMock(return_value=[])),
        ),
        patch("rubik_core.tools.drive.GoogleDriveClient", side_effect=RuntimeError),
        patch("rubik_core.people.research_attendees", _no_research),
        patch(
            "rubik_core.database.repository"
            ".SQLMeetingParticipantsRepository.list_shared_meetings",
            MagicMock(return_value=list(shared)),
        ),
        patch(
            "rubik_core.call_capture.domain_match.resolve_domain_matches", resolve_spy
        ),
        patch("rubik_core.retrieval.hubspot_api.HubSpotAPIClient", return_value=client),
        patch(
            "rubik_core.features.feature_allows",
            side_effect=lambda key, user, db=None: key in allow,
        ),
        patch("rubik_core.meeting_prep.signals._session", MagicMock()),
        patch("rubik_core.xray.persistence.snapshot_index", snapshot_index),
        patch(
            "rubik_core.promises.store.list_open",
            MagicMock(return_value=list(promises)),
        ),
        patch(
            "rubik_core.meeting_prep.signals.fetch_time_off",
            MagicMock(return_value=list(time_off)),
        ),
        patch(
            "backend.routers.home._load_today_priorities",
            loader or MagicMock(return_value=bundle),
        ),
        patch("rubik_core.meeting_prep.signals.reader_today", return_value=today),
        patch("rubik_core.meeting_prep.signals.resolve_reader", return_value=None),
    ]
    stack += [
        patch(f"rubik_core.config.settings.{k}", v) for k, v in settings_patches.items()
    ]
    for p in stack:
        p.start()
    try:
        ctx = asyncio.run(
            collect_meeting_prep_context(
                user_email=USER,
                user_department="Sales",
                user_roles=[],
                event=event,
                reader=reader,
            )
        )
    finally:
        for p in reversed(stack):
            p.stop()
    return ctx, resolve_spy, client, snapshot_index


# ── HubSpot account record ───────────────────────────────────────────────────


def test_crm_domains_skip_personal_and_own_domains_and_cap_at_three():
    attendees = [
        {"email": e}
        for e in (
            USER,
            COLLEAGUE,
            PERSONAL,
            CLIENT,
            "a@one.com",
            "b@two.com",
            "c@three.com",
        )
    ]
    domains, skipped = signals.crm_domains(attendees, USER)

    assert "gmail.com" not in domains + skipped
    assert "acme.example" not in domains + skipped
    assert len(domains) == 3
    assert sorted(domains + skipped) == sorted(
        ["acmeindustries.alt", "one.com", "two.com", "three.com"]
    )


def test_gmail_attendee_never_reaches_hubspot():
    ctx, resolve_spy, _, _ = _collect(_event(USER, CLIENT, PERSONAL))

    asked = resolve_spy.call_args[0][0]
    assert asked == ["acmeindustries.alt"]
    assert "gmail.com" not in ctx["signal_blocks"]["crm"]


def test_account_record_reaches_context_with_both_last_contact_dates():
    ctx, _, client, _ = _collect(_event(USER, CLIENT), reader=_reader())

    assert ctx["crm_provenance"] == {
        "schema_version": 1,
        "workspace": "test",
        "company_ids": [ACME_ID],
        # The won deal also entered the model as a summary/count.
        "deal_ids": ["d1", "d2"],
        "unprovable": False,
    }

    block = ctx["signal_blocks"]["crm"]
    assert block.startswith("## CRM ACCOUNT RECORD (HubSpot, live")
    assert '"Acme — Platform Renewal 2027"; stage Proposal Sent; $45,000' in block
    assert "close date 2026-11-15; owner Jane Rep" in block
    assert (
        'Won deals: 1 (most recent: "Acme — Data review", closed 2026-03-01)' in block
    )
    # The company's date sits BESIDE the deal date, never instead of it.
    assert (
        "Last logged contact — on its deals: 2026-09-20; on the company record: "
        "2026-09-28"
    ) in block

    snap = ctx["account_snapshot"]
    acme = snap["companies"][0]
    assert acme["hubspot_company_id"] == ACME_ID
    assert acme["hubspot_url"].endswith(f"/company/{ACME_ID}")
    assert acme["lifecycle_stage"] == "customer"
    assert acme["open_deals"] == [
        {
            "name": "Acme — Platform Renewal 2027",
            "stage": "Proposal Sent",
            "amount": 45000.0,
            "close_date": "2026-11-15",
            "owner": "Jane Rep",
            "url": acme["open_deals"][0]["url"],
        }
    ]
    assert acme["open_deals"][0]["url"].endswith("/deal/d1")
    assert acme["won_deals_count"] == 1
    assert acme["last_won"] == {
        "name": "Acme — Data review",
        "close_date": "2026-03-01",
    }
    assert acme["deal_last_contact"] == "2026-09-20"
    assert acme["company_last_contact"] == "2026-09-28"
    assert acme["ambiguous"] is False
    assert snap["as_of"]


def test_the_clients_pipeline_never_reaches_the_brief():
    client = FakeHubSpot()
    client.deals_by_company[ACME_ID].append(
        _deal(
            "d9",
            "Initech — client pipeline deal",
            pipeline="initech-client",
            amount="999999",
            contacted="2026-09-30T00:00:00Z",
        )
    )
    ctx, _, client, _ = _collect(_event(USER, CLIENT), client=client)

    block = ctx["signal_blocks"]["crm"]
    assert "Initech" not in block
    assert "999,999" not in block
    acme = ctx["account_snapshot"]["companies"][0]
    assert acme["hubspot_company_id"] is None
    assert acme["open_deals"] == []
    assert acme["company_last_contact"] is None
    assert client.deal_searches == []


def test_ambiguous_domain_is_reported_not_resolved():
    ctx, _, client, _ = _collect(_event(USER, AMBIGUOUS))

    # No deal search for either candidate — nothing was picked.
    assert client.deal_searches == []
    beta = ctx["account_snapshot"]["companies"][0]
    assert beta["ambiguous"] is True
    assert beta["hubspot_company_id"] is None
    assert "Multiple HubSpot companies match" in beta["note"]
    assert "Beta Bank" not in beta["note"]
    block = ctx["signal_blocks"]["crm"]
    assert "Do not assume which one" in block
    assert "Proposal Sent" not in block


def test_hubspot_error_is_a_partial_error_not_a_guess():
    matches = {
        "acmeindustries.alt": DomainMatch(domain="acmeindustries.alt", error="429")
    }
    ctx, _, _, _ = _collect(_event(USER, CLIENT), matches=matches)

    assert any(
        "HubSpot couldn't be checked for acmeindustries.alt" in e for e in ctx["errors"]
    )
    assert "crm" not in ctx["signal_blocks"]
    assert ctx["account_snapshot"] is None


def test_unreadable_deals_say_so_instead_of_reading_as_none():
    client = FakeHubSpot()
    client.search_all = MagicMock(side_effect=RuntimeError("socket closed"))
    ctx, _, _, _ = _collect(_event(USER, CLIENT), client=client)

    assert "HubSpot deals couldn't be read for Acme Industries." in ctx["errors"]
    acme = ctx["account_snapshot"]["companies"][0]
    assert acme["won_deals_count"] is None and acme["open_deals"] == []
    block = ctx["signal_blocks"]["crm"]
    assert "Deal details couldn't be read from HubSpot." in block
    assert "Open deals (our pipelines): none" not in block
    assert "on its deals: not read; on the company record: 2026-09-28" in block
    # The "Built from" strip must not turn "could not read" into "0 open deals".
    hubspot = next(s for s in ctx["signals_used"] if s["key"] == "hubspot")
    assert hubspot["detail"] == "Acme Industries · deals not read"


def test_built_from_counts_deals_only_where_they_were_read():
    acme = {
        "name": "Acme",
        "hubspot_company_id": "1",
        "won_deals_count": 0,
        "open_deals": [{"name": "A"}, {"name": "B"}],
    }
    beta = {"name": "Beta", "hubspot_company_id": "2", "won_deals_count": None,
            "open_deals": []}  # fmt: skip
    used = signals.compute_signals_used(
        {
            "signal_blocks": {"crm": "## CRM ACCOUNT RECORD"},
            "account_snapshot": {"companies": [acme, beta]},
        }
    )
    assert used == [
        {"key": "hubspot", "label": "HubSpot",
         "detail": "2 companies · 2 open deals (deals not read for 1 company)"},
    ]  # fmt: skip


def test_last_contact_dates_and_as_of_are_the_readers_calendar_dates():
    # 01:00Z on Oct 1 is 9 PM on Sep 30 in New York: HubSpot shows Sep 30.
    client = FakeHubSpot(
        {
            ACME_ID: [
                _deal(
                    "d1",
                    "Acme — Platform Renewal 2027",
                    contacted="2026-10-01T01:00:00Z",
                )
            ]
        }
    )
    # The company date as epoch ms (another shape HubSpot sends): the same instant.
    company = {"name": "Acme Industries", "notes_last_contacted": "1790816400000"}
    client.batch_read = lambda *a, **k: [{"id": ACME_ID, "properties": company}]
    ctx, _, _, _ = _collect(
        _event(USER, CLIENT),
        client=client,
        today=(date(2026, 9, 30), "America/New_York"),
    )

    acme = ctx["account_snapshot"]["companies"][0]
    assert acme["deal_last_contact"] == "2026-09-30"
    assert acme["company_last_contact"] == "2026-09-30"
    block = ctx["signal_blocks"]["crm"]
    assert "on its deals: 2026-09-30; on the company record: 2026-09-30" in block
    assert "(as of 2026-09-30; deals in our own pipelines only)" in block


def test_crm_kill_switch_stops_the_lookup():
    ctx, resolve_spy, _, _ = _collect(
        _event(USER, CLIENT),
        overrides={"CUBIE_PREP_CRM_ENABLED": False, "CUBIE_PREP_XRAY_ENABLED": False},
    )

    resolve_spy.assert_not_called()
    assert "crm" not in ctx["signal_blocks"]
    assert ctx["account_snapshot"] is None


def test_no_hubspot_token_means_no_lookup():
    ctx, resolve_spy, _, _ = _collect(
        _event(USER, CLIENT), overrides={"hubspot_read_token": ""}
    )

    resolve_spy.assert_not_called()
    assert ctx["account_snapshot"] is None


# ── Customer X-Ray ───────────────────────────────────────────────────────────


def test_xray_reaches_the_card_and_prompt_when_the_reader_has_xray():
    ctx, _, _, snapshot_index = _collect(_event(USER, CLIENT), reader=_reader())

    snapshot_index.assert_called_once()
    block = ctx["signal_blocks"]["xray"]
    assert "### Acme Industries — X-Ray v3, 2026-09-12" in block
    assert "Churn risk: medium (42/100)" in block
    assert "Data cleanup — New ERP migration in Q4." in block
    card = ctx["account_snapshot"]["companies"][0]["xray"]
    assert card == {
        "version": 3,
        "churn_band": "medium",
        "churn_score": 42,
        "summary": "Steady client; renewal due in Q1.",
        "created_at": "2026-09-12T14:00:00",
        "url": f"/customers/{ACME_ID}/xray",
    }


def test_xray_absent_when_feature_does_not_admit_the_reader():
    ctx, _, _, snapshot_index = _collect(
        _event(USER, CLIENT), reader=_reader("operations"), allow=frozenset({"todos"})
    )

    snapshot_index.assert_not_called()
    assert "xray" not in ctx["signal_blocks"]
    assert ctx["account_snapshot"]["companies"][0]["xray"] is None


def test_xray_skipped_without_a_reader_profile():
    """No reader passed and none loadable (resolve_reader → None): fail closed."""
    ctx, _, _, snapshot_index = _collect(_event(USER, CLIENT), reader=None)

    snapshot_index.assert_not_called()
    assert "xray" not in ctx["signal_blocks"]


def test_xray_kill_switch():
    ctx, _, _, snapshot_index = _collect(
        _event(USER, CLIENT),
        reader=_reader(),
        overrides={"CUBIE_PREP_XRAY_ENABLED": False},
    )

    snapshot_index.assert_not_called()
    assert "xray" not in ctx["signal_blocks"]


# ── Open to-dos ──────────────────────────────────────────────────────────────

_PROMISES = [
    _promise("p1", "Send the revised SOW", cid=CLIENT, label="Pat Doe"),
    _promise("p2", "Review Q3 numbers", cid="someone.else@other.com", label="Other"),
    _promise("p3", "Private note about Pat", cid=CLIENT, private=True),
    _promise(
        "p4",
        "Send Acme the data sample",
        kind="company",
        cid=ACME_ID,
        label="Acme Industries",
        status="slipping",
    ),  # fmt: skip
    _promise("p5", "Call Sam back", label="Sam Roe", due=date(2026, 9, 29)),
]


def test_promises_are_filtered_to_the_people_on_the_invite():
    ctx, _, _, _ = _collect(
        _event(USER, CLIENT, CLIENT_2), reader=_reader(), promises=_PROMISES
    )

    titles = [p["title"] for p in ctx["open_promises"]]
    assert titles == [
        "Send the revised SOW",
        "Send Acme the data sample",
        "Call Sam back",
    ]
    block = ctx["signal_blocks"]["promises"]
    assert "Review Q3 numbers" not in block
    assert "Private note" not in block
    assert "- Send the revised SOW — for Pat Doe — due Sat Oct 3" in block
    assert "(slipping)" in block
    assert "Call Sam back — for Sam Roe — due Tue Sep 29 (overdue)" in block


def test_promises_need_the_feature_and_a_reader():
    ctx, _, _, _ = _collect(
        _event(USER, CLIENT),
        reader=_reader(),
        promises=_PROMISES,
        allow=frozenset({"customer_xray"}),
    )
    assert "promises" not in ctx["signal_blocks"]

    ctx, _, _, _ = _collect(_event(USER, CLIENT), reader=None, promises=_PROMISES)
    assert "promises" not in ctx["signal_blocks"]

    ctx, _, _, _ = _collect(
        _event(USER, CLIENT),
        reader=_reader(),
        promises=_PROMISES,
        overrides={"TODOS_ENABLED": False},
    )
    assert "promises" not in ctx["signal_blocks"]


def test_promises_kill_switch():
    ctx, _, _, _ = _collect(
        _event(USER, CLIENT),
        reader=_reader(),
        promises=_PROMISES,
        overrides={"CUBIE_PREP_PROMISES_ENABLED": False},
    )
    assert "promises" not in ctx["signal_blocks"]


# ── Last meeting's notes ─────────────────────────────────────────────────────

_FATHOM_TEXT = """Pat Doe: Morning all.
Blair Ellison: Let's start with the renewal.

## Action Items & Next Steps (from Fathom)
- Blair to send the revised SOW by Friday (owner: Blair Ellison)
- Pat to confirm the Q1 sample size

## Meeting Summary (from Fathom)
Acme wants the renewal priced per seat."""


def test_extract_agreed_prefers_fathom_action_items_and_summary():
    text = signals.extract_agreed(_FATHOM_TEXT)

    assert text.startswith("Action Items & Next Steps (from Fathom):")
    assert "send the revised SOW by Friday" in text
    assert "priced per seat" in text
    assert "Morning all" not in text


_SMALL_TALK = "\n".join(
    f"{'Pat Doe' if i % 2 else 'Blair Ellison'}: how was the weekend, line {i}?"
    for i in range(200)
)

# A "Notes by Gemini" doc exported as text/plain with transcription on: the
# notes tab, Gemini's footer, then the transcript tab.
_GEMINI_EXPORT = (
    """Notes
Oct 1, 2026
Acme weekly
Invited Pat Doe Blair Ellison
Attachments Acme weekly
Meeting records Transcript

Summary
Acme is waiting on budget approval and wants the renewal priced per seat.

Details
"""
    + "\n".join(
        f"* Topic {i}: a long paragraph of discussion detail." for i in range(60)
    )
    + """

Suggested next steps
* Blair Ellison will send the revised SOW by Friday.
* Pat Doe will confirm the Q1 sample size.

You should review Gemini's notes to make sure they're accurate. Get tips and learn how Gemini takes notes
Please provide feedback about using Gemini to take notes in a short survey.
📖 Transcript
Oct 1, 2026
Acme weekly - Transcript
00:00:00

"""
    + _SMALL_TALK
)


def test_extract_agreed_stops_gemini_notes_at_the_footer_and_keeps_the_summary():
    text = signals.extract_agreed(_GEMINI_EXPORT)

    assert text.startswith(
        "Suggested next steps:\n* Blair Ellison will send the revised"
    )
    assert "confirm the Q1 sample size." in text
    # The Summary survives the long Details that follow it…
    assert "budget approval" in text and "per seat" in text
    # …and neither the footer nor the transcript tab is read as notes.
    assert "You should review Gemini" not in text
    assert "Transcript" not in text
    assert "how was the weekend" not in text
    assert len(text) <= signals.LAST_MEETING_NOTES_MAX_CHARS


@pytest.mark.parametrize(
    "marker",
    [
        "📖 Transcript",
        "Acme weekly - 2026/10/01 10:00 CDT - Transcript",
        "00:00:00",
        "You should review Gemini's notes to make sure they're accurate.",
    ],
)
def test_each_end_of_notes_marker_ends_the_section(marker):
    text = signals.extract_agreed(
        f"Suggested next steps\n* Send the SOW.\n{marker}\nPat Doe: hi there"
    )
    assert text == "Suggested next steps:\n* Send the SOW."


def test_a_transcript_without_notes_gives_no_agreed_block():
    # A Call Capture transcript: a header and dialogue, no notes heading. Its
    # opening is greetings, not what was agreed — so no block at all.
    raw = (
        "Sales call transcript\nAE: Blair Ellison\nDate: 2026-09-23\n"
        "Speakers: Pat Doe, Blair Ellison\n\n" + _SMALL_TALK
    )
    assert signals.extract_agreed(raw) == ""


def _wire_sqlite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/signals.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    return TestSession


def _seed_transcript(TestSession, doc_id, *, owner, participants, created):
    from rubik_core.database import repository
    from rubik_core.database.models import Asset

    with TestSession() as s:
        s.add(
            Asset(
                doc_id=doc_id,
                owner_email=owner,
                status="approved",
                topic="Meeting Transcripts",
                original_filename=f"{doc_id} call",
                source_path=f"gs://bucket/{doc_id}.txt",
                created_at=created,
                updated_at=created,
            )
        )
        s.commit()
    repository.SQLMeetingParticipantsRepository().upsert_participants(
        doc_id, meeting_id=f"m-{doc_id}", emails=participants
    )


def test_last_meeting_notes_are_read_only_where_the_reader_attended(
    tmp_path, monkeypatch
):
    from rubik_core.database.repository import SharedMeeting

    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(
        TestSession, "ours", owner=CLIENT, participants=[USER, CLIENT],
        created=datetime(2026, 9, 23),
    )  # fmt: skip
    _seed_transcript(
        TestSession, "theirs", owner=CLIENT, participants=[CLIENT],
        created=datetime(2026, 9, 25),
    )  # fmt: skip
    shared = [
        # Even if a caller handed over a doc the reader never attended, the
        # re-check refuses to read it.
        SharedMeeting("theirs", "Acme internal", datetime(2026, 9, 25), [CLIENT]),
        SharedMeeting("ours", "Acme weekly", datetime(2026, 9, 23), [CLIENT]),
    ]
    reads: list[str] = []

    def _read(self, record, *, max_bytes=0):
        reads.append(record.doc_id)
        return _FATHOM_TEXT

    with patch(
        "backend.services.transcript_service.TranscriptService.read_transcript_text",
        _read,
    ):
        notes = signals.read_last_meeting_notes(USER, shared)

    assert reads == ["ours"]
    assert [n["doc_id"] for n in notes] == ["ours"]
    block = signals.format_last_meeting_block(notes)
    assert block.startswith("## WHAT WAS AGREED LAST TIME")
    assert '### Wednesday, September 23, 2026 — "Acme weekly"' in block
    assert "revised SOW by Friday" in block


def test_last_meeting_notes_come_from_the_meetings_that_fit_this_invite(
    tmp_path, monkeypatch
):
    """A 1:1 with a colleague the reader also sees in the L10 and other group
    meetings: the notes come from the last 1:1, never the L10's team actions."""
    from rubik_core.database.repository import SharedMeeting

    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    team = [f"p{i}@acme.example" for i in range(8)]
    seeded = [
        ("l10", "Sales L10", datetime(2026, 9, 29), [USER, COLLEAGUE, *team]),
        ("weekly", "Systems weekly", datetime(2026, 9, 28),
         [USER, COLLEAGUE, *team[:4]]),
        ("standup", "Daily standup", datetime(2026, 9, 27),
         [USER, COLLEAGUE, team[0]]),
        ("one_on_one", "Blair / Indigo - 2026/09/22 - Notes by Gemini",
         datetime(2026, 9, 22), [USER, COLLEAGUE]),
    ]  # fmt: skip
    for doc_id, _, created, people in seeded:
        _seed_transcript(
            TestSession, doc_id, owner=USER, participants=people, created=created
        )
    shared = [SharedMeeting(d, t, w, [COLLEAGUE]) for d, t, w, _ in seeded]
    reads: list[str] = []

    def _read(self, record, *, max_bytes=0):
        reads.append(record.doc_id)
        return _FATHOM_TEXT

    with patch(
        "backend.services.transcript_service.TranscriptService.read_transcript_text",
        _read,
    ):
        notes = signals.read_last_meeting_notes(
            USER,
            shared,
            title="Blair / Indigo 1:1",
            attendees=[{"email": USER}, {"email": COLLEAGUE},
                       {"email": "room-4@resource.calendar.google.com",
                        "resource": True}],
        )  # fmt: skip

    # The 10-person L10 is skipped; the title-matched 1:1 beats the newer group
    # meetings; the two chosen are shown newest first.
    assert reads == ["weekly", "one_on_one"]
    assert [n["doc_id"] for n in notes] == ["weekly", "one_on_one"]


def test_shared_meetings_rank_by_how_many_of_the_invitees_were_there():
    from rubik_core.database.repository import SharedMeeting

    newer = SharedMeeting("a", "Acme ops", datetime(2026, 9, 29), [CLIENT])
    fuller = SharedMeeting("b", "Acme ops", datetime(2026, 9, 20), [CLIENT, CLIENT_2])
    ranked = signals.rank_shared_meetings(
        [newer, fuller],
        title="Northwind / Acme — renewal",
        attendees=[{"email": USER}, {"email": CLIENT}, {"email": CLIENT_2}],
        user_email=USER,
        sizes={"a": 3, "b": 3},
    )
    assert [m.doc_id for m in ranked] == ["b", "a"]


def test_a_shared_meeting_without_notes_adds_no_block(tmp_path, monkeypatch):
    from rubik_core.database.repository import SharedMeeting

    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_transcript(
        TestSession, "call", owner=USER, participants=[USER, CLIENT],
        created=datetime(2026, 9, 23),
    )  # fmt: skip
    shared = [SharedMeeting("call", "Acme call", datetime(2026, 9, 23), [CLIENT])]
    with patch(
        "backend.services.transcript_service.TranscriptService.read_transcript_text",
        lambda self, record, *, max_bytes=0: "Pat Doe: Morning all.\n" + _SMALL_TALK,
    ):
        notes = signals.read_last_meeting_notes(USER, shared)

    assert notes == []
    assert signals.format_last_meeting_block(notes) == ""


def test_last_meeting_notes_wired_through_collect():
    from rubik_core.database.repository import SharedMeeting

    shared = [SharedMeeting("ours", "Acme weekly", datetime(2026, 9, 23), [CLIENT])]
    notes = [
        {
            "doc_id": "ours",
            "title": "Acme weekly",
            "when": datetime(2026, 9, 23),
            "text": "Action items:\n- Blair to send the SOW",
        }  # fmt: skip
    ]
    with patch(
        "rubik_core.meeting_prep.signals.read_last_meeting_notes",
        MagicMock(return_value=notes),
    ) as reader_spy:
        ctx, _, _, _ = _collect(_event(USER, CLIENT), shared=shared)

    reader_spy.assert_called_once()
    assert reader_spy.call_args[0] == (USER, shared)
    # The invite itself, so the read can pick the meetings that fit it.
    assert reader_spy.call_args.kwargs["title"] == "Northwind / Acme — renewal"
    assert [a["email"] for a in reader_spy.call_args.kwargs["attendees"]] == [
        USER,
        CLIENT,
    ]
    assert "WHAT WAS AGREED LAST TIME" in ctx["signal_blocks"]["last_meeting_notes"]
    assert any(s["key"] == "last_meeting_notes" for s in ctx["signals_used"])


def test_last_meeting_notes_kill_switch():
    from rubik_core.database.repository import SharedMeeting

    shared = [SharedMeeting("ours", "Acme weekly", datetime(2026, 9, 23), [CLIENT])]
    with patch(
        "rubik_core.meeting_prep.signals.read_last_meeting_notes", MagicMock()
    ) as reader_spy:
        ctx, _, _, _ = _collect(
            _event(USER, CLIENT),
            shared=shared,
            overrides={"CUBIE_PREP_LAST_MEETING_NOTES_ENABLED": False},
        )

    reader_spy.assert_not_called()
    assert "last_meeting_notes" not in ctx["signal_blocks"]


# ── Team availability ────────────────────────────────────────────────────────


def test_availability_block_for_internal_attendees():
    periods = [
        {
            "email": COLLEAGUE,
            "start": "2026-10-06",
            "end": "2026-10-10",
            "return_date": "2026-10-13",
        }
    ]
    ctx, _, _, _ = _collect(_event(USER, COLLEAGUE, CLIENT), time_off=periods)

    block = ctx["signal_blocks"]["availability"]
    assert (
        "- Indigo Foster is out Oct 6–10, back Oct 13 — that includes the day "
        "of this meeting."
    ) in block


def test_fetch_time_off_reads_the_cached_table(tmp_path, monkeypatch):
    from rubik_core.database.models import OOOPeriod

    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    with TestSession() as s:
        s.add_all(
            [
                OOOPeriod(
                    user_email="IFoster@acme.example",
                    start_date="2026-10-06",
                    end_date="2026-10-10",
                    return_date="2026-10-13",
                ),  # fmt: skip
                OOOPeriod(
                    user_email=COLLEAGUE,
                    start_date="2026-12-20",
                    end_date="2026-12-31",
                    return_date="2027-01-04",
                ),  # fmt: skip
                OOOPeriod(
                    user_email="other@acme.example",
                    start_date="2026-10-02",
                    end_date="2026-10-03",
                    return_date="2026-10-05",
                ),  # fmt: skip
            ]
        )
        s.commit()

    rows = signals.fetch_time_off([COLLEAGUE], today=date(2026, 10, 1))

    assert [(r["email"], r["start"]) for r in rows] == [(COLLEAGUE, "2026-10-06")]


def test_availability_kill_switch():
    periods = [{"email": COLLEAGUE, "start": "2026-10-06", "end": "2026-10-10",
                "return_date": "2026-10-13"}]  # fmt: skip
    ctx, _, _, _ = _collect(
        _event(USER, COLLEAGUE),
        time_off=periods,
        overrides={"CUBIE_PREP_AVAILABILITY_ENABLED": False},
    )
    assert "availability" not in ctx["signal_blocks"]


# ── What Cubie already flagged ───────────────────────────────────────────────


def test_flagged_cards_match_the_meeting_people_but_not_the_prep_reminder():
    bundle = {
        "priorities": [
            {"title": "Prep for the renewal", "priority_kind": "upcoming_prep",
             "entity_kind": "event", "entity_id": "evt-1", "urgency": "high"},
            {"title": "Pat is waiting on the SOW", "priority_kind": "follow_up_debt",
             "entity_kind": "person", "entity_id": CLIENT, "urgency": "high",
             "why_it_matters": "Asked Tuesday.", "suggested_action": "Send it."},
            {"title": "Unrelated", "entity_kind": "person",
             "entity_id": "x@elsewhere.com"},
            {"title": "Acme renewal stalled", "entity_kind": "company",
             "entity_id": "acmeindustries.alt", "urgency": "medium"},
        ]
    }  # fmt: skip
    ctx, _, _, _ = _collect(_event(USER, CLIENT), bundle=bundle)

    titles = [p["title"] for p in ctx["flagged_priorities"]]
    assert titles == ["Pat is waiting on the SOW", "Acme renewal stalled"]
    block = ctx["signal_blocks"]["flagged"]
    assert (
        "- [high] Pat is waiting on the SOW — Asked Tuesday. Suggested: Send it."
        in block
    )
    assert ctx["crm_provenance"]["unprovable"] is True


def test_flagged_kill_switch():
    bundle = {"priorities": [{"title": "Pat waiting", "entity_id": CLIENT}]}
    loader = MagicMock(return_value=bundle)
    ctx, _, _, _ = _collect(
        _event(USER, CLIENT),
        loader=loader,
        overrides={"CUBIE_PREP_FLAGGED_ENABLED": False},
    )
    assert "flagged" not in ctx["signal_blocks"]
    assert ctx["flagged_priorities"] == []
    loader.assert_not_called()


# ── Budgets ──────────────────────────────────────────────────────────────────


def test_a_slow_source_is_abandoned_at_its_budget():
    import time

    async def go():
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await signals.run_bounded(time.sleep, 1.5, timeout=0.05)
        return time.monotonic() - started

    assert asyncio.run(go()) < 1.0


def test_a_timed_out_source_is_a_partial_error_not_a_failed_brief():
    import time

    def _hang(*args, **kwargs):
        time.sleep(1.5)
        return []

    with (
        patch("rubik_core.meeting_prep.signals.SIGNAL_TIMEOUT_S", 0.3),
        patch("rubik_core.meeting_prep.signals.time_off_for_meeting", _hang),
    ):
        ctx, _, _, _ = _collect(_event(USER, COLLEAGUE, CLIENT))

    assert "The team time-off lookup took too long and was skipped." in ctx["errors"]
    assert "availability" not in ctx["signal_blocks"]


# ── Nothing new → nothing changes ────────────────────────────────────────────


def test_no_new_data_means_no_blocks_and_no_snapshot():
    ctx, _, _, _ = _collect(
        _event(USER, COLLEAGUE), matches={}, xray=False, promises=[], time_off=[]
    )

    assert ctx["signal_blocks"] == {}
    assert ctx["account_snapshot"] is None
    assert ctx["signals_used"] == []


def test_signals_used_lists_only_sources_that_contributed():
    ctx, _, _, _ = _collect(_event(USER, CLIENT), reader=_reader(), promises=_PROMISES)
    keys = [s["key"] for s in ctx["signals_used"]]

    assert keys == ["hubspot", "xray", "promises"]
    hubspot = ctx["signals_used"][0]
    assert hubspot == {
        "key": "hubspot",
        "label": "HubSpot",
        "detail": "Acme Industries · 1 open deal",
    }


def test_signals_used_counts_existing_sources():
    used = signals.compute_signals_used(
        {
            "email_threads_used": 3,
            "past_meetings_report": "Relevant excerpts from past meeting "
            "transcripts.\n\nA\n\n---\n\nB",
            "prior_meetings": [object()],
            "drive_context": "Doc: Plan\nbody\n\n---\n\nLinked Doc: abc\nbody",
            "attendee_intel": [{"email": "a"}, {"email": "b", "error": "x"}],
        }
    )
    assert used == [
        {"key": "gmail", "label": "Gmail", "detail": "3 threads"},
        {"key": "transcripts", "label": "Meeting transcripts", "detail": "2 excerpts"},
        {"key": "attendance", "label": "Attendance records",
         "detail": "1 shared meeting on record"},
        {"key": "drive", "label": "Google Drive", "detail": "2 linked docs"},
        {"key": "attendee_research", "label": "Attendee research",
         "detail": "1 person researched"},
    ]  # fmt: skip
    # A transcript search that found nothing did not contribute.
    assert (
        signals.compute_signals_used(
            {"past_meetings_report": "No relevant past meetings found."}
        )
        == []
    )


def _thread(i: int) -> dict:
    headers = [
        {"name": "Subject", "value": f"Renewal question {i}"},
        {"name": "From", "value": f"Pat Doe <{CLIENT}>"},
        {"name": "Date", "value": "Tue, 29 Sep 2026 10:00:00 -0500"},
    ]
    return {
        "id": f"t{i}",
        "messages": [{"payload": {"headers": headers}, "snippet": "x" * 400}],
    }


def test_built_from_counts_the_email_threads_the_prompt_carries():
    from backend.routers.home import _format_email_threads_for_prompt

    threads = [_thread(i) for i in range(9)]
    ctx, _, _, _ = _collect(_event(USER, CLIENT), matches={}, emails=threads)

    # The prompt's email block stops at its size cap, well short of nine.
    block = _format_email_threads_for_prompt(ctx["recent_emails"], user_email=USER)
    in_prompt = block.count("\n\n---\n\n") + 1
    assert len(ctx["recent_emails"]) == 9 and in_prompt < 9
    gmail = next(s for s in ctx["signals_used"] if s["key"] == "gmail")
    assert gmail["detail"] == f"{in_prompt} threads"
    assert signals.email_threads_in_block("(none found)") == 0


# ── The prompt ───────────────────────────────────────────────────────────────


def _fake_client(captured: dict, *, fail: bool = False):
    async def _generate_content(*, model, contents, config):
        captured["prompt"] = contents
        captured["system"] = config.system_instruction
        if fail:
            raise RuntimeError("model down")
        return SimpleNamespace(
            text=json.dumps(
                {
                    "executive_summary": "Renewal call.",
                    "stakeholder_map": [],
                    # The model volunteering a card must be ignored.
                    "account_snapshot": {"companies": [{"name": "Invented Inc"}]},
                    "signals_used": [{"key": "made_up"}],
                }
            )
        )

    aio = SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))
    return SimpleNamespace(aio=aio)


def _analyze(ctx: dict, *, fail: bool = False):
    captured: dict = {}
    with (
        patch(
            "rubik_core.models.get_genai_client",
            return_value=_fake_client(captured, fail=fail),
        ),
        patch("backend.routers.home._resolve_prep_debt", lambda *a, **k: None),
    ):
        resp = asyncio.run(generate_meeting_prep_analysis(ctx, USER))
    return resp, captured


def _base_ctx(**overrides) -> dict:
    ctx = {
        "title": "Northwind / Acme — renewal",
        "description": "",
        "location": "",
        "start": "2026-10-07T15:00:00Z",
        "start_local": "Wednesday Oct 7, 11:00 AM EDT",
        "attendees": [{"email": USER}, {"email": CLIENT}],
        "recent_emails": [],
        "past_meetings_report": "",
        "drive_context": "",
        "matched_products": [],
        "attendee_intel": [],
        "is_sales_user": True,
        "is_internal": False,
        "focus_on": None,
        "event_id": "evt-1",
        "errors": [],
    }
    ctx.update(overrides)
    return ctx


_BLOCKS = {
    "crm": "## CRM ACCOUNT RECORD (HubSpot, live — authoritative)\n- Acme",
    "xray": "## CUSTOMER X-RAY ON FILE (dated)\n### Acme — X-Ray v3",
    "last_meeting_notes": "## WHAT WAS AGREED LAST TIME (historical)\n### Sep 23",
    "promises": "## YOUR OPEN TO-DOS FOR THESE PEOPLE (ledger)\n- Send the SOW",
    "availability": "## TEAM AVAILABILITY (time off)\n- Indigo is out",
    "flagged": "## WHAT CUBIE ALREADY FLAGGED (today)\n- [high] Pat waiting",
}


@pytest.mark.parametrize(
    "persona",
    [
        {"is_sales_user": True, "is_internal": False},
        {"is_sales_user": False, "is_internal": True},
    ],
)
def test_prompt_is_byte_identical_without_new_signals(persona):
    _, before = _analyze(_base_ctx(**persona))
    _, empty = _analyze(_base_ctx(**persona, signal_blocks={}))
    _, blank = _analyze(_base_ctx(**persona, signal_blocks={"crm": "", "xray": ""}))

    assert empty["prompt"] == before["prompt"]
    assert empty["system"] == before["system"]
    assert blank["prompt"] == before["prompt"]
    assert blank["system"] == before["system"]
    for marker in (
        "CRM ACCOUNT RECORD",
        "CUSTOMER X-RAY",
        "WHAT WAS AGREED LAST TIME",
        "YOUR OPEN TO-DOS",
        "TEAM AVAILABILITY",
        "WHAT CUBIE ALREADY FLAGGED",
    ):
        assert marker not in before["prompt"]
        assert marker not in before["system"]


@pytest.mark.parametrize(
    "persona,heading",
    [
        ({"is_sales_user": True, "is_internal": False}, "## RESEARCH DATA\n"),
        ({"is_sales_user": False, "is_internal": True}, "## CONTEXTUAL DATA\n"),
    ],
)
def test_each_block_and_its_rule_reach_both_personas(persona, heading):
    _, captured = _analyze(_base_ctx(**persona, signal_blocks=dict(_BLOCKS)))
    prompt, system = captured["prompt"], captured["system"]

    # The blocks open the data section, in a fixed order.
    assert heading + _BLOCKS["crm"] + "\n\n" + _BLOCKS["xray"] in prompt
    for block in _BLOCKS.values():
        assert block in prompt
    # And each has its grounding rule.
    assert "CRM ACCOUNT RECORD is HubSpot's live record and is authoritative" in system
    assert "CUSTOMER X-RAY ON FILE is an earlier Cubie analysis" in system
    assert "WHAT WAS AGREED LAST TIME is taken from the notes" in system
    assert (
        "YOUR OPEN TO-DOS FOR THESE PEOPLE is the reader's own To-Do ledger" in system
    )
    assert "TEAM AVAILABILITY is time off on file" in system
    assert "WHAT CUBIE ALREADY FLAGGED lists today's AI Priorities" in system
    # And the clauses that point at them.
    assert "from CRM ACCOUNT RECORD exactly as written there" in prompt
    assert "Start from WHAT WAS AGREED LAST TIME" in prompt
    assert "Where TEAM AVAILABILITY shows a colleague out" in prompt
    assert "Where WHAT CUBIE ALREADY FLAGGED lists an open item" in prompt


def test_one_block_brings_only_its_own_rule():
    _, captured = _analyze(
        _base_ctx(signal_blocks={"availability": _BLOCKS["availability"]})
    )
    assert "TEAM AVAILABILITY is time off on file" in captured["system"]
    assert "CRM ACCOUNT RECORD" not in captured["system"]
    assert "CRM ACCOUNT RECORD" not in captured["prompt"]
    assert "open_items_you_owe" not in captured["prompt"]


def test_battle_brief_asks_for_what_you_owe_only_with_the_promises_block():
    _, without = _analyze(_base_ctx(signal_blocks={"crm": _BLOCKS["crm"]}))
    assert "open_items_you_owe" not in without["prompt"]

    _, with_promises = _analyze(
        _base_ctx(signal_blocks={"promises": _BLOCKS["promises"]})
    )
    assert (
        "- open_items_you_owe: A JSON ARRAY of what the reader still owes these "
        "attendees, drawn ONLY from YOUR OPEN TO-DOS FOR THESE PEOPLE"
    ) in with_promises["prompt"]


def test_internal_brief_folds_promises_into_what_you_owe():
    _, captured = _analyze(
        _base_ctx(
            is_sales_user=False,
            is_internal=True,
            signal_blocks={"promises": _BLOCKS["promises"]},
        )
    )
    assert (
        "never invent an obligation. Also include every item in YOUR OPEN TO-DOS "
        "FOR THESE PEOPLE, with its due date."
    ) in captured["prompt"]


_SNAPSHOT = {
    "companies": [{"name": "Acme Industries", "domain": "acmeindustries.alt"}],
    "as_of": "2026-10-01T12:00:00",
}
_USED = [{"key": "hubspot", "label": "HubSpot", "detail": "Acme · 1 open deal"}]


def test_snapshot_and_strip_are_copied_from_the_records_not_the_model():
    resp, _ = _analyze(
        _base_ctx(account_snapshot=_SNAPSHOT, signals_used=_USED, signal_blocks={})
    )

    assert resp.account_snapshot == _SNAPSHOT
    assert resp.signals_used == _USED
    assert resp.meeting_time == "Wednesday Oct 7, 11:00 AM EDT"
    assert resp.generation_failed is False


def test_failure_path_keeps_the_snapshot():
    resp, _ = _analyze(
        _base_ctx(account_snapshot=_SNAPSHOT, signals_used=_USED), fail=True
    )

    assert resp.generation_failed is True
    assert resp.account_snapshot == _SNAPSHOT
    assert resp.signals_used == _USED
    assert resp.meeting_time == "Wednesday Oct 7, 11:00 AM EDT"


# ── Deep Prep sees the same blocks ───────────────────────────────────────────


def test_deep_prep_context_gets_the_blocks_and_one_crm_picture():
    from rubik_core.deep_prep.council import _context_block

    ctx = _base_ctx(
        signal_blocks=dict(_BLOCKS),
        crm_relationships="CLIENT RELATIONSHIP (HubSpot CRM, live ...)",
    )
    block = _context_block(ctx)

    for b in _BLOCKS.values():
        assert b in block
    assert "HOW TO READ THE SOURCES ABOVE:" in block
    # The ambiguity-safe record replaces the older first-match block.
    assert "CLIENT RELATIONSHIP (HubSpot CRM" not in block

    legacy_only = _context_block(
        _base_ctx(crm_relationships="CLIENT RELATIONSHIP (HubSpot CRM, live ...)")
    )
    assert "CLIENT RELATIONSHIP (HubSpot CRM" in legacy_only
    assert "HOW TO READ THE SOURCES ABOVE" not in legacy_only
