"""Control-owned modules must close EOS before any tenant database access."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.config import settings
from rubik_core.control import db as control_db
from rubik_core.control.models import Base, Workspace, WorkspaceDomain, WorkspaceModule
from rubik_core.control.repository import (
    active_workspaces,
    clear_module_cache,
    module_enabled,
)
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def control_workspace(tmp_path, monkeypatch):
    clear_module_cache()
    monkeypatch.setattr(
        settings, "CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}"
    )
    monkeypatch.setattr(control_db, "_engine", None)
    engine = control_db.get_engine()
    Base.metadata.create_all(engine)
    with control_db.control_session() as session:
        session.add(Workspace(slug="acme", name="Acme", status="active"))
        session.add(
            WorkspaceDomain(
                workspace_slug="acme", domain="acme.example", is_primary=True
            )
        )
        session.add(WorkspaceModule(workspace_slug="acme", module="eos", enabled=False))
        session.commit()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        clear_module_cache()
        engine.dispose()


def _token() -> str:
    with use_workspace("acme"):
        return mint_session_token(
            "pat@acme.example", "acme", hd="acme.example", hours=1
        )[0]


def test_eos_module_switch_precedes_feature_roster(control_workspace, monkeypatch):
    from rubik_core.features import policy
    from rubik_core.features.registry import FeaturePolicy

    monkeypatch.setattr(
        policy,
        "effective_policy",
        lambda _key, _db=None: (FeaturePolicy(everyone=True), False),
    )
    with use_workspace("acme"):
        assert not module_enabled("eos")
        assert not policy.feature_allows(
            "eos_rocks_all_teams", SimpleNamespace(email="pat@acme.example")
        )


def test_eos_module_off_blocks_routes_before_tenant_db(control_workspace, monkeypatch):
    from backend.auth import user_registry
    from rubik_core.database.db import get_db
    from rubik_core.tenancy.middleware import _is_eos_path

    def forbidden_db():
        raise AssertionError("tenant DB opened before EOS module gate")

    app.dependency_overrides[get_db] = forbidden_db
    monkeypatch.setattr(
        user_registry,
        "get_user",
        lambda email: (_ for _ in ()).throw(
            AssertionError("user lookup opened tenant DB before EOS module gate")
        ),
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_token()}"}
    # This FastAPI version stores lazy routers in app.routes; OpenAPI expands
    # every mounted method and path, including new EOS endpoints.
    eos_routes = sorted(
        (path, next(iter(methods)))
        for path, methods in app.openapi()["paths"].items()
        if path.startswith("/api/eos/")
    )
    assert eos_routes
    for route, method in eos_routes:
        path = re.sub(r"\{[^}]+\}", "1", route)
        assert _is_eos_path(path)
        response = client.request(method.upper(), path, headers=headers)
        assert response.status_code == 404, (path, response.text)
    for method, path in (
        ("POST", "/api/agents/ingest/eos"),
        ("POST", "/api/agents/eos/l10/summary-emails"),
        ("POST", "/api/agents/eos/l10/close-abandoned-sessions"),
        ("POST", "/api/agents/scorecard/sync-sales"),
        ("POST", "/api/sales/loose-ends/team/scorecard-setup"),
        ("POST", "/api/agents/jobs/ingestion-council/run?scope=weekly"),
        ("POST", "/api/agents/jobs/ingestion-council/run?scope=all"),
        ("GET", "/api/users/pat@acme.example/eos-teams"),
        ("PUT", "/api/users/pat@acme.example/eos-teams"),
        ("POST", "/api/todos/1/promote-eos"),
    ):
        response = client.request(method, path, headers=headers)
        assert response.status_code == 404, (path, response.text)


def test_control_modules_default_core_on_addons_off(control_workspace):
    with use_workspace("acme"):
        assert module_enabled("core")
        assert not module_enabled("eos")
        assert not module_enabled("sales_coach")


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_eos_readthrough_helper_skips_table_when_module_off(
    control_workspace, monkeypatch, lookup_fails
):
    from rubik_core.promises.readthrough import eos_todos_for

    if lookup_fails:
        from rubik_core.control import repository

        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )

    class NoQueries:
        def query(self, *_args):
            raise AssertionError("EOS table queried")

    with use_workspace("acme"):
        assert eos_todos_for(NoQueries(), "pat@acme.example") == []


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_priorities_skips_eos_channel_before_agent_construction(
    control_workspace, monkeypatch, lookup_fails
):
    from rubik_core.control import repository
    from rubik_core.priorities import orchestrator
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import (
        ChannelDigest,
        ChannelKind,
        PrioritiesRequest,
    )

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )

    class ForbiddenEos:
        def __init__(self, **_kwargs):
            raise AssertionError("EOS agent opened tenant tables")

    class CoreAgent:
        channel = ChannelKind.GMAIL

        def __init__(self, **_kwargs):
            pass

        def run(self):
            return ChannelDigest(channel=self.channel)

    monkeypatch.setitem(
        orchestrator.CHANNEL_AGENT_REGISTRY, ChannelKind.EOS, ForbiddenEos
    )
    monkeypatch.setitem(
        orchestrator.CHANNEL_AGENT_REGISTRY, ChannelKind.GMAIL, CoreAgent
    )
    request = PrioritiesRequest(
        user_email="pat@acme.example", channels=[ChannelKind.EOS, ChannelKind.GMAIL]
    )
    with use_workspace("acme"):
        digests = orchestrator.PrioritiesOrchestrator(
            config=PrioritiesConfig.default()
        )._run_channels(request, [], SimpleNamespace())
    assert [digest.channel for digest in digests] == [ChannelKind.GMAIL]


def test_active_workspaces_respects_effective_module_defaults(control_workspace):
    with control_db.control_session() as session:
        session.add(Workspace(slug="paused", name="Paused", status="suspended"))
        session.add(
            WorkspaceModule(workspace_slug="paused", module="eos", enabled=True)
        )
        session.commit()
    assert [workspace.slug for workspace in active_workspaces("core")] == ["acme"]
    assert active_workspaces("eos") == []
    with control_db.control_session() as session:
        session.add(
            WorkspaceModule(workspace_slug="acme", module="core", enabled=False)
        )
        session.query(WorkspaceModule).filter_by(
            workspace_slug="acme", module="eos"
        ).update({"enabled": True})
        session.commit()
    clear_module_cache("acme")
    assert active_workspaces("core") == []
    assert [workspace.slug for workspace in active_workspaces("eos")] == ["acme"]


def test_module_flags_cache_is_bounded_by_workspace_and_invalidates(
    control_workspace, monkeypatch
):
    from rubik_core.control import repository
    from rubik_core.features import policy
    from rubik_core.features.registry import FeaturePolicy

    with control_db.control_session() as session:
        session.add(Workspace(slug="other", name="Other", status="active"))
        session.add(WorkspaceModule(workspace_slug="other", module="eos", enabled=True))
        session.commit()
    clear_module_cache()
    original = repository._read_module_flags
    reads: list[str] = []

    def counted(slug):
        reads.append(slug)
        return original(slug)

    monkeypatch.setattr(repository, "_read_module_flags", counted)
    monkeypatch.setattr(
        policy,
        "effective_policy",
        lambda _key, _db=None: (FeaturePolicy(everyone=True), False),
    )
    with use_workspace("acme"):
        for _ in range(500):
            assert policy.feature_allows(
                "copilot", SimpleNamespace(email="pat@acme.example")
            )
            assert not policy.feature_allows(
                "eos_rocks_all_teams", SimpleNamespace(email="pat@acme.example")
            )
    assert repository.module_flags("other")["eos"]
    assert reads == ["acme", "other"]
    clear_module_cache("acme")
    assert not repository.module_flags("acme")["eos"]
    assert reads == ["acme", "other", "acme"]


def test_module_cache_invalidation_does_not_publish_stale_read(
    control_workspace, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rubik_core.control import repository

    started = Event()
    resume = Event()
    reads = 0

    def read(_slug):
        nonlocal reads
        reads += 1
        if reads == 1:
            started.set()
            assert resume.wait(5)
            return {"core": True, "eos": False, "sales_coach": False}
        return {"core": True, "eos": True, "sales_coach": False}

    clear_module_cache()
    monkeypatch.setattr(repository, "_read_module_flags", read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(repository.module_flags, "acme")
        assert started.wait(5)
        clear_module_cache("acme")
        resume.set()
        assert future.result(timeout=5)["eos"] is True
    assert repository.module_flags("acme")["eos"] is True
    assert reads == 2


def test_module_cache_evicts_after_bounded_number_of_workspaces(
    control_workspace, monkeypatch
):
    from rubik_core.control import repository

    clear_module_cache()
    monkeypatch.setattr(
        repository,
        "_read_module_flags",
        lambda _slug: {"core": True, "eos": False, "sales_coach": False},
    )
    for index in range(repository.MODULE_CACHE_MAX_ENTRIES + 1):
        repository.module_flags(f"workspace-{index}")
    assert len(repository._MODULE_CACHE) == repository.MODULE_CACHE_MAX_ENTRIES
    assert not any(key[1] == "workspace-0" for key in repository._MODULE_CACHE)


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_alignment_helpers_hide_cached_eos_before_tenant_db(
    control_workspace, monkeypatch, lookup_fails
):
    from rubik_core.agents import alignment_context
    from rubik_core.control import repository

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    monkeypatch.setattr(
        alignment_context,
        "_cache_get",
        lambda *_a: (_ for _ in ()).throw(AssertionError("stale EOS cache read")),
    )

    class NoQueries:
        def query(self, *_args):
            raise AssertionError("EOS table queried")

    with use_workspace("acme"):
        assert (
            alignment_context.build_alignment_payload("pat@acme.example", NoQueries())
            is None
        )
        assert (
            alignment_context.active_rock_descriptions_for(
                "pat@acme.example", NoQueries()
            )
            == []
        )
        assert (
            alignment_context.build_alignment_context("pat@acme.example", NoQueries())
            == ""
        )


def test_profile_and_admin_identity_expose_control_switches(control_workspace):
    from backend.auth import admin_required, get_current_user
    from rubik_core.database.db import get_db
    from rubik_core.database.schemas import UserProfile

    admin = UserProfile(
        id="pat",
        email="pat@acme.example",
        name="Pat",
        roles=["admin", "user"],
        created_at="2026-10-29T00:00:00",
        updated_at="2026-10-29T00:00:00",
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[admin_required] = lambda: admin
    app.dependency_overrides[get_db] = lambda: SimpleNamespace(get=lambda *_: None)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_token()}"}
    me = client.get("/api/users/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["modules"] == {
        "core": True,
        "eos": False,
        "sales_coach": False,
    }
    identity = client.get("/api/users/workspace/identity", headers=headers)
    assert identity.status_code == 200, identity.text
    assert identity.json() == {
        "primary_domain": "acme.example",
        "domains": ["acme.example"],
        "modules": me.json()["modules"],
    }


def test_loose_ends_team_skips_eos_scorecard_when_module_off(
    control_workspace, monkeypatch
):
    from backend.auth import get_current_user, require_full_app_access
    from backend.routers import loose_ends
    from rubik_core.database.db import get_db
    from rubik_core.database.schemas import UserProfile
    from rubik_core.loop_closure import scorecard, views

    admin = UserProfile(
        id="pat",
        email="pat@acme.example",
        name="Pat",
        roles=["admin", "user"],
        created_at="2026-10-29T00:00:00",
        updated_at="2026-10-29T00:00:00",
    )
    monkeypatch.setattr(
        scorecard,
        "status",
        lambda *_: (_ for _ in ()).throw(AssertionError("EOS scorecard queried")),
    )
    with use_workspace("acme"):
        assert views._scorecard_status(None, []) is None
    monkeypatch.setattr(
        views,
        "team_view",
        lambda _db: {
            "people": [],
            "roster_size": 0,
            "scorecard": views._scorecard_status(None, []),
        },
    )
    monkeypatch.setattr(loose_ends, "record_audit", lambda *_a, **_k: None)
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_full_app_access] = lambda: admin
    app.dependency_overrides[loose_ends.require_loose_ends_team] = lambda: admin
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    response = TestClient(app).get(
        "/api/sales/loose-ends/team",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["scorecard"] is None
