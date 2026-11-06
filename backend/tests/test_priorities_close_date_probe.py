"""The close-date credibility card (``close_date_at_risk``).

Automates a check a sales leader would otherwise run by hand: read a deal's
estimated close date, compare it to how long the client has ACTUALLY been
silent, and ask the AE why the date is still real.

Two properties matter more than the card itself and are pinned hardest here:

* **the card can come back.** This is a recurring condition on a long-lived
  entity — true in September, fixed, true again in November. A `resolved` row
  never expires and ``reopen_entity_everywhere`` skips a manual resolve, so one
  check mark on a durable kind would blind the reader to that deal forever.
  That is the read-filter poisoning that can zero a real feed.
* **silence means CLIENT silence.** An internal note a rep writes to themselves
  must never read as contact.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.signals.hubspot import (
    _live_rows,
    close_date_at_risk_signal,
)
from rubik_core.priorities.models import (
    FORWARD_PRIORITY_KINDS,
    PLATE_ONLY_PRIORITY_KINDS,
    SNOOZE_ONLY_PRIORITY_KINDS,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.orchestrator import _cap_deal_cards, _drop_ungated_kinds

NOW = datetime.now(timezone.utc)


def _deal(*, close_in_days=20, quiet_days=27, contacted=True, deal_id="d-1"):
    props = {
        "hs_object_id": deal_id,
        "dealname": "Acme Platform Rollout",
        "dealstage": "stage-1",
        "pipeline": "default",
        "amount": "25000",
        "closedate": (NOW + timedelta(days=close_in_days)).isoformat(),
        "notes_last_updated": (NOW - timedelta(days=3)).isoformat(),
    }
    if contacted:
        props["notes_last_contacted"] = (NOW - timedelta(days=quiet_days)).isoformat()
    return {"id": deal_id, "properties": props}


def _probe(deal):
    _stale, _stuck, at_risk = _live_rows(
        [deal], "ae@acme.example", "o-1", {"stage-1": "Proposal"}
    )
    return [s for s in (close_date_at_risk_signal(r) for r in at_risk) if s]


# ── when the card fires ──────────────────────────────────────────────────────


def test_a_near_date_with_a_quiet_client_is_flagged():
    """The card's worked example: date inside the horizon, client silent
    longer than the threshold."""
    sigs = _probe(_deal(close_in_days=20, quiet_days=27))
    assert len(sigs) == 1
    assert sigs[0].payload["days_since_engagement"] == 27
    assert sigs[0].payload["close_date_passed"] is False


def test_a_date_that_already_passed_is_flagged_even_with_no_silence():
    """'You have a 7/31 close date that didn't close' is its own problem — the
    record is simply wrong, whether or not anyone has been talking."""
    sigs = _probe(_deal(close_in_days=-12, contacted=False))
    assert len(sigs) == 1
    assert sigs[0].payload["close_date_passed"] is True
    assert sigs[0].payload["days_to_close"] == -12


def test_a_healthy_deal_is_not_flagged():
    """Near date, recent contact — a pipeline working normally must be quiet."""
    assert _probe(_deal(close_in_days=20, quiet_days=3)) == []


def test_a_far_off_date_is_not_flagged_however_quiet_the_client():
    """Beyond the horizon the question isn't worth asking yet; the stale-lead
    probe already covers plain silence."""
    assert _probe(_deal(close_in_days=200, quiet_days=60)) == []


def test_a_deal_with_no_close_date_is_out_of_scope_entirely():
    d = _deal()
    del d["properties"]["closedate"]
    assert _probe(d) == []


# ── honesty about what was measured ──────────────────────────────────────────


def test_no_logged_contact_never_becomes_an_asserted_silence():
    """Without notes_last_contacted we do not know how long the client has been
    quiet. The forward tier stays silent rather than guessing from an internal
    note — the false negative the whole feature exists to remove."""
    assert _probe(_deal(close_in_days=20, contacted=False)) == []


def test_the_card_quotes_a_date_not_a_utc_timestamp():
    """HubSpot sends '2026-09-30T00:00:00Z' and shows the AE '2026-09-30'. A
    card quoting the timestamp disagrees with the record it is citing."""
    sig = _probe(_deal(close_in_days=20, quiet_days=27))[0]
    assert "T00:00:00" not in sig.summary
    assert "T00:00:00" not in sig.payload["close_date"]


def test_presumed_dead_measures_client_silence_not_a_slipped_date():
    """The flag means "silent this long" in every other probe, and the agent's
    merge propagates it across the deal's OTHER signals and forces them to low
    severity. Measuring a slipped date here would demote a deal's stale-lead
    card because its DATE moved, while the client was spoken to yesterday."""
    sig = _probe(_deal(close_in_days=-95, quiet_days=1))[0]
    assert sig.payload["presumed_dead"] is False


def test_an_abandoned_record_is_dropped_rather_than_nagged_about():
    """Six months of client silence is a data-hygiene problem, not a forecast
    one — and re-asking every morning is how the feature gets muted."""
    assert _probe(_deal(close_in_days=10, quiet_days=400)) == []


# ── the card must be able to come back ───────────────────────────────────────


def test_the_kind_may_never_write_a_terminal_state():
    """The read-filter guard. `resolved` never expires and the revive path skips
    a manual resolve, so a durable resolve on a recurring kind is the one
    suppression in Cubie that nothing can undo."""
    assert PriorityKind.CLOSE_DATE_AT_RISK in SNOOZE_ONLY_PRIORITY_KINDS


def test_the_check_mark_and_the_x_both_route_to_a_bounded_snooze():
    from backend.routers.home import (
        _DEAL_DISMISS_SNOOZE_DAYS,
        _DEAL_HANDLED_SNOOZE_DAYS,
        _snooze_only_days,
    )

    assert _snooze_only_days("close_date_at_risk") == _DEAL_HANDLED_SNOOZE_DAYS
    assert _DEAL_HANDLED_SNOOZE_DAYS > 0 and _DEAL_DISMISS_SNOOZE_DAYS > 0


def test_other_deal_cards_keep_their_durable_resolve():
    """Scoped by PRIORITY kind, not entity kind: a deal also carries
    neglect_debt and follow_up_debt, where "done" really can mean done."""
    from backend.routers.home import _snooze_only_days

    assert _snooze_only_days("neglect_debt") is None
    assert _snooze_only_days("follow_up_debt") is None


def test_an_elapsed_snooze_stops_being_listed_as_already_handled():
    """Load-bearing. Nothing else can bring a suppressed card back — the only
    revive path runs over cards that SURVIVED a run, so re-emission is the
    model's decision, and the prompt tells it not to re-emit anything in this
    block. On a deal whose defining property is that nothing is happening, the
    "unless new evidence arrives" escape hatch never opens."""
    from types import SimpleNamespace

    from rubik_core.priorities.agents.decision_maker import _snooze_elapsed

    elapsed = SimpleNamespace(
        state="snoozed", snoozed_until=(NOW - timedelta(days=1)).isoformat()
    )
    in_force = SimpleNamespace(
        state="snoozed", snoozed_until=(NOW + timedelta(days=5)).isoformat()
    )
    assert _snooze_elapsed(elapsed) is True
    assert _snooze_elapsed(in_force) is False
    # A resolved row is durable and must never be treated as elapsed.
    assert (
        _snooze_elapsed(SimpleNamespace(state="resolved", snoozed_until=None)) is False
    )


# ── gating, ranking and the noise budget ─────────────────────────────────────


def _card(kind=PriorityKind.CLOSE_DATE_AT_RISK, title="c"):
    return PriorityCandidate(
        title=title, urgency="high", priority_kind=kind, signal_ids=[]
    )


def test_the_kind_does_not_exist_for_a_reader_without_the_feature():
    """The prompt already omits it, but a prompt is a request, not a gate."""
    assert PriorityKind.CLOSE_DATE_AT_RISK in PLATE_ONLY_PRIORITY_KINDS
    kept, drops = _drop_ungated_kinds([_card(), _card(PriorityKind.FOLLOW_UP_DEBT)])
    assert [p.priority_kind for p in kept] == [PriorityKind.FOLLOW_UP_DEBT]
    assert drops


def test_deal_cards_cannot_take_over_a_five_card_briefing():
    """Tier 1 fires on every open deal whose date has slipped, which in an
    ungroomed CRM is a large slice of the book on day one."""
    cards = [_card(title=f"deal {i}") for i in range(5)] + [
        _card(PriorityKind.FOLLOW_UP_DEBT, title="email")
    ]
    kept, drops = _cap_deal_cards(cards)
    assert (
        sum(1 for p in kept if p.priority_kind is PriorityKind.CLOSE_DATE_AT_RISK) == 3
    )
    assert any(p.priority_kind is PriorityKind.FOLLOW_UP_DEBT for p in kept), (
        "the cap must never cost a non-deal card its slot"
    )
    assert len(drops) == 2


def test_the_kind_is_not_forward_looking():
    """If it were, the critic would apply a future-date clock and every
    already-passed close date would silently take a blocker penalty — and
    _drop_past_anchored would delete the card outright."""
    assert PriorityKind.CLOSE_DATE_AT_RISK not in FORWARD_PRIORITY_KINDS


def test_every_attr_the_plate_rules_name_is_actually_rendered_to_the_model():
    """A rule telling the model to read a key the preview never renders is an
    instruction to read something it cannot see."""
    from rubik_core.priorities.agents.decision_maker import ATTRS_PREVIEW_KEYS

    for key in (
        "close_date",
        "days_to_close",
        "close_date_passed",
        "deal_amount",
        "days_since_engagement",
        "client_days_since_engagement",
        "client_name",
    ):
        assert key in ATTRS_PREVIEW_KEYS, key


# ── the deal AND its client ──────────────────────────────────────────────────
#
# A deal's own "Last Contacted" moves only when the contact is logged on that
# deal. An email to someone at the client who is not on the deal is logged on
# their company instead, so the deal reads "82 days" while the client heard
# from us 13 days ago. The card keeps the deal's number and adds the client's;
# which deals get a card never changes.


def _client(days_ago, name="Contoso Ltd"):
    from rubik_core.sales.rhythm_checks import ClientContact

    return ClientContact(
        company_id="co-1", name=name, last_contact=NOW - timedelta(days=days_ago)
    )


def _probe_with(deal, contact):
    _stale, _stuck, at_risk = _live_rows(
        [deal],
        "ae@acme.example",
        "o-1",
        {"stage-1": "Proposal"},
        clients={deal["id"]: contact},
    )
    return [s for s in (close_date_at_risk_signal(r) for r in at_risk) if s]


def test_a_client_contacted_inside_the_window_keeps_the_deal_number_beside_it():
    sig = _probe_with(_deal(close_in_days=20, quiet_days=82), _client(13))[0]

    # The deal's own date is never replaced...
    assert sig.payload["days_since_engagement"] == 82
    # ...the client's is added beside it.
    assert sig.payload["client_days_since_engagement"] == 13
    assert sig.payload["client_name"] == "Contoso Ltd"
    assert sig.payload["client_last_engagement_at"]
    assert (
        "nothing has been logged on this deal for 82 days, though Contoso Ltd "
        "was contacted 13 days ago" in sig.summary
    )
    assert "no logged client contact for 82 days" not in sig.summary
    # A lighter flag: the deal record is quiet, the client is not.
    assert sig.severity_hint == "medium"


def test_a_client_quiet_past_the_threshold_too_reads_from_the_newer_date():
    sig = _probe_with(_deal(close_in_days=20, quiet_days=82), _client(30))[0]

    assert sig.payload["days_since_engagement"] == 82
    assert (
        "no logged client contact for 30 days (none on this deal itself for 82 days)"
        in sig.summary
    )
    assert sig.severity_hint == "high"


def test_an_older_client_date_adds_nothing_and_the_card_reads_as_before():
    plain = _probe(_deal(close_in_days=20, quiet_days=27))[0]
    sig = _probe_with(_deal(close_in_days=20, quiet_days=27), _client(40))[0]

    assert sig.summary == plain.summary
    assert "client_days_since_engagement" not in sig.payload
    assert sig.severity_hint == plain.severity_hint == "high"


def test_the_card_no_longer_repeats_the_deal_name_as_its_company():
    """Live rows carry no company name, so "(company)" was the deal name again,
    in a snippet the decision-maker cuts at 200 characters."""
    sig = _probe(_deal(close_in_days=20, quiet_days=27))[0]
    assert sig.summary.count("Acme Platform Rollout") == 1


def test_which_deals_get_a_card_never_reads_the_client():
    """The tier reads the deal's own date, so the rep's card and the manager's
    weekly roll-up still agree on every deal."""
    # Quiet deal, client emailed yesterday: still a card (a lighter one).
    assert _probe_with(_deal(close_in_days=20, quiet_days=27), _client(1))
    # Watch band on the deal: still no card, however the client reads.
    assert _probe_with(_deal(close_in_days=20, quiet_days=17), _client(2)) == []
    # Nothing logged on the deal: still no asserted silence, still no card.
    assert _probe_with(_deal(close_in_days=20, contacted=False), _client(30)) == []


def test_a_passed_date_stays_high_whoever_the_client_heard_from():
    sig = _probe_with(_deal(close_in_days=-12, quiet_days=40), _client(3))[0]

    assert sig.severity_hint == "high"
    assert (
        ", and nothing has been logged on this deal for 40 days, though Contoso "
        "Ltd was contacted 3 days ago" in sig.summary
    )


def test_presumed_dead_still_measures_the_deal_own_silence():
    """The client's newer contact may have been about another deal, so it
    never revives a dead record."""
    sig = _probe_with(_deal(close_in_days=20, quiet_days=120), _client(5))[0]

    assert sig.payload["presumed_dead"] is True
    assert sig.severity_hint == "low"
    assert sig.payload["client_days_since_engagement"] == 5


class _FakeHubSpot:
    def __init__(self, deals=(), fail=False):
        self.deals = list(deals)
        self.fail = fail
        self.calls = []

    def search_all(self, object_type, filters, properties, sorts=None):
        return list(self.deals)

    def batch_associations(self, from_type, to_type, ids, *, prefer_type_id=None):
        self.calls.append(("assoc", tuple(ids), prefer_type_id))
        return {str(i): [f"co-{i}"] for i in ids}

    def batch_read(self, object_type, ids, properties, *, properties_with_history=None):
        self.calls.append(("read", object_type, tuple(ids)))
        if self.fail:
            from rubik_core.retrieval.hubspot_api import HubSpotAPIError

            raise HubSpotAPIError(
                "HubSpot transport error: Illegal header value "
                "b'Bearer pat-na1-SECRET-TOKEN\\n'"
            )
        return [
            {
                "id": i,
                "properties": {
                    "name": "Contoso Ltd",
                    "notes_last_contacted": (NOW - timedelta(days=13)).isoformat(),
                },
            }
            for i in ids
        ]


def test_only_deals_quiet_on_their_own_cost_a_company_read():
    from backend.signals.hubspot import _quiet_deal_clients

    fresh = _deal(deal_id="fresh", quiet_days=3)
    watch = _deal(deal_id="watch", quiet_days=14)
    never = _deal(deal_id="never", contacted=False)
    hs = _FakeHubSpot()

    got = _quiet_deal_clients(hs, [fresh, watch, never], "ae@acme.example")

    # One association read (primary company first) and one company read.
    assert hs.calls[0] == ("assoc", ("watch", "never"), 5)
    assert [c[0] for c in hs.calls] == ["assoc", "read"]
    assert set(got) == {"watch", "never"}


def test_a_failed_company_read_keeps_todays_cards_and_logs_no_credential(caplog):
    from backend.signals.hubspot import _quiet_deal_clients

    deal = _deal(close_in_days=20, quiet_days=82)
    with caplog.at_level("WARNING"):
        clients = _quiet_deal_clients(
            _FakeHubSpot(fail=True), [deal], "ae@acme.example"
        )

    assert clients == {}
    sig = _probe_with(deal, None)[0]
    assert sig.summary == _probe(deal)[0].summary
    assert "client company read failed" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_the_live_source_carries_the_client_date_onto_the_signal(monkeypatch):
    from backend.signals import hubspot as hs_mod
    from backend.signals.hubspot import HubspotSignalSource
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    fake = _FakeHubSpot(deals=[_deal(close_in_days=20, quiet_days=82)])
    monkeypatch.setattr(hs_mod, "workspace_secret", lambda *_: "tok")
    monkeypatch.setattr(
        hs_mod,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda *a, **k: fake
    )
    monkeypatch.setattr(
        hs_mod,
        "_cached_directories",
        lambda client: ({"o-1": {"email": "ae@acme.example"}}, {}),
    )

    sigs = HubspotSignalSource().fetch("ae@acme.example", since=NOW)

    card = next(s for s in sigs if s.signal_type == "close_date_at_risk")
    assert card.payload["days_since_engagement"] == 82
    assert card.payload["client_days_since_engagement"] == 13
    # Every signal on the deal carries it, so the merged card does too.
    assert all(s.payload.get("client_name") == "Contoso Ltd" for s in sigs)


def test_the_decision_maker_is_shown_both_numbers():
    """The plate rule tells the model to read the silence from the attrs and
    never from prose, so a client date only in the summary would change
    nothing on the card."""
    from rubik_core.priorities.agents.decision_maker import _format_attrs_preview
    from rubik_core.priorities.agents.hubspot_agent import HubSpotChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig

    sig = _probe_with(_deal(close_in_days=20, quiet_days=82), _client(13))[0]
    agent = HubSpotChannelAgent(
        user_email="ae@acme.example", config=PrioritiesConfig.default()
    )
    attrs = agent.parse_signals([sig])[0].attrs

    preview = _format_attrs_preview(attrs)
    assert "days_since_engagement=82" in preview
    assert "client_days_since_engagement=13" in preview
    assert "client_name=Contoso Ltd" in preview
    assert "client_idle_days=13" in agent.build_prompt(agent.parse_signals([sig]))


def test_the_rules_say_what_the_client_number_means():
    from rubik_core.priorities.agents.decision_maker import _META_RULES_PROMPT

    rule = _META_RULES_PROMPT[_META_RULES_PROMPT.index("A deal's silence") :]
    rule = " ".join(rule[: rule.index("\n\n")].split())
    assert "`client_days_since_engagement`" in rule
    assert "Never say the client has been quiet for longer" in rule
    # The newer contact may be about another deal: it never revives a dead one.
    assert "`presumed_dead`" in rule
