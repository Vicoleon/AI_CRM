"""Admin → Loose Ends (``backend/routers/loose_ends_admin.py``).

The daily Slack post ships OFF and a Cubie admin turns it on, and picks its
channel, here. Pinned: admin only (403 otherwise, every endpoint); the GET /
PUT / preview shapes the Admin card reads (``frontend/src/api/
looseEndsAdmin.ts`` pins the same names from its side); a refused save is a
400 whose ``detail`` is the sentence the admin reads, and stores nothing; every
save leaves one audit row carrying the switch and the channel only; the
preview posts nothing and claims no day.

Slack is faked by STRING TARGET (``backend.services.slack_service.
SlackBotClient``): ``digest_settings.save`` imports it at call time.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.loop_closure import LoopDigestPost, LoopItem
from rubik_core.database.schemas import UserProfile
from rubik_core.loop_closure import digest_settings

client = TestClient(app)

URL = "/api/admin/loose-ends/digest"
AE = "ae@acme.example"
SALES = {"id": "C0SALES01", "name": "sales", "is_member": True, "is_private": False}

GET_FIELDS = {
    "enabled",
    "channel_name",
    "channel_id",
    "bot_in_channel",
    "channel_archived",
    "updated_by",
    "updated_at",
    "scan_enabled",
    "last_post",
}


def _profile(email: str, roles: list[str]) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department="Systems",
        roles=roles,
        created_at="2026-10-07T00:00:00",
        updated_at="2026-10-07T00:00:00",
    )


def _as_admin():
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "Boss@ACME.example", ["admin", "user"]
    )


def _as_user():
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "rep@acme.example", ["user"]
    )


class FakeSlack:
    channels = [dict(SALES)]
    available = True
    posts: list = []

    def __init__(self, *a, **k):
        self.is_available = FakeSlack.available

    def find_channel(self, name_or_id):
        for ch in FakeSlack.channels:
            if name_or_id in (ch["id"], ch["name"]):
                return dict(ch)
        return None

    def post_message(self, *a, **k):  # the preview must never get here
        FakeSlack.posts.append(a)
        raise AssertionError("the preview posted to Slack")


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'loose_ends_admin.db'}",
        connect_args={"check_same_thread": False},
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()

    def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    FakeSlack.channels = [dict(SALES)]
    FakeSlack.available = True
    FakeSlack.posts = []
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", FakeSlack)
    monkeypatch.setattr(
        "rubik_core.loop_closure.roster.roster_emails", lambda _db, **_k: [AE]
    )
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    yield
    app.dependency_overrides.clear()


def _audit_rows(db):
    from rubik_core.database.models import AuditLog

    return (
        db.query(AuditLog).filter(AuditLog.action == "LOOSE_ENDS_DIGEST_SETTINGS").all()
    )


# ── admin only ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", URL, None),
        ("put", URL, {"enabled": True, "channel": "#sales"}),
        ("post", f"{URL}/preview", None),
    ],
)
def test_a_non_admin_gets_403_everywhere(db, method, path, body):
    _as_user()
    res = getattr(client, method)(path, **({"json": body} if body else {}))
    assert res.status_code == 403
    assert digest_settings.load(db)["enabled"] is False
    assert _audit_rows(db) == []


# ── GET ──────────────────────────────────────────────────────────────────────


def test_get_on_a_fresh_install_is_off_and_pointed_at_sales(db):
    _as_admin()
    res = client.get(URL)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == GET_FIELDS
    assert body["enabled"] is False
    assert body["channel_name"] == "#sales" and body["channel_id"] is None
    assert body["bot_in_channel"] is None
    assert body["channel_archived"] is False
    assert body["last_post"] is None
    assert body["scan_enabled"] is True


def test_get_shows_the_last_post(db):
    _as_admin()
    client.put(URL, json={"enabled": True, "channel": "#sales"})
    db.add(
        LoopDigestPost(
            channel_key="C0SALES01",
            local_date="2026-10-07",
            item_count=4,
            posted_at=datetime(2026, 10, 7, 12, 11),
        )
    )
    db.commit()
    last = client.get(URL).json()["last_post"]
    assert last == {
        "local_date": "2026-10-07",
        "posted_at": "2026-10-07T12:11:00",
        "item_count": 4,
        "channel_name": "#sales",
    }


# ── PUT ──────────────────────────────────────────────────────────────────────


def test_turning_it_on_saves_and_audits_the_switch_and_channel_only(db):
    _as_admin()
    res = client.put(URL, json={"enabled": True, "channel": "Sales"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == GET_FIELDS
    assert body["enabled"] is True and body["bot_in_channel"] is True
    assert body["channel_id"] == "C0SALES01" and body["channel_name"] == "#sales"
    assert body["updated_by"] == "boss@acme.example"
    assert client.get(URL).json()["enabled"] is True
    [row] = _audit_rows(db)
    assert row.actor_email == "boss@acme.example"
    assert row.details == {
        "enabled": True,
        "channel_name": "#sales",
        "channel_id": "C0SALES01",
    }


def test_turning_it_on_without_the_bot_in_the_channel_is_a_400_sentence(db):
    _as_admin()
    FakeSlack.channels = [{**SALES, "is_member": False}]
    res = client.put(URL, json={"enabled": True, "channel": "#sales"})
    assert res.status_code == 400
    assert res.json()["detail"] == (
        "Invite Cubie to #sales first: type /invite @Cubie in #sales, then turn "
        "this on."
    )
    assert digest_settings.load(db)["enabled"] is False
    assert _audit_rows(db) == []


def test_an_unknown_channel_is_a_400_sentence(db):
    _as_admin()
    res = client.put(URL, json={"enabled": False, "channel": "#nowhere"})
    assert res.status_code == 400
    assert res.json()["detail"].startswith("Cubie can't see a channel called #nowhere")


def test_turning_it_off_always_works_and_is_audited(db):
    _as_admin()
    client.put(URL, json={"enabled": True, "channel": "#sales"})
    FakeSlack.available = False  # even with the Slack app gone
    res = client.put(URL, json={"enabled": False, "channel": "#sales"})
    assert res.status_code == 200, res.text
    assert res.json()["enabled"] is False
    assert [r.details["enabled"] for r in _audit_rows(db)] == [True, False]


def test_saving_the_same_values_again_re_checks_the_bot(db):
    _as_admin()
    FakeSlack.channels = [{**SALES, "is_member": False}]
    first = client.put(URL, json={"enabled": False, "channel": "#sales"})
    assert first.status_code == 200 and first.json()["bot_in_channel"] is False
    # The admin typed /invite @Cubie in #sales, then "Save and re-check".
    FakeSlack.channels = [dict(SALES)]
    again = client.put(URL, json={"enabled": False, "channel": "#sales"})
    assert again.status_code == 200, again.text
    assert again.json()["bot_in_channel"] is True
    assert len(_audit_rows(db)) == 2


def test_an_archived_channel_shows_on_the_card_and_cannot_be_switched_on(db):
    _as_admin()
    FakeSlack.channels = [{**SALES, "is_archived": True}]
    refused = client.put(URL, json={"enabled": True, "channel": "C0SALES01"})
    assert refused.status_code == 400
    assert "#sales is archived" in refused.json()["detail"]
    off = client.put(URL, json={"enabled": False, "channel": "C0SALES01"})
    assert off.status_code == 200, off.text
    assert off.json()["channel_archived"] is True
    assert client.get(URL).json()["channel_archived"] is True


def test_a_malformed_body_is_a_422(db):
    _as_admin()
    assert client.put(URL, json={"channel": "#sales"}).status_code == 422
    assert client.put(URL, json={"enabled": True}).status_code == 422


# ── preview ──────────────────────────────────────────────────────────────────


def test_the_preview_renders_without_posting_or_claiming_the_day(db):
    _as_admin()
    now = datetime.utcnow()
    db.add(
        LoopItem(
            owner_email=AE,
            kind="unlogged",
            source="gmail",
            source_key="gmail:t1",
            status="open",
            title="SECRET-SUBJECT",
            company_label="Acme Supply",
            opened_at=now - timedelta(days=3),
            first_flagged_at=now - timedelta(days=3),
            last_checked_at=now,
        )
    )
    db.commit()
    res = client.post(f"{URL}/preview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"text", "would_post", "reason"}
    assert body["would_post"] is False  # the post is still off
    assert "off" in body["reason"]
    assert "SECRET-SUBJECT" not in body["text"] and "Acme" not in body["text"]
    assert db.query(LoopDigestPost).count() == 0
    assert FakeSlack.posts == []


def test_the_preview_says_nothing_posts_while_the_scan_is_off(db, monkeypatch):
    _as_admin()
    assert (
        client.put(URL, json={"enabled": True, "channel": "#sales"}).status_code == 200
    )
    now = datetime.utcnow()
    db.add(
        LoopItem(
            owner_email=AE,
            kind="unlogged",
            source="gmail",
            source_key="gmail:t1",
            status="open",
            title="",
            company_label="",
            opened_at=now - timedelta(days=3),
            first_flagged_at=now - timedelta(days=3),
            last_checked_at=now,
        )
    )
    db.commit()
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    body = client.post(f"{URL}/preview").json()
    assert body["would_post"] is False
    assert "switched off on this server" in body["reason"]
    assert client.get(URL).json()["scan_enabled"] is False
