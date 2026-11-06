"""Loose Ends — the daily channel digest (``rubik_core/loop_closure/digest.py``).

The digest is the second sanctioned sales sender, so it is held to the
anti-tsunami rules in the database, not in good intentions: a quiet day posts
nothing and writes no guard row; a day posts at most once per channel; a
missing bot or a failed post leaves no row so the day can still post; and the
text is built from names and counts only — seeded titles and headlines must
never appear in it — and stays under Slack's section limit however big the
team gets.

It ships OFF: whether it posts, and where, is Admin →
Loose Ends (``digest_settings`` in ``workspace_settings``). Off — no row, a
row switched off, a half-written row, or a settings read that fails — posts
nothing and writes no guard row. The admin preview (``render_today``) is the
same builder, over the roster the scan posts over, with no side effects; it
never says "would not post" when the next scan will in fact try.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.config import settings
from rubik_core.database.loop_closure import LoopDigestPost, LoopItem, LoopScanRun
from rubik_core.database.models import WorkspaceSetting
from rubik_core.loop_closure import digest, digest_settings

NOW = datetime(2026, 10, 8, 12, 10)  # naive UTC: Thu 07:10 Chicago
AMY = "amy@acme.example"
ZED = "zed@acme.example"
CHANNEL = "C0LOOSE01"


class SlackRefused(Exception):
    """Shaped like ``SlackAPIError``: the Slack error code on ``.error``."""

    def __init__(self, error):
        super().__init__(error)
        self.error = error


class FakeBot:
    def __init__(self, *, available=True, fail=False, refuse=None):
        self.is_available = available
        self.fail = fail
        self.refuse = refuse
        self.posts: list[tuple[str, str]] = []

    def post_message(self, channel, *, text=None, blocks=None, thread_ts=None):
        if self.refuse:
            raise SlackRefused(self.refuse)
        if self.fail:
            raise RuntimeError("boom")
        self.posts.append((channel, text))
        return {"ok": True, "ts": "1728390000.000100"}


def _store(session, value):
    """Write the Admin → Loose Ends row as the admin screen would leave it."""
    row = session.get(WorkspaceSetting, digest_settings.SETTINGS_KEY)
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(WorkspaceSetting(key=digest_settings.SETTINGS_KEY, value=value))
    else:
        row.value = value
    session.commit()


def _turn_on(session, **over):
    _store(
        session,
        {
            "enabled": True,
            "channel_id": CHANNEL,
            "channel_name": "#sales",
            "bot_in_channel": True,
            **over,
        },
    )


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
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://cubie.example")
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    # The preview reads the roster itself; pin it (by string target, so the
    # function-level import in views.team_view picks it up).
    monkeypatch.setattr(
        "rubik_core.loop_closure.roster.roster_emails", lambda _db, **_k: [AMY, ZED]
    )
    session = Session()
    session.add(User(email=AMY, id="amy", name="Amy Adams", department="Sales"))
    session.add(User(email=ZED, id="zed", name="Zed Zulu", department="Sales"))
    session.commit()
    _turn_on(session)  # most tests are about a day it IS on
    yield session
    session.close()


def _open(db, owner, key, *, days, kind="unlogged", title="", headline=None):
    opened = NOW - timedelta(days=days)
    db.add(
        LoopItem(
            owner_email=owner,
            kind=kind,
            source="gmail",
            source_key=key,
            status="open",
            title=title,
            headline=headline,
            company_label="Acme Supply",
            opened_at=opened,
            first_flagged_at=opened,
            last_checked_at=NOW,
        )
    )
    db.commit()


def _post(db, bot, **kw):
    return digest.post_daily_digest(db, now=NOW, bot=bot, emails=[AMY, ZED], **kw)


def test_a_quiet_day_posts_nothing_and_writes_no_row(db):
    bot = FakeBot()
    assert _post(db, bot) == {"status": "quiet", "item_count": 0}
    assert bot.posts == []
    assert db.query(LoopDigestPost).count() == 0


# ── off unless an admin switched it on ───────────────────────────────────────


@pytest.mark.parametrize(
    "stored",
    [
        None,  # a fresh install: no row at all
        {"enabled": False, "channel_id": CHANNEL, "channel_name": "#sales"},
        # Half-written / hand-edited rows read as OFF, never "post somewhere".
        {"enabled": True, "channel_id": None, "channel_name": "#sales"},
        {"enabled": "yes", "channel_id": CHANNEL, "channel_name": "#sales"},
        {"enabled": True, "channel_id": "not an id", "channel_name": "#sales"},
        "garbage",
    ],
)
def test_off_posts_nothing_and_writes_no_row(db, stored):
    _store(db, stored)
    _open(db, AMY, "gmail:t1", days=3)
    bot = FakeBot()
    assert _post(db, bot) == {"status": "off"}
    assert _post(db, bot, dry_run=True) == {"status": "off"}
    assert bot.posts == []
    assert db.query(LoopDigestPost).count() == 0


def test_a_settings_read_that_fails_reads_as_off(db, monkeypatch):
    def broken(_db):
        raise RuntimeError("database went away")

    monkeypatch.setattr(digest_settings, "_read", broken)
    _open(db, AMY, "gmail:t1", days=3)
    bot = FakeBot()
    assert _post(db, bot) == {"status": "off"}
    assert bot.posts == [] and db.query(LoopDigestPost).count() == 0


def test_off_is_in_the_status_vocabulary_and_no_channel_is_gone():
    assert "off" in digest.STATUSES and "not_in_channel" in digest.STATUSES
    assert "channel_archived" in digest.STATUSES
    assert "no_channel" not in digest.STATUSES


def test_it_posts_to_the_stored_channel_id(db):
    _turn_on(db, channel_id="C0OTHER99", channel_name="#sales-leads")
    _open(db, AMY, "gmail:t1", days=3)
    bot = FakeBot()
    assert _post(db, bot)["status"] == "posted"
    [(channel, _text)] = bot.posts
    assert channel == "C0OTHER99"
    [row] = db.query(LoopDigestPost).all()
    assert row.channel_key == "C0OTHER99"


def test_it_posts_once_per_channel_per_day(db):
    _open(db, AMY, "gmail:t1", days=3)
    bot = FakeBot()
    first = _post(db, bot)
    assert first["status"] == "posted" and first["local_date"] == "2026-10-08"
    second = _post(db, bot)
    assert second["status"] == "already_posted"
    assert len(bot.posts) == 1
    [row] = db.query(LoopDigestPost).all()
    assert row.channel_key == CHANNEL  # case kept
    assert row.posted_at is not None and row.message_ts == "1728390000.000100"
    assert row.item_count == 1


def test_the_text_is_names_and_counts_never_content(db):
    _open(db, ZED, "gmail:t1", days=9, title="SECRET-SUBJECT", headline="AI-HEADLINE")
    _open(db, AMY, "gmail:t2", days=2, kind="open_loop", title="OTHER-SECRET")
    bot = FakeBot()
    _post(db, bot)
    [(channel, text)] = bot.posts
    assert channel == CHANNEL
    for marker in ("SECRET-SUBJECT", "AI-HEADLINE", "OTHER-SECRET", "Acme"):
        assert marker not in text
    assert "2 open items across 2 of 2 people" in text
    assert "1 of the 2 open 7+ days" in text
    # A–Z, never ranked by how many they have.
    assert text.index("Amy Adams") < text.index("Zed Zulu")
    assert "1 waiting on your reply" in text
    assert "1 open 7+ days" in text
    assert "https://cubie.example/sales/loose-ends?view=team" in text


def test_the_text_stays_under_the_slack_limit(db):
    from rubik_core.database.models import User

    emails = []
    for n in range(150):
        email = f"rep{n:03d}@acme.example"
        emails.append(email)
        db.add(
            User(
                email=email, id=f"r{n}", name=f"Rep Number {n:03d}", department="Sales"
            )
        )
        _open(db, email, f"gmail:t{n}", days=n % 20)
    db.commit()
    bot = FakeBot()
    result = digest.post_daily_digest(db, now=NOW, bot=bot, emails=emails)
    assert result["status"] == "posted"
    [(_, text)] = bot.posts
    assert len(text) <= digest.MAX_TEXT_CHARS
    assert "more people" in text
    assert text.rstrip().endswith("view=team")


def test_no_bot_leaves_no_row_so_the_day_can_still_post(db):
    _open(db, AMY, "gmail:t1", days=3)
    assert _post(db, FakeBot(available=False))["status"] == "bot_unavailable"
    assert db.query(LoopDigestPost).count() == 0
    assert _post(db, FakeBot())["status"] == "posted"


def test_a_failed_post_rolls_back_the_guard(db):
    _open(db, AMY, "gmail:t1", days=3)
    result = _post(db, FakeBot(fail=True))
    assert result == {"status": "failed", "error": "RuntimeError"}
    assert db.query(LoopDigestPost).count() == 0
    assert _post(db, FakeBot())["status"] == "posted"


@pytest.mark.parametrize("error", ["not_in_channel", "channel_not_found"])
def test_a_bot_outside_the_channel_is_its_own_status_and_shows_on_the_card(db, error):
    _open(db, AMY, "gmail:t1", days=3)
    result = _post(db, FakeBot(refuse=error))
    assert result == {"status": "not_in_channel", "error": error, "item_count": 1}
    assert db.query(LoopDigestPost).count() == 0  # the day can still post
    assert digest_settings.load(db)["bot_in_channel"] is False
    # Once the bot is invited, the next post goes out and the card catches up.
    assert _post(db, FakeBot())["status"] == "posted"
    assert digest_settings.load(db)["bot_in_channel"] is True


def test_an_archived_channel_is_its_own_status_and_shows_on_the_card(db):
    _open(db, AMY, "gmail:t1", days=3)
    result = _post(db, FakeBot(refuse="is_archived"))
    assert result == {
        "status": "channel_archived",
        "error": "is_archived",
        "item_count": 1,
    }
    assert db.query(LoopDigestPost).count() == 0
    loaded = digest_settings.load(db)
    assert loaded["channel_archived"] is True
    assert loaded["bot_in_channel"] is True  # the fix is a channel, not an invite
    # Unarchived (or moved): the next post goes out and the flag clears.
    assert _post(db, FakeBot())["status"] == "posted"
    assert digest_settings.load(db)["channel_archived"] is False


def test_a_refusal_for_a_channel_the_admin_has_since_left_is_not_recorded(
    db, monkeypatch
):
    _open(db, AMY, "gmail:t1", days=3)

    class SlowRefusal(FakeBot):
        def post_message(self, channel, **kw):
            # While this post was in flight, the admin moved it elsewhere.
            _turn_on(db, channel_id="C0OTHER99", channel_name="#sales-leads")
            return super().post_message(channel, **kw)

    result = _post(db, SlowRefusal(refuse="not_in_channel"))
    assert result["status"] == "not_in_channel"
    loaded = digest_settings.load(db)
    assert loaded["channel_id"] == "C0OTHER99"
    assert loaded["bot_in_channel"] is True  # the old channel's answer stayed put


def test_a_dry_run_posts_nothing(db):
    _open(db, AMY, "gmail:t1", days=3)
    bot = FakeBot()
    result = _post(db, bot, dry_run=True)
    assert result["status"] == "dry_run" and "Amy Adams" in result["text"]
    assert bot.posts == [] and db.query(LoopDigestPost).count() == 0


def test_people_nobody_could_check_are_named_and_never_counted_as_scanned(db):
    _open(db, AMY, "gmail:t1", days=3)
    _open(db, ZED, "gmail:t2", days=9)  # yesterday's item; ZED unreadable today
    bot = FakeBot()
    result = _post(db, bot, unchecked={ZED})
    assert result["status"] == "posted" and result["item_count"] == 1
    [(_, text)] = bot.posts
    # The denominator is the people actually scanned, not the roster.
    assert "1 open item across 1 of 1 people scanned" in text
    assert "Not checked this morning (1 of 2 on the roster" in text
    assert "Zed Zulu" in text
    assert text.index("• Amy Adams") < text.index("Not checked")


def test_a_day_with_nothing_open_still_posts_when_someone_could_not_be_checked(db):
    bot = FakeBot()
    result = _post(db, bot, unchecked={ZED})
    assert result["status"] == "posted" and result["item_count"] == 0
    [(_, text)] = bot.posts
    assert "0 open items across 0 of 1 people scanned" in text
    assert "Zed Zulu" in text


# ── the admin preview ────────────────────────────────────────────────────────


def _scanned(db, *emails, gmail="ok"):
    """A completed morning scan that covered these people."""
    db.add(
        LoopScanRun(
            started_at=NOW - timedelta(minutes=5),
            finished_at=NOW,
            status="completed",
            trigger="scheduler",
            coverage={
                e: {"gmail": gmail, "slack": "ok", "hubspot": "ok"} for e in emails
            },
        )
    )
    db.commit()


def _rows(db):
    return db.query(LoopDigestPost).count()


def test_the_preview_is_the_post_without_sending_or_claiming_anything(db):
    _scanned(db, AMY, ZED)
    _open(db, ZED, "gmail:t1", days=9, title="SECRET-SUBJECT", headline="AI-HEADLINE")
    _open(db, AMY, "gmail:t2", days=2, kind="open_loop", title="OTHER-SECRET")
    preview = digest.render_today(db, now=NOW)
    assert preview["would_post"] is True and preview["reason"] is None
    text = preview["text"]
    for marker in ("SECRET-SUBJECT", "AI-HEADLINE", "OTHER-SECRET", "Acme"):
        assert marker not in text
    assert "2 open items across 2 of 2 people" in text
    assert text.index("Amy Adams") < text.index("Zed Zulu")
    assert _rows(db) == 0
    # It is the very text the post would send.
    bot = FakeBot()
    _post(db, bot)
    assert bot.posts[0][1] == text


def test_the_preview_works_while_the_post_is_off_and_says_so(db):
    _store(db, None)
    _scanned(db, AMY, ZED)
    _open(db, AMY, "gmail:t1", days=3)
    preview = digest.render_today(db, now=NOW)
    assert preview["would_post"] is False
    assert "Amy Adams" in preview["text"]
    assert "off" in preview["reason"] and "#sales" in preview["reason"]
    assert _rows(db) == 0


def test_a_quiet_day_previews_as_nothing(db):
    _scanned(db, AMY, ZED)
    preview = digest.render_today(db, now=NOW)
    assert preview == {
        "text": "",
        "would_post": False,
        "reason": "Nothing open today — a quiet day posts nothing.",
    }


def test_the_preview_counts_nobody_it_could_not_check(db):
    _scanned(db, AMY)  # ZED never covered by a scan
    _open(db, AMY, "gmail:t1", days=3)
    preview = digest.render_today(db, now=NOW)
    assert "1 open item across 1 of 1 people scanned" in preview["text"]
    assert "Not checked this morning (1 of 2 on the roster" in preview["text"]


def test_the_preview_says_when_today_already_went_out(db):
    _scanned(db, AMY, ZED)
    _open(db, AMY, "gmail:t1", days=3)
    assert _post(db, FakeBot())["status"] == "posted"
    preview = digest.render_today(db, now=NOW)
    assert preview["would_post"] is False
    assert "already went to #sales" in preview["reason"]
    assert _rows(db) == 1


def test_the_preview_names_the_invite_fix_after_slack_refused(db):
    _scanned(db, AMY, ZED)
    _open(db, AMY, "gmail:t1", days=3)
    _post(db, FakeBot(refuse="not_in_channel"))
    preview = digest.render_today(db, now=NOW)
    # The post never reads the flag: the next scan tries again, so the
    # preview must not say it would not post. It says why the last one failed.
    assert preview["would_post"] is True
    reason = preview["reason"]
    assert "Slack refused the last post to #sales" in reason
    assert "/invite @Cubie in #sales" in reason
    assert "will try again" in reason and "Save re-checks it now" in reason
    assert "would not post" not in reason


def test_the_preview_says_an_archived_channel_posts_nothing(db):
    _turn_on(db, channel_archived=True)
    _scanned(db, AMY, ZED)
    _open(db, AMY, "gmail:t1", days=3)
    preview = digest.render_today(db, now=NOW)
    assert preview["would_post"] is False
    assert "#sales is archived" in preview["reason"]
    assert "Pick another channel" in preview["reason"]


def test_the_preview_says_nothing_posts_while_the_scan_is_off(db, monkeypatch):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    _scanned(db, AMY, ZED)
    _open(db, AMY, "gmail:t1", days=3)
    preview = digest.render_today(db, now=NOW)
    assert preview["would_post"] is False
    assert preview["reason"] == (
        "The Loose Ends scan is switched off on this server, so nothing posts."
    )
    assert "Amy Adams" in preview["text"]


def test_the_preview_reads_the_roster_the_scan_posts_over(db, monkeypatch):
    calls: list = []

    def roster(_db, *, deactivated_lookup=None):
        calls.append(deactivated_lookup)
        # Slack / Google say ZED has left; only the default lookup asks them.
        return [AMY] if deactivated_lookup is None else [AMY, ZED]

    monkeypatch.setattr("rubik_core.loop_closure.roster.roster_emails", roster)
    _scanned(db, AMY)  # ZED left: no scan covers them any more
    _open(db, ZED, "gmail:t9", days=9)  # and an item of theirs is still open
    preview = digest.render_today(db, now=NOW)
    # The post would leave ZED out and find a quiet day; so does the preview.
    assert calls == [None]
    assert preview == {
        "text": "",
        "would_post": False,
        "reason": "Nothing open today — a quiet day posts nothing.",
    }


def test_last_post_is_the_latest_post_slack_accepted(db):
    assert digest.last_post(db) is None
    _open(db, AMY, "gmail:t1", days=3)
    _post(db, FakeBot())
    last = digest.last_post(db)
    assert last == {
        "local_date": "2026-10-08",
        "posted_at": last["posted_at"],
        "item_count": 1,
        "channel_name": "#sales",
    }
    # A post to a channel that is no longer the current one shows its id.
    _turn_on(db, channel_id="C0NEWONE1", channel_name="#sales-new")
    assert digest.last_post(db)["channel_name"] == CHANNEL
