"""What's New over HTTP: the drawer's feed and the opened count.

``backend/routers/whats_new.py`` reads the Cubie changelog through
``get_changelog_source()`` (spec §4.6.1). Pinned here, through the real app:

* the feed: with the empty Phase 1 source it is ``{"items": []}`` (plus the
  tease switch); an entry about a feature that is switched off, that the
  person is not on, or that the registry no longer knows is never shown, and
  never a crash; entries are newest first, a future one waits, a link that
  leaves the app is dropped, and a Slack link to an older entry still finds it;
* the gate: ``WHATS_NEW_ENABLED`` off answers 404 "What's New is not switched
  on." on every route; off the Feature Access roster answers 403;
* an open counts once per person per entry per source, is refused for an entry
  the person can't see, and is never counted while an admin looks as someone;
* ``/api/users/me`` folds ``whats_new`` off with the kill switch or while
  impersonating, and Insights doesn't count the drawer as usage;
* the scheduler's morning post takes the key and is quiet when switched off.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.whats_new.changelog import ChangelogEntry, get_changelog_source

client = TestClient(app)

PAT = "pat@acme.example"
HOLLIS = "hollis@acme.example"
ADMIN = "admin@acme.example"
BASE = "https://cubie.acme.example"
SCHEDULER_KEY = "test-scheduler-key"
CHANNEL_ID = "C0ANNOUNCE1"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch, set_test_workspace_domains):
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'test_whats_new_router.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database.models import Base, Department, User

    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_ENABLED", True)
    monkeypatch.setattr(settings, "FRONTEND_URL", BASE)
    set_test_workspace_domains("acme.example")
    from rubik_core.company.operations import OperationsConfig

    monkeypatch.setattr(
        "rubik_core.insights.population.get_operations_config", OperationsConfig
    )
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", SCHEDULER_KEY)

    from rubik_core.features import clear_cache
    from rubik_core.whats_new import stats

    clear_cache()
    stats.reset_caches()

    with dbmod.SessionLocal() as db:
        for name in ("Sales", "HR", "Systems"):
            db.add(Department(name=name))
        for email, name, dept in (
            (PAT, "Pat Sales", "Sales"),
            (HOLLIS, "Hollis Ramos", "HR"),
            (ADMIN, "Ada Admin", "Systems"),
        ):
            db.add(User(email=email, id=email, name=name, department=dept))
        db.commit()
    yield
    app.dependency_overrides.clear()
    clear_cache()
    stats.reset_caches()


# ── helpers ──────────────────────────────────────────────────────────────────


def _profile(email, *, department, roles=None):
    now = "2026-10-27T00:00:00"
    return UserProfile(
        id=email,
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        created_at=now,
        updated_at=now,
    )


PEOPLE = {
    PAT: _profile(PAT, department="Sales"),
    HOLLIS: _profile(HOLLIS, department="HR"),
    ADMIN: _profile(ADMIN, department="Systems", roles=["admin", "user"]),
}


def _as(email, *, impersonating=False, impersonation=None):
    """Sign in as ``email`` for the next requests. ``impersonating`` marks the
    request the way ``_stash_actor`` does when an admin looks as someone."""
    user = PEOPLE[email]

    def _dep(request: Request) -> UserProfile:
        if impersonating:
            request.state.actor_impersonating = True
        if impersonation is not None:
            request.state.impersonation = impersonation
        return user

    app.dependency_overrides[get_current_user] = _dep


_counter = itertools.count(1)


def _entry(**over) -> ChangelogEntry:
    n = next(_counter)
    values = {
        "id": f"rel-{n}",
        "title": f"Update {n}",
        "body": f"What changed in update {n}.",
        "feature_key": None,
        "path": "/eos/rocks",
        "published_at": datetime.utcnow() - timedelta(hours=1),
    }
    values.update(over)
    return ChangelogEntry(**values)


class FakeSource:
    """A changelog source holding ``entries`` (Phase 3's is the control DB)."""

    def __init__(self, *entries: ChangelogEntry):
        self.entries = list(entries)

    def list_published(self) -> list[ChangelogEntry]:
        return list(self.entries)


def _use(*entries: ChangelogEntry) -> FakeSource:
    source = FakeSource(*entries)
    app.dependency_overrides[get_changelog_source] = lambda: source
    return source


def _limit_feature_to(key: str, department: str) -> None:
    """Admin → Feature Access narrowed ``key`` to one department."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import FeatureAccess
    from rubik_core.features import clear_cache

    with SessionLocal() as db:
        db.add(
            FeatureAccess(
                feature_key=key,
                departments=[department],
                extra_emails=[],
                exec_allowed=False,
                admin_allowed=False,
                everyone=False,
            )
        )
        db.commit()
    clear_cache()


def _opens():
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.whats_new import WhatsNewOpen

    with SessionLocal() as db:
        return [(o.entry_id, o.source, o.department) for o in db.query(WhatsNewOpen)]


# ── the feed ─────────────────────────────────────────────────────────────────


def test_the_empty_source_gives_an_empty_feed():
    """Phase 1 ships no changelog yet: the real default source, no override."""
    _as(PAT)
    r = client.get("/api/whats-new")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "tease_enabled": True}


def test_an_entry_for_a_switched_off_feature_is_never_shown(monkeypatch):
    """Even for someone on the feature's roster."""
    _limit_feature_to("scheduled_tasks", "sales")
    entry = _entry(feature_key="scheduled_tasks")
    _use(entry)
    _as(PAT)
    assert [i["id"] for i in client.get("/api/whats-new").json()["items"]] == [entry.id]

    monkeypatch.setattr(settings, "SCHEDULED_TASKS_ENABLED", False)
    r = client.get("/api/whats-new")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_an_entry_for_a_feature_the_person_is_not_on_is_hidden():
    _limit_feature_to("scheduled_tasks", "hr")
    entry = _entry(feature_key="scheduled_tasks")
    _use(entry)

    _as(PAT)
    assert client.get("/api/whats-new").json()["items"] == []

    _as(HOLLIS)
    assert [i["id"] for i in client.get("/api/whats-new").json()["items"]] == [entry.id]


def test_an_entry_naming_a_feature_the_registry_no_longer_has_is_ignored():
    """Review Focus 2: an unknown key is skipped, never a crash."""
    _use(_entry(feature_key="help_desk"))
    _as(PAT)
    r = client.get("/api/whats-new")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_the_feed_lists_published_entries_newest_first():
    _limit_feature_to("scheduled_tasks", "sales")
    now = datetime.utcnow()
    old = _entry(published_at=now - timedelta(days=3), path=None)
    new = _entry(
        feature_key="scheduled_tasks",
        published_at=now - timedelta(hours=2),
        path="/scheduled-tasks",
    )
    later = _entry(published_at=now + timedelta(days=1))
    _use(old, later, new)
    _as(PAT)

    body = client.get("/api/whats-new").json()

    assert [i["id"] for i in body["items"]] == [new.id, old.id]
    first = body["items"][0]
    assert first == {
        "id": new.id,
        "title": new.title,
        "body": new.body,
        "path": "/scheduled-tasks",
        "feature_label": "Scheduled tasks",
        "published_at": new.published_at.isoformat(),
    }
    assert body["items"][1]["feature_label"] is None
    assert body["items"][1]["path"] is None


def test_a_link_that_leaves_the_app_is_dropped():
    _use(_entry(path="https://evil.example/login"), _entry(path="//evil.example"))
    _as(PAT)
    assert [i["path"] for i in client.get("/api/whats-new").json()["items"]] == [
        None,
        None,
    ]


def test_a_slack_link_to_an_older_entry_still_finds_it():
    from backend.routers.whats_new import FEED_LIMIT

    now = datetime.utcnow()
    entries = [
        _entry(published_at=now - timedelta(minutes=n + 1))
        for n in range(FEED_LIMIT + 1)
    ]
    oldest = entries[-1]
    _use(*entries)
    _as(PAT)

    plain = client.get("/api/whats-new").json()["items"]
    assert len(plain) == FEED_LIMIT
    assert oldest.id not in {i["id"] for i in plain}

    linked = client.get("/api/whats-new", params={"item": oldest.id}).json()
    assert linked["items"][-1]["id"] == oldest.id


def test_the_tease_follows_the_admin_switch():
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import WorkspaceSetting

    with SessionLocal() as db:
        db.add(WorkspaceSetting(key="whats_new", value={"tease_enabled": False}))
        db.commit()
    _as(PAT)
    assert client.get("/api/whats-new").json()["tease_enabled"] is False


def test_a_source_that_fails_reads_as_an_empty_feed():
    class Broken:
        def list_published(self):
            raise RuntimeError("control database unreachable")

    app.dependency_overrides[get_changelog_source] = lambda: Broken()
    _as(PAT)
    r = client.get("/api/whats-new")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


# ── the gate ─────────────────────────────────────────────────────────────────

ROUTES = [
    ("get", "/api/whats-new", None),
    ("post", "/api/whats-new/rel-1/open", {"source": "try_it"}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_is_404_with_the_kill_switch_off(monkeypatch, method, path, body):
    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", False)
    _use(_entry(id="rel-1"))
    _as(PAT)
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "What's New is not switched on."


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_route_is_403_off_the_roster(method, path, body):
    _limit_feature_to("whats_new", "hr")
    _use(_entry(id="rel-1"))
    _as(PAT)
    kwargs = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 403, r.text
    assert "Feature Access" in r.json()["detail"]


# ── opens ────────────────────────────────────────────────────────────────────


def test_an_open_counts_once_per_person_per_source():
    entry = _entry()
    _use(entry)
    _as(PAT)
    path = f"/api/whats-new/{entry.id}/open"

    assert client.post(path, json={"source": "try_it"}).status_code == 204
    assert client.post(path, json={"source": "try_it"}).status_code == 204
    assert client.post(path, json={"source": "slack"}).status_code == 204

    assert sorted(_opens()) == [
        (entry.id, "slack", "sales"),
        (entry.id, "try_it", "sales"),
    ]


@pytest.mark.parametrize("entry_id", ["rel-missing", "rel-hidden", "bad%2Fid"])
def test_an_open_is_refused_for_an_entry_the_person_cannot_see(entry_id):
    _limit_feature_to("scheduled_tasks", "hr")
    _use(_entry(id="rel-hidden", feature_key="scheduled_tasks"))
    _as(PAT)
    r = client.post(f"/api/whats-new/{entry_id}/open", json={"source": "try_it"})
    assert r.status_code == 404, r.text
    assert _opens() == []


def test_an_unknown_source_is_refused():
    entry = _entry()
    _use(entry)
    _as(PAT)
    r = client.post(f"/api/whats-new/{entry.id}/open", json={"source": "email"})
    assert r.status_code == 422
    assert _opens() == []


def test_an_admin_looking_as_someone_is_never_counted():
    entry = _entry()
    _use(entry)
    _as(PAT, impersonating=True)
    r = client.post(f"/api/whats-new/{entry.id}/open", json={"source": "try_it"})
    assert r.status_code == 204
    assert _opens() == []


# ── touch points ─────────────────────────────────────────────────────────────


def test_users_me_folds_the_feature_off_with_the_switch_or_while_impersonating(
    monkeypatch,
):
    _as(HOLLIS)
    assert client.get("/api/users/me").json()["features"]["whats_new"] is True

    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", False)
    assert client.get("/api/users/me").json()["features"]["whats_new"] is False

    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", True)
    _as(HOLLIS, impersonation={"actor_email": ADMIN, "target_email": HOLLIS})
    assert client.get("/api/users/me").json()["features"]["whats_new"] is False


def test_the_drawer_is_not_counted_as_usage_but_its_settings_are_admin():
    from rubik_core.insights import activity

    assert activity.area_for_route("/api/whats-new") is None
    assert activity.area_for_route("/api/whats-new/{entry_id}/open") is None
    assert activity.area_for_route("/api/admin/whats-new/settings") == "admin"


def test_the_morning_post_endpoint_takes_the_key_and_is_quiet_when_off(monkeypatch):
    calls = []

    def fake_digest(db, *, dry_run=False, **kwargs):
        calls.append(dry_run)
        return {"status": "posted", "items": 3, "message_ts": "1.2"}

    monkeypatch.setattr("rubik_core.whats_new.digest.post_digest", fake_digest)
    path = "/api/agents/whats-new/digest"
    headers = {"X-Scheduler-Key": SCHEDULER_KEY, "X-Cubie-Workspace": "test"}

    assert client.post(path).status_code == 403
    assert (
        client.post(
            path, headers={"X-Scheduler-Key": "wrong", "X-Cubie-Workspace": "test"}
        ).status_code
        == 403
    )
    assert calls == []

    r = client.post(path, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "trigger": "scheduler",
        "status": "posted",
        "items": 3,
        "message_ts": "1.2",
    }
    client.post(f"{path}?dry_run=true", headers=headers)
    assert calls == [False, True]

    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", False)
    r = client.post(path, headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "trigger": "scheduler",
        "status": "disabled",
        "flag": "WHATS_NEW_ENABLED",
    }
    assert calls == [False, True]


def test_the_morning_post_endpoint_runs_the_real_post_on_a_dry_run(monkeypatch):
    """End to end through the app: an entry published since the last post is
    in the dry run's text, with its tracked link, and nothing is recorded."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import WorkspaceSetting
    from rubik_core.whats_new import changelog
    from rubik_core.whats_new import settings as wn_settings

    now = datetime.utcnow()
    entry = _entry(published_at=now - timedelta(days=1))
    monkeypatch.setattr(changelog, "get_changelog_source", lambda: FakeSource(entry))
    mark = {"at": (now - timedelta(days=2)).isoformat(), "entry_id": ""}
    with SessionLocal() as db:
        db.add(
            WorkspaceSetting(
                key=wn_settings.SETTINGS_KEY,
                value={
                    "slack_enabled": True,
                    "channel_name": "#announcements",
                    "channel_id": CHANNEL_ID,
                    "bot_in_channel": True,
                },
            )
        )
        db.add(WorkspaceSetting(key=wn_settings.DIGEST_KEY, value=mark))
        db.commit()

    r = client.post(
        "/api/agents/whats-new/digest?dry_run=true",
        headers={"X-Scheduler-Key": SCHEDULER_KEY, "X-Cubie-Workspace": "test"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "dry_run" and body["items"] == 1
    assert f"<{BASE}/whats-new/{entry.id}?src=slack|See it>" in body["text"]
    with SessionLocal() as db:
        assert db.get(WorkspaceSetting, wn_settings.DIGEST_KEY).value == mark
