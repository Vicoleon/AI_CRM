"""The sales daily-rhythm checks (rubik_core/sales/rhythm_checks.py).

Driven against an in-memory HubSpot that evaluates the same search filters the
checks send, so each test states a portal and asserts what qualifies — and,
just as important, what does not and why.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rubik_core.config import settings
from rubik_core.retrieval import hubspot_api
from rubik_core.retrieval.hubspot_api import HubSpotAPIError, HubSpotAPIRetriever
from rubik_core.sales import rhythm_checks as rc
from rubik_core.sales import rhythm_signals
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

TODAY = date(2026, 10, 1)  # a Thursday
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)  # noon in Chicago


def _iso(y, m, d, h=17):
    return (
        datetime(y, m, d, h, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _as_number(value):
    """A property value as a comparable number (epoch ms for datetimes)."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    dt = hubspot_api._parse_hs_datetime(value)
    return dt.timestamp() * 1000 if dt else None


def _matches(props: dict, f: dict) -> bool:
    name, op = f["propertyName"], f["operator"]
    raw = props.get(name)
    if op == "HAS_PROPERTY":
        return raw not in (None, "")
    if op == "NOT_HAS_PROPERTY":
        return raw in (None, "")
    if op == "EQ":
        return str(raw) == str(f["value"])
    if op == "NEQ":
        return str(raw) != str(f["value"])
    if op == "IN":
        return str(raw) in [str(v) for v in f["values"]]
    left, right = _as_number(raw), _as_number(f["value"])
    if left is None or right is None:
        return False
    return {
        "GTE": left >= right,
        "GT": left > right,
        "LT": left < right,
        "LTE": left <= right,
    }[op]


class FakeHubSpot:
    """Just enough of HubSpotAPIClient for the checks, with a call log."""

    MAX_RECORDS = 5000

    def __init__(self):
        self.token = "fake-rhythm-token"
        self.objects: dict[str, dict[str, dict]] = {
            "deals": {},
            "contacts": {},
            "companies": {},
            "tasks": {},
            "calls": {},
            "emails": {},
            "meetings": {},
            "notes": {},
        }
        self.assoc: dict[tuple[str, str], dict[str, list[str]]] = {}
        #: (from_type, to_type, src, dst) → HubSpot association type ids.
        self.assoc_types: dict[tuple[str, str, str, str], set[int]] = {}
        self.history: dict[str, dict[str, list[dict]]] = {}
        self.calls: list[tuple] = []
        self.fail_history = False
        self.fail_company_read = False
        # Older rhythm fixtures predate the shared-portal ownership model and
        # leave otherwise local records unlinked. New isolation cases turn
        # this off and declare every association explicitly.
        self.fixture_unlinked_owned = True
        self.owners = {
            "77": {
                "email": "ae@acme.example",
                "name": "Avery Exec",
                "user_id": "9001",
            },
            "88": {
                "email": "rep2@acme.example",
                "name": "Robin Two",
                "user_id": "9002",
            },
        }
        self.stages = {
            "90000001": "Quote/Proposal",
            "presentationscheduled": "SOW Sent",
            "qualifiedtobuy": "Legal Review",
            "90000002": "Drafting SOW",
            "closedwon": "8- Closed - Won",
            "appointmentscheduled": "Discovery",
        }

    # ── fixtures ──
    def add(self, obj, rid, **props):
        props.setdefault("hs_object_id", rid)
        if obj == "deals":
            props.setdefault("pipeline", "default")
            props.setdefault("hs_is_closed", "false")
            props.setdefault("hs_is_closed_won", "false")
        self.objects[obj][rid] = props

    def link(self, from_type, to_type, src, *dst, types=()):
        self.assoc.setdefault((from_type, to_type), {}).setdefault(src, []).extend(dst)
        for d in dst:
            self.assoc_types.setdefault((from_type, to_type, src, d), set()).update(
                types
            )

    # ── client surface ──
    def _filter(self, obj, groups):
        out = []
        for rid, props in self.objects[obj].items():
            if any(all(_matches(props, f) for f in g) for g in groups):
                out.append({"id": rid, "properties": dict(props)})
        return out

    def search_all(self, obj, filters, properties, sorts=None):
        self.calls.append(("search", obj, filters))
        return self._filter(obj, [filters])

    def search_all_groups(self, obj, groups, properties, sorts=None):
        self.calls.append(("search_groups", obj, groups))
        return self._filter(obj, groups)

    def fully_owned_deal_associations(self, obj, ids):
        allowed = set(rc.get_hubspot_config().pipeline_ids)
        if not allowed:
            raise HubSpotAPIError("Workspace deal pipelines are not configured")
        result = {}
        for rid in ids:
            rid = str(rid)
            linked = list(self.assoc.get((obj, "deals"), {}).get(rid, []))
            linked += [
                deal_id
                for deal_id, obj_ids in self.assoc.get(("deals", obj), {}).items()
                if rid in obj_ids
            ]
            if not linked and self.fixture_unlinked_owned:
                linked = ["__fixture_owned__"]
            if not linked:
                continue
            if any(
                self.objects["deals"].get(did, {}).get("pipeline") not in allowed
                for did in linked
                if did != "__fixture_owned__"
            ):
                continue
            if obj in {"tasks", "calls", "emails", "meetings", "notes"}:
                related = {
                    kind: self.assoc.get((obj, kind), {}).get(rid, [])
                    for kind in ("contacts", "companies")
                }
                if any(
                    len(self.fully_owned_deal_associations(kind, related[kind]))
                    != len(related[kind])
                    for kind in related
                ):
                    continue
            result[rid] = linked
        return result

    def search_all_owned(self, obj, filters, properties):
        rows = self.search_all(obj, filters, properties)
        if len(rows) >= self.MAX_RECORDS:
            raise HubSpotAPIError(
                "HubSpot ownership sweep exceeds the safe result limit"
            )
        owned = self.fully_owned_deal_associations(
            obj, [str(row["id"]) for row in rows]
        )
        return [row for row in rows if str(row["id"]) in owned]

    def count(self, obj, filters):
        self.calls.append(("count", obj, filters))
        return len(self._filter(obj, [filters]))

    def batch_read(self, obj, ids, properties, *, properties_with_history=None):
        self.calls.append(
            ("batch_read", obj, tuple(ids), bool(properties_with_history))
        )
        if properties_with_history and self.fail_history:
            raise HubSpotAPIError("HubSpot deals batch read failed: HTTP 400: limit")
        if obj == "companies" and self.fail_company_read:
            raise HubSpotAPIError("HubSpot companies batch read failed: HTTP 403")
        rows = []
        for rid in ids:
            props = self.objects[obj].get(str(rid))
            if props is None:
                continue
            row = {"id": str(rid), "properties": dict(props)}
            if properties_with_history:
                row["propertiesWithHistory"] = {
                    p: self.history.get(str(rid), {}).get(p, [])
                    for p in properties_with_history
                }
            rows.append(row)
        return rows

    def batch_associations(self, from_type, to_type, ids, *, prefer_type_id=None):
        self.calls.append(("assoc", from_type, to_type, tuple(ids)))
        table = self.assoc.get((from_type, to_type), {})
        out = {}
        for i in ids:
            linked = list(table.get(str(i), []))
            if prefer_type_id is not None:
                typed = [
                    d
                    for d in linked
                    if prefer_type_id
                    in self.assoc_types.get((from_type, to_type, str(i), d), set())
                ]
                linked = typed or linked
            out[str(i)] = linked
        return out

    def owner_directory(self):
        return self.owners

    def owner_name_map(self):
        return {oid: info["name"] for oid, info in self.owners.items()}

    def deal_stage_labels(self):
        return self.stages


def _no_signals(mailbox, deal_terms, **_kw):
    """No email or meetings anywhere — the HubSpot-only answer."""
    return rhythm_signals.SignalReport(
        touches={d: rhythm_signals.Touch() for d in deal_terms}
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    rc.clear_cache()
    rhythm_signals.clear_cache()
    monkeypatch.setattr(rc, "_today", lambda: TODAY)
    monkeypatch.setattr(hubspot_api, "get_genai_client", lambda: MagicMock())
    monkeypatch.setattr(rc, "SIGNALS", _no_signals)
    monkeypatch.setattr(
        rc, "DAY_ACTIVITY", lambda *_a, **_k: rhythm_signals.DayActivity()
    )

    def fixture_config():
        return HubSpotConnectorConfig(pipeline_ids=("default",), portal_id="9100001")

    monkeypatch.setattr(rc, "get_hubspot_config", fixture_config)
    monkeypatch.setattr(hubspot_api, "get_hubspot_config", fixture_config)
    yield
    rc.clear_cache()


@pytest.fixture
def hs():
    return FakeHubSpot()


def _me(hs):
    scope = rc.resolve_scope(hs, "AE@acme.example")
    assert scope is not None
    return scope


def _team(hs):
    return rc.resolve_scope(hs, None, team=True)


def _searched_filters(hs, obj):
    return [c[2] for c in hs.calls if c[0] == "search" and c[1] == obj]


# ── scope ────────────────────────────────────────────────────────────────────


def test_scope_matches_the_caller_by_email_and_carries_the_user_id(hs):
    scope = _me(hs)
    assert scope.owner_ids == ("77",)
    assert scope.user_ids == ("9001",)
    assert not scope.team


def test_a_caller_who_is_not_a_hubspot_owner_gets_no_scope_not_the_team(hs):
    assert rc.resolve_scope(hs, "ebrooks@acme.example") is None


def test_the_team_scope_reads_every_owner(hs):
    scope = _team(hs)
    assert scope.team and scope.owner_ids is None and scope.user_ids is None


# ── close-date pushes ────────────────────────────────────────────────────────


def test_pushes_count_only_moves_later_by_calendar_day():
    history = [
        # deliberately out of order: HubSpot's ordering is not relied on
        {"value": _iso(2026, 4, 30), "timestamp": _iso(2026, 3, 20)},
        {"value": _iso(2026, 1, 31), "timestamp": _iso(2026, 1, 2)},
        {"value": _iso(2026, 2, 28), "timestamp": _iso(2026, 1, 20)},
        {"value": _iso(2026, 2, 15), "timestamp": _iso(2026, 2, 1)},  # pulled in
        {"value": _iso(2026, 2, 15, 20), "timestamp": _iso(2026, 2, 2)},  # same day
        {"value": _iso(2026, 3, 31), "timestamp": _iso(2026, 2, 10)},
    ]
    # Jan31→Feb28 (+1), →Feb15 (pulled in), →Feb15 (same day), →Mar31 (+1), →Apr30 (+1)
    assert rc.close_date_pushes(history) == 3


def test_no_history_is_zero_pushes():
    assert rc.close_date_pushes(None) == 0
    assert (
        rc.close_date_pushes(
            [{"value": _iso(2026, 1, 1), "timestamp": _iso(2026, 1, 1)}]
        )
        == 0
    )


# ── loss classification ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reason", "note", "expected"),
    [
        ("Pricing", None, "budget"),
        (None, "Over budget", "budget"),
        ("Competitor", "Decided to stay with current vendor due to price", "budget"),
        ("No longer Needed", "They will pass this year", "timing"),
        ("Not Interested", "Not interested at this time", "timing"),
        (None, "revisit in September", "timing"),
        ("Acquired/Merger", "revisit next year", None),
        (None, "Acquired by Globex", None),
        (None, "Winding down business, maybe later", None),
        ("Unresponsive", "no response", None),
        ("Competitor", "Went with Initech", None),
    ],
)
def test_classify_loss(reason, note, expected):
    assert rc.classify_loss(reason, note) == expected


# ── single-threaded ──────────────────────────────────────────────────────────


def test_single_threaded_filters_server_side_and_names_the_lone_contact(hs):
    hs.add(
        "deals",
        "1",
        dealname="Acme - Onboarding",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )
    hs.add(
        "deals",
        "2",
        dealname="Beta - Rollout",
        amount="31000",
        hubspot_owner_id="77",
        num_associated_contacts="0",
    )
    hs.add(
        "deals",
        "3",
        dealname="Gamma - Big",
        amount="90000",
        hubspot_owner_id="77",
        num_associated_contacts="3",
    )  # well threaded
    hs.add(
        "deals",
        "4",
        dealname="Small",
        amount="5000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )  # under the threshold
    hs.add(
        "deals",
        "5",
        dealname="Someone else's",
        amount="50000",
        hubspot_owner_id="88",
        num_associated_contacts="1",
    )
    hs.add("contacts", "c1", firstname="Jane", lastname="Doe")
    hs.link("deals", "contacts", "1", "c1")

    result = rc.single_threaded(hs, _me(hs))

    assert [r.title for r in result.rows] == [
        "Acme - Onboarding",
        "Beta - Rollout",
    ]  # biggest first
    assert result.rows[0].reasons == ["only contact: Jane Doe"]
    assert result.rows[1].reasons == ["no contacts on the deal"]
    # Denominator: my open deals at or over $30k (Acme, Beta, Gamma).
    assert result.scanned == 3
    filters = _searched_filters(hs, "deals")[0]
    assert {
        "propertyName": "num_associated_contacts",
        "operator": "LTE",
        "value": "1",
    } in filters
    assert {
        "propertyName": "pipeline",
        "operator": "IN",
        "values": ["default"],
    } in filters


def test_the_client_pipeline_never_reaches_a_rep(hs):
    hs.add(
        "deals",
        "1",
        dealname="Initech thing",
        amount="99000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
        pipeline="initech",
    )
    assert rc.single_threaded(hs, _me(hs)).rows == []


# ── waiting on the decision-maker ────────────────────────────────────────────


def test_waiting_on_decision_maker(hs):
    # Qualifies: our last touch Thu Sep 24 (5 business days), DM's reply is older.
    hs.add(
        "deals",
        "1",
        dealname="Acme - Onboarding",
        amount="40000",
        hubspot_owner_id="77",
        dealstage="presentationscheduled",
        notes_last_contacted=_iso(2026, 9, 24),
    )
    hs.add(
        "contacts",
        "c1",
        firstname="Dakota",
        lastname="Decider",
        email="dakota@acmecorp.example",
        hs_buying_role="CHAMPION;DECISION_MAKER",
        hs_sales_email_last_replied=_iso(2026, 9, 10),
    )
    hs.add(
        "contacts",
        "c2",
        firstname="Pat",
        lastname="User",
        email="pat@acmecorp.example",
        hs_sales_email_last_replied=_iso(2026, 9, 29),
    )  # not the DM — does not count
    hs.link("deals", "contacts", "1", "c1", "c2")
    # Excluded: the DM replied after our last touch.
    hs.add(
        "deals",
        "2",
        dealname="Beta",
        hubspot_owner_id="77",
        dealstage="qualifiedtobuy",
        notes_last_contacted=_iso(2026, 9, 21),
    )
    hs.add(
        "contacts",
        "c3",
        firstname="Bo",
        email="bo@beta.com",
        hs_buying_role="DECISION_MAKER",
        hs_sales_email_last_replied=_iso(2026, 9, 25),
    )
    hs.link("deals", "contacts", "2", "c3")
    # Excluded: touched yesterday.
    hs.add(
        "deals",
        "3",
        dealname="Gamma",
        hubspot_owner_id="77",
        dealstage="90000001",
        notes_last_contacted=_iso(2026, 9, 30),
    )
    # Qualifies with a gap: nobody marked Decision Maker.
    hs.add(
        "deals",
        "4",
        dealname="Delta",
        hubspot_owner_id="77",
        dealstage="90000001",
        notes_last_contacted=_iso(2026, 9, 18),
    )
    hs.add("contacts", "c4", firstname="Lee", email="lee@delta.com")
    hs.link("deals", "contacts", "4", "c4")
    # Not a late stage: we owe THEM the SOW.
    hs.add(
        "deals",
        "5",
        dealname="Drafting",
        hubspot_owner_id="77",
        dealstage="90000002",
        notes_last_contacted=_iso(2026, 9, 1),
    )

    result = rc.waiting_on_decision_maker(hs, _me(hs), now=NOW)

    assert [r.title for r in result.rows] == [
        "Delta",
        "Acme - Onboarding",
    ]  # longest wait first
    acme = result.rows[1]
    assert "decision-maker: Dakota Decider" in acme.reasons
    assert "5 business days" in acme.reasons[0]
    assert acme.extras["contact_email"] == "dakota@acmecorp.example"
    assert (
        "no contact on this deal is marked Decision Maker in HubSpot"
        in result.rows[0].reasons
    )
    assert result.scanned == 4  # late-stage open deals, Drafting SOW excluded
    stage_filter = next(
        f for f in _searched_filters(hs, "deals")[0] if f["propertyName"] == "dealstage"
    )
    assert "90000002" not in stage_filter["values"]
    assert "closedwon" not in stage_filter["values"]


def test_waiting_says_unavailable_when_stages_cannot_be_read(hs):
    hs.stages = {}
    result = rc.waiting_on_decision_maker(hs, _me(hs), now=NOW)
    assert not result.available and result.reason == rc.CRM_READ_FAILED


def test_the_late_stages_a_company_names_replace_the_label_keywords(monkeypatch):
    # Stage names are company-specific, so workspace config starts empty and the
    # generic label keywords stand in until a company names its own stages.
    stages = {"1": "Quote/Proposal", "3": "Awaiting Signature", "s4": "Discovery"}

    assert rc._late_stage_ids(stages) == ["1"]

    # Named stages (an id or a label, any case, padded) are the whole answer.
    monkeypatch.setattr(
        rc,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(
            pipeline_ids=("default",),
            sales_rhythm_late_stages=("awaiting SIGNATURE", "s4"),
        ),
    )
    assert rc._late_stage_ids(stages) == ["3", "s4"]


# ── slippage ─────────────────────────────────────────────────────────────────


def _pushed(hs, deal_id, n):
    entries = [{"value": _iso(2026, 1, 31), "timestamp": _iso(2026, 1, 1)}]
    for i in range(n):
        entries.append(
            {"value": _iso(2026, 2 + i, 27), "timestamp": _iso(2026, 1, 2 + i)}
        )
    hs.history.setdefault(deal_id, {})["closedate"] = entries


def test_slippage_is_pushed_three_times_or_seven_quiet_days(hs):
    hs.add(
        "deals",
        "1",
        dealname="Pushed",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 30),
    )
    _pushed(hs, "1", 3)
    hs.add(
        "deals",
        "2",
        dealname="Quiet",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 20),
    )
    _pushed(hs, "2", 1)
    hs.add(
        "deals",
        "3",
        dealname="Fine",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 29),
    )
    _pushed(hs, "3", 2)
    hs.add("deals", "4", dealname="Never touched", hubspot_owner_id="77")

    result = rc.close_date_slippage(hs, _me(hs), now=NOW)

    assert [r.title for r in result.rows] == ["Pushed", "Never touched", "Quiet"]
    assert result.rows[0].reasons[0].startswith("close date moved later 3 times")
    assert result.scanned == 4
    assert result.notes == []


def test_slippage_runs_the_quiet_half_when_history_fails_and_says_so(hs):
    hs.fail_history = True
    hs.add(
        "deals",
        "1",
        dealname="Quiet",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 1),
    )
    result = rc.close_date_slippage(hs, _me(hs), now=NOW)
    assert [r.title for r in result.rows] == ["Quiet"]
    assert result.available
    assert "only the 7-day silence half" in result.notes[0]


# ── closing focus ────────────────────────────────────────────────────────────


def test_closing_focus_ranks_deals_that_need_action(hs):
    future = _iso(2026, 10, 9)
    hs.add(
        "deals",
        "1",
        dealname="Healthy",
        amount="80000",
        hubspot_owner_id="77",
        closedate=_iso(2026, 10, 20),
        notes_last_contacted=_iso(2026, 9, 30),
        notes_next_activity_date=future,
    )
    hs.add(
        "deals",
        "2",
        dealname="Quiet big",
        amount="70000",
        hubspot_owner_id="77",
        closedate=_iso(2026, 10, 25),
        notes_last_contacted=_iso(2026, 9, 10),
        notes_next_activity_date=future,
    )
    hs.add(
        "deals",
        "3",
        dealname="Everything wrong",
        amount="10000",
        hubspot_owner_id="77",
        closedate=_iso(2026, 10, 28),
        notes_last_contacted=_iso(2026, 9, 1),
    )
    _pushed(hs, "3", 2)
    hs.add(
        "deals",
        "4",
        dealname="Next month",
        amount="99000",
        hubspot_owner_id="77",
        closedate=_iso(2026, 11, 5),
    )

    result = rc.closing_focus(hs, _me(hs), now=NOW)

    assert [r.title for r in result.rows] == ["Everything wrong", "Quiet big"]
    assert result.scanned == 3  # closing in October only
    worst = result.rows[0].reasons
    assert "no next step booked" in worst
    assert "close date already moved 2 times" in worst


# ── re-engagement ────────────────────────────────────────────────────────────


def test_reengage_lost_picks_reachable_contacts_one_row_each(hs):
    lost = {
        "hs_is_closed": "true",
        "hs_is_closed_won": "false",
        "hubspot_owner_id": "77",
    }
    hs.add(
        "deals",
        "1",
        dealname="Acme - Onboarding",
        amount="88000",
        closedate=_iso(2025, 4, 30),
        loss_reason="Pricing",
        **lost,
    )
    hs.add(
        "deals",
        "2",
        dealname="Acme - Data Migration",
        amount="4000",
        closedate=_iso(2025, 5, 30),
        closed_lost_reason="They will pass this year",
        **lost,
    )
    hs.add(
        "deals",
        "3",
        dealname="Bounced",
        amount="50000",
        closedate=_iso(2025, 6, 1),
        loss_reason="Pricing",
        **lost,
    )
    hs.add(
        "deals",
        "4",
        dealname="Moved on",
        amount="40000",
        closedate=_iso(2025, 6, 1),
        loss_reason="Pricing",
        **lost,
    )
    hs.add(
        "deals",
        "5",
        dealname="Too recent",
        amount="40000",
        closedate=_iso(2026, 3, 1),
        loss_reason="Pricing",
        **lost,
    )
    hs.add(
        "deals",
        "6",
        dealname="Competitor",
        amount="40000",
        closedate=_iso(2025, 6, 1),
        loss_reason="Competitor",
        closed_lost_reason="Went with Initech",
        **lost,
    )
    hs.add("companies", "co1", name="Acme Industries")
    hs.add("companies", "co3", name="Bounce Co")
    hs.add("companies", "co4", name="Old Co")
    hs.add(
        "contacts",
        "c1",
        firstname="Ann",
        lastname="Lee",
        email="ann@acmecorp.example",
        jobtitle="COO",
        associatedcompanyid="co1",
    )
    hs.add(
        "contacts",
        "c3",
        firstname="Bob",
        email="bob@bounce.com",
        hs_email_hard_bounce_reason_enum="UNKNOWN_USER",
        associatedcompanyid="co3",
    )
    hs.add(
        "contacts",
        "c4",
        firstname="Cy",
        email="cy@new.com",
        associatedcompanyid="co999",
    )
    for deal, contact, company in (
        ("1", "c1", "co1"),
        ("2", "c1", "co1"),
        ("3", "c3", "co3"),
        ("4", "c4", "co4"),
    ):
        hs.link("deals", "contacts", deal, contact)
        hs.link("deals", "companies", deal, company)

    result = rc.reengage_lost(hs, _me(hs))

    assert [r.title for r in result.rows] == ["Acme - Onboarding"]
    row = result.rows[0]
    assert row.extras["contact_email"] == "ann@acmecorp.example"
    assert row.extras["company_name"] == "Acme Industries"
    assert row.extras["other_deals"] == ["Acme - Data Migration"]
    assert "over budget" in row.reasons[0]
    assert result.scanned == 5  # lost in the 12–24 month window
    assert "cannot confirm employment" in result.notes[0]


# ── missing fields ───────────────────────────────────────────────────────────


def test_missing_fields_reads_records_touched_today_by_me(hs):
    today_ts = _iso(2026, 10, 1, 15)
    hs.add(
        "contacts",
        "c1",
        firstname="No",
        lastname="Title",
        lastmodifieddate=today_ts,
        hs_updated_by_user_id="9001",
        hs_buying_role="CHAMPION",
    )
    hs.add(
        "contacts",
        "c2",
        firstname="All",
        lastname="Good",
        lastmodifieddate=today_ts,
        hs_updated_by_user_id="9001",
        jobtitle="CFO",
        hs_buying_role="DECISION_MAKER",
    )
    hs.add(
        "contacts",
        "c3",
        firstname="Someone",
        lastname="Else",
        lastmodifieddate=today_ts,
        hs_updated_by_user_id="9002",
    )
    hs.add(
        "deals",
        "1",
        dealname="No next step",
        amount="1000",
        closedate=_iso(2026, 11, 1),
        hs_lastmodifieddate=today_ts,
        hs_updated_by_user_id="9001",
    )

    result = rc.missing_fields(hs, _me(hs), day=TODAY)

    assert [(r.title, r.reasons) for r in result.rows] == [
        ("No next step", ["no next step booked"]),
        ("No Title", ["no job title"]),
    ]
    assert result.scanned == 3
    assert "reads Buying role in place of Persona" in result.notes[0]
    assert "not set up" not in result.notes[0]


def test_missing_fields_team_scope_means_any_person_not_a_workflow(hs):
    rc.missing_fields(hs, _team(hs), day=TODAY)
    groups = next(
        c[2] for c in hs.calls if c[0] == "search_groups" and c[1] == "contacts"
    )
    assert {
        "propertyName": "hs_updated_by_user_id",
        "operator": "HAS_PROPERTY",
    } in groups[1]


def test_missing_fields_hides_foreign_and_mixed_contacts(hs):
    hs.fixture_unlinked_owned = False
    hs.add("deals", "own", pipeline="default")
    hs.add("deals", "foreign", pipeline="other-workspace")
    for cid, name in (("own", "Allowed"), ("foreign", "Foreign"), ("mixed", "Mixed")):
        hs.add(
            "contacts",
            cid,
            firstname=name,
            lastmodifieddate=_iso(2026, 10, 1, 15),
            hs_updated_by_user_id="9001",
        )
    hs.link("contacts", "deals", "own", "own")
    hs.link("contacts", "deals", "foreign", "foreign")
    hs.link("contacts", "deals", "mixed", "own", "foreign")

    result = rc.missing_fields(hs, _me(hs), day=TODAY)

    assert result.scanned == 1
    assert [row.title for row in result.rows] == ["Allowed"]


def test_missing_fields_without_a_user_id_says_so(hs):
    hs.owners["77"]["user_id"] = ""
    result = rc.missing_fields(hs, _me(hs), day=TODAY)
    assert not result.available and result.reason == rc.NO_HUBSPOT_USER


# ── overdue ──────────────────────────────────────────────────────────────────


def test_overdue_lists_tasks_and_passed_close_dates(hs):
    hs.add(
        "tasks",
        "t1",
        hs_task_subject="Send SOW",
        hs_timestamp=_iso(2026, 9, 25),
        hs_task_status="NOT_STARTED",
        hubspot_owner_id="77",
    )
    hs.add(
        "tasks",
        "t2",
        hs_task_subject="Done",
        hs_timestamp=_iso(2026, 9, 20),
        hs_task_status="COMPLETED",
        hubspot_owner_id="77",
    )
    hs.add(
        "tasks",
        "t3",
        hs_task_subject="Later",
        hs_timestamp=_iso(2026, 10, 5),
        hs_task_status="NOT_STARTED",
        hubspot_owner_id="77",
    )
    hs.add(
        "deals",
        "1",
        dealname="Stale close",
        hubspot_owner_id="77",
        closedate=_iso(2026, 9, 1),
    )
    hs.add(
        "deals",
        "2",
        dealname="Future close",
        hubspot_owner_id="77",
        closedate=_iso(2026, 12, 1),
    )

    result = rc.overdue(hs, _me(hs), today=TODAY)

    assert [r.title for r in result.rows] == ["Stale close", "Send SOW"]
    assert result.scanned == 4  # 2 open tasks + 2 open deals


def test_overdue_counts_and_shows_only_owned_tasks(hs):
    hs.fixture_unlinked_owned = False
    hs.add("deals", "own", pipeline="default", hubspot_owner_id="77")
    hs.add("deals", "foreign", pipeline="other-workspace")
    for tid, title in (
        ("own", "Owned task"),
        ("foreign", "Foreign task"),
        ("mixed", "Mixed task"),
    ):
        hs.add(
            "tasks",
            tid,
            hs_task_subject=title,
            hs_timestamp=_iso(2026, 9, 25),
            hs_task_status="NOT_STARTED",
            hubspot_owner_id="77",
        )
    hs.link("tasks", "deals", "own", "own")
    hs.link("tasks", "deals", "foreign", "foreign")
    hs.link("tasks", "deals", "mixed", "own", "foreign")

    result = rc.overdue(hs, _me(hs), today=TODAY)

    assert result.scanned == 2  # one owned task and one owned open deal
    assert [row.title for row in result.rows] == ["Owned task"]
    assert not any(call[0] == "count" and call[1] == "tasks" for call in hs.calls)


def test_the_team_overdue_view_leaves_people_s_tasks_out(hs):
    rc.overdue(hs, _team(hs), today=TODAY, include_tasks=False)
    assert not [c for c in hs.calls if c[1] == "tasks"]


# ── the client's own last contact ────────────────────────────────────────────
#
# Two rows in a "top 10 closing this month" list said "no contact in 82 /
# 50 days". Both deals' own Last Contacted said exactly that, but the reps had
# emailed someone at each client who was not on the deal 13 / 7 days earlier,
# and HubSpot logged it on the client's company record instead.


def _deal(hs, rid, name, *, last, owner="77", amount="10000", close=(2026, 10, 30)):
    hs.add(
        "deals",
        rid,
        dealname=name,
        amount=amount,
        hubspot_owner_id=owner,
        closedate=_iso(*close),
        notes_last_contacted=last,
    )


def _client(hs, deal_id, company_id, name, last, *, types=(5,)):
    hs.add("companies", company_id, name=name, notes_last_contacted=last)
    hs.link("deals", "companies", deal_id, company_id, types=types)


def test_the_team_list_gives_the_client_s_newer_contact_beside_the_deal_s(hs):
    _deal(
        hs,
        "1",
        "Contoso Ltd - Annual Review",
        last=_iso(2026, 7, 10, 21),
        owner="88",
    )
    _client(hs, "1", "co1", "Contoso Ltd", _iso(2026, 9, 18, 15))
    _deal(
        hs,
        "2",
        "Initech Corp - Platform Rollout",
        last=_iso(2026, 8, 11, 23),
        owner="88",
    )
    _client(hs, "2", "co2", "Initech Corp", _iso(2026, 9, 24, 15))

    result = rc.closing_focus(hs, _team(hs), now=NOW)

    reasons = {r.title: r.reasons for r in result.rows}
    assert (
        "no logged client contact in 13 days (none on this deal itself in 82 days)"
        in reasons["Contoso Ltd - Annual Review"]
    )
    assert (
        "no logged client contact in 7 days (none on this deal itself in 50 days)"
        in reasons["Initech Corp - Platform Rollout"]
    )
    assert result.client_records and not result.partial


def test_a_client_contacted_this_week_is_a_lighter_flag_ranked_after_quiet_ones(hs):
    # Same other problem on both (no next step booked); the bigger deal's
    # client heard from us two days ago, the smaller one's never did.
    _deal(hs, "1", "Client active", last=_iso(2026, 9, 1), amount="90000")
    _client(hs, "1", "co1", "Acme Bank", _iso(2026, 9, 29))
    _deal(hs, "2", "Client quiet", last=_iso(2026, 9, 1), amount="10000")

    result = rc.closing_focus(hs, _me(hs), now=NOW)

    assert [r.title for r in result.rows] == ["Client quiet", "Client active"]
    assert (
        "Acme Bank was contacted 2 days ago, but no logged client contact on this "
        "deal in 30 days" in result.rows[1].reasons
    )
    assert "no logged client contact in 30 days" in result.rows[0].reasons


def test_a_deal_never_contacted_says_so_beside_its_client(hs):
    _deal(hs, "1", "Never on the deal", last=None)
    _client(hs, "1", "co1", "Acme Bank", _iso(2026, 9, 30))
    _deal(hs, "2", "Never, client quiet too", last=None)
    _client(hs, "2", "co2", "Beta Bank", _iso(2026, 9, 11))

    reasons = {
        r.title: r.reasons for r in rc.close_date_slippage(hs, _me(hs), now=NOW).rows
    }

    assert reasons["Never on the deal"] == [
        "Acme Bank was contacted yesterday, but this deal has no logged client "
        "contact on record"
    ]
    assert reasons["Never, client quiet too"] == [
        "no logged client contact in 20 days (none ever on this deal)"
    ]


def test_an_older_client_date_changes_nothing(hs):
    _deal(hs, "1", "Deal is newer", last=_iso(2026, 9, 10))
    _client(hs, "1", "co1", "Acme Bank", _iso(2026, 8, 1))

    result = rc.close_date_slippage(hs, _me(hs), now=NOW)

    assert result.rows[0].reasons == ["no logged client contact in 21 days"]


def test_a_client_date_newer_by_hours_only_does_not_repeat_the_number():
    """Same day count on both: "no contact in 21 days (none on this deal itself
    in 21 days)" says nothing the deal's own date did not."""
    deal_last = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)
    client = rc.ClientContact(
        company_id="co1",
        name="Acme Bank",
        last_contact=datetime(2026, 9, 10, 15, tzinfo=timezone.utc),
    )

    quiet = rc.silence(
        deal_last, client, now=NOW, quiet_days=7, phrase="logged client contact"
    )

    assert (quiet.state, quiet.days) == (rc.QUIET, 21)
    assert quiet.reason == "no logged client contact in 21 days"


def test_the_rhythm_read_failure_logs_no_credential(hs, caplog, monkeypatch):
    # A transport error is retried; skip the back-off.
    monkeypatch.setattr("rubik_core.sales.rhythm_checks.time.sleep", lambda _s: None)

    def boom(*_a, **_k):
        raise HubSpotAPIError(
            "HubSpot transport error: Illegal header value "
            "b'Bearer pat-na1-SECRET-TOKEN\\n'"
        )

    _deal(hs, "1", "Quiet", last=_iso(2026, 9, 1))
    hs.batch_associations = boom

    with caplog.at_level("WARNING"):
        result = rc.close_date_slippage(hs, _me(hs), now=NOW)

    assert not result.client_records
    assert "client company read failed" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_only_quiet_deals_cost_a_company_read(hs):
    _deal(hs, "1", "Fresh", last=_iso(2026, 9, 30))
    _deal(hs, "2", "Quiet", last=_iso(2026, 9, 1))

    rc.closing_focus(hs, _me(hs), now=NOW)

    company_reads = [c for c in hs.calls if c[0] == "assoc" and c[2] == "companies"]
    assert company_reads == [("assoc", "deals", "companies", ("2",))]


def test_the_primary_company_wins_over_a_second_link(hs):
    # A partner deal can carry the partner's company too; its date is
    # not the client's.
    _deal(hs, "1", "Globex - Initech partnership", last=_iso(2026, 9, 1))
    _client(hs, "1", "client", "Globex Corp", _iso(2026, 9, 20), types=(5, 341))
    _client(hs, "1", "vendor", "Initech", _iso(2026, 9, 30), types=(341,))

    result = rc.close_date_slippage(hs, _me(hs), now=NOW)

    assert result.rows[0].reasons == [
        "no logged client contact in 11 days (none on this deal itself in 30 days)"
    ]


def test_without_a_primary_label_the_most_recently_contacted_company_wins(hs):
    _deal(hs, "1", "Older deal", last=_iso(2026, 9, 1))
    _client(hs, "1", "a", "Parent Co", _iso(2026, 9, 10), types=())
    _client(hs, "1", "b", "Same Client DBA", _iso(2026, 9, 25), types=())

    contact = rc.client_contacts(hs, ["1"])["1"]

    assert contact.name == "Same Client DBA"
    assert contact.last_contact == datetime(2026, 9, 25, 17, tzinfo=timezone.utc)


def test_a_failed_company_read_keeps_the_deal_wording_and_says_so(hs):
    hs.fail_company_read = True
    _deal(hs, "1", "Quiet", last=_iso(2026, 9, 1))
    _client(hs, "1", "co1", "Acme Bank", _iso(2026, 9, 30))

    result = rc.close_date_slippage(hs, _team(hs), now=NOW)

    assert result.rows[0].reasons == ["no logged client contact in 30 days"]
    assert result.partial and not result.client_records
    assert "client company records could not be read" in result.notes[0]
    block = rc.format_for_chat(result, _team(hs))
    assert "client's company record" not in block  # never claims what it didn't read


def test_the_team_caveat_no_longer_blames_unlogged_email(hs):
    _deal(hs, "1", "Quiet", last=_iso(2026, 9, 1))

    result = rc.closing_focus(hs, _team(hs), now=NOW)
    block = rc.format_for_chat(result, _team(hs))

    assert "on the deal and on the client's company record" in block
    assert "emailing without logging it" not in block
    assert "does not read anyone's email or calendar" in block


def test_slippage_ranks_whole_client_quiet_before_deal_only(hs):
    _deal(hs, "1", "Quiet 30", last=_iso(2026, 9, 1))
    _deal(hs, "2", "Deal-only 61", last=_iso(2026, 8, 1))
    _client(hs, "2", "co2", "Busy Client", _iso(2026, 9, 30))
    _deal(hs, "3", "Quiet 15", last=_iso(2026, 9, 16))

    result = rc.close_date_slippage(hs, _me(hs), now=NOW)

    assert [r.title for r in result.rows] == ["Quiet 30", "Quiet 15", "Deal-only 61"]


def test_the_http_client_reads_a_deal_s_primary_company(monkeypatch):
    import httpx

    payload = {
        "results": [
            {
                "from": {"id": "1"},
                "to": [
                    {
                        "toObjectId": 11,
                        "associationTypes": [
                            {"category": "HUBSPOT_DEFINED", "typeId": 341}
                        ],
                    },
                    {
                        "toObjectId": 12,
                        "associationTypes": [
                            {"category": "HUBSPOT_DEFINED", "typeId": 5},
                            {"category": "HUBSPOT_DEFINED", "typeId": 341},
                        ],
                    },
                ],
            },
            {
                "from": {"id": "2"},
                "to": [
                    {
                        "toObjectId": 21,
                        "associationTypes": [
                            {"category": "HUBSPOT_DEFINED", "typeId": 341}
                        ],
                    }
                ],
            },
        ]
    }
    real_client = httpx.Client

    def respond(request):
        if request.url.path.endswith("/crm/v3/objects/deals/batch/read"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "1", "properties": {"pipeline": "default"}},
                        {"id": "2", "properties": {"pipeline": "default"}},
                    ]
                },
            )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        hubspot_api.httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(respond)),
    )
    c = hubspot_api.HubSpotAPIClient(token="tok", base_url="https://api.hubapi.test")

    assert c.batch_associations("deals", "companies", ["1", "2"], prefer_type_id=5) == {
        "1": ["12"],
        "2": ["21"],  # no primary label: keeps what it has
    }
    assert c.batch_associations("deals", "companies", ["1", "2"]) == {
        "1": ["11", "12"],
        "2": ["21"],
    }


# ── run_check: workspace scope and failure ───────────────────────────────────


def test_run_check_refuses_unconfigured_deal_pipelines_before_client_calls(
    monkeypatch, hs
):
    monkeypatch.setattr(rc, "get_hubspot_config", HubSpotConnectorConfig)
    scope = rc.RhythmScope(
        owner_ids=("77",), user_ids=("9001",), team=False, label="you"
    )

    result = rc.run_check("single_threaded", hs, scope)

    assert not result.available and result.reason == rc.CRM_UNAVAILABLE
    assert hs.calls == []


def test_run_check_is_scoped_to_workspace_and_connector_config(monkeypatch, hs):
    workspace = {"slug": "acme", "pipeline": "acme"}
    monkeypatch.setattr(rc, "current_workspace", lambda: workspace["slug"])
    monkeypatch.setattr(
        rc,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=(workspace["pipeline"],)),
    )
    scope = rc.RhythmScope(
        owner_ids=("77",), user_ids=("9001",), team=False, label="you"
    )
    hs.add(
        "deals",
        "a",
        pipeline="acme",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )
    hs.add(
        "deals",
        "b",
        pipeline="beta",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )

    acme = rc.run_check("single_threaded", hs, scope)
    workspace.update(slug="beta", pipeline="beta")
    beta = rc.run_check("single_threaded", hs, scope)

    assert [row.record_id for row in acme.rows] == ["a"]
    assert [row.record_id for row in beta.rows] == ["b"]


def test_run_check_reloads_after_a_rotated_hubspot_token(hs):
    scope = rc.RhythmScope(
        owner_ids=("77",), user_ids=("9001",), team=False, label="you"
    )
    hs.add(
        "deals",
        "old",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )
    first = rc.run_check("single_threaded", hs, scope)
    hs.objects["deals"].clear()
    hs.add(
        "deals",
        "new",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )
    hs.token = "rotated-rhythm-token"

    second = rc.run_check("single_threaded", hs, scope)

    assert [row.record_id for row in first.rows] == ["old"]
    assert [row.record_id for row in second.rows] == ["new"]


def test_directory_cache_does_not_cross_a_rotated_hubspot_token(hs):
    _, old_stages = rc.directory(hs)
    hs.stages = {"new_stage": "New stage"}
    hs.token = "rotated-rhythm-token"

    _, new_stages = rc.directory(hs)

    assert "new_stage" not in old_stages
    assert new_stages == {"new_stage": "New stage"}


def test_directory_cache_remains_bounded_across_token_rotations(hs):
    scope = rc.RhythmScope(
        owner_ids=("77",), user_ids=("9001",), team=False, label="you"
    )
    for index in range(rc._DIR_CACHE_MAX + 2):
        hs.token = f"rotated-token-{index}"
        rc.directory(hs)
        rc.run_check("single_threaded", hs, scope)

    assert len(rc._dir_cache) <= rc._DIR_CACHE_MAX


def test_run_check_rechecks_crm_after_a_deal_moves_to_a_foreign_pipeline(hs):
    scope = _me(hs)
    hs.add(
        "deals",
        "1",
        pipeline="default",
        dealname="Previously ours",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )
    first = rc.run_check("single_threaded", hs, scope)
    before = len(hs.calls)
    hs.objects["deals"]["1"]["pipeline"] = "other-workspace"
    second = rc.run_check("single_threaded", hs, scope)
    assert [row.record_id for row in first.rows] == ["1"]
    assert second.rows == []
    assert len(hs.calls) > before


def test_run_check_turns_a_failure_into_unavailable_and_does_not_cache_it(hs):
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise HubSpotAPIError("HubSpot deals search failed: HTTP 400: nope")

    hs.count = boom
    scope = _me(hs)
    first = rc.run_check("single_threaded", hs, scope)
    second = rc.run_check("single_threaded", hs, scope)
    assert not first.available and first.reason == rc.CRM_READ_FAILED
    assert first.rows == []
    assert calls["n"] == 2  # retried for real the second time, not served from cache


# ── chat formatting and the planner path ─────────────────────────────────────


def test_chat_block_states_the_denominator_and_notes(hs):
    result = rc.CheckResult(
        check="single_threaded",
        rows=[
            rc.CheckRow(
                record_id="1",
                object_type="deal",
                title="Acme",
                reasons=["only contact: Jane"],
                amount=45000,
            )
        ],
        scanned=7,
        notes=["a caveat"],
    )
    block = rc.format_for_chat(result, _me(hs))
    assert "1 of 7 open deals of $30,000 or more qualify." in block
    assert "Acme | $45,000 | only contact: Jane" in block
    assert "Note: a caveat" in block


def test_team_block_names_owners(hs):
    result = rc.CheckResult(
        check="closing_focus",
        rows=[
            rc.CheckRow(
                record_id="1",
                object_type="deal",
                title="Acme",
                reasons=["closes Oct 9, 2026"],
                owner_name="Robin Two",
            )
        ],
        scanned=12,
    )
    block = rc.format_for_chat(result, _team(hs))
    assert "every deal owner" in block and "owner Robin Two" in block


def _mock_plan(spec):
    def _plan(self, query):
        return spec

    return _plan


def test_the_planner_routes_a_team_question_to_the_check(monkeypatch, hs):
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", lambda: hs)
    monkeypatch.setattr(hubspot_api, "_rhythm_profile", lambda email: CEO)
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_plan",
        _mock_plan({"intent": "closing_focus", "owner_scope": "team"}),
    )
    for i in range(12):
        hs.add(
            "deals",
            str(i),
            dealname=f"Deal {i:02d}",
            amount=str(1000 * (i + 1)),
            hubspot_owner_id="77" if i % 2 else "88",
            closedate=_iso(2026, 10, 20),
        )

    block, _ = HubSpotAPIRetriever().retrieve(
        "Which 10 deals closing this month across my team need action today?",
        caller_email="ebrooks@acme.example",
    )

    assert "12 of 12 open deals closing this month qualify." in block
    assert "10. " in block and "11. " not in block  # the team view's top 10
    assert "…and 2 more." in block


def test_a_my_question_from_a_non_owner_is_refused_not_widened(monkeypatch, hs):
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", lambda: hs)
    monkeypatch.setattr(
        HubSpotAPIRetriever,
        "_plan",
        _mock_plan({"intent": "single_threaded", "owner_scope": "me"}),
    )
    hs.add(
        "deals",
        "1",
        dealname="Acme",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )
    block, _ = HubSpotAPIRetriever().retrieve(
        "List my active opportunities over $30k that only have a single contact",
        caller_email="ebrooks@acme.example",
    )
    assert "couldn't match you" in block
    assert "Acme" not in block


# ── review fixes ─────────────────────────────────────────────────────────────

CEO = SimpleNamespace(
    email="ebrooks@acme.example",
    department="Executive",
    managed_departments=["*"],
    is_exec=True,
    is_admin=False,
    roles=["user"],
)
REP = SimpleNamespace(
    email="ae@acme.example",
    department="Sales",
    managed_departments=[],
    is_exec=False,
    is_admin=False,
    roles=["user"],
)
MARKETER = SimpleNamespace(
    email="rep2@acme.example",
    department="Marketing",
    managed_departments=[],
    is_exec=False,
    is_admin=False,
    roles=["user"],
)


@pytest.fixture(autouse=True)
def _ceo_named_on_the_team_view(monkeypatch):
    """The team view ships with nobody on it; an admin names the leaders who
    review the pipeline. Installed here the same way, as Feature Access data."""
    from backend.tests._rosters import pin_rosters
    from rubik_core.features import FeaturePolicy

    pin_rosters(
        monkeypatch,
        {
            "sales_rhythm_team": FeaturePolicy(
                extra_emails=frozenset({"ebrooks@acme.example"}),
                exec_allowed=False,
                admin_allowed=False,
            )
        },
    )


def _ask(monkeypatch, hs, spec, profile, email):
    monkeypatch.setattr(hubspot_api, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", lambda: hs)
    monkeypatch.setattr(hubspot_api, "_rhythm_profile", lambda _e: profile)
    monkeypatch.setattr(HubSpotAPIRetriever, "_plan", _mock_plan(spec))
    block, _ = HubSpotAPIRetriever().retrieve(
        "which deals need action today", caller_email=email
    )
    return block


def _seed_two_reps(hs):
    hs.add(
        "deals",
        "mine",
        dealname="My deal",
        amount="45000",
        hubspot_owner_id="77",
        num_associated_contacts="1",
    )
    hs.add(
        "deals",
        "theirs",
        dealname="Colleague deal",
        amount="46000",
        hubspot_owner_id="88",
        num_associated_contacts="1",
    )


def test_a_team_question_from_someone_off_the_team_roster_is_refused(monkeypatch, hs):
    _seed_two_reps(hs)
    block = _ask(
        monkeypatch,
        hs,
        {"intent": "single_threaded", "owner_scope": "team"},
        REP,
        "ae@acme.example",
    )
    assert "only open to people an admin has added by name" in block
    assert "Colleague deal" not in block and "My deal" not in block


def test_a_question_with_no_scope_is_about_me_never_the_team(monkeypatch, hs):
    _seed_two_reps(hs)
    block = _ask(monkeypatch, hs, {"intent": "single_threaded"}, REP, "ae@acme.example")
    assert "My deal" in block
    assert "Colleague deal" not in block


def test_a_named_colleague_needs_the_team_roster(monkeypatch, hs):
    _seed_two_reps(hs)
    block = _ask(
        monkeypatch,
        hs,
        {"intent": "single_threaded", "owner_name": "Robin Two"},
        REP,
        "ae@acme.example",
    )
    assert "Colleague deal" not in block


def test_no_one_reads_a_colleague_s_activity_log(monkeypatch, hs):
    hs.add(
        "notes",
        "n1",
        hs_note_body="Private call notes",
        hs_timestamp=_iso(2026, 10, 1, 15),
        hubspot_owner_id="88",
    )
    block = _ask(
        monkeypatch,
        hs,
        {"intent": "activity_recap", "owner_scope": "team"},
        CEO,
        "ebrooks@acme.example",
    )
    assert "only available for your own records" in block
    assert "Private call notes" not in block


def test_activity_recap_hides_foreign_mixed_and_unlinked_portal_activity(hs):
    hs.fixture_unlinked_owned = False
    hs.add("deals", "own", pipeline="default")
    hs.add("deals", "foreign", pipeline="other-workspace")
    hs.add("contacts", "foreign-contact", firstname="Foreign")
    hs.link("contacts", "deals", "foreign-contact", "foreign")
    for aid, title in (
        ("own", "Owned call"),
        ("foreign", "Foreign call secret"),
        ("mixed", "Mixed call secret"),
        ("foreign-contact", "Shared contact call secret"),
        ("unlinked", "Unlinked call secret"),
    ):
        hs.add(
            "calls",
            aid,
            hs_call_title=title,
            hs_call_body=f"{title} body",
            hs_timestamp=_iso(2026, 10, 1, 15),
            hubspot_owner_id="77",
        )
    hs.link("calls", "deals", "own", "own")
    hs.link("calls", "deals", "foreign", "foreign")
    hs.link("calls", "deals", "mixed", "own", "foreign")
    hs.link("calls", "deals", "foreign-contact", "own")
    hs.link("calls", "contacts", "foreign-contact", "foreign-contact")

    result = rc.activity_today(hs, _me(hs), day=TODAY)

    assert [row.title for row in result.rows] == ["Owned call"]
    assert result.scanned == 1
    assert "secret" not in rc.format_for_chat(result, _me(hs)).lower()


def test_the_team_overdue_view_never_lists_colleagues_tasks(monkeypatch, hs):
    hs.add(
        "tasks",
        "t1",
        hs_task_subject="Robin's private to-do",
        hs_timestamp=_iso(2026, 9, 20),
        hs_task_status="NOT_STARTED",
        hubspot_owner_id="88",
    )
    block = _ask(
        monkeypatch,
        hs,
        {"intent": "overdue_items", "owner_name": "Robin Two"},
        CEO,
        "ebrooks@acme.example",
    )
    assert "Robin's private to-do" not in block


def test_a_rep_off_every_roster_cannot_run_the_checks(monkeypatch, hs):
    from backend.tests._rosters import pin_rosters
    from rubik_core.features import FeaturePolicy

    # The personal checks ship open to everyone; an admin narrowed them here.
    pin_rosters(
        monkeypatch,
        {
            "sales_rhythm": FeaturePolicy(
                departments=frozenset({"sales", "systems"}), exec_allowed=False
            )
        },
    )
    _seed_two_reps(hs)
    block = _ask(
        monkeypatch,
        hs,
        {"intent": "single_threaded", "owner_scope": "me"},
        MARKETER,
        "rep2@acme.example",
    )
    assert "aren't switched on for your team" in block
    assert "Colleague deal" not in block


def test_the_kill_switch_stops_the_chat_checks(monkeypatch, hs):
    _seed_two_reps(hs)
    monkeypatch.setattr(settings, "SALES_RHYTHM_ENABLED", False)
    block = _ask(
        monkeypatch,
        hs,
        {"intent": "single_threaded", "owner_scope": "me"},
        REP,
        "ae@acme.example",
    )
    assert "switched off" in block and "My deal" not in block


def test_a_threshold_the_question_names_is_used_and_shown(monkeypatch, hs):
    _seed_two_reps(hs)
    spec = {
        "intent": "single_threaded",
        "owner_scope": "me",
        "filters": [{"field": "amount_gte", "value": 50000}],
    }
    block = _ask(monkeypatch, hs, spec, REP, "ae@acme.example")
    assert "$50,000" in block
    assert "My deal" not in block  # $45,000 is under the threshold asked for


def test_list_all_lifts_the_checklist_s_cap(monkeypatch, hs):
    for i in range(8):
        hs.add(
            "deals",
            f"d{i}",
            dealname=f"Deal {i}",
            hubspot_owner_id="77",
            closedate=_iso(2026, 10, 20),
        )
    block = _ask(
        monkeypatch,
        hs,
        {"intent": "closing_focus", "owner_scope": "me", "limit": "all"},
        REP,
        "ae@acme.example",
    )
    assert "8. " in block and "…and" not in block


def test_a_total_recap_failure_is_unavailable_not_empty(hs):
    def boom(*_a, **_k):
        raise HubSpotAPIError("HubSpot calls search failed: HTTP 401: expired")

    hs.search_all = boom
    result = rc.run_check("activity_today", hs, _me(hs))
    assert not result.available and result.reason == rc.CRM_READ_FAILED
    assert "Nothing has been logged" not in rc.format_for_chat(result, _me(hs))


def test_a_partial_answer_retries_on_the_next_request(hs):
    hs.fail_history = True
    hs.add(
        "deals",
        "1",
        dealname="Quiet",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 1),
    )
    scope = _me(hs)
    first = rc.run_check("close_date_slippage", hs, scope)
    assert first.partial
    hs.fail_history = False
    _pushed(hs, "1", 3)
    second = rc.run_check("close_date_slippage", hs, scope)
    assert not second.partial
    assert "moved later 3 times" in second.rows[0].reasons[0]


def test_a_failed_stage_read_is_not_cached_for_half_an_hour(hs):
    stages = hs.stages
    hs.stages = {}
    rc.directory(hs)
    hs.stages = stages
    _, labels = rc.directory(hs)
    assert labels == stages


def test_months_back_keeps_the_day_and_clamps_to_month_end():
    assert rc.months_back(date(2026, 10, 31), 12) == date(2025, 10, 31)
    assert rc.months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert rc.months_back(date(2026, 1, 15), 24) == date(2024, 1, 15)


def test_reengagement_window_is_exactly_12_to_24_months(monkeypatch, hs):
    monkeypatch.setattr(rc, "_today", lambda: date(2026, 10, 31))
    lost = {
        "hs_is_closed": "true",
        "hs_is_closed_won": "false",
        "hubspot_owner_id": "77",
        "loss_reason": "Pricing",
    }
    hs.add(
        "deals",
        "in",
        dealname="12.5 months ago",
        amount="1",
        closedate=_iso(2025, 10, 15),
        **lost,
    )
    hs.add(
        "deals",
        "old",
        dealname="almost 25 months ago",
        amount="1",
        closedate=_iso(2024, 10, 5),
        **lost,
    )
    result = rc.reengage_lost(hs, _me(hs), today=date(2026, 10, 31))
    assert result.scanned == 1  # only the 12.5-month-old loss is in the window


def test_reengagement_does_not_claim_a_company_link_it_never_checked(hs):
    lost = {
        "hs_is_closed": "true",
        "hs_is_closed_won": "false",
        "hubspot_owner_id": "77",
    }
    hs.add(
        "deals",
        "1",
        dealname="No company",
        amount="9000",
        closedate=_iso(2025, 4, 30),
        loss_reason="Pricing",
        **lost,
    )
    hs.add("contacts", "c1", firstname="Ann", email="ann@acmecorp.example")
    hs.link("deals", "contacts", "1", "c1")
    row = rc.reengage_lost(hs, _me(hs)).rows[0]
    assert "no company link on record" in row.reasons[2]


def test_reengagement_says_when_it_capped_the_walk(monkeypatch, hs):
    monkeypatch.setattr(rc, "REENGAGE_MAX_CANDIDATES", 1)
    lost = {
        "hs_is_closed": "true",
        "hs_is_closed_won": "false",
        "hubspot_owner_id": "77",
        "loss_reason": "Pricing",
    }
    for i in range(3):
        hs.add(
            "deals",
            str(i),
            dealname=f"L{i}",
            amount=str(i + 1),
            closedate=_iso(2025, 4, 30),
            **lost,
        )
    notes = rc.reengage_lost(hs, _me(hs)).notes
    assert any("Checked the 1 largest of the 3" in n for n in notes)


def test_overdue_counts_calendar_days(hs):
    hs.add(
        "tasks",
        "t1",
        hs_task_subject="Yesterday",
        hs_timestamp=_iso(2026, 9, 30, 15),
        hs_task_status="NOT_STARTED",
        hubspot_owner_id="77",
    )
    row = rc.overdue(hs, _me(hs), today=TODAY).rows[0]
    assert "(1 day overdue)" in row.reasons[0]
