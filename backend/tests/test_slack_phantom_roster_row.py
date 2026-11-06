"""A roster address that isn't a Slack member must not page ops as a "gap".

The case: the morning ops alert kept naming ``blair@acme.example`` under *"No
HomeCard for their local today (generation gap)"* every single day for weeks,
telling ops to "check the priorities-batch job logs and HomeCard dates". There
is only one Blair, at ``bellison@acme.example`` — the other row is a
stale/typo'd ``User`` with no Slack account behind it. No batch rerun could
ever have fixed it, and neither offboarding source flags it: Slack's
``deleted`` and Google's ``suspended`` both describe accounts that EXIST, and
this one simply doesn't.

The third de-noising of this alert (after the offboarded-employee and
filtered-empty cases). Pinned here: such an address is reported as
roster hygiene with the actual fix, is kept out of the alertable bucket, and —
critically — a Slack sweep that comes back empty or partial must NEVER cause a
real user to be labelled a phantom.
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

    def __init__(self, members=None, raises=False):
        self._members = members
        self._raises = raises
        self.dms: list[tuple[str, str]] = []

    def member_emails(self):
        if self._raises:
            raise RuntimeError("users.list exploded")
        return set(self._members or set())

    def send_dm(self, email, text=None, blocks=None):
        self.dms.append((email, text))
        return "ts-1"


class _FakeRegistry:
    def get_user(self, email):
        return {"email": email}


REAL = "bellison@acme.example"
PHANTOM = "blair@acme.example"


def _run(monkeypatch, bot, *, items_by_email, ops=("ops@acme.example",)):
    """Targeted delivery run where every named recipient has NO card."""
    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(
        sd,
        "_load_bundle_priority_items",
        lambda email, **_k: items_by_email.get(email, []),
    )
    monkeypatch.setattr(sd, "deactivated_priorities_emails", lambda *a, **k: set())
    monkeypatch.setattr(sd, "_bundle_pto_for", lambda email: None)
    monkeypatch.setattr(sd, "_bundle_hygiene_for", lambda email: None)
    # No card at all → every empty recipient lands in users_missing_card first.
    monkeypatch.setattr(sd, "_today_bundle_status", lambda email: (False, None, 0))
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: list(ops))
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)
    return sd.deliver_slack_priorities(MagicMock(), emails=list(items_by_email.keys()))


# ── _not_workspace_members ───────────────────────────────────────────────────


def test_non_member_is_identified():
    bot = _FakeBot(members={REAL, "ebrooks@acme.example"})

    assert sd._not_workspace_members(bot, [REAL, PHANTOM]) == {PHANTOM}


def test_membership_check_is_case_insensitive():
    bot = _FakeBot(members={REAL})

    assert sd._not_workspace_members(bot, ["BEllison@ACME.example"]) == set()


def test_empty_sweep_never_labels_anyone_a_phantom():
    """An empty roster means "unknown", not "nobody exists".

    Treating absence as proof here would mislabel the whole company the moment
    the bot token or the ``users:read`` scope lapsed.
    """
    bot = _FakeBot(members=set())

    assert sd._not_workspace_members(bot, [REAL, PHANTOM]) == set()


def test_failed_sweep_never_labels_anyone_a_phantom():
    bot = _FakeBot(raises=True)

    assert sd._not_workspace_members(bot, [REAL, PHANTOM]) == set()


def test_no_candidates_short_circuits():
    bot = _FakeBot(raises=True)  # would blow up if it were consulted

    assert sd._not_workspace_members(bot, []) == set()


# ── delivery classification ──────────────────────────────────────────────────


def test_phantom_row_is_reclassified_out_of_the_alertable_bucket(monkeypatch):
    """The exact production case: one real user with a genuine gap, one phantom."""
    bot = _FakeBot(members={REAL})
    result = _run(monkeypatch, bot, items_by_email={REAL: [], PHANTOM: []})

    assert result.users_no_slack_account == [PHANTOM]
    assert result.users_missing_card == [REAL]  # the real gap still alerts
    # The union keeps both, for back-compat with the endpoint response.
    assert sorted(result.users_skipped_empty) == sorted([REAL, PHANTOM])


def test_phantom_alone_does_not_trip_the_alarm(monkeypatch):
    """A stale roster row is data hygiene, not an outage — it must not page.

    This is the whole point: before the fix, this case DM'd ops a "generation
    gap" every morning for weeks.
    """
    bot = _FakeBot(members={REAL})
    result = _run(monkeypatch, bot, items_by_email={PHANTOM: []})

    assert result.users_no_slack_account == [PHANTOM]
    assert result.users_missing_card == []
    assert bot.dms == []  # nothing sent — not to the phantom, not to ops


def test_real_gap_still_alerts_with_the_phantom_as_context(monkeypatch):
    bot = _FakeBot(members={REAL})
    _run(monkeypatch, bot, items_by_email={REAL: [], PHANTOM: []})

    ops_dms = [(e, t) for (e, t) in bot.dms if e == "ops@acme.example"]
    assert len(ops_dms) == 1
    text = ops_dms[0][1]
    # The real user is still diagnosed as a generation gap...
    assert "No HomeCard for their local today" in text
    assert REAL in text
    # ...while the phantom is explicitly NOT one, and carries the actual fix.
    assert "roster hygiene, NOT a generation gap" in text
    assert PHANTOM in text
    assert "Admin → Users" in text


def test_unknown_membership_keeps_the_generation_gap_wording(monkeypatch):
    """With no usable Slack roster, every missing card stays a reported gap.

    Failing closed here would silently stop alerting on real outages.
    """
    bot = _FakeBot(raises=True)
    result = _run(monkeypatch, bot, items_by_email={REAL: [], PHANTOM: []})

    assert result.users_no_slack_account == []
    assert sorted(result.users_missing_card) == sorted([REAL, PHANTOM])
    text = [t for (e, t) in bot.dms if e == "ops@acme.example"][0]
    assert "No HomeCard for their local today" in text
    assert "roster hygiene" not in text
