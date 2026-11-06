"""Platform support sessions route, audit, and revoke before tenant work."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.config import settings
from rubik_core.control import db as control_db
from rubik_core.control.models import (
    Base as ControlBase,
)
from rubik_core.control.models import (
    PlatformAdmin,
    SupportSession,
    Workspace,
    WorkspaceDomain,
)
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import AuditLog, Role, User
from rubik_core.database.models import Base as WorkspaceBase
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.registry import clear_engines, engine_for


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    from rubik_core.insights import activity

    monkeypatch.setattr(activity, "record_request", lambda *_a, **_k: None)
    old_control_engine = control_db._engine
    clear_engines()
    monkeypatch.setattr(settings, "WORKSPACE_DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        settings, "CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}"
    )
    control_db._engine = None
    control_engine = control_db.get_engine()
    ControlBase.metadata.create_all(control_engine)
    with control_db.control_session() as control:
        control.add_all(
            [
                Workspace(slug="rubiksoft", name="RubikSoft", status="active"),
                Workspace(slug="acme", name="Acme", status="active"),
            ]
        )
        control.flush()
        control.add_all(
            [
                WorkspaceDomain(
                    workspace_slug="rubiksoft",
                    domain="rubik-soft.com",
                    is_primary=True,
                ),
                WorkspaceDomain(
                    workspace_slug="acme", domain="acme.example", is_primary=True
                ),
                PlatformAdmin(
                    email="staff@rubik-soft.com", home_workspace_slug="rubiksoft"
                ),
            ]
        )
        control.commit()
    for slug in ("rubiksoft", "acme"):
        with use_workspace(slug):
            WorkspaceBase.metadata.create_all(engine_for(slug))
    with use_workspace("rubiksoft"):
        with SessionLocal() as db:
            admin_role = Role(name="admin", description="Workspace admin")
            db.add(
                User(
                    email="staff@rubik-soft.com",
                    id="staff-1",
                    name="RubikSoft Staff",
                    roles=[admin_role],
                )
            )
            db.commit()
    token = _home_token()
    try:
        yield token
    finally:
        app.dependency_overrides.clear()
        clear_engines()
        control_engine.dispose()
        control_db._engine = old_control_engine


def _home_token() -> str:
    with use_workspace("rubiksoft"):
        return mint_session_token(
            "staff@rubik-soft.com", "rubiksoft", hd="rubik-soft.com", hours=1
        )[0]


def _enter(client: TestClient, token: str) -> str:
    response = client.post(
        "/api/platform/support/enter",
        headers={"Authorization": f"Bearer {token}"},
        json={"workspace": "acme"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["workspace"] == {"slug": "acme", "name": "Acme"}
    return payload["token"]


def _audit_rows(slug: str) -> list[AuditLog]:
    with use_workspace(slug):
        with SessionLocal() as db:
            return list(
                db.query(AuditLog)
                .filter(AuditLog.action == "PLATFORM_SUPPORT_REQUEST")
                .order_by(AuditLog.id)
                .all()
            )


def test_support_write_is_attributed_in_target_workspace(workspaces):
    with TestClient(app) as client:
        support_token = _enter(client, workspaces)
        response = client.patch(
            "/api/users/workspace/settings",
            headers={"Authorization": f"Bearer {support_token}"},
            json={"workspace_sync_enabled": True},
        )
        assert response.status_code == 200, response.text
        me = client.get(
            "/api/users/me", headers={"Authorization": f"Bearer {support_token}"}
        )
        assert me.status_code == 200, me.text
        assert me.json()["support"] == {
            "actor": "staff@rubik-soft.com",
            "workspace_name": "Acme",
        }
    target = _audit_rows("acme")
    assert len(target) == 1
    assert target[0].actor_email == "staff@rubik-soft.com"
    assert target[0].status == "success"
    assert target[0].details["method"] == "PATCH"
    assert target[0].details["path"] == "/api/users/workspace/settings"
    assert target[0].details["status_code"] == 200
    assert target[0].details["sid"]
    assert not any("Bearer" in str(row.details) for row in target)
    assert _audit_rows("rubiksoft") == []


def test_support_audit_insert_failure_blocks_mutation(workspaces, monkeypatch):
    from backend import audit

    with TestClient(app) as client:
        support_token = _enter(client, workspaces)

        def fail_insert(*_args, **_kwargs):
            raise RuntimeError("audit insert down")

        monkeypatch.setattr(audit, "begin_support_audit", fail_insert)
        response = client.patch(
            "/api/users/workspace/settings",
            headers={"Authorization": f"Bearer {support_token}"},
            json={"workspace_sync_enabled": True},
        )
        assert response.status_code == 503
    assert _audit_rows("acme") == []
    with use_workspace("acme"):
        from rubik_core.auth.registry import UserRegistry

        assert not UserRegistry().get_workspace_settings().get("workspace_sync_enabled")


def test_support_audit_finish_failure_keeps_pending_intent(workspaces, monkeypatch):
    from backend import audit

    with TestClient(app) as client:
        token = _enter(client, workspaces)

        def fail_finish(*_args, **_kwargs):
            raise audit.SupportAuditError("audit outcome down")

        monkeypatch.setattr(audit, "finish_support_audit", fail_finish)
        with pytest.raises(audit.SupportAuditError, match="audit outcome down"):
            client.patch(
                "/api/users/workspace/settings",
                headers={"Authorization": f"Bearer {token}"},
                json={"workspace_sync_enabled": True},
            )
    rows = _audit_rows("acme")
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].details["outcome"] == "pending"


def test_support_handler_failure_is_recorded(workspaces, monkeypatch):
    from backend.routers import users

    def fail_update(*_args, **_kwargs):
        raise RuntimeError("target write failed")

    with TestClient(app) as client:
        token = _enter(client, workspaces)
        monkeypatch.setattr(
            users.user_registry, "update_workspace_settings", fail_update
        )
        response = client.patch(
            "/api/users/workspace/settings",
            headers={"Authorization": f"Bearer {token}"},
            json={"workspace_sync_enabled": True},
        )
        assert response.status_code == 500
    rows = _audit_rows("acme")
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].details["status_code"] == 500


def test_support_module_refusal_is_still_audited(workspaces):
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        response = client.post(
            "/api/eos/rocks",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert response.status_code == 404
    rows = _audit_rows("acme")
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].details["status_code"] == 404


def test_support_exit_revokes_sid_and_platform_admin_removal_revokes(workspaces):
    with TestClient(app) as client:
        first_token = _enter(client, workspaces)
        response = client.post(
            "/api/platform/support/exit",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert response.status_code == 200, response.text
        replay = client.get(
            "/api/users/me", headers={"Authorization": f"Bearer {first_token}"}
        )
        assert replay.status_code == 401
        second_token = _enter(client, workspaces)
        from backend.auth import user_registry

        original_get_user = user_registry.get_user

        def unexpected_target_lookup(*_args, **_kwargs):
            raise AssertionError("revoked token reached target DB")

        with control_db.control_session() as control:
            row = control.get(PlatformAdmin, "staff@rubik-soft.com")
            control.delete(row)
            control.commit()
        from unittest.mock import patch

        with patch.object(user_registry, "get_user", unexpected_target_lookup):
            removed = client.get(
                "/api/users/me", headers={"Authorization": f"Bearer {second_token}"}
            )
            assert removed.status_code == 401
        assert user_registry.get_user == original_get_user
    with control_db.control_session() as control:
        sessions = control.query(SupportSession).all()
        assert len(sessions) == 0  # platform admin FK cascade revokes all sids


def test_ordinary_admin_cannot_enter_or_collide_with_target_user(workspaces):
    with control_db.control_session() as control:
        control.delete(control.get(PlatformAdmin, "staff@rubik-soft.com"))
        control.commit()
    with TestClient(app) as client:
        refused = client.post(
            "/api/platform/support/enter",
            headers={"Authorization": f"Bearer {workspaces}"},
            json={"workspace": "acme"},
        )
        assert refused.status_code == 403


def test_platform_admin_home_binding_and_target_status_are_live(
    workspaces, monkeypatch
):
    from backend.routers import platform_support

    def forbidden_target_user(*_args, **_kwargs):
        raise AssertionError("rejected Enter touched target workspace")

    monkeypatch.setattr(platform_support, "_ensure_support_user", forbidden_target_user)
    with TestClient(app) as client:
        with control_db.control_session() as control:
            admin = control.get(PlatformAdmin, "staff@rubik-soft.com")
            admin.home_workspace_slug = "acme"
            control.commit()
        wrong_home = client.post(
            "/api/platform/support/enter",
            headers={"Authorization": f"Bearer {workspaces}"},
            json={"workspace": "acme"},
        )
        assert wrong_home.status_code == 403
        with control_db.control_session() as control:
            admin = control.get(PlatformAdmin, "staff@rubik-soft.com")
            admin.home_workspace_slug = "rubiksoft"
            target = control.get(Workspace, "acme")
            target.status = "suspended"
            control.commit()
        suspended = client.post(
            "/api/platform/support/enter",
            headers={"Authorization": f"Bearer {workspaces}"},
            json={"workspace": "acme"},
        )
        assert suspended.status_code == 404


def test_existing_normal_target_user_blocks_support_entry(workspaces):
    with use_workspace("acme"):
        with SessionLocal() as db:
            db.add(
                User(
                    email="staff@rubik-soft.com",
                    id="acme-employee",
                    name="An Acme employee",
                )
            )
            db.commit()
    with TestClient(app) as client:
        refused = client.post(
            "/api/platform/support/enter",
            headers={"Authorization": f"Bearer {workspaces}"},
            json={"workspace": "acme"},
        )
        assert refused.status_code == 409, refused.text
    with control_db.control_session() as control:
        assert control.query(SupportSession).count() == 0


def test_support_get_is_not_write_audited_and_impersonation_ignored(workspaces):
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        response = client.get(
            "/api/users/me",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Impersonate-Department": "Sales",
                "X-Impersonate-User": "someone@acme.example",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["email"] == "staff@rubik-soft.com"
    assert _audit_rows("acme") == []
    with use_workspace("acme"):
        with SessionLocal() as db:
            assert (
                db.query(User).filter(User.email.like("impersonated_user@%")).count()
                == 0
            )


def test_support_context_refuses_google_delegation(workspaces, monkeypatch):
    from rubik_core.tenancy.support_context import SupportScope, use_support_session
    from rubik_core.tools._workspace_auth import load_workspace_credentials

    with use_workspace("acme"):
        with use_support_session(
            SupportScope(
                actor="staff@rubik-soft.com",
                sid="some-sid",
                workspace_slug="acme",
                workspace_name="Acme",
            )
        ):
            assert load_workspace_credentials(["scope"]) is None


def test_support_session_short_lived_and_non_admin_row(workspaces):
    from rubik_core.auth.session_tokens import decode_session_token

    with TestClient(app) as client:
        token = _enter(client, workspaces)
    claims = decode_session_token(token)
    assert claims is not None
    assert claims.typ == "support"
    assert claims.sub == claims.sup == "staff@rubik-soft.com"
    assert claims.ws == "acme"
    assert claims.sid
    with control_db.control_session() as control:
        session = control.get(SupportSession, claims.sid)
        assert session is not None
        assert session.expires_at.replace(tzinfo=timezone.utc) > datetime.now(
            timezone.utc
        )
    with use_workspace("acme"):
        with SessionLocal() as db:
            row = db.get(User, "staff@rubik-soft.com")
            assert row.is_platform_support is True
            assert row.is_exec is False
            assert row.roles == []


def test_normal_jwt_cannot_use_flagged_support_row(workspaces):
    with TestClient(app) as client:
        _enter(client, workspaces)
        with use_workspace("acme"):
            forged_normal = mint_session_token(
                "staff@rubik-soft.com", "acme", hd="acme.example", hours=1
            )[0]
        response = client.get(
            "/api/users/me", headers={"Authorization": f"Bearer {forged_normal}"}
        )
        assert response.status_code == 401


def test_support_identity_cannot_be_promoted_or_deleted_by_user_api(workspaces):
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        headers = {"Authorization": f"Bearer {token}"}
        update = client.patch(
            "/api/users/staff@rubik-soft.com",
            headers=headers,
            json={"roles": ["admin"], "is_exec": True},
        )
        assert update.status_code == 409
        create = client.post(
            "/api/users/",
            headers=headers,
            json={"email": "staff@rubik-soft.com", "name": "Staff"},
        )
        assert create.status_code == 409
        delete = client.delete(
            "/api/users/staff@rubik-soft.com?force=true", headers=headers
        )
        assert delete.status_code == 409
    with use_workspace("acme"):
        with SessionLocal() as db:
            row = db.get(User, "staff@rubik-soft.com")
            assert row.is_platform_support
            assert row.roles == []
            assert row.is_exec is False


def test_support_cannot_dispatch_cloud_job_or_background_fallback(
    workspaces, monkeypatch
):
    from fastapi import BackgroundTasks, HTTPException

    from backend.jobs import launcher
    from rubik_core.tenancy.support_context import SupportScope, use_support_session

    def unexpected_job_config(*_args, **_kwargs):
        raise AssertionError("support dispatch reached Cloud Run configuration")

    monkeypatch.setattr(launcher, "gcp_project_id", unexpected_job_config)
    tasks = BackgroundTasks()
    fallback_called = []
    scope = SupportScope(
        actor="staff@rubik-soft.com",
        sid="support-sid",
        workspace_slug="acme",
        workspace_name="Acme",
    )
    with use_support_session(scope):
        with pytest.raises(HTTPException) as run_error:
            launcher.run_job_request("cubie-worker", {})
        with pytest.raises(HTTPException) as module_error:
            launcher.launch_module_execution("backend.jobs.crm_sync_job", [])
        with pytest.raises(HTTPException) as fallback_error:
            launcher.submit_background_work(
                tasks,
                module="backend.jobs.crm_sync_job",
                args=[],
                fallback=lambda: fallback_called.append(True),
                label="support-test",
            )
    assert [
        run_error.value.status_code,
        module_error.value.status_code,
        fallback_error.value.status_code,
    ] == [403, 403, 403]
    assert tasks.tasks == []
    assert fallback_called == []


def test_support_background_routes_refuse_before_pending_rows(workspaces, monkeypatch):
    from types import SimpleNamespace

    from backend.routers import home, xray
    from rubik_core.tools import _workspace_auth

    def unexpected_pending(*_args, **_kwargs):
        raise AssertionError("support request created a pending background row")

    monkeypatch.setattr(home.run_state, "create_run", unexpected_pending)
    monkeypatch.setattr(xray.service, "create_pending_deep", unexpected_pending)
    monkeypatch.setattr(xray.service, "deep_runner_enabled", lambda: True)
    monkeypatch.setattr(xray, "_DEEP_POOL", SimpleNamespace(submit=unexpected_pending))
    monkeypatch.setattr(
        _workspace_auth, "load_workspace_credentials", unexpected_pending
    )
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        headers = {"Authorization": f"Bearer {token}"}
        priorities = client.post(
            "/api/homefeed/priorities",
            headers=headers,
            json={"force_refresh": True},
        )
        xray_deep = client.post("/api/xray/acme/deep", headers=headers, json={})
    assert priorities.status_code == 403, priorities.text
    assert xray_deep.status_code == 403, xray_deep.text
    rows = _audit_rows("acme")
    assert [row.details["status_code"] for row in rows] == [403, 403]
    assert all(row.status == "failure" for row in rows)


def test_support_meeting_prep_get_cannot_start_background_build(
    workspaces, monkeypatch
):
    from rubik_core.meeting_prep import background

    def unexpected_build(*_args, **_kwargs):
        raise AssertionError("support GET started a background build")

    monkeypatch.setattr(background, "request_build", unexpected_build)
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        response = client.get(
            "/api/homefeed/meeting-prep/missing-event",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403, response.text
    assert _audit_rows("acme") == []


def test_support_jobs_and_agent_scheduler_routes_refuse_before_work(
    workspaces, monkeypatch
):
    from backend.agents.overnight import OvernightAgent
    from backend.routers import jobs

    def unexpected_job(*_args, **_kwargs):
        raise AssertionError("support launched a Job")

    monkeypatch.setattr(jobs, "_launch_priorities_job_execution", unexpected_job)
    monkeypatch.setattr(jobs, "_run_pipeline_sync", unexpected_job)
    monkeypatch.setattr(OvernightAgent, "run", unexpected_job)
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        headers = {"Authorization": f"Bearer {token}"}
        priorities = client.post("/api/jobs/priorities/run-all", headers=headers)
        overnight = client.post("/api/agents/overnight/run", headers=headers)
        briefing = client.post(
            "/api/jobs/overnight-briefing",
            headers=headers,
            json={"target_email": "employee@acme.example"},
        )
    assert priorities.status_code == 403, priorities.text
    assert overnight.status_code == 403, overnight.text
    assert briefing.status_code == 403, briefing.text
    rows = _audit_rows("acme")
    assert [row.details["status_code"] for row in rows] == [403, 403, 403]


def test_support_user_create_refuses_before_roster_write(workspaces):
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        response = client.post(
            "/api/users/",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "new@acme.example", "name": "New User"},
        )
    assert response.status_code == 403, response.text
    with use_workspace("acme"):
        with SessionLocal() as db:
            assert db.get(User, "new@acme.example") is None
    assert _audit_rows("acme")[0].details["status_code"] == 403


def test_support_eos_ingest_write_refuses_before_row_creation(workspaces):
    from rubik_core.control.models import WorkspaceModule

    with control_db.control_session() as control:
        control.add(WorkspaceModule(workspace_slug="acme", module="eos", enabled=True))
        control.commit()
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        response = client.post(
            "/api/eos/processes/",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "Must not be created"},
        )
    assert response.status_code == 403, response.text
    with use_workspace("acme"):
        from rubik_core.database.eos import EosProcess

        with SessionLocal() as db:
            assert db.query(EosProcess).count() == 0
    assert _audit_rows("acme")[0].details["status_code"] == 403


def test_support_direct_dam_upload_refuses_before_asset_gcs_or_fallback(
    workspaces, monkeypatch
):
    import asyncio
    import io
    from types import SimpleNamespace

    from fastapi import BackgroundTasks, HTTPException, UploadFile

    from backend.routers import dam
    from rubik_core.config import AppMode
    from rubik_core.tenancy.support_context import SupportScope, use_support_session

    side_effects = []
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(dam.dam_registry, "get_asset_by_content_hash", lambda *_: None)
    monkeypatch.setattr(
        dam.dam_registry,
        "register_asset",
        lambda *_: side_effects.append("asset"),
    )
    fake_processor = SimpleNamespace(
        processor=SimpleNamespace(
            loader=SimpleNamespace(generate_doc_id=lambda *_: "doc-1"),
            vector_store=SimpleNamespace(
                upload_to_gcs=lambda *_: side_effects.append("gcs")
            ),
        )
    )
    background_tasks = BackgroundTasks()
    scope = SupportScope(
        actor="staff@rubik-soft.com",
        sid="support-sid",
        workspace_slug="acme",
        workspace_name="Acme",
    )
    with use_workspace("acme"), use_support_session(scope):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(
                dam.upload_document(
                    background_tasks=background_tasks,
                    file=UploadFile(file=io.BytesIO(b"report"), filename="report.txt"),
                    topic="Test",
                    department="general",
                    description="",
                    tags="",
                    is_company_wide=False,
                    force=False,
                    replace_doc_id="",
                    user=SimpleNamespace(email=scope.actor, is_admin=True),
                    file_processor=fake_processor,
                )
            )
    assert refused.value.status_code == 403
    assert side_effects == []
    assert background_tasks.tasks == []


def test_support_chat_file_publish_refuses_before_dam_side_effects(
    workspaces, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from fastapi import BackgroundTasks

    from backend.routers import chat_files, dam, ingest

    side_effects = []
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(
        chat_files.service,
        "owned",
        lambda *_: (
            SimpleNamespace(id="private-1", storage_uri="private://1", summary=""),
        ),
    )
    monkeypatch.setattr(chat_files.storage, "load_bytes", lambda *_: b"report")
    monkeypatch.setattr(
        chat_files.service,
        "publish_payload",
        lambda *_: ("report.txt", b"report", "original"),
    )
    monkeypatch.setattr(
        chat_files.service, "record_publication", lambda *_a, **_k: None
    )
    monkeypatch.setattr(chat_files, "record_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(ingest, "get_processor", lambda: object())
    monkeypatch.setattr(
        dam,
        "get_file_processor",
        lambda *_: SimpleNamespace(
            processor=SimpleNamespace(
                loader=SimpleNamespace(generate_doc_id=lambda *_: "doc-1"),
                vector_store=SimpleNamespace(
                    upload_to_gcs=lambda *_: side_effects.append("gcs")
                ),
            )
        ),
    )
    monkeypatch.setattr(dam.dam_registry, "get_asset_by_content_hash", lambda *_: None)
    monkeypatch.setattr(
        dam.dam_registry,
        "register_asset",
        lambda *_: side_effects.append("asset"),
    )
    monkeypatch.setattr(
        BackgroundTasks,
        "add_task",
        lambda *_a, **_k: side_effects.append("fallback"),
    )
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        response = client.post(
            "/api/chat/files/private-1/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={"topic": "Test", "department": "general"},
        )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == (
        "Platform support sessions cannot start background work."
    )
    assert side_effects == []
    assert not (tmp_path / "files").exists()
    rows = _audit_rows("acme")
    assert len(rows) == 1
    assert rows[0].details["status_code"] == 403


def test_support_identity_cannot_be_added_to_group(workspaces):
    from rubik_core.database.models import Group

    with use_workspace("acme"):
        with SessionLocal() as db:
            db.add(Group(id="group-1", name="A group"))
            db.commit()
    with TestClient(app) as client:
        token = _enter(client, workspaces)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post(
            "/api/groups/group-1/members",
            headers=headers,
            json={"email": "staff@rubik-soft.com"},
        )
        me = client.get("/api/users/me", headers=headers)
    assert response.status_code == 409, response.text
    assert me.status_code == 200, me.text
    with use_workspace("acme"):
        with SessionLocal() as db:
            row = db.get(User, "staff@rubik-soft.com")
            assert row.groups == []
    assert _audit_rows("acme")[0].details["status_code"] == 409

    # Existing bad memberships can still be removed by a normal admin path.
    from rubik_core.database.repository import SQLMetadataRegistry

    with use_workspace("acme"):
        with SessionLocal() as db:
            row = db.get(User, "staff@rubik-soft.com")
            row.groups.append(db.get(Group, "group-1"))
            db.commit()
        assert SQLMetadataRegistry().remove_user_from_group(
            "staff@rubik-soft.com", "group-1"
        )
        with SessionLocal() as db:
            assert db.get(User, "staff@rubik-soft.com").groups == []


@pytest.mark.parametrize(
    ("email", "sup", "sid"),
    [
        (None, "staff@rubik-soft.com", "sid"),
        (42, "staff@rubik-soft.com", "sid"),
        ("staff@rubik-soft.com", 42, "sid"),
        ("staff@rubik-soft.com", "staff@rubik-soft.com", 42),
    ],
)
def test_support_token_bad_identity_types_raise_value_error(
    workspaces, email, sup, sid
):
    with use_workspace("rubiksoft"):
        with pytest.raises(ValueError):
            mint_session_token(
                email,
                "acme",
                typ="support",
                sup=sup,
                sid=sid,
                hours=0.25,
            )
