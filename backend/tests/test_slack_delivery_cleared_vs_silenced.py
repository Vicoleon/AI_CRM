"""A card emptied on read: "they handled it today" vs "the filter is eating it".

Regression. The Friday afternoon Slack job posts no body, so it asks for the
DAILY report and re-reads that morning's card. By then a reader had handled
every item on it, the read path hid them all, and the ops alert said
"Silenced by filter … a resolved EOS To-Do keeps getting re-proposed … check
priority_memory" — a stale-row diagnosis for a person who had simply cleared
their list. Meanwhile everyone else got the morning briefing twice, and nothing
said so.

What this file pins:

1. **When the hiding row was written decides the bucket.** Rows written after
   the card was built → ``users_cleared_since_briefing`` (FYI, no alarm). Rows
   that already existed → ``users_silenced_by_filter`` (alarm), with the rows
   named. Nothing on record → silenced and called unexplained, never "handled".
2. **The alert names rows by id and time only**, never a priority title.
3. **One briefing per person per day.** A scheduled daily run skips anyone who
   already had a DM on their local today, and says a second daily run fired. A
   targeted re-send and the look-ahead report are never skipped.
4. **The read path and the alert share one implementation** of the memory and
   deal-defer rules (``home._memory_filter`` / ``home._deal_defer_filter``).

Collaborators are faked by STRING target or on the imported module; nothing here
touches the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import backend.agents.slack_delivery as sd
from backend.routers import home
from rubik_core.priorities import memory as priority_memory
from rubik_core.priorities.memory import MemoryRow

#: Every ``@acme.example`` colleague here is a roster member, so this module
#: opts into the one fixture carve-out (``conftest.roster_admits_acme_example``).
pytestmark = pytest.mark.usefixtures("roster_admits_acme_example")


@pytest.fixture(autouse=True)
def _workspace_domain(set_test_workspace_domains):
    set_test_workspace_domains("acme.example")


BUILT = datetime(2026, 10, 16, 5, 24)  # the card's generated_at, naive UTC
USER = "ceo@acme.example"
OPS = "ops@acme.example"


class _FakeBot:
    is_available = True

    def __init__(self):
        self.dms: list[tuple[str, str]] = []

    def send_dm(self, email, text=None, blocks=None):
        self.dms.append((email, text))
        return "ts-1"


class _FakeRegistry:
    def get_user(self, email):
        return {"email": email}


def _drop(cause="resolved", *, at=None, eid="todo-1", by=USER, kind="todo"):
    return home.ReadFilterDrop(
        entity_kind=kind,
        entity_id=eid,
        priority_kind="eos_todo_due",
        cause=cause,
        changed_at=at,
        changed_by=by,
    )


def _item():
    return sd._BundlePriority(title="T", severity="info", body="b", link=None)


def _common(monkeypatch, bot, *, items_by_email, stale=None):
    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(
        sd,
        "_load_bundle_priority_items",
        lambda email, **_k: items_by_email.get(email, []),
    )
    monkeypatch.setattr(sd, "deactivated_priorities_emails", lambda *a, **k: set())
    monkeypatch.setattr(sd, "_bundle_pto_for", lambda email: None)
    monkeypatch.setattr(sd, "_bundle_hygiene_for", lambda email: None)
    monkeypatch.setattr(sd, "_bundle_generated_at_for", lambda email: None)
    monkeypatch.setattr(sd, "_record_delivery", lambda email, profile: None)
    monkeypatch.setattr(sd, "_today_bundle_status", lambda email: (True, "ok", 3))
    monkeypatch.setattr(sd, "_rows_predating_card", lambda email: stale)
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: [OPS])
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)


def _targeted(monkeypatch, bot, *, items_by_email, stale=None):
    _common(monkeypatch, bot, items_by_email=items_by_email, stale=stale)
    return sd.deliver_slack_priorities(MagicMock(), emails=list(items_by_email))


def _scheduled(monkeypatch, bot, *, items_by_email, already, report="daily"):
    """A scheduler-shaped run (no ``emails``): recipients come from ``users``."""
    _common(monkeypatch, bot, items_by_email=items_by_email)
    monkeypatch.setattr(sd, "_enabled_departments", lambda db: set())
    monkeypatch.setattr(sd, "_today_card_present", lambda email: True)
    monkeypatch.setattr(sd, "_next_week_card_present", lambda email: True)
    monkeypatch.setattr(sd, "_next_week_window_for", lambda email: (None, None))
    seen: list[list[str]] = []

    def _already(emails):
        seen.append(list(emails))
        return set(already)

    monkeypatch.setattr(sd, "_already_delivered_today", _already)
    db = MagicMock()
    # No department is opted in, so the recipients are the Exec team.
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        SimpleNamespace(
            email=e, is_exec=True, department=None, is_platform_support=False
        )
        for e in items_by_email
    ]
    result = sd.deliver_slack_priorities(db, report=report)
    return result, seen


def _ops_texts(bot):
    return [t for (e, t) in bot.dms if e == OPS]


# ── 1. classification ────────────────────────────────────────────────────────


def test_a_card_cleared_after_it_was_built_is_fyi_not_an_alarm(monkeypatch):
    """The cleared case: every hiding row was written today, after the card."""
    bot = _FakeBot()
    result = _targeted(monkeypatch, bot, items_by_email={USER: []}, stale=[])

    assert result.users_cleared_since_briefing == [USER]
    assert result.users_silenced_by_filter == []
    assert _ops_texts(bot) == [], "a cleared list must not page ops"


def test_a_row_that_predates_the_card_alarms_and_is_named(monkeypatch):
    bot = _FakeBot()
    stale = [_drop("resolved", at=BUILT - timedelta(days=2), eid="769abf3d")]
    result = _targeted(monkeypatch, bot, items_by_email={USER: []}, stale=stale)

    assert result.users_silenced_by_filter == [USER]
    assert result.silenced_rows[USER] == stale
    text = _ops_texts(bot)[0]
    assert "hidden on read" in text
    assert "resolved todo `769abf3d` (eos_todo_due), 2026-10-14 05:24 UTC" in text
    assert f"by {USER}" in text
    assert "Recover:" in text


def test_an_unattributed_drop_alarms_as_unexplained(monkeypatch):
    bot = _FakeBot()
    result = _targeted(monkeypatch, bot, items_by_email={USER: []}, stale=None)

    assert result.users_silenced_by_filter == [USER]
    assert USER not in result.silenced_rows
    assert "nothing on record accounts for it" in _ops_texts(bot)[0]


def test_a_cleared_user_is_listed_as_context_when_the_alert_fires_anyway(
    monkeypatch,
):
    bot = _FakeBot()
    _common(monkeypatch, bot, items_by_email={USER: [], "x@acme.example": []})
    monkeypatch.setattr(
        sd,
        "_rows_predating_card",
        lambda email: [] if email == USER else [_drop(at=BUILT - timedelta(hours=1))],
    )
    sd.deliver_slack_priorities(MagicMock(), emails=[USER, "x@acme.example"])

    text = _ops_texts(bot)[0]
    assert "handled everything since the briefing" in text
    assert USER in text.split("handled everything since the briefing")[1]


def test_the_alert_never_carries_a_priority_title(monkeypatch):
    bot = _FakeBot()
    _targeted(
        monkeypatch,
        bot,
        items_by_email={USER: []},
        stale=[_drop(at=BUILT - timedelta(days=1))],
    )
    # ReadFilterDrop has no title field at all — the type is the guarantee.
    assert "title" not in home.ReadFilterDrop.__dataclass_fields__
    assert _ops_texts(bot)


# ── _rows_predating_card ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("explained", "expected"),
    [
        # every row written after the card → handled today
        ((BUILT, [_drop(at=BUILT + timedelta(hours=9))]), []),
        # one row older than the card → that one is reported
        (
            (
                BUILT,
                [
                    _drop(at=BUILT + timedelta(hours=9), eid="new"),
                    _drop(at=BUILT - timedelta(days=3), eid="old"),
                ],
            ),
            ["old"],
        ),
        # a row with no time can't be cleared-today: treat as predating
        ((BUILT, [_drop(at=None, eid="untimed")]), ["untimed"]),
        # a card that never said when it was built: every row predates it
        ((None, [_drop(at=BUILT, eid="any")]), ["any"]),
        # no card, or a card emptied with nothing to show for it → unexplained
        (None, None),
        ((BUILT, []), None),
    ],
)
def test_rows_predating_card(monkeypatch, explained, expected):
    monkeypatch.setattr(
        "backend.routers.home.today_read_filter_drops", lambda email: explained
    )
    got = sd._rows_predating_card(USER)
    assert (None if got is None else [d.entity_id for d in got]) == expected


def test_rows_predating_card_is_unexplained_when_the_lookup_fails(monkeypatch):
    def _boom(email):
        raise RuntimeError("db down")

    monkeypatch.setattr("backend.routers.home.today_read_filter_drops", _boom)
    assert sd._rows_predating_card(USER) is None


# ── 3. one briefing per person per day ───────────────────────────────────────


def test_a_second_scheduled_daily_run_skips_and_says_so(monkeypatch):
    """The bodyless Friday job: nobody gets the morning briefing twice."""
    bot = _FakeBot()
    result, seen = _scheduled(
        monkeypatch,
        bot,
        items_by_email={USER: [_item()], "rep@acme.example": [_item()]},
        already={USER, "rep@acme.example"},
    )

    assert seen, "a scheduled daily run must check who already had a DM"
    assert sorted(result.users_already_delivered) == [USER, "rep@acme.example"]
    assert result.users_delivered == 0
    assert [e for (e, _) in bot.dms if e != OPS] == []
    text = _ops_texts(bot)[0]
    assert "Already sent today: 2" in text
    assert "second daily run" in text
    assert '{"report":"next_week"}' in text
    assert "Recover:" not in text  # a job fix, not a regenerate + re-send


def test_only_people_already_sent_are_skipped(monkeypatch):
    bot = _FakeBot()
    result, _ = _scheduled(
        monkeypatch,
        bot,
        items_by_email={USER: [_item()], "rep@acme.example": [_item()]},
        already={USER},
    )

    assert result.users_already_delivered == [USER]
    assert result.users_delivered == 1
    assert "rep@acme.example" in [e for (e, _) in bot.dms]


def test_the_lookahead_report_is_never_skipped(monkeypatch):
    bot = _FakeBot()
    result, seen = _scheduled(
        monkeypatch,
        bot,
        items_by_email={USER: [_item()]},
        already={USER},
        report="next_week",
    )

    assert seen == []
    assert result.users_already_delivered == []
    assert result.users_delivered == 1


def test_a_targeted_resend_is_never_skipped(monkeypatch):
    bot = _FakeBot()
    calls: list = []
    monkeypatch.setattr(
        sd, "_already_delivered_today", lambda emails: calls.append(emails) or {USER}
    )
    result = _targeted(monkeypatch, bot, items_by_email={USER: [_item()]})

    assert calls == []
    assert result.users_delivered == 1


# ── _already_delivered_today against a real table ────────────────────────────


@pytest.fixture
def events_db(tmp_path, monkeypatch):
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database.models import Base

    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'delivered.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield dbmod
    dbmod._engine = None


def _event(dbmod, email, at, kind="delivered_slack"):
    from rubik_core.database.insights import PriorityEvent

    with dbmod.SessionLocal() as s:
        s.add(PriorityEvent(user_email=email, kind=kind, actor="system", at=at))
        s.commit()


def test_already_delivered_reads_todays_dm_rows_only(events_db):
    now = datetime.utcnow()
    _event(events_db, USER, now - timedelta(seconds=5))
    _event(events_db, "rep@acme.example", now - timedelta(hours=30))  # a day ago
    _event(events_db, "mgr@acme.example", now, kind="resolved")  # not a DM

    got = sd._already_delivered_today(
        [USER.upper(), "rep@acme.example", "mgr@acme.example"]
    )
    assert got == {USER}


def test_already_delivered_fails_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no table")

    monkeypatch.setattr("rubik_core.database.db.SessionLocal", _boom)
    assert sd._already_delivered_today([USER]) == set()


# ── 4. the read path and the alert share one rule set ────────────────────────


def _row(state, *, eid, changed, by=USER, ek="todo", pk="eos_todo_due"):
    return MemoryRow(
        id=f"m-{eid}",
        user_email=USER,
        entity_kind=ek,
        entity_id=eid,
        priority_kind=pk,
        state=state,
        snoozed_until=None,
        last_priority_id=None,
        resolved_evidence=None,
        first_seen_at="2026-10-01T00:00:00",
        last_seen_at=changed,
        last_state_change_at=changed,
        last_state_change_by=by,
    )


def _p(eid, ek="todo", pk="eos_todo_due", **extra):
    return {"id": f"p-{eid}", "entity_kind": ek, "entity_id": eid,
            "priority_kind": pk, **extra}  # fmt: skip


def test_memory_filter_reports_what_hid_each_item():
    rows = [
        _row("resolved", eid="a", changed="2026-10-16T14:00:00"),
        _row("dismissed", eid="b", changed="2026-10-16T15:00:00", by="system"),
    ]
    with patch.object(priority_memory, "load_all", return_value=rows):
        kept, drops = home._memory_filter(USER, [_p("a"), _p("b"), _p("c")], BUILT)

    assert [p["entity_id"] for p in kept] == ["c"]
    assert [(d.entity_id, d.cause, d.changed_by) for d in drops] == [
        ("a", "resolved", USER),
        ("b", "dismissed", "system"),
    ]
    assert drops[0].changed_at == datetime(2026, 10, 16, 14, 0)


def test_a_multi_entity_card_is_judged_by_its_primary_entity():
    """``record_seen`` writes the primary only, so a sibling with no row must not
    keep a finished card up, and a sibling's row alone must not hide it."""
    rows = [
        _row("resolved", eid="a", changed="2026-10-16T09:00:00"),
        _row("resolved", eid="d", changed="2026-10-16T13:00:00"),
    ]
    done = _p("a", covered_entity_ids=["a", "b"])  # b has no row
    open_card = _p("c", covered_entity_ids=["c", "d"])  # only the sibling is done
    with patch.object(priority_memory, "load_all", return_value=rows):
        kept, drops = home._memory_filter(USER, [done, open_card], BUILT)

    assert [p["entity_id"] for p in kept] == ["c"]
    assert [(d.entity_id, d.changed_at.hour) for d in drops] == [("a", 9)]


def test_today_read_filter_drops_applies_memory_then_defers(monkeypatch):
    bundle = {
        "generated_at": "2026-10-16T05:24:00+00:00",
        "priorities": [_p("a"), _p("d1", ek="deal", pk="deal_stalled")],
    }
    monkeypatch.setattr(home, "_today_daily_bundle", lambda email: ("x", bundle))
    defer = SimpleNamespace(
        deal_id="d1", created_at=datetime(2026, 10, 15, 20, 0), deferred_by=USER
    )
    monkeypatch.setattr(
        "rubik_core.sales.deal_defer.active_by_deal",
        lambda ids, reader=None: {"d1": defer} if "d1" in ids else {},
    )
    rows = [_row("resolved", eid="a", changed="2026-10-16T14:00:00")]
    with patch.object(priority_memory, "load_all", return_value=rows):
        built, drops = home.today_read_filter_drops(USER)

    assert built == BUILT
    assert [(d.entity_id, d.cause) for d in drops] == [
        ("a", "resolved"),
        ("d1", "deal_deferred"),
    ]
    assert drops[1].changed_at == datetime(2026, 10, 15, 20, 0)


def test_today_read_filter_drops_is_none_without_a_card(monkeypatch):
    monkeypatch.setattr(home, "_today_daily_bundle", lambda email: ("x", None))
    assert home.today_read_filter_drops(USER) is None


def test_the_read_path_still_hides_what_the_alert_explains(monkeypatch):
    """Same rules on both sides: _apply_priority_memory keeps what _memory_filter keeps."""
    rows = [_row("resolved", eid="a", changed="2026-10-16T14:00:00")]
    bundle = {"generated_at": "2026-10-16T05:24:00+00:00",
              "priorities": [_p("a"), _p("c")]}  # fmt: skip
    with patch.object(priority_memory, "load_all", return_value=rows):
        out = home._apply_priority_memory(USER, bundle)
    assert [p["entity_id"] for p in out["priorities"]] == ["c"]
