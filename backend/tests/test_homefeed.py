from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.config import settings

client = TestClient(app)


# The /overnight-briefing endpoint does its own auth inline rather than via
# FastAPI Depends, so instead of `app.dependency_overrides` we patch the inline
# lookups on `backend.auth` to return a deterministic user.
#
# Auth model (SOC 2 H-1): Cloud Scheduler must present the shared X-Scheduler-Key
# secret. There is no longer an X-CloudScheduler header bypass — that header was
# client-forgeable on an --allow-unauthenticated service.
#
# The endpoint dispatches a workspace-scoped Job, so tests patch the launcher.


def _make_user(*, email="test@example.com", admin=False):
    u = MagicMock()
    u.email = email
    u.roles = ["admin"] if admin else ["user"]
    return u


@patch(
    "backend.routers.jobs.launch_module_execution",
    return_value={"name": "executions/briefing"},
)
@patch("backend.auth.get_current_user")
@patch("backend.auth.security", new_callable=AsyncMock)
def test_trigger_briefing_success(
    mock_security, mock_get_user, mock_pipeline, monkeypatch
):
    """POST /api/jobs/overnight-briefing accepts a self-triggered run."""
    mock_security.return_value = MagicMock()  # any credentials object
    mock_get_user.return_value = _make_user(email="test@acme.example")
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", True)

    response = client.post(
        "/api/jobs/overnight-briefing",
        json={"target_email": "test@acme.example"},
        headers={"Authorization": "Bearer test@acme.example"},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "dispatched"
    mock_pipeline.assert_called_once_with(
        "backend.jobs.overnight_briefing_job",
        ["--target-email", "test@acme.example"],
    )


@patch("backend.routers.jobs.launch_module_execution")
@patch("backend.auth.get_current_user")
@patch("backend.auth.security", new_callable=AsyncMock)
def test_trigger_briefing_forbidden(
    mock_security, mock_get_user, mock_pipeline, monkeypatch
):
    """Non-admin triggering for someone else's email → 403."""
    mock_security.return_value = MagicMock()
    mock_get_user.return_value = _make_user(email="test@acme.example", admin=False)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", True)

    response = client.post(
        "/api/jobs/overnight-briefing",
        json={"target_email": "other@example.com"},
        headers={"Authorization": "Bearer test@acme.example"},
    )

    assert response.status_code == 403
    data = response.json()
    assert "admin" in data["detail"]
    # And the background pipeline should NOT have been queued.
    mock_pipeline.assert_not_called()


@patch(
    "backend.routers.jobs.launch_module_execution",
    return_value={"name": "executions/briefing"},
)
def test_trigger_briefing_scheduler_key(mock_pipeline, monkeypatch):
    """A valid X-Scheduler-Key authorizes a run with no user token (H-1)."""
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "test-key")

    response = client.post(
        "/api/jobs/overnight-briefing",
        json={"target_email": "anyone@acme.example"},
        headers={"X-Scheduler-Key": "test-key", "X-Cubie-Workspace": "test"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "dispatched"
    mock_pipeline.assert_called_once_with(
        "backend.jobs.overnight_briefing_job",
        ["--target-email", "anyone@acme.example"],
    )


@patch("backend.routers.jobs.launch_module_execution")
def test_trigger_briefing_legacy_header_no_bypass(mock_pipeline, monkeypatch):
    """The removed X-CloudScheduler header is no longer an auth bypass (H-1).

    With no user token and only the old (forged) header, the request must be
    rejected and the pipeline must not run.
    """
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "test-key")
    # Pin mock auth off so the "no user token" path deterministically 401s
    # regardless of the ambient dev env (settings is a shared singleton).
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)

    response = client.post(
        "/api/jobs/overnight-briefing",
        json={"target_email": "victim@acme.example"},
        headers={"X-CloudScheduler": "true"},
    )

    assert response.status_code == 401, response.text
    mock_pipeline.assert_not_called()
