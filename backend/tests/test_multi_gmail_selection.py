"""A selected extra mailbox never reverts to the work DWD identity."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_selected_gmail_uses_only_owner_bound_oauth_credentials(monkeypatch):
    from rubik_core.tenancy import gmail_accounts
    from rubik_core.tools import _workspace_auth, gmail

    credential = object()
    calls = []
    monkeypatch.setattr(
        gmail_accounts,
        "get_account_credentials",
        lambda owner, account_id: (
            calls.append((owner, account_id)) or (credential, "personal@gmail.com")
        ),
    )
    monkeypatch.setattr(
        _workspace_auth, "safe_refresh", lambda creds: calls.append(("refresh", creds))
    )
    monkeypatch.setattr(
        gmail,
        "build",
        lambda *args, **kwargs: (
            calls.append(("build", kwargs["credentials"])) or SimpleNamespace()
        ),
    )
    client = gmail.GmailClient("work@rubik-soft.com", account_id="personal-id")
    assert client.user_email == "personal@gmail.com"
    assert calls == [
        ("work@rubik-soft.com", "personal-id"),
        ("refresh", credential),
        ("build", credential),
    ]


def test_revoked_extra_mailbox_never_calls_work_dwd(monkeypatch):
    from rubik_core.tenancy import gmail_accounts
    from rubik_core.tools import _workspace_auth, gmail

    monkeypatch.setattr(
        gmail_accounts,
        "get_account_credentials",
        lambda owner, account_id: (_ for _ in ()).throw(PermissionError("revoked")),
    )
    monkeypatch.setattr(
        _workspace_auth,
        "load_workspace_credentials",
        lambda scopes: pytest.fail("DWD fallback is forbidden"),
    )
    with pytest.raises(PermissionError, match="revoked"):
        gmail.GmailClient("work@rubik-soft.com", account_id="revoked-id")


def test_selected_mailbox_is_carried_through_inbox_and_draft_without_work_memory(
    monkeypatch,
):
    from backend.routers import email

    calls = []

    class StubService:
        def __init__(self, *, user_email, account_id):
            calls.append(("inbox", user_email, account_id))

        def get_unread(self, *, max_results):
            return [SimpleNamespace(thread_id="same-thread", account_id="personal-id")]

    class StubClient:
        service = object()

        def __init__(self, *, user_email, account_id):
            calls.append(("draft", user_email, account_id))

        def create_draft(self, **kwargs):
            return {"draft_id": "personal-draft"}

    monkeypatch.setattr(email, "GmailService", StubService)
    monkeypatch.setattr(email, "GmailClient", StubClient)
    monkeypatch.setattr(email, "_log_audit_action", lambda *a, **kw: None)
    monkeypatch.setattr(
        email,
        "_resolve_followup_debt",
        lambda *a: pytest.fail("Personal thread must not resolve work priorities"),
    )
    user = SimpleNamespace(email="work@rubik-soft.com")
    inbox = email.email_inbox(
        account_id="personal-id", max_results=5, user=user, db=object()
    )
    draft = email.create_draft(
        email.DraftRequest(
            to=["friend@example.com"],
            subject="Hello",
            body_markdown="Hi",
            thread_id="same-thread",
            account_id="personal-id",
        ),
        http_request=SimpleNamespace(state=SimpleNamespace()),
        user=user,
        db=object(),
    )
    assert inbox["items"][0].thread_id == "same-thread"
    assert draft == {"draft_id": "personal-draft", "account_id": "personal-id"}
    assert calls == [
        ("inbox", "work@rubik-soft.com", "personal-id"),
        ("draft", "work@rubik-soft.com", "personal-id"),
    ]


def test_denied_personal_draft_does_not_record_success_or_expose_recipient(
    monkeypatch,
):
    from fastapi import HTTPException

    from backend.routers import email

    monkeypatch.setattr(
        email,
        "GmailClient",
        lambda **kwargs: (_ for _ in ()).throw(PermissionError("private token detail")),
    )
    monkeypatch.setattr(
        email,
        "_log_audit_action",
        lambda *a, **kw: pytest.fail("Denied draft must not be audited as success"),
    )
    with pytest.raises(HTTPException) as error:
        email.create_draft(
            email.DraftRequest(
                to=["private@example.com"],
                subject="Sensitive",
                body_markdown="Private",
                account_id="other-person-id",
            ),
            http_request=SimpleNamespace(state=SimpleNamespace()),
            user=SimpleNamespace(email="work@rubik-soft.com"),
            db=object(),
        )
    assert error.value.status_code == 403
    assert "private" not in error.value.detail.lower()
