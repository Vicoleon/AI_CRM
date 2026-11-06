"""The What's New morning post (``rubik_core/whats_new/digest.py``).

Spec §4.6.1: a workspace may opt into a digest of new Cubie release notes.
Pinned here:

* the first run only sets the mark, so switching posting on never floods the
  channel with every note ever written;
* what is due: published after the mark, not in the future, about a feature
  that runs here (a note about a switched-off or unknown feature is never
  posted); oldest first, ordered by ``(published_at, id)``;
* at most twelve notes per post, and fewer when the text would pass Slack's
  guideline; the rest wait for the next post, none is skipped;
* the message: one opener by count, one bullet per note, escaped, with the
  note's tracked ``?src=slack`` link, never ``<!``;
* every Slack outcome maps to a named status: the channel or the bot not
  ready keeps the mark (the notes go out once that is fixed, and what was
  learned is remembered), any other error or a timeout moves it (never posted
  twice);
* posting off posts nothing, a dry run records nothing, the kill switch off
  does nothing.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.services.slack_service import SlackAPIError, SlackNotConnectedError
from rubik_core.database.models import WorkspaceSetting
from rubik_core.whats_new import digest
from rubik_core.whats_new import settings as wn
from rubik_core.whats_new.changelog import ChangelogEntry

CHANNEL_ID = "C0ANNOUNCE1"
BASE = "https://cubie.acme.example"
NOW = datetime(2026, 10, 27, 12, 25)
MARK = NOW - timedelta(days=1)


class PostBot:
    """``SlackBotClient`` as the morning post uses it."""

    def __init__(self, *, error=None, available=True):
        self.is_available = available
        self.error = error
        self.posts: list[dict] = []

    def post_message(self, channel, *, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text})
        if self.error is not None:
            raise self.error
        return {"ok": True, "ts": "1760000000.000200"}


class Source:
    def __init__(self, *entries):
        self.entries = list(entries)

    def list_published(self):
        return list(self.entries)


_counter = itertools.count(1)


def _entry(**over) -> ChangelogEntry:
    n = next(_counter)
    values = {
        "id": f"rel-{n:03d}",
        "title": f"Update {n}",
        "body": f"What changed in update {n}.",
        "feature_key": None,
        "path": None,
        "published_at": NOW - timedelta(hours=1),
    }
    values.update(over)
    return ChangelogEntry(**values)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.config.settings.WHATS_NEW_ENABLED", True)
    monkeypatch.setattr("rubik_core.config.settings.SCHEDULED_TASKS_ENABLED", True)
    monkeypatch.setattr("rubik_core.config.settings.FRONTEND_URL", BASE)
    session = Session()
    yield session
    session.close()


def _slack_on(db, **overrides):
    value = {
        "slack_enabled": True,
        "channel_name": "#announcements",
        "channel_id": CHANNEL_ID,
        "bot_in_channel": True,
        "channel_archived": False,
        "tease_enabled": True,
        **overrides,
    }
    row = db.get(WorkspaceSetting, wn.SETTINGS_KEY)
    if row is None:
        db.add(WorkspaceSetting(key=wn.SETTINGS_KEY, value=value))
    else:
        row.value = value
    db.commit()


def _set_mark(db, at=MARK, entry_id=""):
    wn.write_digest_mark(db, at, entry_id)
    db.commit()


def _mark(db):
    db.expire_all()
    return wn.read_digest_mark(db)


def _stored_settings(db):
    db.expire_all()
    return dict(db.get(WorkspaceSetting, wn.SETTINGS_KEY).value)


def _post(db, *entries, bot=None, **kwargs):
    return digest.post_digest(
        db, now=NOW, bot=bot or PostBot(), source=Source(*entries), **kwargs
    )


# ── the first run, and what is due ───────────────────────────────────────────


def test_the_first_run_only_sets_the_mark(db):
    _slack_on(db)
    bot = PostBot()

    result = _post(db, _entry(), _entry(), bot=bot)

    assert result == {"status": "started", "items": 0, "waiting": 0}
    assert bot.posts == []
    assert _mark(db) == (NOW, "")

    # The notes published before it never go out; a later one does.
    later = _entry(published_at=NOW + timedelta(minutes=5))
    result = digest.post_digest(
        db, now=NOW + timedelta(hours=1), bot=bot, source=Source(later)
    )
    assert result["status"] == "posted" and result["items"] == 1
    assert later.title in bot.posts[0]["text"]


def test_notes_after_the_mark_go_out_oldest_first_and_the_mark_moves(db):
    _slack_on(db)
    _set_mark(db)
    before = _entry(published_at=MARK - timedelta(minutes=1), title="Old news")
    second = _entry(published_at=NOW - timedelta(hours=1), title="Second")
    first = _entry(published_at=NOW - timedelta(hours=3), title="First")
    future = _entry(published_at=NOW + timedelta(hours=1), title="Not yet")
    bot = PostBot()

    result = _post(db, second, before, future, first, bot=bot)

    assert result == {
        "status": "posted",
        "items": 2,
        "waiting": 0,
        "message_ts": "1760000000.000200",
    }
    text = bot.posts[0]["text"]
    assert bot.posts[0]["channel"] == CHANNEL_ID
    assert text.index("First") < text.index("Second")
    assert "Old news" not in text and "Not yet" not in text
    assert _mark(db) == (second.published_at, second.id)

    # A second run has nothing left.
    assert _post(db, second, before, first)["status"] == "quiet"


def test_two_notes_in_the_same_second_are_never_confused(db):
    _slack_on(db)
    at = NOW - timedelta(hours=1)
    a, b = _entry(id="rel-a", published_at=at), _entry(id="rel-b", published_at=at)
    _set_mark(db, at, "rel-a")
    bot = PostBot()

    result = _post(db, a, b, bot=bot)

    assert result["items"] == 1
    assert b.title in bot.posts[0]["text"] and a.title not in bot.posts[0]["text"]


def test_a_note_about_a_feature_that_does_not_run_here_is_never_posted(db, monkeypatch):
    monkeypatch.setattr("rubik_core.config.settings.SCHEDULED_TASKS_ENABLED", False)
    _slack_on(db)
    _set_mark(db)
    bot = PostBot()

    result = _post(
        db,
        _entry(feature_key="scheduled_tasks", title="Off here"),
        _entry(feature_key="help_desk", title="Gone"),
        _entry(title="For everyone"),
        bot=bot,
    )

    assert result["items"] == 1
    assert "For everyone" in bot.posts[0]["text"]
    assert "Off here" not in bot.posts[0]["text"]
    assert "Gone" not in bot.posts[0]["text"]


def test_twelve_per_post_and_the_rest_wait(db):
    _slack_on(db)
    _set_mark(db)
    entries = [_entry(published_at=NOW - timedelta(minutes=60 - n)) for n in range(15)]

    result = _post(db, *entries)

    assert result["items"] == 12 and result["waiting"] == 3
    assert _mark(db) == (entries[11].published_at, entries[11].id)
    result = _post(db, *entries)
    assert result["items"] == 3 and result["waiting"] == 0


def test_a_long_batch_takes_what_fits_and_leaves_the_rest(db):
    _slack_on(db)
    _set_mark(db)
    long = "word " * 200
    entries = [
        _entry(
            published_at=NOW - timedelta(minutes=60 - n),
            title=f"T{n} {long}",
            body=long,
        )
        for n in range(12)
    ]
    bot = PostBot()

    result = _post(db, *entries, bot=bot)

    assert len(bot.posts[0]["text"]) <= digest.MAX_TEXT_CHARS
    assert 1 <= result["items"] < 12
    assert result["items"] + result["waiting"] == 12
    last = entries[result["items"] - 1]
    assert _mark(db) == (last.published_at, last.id)


def test_a_source_that_fails_posts_nothing_and_keeps_the_mark(db):
    class Broken:
        def list_published(self):
            raise RuntimeError("control database away")

    _slack_on(db)
    _set_mark(db)
    bot = PostBot()

    result = digest.post_digest(db, now=NOW, bot=bot, source=Broken())

    assert result["status"] == "failed" and result["error"] == "source"
    assert bot.posts == []
    assert _mark(db) == (MARK, "")


# ── the message ──────────────────────────────────────────────────────────────


def test_the_message_is_escaped_with_one_tracked_link_per_note(db):
    _slack_on(db)
    _set_mark(db)
    sneaky = _entry(
        title="<!channel> look",
        body="Click <https://evil.example|here> & win",
        published_at=NOW - timedelta(hours=2),
    )
    plain = _entry(title="Rocks reorder", body="Drag a Rock.")
    bot = PostBot()

    _post(db, sneaky, plain, bot=bot)

    text = bot.posts[0]["text"]
    assert text.splitlines()[0] == "2 new things in Cubie:"
    assert "<!" not in text
    assert "&lt;!channel&gt;" in text and "&amp; win" in text
    assert f"<{BASE}/whats-new/{sneaky.id}?src=slack|See it>" in text
    assert f"• *Rocks reorder*: Drag a Rock. <{BASE}/whats-new/{plain.id}" in text


def test_one_note_reads_in_the_singular(db):
    _slack_on(db)
    _set_mark(db)
    bot = PostBot()
    _post(db, _entry(), bot=bot)
    assert bot.posts[0]["text"].splitlines()[0] == "One new thing in Cubie:"


def test_without_a_public_address_the_bullets_carry_no_link(db, monkeypatch):
    monkeypatch.setattr(
        "rubik_core.config.settings.FRONTEND_URL", "http://localhost:5173"
    )
    _slack_on(db)
    _set_mark(db)
    bot = PostBot()
    _post(db, _entry(title="No link"), bot=bot)
    assert "See it" not in bot.posts[0]["text"]
    assert "No link" in bot.posts[0]["text"]


def test_a_long_body_is_cut_on_a_word():
    entry = _entry(body="word " * 200)
    line = digest.line(entry)
    body = line.split(": ", 1)[1]
    assert body.endswith("…")
    assert len(body) <= digest.MAX_BODY_CHARS + 1


def test_the_real_client_is_built_with_one_attempt(db, monkeypatch):
    _slack_on(db)
    _set_mark(db)
    built: list[dict] = []

    class Recorder(PostBot):
        def __init__(self, *args, **kwargs):
            built.append(kwargs)
            super().__init__()

    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", Recorder)

    result = digest.post_digest(db, now=NOW, source=Source(_entry()))

    assert result["status"] == "posted"
    assert built == [{"timeout": 8.0, "max_retries": 1}]


# ── outcomes ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "status", "mark_moves", "code"),
    [
        # The channel or the bot isn't ready: the notes wait.
        (SlackAPIError("not_in_channel"), "not_in_channel", False, "not_in_channel"),
        (
            SlackAPIError("channel_not_found"),
            "not_in_channel",
            False,
            "channel_not_found",
        ),
        (SlackAPIError("is_archived"), "channel_archived", False, "is_archived"),
        (SlackNotConnectedError("no token"), "bot_unavailable", False, None),
        # Anything else is a failed post; a timeout may have gone through.
        (SlackAPIError("timeout"), "failed", True, "timeout"),
        (SlackAPIError("invalid_blocks"), "failed", True, "invalid_blocks"),
        (RuntimeError("boom"), "failed", True, "RuntimeError"),
    ],
)
def test_every_slack_outcome_is_a_named_status(db, error, status, mark_moves, code):
    _slack_on(db)
    _set_mark(db)
    entries = [_entry(), _entry()]
    bot = PostBot(error=error)

    result = _post(db, *entries, bot=bot)

    assert result["status"] == status
    assert result.get("error") == code
    assert len(bot.posts) == 1, "one attempt, never retried"
    if mark_moves:
        assert result["items"] == 2 and result["waiting"] == 0
        assert _mark(db) == (entries[-1].published_at, entries[-1].id)
    else:
        assert result["items"] == 0 and result["waiting"] == 2
        assert _mark(db) == (MARK, "")


def test_the_bot_missing_from_the_channel_is_remembered_and_the_notes_wait(db):
    _slack_on(db)
    _set_mark(db)
    entry = _entry()

    _post(db, entry, bot=PostBot(error=SlackAPIError("not_in_channel")))
    assert _stored_settings(db)["bot_in_channel"] is False

    # The admin invites the bot; the next post carries it and that is remembered.
    bot = PostBot()
    result = _post(db, entry, bot=bot)
    assert result["status"] == "posted" and result["items"] == 1
    assert _stored_settings(db)["bot_in_channel"] is True


def test_an_archived_channel_is_remembered(db):
    _slack_on(db)
    _set_mark(db)

    _post(db, _entry(), bot=PostBot(error=SlackAPIError("is_archived")))

    assert _stored_settings(db)["channel_archived"] is True


def test_no_bot_token_posts_nothing(db):
    _slack_on(db)
    _set_mark(db)
    bot = PostBot(available=False)

    result = _post(db, _entry(), bot=bot)

    assert result == {"status": "bot_unavailable", "items": 0, "waiting": 1}
    assert bot.posts == []
    assert _mark(db) == (MARK, "")


# ── switched off, dry runs ───────────────────────────────────────────────────


def test_posting_off_posts_nothing(db):
    _slack_on(db, slack_enabled=False)
    _set_mark(db)
    bot = PostBot()

    result = _post(db, _entry(), bot=bot)

    assert result["status"] == "not_configured"
    assert bot.posts == []
    assert _mark(db) == (MARK, "")


def test_posting_on_without_a_channel_is_not_configured(db):
    _slack_on(db, channel_id=None)
    assert _post(db, _entry())["status"] == "not_configured"


def test_nothing_due_is_quiet(db):
    _slack_on(db)
    _set_mark(db)
    assert _post(db) == {"status": "quiet", "items": 0, "waiting": 0}


def test_a_dry_run_records_nothing_and_shows_the_exact_text(db):
    _slack_on(db)
    _set_mark(db)
    entry = _entry(title="Dry")
    bot = PostBot()

    result = _post(db, entry, bot=bot, dry_run=True)

    assert result["status"] == "dry_run" and result["items"] == 1
    assert result["posting_on"] is True
    assert "Dry" in result["text"]
    assert bot.posts == []
    assert _mark(db) == (MARK, "")


def test_a_dry_run_works_while_posting_is_off(db):
    _slack_on(db, slack_enabled=False)
    _set_mark(db)
    result = _post(db, _entry(), dry_run=True)
    assert result["status"] == "dry_run" and result["posting_on"] is False


def test_a_dry_run_before_the_first_post_sets_no_mark(db):
    _slack_on(db)
    result = _post(db, _entry(), dry_run=True)
    assert result["status"] == "dry_run" and result["items"] == 0
    assert _mark(db) is None


def test_the_kill_switch_off_does_nothing(db, monkeypatch):
    monkeypatch.setattr("rubik_core.config.settings.WHATS_NEW_ENABLED", False)
    _slack_on(db)
    bot = PostBot()
    assert _post(db, _entry(), bot=bot)["status"] == "disabled"
    assert bot.posts == [] and _mark(db) is None


def test_every_status_is_named():
    assert set(digest.STATUSES) == {
        "disabled",
        "not_configured",
        "started",
        "quiet",
        "dry_run",
        "posted",
        "busy",
        "bot_unavailable",
        "not_in_channel",
        "channel_archived",
        "failed",
    }
