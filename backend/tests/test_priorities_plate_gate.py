"""What's On Your Plate (registry key ``open_commitments``) — the gate.

The card is a morning briefing about what is WAITING ON the reader rather than
what is on their calendar. Three filters stood in the way, and all three are
lifted only for readers whose department an admin has onboarded:

* the high→medium demotion of backward debt with no future anchor
  (``_drop_past_anchored``), which guaranteed a calendar event always outranked
  "the client has been silent for three weeks";
* the decision-maker's "pure 'you went quiet' with no forward hook is NOT a
  priority", which kept the card from being emitted at all;
* the Gmail agent's freshest-first truncation, which discarded the stalest
  thread first — the exact one a follow-up card exists to surface.

The through-line of this file: **off must be byte-identical to today, and on
must actually admit somebody.** A gate test that only proves denial passes even
when the wiring is broken — ``feature_allows`` duck-types its ``user`` and a
bare email string silently returns False for everyone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from rubik_core.priorities.models import (
    HIGH_URGENCY_EXEMPT_KINDS,
    PLATE_HIGH_URGENCY_EXEMPT_KINDS,
    ChannelKind,
    ChannelSignal,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.orchestrator import _drop_past_anchored
from rubik_core.priorities.temporal import resolve_user_now

NOW_UTC = datetime(2026, 6, 25, 16, 0, tzinfo=timezone.utc)
PACIFIC = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def _ships_without_a_stored_roster(monkeypatch):
    """Exercise the registry default without relying on a live tenant table."""
    from rubik_core.config import settings
    from rubik_core.features import policy

    monkeypatch.setattr(policy, "stored_policy", lambda _key, db=None: None)
    monkeypatch.setattr(settings, "OPEN_COMMITMENTS_ENABLED", True)


def _ctx(**kw):
    return resolve_user_now(now_utc=NOW_UTC, tz_name=PACIFIC, **kw)


def _sig(sid, channel, **attrs):
    return ChannelSignal(id=sid, channel=channel, title=f"sig-{sid}", attrs=dict(attrs))


def _prio(kind, sid, *, urgency="high", title="p"):
    return PriorityCandidate(
        title=title, urgency=urgency, priority_kind=kind, signal_ids=[sid]
    )


def _silent_thread(idle_days: int = 21):
    """A thread the reader spoke last on, silent for three weeks — the card's
    worked example."""
    return {
        "th": _sig(
            "th",
            ChannelKind.GMAIL,
            direction="i_sent_last",
            reader_last_message_kind="ask",
            thread_idle_days=idle_days,
        )
    }


# ── The demotion, off and on ─────────────────────────────────────────────────


def test_off_a_three_week_silence_is_still_demoted_exactly_as_today():
    """The default MUST match shipped behaviour — a reader whose department is
    not onboarded sees the same ranking they saw yesterday."""
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.FOLLOW_UP_DEBT, "th")], _silent_thread(), _ctx()
    )
    assert [p.urgency for p in kept] == ["medium"]
    assert any("down-ranked" in d for d in drops)


def test_on_a_three_week_silence_keeps_the_urgency_the_model_assigned():
    """The whole point of the feature: silence that needs a decision must be
    allowed to outrank a routine upcoming meeting."""
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.FOLLOW_UP_DEBT, "th")],
        _silent_thread(),
        _ctx(),
        plate_enabled=True,
    )
    assert [p.urgency for p in kept] == ["high"]
    assert not any("down-ranked" in d for d in drops)


def test_on_covers_all_three_debt_kinds_not_just_follow_up():
    """A promise you broke, a client gone cold and an unanswered ask are the
    same shape — an obligation past its date. All three or none."""
    for kind in (
        PriorityKind.FOLLOW_UP_DEBT,
        PriorityKind.COMMITMENT_DEBT,
        PriorityKind.NEGLECT_DEBT,
    ):
        kept, _ = _drop_past_anchored(
            [_prio(kind, "th")], _silent_thread(), _ctx(), plate_enabled=True
        )
        assert [p.urgency for p in kept] == ["high"], kind


def test_the_feature_never_rescues_a_forward_kind_from_being_dropped():
    """The plate exemption is about URGENCY of backward debt. It must not
    resurrect "prep for a meeting that already happened" — that backstop is
    what makes a stale forward card structurally impossible."""
    signals = {"e": _sig("e", ChannelKind.CALENDAR, start_iso="2026-06-20T10:00:00Z")}
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.UPCOMING_PREP, "e")], signals, _ctx(), plate_enabled=True
    )
    assert kept == []
    assert drops


def test_the_plate_set_only_widens_and_never_replaces_the_shipped_exemptions():
    """Composition, not substitution: every kind exempt today stays exempt."""
    assert not (PLATE_HIGH_URGENCY_EXEMPT_KINDS & HIGH_URGENCY_EXEMPT_KINDS), (
        "the plate set must be the DELTA — an overlap means a kind is exempt "
        "twice and removing the feature would not restore today's behaviour"
    )
    assert PriorityKind.PROMISE_DUE in HIGH_URGENCY_EXEMPT_KINDS


# ── The prompt, off and on ───────────────────────────────────────────────────


def _flat(text: str) -> str:
    """Collapse the prompt's hard wrapping so an assertion can name a phrase
    without caring where the line happened to break."""
    return " ".join(text.split())


def _prompt(plate_enabled: bool) -> str:
    from rubik_core.priorities.agents import DecisionMakerAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import ConsolidatedDigest

    agent = DecisionMakerAgent(PrioritiesConfig.default())
    return agent._build_prompt(
        ConsolidatedDigest(narrative="n", all_signals=[]),
        max_priorities=5,
        focus=None,
        entity_index={},
        department="Sales",
        playbook_text="",
        plate_enabled=plate_enabled,
        temporal=_ctx(),
    )


def test_off_the_prompt_does_not_mention_the_plate_at_all():
    """Byte-identical prompts for non-onboarded readers is the reason this is
    a separate section rather than an edit to rule 4."""
    assert "WHAT'S ON YOUR PLATE" not in _prompt(False)


def test_on_the_prompt_lifts_the_clause_that_suppressed_the_card():
    """Exempting a card from the urgency demotion does nothing if the prompt
    never lets the model emit it. Both halves, or neither."""
    text = _flat(_prompt(True))
    assert "WHAT'S ON YOUR PLATE" in text
    assert "does not need a forward hook" in text


def test_on_the_prompt_still_forbids_inventing_the_silence_duration():
    """Cubie may only say how long it has been by reading thread_idle_days —
    a number inferred from a date in a message body is a fabrication."""
    assert "thread_idle_days" in _prompt(True)


def test_on_the_prompt_does_not_reopen_already_answered_threads():
    """Rule 4's closing clause is load-bearing: a thread the reader ANSWERED is
    handled. Loosening the silence rule must not drag it back in."""
    text = _flat(_prompt(True))
    assert "is still handled" in text and "is still left out" in text


# ── The gate itself ──────────────────────────────────────────────────────────


def _row(email="ae@acme.example", department="Sales", is_exec=False):
    return SimpleNamespace(
        email=email,
        department=department,
        managed_departments=None,
        is_exec=is_exec,
        roles=[SimpleNamespace(name="user")],
    )


def test_the_gate_ADMITS_an_onboarded_reader():
    """The test that actually proves the wiring. A denial-only test passes even
    when a bare email string is being handed to feature_allows, which reads no
    department and silently denies every user in the nightly job."""
    from rubik_core.features import feature_allows

    assert feature_allows("open_commitments", _row()) is True


def _narrow_to_sales(monkeypatch) -> None:
    """The card ships open to everyone; a case about a department LEFT OUT
    installs the roster an admin would narrow it to (Sales, Exec flag on), as
    Feature Access data."""
    from backend.tests._rosters import pin_rosters
    from rubik_core.features import FeaturePolicy

    pin_rosters(
        monkeypatch,
        {
            "open_commitments": FeaturePolicy(
                departments=frozenset({"sales"}), exec_allowed=True
            )
        },
    )


def test_it_ships_open_to_everyone():
    from rubik_core.features import feature_allows

    assert feature_allows("open_commitments", _row(department="Compliance")) is True


def test_a_narrowed_roster_denies_a_department_left_out(monkeypatch):
    from rubik_core.features import feature_allows

    _narrow_to_sales(monkeypatch)
    assert feature_allows("open_commitments", _row(department="Compliance")) is False


def test_an_org_wide_exec_is_admitted_by_flag():
    """An org-wide exec's department slugifies to nothing and matches no roster,
    so they reach their OWN plate by the exec flag — the customer_xray shape. The
    card is personal, never a roll-up of anyone else's work."""
    from rubik_core.features import feature_allows

    assert (
        feature_allows("open_commitments", _row(department="Exec", is_exec=True))
        is True
    )


def test_a_bare_email_string_is_the_footgun_this_gate_must_not_hit(monkeypatch):
    """Documents WHY _resolve_user_context loads the row. feature_allows
    duck-types its user: once the roster is narrowed, a string raises nothing
    and denies everyone, which in a nightly batch looks exactly like "the
    feature just never appears"."""
    from rubik_core.features import feature_allows

    _narrow_to_sales(monkeypatch)
    assert feature_allows("open_commitments", "ae@acme.example") is False


def test_the_kill_switch_denies_before_the_roster_is_ever_consulted(monkeypatch):
    """The env switch decides whether it RUNS in this environment; the registry
    decides WHO reaches it. feature_allows does not consult the kill switch, so
    the orchestrator checks it first — and ships OFF."""
    from rubik_core.config import settings
    from rubik_core.priorities import orchestrator as orch

    monkeypatch.setattr(settings, "OPEN_COMMITMENTS_ENABLED", False, raising=False)
    monkeypatch.setattr(orch, "_resolve_user_department", lambda _e: "Sales")
    dept, allowed = orch._resolve_user_context("ae@acme.example")
    assert dept == "Sales"
    assert allowed is False


def test_it_ships_switched_off():
    from rubik_core.config import Settings

    assert Settings.model_fields["OPEN_COMMITMENTS_ENABLED"].default is False


def test_a_gate_that_cannot_read_denies_rather_than_raising(monkeypatch):
    """A denial is SILENCE — the reader still gets their normal briefing. It
    must never surface as an error: an all-errored run is treated as poisoned
    upstream, which would push a not-onboarded user toward a FAILED bundle."""
    from rubik_core.config import settings
    from rubik_core.priorities import orchestrator as orch

    monkeypatch.setattr(settings, "OPEN_COMMITMENTS_ENABLED", True, raising=False)
    monkeypatch.setattr(orch, "_resolve_user_department", lambda _e: "Sales")

    def _boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr("rubik_core.database.db.SessionLocal", _boom)
    dept, allowed = orch._resolve_user_context("ae@acme.example")
    assert dept == "Sales"
    assert allowed is False
