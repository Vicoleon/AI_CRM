"""The active inbox triage route stays visible when a selected provider fails."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from backend.routers.home import EmailSummary, _flag_important_emails
from rubik_core.ai_providers.config import ROUTABLE_FEATURES
from rubik_core.ai_providers.registry import configure_provider, set_model_route
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def email_workspace(tmp_path, monkeypatch):
    slug = "email-byok-status"
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    with control_session() as session:
        session.add(Workspace(slug=slug, name="Email BYOK Status", status="active"))
        session.commit()
    yield slug
    with control_session() as session:
        session.execute(
            delete(WorkspaceConnector).where(WorkspaceConnector.workspace_slug == slug)
        )
        session.delete(session.get(Workspace, slug))
        session.commit()


def test_inbox_triage_failed_selected_key_is_visible_without_vertex(
    email_workspace, monkeypatch, caplog
):
    from rubik_core.ai_providers import runtime

    assert "email_importance" in ROUTABLE_FEATURES
    assert "email_action_items" not in ROUTABLE_FEATURES
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("managed Vertex called"),
    )
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=401, text="PRIVATE-PROVIDER-DETAIL"
        ),
    )
    email = EmailSummary(
        thread_id="thread-1",
        subject="Please review",
        sender="colleague@example.com",
        snippet="Need your answer",
        date="2026-10-30T12:00:00Z",
        is_unread=True,
    )
    with use_workspace(email_workspace):
        configure_provider("openai", api_key="private-email-key")
        set_model_route("email_importance", "openai", "gpt-6-luna")
        asyncio.run(_flag_important_emails([email], "Jose"))
    assert email.may_be_important is False
    assert email.importance_unavailable is True
    assert "private-email-key" not in caplog.text
    assert "PRIVATE-PROVIDER-DETAIL" not in caplog.text
