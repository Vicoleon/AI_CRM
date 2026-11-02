"""Regression tests for the Gmail draft signature / confidentiality disclaimer.

A Gmail draft created via the API does NOT inherit the user's send-as
signature the way the web composer does, so ``GmailClient.create_draft`` must
embed it. These tests pin that behavior: the signature (which carries the company's
confidentiality disclaimer) is fetched from ``users.settings.sendAs`` and
appended to every draft, while a user with no signature still gets the original
plain-text draft.
"""

import base64
from unittest.mock import MagicMock, patch

from rubik_core.tools.gmail import GmailClient, _html_to_text


def _client_with_service(mock_service):
    """A GmailClient whose Google API resource is a mock (no real auth)."""
    with patch.object(GmailClient, "_build_service", return_value=mock_service):
        client = GmailClient("emery@acme.example")
    client.service = mock_service
    return client


def _sendas_execute(service):
    return service.users.return_value.settings.return_value.sendAs.return_value.list.return_value.execute


def _draft_create(service):
    return service.users.return_value.drafts.return_value.create


def _decode_raw(create_mock) -> str:
    body = create_mock.call_args.kwargs["body"]
    return base64.urlsafe_b64decode(body["message"]["raw"]).decode()


# ── get_signature ──────────────────────────────────────────────────────────


def test_get_signature_returns_primary_sendas_signature():
    service = MagicMock()
    _sendas_execute(service).return_value = {
        "sendAs": [
            {
                "sendAsEmail": "alias@acme.example",
                "isPrimary": False,
                "signature": "<div>Alias</div>",
            },
            {
                "sendAsEmail": "emery@acme.example",
                "isPrimary": True,
                "signature": "<div>Emery Brooks<br>CEO</div>",
            },
        ]
    }
    client = _client_with_service(service)
    assert client.get_signature() == "<div>Emery Brooks<br>CEO</div>"


def test_get_signature_empty_when_none_configured():
    service = MagicMock()
    _sendas_execute(service).return_value = {"sendAs": []}
    assert _client_with_service(service).get_signature() == ""


def test_get_signature_swallows_api_errors():
    service = MagicMock()
    _sendas_execute(service).side_effect = RuntimeError("scope missing")
    assert _client_with_service(service).get_signature() == ""


# ── create_draft ───────────────────────────────────────────────────────────


def test_create_draft_appends_signature_and_disclaimer():
    service = MagicMock()
    _sendas_execute(service).return_value = {
        "sendAs": [
            {
                "isPrimary": True,
                "sendAsEmail": "emery@acme.example",
                "signature": (
                    "<div>Emery Brooks<br>CEO, Acme<br>"
                    "CONFIDENTIALITY NOTICE: This email is confidential.</div>"
                ),
            }
        ]
    }
    _draft_create(service).return_value.execute.return_value = {"id": "d1"}
    client = _client_with_service(service)

    result = client.create_draft(
        to=["arden@example.com"],
        subject="Follow-up",
        body_markdown="Hi Arden,\n\nGreat call today.",
    )
    assert result["draft_id"] == "d1"
    assert result["gmail_draft_url"] == (
        "https://mail.google.com/mail/?authuser=emery%40acme.example#drafts?compose=d1"
    )

    decoded = _decode_raw(_draft_create(service))
    # Disclaimer + signature made it into the draft.
    assert "CONFIDENTIALITY NOTICE" in decoded
    assert "Emery Brooks" in decoded
    # Rich signature rides in an HTML alternative part; body is preserved.
    assert "text/html" in decoded
    assert "Hi Arden," in decoded


def test_create_draft_plaintext_when_no_signature():
    service = MagicMock()
    _sendas_execute(service).return_value = {"sendAs": []}
    _draft_create(service).return_value.execute.return_value = {"id": "d2"}
    client = _client_with_service(service)

    client.create_draft(to=["a@b.com"], subject="s", body_markdown="Body only")
    decoded = _decode_raw(_draft_create(service))
    assert "Body only" in decoded
    # No signature → original plain-text-only draft (no HTML part added).
    assert "text/html" not in decoded


def test_html_to_text_strips_tags_keeps_disclaimer():
    html = "<div>Emery<br>CEO</div><p>CONFIDENTIAL: do not forward</p>"
    text = _html_to_text(html)
    assert "Emery" in text
    assert "CEO" in text
    assert "CONFIDENTIAL: do not forward" in text
    assert "<" not in text
