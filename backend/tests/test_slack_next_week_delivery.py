"""The Friday next-week look-ahead DM must not be a re-send of the daily one.

Regression. Two Slack deliveries run every Friday — the weekday
morning briefing (04:30 ET) and a second run right after the
``priorities-next-week`` generation job (15:00 ET) — but delivery had no notion
of *which* report it was sending. Both calls went through
``home._load_today_priorities``, which merges the visible look-ahead into the
daily bundle for the web pane, and ``_priority_blocks`` then sliced
``items[:top_n]``. Daily generation caps at 5 and ``top_n`` is 5, so the
afternoon DM was a verbatim re-send of the morning's with the look-ahead
collapsed into "+N more in Cubie" — under a header that still said "your top
priorities for today".

Observed in production: a Friday pair where the afternoon DM repeats all three
of the morning's items in the same order and appends one look-ahead item; and
a report that the two priorities that surfaced were the same when they should
have been next week's. A user only ever saw a clean look-ahead when their
daily card was empty (on PTO, say).

What is pinned here: the daily DM excludes merged look-ahead items, the
next-week DM reads the look-ahead card on its OWN (never the merged bundle), and
its header names the target week.
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
        self.dms: list[tuple[str, str, list | None]] = []  # (email, text, blocks)

    def send_dm(self, email, text=None, blocks=None):
        self.dms.append((email, text, blocks))
        return "ts-123"


class _FakeRegistry:
    def get_user(self, email):
        return {"email": email}


def _p(title: str, *, origin: str = "priority") -> dict:
    """A bundle-shaped priority dict as the read path projects it."""
    return {
        "title": title,
        "why_it_matters": f"why {title}",
        "suggested_action": f"do {title}",
        "urgency": "high",
        "source_links": [],
        "origin": origin,
    }


# ── item loading: which card each report reads ────────────────────────────────


def test_daily_report_drops_merged_next_week_items(monkeypatch):
    """The morning DM must carry only today's items.

    The read path merges look-ahead items in for the web pane; leaving them in
    the DM is what let the look-ahead pad (and on a full card, get pushed out
    of) the morning briefing.
    """
    import backend.routers.home as home

    monkeypatch.setattr(
        home,
        "_load_today_priorities",
        lambda email: {
            "priorities": [
                _p("Daily one"),
                _p("Daily two"),
                _p("Look-ahead", origin="next_week"),
            ]
        },
    )

    items = sd._load_bundle_priority_items("bellison@acme.example")

    assert [i.title for i in items] == ["Daily one", "Daily two"]


def test_next_week_report_reads_the_lookahead_card_not_the_daily_bundle(monkeypatch):
    """The Friday DM must read the look-ahead card directly.

    Going through the merged accessor is the bug: it returns the daily items
    first, so the DM re-sent the morning briefing. ``_load_today_priorities``
    raises here to prove the next-week path never touches it — which also means
    the path skips ``_merge_next_week``'s signal-id dedup, so Monday/Tuesday
    items that Friday's 72h daily lookahead also covers still reach the DM.
    """
    import backend.routers.home as home

    def _boom(email):
        raise AssertionError("the next-week DM must not read the daily bundle")

    monkeypatch.setattr(home, "_load_today_priorities", _boom)
    monkeypatch.setattr(
        home,
        "load_next_week_priorities",
        lambda email: {
            "week_start": "2026-08-17",
            "week_end": "2026-08-23",
            "priorities": [_p("Monday prospect call"), _p("Wednesday deadline")],
        },
    )

    items = sd._load_bundle_priority_items("bellison@acme.example", report="next_week")

    assert [i.title for i in items] == ["Monday prospect call", "Wednesday deadline"]


def test_next_week_report_with_no_visible_card_sends_nothing(monkeypatch):
    import backend.routers.home as home

    monkeypatch.setattr(home, "load_next_week_priorities", lambda email: None)

    assert sd._load_bundle_priority_items("a@x.example", report="next_week") == []


# ── header: the DM must announce the week, never "today" ──────────────────────


def test_next_week_header_names_the_target_week():
    header = sd._next_week_header("2026-08-17", "2026-08-23")

    assert header == "The week ahead — Mon Aug 17 to Sun Aug 23"
    assert "today" not in header.lower()


@pytest.mark.parametrize("start,end", [(None, None), ("", ""), ("not-a-date", "x")])
def test_next_week_header_falls_back_without_a_usable_window(start, end):
    """A missing/garbled window must still produce a look-ahead header — the one
    thing it may never do is fall back to the daily "for today" wording."""
    header = sd._next_week_header(start, end)

    assert header == "The week ahead — your next-week look-ahead"
    assert "today" not in header.lower()


def test_priority_blocks_header_override_replaces_the_daily_line():
    items = [sd._BundlePriority(title="T", severity="info", body="b", link=None)]

    fallback, blocks = sd._priority_blocks(
        items, top_n=5, web_url="", header="The week ahead — Mon Aug 17 to Sun Aug 23"
    )

    assert blocks[0]["text"]["text"] == "The week ahead — Mon Aug 17 to Sun Aug 23"
    assert "priorities for today" not in fallback


def test_priority_blocks_without_override_keeps_the_daily_header():
    items = [sd._BundlePriority(title="T", severity="info", body="b", link=None)]

    _fallback, blocks = sd._priority_blocks(
        items, top_n=5, web_url="", greeting="Good morning"
    )

    assert blocks[0]["text"]["text"] == "Good morning — your top priorities for today"


# ── end-to-end delivery ──────────────────────────────────────────────────────


def _run(monkeypatch, bot, *, report, items_by_email, pto=None, hygiene=None):
    """Targeted deliver_slack_priorities run with every collaborator faked."""
    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(
        sd,
        "_load_bundle_priority_items",
        lambda email, report=sd.REPORT_KIND_DAILY: items_by_email.get(
            (email, report), []
        ),
    )
    monkeypatch.setattr(sd, "deactivated_priorities_emails", lambda *a, **k: set())
    monkeypatch.setattr(sd, "_bundle_pto_for", lambda email: pto)
    monkeypatch.setattr(sd, "_bundle_hygiene_for", lambda email: hygiene)
    monkeypatch.setattr(sd, "_today_bundle_status", lambda email: (True, "ok", 0))
    monkeypatch.setattr(sd, "_next_week_bundle_status", lambda email: (True, "ok", 0))
    monkeypatch.setattr(
        sd, "_next_week_window_for", lambda email: ("2026-08-17", "2026-08-23")
    )
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: [])
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)
    emails = sorted({email for (email, _r) in items_by_email})
    return sd.deliver_slack_priorities(MagicMock(), emails=emails, report=report)


def _item(title: str) -> sd._BundlePriority:
    return sd._BundlePriority(title=title, severity="urgent", body="b", link=None)


def test_next_week_run_delivers_the_lookahead_under_a_week_header(monkeypatch):
    bot = _FakeBot()
    email = "bellison@acme.example"

    result = _run(
        monkeypatch,
        bot,
        report="next_week",
        items_by_email={
            (email, "next_week"): [_item("Monday prospect call")],
            # What the morning DM sent. Nothing here may reach the look-ahead DM.
            (email, "daily"): [_item("Today's 1:1")],
        },
    )

    assert result.report == "next_week"
    assert result.users_delivered == 1
    (_to, fallback, blocks) = bot.dms[0]
    assert blocks[0]["text"]["text"] == "The week ahead — Mon Aug 17 to Sun Aug 23"
    assert "Monday prospect call" in fallback
    assert "Today's 1:1" not in fallback  # never a re-send of the morning DM


def test_next_week_run_skips_the_pto_note_and_the_invite_line(monkeypatch):
    """Both extras are about TODAY.

    A PTO note answers "are you working today", which says nothing about next
    week — and it is not the right reply to an empty look-ahead. The unanswered
    invite line is likewise today's, so repeating it would just re-tail the
    morning DM.
    """
    bot = _FakeBot()
    email = "bellison@acme.example"

    result = _run(
        monkeypatch,
        bot,
        report="next_week",
        items_by_email={(email, "next_week"): []},
        pto={"on_pto": True, "return_date": "2026-08-24"},
        hygiene={"unanswered_count": 3, "invites": [{"title": "Some invite"}]},
    )

    assert bot.dms == []  # no PTO note on a look-ahead run
    assert result.users_on_pto == []
    assert result.users_empty_no_signals == [email]


def test_next_week_run_still_sends_the_pto_users_lookahead(monkeypatch):
    """Being off this Friday must not suppress next week's look-ahead.

    This is the one case that accidentally worked before the fix: an empty
    daily card left only look-ahead items behind. It has
    to keep working now that the reports are separate.
    """
    bot = _FakeBot()
    email = "bellison@acme.example"

    result = _run(
        monkeypatch,
        bot,
        report="next_week",
        items_by_email={(email, "next_week"): [_item("Monday compliance review")]},
        pto={"on_pto": True, "return_date": "2026-08-24"},
        hygiene={"unanswered_count": 2, "invites": []},
    )

    assert result.users_delivered == 1
    (_to, fallback, blocks) = bot.dms[0]
    assert "Monday compliance review" in fallback
    assert "haven't answered" not in fallback  # today's invite line stays out


def test_daily_run_is_unchanged(monkeypatch):
    bot = _FakeBot()
    email = "bellison@acme.example"

    result = _run(
        monkeypatch,
        bot,
        report="daily",
        items_by_email={(email, "daily"): [_item("Today's 1:1")]},
    )

    assert result.report == "daily"
    assert result.users_delivered == 1
    (_to, fallback, blocks) = bot.dms[0]
    assert "your top priorities for today" in blocks[0]["text"]["text"]
    assert "Today's 1:1" in fallback


def test_unknown_report_falls_back_to_the_daily_briefing(monkeypatch):
    """A typo'd scheduler body must not cost anyone their briefing."""
    bot = _FakeBot()
    email = "bellison@acme.example"

    result = _run(
        monkeypatch,
        bot,
        report="nextweek",  # not a real report kind
        items_by_email={(email, "daily"): [_item("Today's 1:1")]},
    )

    assert result.report == "daily"
    assert result.users_delivered == 1


# ── ops alert wording ────────────────────────────────────────────────────────


def test_ops_alert_for_a_next_week_run_blames_the_right_job(monkeypatch):
    """A missing look-ahead card points at priorities-next-week, not the morning
    batch — the alert must not send ops chasing the wrong job."""
    bot = _FakeBot()
    result = sd.SlackDeliveryResult(report="next_week")
    result.users_missing_card = ["ebrooks@acme.example"]
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: ["ops@acme.example"])

    sd._alert_ops_delivery_failures(MagicMock(), bot, result)

    text = bot.dms[0][1]
    assert "next-week look-ahead — delivery problems" in text
    assert "priorities-next-week" in text
    assert "next_week_report" in text
    assert '"report": "next_week"' in text  # correct re-send recipe


def test_ops_alert_for_a_daily_run_keeps_its_wording(monkeypatch):
    bot = _FakeBot()
    result = sd.SlackDeliveryResult()
    result.users_missing_card = ["ebrooks@acme.example"]
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: ["ops@acme.example"])

    sd._alert_ops_delivery_failures(MagicMock(), bot, result)

    text = bot.dms[0][1]
    assert "morning priorities — delivery problems" in text
    assert "No HomeCard for their local today" in text
    assert "run-all" in text
