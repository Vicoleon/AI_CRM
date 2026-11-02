"""Admin → What's New over HTTP (``backend/routers/whats_new_admin.py``).

Pinned here:

* admins only: anyone else gets 403 on read and on save;
* a fresh install reads the shipped choices (``#announcements``, posting off,
  the tease on), and nothing about approvers is left in the answer;
* a refused save is a 400 whose detail is the sentence the admin reads (the
  Slack checks) and stores nothing;
* every accepted save is audited, and an older client's extra fields
  (approvers) are ignored rather than refused;
* the tab works with ``WHATS_NEW_ENABLED`` off, so it can be set up first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

PATH = "/api/admin/whats-new/settings"
ADMIN = "admin@acme.example"
PAT = "pat@acme.example"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'test_whats_new_admin.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", True)
    FakeBot.channels = {}
    FakeBot.available = True
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", FakeBot)
    yield
    app.dependency_overrides.clear()


def _profile(email, *, roles=None):
    now = "2026-10-27T00:00:00"
    return UserProfile(
        id=email,
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department="Systems",
        created_at=now,
        updated_at=now,
    )


def _as_admin():
    app.dependency_overrides[get_current_user] = lambda: _profile(
        ADMIN, roles=["admin", "user"]
    )


def _as_user():
    app.dependency_overrides[get_current_user] = lambda: _profile(PAT)


class FakeBot:
    """``SlackBotClient`` as ``settings.save`` uses it: channel lookups only."""

    channels: dict[str, dict] = {}
    available = True

    def __init__(self, *args, **kwargs):
        pass

    @property
    def is_available(self) -> bool:
        return FakeBot.available

    def find_channel(self, name_or_id):
        return FakeBot.channels.get(name_or_id)


def _body(**overrides):
    body = {
        "slack_enabled": False,
        "channel": "#announcements",
        "tease_enabled": True,
    }
    body.update(overrides)
    return body


def _audits():
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import AuditLog

    with SessionLocal() as db:
        return [
            (row.action, row.actor_email, row.doc_id)
            for row in db.query(AuditLog).all()
        ]


def test_only_admins_read_or_save():
    _as_user()
    assert client.get(PATH).status_code == 403
    assert client.put(PATH, json=_body()).status_code == 403
    assert _audits() == []


def test_a_fresh_install_reads_the_shipped_choices():
    _as_admin()
    r = client.get(PATH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body) == [
        "slack_enabled",
        "channel_name",
        "channel_id",
        "bot_in_channel",
        "channel_archived",
        "tease_enabled",
        "updated_by",
        "updated_at",
    ]
    assert body["slack_enabled"] is False
    assert body["channel_name"] == "#announcements"
    assert body["tease_enabled"] is True


def test_saving_with_posting_off_always_works_and_is_audited():
    _as_admin()
    r = client.put(PATH, json=_body(tease_enabled=False, channel="#team-news"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tease_enabled"] is False
    assert body["channel_name"] == "#team-news"
    assert body["updated_by"] == ADMIN
    assert client.get(PATH).json()["tease_enabled"] is False
    assert _audits() == [("WHATS_NEW_SETTINGS", ADMIN, "whats_new")]


def test_an_older_client_sending_approvers_is_not_refused():
    _as_admin()
    r = client.put(
        PATH,
        json=_body(approvers={"*": ["someone@acme.example"]}, dm_approvers=True),
    )
    assert r.status_code == 200, r.text
    assert "approvers" not in r.json()


def test_turning_posting_on_needs_the_bot_in_the_channel():
    FakeBot.channels = {
        "announcements": {
            "id": "C0ANNOUNCE1",
            "name": "announcements",
            "is_member": False,
            "is_archived": False,
        }
    }
    _as_admin()
    r = client.put(PATH, json=_body(slack_enabled=True))
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "Invite Cubie to #announcements first: type /invite @Cubie in that channel."
    )
    assert client.get(PATH).json()["slack_enabled"] is False
    assert _audits() == []

    FakeBot.channels["announcements"]["is_member"] = True
    r = client.put(PATH, json=_body(slack_enabled=True))
    assert r.status_code == 200, r.text
    assert r.json()["slack_enabled"] is True
    assert r.json()["channel_id"] == "C0ANNOUNCE1"
    assert r.json()["bot_in_channel"] is True


def test_a_channel_slack_does_not_know_is_refused_with_the_sentence():
    _as_admin()
    r = client.put(PATH, json=_body(slack_enabled=True, channel="#nowhere"))
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "Cubie can't find #nowhere. Create it in Slack, then save again."
    )


def test_the_tab_works_before_the_feature_is_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", False)
    _as_admin()
    assert client.get(PATH).status_code == 200
    assert client.put(PATH, json=_body()).status_code == 200
