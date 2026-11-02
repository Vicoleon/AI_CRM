"""Workspace scope survives raw thread hops and context-free buffer drains."""

from __future__ import annotations

import ast
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.cost import usage
from rubik_core.database.insights import ActivityDaily
from rubik_core.database.models import Base, LLMUsageEvent
from rubik_core.insights import activity
from rubik_core.tenancy import context as tenancy_context
from rubik_core.tenancy.context import current_workspace, use_workspace


@pytest.fixture
def tenant_sessions(tmp_path, monkeypatch):
    """Use independent SQLite files and a context-bound session factory."""
    from rubik_core.database import db as dbmod

    engines = {
        slug: create_engine(f"sqlite:///{tmp_path / f'{slug}.db'}")
        for slug in ("alpha", "beta")
    }
    for engine in engines.values():
        Base.metadata.create_all(engine)
    factories = {slug: sessionmaker(bind=engine) for slug, engine in engines.items()}
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: factories[current_workspace()]())
    monkeypatch.setattr(usage, "_FLUSH_THRESHOLD", 1000)
    monkeypatch.setattr(activity, "_FLUSH_THRESHOLD", 1000)
    with usage._LOCK:
        usage._BUFFER.clear()
        usage._last_flush_monotonic = usage.time.monotonic()
    activity.reset_buffer()
    activity._last_flush_monotonic = activity.time.monotonic()
    try:
        yield factories
    finally:
        with usage._LOCK:
            usage._BUFFER.clear()
        activity.reset_buffer()
        for engine in engines.values():
            engine.dispose()


def test_context_helpers_carry_workspace_into_threads(monkeypatch):
    from rubik_core.tenancy.threads import (
        ContextThreadPoolExecutor,
        context_thread,
        submit_with_context,
    )

    monkeypatch.setattr(tenancy_context, "_process_workspace", None)
    with use_workspace("alpha"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(tenancy_context.NoWorkspaceContext):
                pool.submit(current_workspace).result()
            assert submit_with_context(pool, current_workspace).result() == "alpha"
        with ContextThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(current_workspace).result() == "alpha"
        seen: list[str] = []
        thread = context_thread(
            lambda: seen.append(current_workspace()), name="scope-test"
        )
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert seen == ["alpha"]


def test_slack_parallel_name_lookup_keeps_workspace(monkeypatch):
    from backend.services.slack_service import SlackService

    monkeypatch.setattr(tenancy_context, "_process_workspace", None)
    seen: list[tuple[str, str]] = []
    service = object.__new__(SlackService)
    with use_workspace("alpha"):
        service._prefetch_names(
            lambda item: seen.append((item, current_workspace())), ["U1", "U2"]
        )
    assert sorted(seen) == [("U1", "alpha"), ("U2", "alpha")]


def test_priorities_timeout_worker_keeps_workspace(monkeypatch):
    from backend.routers import jobs

    monkeypatch.setattr(tenancy_context, "_process_workspace", None)
    monkeypatch.setattr(
        jobs, "_run_priorities_for_user", lambda _email: current_workspace()
    )
    with use_workspace("alpha"):
        assert jobs._run_user_with_timeout("rep@alpha.example") == "alpha"


def test_thread_hop_inventory_has_no_raw_worker_construction():
    root = Path(__file__).resolve().parents[2]
    paths = (
        "backend/routers/xray.py",
        "backend/routers/jobs.py",
        "backend/services/slack_service.py",
        "rubik_core/meeting_prep/background.py",
        "rubik_core/adk_agent/tools.py",
        "rubik_core/ingestion_council/council.py",
        "rubik_core/priorities/orchestrator.py",
        "rubik_core/priorities/batch.py",
        "rubik_core/xray/orchestrator.py",
        "rubik_core/database/migration_preflight.py",
    )
    for path in paths:
        tree = ast.parse((root / path).read_text())
        raw_calls = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
            in {"ThreadPoolExecutor", "Thread"}
        ]
        assert raw_calls == [], f"{path} starts raw workers at {raw_calls}"


def test_context_free_flush_never_mixes_cost_rows(tenant_sessions):
    with use_workspace("alpha"):
        usage.record_usage(
            operation="generate",
            model="gemini-2.5-pro",
            prompt_tokens=11,
            feature="alpha-feature",
        )
    with use_workspace("beta"):
        usage.record_usage(
            operation="generate",
            model="gemini-2.5-pro",
            prompt_tokens=23,
            feature="beta-feature",
        )

    written: list[int] = []
    thread = threading.Thread(target=lambda: written.append(usage.flush_usage()))
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert written == [2]
    for slug, feature in (("alpha", "alpha-feature"), ("beta", "beta-feature")):
        with tenant_sessions[slug]() as db:
            assert [row.feature for row in db.query(LLMUsageEvent).all()] == [feature]


def test_support_request_flushes_other_workspace_buffers_without_support_scope(
    tenant_sessions, monkeypatch
):
    from rubik_core.database import db as dbmod
    from rubik_core.tenancy.support_context import (
        SupportScope,
        current_support_session,
        use_support_session,
    )

    original = dbmod.SessionLocal

    def support_guarded_session():
        support = current_support_session()
        if support and support.workspace_slug != current_workspace():
            raise PermissionError("support scope cannot open another workspace")
        return original()

    monkeypatch.setattr(dbmod, "SessionLocal", support_guarded_session)
    monkeypatch.setattr(activity, "business_tz", lambda: ZoneInfo("UTC"))
    for slug in ("alpha", "beta"):
        with use_workspace(slug):
            usage.record_usage(operation="generate", model="test", prompt_tokens=1)
            activity.record_activity("rep@example.com", "chat")

    support = SupportScope("operator@example.com", "sid", "alpha", "Alpha")
    with use_support_session(support), use_workspace("alpha"):
        assert usage.flush_usage() == 2
        assert activity.flush_activity() == 2

    for slug in ("alpha", "beta"):
        with tenant_sessions[slug]() as db:
            assert db.query(LLMUsageEvent).count() == 1
            assert db.query(ActivityDaily).count() == 1


def test_one_unavailable_workspace_does_not_drop_another_cost_batch(
    tenant_sessions, monkeypatch
):
    from rubik_core.database import db as dbmod

    with use_workspace("alpha"):
        usage.record_usage(
            operation="generate", model="gemini-2.5-pro", prompt_tokens=11
        )
    with use_workspace("beta"):
        usage.record_usage(
            operation="generate", model="gemini-2.5-pro", prompt_tokens=23
        )
    original = dbmod.SessionLocal

    def sometimes_unavailable():
        if current_workspace() == "alpha":
            raise RuntimeError("alpha unavailable")
        return original()

    monkeypatch.setattr(dbmod, "SessionLocal", sometimes_unavailable)
    assert usage.flush_usage() == 1
    with tenant_sessions["alpha"]() as db:
        assert db.query(LLMUsageEvent).count() == 0
    with tenant_sessions["beta"]() as db:
        assert db.query(LLMUsageEvent).count() == 1


def test_context_free_flush_keeps_activity_days_and_rows_separate(
    tenant_sessions, monkeypatch
):
    monkeypatch.setattr(
        activity,
        "business_tz",
        lambda: ZoneInfo(
            {"alpha": "America/Chicago", "beta": "UTC"}[current_workspace()]
        ),
    )
    at = datetime(2026, 10, 8, 3, 0)
    with use_workspace("alpha"):
        activity.record_activity("rep@example.com", "chat", at=at)
    with use_workspace("beta"):
        activity.record_activity("rep@example.com", "chat", at=at)

    written: list[int] = []
    thread = threading.Thread(target=lambda: written.append(activity.flush_activity()))
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert written == [2]
    for slug, day in (("alpha", date(2026, 10, 7)), ("beta", date(2026, 10, 8))):
        with tenant_sessions[slug]() as db:
            assert [
                (row.user_email, row.day) for row in db.query(ActivityDaily).all()
            ] == [("rep@example.com", day)]


def test_recorders_report_missing_workspace_without_buffering(monkeypatch, caplog):
    monkeypatch.setattr(tenancy_context, "_process_workspace", None)
    with usage._LOCK:
        usage._BUFFER.clear()
    activity.reset_buffer()
    with caplog.at_level(logging.WARNING):
        contextvars.Context().run(
            usage.record_usage,
            operation="generate",
            model="gemini-2.5-pro",
            prompt_tokens=10,
        )
        contextvars.Context().run(activity.record_activity, "rep@example.com", "chat")
    assert usage._BUFFER == {}
    assert activity._BUFFER == {}
    assert "workspace" in caplog.text.lower()
