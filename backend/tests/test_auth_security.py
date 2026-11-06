from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from backend.auth import get_current_user
from rubik_core.config import AppMode


@pytest.fixture
def mock_request():
    request = MagicMock(spec=Request)
    request.headers = {}
    return request


@pytest.fixture
def mock_credentials():
    credentials = MagicMock()
    credentials.credentials = "mock-token"
    return credentials


# We'll use patch.object on the 'settings' imported in backend.auth
@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
@patch("requests.get")
def test_mock_token_disabled_by_default(
    mock_get, mock_registry, mock_settings, mock_request, mock_credentials
):
    mock_settings.ENABLE_MOCK_AUTH = False
    mock_settings.APP_MODE = AppMode.LOCAL

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_get.return_value = mock_resp

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(mock_request, mock_credentials)
    assert excinfo.value.status_code == 401


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
def test_mock_token_enabled_not_gcp(
    mock_registry, mock_settings, mock_request, mock_credentials
):
    """ENABLE_MOCK_AUTH short-circuits auth; the default mock identity is the
    local developer, ``CUBIE_DEV_USER_EMAIL`` (see backend.auth)."""
    mock_settings.ENABLE_MOCK_AUTH = True
    mock_settings.APP_MODE = AppMode.LOCAL
    mock_settings.CUBIE_DEV_USER_EMAIL = "dev@acme.example"

    # Mock user must match UserProfile from schemas
    from rubik_core.database.schemas import UserProfile

    mock_user = UserProfile(
        id="123",
        email="dev@acme.example",
        name="Dev User",
        roles=["admin", "user"],
        created_at="2024-03-03T12:00:00",
        updated_at="2024-03-03T12:00:00",
    )
    mock_registry.get_user.return_value = mock_user

    user = get_current_user(mock_request, mock_credentials)
    assert user.email == "dev@acme.example"
    mock_registry.get_user.assert_called_with("dev@acme.example")


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
def test_mock_auth_header_overrides_identity(
    mock_registry, mock_settings, mock_request, mock_credentials
):
    """X-Mock-User header picks the mock identity when mock auth is on."""
    mock_settings.ENABLE_MOCK_AUTH = True
    mock_settings.APP_MODE = AppMode.LOCAL
    mock_request.headers = {"X-Mock-User": "teammate@acme.example"}

    from rubik_core.database.schemas import UserProfile

    mock_user = UserProfile(
        id="456",
        email="teammate@acme.example",
        name="Teammate",
        roles=["user"],
        created_at="2024-03-03T12:00:00",
        updated_at="2024-03-03T12:00:00",
    )
    mock_registry.get_user.return_value = mock_user

    user = get_current_user(mock_request, mock_credentials)
    assert user.email == "teammate@acme.example"
    mock_registry.get_user.assert_called_with("teammate@acme.example")


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
@patch("requests.get")
def test_mock_token_rejected_when_mock_auth_disabled_on_gcp(
    mock_get, mock_registry, mock_settings, mock_request, mock_credentials
):
    """An opaque mock token cannot become a session in production mode."""
    mock_settings.ENABLE_MOCK_AUTH = False
    mock_settings.APP_MODE = AppMode.GCP

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(mock_request, mock_credentials)
    assert excinfo.value.status_code == 401
    assert "Invalid session" in excinfo.value.detail
    mock_get.assert_not_called()
    mock_registry.get_user.assert_not_called()


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
def test_tenant_setting_cannot_grant_admin(
    mock_registry, mock_settings, mock_request, mock_credentials
):
    mock_settings.ENABLE_MOCK_AUTH = False  # Use real-ish path
    mock_settings.APP_MODE = AppMode.LOCAL
    from rubik_core.auth.session_tokens import mint_session_token

    mock_credentials.credentials = mint_session_token(
        "admin@db.com", "test", hours=1.0
    )[0]

    mock_registry.get_workspace_settings.return_value = {"admin_email": "admin@db.com"}

    from rubik_core.database.schemas import UserProfile

    existing_user = UserProfile(
        id="123",
        email="admin@db.com",
        name="DB Admin",
        roles=["user"],
        created_at="2024-03-03T12:00:00",
        updated_at="2024-03-03T12:00:00",
    )
    mock_registry.get_user.return_value = existing_user

    user = get_current_user(mock_request, mock_credentials)
    assert "admin" not in user.roles
    mock_registry.update_user.assert_not_called()


# ── Mock auth keeps provisioned roles, including for the developer ──


def _dev_profile(email: str, roles: list[str]):
    from rubik_core.database.schemas import UserProfile

    return UserProfile(
        id="dev",
        email=email,
        name="Dev",
        roles=roles,
        created_at="2026-10-28T00:00:00",
        updated_at="2026-10-28T00:00:00",
    )


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
def test_the_developer_is_the_configured_identity_without_role_promotion(
    mock_registry, mock_settings, mock_request, mock_credentials
):
    mock_settings.ENABLE_MOCK_AUTH = True
    mock_settings.APP_MODE = AppMode.DEV_MOCK_AUTH
    mock_settings.CUBIE_DEV_USER_EMAIL = " Builder@ACME.example "
    mock_registry.get_user.return_value = _dev_profile("builder@acme.example", ["user"])

    user = get_current_user(mock_request, mock_credentials)

    mock_registry.get_user.assert_called_with("builder@acme.example")
    mock_registry.update_user.assert_not_called()
    assert "admin" not in user.roles


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
def test_the_developer_is_not_made_admin_outside_dev_mock_auth(
    mock_registry, mock_settings, mock_request, mock_credentials
):
    mock_settings.ENABLE_MOCK_AUTH = True
    mock_settings.APP_MODE = AppMode.LOCAL
    mock_settings.CUBIE_DEV_USER_EMAIL = "dev@acme.example"
    mock_registry.get_user.return_value = _dev_profile("dev@acme.example", ["user"])

    user = get_current_user(mock_request, mock_credentials)

    assert user.email == "dev@acme.example"
    assert "admin" not in user.roles
    mock_registry.update_user.assert_not_called()


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
def test_a_named_mock_user_is_never_promoted(
    mock_registry, mock_settings, mock_request, mock_credentials
):
    mock_settings.ENABLE_MOCK_AUTH = True
    mock_settings.APP_MODE = AppMode.DEV_MOCK_AUTH
    mock_settings.CUBIE_DEV_USER_EMAIL = "dev@acme.example"
    mock_request.headers = {"X-Mock-User": "teammate@acme.example"}
    mock_registry.get_user.return_value = _dev_profile(
        "teammate@acme.example", ["user"]
    )

    user = get_current_user(mock_request, mock_credentials)

    assert user.email == "teammate@acme.example"
    mock_registry.update_user.assert_not_called()


@patch("backend.auth.settings")
@patch("backend.auth.user_registry")
def test_a_blank_developer_setting_falls_back_to_the_fictional_default(
    mock_registry, mock_settings, mock_request, mock_credentials
):
    mock_settings.ENABLE_MOCK_AUTH = True
    mock_settings.APP_MODE = AppMode.LOCAL
    mock_settings.CUBIE_DEV_USER_EMAIL = "  "
    mock_registry.get_user.return_value = _dev_profile("dev@acme.example", ["user"])

    get_current_user(mock_request, mock_credentials)

    mock_registry.get_user.assert_called_with("dev@acme.example")
