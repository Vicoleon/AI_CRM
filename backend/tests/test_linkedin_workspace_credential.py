"""The licensed LinkedIn key belongs to one workspace; public search stays available."""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.people import config
from rubik_core.people.sources.base import resolve_source
from rubik_core.tenancy import context
from rubik_core.tenancy.secrets import bind_workspace_secret_ref, write_secret


@pytest.fixture
def linkedin_workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    with control_session() as db:
        db.add_all(
            [
                Workspace(slug="linkedin-a", name="LinkedIn A", status="active"),
                Workspace(slug="linkedin-b", name="LinkedIn B", status="active"),
            ]
        )
        db.commit()
    try:
        yield
    finally:
        with control_session() as db:
            db.execute(
                delete(WorkspaceConnector).where(
                    WorkspaceConnector.workspace_slug.in_(("linkedin-a", "linkedin-b"))
                )
            )
            for slug in ("linkedin-a", "linkedin-b"):
                row = db.get(Workspace, slug)
                if row is not None:
                    db.delete(row)
            db.commit()


def test_linkedin_key_and_source_plan_are_workspace_bound(linkedin_workspaces):
    with context.use_workspace("linkedin-a"):
        ref = bind_workspace_secret_ref("linkedin", "data_api_key")
        write_secret(ref, "  a-private-key\n")
        assert config.linkedin_api_key() == "a-private-key"
        assert resolve_source().names() == ["linkedin_api", "web_search"]

    with context.use_workspace("linkedin-b"):
        assert config.linkedin_api_key() == ""
        assert resolve_source().names() == ["web_search"]


def test_linkedin_key_refuses_without_workspace_context(monkeypatch):
    monkeypatch.setattr(context, "_process_workspace", None)
    with pytest.raises(context.NoWorkspaceContext):
        config.linkedin_api_key()
