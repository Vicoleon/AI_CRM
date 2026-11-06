"""Workspace identity gates for session requests and Google sign-in."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

from rubik_core.auth.google_id_token import GoogleIdentityError, verify_google_id_token
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def profile() -> UserProfile:
    return UserProfile(
        id="pat",
        email="pat@acme.example",
        name="Pat",
        roles=["user"],
        created_at="2026-10-29T00:00:00",
        updated_at="2026-10-29T00:00:00",
    )


def _credentials(token: str):
    return MagicMock(credentials=token)


def test_removed_domain_refuses_existing_session_before_user_roles(
    monkeypatch, profile
):
    from backend.auth import get_current_user
    from rubik_core.auth import session_tokens

    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setattr(session_tokens, "session_lifetime_hours", lambda: 1.0)
    monkeypatch.setattr("backend.auth.user_registry.get_user", lambda email: profile)
    domain_check = Mock(return_value=False)
    monkeypatch.setattr("backend.auth.domain_belongs_to_workspace", domain_check)
    with use_workspace("acme"):
        token, _, _ = mint_session_token(profile.email, "acme", hd="acme.example")
        with pytest.raises(HTTPException) as refused:
            get_current_user(MagicMock(), _credentials(token))
    assert refused.value.status_code == 401
    assert refused.value.detail["code"] == "workspace_mismatch"
    domain_check.assert_called_once_with("acme", "acme.example")


def test_bound_domain_allows_existing_session(monkeypatch, profile):
    from backend.auth import get_current_user
    from rubik_core.auth import session_tokens

    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setattr(session_tokens, "session_lifetime_hours", lambda: 1.0)
    monkeypatch.setattr("backend.auth.user_registry.get_user", lambda email: profile)
    monkeypatch.setattr("backend.auth.user_registry.get_workspace_settings", lambda: {})
    monkeypatch.setattr(
        "backend.auth.domain_belongs_to_workspace", lambda ws, domain: True
    )
    with use_workspace("acme"):
        token, _, _ = mint_session_token(profile.email, "acme", hd="acme.example")
        user = get_current_user(MagicMock(), _credentials(token))
    assert user.email == profile.email


def test_verified_hosted_domain_allows_alias_email(monkeypatch, profile):
    from backend.auth import get_current_user

    alias = profile.model_copy(update={"email": "pat@alias.example"})
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setattr("backend.auth.user_registry.get_user", lambda email: alias)
    monkeypatch.setattr("backend.auth.user_registry.get_workspace_settings", lambda: {})
    check = Mock(return_value=True)
    monkeypatch.setattr("backend.auth.domain_belongs_to_workspace", check)
    with use_workspace("acme"):
        token, _, _ = mint_session_token(
            alias.email, "acme", hd="acme.example", hours=1.0
        )
        user = get_current_user(MagicMock(), _credentials(token))
    assert user.email == "pat@alias.example"
    check.assert_called_once_with("acme", "acme.example")


def test_opaque_google_access_token_is_never_checked_per_request(monkeypatch):
    from backend.auth import get_current_user

    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    with (
        patch("requests.get") as get,
        patch("backend.auth.user_registry.get_user") as lookup,
    ):
        with pytest.raises(HTTPException) as refused:
            get_current_user(MagicMock(), _credentials("ya29.google-access"))
    assert refused.value.status_code == 401
    get.assert_not_called()
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"aud": "other-client"}, "invalid_google_token"),
        ({"iss": "https://other.example"}, "invalid_google_token"),
        ({"email_verified": False}, "invalid_google_token"),
        ({"hd": None}, "workspace_personal"),
        ({"exp": 1}, "invalid_google_token"),
    ],
)
def test_google_claims_require_audience_issuer_verified_email_and_hd(
    monkeypatch, change, expected
):
    from rubik_core.auth import google_id_token

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "our-client")
    claims = {
        "aud": "our-client",
        "iss": "https://accounts.google.com",
        "email_verified": True,
        "email": "pat@acme.example",
        "hd": "acme.example",
        "sub": "google-sub",
        "exp": 2**31,
    }
    monkeypatch.setattr(
        google_id_token,
        "_verify_google_token",
        lambda raw, client_id: {**claims, **change},
    )
    with pytest.raises(GoogleIdentityError) as refused:
        verify_google_id_token("signed-token")
    assert refused.value.code == expected
