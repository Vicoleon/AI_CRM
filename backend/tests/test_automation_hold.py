"""An operator cost hold must stop workers before billing or inline fallback."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks, HTTPException, Response
from fastapi.testclient import TestClient

from backend.jobs import bootstrap, launcher
from backend.main import app
from rubik_core.automation import AutomationsPausedError, automations_enabled
from rubik_core.config import AppMode, settings


@pytest.fixture(autouse=True)
def held(monkeypatch):
    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", "false")


@pytest.mark.parametrize("raw", ["false", "0", "off", "", "malformed"])
def test_explicit_hold_is_fail_closed(monkeypatch, raw):
    monkeypatch.setattr(settings, "CUBIE_AUTOMATIONS_ENABLED", True)
    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", raw)
    assert automations_enabled() is False


def test_existing_configuration_is_compatible_without_operator_override(monkeypatch):
    monkeypatch.delenv("CUBIE_AUTOMATIONS_ENABLED")
    monkeypatch.setattr(settings, "CUBIE_AUTOMATIONS_ENABLED", True)
    assert automations_enabled() is True
    monkeypatch.setattr(settings, "CUBIE_AUTOMATIONS_ENABLED", False)
    assert automations_enabled() is False


def test_direct_job_launch_refuses_before_workspace_or_cloud_auth(monkeypatch):
    workspace = Mock(side_effect=AssertionError("workspace lookup must not happen"))
    monkeypatch.setattr(launcher, "_with_workspace_override", workspace)
    with pytest.raises(AutomationsPausedError):
        launcher.run_job_request("cubie-priorities-batch", {})
    workspace.assert_not_called()


@pytest.mark.parametrize("mode", [AppMode.GCP, AppMode.LOCAL])
def test_hold_never_queues_inline_fallback_or_cloud_execution(monkeypatch, mode):
    monkeypatch.setattr(settings, "APP_MODE", mode)
    launch = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(launcher, "launch_module_execution", launch)
    tasks = BackgroundTasks()
    fallback = Mock()

    with pytest.raises(HTTPException) as error:
        launcher.submit_background_work(
            tasks,
            module="backend.jobs.priorities_on_demand_job",
            args=[],
            fallback=fallback,
            label="held-priorities",
        )

    assert error.value.status_code == 503
    assert tasks.tasks == []
    launch.assert_not_called()
    fallback.assert_not_called()


def test_launch_time_hold_is_not_treated_as_cloud_failure(monkeypatch):
    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", "true")
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(
        launcher,
        "launch_module_execution",
        Mock(side_effect=AutomationsPausedError("Automatic processing is paused")),
    )
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as error:
        launcher.submit_background_work(
            tasks,
            module="backend.jobs.priorities_on_demand_job",
            args=[],
            fallback=Mock(),
            label="held-during-dispatch",
        )
    assert error.value.status_code == 503
    assert tasks.tasks == []


def test_held_worker_exits_successfully_without_workspace_lookup(monkeypatch):
    lookup = Mock(side_effect=AssertionError("must not open workspace"))
    monkeypatch.setattr(bootstrap, "bootstrap_process_workspace", lookup)
    with pytest.raises(SystemExit) as stopped:
        bootstrap.bootstrap_job_workspace()
    assert stopped.value.code == 0
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "module",
    [
        "rubik_core.priorities.batch_runner",
        "backend.jobs.priorities_on_demand_job",
        "backend.jobs.fathom_sync_job",
        "backend.jobs.dam_ingest_job",
    ],
)
def test_real_job_entrypoints_do_no_work_while_held(monkeypatch, module):
    lookup = Mock(side_effect=AssertionError("must not open workspace"))
    monkeypatch.setattr(bootstrap, "bootstrap_process_workspace", lookup)
    with pytest.raises(SystemExit) as stopped:
        importlib.import_module(module).main([])
    assert stopped.value.code == 0
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs/priorities/run-all",
        "/api/jobs/priorities/run-next-week",
        "/api/agents/overnight/run",
        "/api/agents/priorities/deliver-slack",
        "/api/agents/scheduled-tasks/tick",
    ],
)
def test_scheduler_routes_refuse_before_pipeline_dispatch(monkeypatch, path):
    dispatch = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr("backend.routers.jobs.run_job_request", dispatch)
    response = TestClient(app).post(path, headers={"X-Cubie-Workspace": "test"})
    assert response.status_code == 503, response.text
    dispatch.assert_not_called()


def test_home_priorities_miss_refuses_before_pending_run(monkeypatch):
    from backend.routers import home

    create = Mock(side_effect=AssertionError("must not create run"))
    monkeypatch.setattr(home.run_state, "create_run", create)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            home.generate_priorities(
                BackgroundTasks(),
                Response(),
                body=home.PrioritiesRequestBody(force_refresh=True),
                user=SimpleNamespace(email="owner@acme.example"),
            )
        )
    assert error.value.status_code == 503
    create.assert_not_called()


def test_home_priorities_still_returns_existing_card(monkeypatch):
    from backend.routers import home

    cached = {
        "generated_at": "2026-11-06T12:00:00Z",
        "headline": "Existing priorities",
        "priorities": [],
        "revision_count": 0,
    }
    monkeypatch.setattr(home, "_load_today_priorities", lambda email: cached)
    create = Mock(side_effect=AssertionError("must not create run"))
    monkeypatch.setattr(home.run_state, "create_run", create)
    response = asyncio.run(
        home.generate_priorities(
            BackgroundTasks(),
            Response(),
            user=SimpleNamespace(email="owner@acme.example"),
        )
    )
    assert response.headline == "Existing priorities"
    create.assert_not_called()


def test_more_priorities_miss_refuses_before_pending_run(monkeypatch):
    from backend.routers import home

    monkeypatch.setattr(home, "_fetch_other_recommendations", lambda *a: [])
    create = Mock(side_effect=AssertionError("must not create run"))
    monkeypatch.setattr(home.run_state, "create_run", create)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            home.get_more_priorities(
                BackgroundTasks(), user=SimpleNamespace(email="owner@acme.example")
            )
        )
    assert error.value.status_code == 503
    create.assert_not_called()


def test_briefing_keeps_deterministic_mail_without_ai_triage(monkeypatch):
    from backend.routers import home

    email = home.EmailSummary(
        thread_id="work-thread",
        subject="Work update",
        sender="colleague@acme.example",
        snippet="A normal message",
        date="2026-11-06T12:00:00Z",
        is_unread=True,
    )
    monkeypatch.setattr(home, "_fetch_emails", lambda _: [object()])
    monkeypatch.setattr(home, "_fetch_calendar", lambda _: {})
    monkeypatch.setattr(home, "_parse_email_summary", lambda *a: email)
    monkeypatch.setattr(home, "_compute_free_slots", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.routers.calendar._user_working_hours", lambda _: (None, None)
    )
    triage = AsyncMock(side_effect=AssertionError("must not call model"))
    monkeypatch.setattr(home, "_flag_important_emails", triage)

    response = asyncio.run(
        home.generate_briefing(
            SimpleNamespace(email="owner@acme.example", name="Owner")
        )
    )
    assert [row.thread_id for row in response.emails] == ["work-thread"]
    triage.assert_not_awaited()


def test_meeting_prep_hold_never_falls_back_to_pool(monkeypatch):
    from rubik_core.meeting_prep import background

    submit = Mock(side_effect=AssertionError("must not submit"))
    monkeypatch.setattr(background._POOL, "submit", submit)
    with pytest.raises(AutomationsPausedError):
        background.dispatch(
            run_id="run",
            user_email="owner@acme.example",
            event_id="event",
            focus_on=None,
            keep_edits=True,
        )
    submit.assert_not_called()


def test_meeting_prep_hold_precedes_database_rows():
    from rubik_core.meeting_prep import background

    db = Mock()
    with pytest.raises(AutomationsPausedError):
        background.request_build(
            db,
            user_email="owner@acme.example",
            event_id="event",
            kind=background.KIND_FIRST,
        )
    assert db.mock_calls == []


def test_meeting_prep_launch_time_hold_never_falls_back(monkeypatch):
    from rubik_core.meeting_prep import background

    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", "true")
    monkeypatch.setattr(background, "runner_mode", lambda: "job")
    monkeypatch.setattr(
        "backend.routers.home._launch_meeting_prep_job_execution",
        Mock(side_effect=AutomationsPausedError("Automatic processing is paused")),
    )
    submit = Mock(side_effect=AssertionError("must not submit"))
    monkeypatch.setattr(background._POOL, "submit", submit)
    with pytest.raises(AutomationsPausedError):
        background.dispatch(
            run_id="run",
            user_email="owner@acme.example",
            event_id="event",
            focus_on=None,
            keep_edits=True,
        )
    submit.assert_not_called()


def test_dam_hold_precedes_staging_and_inline_fallback(monkeypatch):
    from backend.routers import dam

    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as error:
        dam._launch_version_ingest(
            tasks,
            None,
            asset=None,
            file_path=Path("unused"),
            cleanup_dir=None,
            actor_email="owner@acme.example",
            version_number=2,
            supersede_source_path=None,
            already_staged=False,
        )
    assert error.value.status_code == 503
    assert tasks.tasks == []


def test_xray_hold_precedes_ownership_lookup_and_pending_row(monkeypatch):
    from backend.routers import xray

    ownership = AsyncMock(side_effect=AssertionError("must not fetch CRM"))
    pending = Mock(side_effect=AssertionError("must not create snapshot"))
    monkeypatch.setattr(xray, "_require_owned_company", ownership)
    monkeypatch.setattr(xray.service, "create_pending_deep", pending)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            xray.generate_deep(
                "company", user=SimpleNamespace(email="owner@acme.example")
            )
        )
    assert error.value.status_code == 503
    ownership.assert_not_awaited()
    pending.assert_not_called()


@pytest.mark.parametrize(
    ("module", "factory"),
    [
        ("rubik_core.scripts.sync_meet_transcripts", "get_processor"),
        ("rubik_core.scripts.transcript_sync", "_build_processor"),
    ],
)
def test_transcript_sync_hold_precedes_processor_or_database(
    monkeypatch, module, factory
):
    script = importlib.import_module(module)
    build = Mock(side_effect=AssertionError("must not build AI processor"))
    database = Mock(side_effect=AssertionError("must not open tenant database"))
    monkeypatch.setattr(script, factory, build)
    monkeypatch.setattr(script, "SessionLocal", database)
    with pytest.raises(AutomationsPausedError):
        script.run_sync()
    build.assert_not_called()
    database.assert_not_called()


def test_smart_teams_completion_keeps_interview_open(monkeypatch):
    from backend.routers import smart_teams

    row = SimpleNamespace(status="in_progress", completed_at=None)
    monkeypatch.setattr(smart_teams, "_open_interview", lambda *a: row)
    db = Mock()
    with pytest.raises(HTTPException) as error:
        smart_teams.complete_interview(
            BackgroundTasks(), user=SimpleNamespace(email="owner@acme.example"), db=db
        )
    assert error.value.status_code == 503
    assert row.status == "in_progress"
    assert row.completed_at is None
    assert db.mock_calls == []


def test_smart_teams_completion_existing_state_still_reads(monkeypatch):
    from backend.routers import smart_teams

    monkeypatch.setattr(smart_teams, "_open_interview", lambda *a: None)
    monkeypatch.setattr(
        smart_teams, "_completion_state", lambda *a: {"status": "ready"}
    )
    result = smart_teams.complete_interview(
        BackgroundTasks(), user=SimpleNamespace(email="owner@acme.example"), db=Mock()
    )
    assert result == {"status": "ready", "enqueued": False}


def test_smart_teams_upload_precedes_bytes_and_row(monkeypatch):
    from backend.routers import smart_teams_uploads

    upload = Mock()
    create = Mock(side_effect=AssertionError("must not persist upload"))
    monkeypatch.setattr(smart_teams_uploads.up, "create_upload", create)
    with pytest.raises(HTTPException) as error:
        smart_teams_uploads.create_own_upload(
            BackgroundTasks(),
            file=upload,
            user=SimpleNamespace(email="owner@acme.example"),
            db=Mock(),
        )
    assert error.value.status_code == 503
    upload.file.read.assert_not_called()
    create.assert_not_called()


@pytest.mark.parametrize("pair", [False, True])
def test_smart_teams_generation_hold_precedes_document_row(monkeypatch, pair):
    from backend.routers import smart_teams_comparison, smart_teams_guides

    db = Mock()
    with pytest.raises(HTTPException) as error:
        if pair:
            smart_teams_comparison.generate_pair_guide(
                "colleague@acme.example",
                BackgroundTasks(),
                user=SimpleNamespace(email="owner@acme.example"),
                db=db,
            )
        else:
            smart_teams_guides.generate_guide(
                BackgroundTasks(),
                user=SimpleNamespace(email="owner@acme.example"),
                db=db,
            )
    assert error.value.status_code == 503
    assert db.mock_calls == []


@pytest.mark.parametrize("published", [False, True])
def test_smart_teams_held_publication_keeps_consent_unchanged(monkeypatch, published):
    from backend.routers import smart_teams_guides

    document = SimpleNamespace(
        kind=smart_teams_guides.guide.KIND_PERSONAL,
        subject_key="owner@acme.example",
        published=published,
        rag_ingested=True,
    )
    db = Mock()
    db.get.return_value = document
    user = SimpleNamespace(email="owner@acme.example")
    monkeypatch.setattr(smart_teams_guides.authz, "is_subject", lambda *a: True)
    with pytest.raises(HTTPException) as error:
        if published:
            smart_teams_guides.unpublish_guide(
                "guide", BackgroundTasks(), user=user, db=db
            )
        else:
            smart_teams_guides.approve_guide(
                "guide", BackgroundTasks(), user=user, db=db
            )
    assert error.value.status_code == 503
    assert document.published is published
    db.commit.assert_not_called()
    db.add.assert_not_called()


def test_dam_restore_precedes_file_materialization_and_version(monkeypatch):
    from backend.routers import dam

    materialize = Mock(side_effect=AssertionError("must not download old bytes"))
    add = Mock(side_effect=AssertionError("must not publish a processing version"))
    monkeypatch.setattr(dam, "materialize", materialize)
    monkeypatch.setattr(dam.dam_registry, "add_version", add)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            dam.restore_version(
                "doc", 1, BackgroundTasks(), user=Mock(), file_processor=Mock()
            )
        )
    assert error.value.status_code == 503
    materialize.assert_not_called()
    add.assert_not_called()


def test_crm_sync_hold_precedes_pending_run(monkeypatch):
    from backend.routers import crm_sync

    create = Mock(side_effect=AssertionError("must not create run"))
    monkeypatch.setattr(crm_sync.run_state, "create_run", create)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            crm_sync.start_scan(
                BackgroundTasks(), user=SimpleNamespace(email="owner@acme.example")
            )
        )
    assert error.value.status_code == 503
    create.assert_not_called()


def test_deep_prep_hold_precedes_queued_row(monkeypatch):
    from backend.routers import home

    monkeypatch.setattr(settings, "CUBIE_DEEP_PREP_ENABLED", True)
    db = Mock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(home.queue_deep_meeting_prep("event", user=Mock(), db=db))
    assert error.value.status_code == 503
    assert db.mock_calls == []


def test_embedding_hold_precedes_rebuild_queue(monkeypatch):
    from backend.routers import ai_providers
    from rubik_core.ai_providers import cloud_embedding_workflow

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    queue = Mock(side_effect=AssertionError("must not create rebuild"))
    monkeypatch.setattr(
        cloud_embedding_workflow, "queue_cloud_embedding_rebuild", queue
    )
    with pytest.raises(HTTPException) as error:
        ai_providers.prepare_embedding_index_route(
            ai_providers.EmbeddingPrepareRequest(provider="openai", model="embedding"),
            user=Mock(),
        )
    assert error.value.status_code == 503
    queue.assert_not_called()


def test_scheduled_run_now_hold_precedes_dispatch_and_run_row(monkeypatch):
    from backend.routers import scheduled_tasks

    dispatch = Mock(side_effect=AssertionError("must not dispatch or create run"))
    monkeypatch.setattr(scheduled_tasks.dispatch, "dispatch_task_now", dispatch)
    db = Mock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(scheduled_tasks.run_scheduled_task_now("task", user=Mock(), db=db))
    assert error.value.status_code == 503
    dispatch.assert_not_called()
    assert db.mock_calls == []
