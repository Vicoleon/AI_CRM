"""Admin → Loose Ends: the daily Slack post's switch and channel.

The rule: the post goes to #sales but ships OFF, and an
admin turns it on and changes the channel from the Admin screen. Pinned here
(``rubik_core/loop_closure/digest_settings.py`` and
``SlackBotClient.find_channel``):

* the defaults are OFF, pointed at #sales, and every read failure returns them
  — a database hiccup never posts;
* "#sales", "sales" and a channel id all name a channel; nonsense does not;
* switching ON needs a channel Slack knows AND the Cubie bot in it, and the
  refusal is the sentence the admin reads, naming the fix;
* switching OFF is always allowed for the current channel — even when Slack
  cannot see it or the bot is not installed — so the post can always be
  stopped; a NEW channel Slack cannot see is refused (a typo);
* nothing is stored when a save is refused;
* an archived channel cannot be switched on, and a save with nothing changed
  re-checks the bot with Slack (the card's "Save and re-check");
* what a post learns lands only on the channel it posted to.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import WorkspaceSetting
from rubik_core.loop_closure import digest_settings

ADMIN = "Admin@ACME.example"
SALES = {"id": "C0SALES01", "name": "sales", "is_member": True, "is_private": False}


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


def _stored(db):
    row = db.get(WorkspaceSetting, digest_settings.SETTINGS_KEY)
    return None if row is None else dict(row.value)


def _save(db, bot, *, enabled, channel="#sales"):
    return digest_settings.save(
        db, enabled=enabled, channel=channel, actor_email=ADMIN, bot=bot
    )


# ── defaults and failing closed ──────────────────────────────────────────────


def test_a_fresh_install_is_off_and_pointed_at_sales(db):
    assert digest_settings.DEFAULTS == {
        "enabled": False,
        "channel_name": "#sales",
        "channel_id": None,
    }
    assert digest_settings.load(db) == {
        "enabled": False,
        "channel_name": "#sales",
        "channel_id": None,
        "bot_in_channel": None,
        "channel_archived": False,
        "updated_by": None,
        "updated_at": None,
    }
    # Without a session it opens its own and reads the same.
    assert digest_settings.load()["enabled"] is False


def test_a_read_failure_on_the_callers_session_returns_off(db, monkeypatch):
    db.add(
        WorkspaceSetting(
            key=digest_settings.SETTINGS_KEY,
            value={
                "enabled": True,
                "channel_id": "C0SALES01",
                "channel_name": "#sales",
            },
        )
    )
    db.commit()
    assert digest_settings.load(db)["enabled"] is True

    def broken(_db):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(digest_settings, "_read", broken)
    assert digest_settings.load(db)["enabled"] is False
    assert digest_settings.load(db)["channel_name"] == "#sales"


def test_a_database_that_will_not_open_returns_off(monkeypatch):
    def no_database():
        raise RuntimeError("could not connect")

    monkeypatch.setattr("rubik_core.database.db.SessionLocal", no_database)
    assert digest_settings.load() == {
        **digest_settings.DEFAULTS,
        "bot_in_channel": None,
        "channel_archived": False,
        "updated_by": None,
        "updated_at": None,
    }


# ── naming the channel ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("sales", ("sales", None)),
        ("#sales", ("sales", None)),
        ("  #Sales ", ("sales", None)),
        ("#sales-leads_2", ("sales-leads_2", None)),
        ("C0123ABCD", ("C0123ABCD", "C0123ABCD")),
        (" G0PRIV8TE ", ("G0PRIV8TE", "G0PRIV8TE")),
        # Typed with a "#", or with no digit in it, it is a name in capitals —
        # never an id Slack would answer channel_not_found for.
        ("#G0PRIV8TE", ("g0priv8te", None)),
        ("#CUSTOMERSUCCESS", ("customersuccess", None)),
        ("#GROWTH1", ("growth1", None)),
        ("CUSTOMERSUCCESS", ("customersuccess", None)),
    ],
)
def test_a_channel_is_a_name_or_an_id(typed, expected):
    assert digest_settings.normalise_channel(typed) == expected


@pytest.mark.parametrize("typed", ["", "  ", "#", "sales team", "#sales!", "x" * 81])
def test_nonsense_is_not_a_channel(typed):
    with pytest.raises(ValueError) as err:
        digest_settings.normalise_channel(typed)
    assert "#sales" in str(err.value)


# ── switching it on ──────────────────────────────────────────────────────────


def test_turning_it_on_stores_the_channel_the_bot_is_in(db):
    bot = FakeSlack([SALES])
    saved = _save(db, bot, enabled=True, channel="Sales")
    assert bot.lookups == ["sales"]
    assert saved["enabled"] is True
    assert saved["channel_id"] == "C0SALES01" and saved["channel_name"] == "#sales"
    assert saved["bot_in_channel"] is True
    assert saved["updated_by"] == "admin@acme.example" and saved["updated_at"]
    assert digest_settings.load(db) == saved


def test_a_channel_id_is_looked_up_and_named_from_slack(db):
    bot = FakeSlack([SALES])
    saved = _save(db, bot, enabled=True, channel="C0SALES01")
    assert bot.lookups == ["C0SALES01"]
    assert saved["channel_name"] == "#sales" and saved["channel_id"] == "C0SALES01"


def test_an_unknown_channel_is_refused_and_nothing_is_stored(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([SALES]), enabled=True, channel="#slaes")
    assert str(err.value).startswith("Cubie can't see a channel called #slaes")
    assert "/invite @Cubie in #slaes" in str(err.value)
    assert _stored(db) is None


def test_turning_it_on_needs_the_bot_in_the_channel(db):
    outside = {**SALES, "is_member": False}
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([outside]), enabled=True)
    assert str(err.value) == (
        "Invite Cubie to #sales first: type /invite @Cubie in #sales, then turn "
        "this on."
    )
    assert _stored(db) is None
    assert digest_settings.load(db)["enabled"] is False


def test_turning_it_on_without_the_slack_app_is_a_plain_sentence(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([SALES], available=False), enabled=True)
    assert "isn't connected to Slack" in str(err.value)
    assert _stored(db) is None


def test_a_slack_error_while_turning_it_on_is_a_sentence_not_a_crash(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack(error=RuntimeError("ratelimited")), enabled=True)
    assert "couldn't check #sales with Slack" in str(err.value)
    assert "was not turned on" in str(err.value)
    assert _stored(db) is None


def test_a_slack_error_while_it_is_already_on_says_it_is_still_on(db):
    _save(db, FakeSlack([SALES]), enabled=True)
    before = _stored(db)
    leads = "#sales-leads"
    with pytest.raises(ValueError) as err:
        _save(
            db,
            FakeSlack(error=RuntimeError("ratelimited")),
            enabled=True,
            channel=leads,
        )
    message = str(err.value)
    assert "nothing was changed" in message
    assert "The post is still on for #sales" in message
    assert "not turned on" not in message
    assert _stored(db) == before
    assert digest_settings.load(db)["enabled"] is True


def test_a_capitalised_name_is_looked_up_as_a_name(db):
    growth = {"id": "C0GROW001", "name": "customersuccess", "is_member": True}
    bot = FakeSlack([growth])
    saved = _save(db, bot, enabled=True, channel="#CUSTOMERSUCCESS")
    assert bot.lookups == ["customersuccess"]
    assert saved["channel_id"] == "C0GROW001"
    assert saved["channel_name"] == "#customersuccess"


def test_an_id_shaped_name_slack_has_no_id_for_is_retried_as_a_name(db):
    growth = {"id": "C0GROW001", "name": "growth1", "is_member": True}
    bot = FakeSlack([growth])
    saved = _save(db, bot, enabled=True, channel="GROWTH1")
    assert bot.lookups == ["GROWTH1", "growth1"]
    assert saved["channel_id"] == "C0GROW001" and saved["channel_name"] == "#growth1"


def test_an_archived_channel_cannot_be_switched_on(db):
    archived = {**SALES, "is_archived": True}
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([archived]), enabled=True, channel="C0SALES01")
    assert str(err.value) == (
        "Slack says #sales is archived, so Cubie can't post there. Pick another "
        "channel, then turn this on."
    )
    assert _stored(db) is None
    # Off, it saves, and remembers what Slack said.
    saved = _save(db, FakeSlack([archived]), enabled=False, channel="C0SALES01")
    assert saved["enabled"] is False and saved["channel_archived"] is True


def test_saving_unchanged_values_re_checks_the_bot(db):
    outside = {**SALES, "is_member": False}
    _save(db, FakeSlack([outside]), enabled=False)
    assert digest_settings.load(db)["bot_in_channel"] is False
    # The admin typed /invite @Cubie; the same values saved again ask Slack again.
    bot = FakeSlack([SALES])
    saved = _save(db, bot, enabled=False)
    assert bot.lookups == ["sales"]
    assert saved["bot_in_channel"] is True


# ── switching it off is always allowed ───────────────────────────────────────


def test_the_channel_can_be_set_while_off_before_the_bot_is_invited(db):
    outside = {**SALES, "is_member": False}
    saved = _save(db, FakeSlack([outside]), enabled=False)
    assert saved["enabled"] is False
    assert saved["channel_id"] == "C0SALES01"
    assert saved["bot_in_channel"] is False  # stored as found


def test_turning_the_current_channel_off_works_even_when_slack_cannot_see_it(db):
    _save(db, FakeSlack([SALES]), enabled=True)
    # The channel was archived or made private since; it must still turn off.
    saved = _save(db, FakeSlack([]), enabled=False)
    assert saved["enabled"] is False
    assert saved["channel_id"] == "C0SALES01" and saved["channel_name"] == "#sales"
    assert saved["bot_in_channel"] is False


def test_turning_it_off_works_without_the_slack_app(db):
    _save(db, FakeSlack([SALES]), enabled=True)
    saved = _save(db, FakeSlack(available=False), enabled=False)
    assert saved["enabled"] is False and saved["channel_id"] == "C0SALES01"
    assert saved["bot_in_channel"] is None  # nobody checked


def test_turning_it_off_works_when_slack_errors(db):
    saved = _save(db, FakeSlack(error=RuntimeError("ratelimited")), enabled=False)
    assert saved["enabled"] is False and saved["channel_name"] == "#sales"


def test_a_new_channel_slack_cannot_see_is_refused_even_while_off(db):
    with pytest.raises(ValueError) as err:
        _save(db, FakeSlack([SALES]), enabled=False, channel="#sales-typo")
    assert "can't see a channel called #sales-typo" in str(err.value)
    assert _stored(db) is None


def test_the_post_can_be_moved_to_another_channel(db):
    leads = {"id": "C0LEADS01", "name": "sales-leads", "is_member": True}
    _save(db, FakeSlack([SALES, leads]), enabled=True)
    saved = _save(db, FakeSlack([SALES, leads]), enabled=True, channel="#sales-leads")
    assert (
        saved["channel_id"] == "C0LEADS01" and saved["channel_name"] == "#sales-leads"
    )


def test_recording_the_bots_membership_after_a_post(db):
    record = digest_settings.record_bot_membership
    record(db, False, channel_id="C0SALES01")  # no row: nothing to do
    assert _stored(db) is None
    _save(db, FakeSlack([SALES]), enabled=True)
    record(db, False, channel_id="C0SALES01")
    assert digest_settings.load(db)["bot_in_channel"] is False
    assert digest_settings.load(db)["enabled"] is True  # nothing else moved
    record(db, True, channel_id="C0SALES01")
    assert digest_settings.load(db)["bot_in_channel"] is True


def test_a_posts_answer_never_lands_on_a_channel_it_did_not_post_to(db):
    leads = {"id": "C0LEADS01", "name": "sales-leads", "is_member": True}
    _save(db, FakeSlack([SALES, leads]), enabled=True)
    # The admin moved the post to #sales-leads while a #sales post was in flight.
    _save(db, FakeSlack([SALES, leads]), enabled=True, channel="#sales-leads")
    before = _stored(db)
    digest_settings.record_bot_membership(db, False, channel_id="C0SALES01")
    digest_settings.record_channel_archived(db, channel_id="C0SALES01")
    assert _stored(db) == before
    assert digest_settings.load(db)["bot_in_channel"] is True


def test_recording_an_archived_channel_and_a_post_that_went_through(db):
    _save(db, FakeSlack([SALES]), enabled=True)
    digest_settings.record_channel_archived(db, channel_id="C0SALES01")
    loaded = digest_settings.load(db)
    assert loaded["channel_archived"] is True and loaded["enabled"] is True
    # A post that went through means it is not archived (any more).
    digest_settings.record_bot_membership(db, True, channel_id="C0SALES01")
    assert digest_settings.load(db)["channel_archived"] is False


def test_recording_locks_the_row_it_rewrites(db, monkeypatch):
    from sqlalchemy.orm import Query

    locked: list[bool] = []
    original = Query.with_for_update

    def spy(self, *a, **k):
        locked.append(True)
        return original(self, *a, **k)

    monkeypatch.setattr(Query, "with_for_update", spy)
    _save(db, FakeSlack([SALES]), enabled=True)
    digest_settings.record_bot_membership(db, False, channel_id="C0SALES01")
    assert locked == [True]
    assert digest_settings.load(db)["bot_in_channel"] is False


# ── SlackBotClient.find_channel ──────────────────────────────────────────────


class _Api:
    """Stands in for ``SlackBotClient._api``: canned pages per method/types."""

    def __init__(self, pages=None, info=None, refuse_private=False):
        self.pages = pages or []
        self.info = info
        self.refuse_private = refuse_private
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, *, http_method="post", params=None, json_body=None):
        from backend.services.slack_service import SlackAPIError

        params = dict(params or {})
        self.calls.append((method, params))
        if method == "conversations.info":
            if self.info is None:
                raise SlackAPIError("channel_not_found")
            return {"ok": True, "channel": self.info}
        if self.refuse_private and "private_channel" in params.get("types", ""):
            raise SlackAPIError("missing_scope")
        index = int(params.get("cursor") or 0)
        page = self.pages[index]
        cursor = str(index + 1) if index + 1 < len(self.pages) else ""
        return {
            "ok": True,
            "channels": page,
            "response_metadata": {"next_cursor": cursor},
        }


def _client(api):
    from backend.services.slack_service import SlackBotClient

    bot = SlackBotClient(bot_token="xoxb-test")
    bot._api = api
    return bot


def test_find_channel_pages_through_the_list_by_name():
    api = _Api(
        pages=[
            [{"id": "C1", "name": "general", "is_member": True}],
            [
                {
                    "id": "C0SALES01",
                    "name": "Sales",
                    "is_member": False,
                    "is_private": True,
                }
            ],
        ]
    )
    found = _client(api).find_channel("#sales")
    assert found == {
        "id": "C0SALES01",
        "name": "Sales",
        "is_member": False,
        "is_private": True,
        "is_archived": False,
    }
    assert [c[0] for c in api.calls] == ["conversations.list", "conversations.list"]
    assert api.calls[0][1]["types"] == "public_channel,private_channel"
    assert api.calls[0][1]["exclude_archived"] == "true"


def test_find_channel_falls_back_to_public_channels_without_groups_read():
    api = _Api(pages=[[{"id": "C0SALES01", "name": "sales", "is_member": True}]])
    api.refuse_private = True
    found = _client(api).find_channel("sales")
    assert found["id"] == "C0SALES01" and found["is_member"] is True
    assert api.calls[-1][1]["types"] == "public_channel"


def test_find_channel_by_id_and_a_missing_one():
    api = _Api(info={"id": "C0SALES01", "name": "sales", "is_member": True})
    assert _client(api).find_channel("C0SALES01")["name"] == "sales"
    assert api.calls == [("conversations.info", {"channel": "C0SALES01"})]
    assert _client(_Api(info=None)).find_channel("C0GONE000") is None
    assert _client(_Api(pages=[[]])).find_channel("nowhere") is None
    assert _client(_Api()).find_channel("  ") is None


def test_find_channel_by_id_says_when_the_channel_is_archived():
    # conversations.info answers for an archived channel, members and all.
    api = _Api(
        info={
            "id": "C0SALES01",
            "name": "sales",
            "is_member": True,
            "is_archived": True,
        }
    )
    found = _client(api).find_channel("C0SALES01")
    assert found["is_archived"] is True and found["is_member"] is True
