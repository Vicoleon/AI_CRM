"""The morning-delivery ops alert: a recipient with NO card for their local
today (a real generation gap) must page the Systems team — never vanish.

Regression: nearly every user's bundle was keyed to the wrong local date
(LA timezone fallback), `deliver_slack_priorities` skipped them all via
`if not items: continue`, and nobody was told anything.

Follow-up: the alert must NOT fire the same "no bundle / timezone
bug" text for a recipient whose card simply had nothing in it — that's a
legitimate quiet day, and conflating it with a generation gap is what made an
offboarded employee look like a recurring bug. Those cases are covered in
test_deactivated_user_gate.py; here we pin the genuine-failure path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import backend.agents.slack_delivery as sd

#: Every ``@acme.example`` colleague here is a roster member, so this module
#: opts into the one fixture carve-out (``conftest.roster_admits_acme_example``).
pytestmark = pytest.mark.usefixtures("roster_admits_acme_example")


class _FakeBot:
    is_available = True

    def __init__(self):
        self.dms: list[tuple[str, str]] = []  # (email, text)

    def send_dm(self, email, text=None, blocks=None):
        self.dms.append((email, text))
        return "ts-123"


class _FakeRegistry:
    def get_user(self, email):
        return {"email": email}


def _run_delivery(
    monkeypatch,
    bot,
    *,
    items_by_email,
    ops=("ops@acme.example",),
    bundle_status=None,
):
    """Targeted deliver_slack_priorities run with every collaborator faked.

    Empty recipients are classified as MISSING (no card) by default, so the
    genuine-failure alert fires — matching this file's regression intent.
    ``bundle_status`` overrides that with a ``(present, status, stored_count)``
    tuple, for the cases where a card DOES exist.
    """
    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(
        sd,
        "_load_bundle_priority_items",
        lambda email, **_k: items_by_email.get(email, []),
    )
    # No deactivated users; keep the detection path off the network.
    monkeypatch.setattr(sd, "deactivated_priorities_emails", lambda *a, **k: set())
    monkeypatch.setattr(sd, "_bundle_pto_for", lambda email: None)  # no PTO, no DB
    # Default: an empty recipient has no card at all → a real generation gap.
    monkeypatch.setattr(
        sd, "_today_bundle_status", lambda email: bundle_status or (False, None, 0)
    )
    # An emptied card is unattributed unless a test says otherwise (no DB here).
    monkeypatch.setattr(sd, "_rows_predating_card", lambda email: None)
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: list(ops))
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)
    return sd.deliver_slack_priorities(MagicMock(), emails=list(items_by_email.keys()))


def _item():
    return sd._BundlePriority(title="T", severity="info", body="b", link=None)


def test_missing_card_recipients_are_named_and_ops_alerted(monkeypatch):
    bot = _FakeBot()
    result = _run_delivery(
        monkeypatch,
        bot,
        items_by_email={
            "ebrooks@acme.example": [],
            "sales1@acme.example": [],
            "bellison@acme.example": [_item()],
        },
    )

    assert sorted(result.users_skipped_empty) == [
        "ebrooks@acme.example",
        "sales1@acme.example",
    ]
    # Both had NO card → they land in the alertable missing-card bucket.
    assert sorted(result.users_missing_card) == [
        "ebrooks@acme.example",
        "sales1@acme.example",
    ]
    assert result.users_empty_no_signals == []
    assert result.users_delivered == 1

    ops_dms = [(e, t) for (e, t) in bot.dms if e == "ops@acme.example"]
    assert len(ops_dms) == 1
    text = ops_dms[0][1]
    assert "ebrooks@acme.example" in text
    assert "sales1@acme.example" in text
    assert "deliver-slack" in text  # recovery recipe included


def test_card_with_items_fully_filtered_alerts_as_silenced(monkeypatch):
    """A card that HAS items but delivers none must alert — not read as quiet.

    Regression: a user's card held 1 priority keyed to an EOS To-Do they had
    resolved, the read-path UC-25 filter dropped it, and delivery saw an
    empty list. `generation_status` was `ok`, so this looked exactly like a
    legitimate quiet day and no alert fired for 8+ days.
    """
    bot = _FakeBot()
    result = _run_delivery(
        monkeypatch,
        bot,
        items_by_email={"a@acme.example": []},
        # card present, generation succeeded, 3 items stored, 0 delivered
        bundle_status=(True, "ok", 3),
    )

    assert result.users_silenced_by_filter == ["a@acme.example"]
    assert result.users_missing_card == []
    assert result.users_empty_no_signals == []
    assert result.users_skipped_empty == ["a@acme.example"]

    ops_texts = [t for (e, t) in bot.dms if e == "ops@acme.example"]
    assert ops_texts, "ops must be alerted when every item is filtered out"
    assert "hidden on read" in ops_texts[0]
    assert "a@acme.example" in ops_texts[0]


def test_empty_card_quiet_day_does_not_alert(monkeypatch):
    """The counterpart: a card written empty (0 stored) stays a quiet day."""
    bot = _FakeBot()
    result = _run_delivery(
        monkeypatch,
        bot,
        items_by_email={"a@acme.example": []},
        bundle_status=(True, "no_signals", 0),
    )

    assert result.users_empty_no_signals == ["a@acme.example"]
    assert result.users_silenced_by_filter == []
    assert result.users_missing_card == []
    assert all(e != "ops@acme.example" for (e, _) in bot.dms)


def test_no_ops_alert_when_everyone_delivered(monkeypatch):
    bot = _FakeBot()
    result = _run_delivery(
        monkeypatch,
        bot,
        items_by_email={
            "a@acme.example": [_item()],
            "b@acme.example": [_item()],
        },
    )

    assert result.users_skipped_empty == []
    assert result.users_delivered == 2
    assert all(e != "ops@acme.example" for (e, _) in bot.dms)


def test_ops_alert_failure_never_breaks_delivery(monkeypatch):
    bot = _FakeBot()

    def _boom(db):
        raise RuntimeError("ops lookup exploded")

    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(sd, "_load_bundle_priority_items", lambda email, **_k: [])
    monkeypatch.setattr(sd, "deactivated_priorities_emails", lambda *a, **k: set())
    monkeypatch.setattr(sd, "_bundle_pto_for", lambda email: None)
    monkeypatch.setattr(sd, "_today_bundle_status", lambda email: (False, None, 0))
    monkeypatch.setattr(sd, "_ops_recipients", _boom)
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)

    result = sd.deliver_slack_priorities(MagicMock(), emails=["x@acme.example"])
    assert result.users_skipped_empty == ["x@acme.example"]  # loop finished
    assert result.users_missing_card == ["x@acme.example"]
