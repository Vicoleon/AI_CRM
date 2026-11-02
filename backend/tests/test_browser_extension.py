"""Cubie for Chrome — the per-browser key, its gates, and the new tab's payload.

What has to be true:

1. Only someone on the ``chrome_extension`` roster, with the switch on, can
   connect a browser, and the raw key is returned once and never stored.
2. The key opens ONLY ``/api/browser-extension``. Everywhere else it is refused
   before the Google check, which would otherwise send it to Google.
3. A disconnected, replaced, expired or offboarded key stops working at once;
   a person moved off the roster gets 403, not their data.
4. The glance never starts a priorities run, shows a promise once wherever it
   lives (EOS To-Do, My To-Dos, a "You said you would…" card), and one broken
   ledger never blanks the rest.
5. ✓ from the new tab goes through the same handler the Cubie page uses, and
   only ever for the person's own items.

Patched by STRING target throughout (other suites ``importlib.reload()``
modules mid-run).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.browser_extension import connections
from rubik_core.config import settings
from rubik_core.database.browser_connections import BrowserConnection
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy
from rubik_core.features import clear_cache as clear_feature_cache
from rubik_core.tenancy.context import use_workspace

client = TestClient(app)

ME = "ana@acme.example"
OTHER = "luis@acme.example"


def _profile(email: str = ME, *, department: str = "Systems") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=f"{email.split('@')[0].title()} Example",
        department=department,
        roles=["user"],
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
    )


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", factory)
    monkeypatch.setattr("backend.routers.browser_extension.SessionLocal", factory)
    monkeypatch.setattr("backend.services.browser_glance.SessionLocal", factory)
    clear_feature_cache()
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        clear_feature_cache()
        engine.dispose()


@pytest.fixture
def people(monkeypatch):
    """The user registry, as a dict the test can edit (delete = offboarded)."""
    roster = {ME: _profile(ME), OTHER: _profile(OTHER)}
    monkeypatch.setattr("backend.auth.user_registry.get_user", roster.get)
    monkeypatch.setattr("backend.auth.user_registry.get_workspace_settings", lambda: {})
    return roster


@pytest.fixture(autouse=True)
def _switched_on(monkeypatch):
    monkeypatch.setattr(settings, "CHROME_EXTENSION_ENABLED", True)


@pytest.fixture
def glance_stub(monkeypatch):
    seen = []

    def fake(user):
        seen.append(user.email)
        return {"user": {"first_name": "Ana"}, "counts": {"overdue": 0}}

    monkeypatch.setattr("backend.services.browser_glance.build_glance", fake)
    return seen


def _as(email: str = ME, **kw) -> UserProfile:
    profile = _profile(email, **kw)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


def _narrow_to_systems(monkeypatch) -> None:
    """The extension ships open to everyone; a case about someone OFF the
    roster installs the roster an admin would narrow it to, as data."""
    pin_rosters(
        monkeypatch,
        {
            "chrome_extension": FeaturePolicy(
                departments=frozenset({"systems"}), admin_allowed=True
            )
        },
    )


def _connect(**body) -> dict:
    res = client.post("/api/browser-extension/connections", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 1. Connecting ────────────────────────────────────────────────────────────


def test_connect_returns_the_key_once_and_stores_only_its_hash(Session, people):
    _as()
    out = _connect(label="Chrome on macOS")
    token = out["token"]
    assert token.startswith("cbx_test.") and len(token) > 40
    assert connections.parse_extension_workspace(token) == "test"
    assert out["connection"]["label"] == "Chrome on macOS"
    assert out["connection"]["hint"] == token[-4:]
    assert "token_hash" not in out["connection"]

    with Session() as db:
        row = db.query(BrowserConnection).one()
        assert row.user_email == ME
        assert row.hosted_domain == "acme.example"
        assert row.token_hash == connections.hash_token(token)
        assert token not in (row.token_hash, row.token_hint, row.label)

    listed = client.get("/api/browser-extension/connections").json()["connections"]
    assert [c["id"] for c in listed] == [out["connection"]["id"]]


def test_switched_off_is_404_everywhere(Session, people, monkeypatch):
    _as()
    token = _connect()["token"]
    monkeypatch.setattr(settings, "CHROME_EXTENSION_ENABLED", False)
    assert client.post("/api/browser-extension/connections", json={}).status_code == 404
    assert client.get("/api/browser-extension/connections").status_code == 404
    assert (
        client.get("/api/browser-extension/glance", headers=_bearer(token)).status_code
        == 404
    )


def test_someone_off_the_roster_cannot_connect(Session, people, monkeypatch):
    _narrow_to_systems(monkeypatch)
    _as(department="Finance")
    res = client.post("/api/browser-extension/connections", json={})
    assert res.status_code == 403


# ── 2. The key opens only the extension's routes ────────────────────────────


def test_the_key_opens_the_glance(Session, people, glance_stub):
    _as()
    token = _connect()["token"]
    app.dependency_overrides.clear()
    res = client.get("/api/browser-extension/glance", headers=_bearer(token))
    assert res.status_code == 200, res.text
    assert glance_stub == [ME]


def test_alias_email_uses_google_verified_hosted_domain(
    Session, people, glance_stub, set_test_workspace_domains
):
    alias = "ana@alias.example"
    people[alias] = _profile(alias)
    with use_workspace("test"):
        session = mint_session_token(alias, "test", hd="acme.example", hours=1.0)[0]
    connected = client.post(
        "/api/browser-extension/connections", json={}, headers=_bearer(session)
    )
    assert connected.status_code == 200, connected.text
    token = connected.json()["token"]
    with Session() as db:
        row = db.query(BrowserConnection).one()
        assert row.user_email == alias
        assert row.hosted_domain == "acme.example"
    response = client.get("/api/browser-extension/glance", headers=_bearer(token))
    assert response.status_code == 200, response.text
    assert glance_stub == [alias]
    set_test_workspace_domains()
    assert (
        client.get("/api/browser-extension/glance", headers=_bearer(token)).status_code
        == 401
    )


def test_extension_key_slug_tamper_is_rejected_before_tenant_db(
    Session, people, glance_stub, monkeypatch
):
    _as()
    token = _connect()["token"]
    assert connections.parse_extension_workspace(token) == "test"
    prefix, random = token.split(".", 1)
    assert prefix.endswith("test")
    tampered = prefix[: -len("test")] + "acme." + random
    app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setattr(
        "backend.routers.browser_extension.SessionLocal",
        lambda: pytest.fail("tampered key opened a tenant database"),
    )
    response = client.get("/api/browser-extension/glance", headers=_bearer(tampered))
    assert response.status_code == 401
    assert glance_stub == []


@pytest.mark.parametrize("status", [None, "suspended"])
def test_extension_key_requires_an_active_workspace_before_tenant_db(
    Session, people, glance_stub, monkeypatch, status
):
    with use_workspace("other"):
        token = connections.mint_extension_key()
    monkeypatch.setattr(
        "rubik_core.tenancy.middleware.get_workspace",
        lambda slug: SimpleNamespace(slug=slug, status=status) if status else None,
    )
    monkeypatch.setattr(
        "backend.routers.browser_extension.SessionLocal",
        lambda: pytest.fail("inactive workspace opened a tenant database"),
    )
    response = client.get("/api/browser-extension/glance", headers=_bearer(token))
    assert response.status_code == 401
    assert glance_stub == []


@pytest.mark.parametrize("header", [None, "Bearer nope", "Bearer atlx_unknown"])
def test_no_key_or_a_wrong_one_is_401(Session, people, glance_stub, header):
    headers = {"Authorization": header} if header else {}
    res = client.get("/api/browser-extension/glance", headers=headers)
    assert res.status_code == 401
    assert glance_stub == []


def test_the_key_is_refused_elsewhere_without_calling_google(
    Session, people, monkeypatch
):
    _as()
    token = _connect()["token"]
    app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)

    def no_google(*_a, **_k):
        raise AssertionError("a Cubie for Chrome key was sent to Google")

    monkeypatch.setattr("requests.get", no_google)
    res = client.get("/api/users/me", headers=_bearer(token))
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "invalid_session"


def test_management_routes_do_not_accept_the_key(Session, people, monkeypatch):
    _as()
    token = _connect()["token"]
    app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    res = client.post(
        "/api/browser-extension/connections", json={}, headers=_bearer(token)
    )
    assert res.status_code == 401


# ── 3. Keys that stop working ────────────────────────────────────────────────


def test_disconnect_in_settings_kills_the_key(Session, people, glance_stub):
    _as()
    out = _connect()
    res = client.delete(f"/api/browser-extension/connections/{out['connection']['id']}")
    assert res.status_code == 200
    app.dependency_overrides.clear()
    res = client.get("/api/browser-extension/glance", headers=_bearer(out["token"]))
    assert res.status_code == 401


def test_cannot_disconnect_someone_elses_browser(Session, people):
    _as(OTHER)
    theirs = _connect()["connection"]["id"]
    _as(ME)
    res = client.delete(f"/api/browser-extension/connections/{theirs}")
    assert res.status_code == 404


def test_sign_out_in_the_extension_kills_its_own_key(Session, people, glance_stub):
    _as()
    token = _connect()["token"]
    app.dependency_overrides.clear()
    res = client.post("/api/browser-extension/disconnect", headers=_bearer(token))
    assert res.status_code == 200
    with Session() as db:
        assert db.query(BrowserConnection).one().revoked_reason == "disconnected"
    res = client.get("/api/browser-extension/glance", headers=_bearer(token))
    assert res.status_code == 401


def test_reconnecting_replaces_the_old_key(Session, people, glance_stub):
    _as()
    first = _connect()
    second = _connect(replaces=first["connection"]["id"])
    app.dependency_overrides.clear()
    old = client.get("/api/browser-extension/glance", headers=_bearer(first["token"]))
    new = client.get("/api/browser-extension/glance", headers=_bearer(second["token"]))
    assert (old.status_code, new.status_code) == (401, 200)


def test_removing_verified_domain_revokes_connection(
    Session, people, glance_stub, set_test_workspace_domains
):
    _as()
    token = _connect()["token"]
    app.dependency_overrides.clear()
    set_test_workspace_domains()
    response = client.get("/api/browser-extension/glance", headers=_bearer(token))
    assert response.status_code == 401
    assert glance_stub == []
    with Session() as db:
        assert connections.list_live(db, ME) == []


def test_replaces_cannot_revoke_someone_elses_key(Session, people):
    _as(OTHER)
    theirs = _connect()
    _as(ME)
    _connect(replaces=theirs["connection"]["id"])
    with Session() as db:
        row = db.get(BrowserConnection, theirs["connection"]["id"])
        assert row.revoked_at is None


def test_at_most_five_live_keys_per_person(Session, people):
    _as()
    tokens = [_connect()["token"] for _ in range(connections.MAX_LIVE_PER_USER + 2)]
    with Session() as db:
        live = connections.list_live(db, ME)
        assert len(live) == connections.MAX_LIVE_PER_USER
        # The newest survive.
        assert {r.token_hash for r in live} == {
            connections.hash_token(t) for t in tokens[-connections.MAX_LIVE_PER_USER :]
        }


def test_an_expired_key_is_dead(Session):
    with Session() as db:
        minted = connections.mint(
            db, ME, hosted_domain="acme.example", label=None, now=datetime(2026, 1, 1)
        )
        db.commit()
        later = datetime(2026, 1, 1) + timedelta(
            days=settings.CHROME_EXTENSION_TOKEN_TTL_DAYS, seconds=1
        )
        assert connections.resolve(db, minted.raw_token, now=later) is None
        assert connections.resolve(db, minted.raw_token, now=datetime(2026, 2, 1))


def test_offboarded_person_is_401(Session, people, glance_stub):
    _as()
    token = _connect()["token"]
    app.dependency_overrides.clear()
    del people[ME]
    res = client.get("/api/browser-extension/glance", headers=_bearer(token))
    assert res.status_code == 401


def test_offboarding_revokes_every_key(Session):
    with Session() as db:
        for _ in range(2):
            connections.mint(db, ME, hosted_domain="acme.example", label=None)
        connections.mint(db, OTHER, hosted_domain="acme.example", label=None)
        db.commit()
        assert connections.revoke_all(db, ME.upper(), reason="offboarded") == 2
        db.commit()
        assert connections.list_live(db, ME) == []
        assert len(connections.list_live(db, OTHER)) == 1


def test_moved_off_the_roster_is_403(Session, people, glance_stub, monkeypatch):
    _narrow_to_systems(monkeypatch)
    _as()
    token = _connect()["token"]
    app.dependency_overrides.clear()
    people[ME] = _profile(ME, department="Finance")
    res = client.get("/api/browser-extension/glance", headers=_bearer(token))
    assert res.status_code == 403
    assert glance_stub == []


def test_last_used_is_stamped_coarsely(Session):
    t0 = datetime(2026, 10, 15, 9, 0)
    with Session() as db:
        minted = connections.mint(
            db, ME, hosted_domain="acme.example", label=None, now=t0
        )
        row = connections.resolve(db, minted.raw_token, now=t0)
        assert row.last_used_at == t0
        connections.resolve(db, minted.raw_token, now=t0 + timedelta(minutes=3))
        assert row.last_used_at == t0
        later = t0 + connections.LAST_USED_RESOLUTION
        connections.resolve(db, minted.raw_token, now=later)
        assert row.last_used_at == later


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """/users/me reads the real users tables through get_db; same set-up as
    test_whats_new_router.py's fold test."""
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'browser_ext.db'}"
    )
    import rubik_core.database.db as dbmod

    monkeypatch.setattr(dbmod, "_engine", None)
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    clear_feature_cache()
    yield
    app.dependency_overrides.clear()
    clear_feature_cache()


def test_me_reports_the_feature_off_with_the_switch(file_db, monkeypatch):
    _as()
    assert client.get("/api/users/me").json()["features"]["chrome_extension"] is True
    monkeypatch.setattr(settings, "CHROME_EXTENSION_ENABLED", False)
    assert client.get("/api/users/me").json()["features"]["chrome_extension"] is False


# ── 4. What the new tab shows ────────────────────────────────────────────────

TODAY = "2026-10-15"


@pytest.fixture
def glance_world(monkeypatch, Session):
    """build_glance with every source faked; tests edit the dict."""
    world = {
        "features": {"copilot", "todos", "chrome_extension"},
        "eos": [],
        "ledger": [],
        "bundle": None,
        "bundle_error": False,
        "bundle_calls": 0,
    }
    monkeypatch.setattr(settings, "TODOS_ENABLED", True)
    monkeypatch.setattr(
        "backend.services.browser_glance.user_today_str", lambda _e: TODAY
    )
    monkeypatch.setattr(
        "backend.services.browser_glance.feature_allows",
        lambda key, _user: key in world["features"],
    )
    monkeypatch.setattr(
        "rubik_core.promises.readthrough.eos_todos_for", lambda _db, _e: world["eos"]
    )
    monkeypatch.setattr(
        "rubik_core.promises.store.list_for_owner", lambda _db, _e: world["ledger"]
    )

    def bundle(_email):
        world["bundle_calls"] += 1
        if world["bundle_error"]:
            raise RuntimeError("home_cards unreadable")
        return world["bundle"]

    monkeypatch.setattr("backend.services.browser_glance._load_bundle", bundle)
    return world


def _glance():
    from backend.services.browser_glance import build_glance

    return build_glance(_profile())


def _eos(id_, due, title="EOS item"):
    return {"id": id_, "title": title, "due_local": due, "team": "Systems"}


def _ledger(id_, due, status="open", title="Ledger item", to="Dakota"):
    return SimpleNamespace(
        id=id_,
        title=title,
        status=status,
        due_local=date.fromisoformat(due) if due else None,
        counterparty_label=to,
    )


def _card(id_, ek="deal", eid="d1", pk="neglect_debt", **kw):
    return {
        "id": id_,
        "title": f"Card {id_}",
        "why_it_matters": "Because.",
        "urgency": "high",
        "entity_kind": ek,
        "entity_id": eid,
        "priority_kind": pk,
        "covered_entity_ids": [eid] if eid else [],
        **kw,
    }


def test_overdue_and_due_today_eos_todos_are_promises(glance_world):
    glance_world["eos"] = [
        _eos("t-late", "2026-10-12"),
        _eos("t-today", TODAY),
        _eos("t-later", "2026-10-20"),
        _eos("t-undated", None),
    ]
    g = _glance()
    items = g["promises"]["items"]
    assert [i["id"] for i in items] == ["t-late", "t-today"]
    assert [i["days_late"] for i in items] == [3, 0]
    assert items[0]["overdue"] is True and items[1]["overdue"] is False
    assert items[0]["href"] == "/eos/todos"
    assert g["counts"]["overdue"] == 1


def test_ledger_rows_need_the_todos_roster(glance_world):
    glance_world["ledger"] = [_ledger("p1", "2026-10-10")]
    assert [i["id"] for i in _glance()["promises"]["items"]] == ["p1"]
    glance_world["features"].discard("todos")
    assert _glance()["promises"]["items"] == []


def test_closed_ledger_rows_are_left_out(glance_world):
    glance_world["ledger"] = [
        _ledger("kept", "2026-10-10", status="kept"),
        _ledger("suggested", "2026-10-10", status="suggested"),
        _ledger("slipping", "2026-10-10", status="slipping"),
    ]
    assert [i["id"] for i in _glance()["promises"]["items"]] == ["slipping"]


def test_a_promise_card_moves_out_of_today_and_shows_once(glance_world):
    glance_world["ledger"] = [_ledger("p1", "2026-10-13")]
    glance_world["bundle"] = {
        "generated_at": "2026-10-15T11:04:00",
        "headline": "Northwind first.",
        "generated_count": 4,
        "priorities": [
            # Same promise as the ledger row: shown once, as the ledger row.
            _card("c1", ek="promise", eid="p1", pk="promise_due"),
            # A heard promise with no ledger row: shown as a promise.
            _card(
                "c2",
                ek="promise",
                eid="p9",
                pk="promise_unkept",
                target_label="Dakota",
                attrs={"promise_link": "https://meet.google.com/x"},
            ),
            _card("c3"),
        ],
    }
    g = _glance()
    promises = g["promises"]["items"]
    assert [(i["kind"], i["id"]) for i in promises] == [
        ("promise", "p1"),
        ("priority", "c2"),
    ]
    assert promises[1]["href"] == "https://meet.google.com/x"
    assert promises[1]["overdue"] is True
    assert [i["id"] for i in g["priorities"]["items"]] == ["c3"]
    assert g["counts"]["overdue"] == 2
    assert g["headline"] == "Northwind first."
    assert g["priorities"]["as_of"] == "2026-10-15T11:04:00"


def test_a_todo_card_is_dropped_when_its_eos_todo_is_listed(glance_world):
    glance_world["eos"] = [_eos("t1", "2026-10-14")]
    glance_world["bundle"] = {
        "priorities": [
            _card("c1", ek="todo", eid="t1"),
            _card("c2", ek="todo", eid="t2"),
        ]
    }
    g = _glance()
    assert [i["id"] for i in g["priorities"]["items"]] == ["c2"]
    assert g["priorities"]["items"][0]["href"] == "/eos/todos"


def test_next_week_and_recommendation_cards_stay_off_the_tab(glance_world):
    glance_world["bundle"] = {
        "priorities": [
            _card("now"),
            _card("nw", origin="next_week"),
            _card("rec", origin="recommendation"),
        ]
    }
    assert [i["id"] for i in _glance()["priorities"]["items"]] == ["now"]


def test_a_meeting_card_opens_meeting_prep(glance_world):
    glance_world["bundle"] = {"priorities": [_card("m", ek="event", eid="evt_1")]}
    assert _glance()["priorities"]["items"][0]["href"] == "/meeting-prep/evt_1"


def test_no_card_today_is_not_ready_and_never_starts_a_run(glance_world):
    g = _glance()
    assert g["priorities"]["status"] == "not_ready"
    assert g["priorities"]["items"] == []
    assert glance_world["bundle_calls"] == 1  # a read, nothing else


def test_without_the_copilot_there_are_no_priorities_to_read(glance_world):
    glance_world["features"].discard("copilot")
    glance_world["eos"] = [_eos("t1", "2026-10-14")]
    g = _glance()
    assert g["priorities"]["status"] == "not_included"
    assert g["can_ask"] is False
    assert glance_world["bundle_calls"] == 0
    assert [i["id"] for i in g["promises"]["items"]] == ["t1"]


def test_a_broken_source_never_blanks_the_tab(glance_world, monkeypatch):
    glance_world["bundle_error"] = True
    glance_world["ledger"] = [_ledger("p1", "2026-10-14")]

    def boom(_db, _e):
        raise RuntimeError("eos down")

    monkeypatch.setattr("rubik_core.promises.readthrough.eos_todos_for", boom)
    g = _glance()
    assert g["priorities"]["status"] == "unavailable"
    assert [i["id"] for i in g["promises"]["items"]] == ["p1"]


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_eos_module_off_skips_eos_todo_glance(glance_world, monkeypatch, lookup_fails):
    from rubik_core.control import repository

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    else:
        original = repository.module_enabled
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda name, slug=None: False if name == "eos" else original(name, slug),
        )
    monkeypatch.setattr(
        "rubik_core.promises.readthrough.eos_todos_for",
        lambda *_: (_ for _ in ()).throw(AssertionError("EOS table read")),
    )
    glance_world["ledger"] = [_ledger("p1", "2026-10-14")]
    glance_world["bundle"] = {
        "priorities": [
            _card("eos-todo", ek="todo", eid="eos-1", pk="eos_todo_due"),
            _card("eos-milestone", ek="milestone", eid="eos-2", pk="eos_milestone_due"),
            _card("eos-rock", ek="rock", eid="eos-3", pk="eos_rock_attention"),
            _card("eos-issue", ek="issue", eid="eos-4", pk="eos_issue_open"),
            _card("regular-card"),
        ],
        "generated_count": 5,
    }
    glance = _glance()
    assert [item["id"] for item in glance["promises"]["items"]] == ["p1"]
    assert [item["id"] for item in glance["priorities"]["items"]] == ["regular-card"]
    assert all(
        not item["href"].startswith("/eos/")
        for item in glance["priorities"]["items"] + glance["promises"]["items"]
    )


def test_longest_overdue_first_and_capped(glance_world):
    glance_world["eos"] = [_eos(f"t{d}", f"2026-10-{15 - d:02d}") for d in range(9)]
    g = _glance()
    assert [i["days_late"] for i in g["promises"]["items"]] == [8, 7, 6, 5, 4, 3]
    assert g["promises"]["more"] == 3
    assert g["counts"]["overdue"] == 8


def test_first_name(glance_world):
    assert _glance()["user"]["first_name"] == "Ana"


# ── 5. ✓ from the new tab ────────────────────────────────────────────────────


def _token_for(email=ME) -> str:
    _as(email)
    token = _connect()["token"]
    app.dependency_overrides.clear()
    return token


def test_done_on_a_card_goes_through_resolve(Session, people, monkeypatch):
    token = _token_for()
    calls = []

    async def fake_resolve(priority_id, user):
        calls.append((priority_id, user.email))

    monkeypatch.setattr("backend.routers.home.resolve_priority", fake_resolve)
    res = client.post(
        "/api/browser-extension/done",
        json={"kind": "priority", "id": "c1"},
        headers=_bearer(token),
    )
    assert res.status_code == 200, res.text
    assert calls == [("c1", ME)]


def test_a_rebuilt_card_answers_404(Session, people, monkeypatch):
    token = _token_for()

    async def gone(priority_id, user):
        raise HTTPException(status_code=404, detail="priority not found")

    monkeypatch.setattr("backend.routers.home.resolve_priority", gone)
    res = client.post(
        "/api/browser-extension/done",
        json={"kind": "priority", "id": "c1"},
        headers=_bearer(token),
    )
    assert res.status_code == 404


def test_done_on_a_promise_needs_my_todos(Session, people, monkeypatch):
    token = _token_for()
    monkeypatch.setattr(settings, "TODOS_ENABLED", False)
    res = client.post(
        "/api/browser-extension/done",
        json={"kind": "promise", "id": "p1"},
        headers=_bearer(token),
    )
    assert res.status_code == 403


def test_done_on_an_eos_todo_only_for_your_own(Session, people, monkeypatch):
    token = _token_for()
    mine = "6f9a2f0e-4a7b-4f43-9a51-0d2a0b0c0d01"
    theirs = "6f9a2f0e-4a7b-4f43-9a51-0d2a0b0c0d02"
    monkeypatch.setattr(
        "rubik_core.promises.readthrough.eos_todos_for",
        lambda _db, _email: [{"id": mine}],
    )
    updated = []

    def fake_update(todo_id, db, current_user, todo_in):
        updated.append((str(todo_id), current_user.email, todo_in.status.value))

    monkeypatch.setattr("backend.routers.eos.todos.update_todo", fake_update)
    ok = client.post(
        "/api/browser-extension/done",
        json={"kind": "eos_todo", "id": mine},
        headers=_bearer(token),
    )
    refused = client.post(
        "/api/browser-extension/done",
        json={"kind": "eos_todo", "id": theirs},
        headers=_bearer(token),
    )
    assert (ok.status_code, refused.status_code) == (200, 404)
    assert updated == [(mine, ME, "done")]


def test_done_rejects_an_unknown_kind(Session, people):
    token = _token_for()
    res = client.post(
        "/api/browser-extension/done",
        json={"kind": "deal", "id": "1"},
        headers=_bearer(token),
    )
    assert res.status_code == 422
