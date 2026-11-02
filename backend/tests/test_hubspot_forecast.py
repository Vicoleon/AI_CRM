"""Regression tests for the HubSpot deal-forecast ("expected to close") path.

The bug these guard: asked "what deals are expected to close this month for Parker
Jensen?", Cubie answered "none this month" while HubSpot's dashboard showed 6 —
a massive undercount. Root causes, all locked down here:

1. Open deals were windowed by ``createdate`` (when the deal was created) instead
   of ``closedate`` (its PROJECTED close date). A forecast question must filter on
   the expected close date.
2. "this month" resolved to month-TO-DATE, so deals projected to close later in
   the month were silently dropped. Forward-looking intents must use the FULL
   calendar period.
3. There was no forward window (next_month/next_quarter/next_week) and no intent
   that returns the LIST of open deals expected to close in a period.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from rubik_core.retrieval import hubspot_api
from rubik_core.retrieval.hubspot_api import (
    HubSpotAPIClient,
    HubSpotAPIRetriever,
    _days_since,
    _kpi_expected_to_close,
    _parse_hs_datetime,
    resolve_window,
)
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

# A fixed "today" so windows are deterministic: Wed 2026-07-15 (Q3).
_TODAY = date(2026, 7, 15)


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr(hubspot_api, "_today", lambda: _TODAY)


@pytest.fixture(autouse=True)
def _workspace_pipelines(monkeypatch):
    monkeypatch.setattr(
        hubspot_api,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )


@pytest.fixture(autouse=True)
def _no_client_company_links(monkeypatch):
    """Synthetic search rows have workspace-owned deal associations.

    Forecast behavior is under test here; live CRM ownership proof has its own
    suite and must not make a network request for these canned records.
    """
    monkeypatch.setattr(
        HubSpotAPIClient,
        "fully_owned_deal_associations",
        lambda self, object_type, ids: {
            str(record_id): ["workspace-deal"] for record_id in ids
        },
    )
    monkeypatch.setattr(
        HubSpotAPIClient,
        "batch_associations",
        lambda self, f, t, ids, prefer_type_id=None: {
            str(i): (
                ["workspace-deal"]
                if f in ("contacts", "companies") and t == "deals"
                else []
            )
            for i in ids
        },
    )
    monkeypatch.setattr(hubspot_api, "get_genai_client", lambda: MagicMock())


def _ms(y, m, d):
    return hubspot_api._ms(date(y, m, d))


def _prop_filters(body):
    """Flatten the propertyName→(operator,value) filters in a search body."""
    out = []
    for grp in body.get("filterGroups", []):
        for f in grp.get("filters", []):
            out.append((f["propertyName"], f["operator"], f.get("value")))
    return out


# ── resolve_window: forward-looking periods ──────────────────────────────────


def test_next_month_is_the_full_following_calendar_month():
    s, e, label = resolve_window("next_month", None, None)
    assert s == _ms(2026, 8, 1)
    assert e == _ms(2026, 9, 1)  # exclusive
    assert "2026-08-01" in label and "2026-08-31" in label


def test_next_quarter_is_the_full_following_quarter():
    # From Q3 (Jul–Sep) 2026, next quarter is Q4: Oct 1 → Dec 31.
    s, e, _ = resolve_window("next_quarter", None, None)
    assert s == _ms(2026, 10, 1)
    assert e == _ms(2027, 1, 1)


def test_next_week_is_the_following_monday_week():
    # Week of 2026-07-15 (Wed) is Mon 07-13; next week starts Mon 07-20.
    s, e, _ = resolve_window("next_week", None, None)
    assert s == _ms(2026, 7, 20)
    assert e == _ms(2026, 7, 27)


def test_this_month_full_period_covers_the_whole_month():
    """Forward-looking: the window must reach month end, not stop at today."""
    s, e, _ = resolve_window("this_month", None, None, full_period=True)
    assert s == _ms(2026, 7, 1)
    assert e == _ms(2026, 8, 1)  # NOT today+1 (2026-07-16)


def test_this_month_default_is_month_to_date():
    """Backward-looking metrics (deals won) still stop at today+1."""
    s, e, _ = resolve_window("this_month", None, None)
    assert s == _ms(2026, 7, 1)
    assert e == _ms(2026, 7, 16)  # today+1


def test_this_quarter_full_period_reaches_quarter_end():
    s, e, _ = resolve_window("this_quarter", None, None, full_period=True)
    assert s == _ms(2026, 7, 1)
    assert e == _ms(2026, 10, 1)


# ── _kpi_expected_to_close: filters OPEN deals by closedate ───────────────────


def _canned_search(captured):
    def fake_search(self, object_type, body):
        captured.append((object_type, body))
        return {
            "results": [
                {
                    "id": "1",
                    "properties": {
                        "pipeline": "default",
                        "dealname": "Globex - '26 Renewal",
                        "amount": "3950",
                        "closedate": "2026-07-31T00:00:00Z",
                        "hubspot_owner_id": "77",
                        "hs_deal_stage_probability": "0.5",
                    },
                },
                {
                    "id": "2",
                    "properties": {
                        "pipeline": "default",
                        "dealname": "Initech - Onboarding",
                        "amount": "6500",
                        "closedate": "2026-07-20T00:00:00Z",
                        "hubspot_owner_id": "77",
                        "hs_deal_stage_probability": "0.5",
                    },
                },
            ]
        }

    return fake_search


def test_expected_to_close_filters_open_deals_by_closedate(monkeypatch):
    captured = []
    monkeypatch.setattr(HubSpotAPIClient, "_search", _canned_search(captured))
    monkeypatch.setattr(
        HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Parker Jensen"}
    )

    client = HubSpotAPIClient(token="tok")
    window = resolve_window("this_month", None, None, full_period=True)
    block = _kpi_expected_to_close(client, window, "77", " (owner: Parker Jensen)")

    _, body = captured[0]
    filters = _prop_filters(body)
    props = {name for name, _, _ in filters}

    # The forecast filters on closedate — the projected close date — NOT createdate.
    assert ("hs_is_closed", "EQ", "false") in filters
    assert ("closedate", "GTE", str(_ms(2026, 7, 1))) in filters
    assert ("closedate", "LT", str(_ms(2026, 8, 1))) in filters
    assert "createdate" not in props, "forecast must not window by createdate"
    assert ("hubspot_owner_id", "EQ", "77") in filters

    # And the rendered block reports the count + totals, not a bare 'none'.
    assert "**2** open deal(s)" in block
    assert "$10,450" in block  # 3950 + 6500
    assert "expected close 2026-07-20" in block


def test_expected_to_close_needs_a_timeframe(monkeypatch):
    monkeypatch.setattr(HubSpotAPIClient, "owner_name_map", lambda self: {})
    client = HubSpotAPIClient(token="tok")
    block = _kpi_expected_to_close(client, (None, None, ""), None, "")
    assert "Specify a timeframe" in block


# ── _deal_list: open deals window by closedate (the exact reported bug) ───────


def test_deal_list_open_windows_by_closedate_not_createdate(monkeypatch):
    captured = []
    monkeypatch.setattr(HubSpotAPIClient, "_search", _canned_search(captured))
    monkeypatch.setattr(
        HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Parker Jensen"}
    )

    client = HubSpotAPIClient(token="tok")
    retriever = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)  # skip genai __init__
    spec = {"deal_status": "open"}
    window = resolve_window("this_month", None, None, full_period=True)
    retriever._deal_list(client, spec, window, "77", " (owner: Parker Jensen)")

    filters = _prop_filters(captured[0][1])
    props = {name for name, _, _ in filters}
    assert ("closedate", "GTE", str(_ms(2026, 7, 1))) in filters
    assert "createdate" not in props


# ── retrieve(): full forecast path with a mocked planner ─────────────────────


def _mock_plan(spec):
    def _plan(self, query):
        return spec

    return _plan


def test_retrieve_expected_to_close_this_month_uses_full_month(monkeypatch):
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    captured = []
    monkeypatch.setattr(HubSpotAPIClient, "_search", _canned_search(captured))
    monkeypatch.setattr(
        HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Parker Jensen"}
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_plan",
        _mock_plan(
            {
                "intent": "expected_to_close",
                "relative_timeframe": "this_month",
                "owner_name": "Parker Jensen",
            }
        ),
    )

    block, _debug = HubSpotAPIRetriever().retrieve(
        "what deals are expected to close this month for Parker Jensen?"
    )

    # The deals search must span the FULL month [Jul 1, Aug 1) on closedate.
    deal_body = next(b for obj, b in captured if obj == "deals")
    filters = _prop_filters(deal_body)
    assert ("closedate", "GTE", str(_ms(2026, 7, 1))) in filters
    assert ("closedate", "LT", str(_ms(2026, 8, 1))) in filters
    assert "expected to close" in block.lower()
    assert "**2** open deal(s)" in block


def test_retrieve_expected_to_close_next_month(monkeypatch):
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    captured = []
    monkeypatch.setattr(HubSpotAPIClient, "_search", _canned_search(captured))
    monkeypatch.setattr(
        HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Parker Jensen"}
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_plan",
        _mock_plan(
            {
                "intent": "expected_to_close",
                "relative_timeframe": "next_month",
                "owner_name": "Parker Jensen",
            }
        ),
    )

    block, _debug = HubSpotAPIRetriever().retrieve(
        "what deals are expected to close next month for Parker Jensen?"
    )

    deal_body = next(b for obj, b in captured if obj == "deals")
    filters = _prop_filters(deal_body)
    assert ("closedate", "GTE", str(_ms(2026, 8, 1))) in filters
    assert ("closedate", "LT", str(_ms(2026, 9, 1))) in filters
    assert block  # non-empty forecast block


# ── outreach recency: deals_needing_followup ─────────────────────────────────


def test_parse_hs_datetime_handles_iso_and_epoch_ms():
    iso = _parse_hs_datetime("2026-05-27T12:00:00Z")
    assert iso is not None and iso.year == 2026 and iso.month == 5
    epoch = _parse_hs_datetime(str(_ms(2026, 5, 27)))
    assert epoch is not None and epoch.year == 2026 and epoch.month == 5
    assert _parse_hs_datetime("") is None
    assert _parse_hs_datetime(None) is None


def test_days_since_counts_whole_days():
    twenty = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    assert _days_since(twenty) in (19, 20, 21)
    assert _days_since(None) is None


def _iso_days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _followup_search(captured):
    """Open deals with varied last-contact recency (relative to real now)."""

    def fake_search(self, object_type, body):
        captured.append((object_type, body))
        return {
            "results": [
                {  # fresh — contacted 3 days ago → NOT flagged at 10d
                    "id": "1",
                    "properties": {
                        "pipeline": "default",
                        "dealname": "Fresh Deal",
                        "amount": "1000",
                        "closedate": "2026-07-31T00:00:00Z",
                        "hubspot_owner_id": "77",
                        "notes_last_contacted": _iso_days_ago(3),
                        "notes_last_updated": _iso_days_ago(3),
                    },
                },
                {  # stale — contacted 20 days ago → FLAGGED at 10d
                    "id": "2",
                    "properties": {
                        "pipeline": "default",
                        "dealname": "Stale Deal",
                        "amount": "2000",
                        "closedate": "2026-07-31T00:00:00Z",
                        "hubspot_owner_id": "77",
                        "notes_last_contacted": _iso_days_ago(20),
                        # note edited recently, but no CONTACT — must not rescue it
                        "notes_last_updated": _iso_days_ago(1),
                    },
                },
                {  # never contacted → FLAGGED, sorts first
                    "id": "3",
                    "properties": {
                        "pipeline": "default",
                        "dealname": "Never Contacted Deal",
                        "amount": "3000",
                        "closedate": "2026-07-31T00:00:00Z",
                        "hubspot_owner_id": "77",
                    },
                },
            ]
        }

    return fake_search


def test_deals_needing_followup_flags_by_last_contact(monkeypatch):
    captured = []
    monkeypatch.setattr(HubSpotAPIClient, "_search", _followup_search(captured))
    monkeypatch.setattr(
        HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Parker Jensen"}
    )

    client = HubSpotAPIClient(token="tok")
    retriever = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    spec = {"stale_days": 10}
    window = resolve_window("this_month", None, None, full_period=True)
    block = retriever._deals_needing_followup(
        client, spec, window, "77", " (owner: Parker Jensen)"
    )

    # Scopes open deals by closedate over the full month.
    filters = _prop_filters(captured[0][1])
    props = {name for name, _, _ in filters}
    assert ("hs_is_closed", "EQ", "false") in filters
    assert ("closedate", "GTE", str(_ms(2026, 7, 1))) in filters
    assert "createdate" not in props
    # notes_last_contacted must be among requested properties.
    assert "notes_last_contacted" in captured[0][1]["properties"]

    # 2 of 3 flagged; fresh one excluded; never-contacted listed first.
    assert "2 of 3 open deal(s) need follow-up" in block
    assert "Stale Deal" in block and "Never Contacted Deal" in block
    assert "Fresh Deal" not in block
    assert block.index("Never Contacted Deal") < block.index("Stale Deal")
    assert "no logged contact on record" in block


def test_cold_scan_shows_the_client_s_newer_contact_beside_the_deal(monkeypatch):
    """The deal's own record can say 82 days while the client was emailed 13
    days earlier, logged on the company instead."""

    def deal(rid, name, days):
        return {
            "id": rid,
            "properties": {
                "pipeline": "default",
                "dealname": name,
                "amount": "12500",
                "closedate": "2026-07-31T00:00:00Z",
                "hubspot_owner_id": "77",
                "notes_last_contacted": _iso_days_ago(days),
            },
        }

    monkeypatch.setattr(
        HubSpotAPIClient,
        "_search",
        lambda self, obj, body: {
            "results": [
                deal("1", "Contoso Ltd - Annual Review", 82),
                deal("2", "Quiet Client Rollout", 40),
                deal("3", "Busy Client Rollout", 60),
            ]
        },
    )
    monkeypatch.setattr(HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Sue"})
    monkeypatch.setattr(
        HubSpotAPIClient,
        "batch_associations",
        lambda self, f, t, ids, prefer_type_id=None: {
            "1": ["c1"],
            "2": [],
            "3": ["c3"],
        },
    )
    # Stamped now, before the scan reads its clock, so 13 days stays 13.
    companies = [
        {
            "id": "c1",
            "properties": {
                "name": "Contoso Ltd",
                "notes_last_contacted": _iso_days_ago(13),
            },
        },
        {
            "id": "c3",
            "properties": {"name": "Busy Co", "notes_last_contacted": _iso_days_ago(2)},
        },
    ]
    monkeypatch.setattr(
        HubSpotAPIClient,
        "batch_read",
        lambda self, obj, ids, props, properties_with_history=None: companies,
    )
    retriever = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    window = resolve_window("this_month", None, None, full_period=True)

    block = retriever._deals_needing_followup(
        HubSpotAPIClient(token="tok"), {"stale_days": 10}, window, "77", ""
    )

    contoso = next(ln for ln in block.splitlines() if "Contoso Ltd - Annual" in ln)
    assert "last contact on this deal" in contoso and "(82d ago)" in contoso
    assert "Contoso Ltd last contacted" in contoso and "(13d ago)" in contoso
    assert "3 of 3 open deal(s) need follow-up" in block
    # The whole client quiet longest first; the deal whose client heard from us
    # two days ago last, whatever its own date says.
    assert (
        block.index("Quiet Client Rollout")
        < block.index("Contoso Ltd - Annual")
        < block.index("Busy Client Rollout")
    )


def test_deals_needing_followup_all_recent(monkeypatch):
    def all_fresh(self, object_type, body):
        return {
            "results": [
                {
                    "id": "1",
                    "properties": {
                        "pipeline": "default",
                        "dealname": "A",
                        "closedate": "2026-07-31T00:00:00Z",
                        "hubspot_owner_id": "77",
                        "notes_last_contacted": _iso_days_ago(2),
                    },
                }
            ]
        }

    monkeypatch.setattr(HubSpotAPIClient, "_search", all_fresh)
    monkeypatch.setattr(
        HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Parker"}
    )
    client = HubSpotAPIClient(token="tok")
    retriever = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    window = resolve_window("this_month", None, None, full_period=True)
    block = retriever._deals_needing_followup(
        client, {"stale_days": 10}, window, "77", ""
    )
    assert "None — every one of the 1 open deal(s)" in block


def test_retrieve_routes_deals_needing_followup(monkeypatch):
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    captured = []
    monkeypatch.setattr(HubSpotAPIClient, "_search", _followup_search(captured))
    monkeypatch.setattr(
        HubSpotAPIClient, "owner_name_map", lambda self: {"77": "Parker Jensen"}
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_plan",
        _mock_plan(
            {
                "intent": "deals_needing_followup",
                "relative_timeframe": "this_month",
                "owner_name": "Parker Jensen",
                "stale_days": 10,
            }
        ),
    )

    block, _debug = HubSpotAPIRetriever().retrieve(
        "of the deals closing this month, which has Parker not contacted in 10 days?"
    )
    # Full-month closedate window + follow-up framing.
    deal_body = next(b for obj, b in captured if obj == "deals")
    filters = _prop_filters(deal_body)
    assert ("closedate", "GTE", str(_ms(2026, 7, 1))) in filters
    assert ("closedate", "LT", str(_ms(2026, 8, 1))) in filters
    assert "need follow-up" in block
    assert "last 10 days" in block


# ── General outreach/last-touch resolver (company | deal | contact | rep) ────
#
# The generalization: ONE `outreach_recency` intent + a target-aware resolver
# replaces the per-phrasing intents (deals_needing_followup, last_touch),
# and every query emits a structured `[crm-query]` log so gaps surface. These guard
# the resolver's honesty (multi-match, not-found, no-owner, epoch junk), the
# MAX-not-prefer company recency, "me" self-scoping, and the feedback-loop.

import logging  # noqa: E402

from rubik_core.retrieval import hubspot_api as _hs  # noqa: E402


def _bare_retriever():
    """A retriever without the genai client (unit-testing individual methods)."""
    r = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    r.model_id = "test-flash"
    return r


def _company(cid, name, **props):
    return {"id": cid, "properties": {"name": name, **props}}


def test_resolve_target_exact_name_autopicks_among_many(monkeypatch):
    """Three companies share the 'Northwind' name; the exact name must auto-pick —
    NOT [0]."""
    monkeypatch.setattr(
        HubSpotAPIClient,
        "search_objects",
        lambda self, obj, q, props, limit=5: [
            _company("1", "Northwind Traders, Ltd."),
            _company("2", "Northwind Health, Inc."),
            _company("3", "Umbrella Group, LLC dba Northwind Alliance"),
        ],
    )
    r = _bare_retriever()
    t = r._resolve_target(
        HubSpotAPIClient(token="tok"),
        {"target_type": "company", "target_name": "Northwind Health, Inc."},
        None,
        "",
        False,
        set(),
    )
    assert t.status == "ok"
    assert t.match_basis == "exact"
    assert t.obj_id == "2"  # the exact match, not the first result


def test_resolve_target_multi_match_asks(monkeypatch):
    monkeypatch.setattr(
        HubSpotAPIClient,
        "search_objects",
        lambda self, obj, q, props, limit=5: [
            _company("1", "Northwind Traders"),
            _company("3", "Umbrella dba Northwind Alliance"),
        ],
    )
    r = _bare_retriever()
    c = HubSpotAPIClient(token="tok")
    spec = {"target_type": "company", "target_name": "Northwind"}
    t = r._resolve_target(c, spec, None, "", False, set())
    assert t.status == "multi_match" and len(t.candidates) == 2
    outcome = {}
    block = r._outreach_recency(
        c, spec, (None, None, ""), None, "", False, "", set(), outcome
    )
    assert "which do you mean" in block.lower()
    assert outcome["resolution"] == "multi_match" and outcome["gap"] is True


def test_resolve_deal_drops_disallowed_pipeline(monkeypatch):
    """An Initech (client-pipeline) deal must be dropped before the match rule."""
    monkeypatch.setattr(
        HubSpotAPIClient,
        "search_objects",
        lambda self, obj, q, props, limit=5: [
            {
                "id": "1",
                "properties": {"dealname": "Initech Deal", "pipeline": "9100004"},
            },
            {
                "id": "2",
                "properties": {
                    "dealname": "Globex Northwind Rollout",
                    "pipeline": "default",
                },
            },
        ],
    )
    r = _bare_retriever()
    t = r._resolve_target(
        HubSpotAPIClient(token="tok"),
        {"target_type": "deal", "target_name": "Northwind"},
        None,
        "",
        False,
        {"default", "9100002", "9100003"},
    )
    assert t.status == "ok" and t.obj_id == "2"  # only the Globex deal survives


def test_recency_point_company_takes_max_of_rollup_and_deal(monkeypatch):
    """Company rollup lags (2026-03-01) but a deal has a fresher contact (2026-05-27):
    report the MAX so the stale rollup can't mask the fresher deal touch."""
    monkeypatch.setattr(
        HubSpotAPIClient,
        "search_objects",
        lambda self, obj, q, props, limit=5: [
            _company(
                "9",
                "Northwind Health, Inc.",
                notes_last_contacted="2026-03-01T00:00:00Z",
            )
        ],
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_company_deal_last_contacted",
        lambda self, c, cid, allowed=None: "2026-05-27T00:00:00Z",
    )
    r = _bare_retriever()
    c = HubSpotAPIClient(token="tok")
    t = r._resolve_target(
        c,
        {"target_type": "company", "target_name": "Northwind Health, Inc."},
        None,
        "",
        False,
        set(),
    )
    outcome = {}
    block = r._recency_point(c, t, set(), outcome)
    assert "2026-05-27" in block and "2026-03-01" not in block
    assert "from an associated deal" in block
    assert outcome["resolution"] == "answered"


def test_recency_point_epoch_junk_is_no_data(monkeypatch):
    monkeypatch.setattr(
        HubSpotAPIClient,
        "search_objects",
        lambda self, obj, q, props, limit=5: [
            _company("9", "Umbrella Group", notes_last_contacted="1970-01-21T00:00:00Z")
        ],
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_company_deal_last_contacted",
        lambda self, c, cid, allowed=None: None,
    )
    r = _bare_retriever()
    c = HubSpotAPIClient(token="tok")
    t = r._resolve_target(
        c, {"target_type": "company", "target_name": "Umbrella"}, None, "", False, set()
    )
    outcome = {}
    block = r._recency_point(c, t, set(), outcome)
    assert "No logged contact" in block and "1970" not in block
    assert outcome["resolution"] == "no_data" and outcome["gap"] is False


def test_resolve_owner_self_matches_caller(monkeypatch):
    monkeypatch.setattr(
        HubSpotAPIClient,
        "owner_directory",
        lambda self: {"77": {"email": "pjensen@acme.example", "name": "Parker Jensen"}},
    )
    r = _bare_retriever()
    c = HubSpotAPIClient(token="tok")
    oid, who = r._resolve_owner_self(c, "PJensen@acme.example")  # case-insensitive
    assert oid == "77" and who == "Parker Jensen"
    oid2, why = r._resolve_owner_self(c, "nobody@acme.example")
    assert oid2 is None and "not a HubSpot" in why
    assert r._resolve_owner_self(c, None)[0] is None


def test_owner_scoped_company_set_is_unavailable():
    """'which of my clients have gone cold' — owner is unreliable on companies here,
    so it must return unavailable, never a false '0 of 0 cold'."""
    r = _bare_retriever()
    t = r._resolve_target(
        HubSpotAPIClient(token="tok"),
        {"target_type": "company"},
        "77",
        "me",
        False,
        set(),
    )
    assert t.status == "unavailable" and t.is_set


def test_cold_scan_honors_deal_status_won(monkeypatch):
    captured = {}

    def fake_search_all(self, obj, filt, props, sorts=None):
        captured["filt"] = filt
        return []

    monkeypatch.setattr(HubSpotAPIClient, "search_all", fake_search_all)
    r = _bare_retriever()
    r._deals_needing_followup(
        HubSpotAPIClient(token="tok"),
        {"deal_status": "won", "stale_days": 30},
        (None, None, ""),
        None,
        "",
    )
    ops = {(f["propertyName"], f.get("value")) for f in captured["filt"]}
    assert ("hs_is_closed_won", "true") in ops  # _WON, not the default _OPEN
    assert ("hs_is_closed", "false") not in ops


def test_cold_scan_partial_filter_caveat(monkeypatch):
    monkeypatch.setattr(
        HubSpotAPIClient, "search_all", lambda self, o, f, p, sorts=None: []
    )
    r = _bare_retriever()
    outcome = {}
    t = _hs.ResolvedTarget("deal", True, "ok", owner_id=None, set_object_type="deals")
    block = r._recency_cold_scan(
        HubSpotAPIClient(token="tok"),
        {"filters": [{"field": "product", "value": "QC"}]},
        (None, None, ""),
        t,
        "",
        set(),
        outcome,
    )
    assert "couldn't filter on product" in block
    assert outcome["resolution"] == "partial_filter" and outcome["gap"] is True


def test_answer_kind_status_reads_stage(monkeypatch):
    monkeypatch.setattr(
        HubSpotAPIClient, "deal_stage_labels", lambda self: {"s1": "Proposal Sent"}
    )
    r = _bare_retriever()
    t = _hs.ResolvedTarget(
        "deal",
        False,
        "ok",
        obj={
            "id": "5",
            "properties": {"dealname": "Globex Renewal", "dealstage": "s1"},
        },
        obj_id="5",
        display_name="Globex Renewal",
    )
    block = r._attribute_answer(HubSpotAPIClient(token="tok"), t, "status")
    assert "Proposal Sent" in block and "Globex Renewal" in block


def test_alias_last_touch_normalizes(monkeypatch):
    """The legacy last_touch intent + company_name still routes to company recency."""
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(HubSpotAPIClient, "owner_name_map", lambda self: {})
    monkeypatch.setattr(
        HubSpotAPIClient,
        "search_objects",
        lambda self, obj, q, props, limit=5: [
            _company(
                "9", "Northwind Health, Inc.", notes_last_contacted=_iso_days_ago(49)
            )
        ],
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_company_deal_last_contacted",
        lambda self, c, cid, allowed=None: None,
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_plan",
        _mock_plan({"intent": "last_touch", "company_name": "Northwind Health"}),
    )
    block, _ = HubSpotAPIRetriever().retrieve(
        "when was the last touch with Northwind Health?"
    )
    assert "Last logged contact with Northwind Health" in block


def test_gate_miss_is_logged(monkeypatch, caplog):
    """A non-CRM query returns '' but still emits a [crm-query] gate_miss gap line."""
    r = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    r.model_id = "test-flash"
    with caplog.at_level(logging.WARNING):
        block, _ = r.retrieve("hello there, how are you today")
    assert block == ""
    hits = [rec for rec in caplog.records if "[crm-query]" in rec.message]
    assert len(hits) == 1
    assert "gate_miss" in hits[0].message and hits[0].levelname == "WARNING"


def test_log_line_emitted_once_at_info_when_answered(monkeypatch, caplog):
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(HubSpotAPIClient, "owner_name_map", lambda self: {})
    monkeypatch.setattr(
        HubSpotAPIClient, "search_all", lambda self, o, f, p, sorts=None: []
    )
    monkeypatch.setattr(
        HubSpotAPIRetriever, "_plan", _mock_plan({"intent": "pipeline_value"})
    )
    r = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    r.model_id = "test-flash"
    with caplog.at_level(logging.INFO):
        r.retrieve("what's our open pipeline value?")
    hits = [rec for rec in caplog.records if "[crm-query]" in rec.message]
    assert len(hits) == 1 and hits[0].levelname == "INFO"
    assert '"gap": false' in hits[0].message
    assert "tok" not in hits[0].message  # token never logged


def test_caller_email_optional_no_owner_degrades(monkeypatch):
    """retrieve() with no caller_email + owner_scope=me degrades honestly, no crash."""
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(HubSpotAPIClient, "owner_directory", lambda self: {})
    monkeypatch.setattr(HubSpotAPIClient, "owner_name_map", lambda self: {})
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_plan",
        _mock_plan(
            {
                "intent": "outreach_recency",
                "target_type": "rep",
                "owner_scope": "me",
                "stale_days": 7,
            }
        ),
    )
    r = HubSpotAPIRetriever.__new__(HubSpotAPIRetriever)
    r.model_id = "test-flash"
    block, _ = r.retrieve("which of my deals are cold?")  # no caller_email passed
    assert "owner" in block.lower()  # no_owner_match honesty, not a traceback
