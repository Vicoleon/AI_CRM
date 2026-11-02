"""Admin → What's New: the morning post's switch and channel, and the tease.

Pinned here (``rubik_core/whats_new/settings.py``):

* a fresh install has Slack posting OFF, pointed at #announcements, and the
  weekly tease on;
* a read failure fails CLOSED (no posting), and keys an older release stored
  (the approvers, the approver DMs) are ignored;
* switching posting ON needs a channel Slack knows, not archived, with the bot
  in it, and each refusal is the sentence the admin reads; saving with posting
  OFF always works and keeps the typed channel;
* the admin settings and the morning post's mark live under two keys that
  never write each other.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import WorkspaceSetting
from rubik_core.whats_new import settings as wn

ADMIN = "Admin@Acme.example"
CHANNEL = {
    "id": "C0ANNOUNCE1",
    "name": "announcements",
    "is_member": True,
    "is_private": False,
}


class FakeSlack:
    """``SlackBotClient`` as ``save`` uses it: ``is_available`` + ``find_channel``."""

    def __init__(self, channels=(), *, available=True, error=None):
        self.is_available = available
        self.channels = [dict(c) for c in channels]
        self.error = error
        self.lookups: list[str] = []

    def find_channel(self, name_or_id):
        self.lookups.append(name_or_id)
        if self.error:
            raise self.error
        for ch in self.channels:
            if name_or_id in (ch["id"], ch["name"]):
                return dict(ch)
        return None


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
    session = Session()
    yield session
    session.close()


def _stored(db, key=wn.SETTINGS_KEY):
    db.expire_all()
    row = db.get(WorkspaceSetting, key)
    return None if row is None else dict(row.value)


def _save(db, bot, *, slack=False, channel="#announcements", tease=True):
    return wn.save(
        db,
        slack_enabled=slack,
        channel=channel,
        tease_enabled=tease,
        actor_email=ADMIN,
        bot=bot,
    )


# ── defaults and failing closed ──────────────────────────────────────────────


def test_a_fresh_install_has_posting_off_and_the_tease_on(db):
    config = wn.load(db)
    assert config["slack_enabled"] is False
    assert config["channel_name"] == "#announcements"
    assert config["channel_id"] is None
    assert config["tease_enabled"] is True
    assert "approvers" not in config


def test_the_defaults_are_never_mutated_by_a_caller(db):
    wn.load(db)["tease_enabled"] = False
    assert wn.load(db)["tease_enabled"] is True
    assert wn.DEFAULTS["tease_enabled"] is True


def test_a_read_failure_fails_closed(db, monkeypatch):
    db.add(
        WorkspaceSetting(
            key=wn.SETTINGS_KEY,
            value={"slack_enabled": True, "channel_id": "C0ANNOUNCE1"},
        )
    )
    db.commit()

    def boom(*_a, **_k):
        raise RuntimeError("database away")

    monkeypatch.setattr(wn, "_row", boom)
    assert wn.load(db)["slack_enabled"] is False


def test_a_hand_edited_row_reads_as_off_without_a_channel_id(db):
    db.add(WorkspaceSetting(key=wn.SETTINGS_KEY, value={"slack_enabled": True}))
    db.commit()
    assert wn.load(db)["slack_enabled"] is False


def test_keys_an_older_release_stored_are_ignored(db):
    """The approval era stored approvers and an approver-DM switch here."""
    db.add(
        WorkspaceSetting(
            key=wn.SETTINGS_KEY,
            value={
                "approvers": {"*": ["someone@acme.example"]},
                "dm_approvers": True,
                "tease_enabled": False,
            },
        )
    )
    db.commit()
    config = wn.load(db)
    assert "approvers" not in config and "dm_approvers" not in config
    assert config["tease_enabled"] is False


# ── the channel ──────────────────────────────────────────────────────────────


def test_saving_with_posting_off_always_works_and_stores_the_typed_channel(db):
    out = _save(db, FakeSlack([], available=False), channel="#Team-News")
    assert out["slack_enabled"] is False
    assert out["channel_name"] == "#team-news"
    assert out["channel_id"] is None
    assert out["bot_in_channel"] is None  # nobody checked
    # Slack erroring while off is not a refusal either.
    out = _save(db, FakeSlack([], error=RuntimeError("boom")), channel="#other")
    assert out["channel_name"] == "#other"


def test_turning_posting_on_records_the_resolved_channel(db):
    out = _save(db, FakeSlack([CHANNEL]), slack=True, tease=False)
    assert out["slack_enabled"] is True
    assert out["channel_id"] == "C0ANNOUNCE1"
    assert out["bot_in_channel"] is True
    assert out["tease_enabled"] is False
    assert out["updated_by"] == "admin@acme.example"
    assert _stored(db)["slack_enabled"] is True
    assert "approvers" not in _stored(db)


def test_posting_on_without_the_bot_in_the_channel_names_the_invite(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([{**CHANNEL, "is_member": False}]), slack=True)
    assert str(err.value) == (
        "Invite Cubie to #announcements first: type /invite @Cubie in that channel."
    )
    assert _stored(db) is None


def test_posting_on_for_a_channel_slack_cannot_find(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([]), slack=True)
    assert str(err.value) == (
        "Cubie can't find #announcements. Create it in Slack, then save again."
    )


def test_posting_on_for_an_archived_channel(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([{**CHANNEL, "is_archived": True}]), slack=True)
    assert "is archived" in str(err.value)


def test_posting_on_without_the_slack_app(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([], available=False), slack=True)
    assert "isn't connected to Slack" in str(err.value)


def test_posting_on_while_slack_errors(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([], error=RuntimeError("boom")), slack=True)
    assert "couldn't check" in str(err.value)


@pytest.mark.parametrize("channel", ["", "   ", "#Not A Channel!"])
def test_impossible_channel_names_are_refused_when_posting_is_on(db, channel):
    with pytest.raises(ValueError):
        _save(db, FakeSlack([CHANNEL]), slack=True, channel=channel)


def test_clearing_the_channel_with_posting_off_keeps_the_current_one(db):
    _save(db, FakeSlack([CHANNEL]), slack=True)
    out = _save(db, FakeSlack([CHANNEL]), slack=False, channel="")
    assert out["channel_id"] == "C0ANNOUNCE1"
    assert out["slack_enabled"] is False


def test_a_channel_id_is_accepted(db):
    out = _save(db, FakeSlack([CHANNEL]), slack=True, channel="C0ANNOUNCE1")
    assert out["channel_name"] == "#announcements"


# ── what a post learns, and the post's own key ───────────────────────────────


def test_a_post_records_bot_membership_only_for_the_same_channel(db):
    _save(db, FakeSlack([CHANNEL]), slack=True)
    wn.record_bot_membership(db, False, channel_id="C0OTHER999")
    assert _stored(db)["bot_in_channel"] is True
    wn.record_bot_membership(db, False, channel_id="C0ANNOUNCE1")
    assert _stored(db)["bot_in_channel"] is False
    wn.record_channel_archived(db, channel_id="C0ANNOUNCE1")
    assert _stored(db)["channel_archived"] is True


def test_a_fresh_install_has_no_mark(db):
    assert wn.read_digest_mark(db) is None


def test_the_mark_lives_under_its_own_key(db):
    _save(db, FakeSlack([CHANNEL]), slack=True)
    before = _stored(db)
    at = datetime(2026, 10, 27, 12, 30)
    wn.write_digest_mark(db, at, "rel-7")
    db.commit()

    assert _stored(db) == before  # admin settings untouched
    assert wn.read_digest_mark(db) == (at, "rel-7")
    # An admin save never touches the mark.
    _save(db, FakeSlack([CHANNEL]), slack=False)
    assert wn.read_digest_mark(db) == (at, "rel-7")


@pytest.mark.parametrize(
    "stored",
    [
        "not a dict",
        {"at": "yesterday", "entry_id": ""},
        {"at": "2026-10-27T12:30:00", "entry_id": "../etc"},
        {"at": "2026-10-27T12:30:00"},
    ],
)
def test_an_unreadable_mark_reads_as_none(db, stored):
    db.add(WorkspaceSetting(key=wn.DIGEST_KEY, value=stored))
    db.commit()
    assert wn.read_digest_mark(db) is None
