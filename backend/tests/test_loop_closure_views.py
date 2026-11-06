"""Loose Ends — the read models (``rubik_core/loop_closure/views.py``) and the roster.

Pins the JSON shapes of README §5 key for key, and the privacy rule that makes
the team view safe to show a leader: no title, headline, subject or snippet can
appear in it. That is asserted the blunt way — seed every owner-only column
with a unique marker string and search the serialized team JSON for it — so a
future field cannot leak through a path nobody thought to check.

Also: people A–Z with items oldest first (no ranking), the age chip, the
"this week" boundary on the org clock, the noise rate beside its denominator,
impersonation hiding content on the AE's own page, and who the roster admits.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.config import settings
from rubik_core.database.loop_closure import LoopItem, LoopMute, LoopScanRun
from rubik_core.features.policy import clear_cache
from rubik_core.loop_closure import views
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 12, 10)  # naive UTC: Thu 07:10 Chicago
AMY = "amy@acme.example"
ZED = "zed@acme.example"
MIA = "mia@acme.example"

SECRETS = (
    "SECRET-SUBJECT-1",
    "SECRET-SUBJECT-2",
    "AI-HEADLINE-3",
    "SLACK-TEXT-4",
    "TASK-SUBJECT-5",
)

MINE_KEYS = {
    "scanned_at",
    "coverage",
    "notes",
    "totals",
    "groups",
    "thresholds",
    "mutes",
    "content_hidden",
}
TEAM_KEYS = {"scanned_at", "people", "roster_size", "health", "scorecard", "thresholds"}
PERSON_KEYS = {
    "email",
    "name",
    "open",
    "by_kind",
    "aging",
    "over_escalation",
    "closed_this_week",
    "coverage",
    "items",
}
TEAM_ITEM_KEYS = {"id", "kind", "who", "days_open", "age"}
HEALTH_KEYS = {
    "window_days",
    "flagged",
    "dismissed",
    "noise_rate",
    "dismissed_by_reason",
    "closed",
    "closed_by_reason",
    "median_days_to_close",
    "weekly",
}


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base, User

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr(
        "rubik_core.sales.rhythm_checks.get_hubspot_config",
        lambda: HubSpotConnectorConfig(portal_id="9100001"),
    )
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    # Loose Ends ships open to everyone. These tests run the configuration an
    # admin sets when it is for the sales team only — the Sales department on
    # the roster — as Feature Access data.
    from backend.tests._rosters import pin_rosters
    from rubik_core.features import FeaturePolicy

    pin_rosters(
        monkeypatch,
        {
            "loop_closure": FeaturePolicy(
                departments=frozenset({"sales"}),
                exec_allowed=False,
                admin_allowed=False,
            )
        },
    )
    clear_cache()
    session = Session()
    for email, name in ((AMY, "Amy Adams"), (ZED, "Zed Zulu"), (MIA, "Mia Moss")):
        session.add(User(email=email, id=email[:3], name=name, department="Sales"))
    session.commit()
    yield session
    session.close()
    clear_cache()


def _item(owner, kind, source, key, *, days, status="open", **kw) -> LoopItem:
    opened = NOW - timedelta(days=days)
    return LoopItem(
        owner_email=owner,
        kind=kind,
        source=source,
        source_key=key,
        status=status,
        title=kw.pop("title", ""),
        opened_at=opened,
        first_flagged_at=kw.pop("first_flagged_at", opened + timedelta(hours=20)),
        last_checked_at=NOW,
        **kw,
    )


def _seed(db):
    db.add_all(
        [
            _item(
                AMY,
                "unlogged",
                "gmail",
                "gmail:t1",
                days=9,
                title=SECRETS[0],
                counterparty_email="jane@acmecorp.example",
                counterparty_label="Jane Doe",
                company_label="Acme Supply",
                hubspot_contact_id="c1",
                hubspot_company_id="co1",
                link="https://mail.google.com/mail/u/0/#all/t1",
                detail={"crm_contact_name": "Jane Doe"},
            ),
            _item(
                AMY,
                "open_loop",
                "gmail",
                "gmail:t2:m2",
                days=4,
                title=SECRETS[1],
                headline=SECRETS[2],
                counterparty_email="bob@beta.com",
                counterparty_label="Bob Roe",
                company_label="Beta Bank",
                thread_id="t2",
                message_id="m2",
                link="https://mail.google.com/mail/u/0/#all/t2",
            ),
            _item(
                AMY,
                "unlogged",
                "slack",
                "slack:C1:1.1",
                days=1,
                title=SECRETS[3],
                company_label="Acme Supply",
                hubspot_company_id="co1",
                link="https://slack.example/p1",
            ),
            _item(
                ZED,
                "todo",
                "hubspot_task",
                "hstask:t9",
                days=16,
                title=SECRETS[4],
                company_label="Acme Supply",
                hubspot_company_id="co1",
                link="https://app.hubspot.com/tasks/9100001/view/all",
            ),
            # Not open: never listed, but counted in the health numbers.
            _item(
                AMY,
                "open_loop",
                "gmail",
                "gmail:t3:m3",
                days=6,
                status="dismissed",
                title="SECRET-DISMISSED",
                dismiss_reason="automated",
                dismissed_at=NOW - timedelta(days=1),
                dismissed_by=AMY,
            ),
            _item(
                AMY,
                "unlogged",
                "gmail",
                "gmail:t4",
                days=5,
                status="closed",
                title="SECRET-CLOSED",
                close_reason="logged_in_hubspot",
                closed_at=NOW - timedelta(days=1),
            ),
            _item(
                ZED,
                "unlogged",
                "gmail",
                "gmail:t5",
                days=20,
                status="closed",
                title="SECRET-AGED",
                close_reason="aged_out",
                closed_at=NOW - timedelta(hours=1),
            ),
        ]
    )
    db.add(LoopMute(owner_email=AMY, pattern="@vendor.com", created_by=AMY))
    db.add(
        LoopScanRun(
            started_at=NOW - timedelta(minutes=5),
            finished_at=NOW - timedelta(minutes=1),
            status="completed",
            trigger="scheduler",
            coverage={
                AMY: {
                    "gmail": "ok",
                    "slack": "not_connected",
                    "hubspot": "ok",
                    "ai": "ok",
                    "threads_scanned": 52,
                    "slack_messages_scanned": 0,
                    "promises_scanned": 4,
                    "tasks_scanned": 7,
                    "new": 2,
                    "closed": 1,
                },
                ZED: {
                    "gmail": "unavailable",
                    "slack": "ok",
                    "hubspot": "partial",
                    "ai": "ok",
                    "threads_scanned": 0,
                    "slack_messages_scanned": 3,
                    "promises_scanned": 0,
                    "tasks_scanned": 2,
                    "new": 0,
                    "closed": 0,
                },
            },
        )
    )
    db.commit()


# ── mine ─────────────────────────────────────────────────────────────────────


def test_mine_has_exactly_the_documented_shape(db):
    _seed(db)
    body = views.mine_view(db, AMY, now=NOW)
    assert set(body) == MINE_KEYS
    assert body["scanned_at"] == "2026-10-08T12:09:00"
    assert body["coverage"] == {
        "gmail": "ok",
        "slack": "not_connected",
        "hubspot": "ok",
        "ai": "ok",
        "threads_scanned": 52,
        "slack_messages_scanned": 0,
        "promises_scanned": 4,
        "tasks_scanned": 7,
    }
    assert body["notes"] == [
        "Slack is not connected, so Slack messages were not checked. Connect it "
        "in Settings → Slack."
    ]
    assert body["thresholds"] == {
        "open_loop_business_days": 2,
        "todo_grace_days": 5,
        "todo_undated_days": 5,
        "escalation_days": 7,
    }
    assert [g["kind"] for g in body["groups"]] == ["unlogged", "open_loop", "todo"]
    for group in body["groups"]:
        assert set(group) == {"kind", "label", "hint", "empty_line", "count", "items"}
    assert body["groups"][0]["label"] == "Not in HubSpot"
    assert body["groups"][1]["label"] == "Waiting on your reply"
    assert body["groups"][2]["label"] == "You said you would"
    assert body["groups"][2]["count"] == 0 and body["groups"][2]["items"] == []
    assert body["mutes"][0]["pattern"] == "@vendor.com"
    assert set(body["mutes"][0]) == {"id", "pattern", "created_at"}
    assert body["content_hidden"] is False


def test_mine_totals_count_this_week_on_the_org_clock(db):
    _seed(db)
    totals = views.mine_view(db, AMY, now=NOW)["totals"]
    # Open: the two unlogged + the open loop. Over 7 days: the 9-day one.
    assert totals["open"] == 3 and totals["over_escalation"] == 1
    # "This week" starts Monday 2026-10-05 00:00 Chicago = 05:00 UTC.
    monday = views.week_start(NOW)
    assert monday == datetime(2026, 10, 5, 5, 0)
    # Flagged 20h after opening: the Slack item (Thu) and the open loop (Mon
    # 08:10 UTC) are this week; the 5-, 6- and 9-day ones were flagged before.
    assert totals["flagged_this_week"] == 2
    assert totals["closed_this_week"] == 1  # logged_in_hubspot yesterday


def test_a_loose_end_carries_its_action_and_age(db):
    _seed(db)
    groups = {g["kind"]: g for g in views.mine_view(db, AMY, now=NOW)["groups"]}
    oldest, slack = groups["unlogged"]["items"]  # oldest first
    assert oldest["title"] == SECRETS[0]
    assert oldest["days_open"] == 9
    assert oldest["age"] == {"state": "breached", "label": "9 days"}
    assert oldest["primary_action"] == {
        "kind": "log_in_hubspot",
        "label": "Log it in HubSpot",
    }
    assert oldest["secondary_action"]["kind"] == "open_link"
    assert oldest["secondary_action"]["label"] == "Open in Gmail"
    # What "Log it in HubSpot" logs is the AE's own sent email, read when they
    # click (GET …/log-preview) — never a note, and never on the row.
    assert "note_preview" not in oldest
    assert oldest["counterparty"] == "Jane Doe" and oldest["company"] == "Acme Supply"
    assert oldest["reopened"] is False
    assert "headline" not in oldest  # none stored → key absent, not null
    # A Slack item has no email to log and Cubie writes no notes: it opens the
    # company in HubSpot, with the Slack message beside it. Never a log button.
    assert slack["primary_action"] == {
        "kind": "open_link",
        "label": "Open in HubSpot",
        "href": "https://app.hubspot.com/contacts/9100001/company/co1",
    }
    assert slack["secondary_action"] == {
        "kind": "open_link",
        "label": "Open in Slack",
        "href": "https://slack.example/p1",
    }
    assert "note_preview" not in slack
    assert slack["age"] == {"state": "at_risk", "label": "1 day"}

    [loop] = groups["open_loop"]["items"]
    assert loop["headline"] == SECRETS[2]
    assert loop["primary_action"] == {"kind": "draft_reply", "label": "Draft a reply"}
    assert "note_preview" not in loop
    assert set(loop) == {
        "id",
        "kind",
        "source",
        "title",
        "headline",
        "counterparty",
        "company",
        "opened_at",
        "first_flagged_at",
        "days_open",
        "age",
        "reopened",
        "primary_action",
        "secondary_action",
    }


def test_impersonation_hides_titles_and_headlines(db):
    _seed(db)
    body = views.mine_view(db, AMY, now=NOW, content_hidden=True)
    assert body["content_hidden"] is True
    blob = json.dumps(body)
    for marker in SECRETS[:4]:
        assert marker not in blob
    for group in body["groups"]:
        for item in group["items"]:
            assert item["title"] == ""
            assert "headline" not in item and "note_preview" not in item
            # CRM data stays.
            assert item["age"]["label"]


def test_before_the_first_scan_the_page_says_so(db):
    body = views.mine_view(db, MIA, now=NOW)
    assert body["scanned_at"] is None and body["coverage"] is None
    assert "has not scanned" in body["notes"][0]
    assert body["totals"] == {
        "open": 0,
        "over_escalation": 0,
        "flagged_this_week": 0,
        "closed_this_week": 0,
    }


def test_a_hubspot_task_opens_the_task_and_offers_the_company(db):
    _seed(db)
    [task] = views.mine_view(db, ZED, now=NOW)["groups"][2]["items"]
    assert task["primary_action"]["label"] == "Open the task"
    assert task["secondary_action"] == {
        "kind": "open_link",
        "label": "Open the company",
        "href": "https://app.hubspot.com/contacts/9100001/company/co1",
    }


# ── team ─────────────────────────────────────────────────────────────────────


def test_team_has_exactly_the_documented_shape(db):
    _seed(db)
    body = views.team_view(db, now=NOW, emails=[ZED, AMY, MIA])
    assert set(body) == TEAM_KEYS
    assert body["roster_size"] == 3
    for person in body["people"]:
        assert set(person) == PERSON_KEYS
        assert set(person["by_kind"]) == {"unlogged", "open_loop", "todo"}
        assert set(person["aging"]) == {"d0_2", "d3_7", "d8_14", "d15_plus"}
        for item in person["items"]:
            assert set(item) == TEAM_ITEM_KEYS
    assert set(body["health"]) == HEALTH_KEYS
    assert set(body["scorecard"]) == {
        "metric_name",
        "metrics_found",
        "people_without_metric",
        "people_without_seat",
        "can_setup",
    }
    assert body["thresholds"] == {
        "open_loop_business_days": 2,
        "todo_grace_days": 5,
        "todo_undated_days": 5,
        "escalation_days": 7,
    }


def test_team_view_carries_no_message_content_anywhere(db):
    _seed(db)
    blob = json.dumps(views.team_view(db, now=NOW, emails=[AMY, ZED, MIA]))
    for marker in (*SECRETS, "SECRET-DISMISSED", "SECRET-CLOSED", "SECRET-AGED"):
        assert marker not in blob
    for word in ("title", "headline", "subject", "snippet", "note_preview"):
        assert f'"{word}"' not in blob


def test_team_lists_people_a_to_z_and_items_oldest_first(db):
    _seed(db)
    body = views.team_view(db, now=NOW, emails=[ZED, MIA, AMY])
    assert [p["name"] for p in body["people"]] == ["Amy Adams", "Mia Moss", "Zed Zulu"]
    amy = body["people"][0]
    assert [i["days_open"] for i in amy["items"]] == [9, 4, 1]
    assert amy["items"][0]["who"] == "Jane Doe · Acme Supply"  # HubSpot's name
    # "Bob Roe" came from the mail header, not a HubSpot contact: never shown.
    assert amy["items"][1]["who"] == "Beta Bank"
    assert amy["items"][2]["who"] == "Acme Supply"
    assert amy["open"] == 3
    assert amy["by_kind"] == {"unlogged": 2, "open_loop": 1, "todo": 0}
    assert amy["aging"] == {"d0_2": 1, "d3_7": 1, "d8_14": 1, "d15_plus": 0}
    assert amy["over_escalation"] == 1
    assert amy["closed_this_week"] == 1
    assert amy["coverage"] == {
        "gmail": "ok",
        "slack": "not_connected",
        "hubspot": "ok",
        "ai": "ok",
    }
    mia = body["people"][1]
    assert mia["open"] == 0 and mia["items"] == [] and mia["coverage"] is None
    zed = body["people"][2]
    assert zed["aging"]["d15_plus"] == 1
    assert zed["closed_this_week"] == 0  # aged_out is never "closed"


def test_team_items_are_capped_at_25_per_person(db):
    for n in range(30):
        db.add(_item(MIA, "todo", "promise", f"promise:p{n}", days=n + 5))
    db.commit()
    [person] = views.team_view(db, now=NOW, emails=[MIA])["people"]
    assert person["open"] == 30 and len(person["items"]) == 25
    assert person["items"][0]["days_open"] == 34  # oldest first


def test_the_noise_rate_ships_with_its_denominator(db):
    _seed(db)
    health = views.team_view(db, now=NOW, emails=[AMY, ZED, MIA])["health"]
    # Flagged in the last 30 days: all 7 seeded items.
    assert health["window_days"] == 30
    assert health["flagged"] == 7
    assert health["dismissed"] == 1
    assert health["noise_rate"] == round(1 / 7, 3)
    assert health["dismissed_by_reason"] == {"automated": 1}
    assert health["closed"] == 1  # aged_out left out
    assert health["closed_by_reason"] == {"logged_in_hubspot": 1}
    assert health["median_days_to_close"] is not None
    assert sum(w["flagged"] for w in health["weekly"]) == 7
    assert all(
        set(w) == {"week_start", "flagged", "closed", "median_days_to_close"}
        for w in health["weekly"]
    )


def test_an_empty_team_has_no_noise_rate_rather_than_zero(db):
    health = views.team_view(db, now=NOW, emails=[MIA])["health"]
    assert health["flagged"] == 0 and health["noise_rate"] is None


def test_age_chip_turns_at_the_escalation_day():
    assert views.age_chip(6, 7) == {"state": "at_risk", "label": "6 days"}
    assert views.age_chip(7, 7) == {"state": "breached", "label": "7 days"}
    assert views.age_chip(0, 7)["label"] == "under 1 day"


def test_latest_run_is_the_newest_completed_one(db):
    db.add(
        LoopScanRun(
            started_at=NOW - timedelta(days=1), status="completed", trigger="cli"
        )
    )
    db.add(LoopScanRun(started_at=NOW, status="failed", trigger="cli"))
    db.commit()
    assert views.latest_run(db).started_at == NOW - timedelta(days=1)


# ── roster ───────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_the_roster_is_the_sales_department_minus_fixtures_and_leavers(db):
    from rubik_core.database.models import Role, User
    from rubik_core.loop_closure.roster import roster_emails

    admin_role = Role(name="admin")
    db.add_all(
        [
            User(email="sys@acme.example", id="s", name="Sys", department="Systems"),
            User(
                email="boss@acme.example",
                id="b",
                name="Boss",
                department="Systems",
                is_exec=True,
            ),
            User(email="Gone@acme.example", id="g", name="Gone", department="Sales"),
            User(email="rep@outside.com", id="o", name="Out", department="Sales"),
        ]
    )
    admin = User(email="adm@acme.example", id="a", name="Adm", department="Systems")
    admin.roles = [admin_role]
    db.add(admin)
    db.commit()
    got = roster_emails(db, deactivated_lookup=lambda emails: {"gone@acme.example"})
    # Sales only: no admin, no exec, no off-domain address, no leaver.
    assert got == [AMY, MIA, ZED]

    # A failing lookup fails OPEN.
    def _boom(_emails):
        raise RuntimeError("slack down")

    assert "gone@acme.example" in roster_emails(db, deactivated_lookup=_boom)


# ── Review fixes ─────────────────────────────────────────────────────────────


def test_an_unlogged_email_without_a_hubspot_contact_opens_the_record_instead(db):
    """ "Log it in HubSpot" attaches the AE's email to a CONTACT; an email on no
    contact would show on no record. Matched by company domain only, the row
    opens the company in HubSpot; matched to nothing, it opens Gmail."""
    db.add_all(
        [
            _item(
                AMY,
                "unlogged",
                "gmail",
                "gmail:t7",
                days=3,
                hubspot_company_id="co7",
                link="https://mail.google.com/mail/u/0/#all/t7",
            ),
            _item(
                AMY,
                "unlogged",
                "gmail",
                "gmail:t8",
                days=2,
                link="https://mail.google.com/mail/u/0/#all/t8",
            ),
        ]
    )
    db.commit()
    by_company, bare = views.mine_view(db, AMY, now=NOW)["groups"][0]["items"]
    assert by_company["primary_action"] == {
        "kind": "open_link",
        "label": "Open in HubSpot",
        "href": "https://app.hubspot.com/contacts/9100001/company/co7",
    }
    assert by_company["secondary_action"]["label"] == "Open in Gmail"
    assert bare["primary_action"] == {
        "kind": "open_link",
        "label": "Open in Gmail",
        "href": "https://mail.google.com/mail/u/0/#all/t8",
    }
    assert "secondary_action" not in bare
    for row in (by_company, bare):
        assert row["primary_action"]["kind"] != "log_in_hubspot"


def test_a_row_without_a_link_points_at_my_todos_only_for_who_can_open_it(db):
    db.add(_item(AMY, "todo", "promise", "promise:p9", days=5))
    db.commit()

    def action(**kw):
        [row] = views.mine_view(db, AMY, now=NOW, **kw)["groups"][2]["items"]
        return row["primary_action"]

    # A Sales AE has no My To-Dos page (/todos redirects to the briefing).
    assert action() == {"kind": "open_link", "label": "No link to open"}
    assert action(has_todos=True) == {
        "kind": "open_link",
        "label": "Open My To-Dos",
        "href": "/todos",
    }


def test_the_team_view_never_shows_a_name_taken_from_mail(db):
    db.add(
        _item(
            MIA,
            "open_loop",
            "gmail",
            "gmail:t8:m8",
            days=3,
            counterparty_email="pat@personalmail.com",
            counterparty_label="pat@personalmail.com",
        )
    )
    db.add(
        _item(
            MIA,
            "todo",
            "promise",
            "promise:p8",
            days=6,
            counterparty_label="Name The Model Heard",
        )
    )
    db.commit()
    [mia] = views.team_view(db, now=NOW, emails=[MIA])["people"]
    assert [i["who"] for i in mia["items"]] == ["No contact named", "No contact named"]
    blob = json.dumps(mia)
    assert "personalmail" not in blob and "Model Heard" not in blob


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_a_team_read_never_asks_slack_or_google_who_has_left(db, monkeypatch):
    def _boom(_emails):
        raise AssertionError("the Team tab ran the directory lookup")

    monkeypatch.setattr("rubik_core.loop_closure.roster._default_deactivated", _boom)
    body = views.team_view(db, now=NOW)  # the roster, read the cheap way
    assert body["roster_size"] == 3


def test_a_todo_is_listed_only_once_it_is_five_days_overdue(db):
    """To-dos flagged under the old 3-day rule stay open but off every list
    until they are 5 days overdue, and every count agrees with the rows."""
    db.add_all(
        [
            _item(AMY, "todo", "promise", "promise:p3", days=3, detail={"dated": True}),
            _item(AMY, "todo", "promise", "promise:p4", days=4, detail={"dated": True}),
            _item(AMY, "todo", "promise", "promise:p5", days=5, detail={"dated": True}),
            _item(AMY, "open_loop", "gmail", "gmail:t1:m1", days=3),
        ]
    )
    db.commit()
    mine = views.mine_view(db, AMY, now=NOW)
    [todo] = [g for g in mine["groups"] if g["kind"] == "todo"]
    assert todo["count"] == 1 and [i["days_open"] for i in todo["items"]] == [5]
    assert "5 or more days overdue" in todo["hint"]
    assert mine["totals"]["open"] == 2  # the 5-day to-do and the open loop
    [amy] = views.team_view(db, now=NOW, emails=[AMY])["people"]
    assert amy["open"] == 2 and amy["by_kind"]["todo"] == 1
    assert len(amy["items"]) == 2


def test_an_undated_promise_is_listed_by_the_undated_days():
    limits = {"todo_grace_days": 5, "todo_undated_days": 7}
    undated = _item(AMY, "todo", "promise", "p:u", days=6, detail={"dated": False})
    dated = _item(AMY, "todo", "promise", "p:d", days=6, detail={"dated": True})
    assert not views.listed(undated, NOW, limits)
    assert views.listed(dated, NOW, limits)
    # Only to-dos have a line to cross: a fresh open loop is always listed.
    assert views.listed(_item(AMY, "open_loop", "gmail", "g:t", days=0), NOW, limits)


def test_a_hubspot_task_counts_calendar_days_on_the_org_clock(db):
    """Due Oct 3 at 08:00 Chicago; NOW is Oct 8 at 07:10 Chicago. 4 days 23
    hours have passed, and it is 5 days overdue by the calendar, the count the
    scan flags it by. So it is listed, and its chip says 5, not 4."""
    task = LoopItem(
        owner_email=AMY,
        kind="todo",
        source="hubspot_task",
        source_key="hstask:t1",
        status="open",
        title="",
        opened_at=datetime(2026, 10, 3, 13, 0),
        first_flagged_at=NOW,
        last_checked_at=NOW,
    )
    db.add(task)
    db.commit()
    assert views.days_open(task, NOW) == 5
    [todo] = [
        g for g in views.mine_view(db, AMY, now=NOW)["groups"] if g["kind"] == "todo"
    ]
    assert [i["age"]["label"] for i in todo["items"]] == ["5 days"]


def test_rescheduled_is_named_but_never_counted_as_closed(db):
    _seed(db)
    db.add(
        _item(
            ZED,
            "todo",
            "promise",
            "promise:p7",
            days=6,
            status="closed",
            close_reason="rescheduled",
            closed_at=NOW - timedelta(hours=2),
        )
    )
    db.commit()
    body = views.team_view(db, now=NOW, emails=[AMY, ZED, MIA])
    health = body["health"]
    assert health["closed"] == 1  # still only the logged_in_hubspot one
    assert health["closed_by_reason"] == {"logged_in_hubspot": 1, "rescheduled": 1}
    zed = next(p for p in body["people"] if p["email"] == ZED)
    assert zed["closed_this_week"] == 0


def test_unchecked_people_are_the_ones_the_latest_scan_could_not_read(db):
    _seed(db)  # AMY read cleanly; ZED's mailbox unavailable; MIA never covered
    assert views.unchecked_emails(db, [AMY, ZED, MIA]) == {ZED, MIA}
    db.add(
        LoopScanRun(
            started_at=NOW,
            finished_at=NOW,
            status="completed",
            trigger="scheduler",
            coverage={
                MIA: {"gmail": "ok", "slack": "ok", "hubspot": "ok", "ai": "ok"},
                AMY: {
                    "gmail": "unavailable",
                    "slack": "unavailable",
                    "hubspot": "unavailable",
                    "ai": "unavailable",
                    "scan": "failed",
                },
            },
        )
    )
    db.commit()
    assert views.unchecked_emails(db, [AMY, ZED, MIA]) == {AMY, ZED}
