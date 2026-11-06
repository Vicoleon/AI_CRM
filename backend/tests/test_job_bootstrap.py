"""Every Cloud Run Job launch carries one explicit workspace."""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from backend.jobs import launcher
from rubik_core.config import AppMode, settings
from rubik_core.tenancy import context
from rubik_core.tenancy.context import use_workspace


def _capture_launch(monkeypatch):
    captured = []
    monkeypatch.setattr(launcher, "gcp_project_id", lambda: "test-project")

    class Credentials:
        token = "test-token"

        def refresh(self, _request):
            pass

    monkeypatch.setattr("google.auth.compute_engine.Credentials", Credentials)
    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: object())

    def post(url, *, json, headers, timeout):
        captured.append((url, json, headers, timeout))
        return SimpleNamespace(status_code=200, json=lambda: {"name": "job-1"})

    monkeypatch.setattr("requests.post", post)
    return captured


def test_every_job_launch_carries_current_workspace(monkeypatch):
    captured = _capture_launch(monkeypatch)
    with use_workspace("alpha"):
        assert launcher.run_job_request("cubie-worker", {}) == {"name": "job-1"}
    [(_, body, _, _)] = captured
    overrides = body["overrides"]["containerOverrides"]
    assert overrides[0]["env"] == [{"name": "CUBIE_WORKSPACE", "value": "alpha"}]


def test_conflicting_workspace_override_is_refused_before_http(monkeypatch):
    captured = _capture_launch(monkeypatch)
    body = {
        "overrides": {
            "containerOverrides": [
                {"env": [{"name": "CUBIE_WORKSPACE", "value": "beta"}]}
            ]
        }
    }
    with use_workspace("alpha"):
        with pytest.raises(launcher.JobLaunchError, match="workspace"):
            launcher.run_job_request("cubie-worker", body)
    assert captured == []


def test_xray_direct_job_launcher_uses_shared_workspace_bound_post(monkeypatch):
    from backend.routers.xray import _launch_xray_deep_job_execution

    captured = _capture_launch(monkeypatch)
    with use_workspace("alpha"):
        _launch_xray_deep_job_execution(
            snapshot_id="snap1",
            company_id="co1",
            user_email="owner@alpha.example",
            focus="",
            company_name="Client",
            company_domain="client.example",
        )
    [(url, body, _, _)] = captured
    assert url.endswith("/jobs/cubie-xray-deep:run")
    assert {"name": "CUBIE_WORKSPACE", "value": "alpha"} in body["overrides"][
        "containerOverrides"
    ][0]["env"]


def test_missing_workspace_refuses_before_cloud_auth(monkeypatch):
    captured = _capture_launch(monkeypatch)
    monkeypatch.setattr(context, "_process_workspace", None)
    with pytest.raises(launcher.JobLaunchError, match="workspace"):
        launcher.run_job_request("cubie-worker", {})
    assert captured == []


_JOB_MAINS = [
    "close_date_rollup",
    "crm_sync_job",
    "dam_ingest_job",
    "deep_prep_job",
    "eos_reingest_job",
    "fathom_sync_job",
    "graph_backfill_job",
    "graph_hubspot_sweep_job",
    "l10_calendar_reconcile_job",
    "l10_summary_email_job",
    "loop_closure_scan_job",
    "meeting_prep_job",
    "meeting_routine_fire_job",
    "offboard_sweep_job",
    "priorities_on_demand_job",
    "sales_kaizen",
    "scheduled_task_run_job",
    "smart_teams_comparison_job",
    "smart_teams_guide_job",
    "smart_teams_parse_job",
    "smart_teams_score_job",
    "smart_teams_skill_sweep_job",
    "stale_work_sweep_job",
    "xray_deep_job",
]


@pytest.mark.parametrize("module_name", _JOB_MAINS)
def test_job_main_refuses_missing_workspace_before_work(monkeypatch, module_name):
    monkeypatch.delenv("CUBIE_WORKSPACE", raising=False)
    monkeypatch.setattr(context, "_process_workspace", None)
    module = importlib.import_module(f"backend.jobs.{module_name}")
    assert module.main([]) == 2


def test_inline_fallback_keeps_its_workspace_after_request_scope(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    tasks = BackgroundTasks()
    seen: list[str] = []
    with use_workspace("alpha"):
        executor, _ = launcher.submit_background_work(
            tasks,
            module="backend.jobs.fathom_sync_job",
            args=[],
            fallback=lambda: seen.append(context.current_workspace()),
            label="scope-test",
        )
    assert executor == "inline"
    asyncio.run(tasks())
    assert seen == ["alpha"]


def test_inline_fallback_rejects_conflicting_workspace_before_queue(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    tasks = BackgroundTasks()
    with use_workspace("alpha"):
        with pytest.raises(launcher.JobLaunchError, match="Conflicting"):
            launcher.submit_background_work(
                tasks,
                module="backend.jobs.fathom_sync_job",
                args=[],
                fallback=lambda: None,
                label="scope-test",
                env={"CUBIE_WORKSPACE": "beta"},
            )
    assert tasks.tasks == []


_HAND_RUN_MAIN_FILES = [
    "backend/agents/overnight.py",
    "backend/agents/ingestion_council.py",
    "rubik_core/database/seed.py",
    "rubik_core/scripts/transcript_sync.py",
    "scripts/close_stale_l10_sessions.py",
    "scripts/grant_upload_access.py",
    "scripts/reindex_to_rag.py",
    "scripts/seed_eos_minimum.py",
    "scripts/seed_insights_demo.py",
    "scripts/sync_cloudsql_to_sqlite.py",
    "seed_rags.py",
]


@pytest.mark.parametrize("path", _HAND_RUN_MAIN_FILES)
def test_hand_run_main_bootstraps_before_work(path):
    source = (Path(__file__).resolve().parents[2] / path).read_text()
    tree = ast.parse(source)
    main = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
    )
    statements = main.body[1:] if ast.get_docstring(main) else main.body
    if path == "rubik_core/scripts/transcript_sync.py":
        # The global operator hold may exit successfully before any tenant
        # lookup. Once enabled, workspace binding must still be the first work.
        hold = statements[0]
        assert isinstance(hold, ast.If)
        assert isinstance(hold.test, ast.UnaryOp)
        assert isinstance(hold.test.op, ast.Not)
        assert isinstance(hold.test.operand, ast.Call)
        assert hold.test.operand.func.id == "automations_enabled"
        assert isinstance(hold.body[-1], ast.Return)
        statements = statements[1:]
    if path == "scripts/sync_cloudsql_to_sqlite.py":
        assert isinstance(statements[0], ast.Expr)
        assert statements[0].value.func.id == "_bootstrap_settings_for_cloud_sql"
        statements = statements[1:]
    assert isinstance(statements[0], ast.ImportFrom)
    assert any(alias.name == "require_cli_workspace" for alias in statements[0].names)
    assert isinstance(statements[1], ast.Expr)
    assert isinstance(statements[1].value, ast.Call)
    assert isinstance(statements[1].value.func, ast.Name)
    assert statements[1].value.func.id == "require_cli_workspace"


def test_cloudsql_snapshot_clears_local_overrides_before_control_lookup(monkeypatch):
    from scripts import sync_cloudsql_to_sqlite

    for name in ("APP_MODE", "DATABASE_URL", "CLOUDSQL_CONNECTION_NAME"):
        monkeypatch.setenv(name, "local-override")

    def reject_before_db():
        assert all(
            name not in __import__("os").environ
            for name in ("APP_MODE", "DATABASE_URL", "CLOUDSQL_CONNECTION_NAME")
        )
        raise RuntimeError("stopped before database access")

    monkeypatch.setattr(
        "rubik_core.tenancy.cli.require_cli_workspace", reject_before_db
    )
    with pytest.raises(RuntimeError, match="stopped before database access"):
        sync_cloudsql_to_sqlite.main()


def test_insights_demo_reset_uses_explicit_disposable_sqlite(monkeypatch, tmp_path):
    from rubik_core.company import domain
    from rubik_core.database import db as tenant_db
    from rubik_core.database.models import Base
    from scripts import seed_insights_demo

    target = tmp_path / "disposable.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{target}")
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.setattr("rubik_core.tenancy.cli.require_cli_workspace", lambda: "test")
    monkeypatch.setattr(domain, "workspace_domain", lambda: "example.test")
    monkeypatch.setattr(seed_insights_demo, "seed", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        tenant_db,
        "get_engine",
        lambda: pytest.fail("demo seed must not open a workspace engine"),
    )
    seen = []
    monkeypatch.setattr(
        Base.metadata, "drop_all", lambda *, bind: seen.append(str(bind.url))
    )
    monkeypatch.setattr(
        Base.metadata, "create_all", lambda *, bind: seen.append(str(bind.url))
    )
    monkeypatch.setattr("sys.argv", ["seed_insights_demo.py", "--reset"])

    seed_insights_demo.main()

    assert seen == [f"sqlite:///{target}"] * 2


def test_hand_run_command_without_workspace_exits_before_control_db(monkeypatch):
    from rubik_core.tenancy.cli import require_cli_workspace

    monkeypatch.delenv("CUBIE_WORKSPACE", raising=False)
    monkeypatch.setattr(context, "_process_workspace", None)
    with pytest.raises(SystemExit) as exc:
        require_cli_workspace()
    assert exc.value.code == 2


def test_only_shared_launcher_builds_cloud_run_job_post():
    root = Path(__file__).resolve().parents[2]
    builders = []
    for tree in (root / "backend", root / "rubik_core"):
        for path in tree.rglob("*.py"):
            if "tests" in path.parts:
                continue
            # Platform setup enables the Cloud Run API by this service name;
            # only an actual v2 Job endpoint constructs an execution request.
            if "https://run.googleapis.com/v2/projects/" in path.read_text():
                builders.append(path.relative_to(root).as_posix())
    assert builders == ["backend/jobs/launcher.py"]
