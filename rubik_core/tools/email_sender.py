"""System-generated email SEND — the one sanctioned exception to draft-only.

Cubie's chat / AI-drafting path (``rubik_core/tools/gmail.py``) deliberately
excludes the ``gmail.send`` scope: the assistant never sends on a user's behalf,
it only drafts. This module is separate on purpose. It exists solely for backend
features that *generate an artifact the user explicitly asked for and deliver
it* (e.g. an emailed Deep Prep PDF). It is never registered as a chat tool and
must never be called from the coordinator.

Transport: the shared workspace-sync service account — granted ``gmail.send``
via domain-wide delegation by the company's Workspace admin — impersonates a
real Workspace mailbox and calls
``users.messages.send``. MIME is built with the stdlib ``email`` module; no SMTP,
no third-party mail SDK. We reuse ``_workspace_auth.load_workspace_credentials``
so the same ADC-isolation contract holds (the DWD key never leaks into ADC).

Safety rails copied from that system:
  * a swappable ``Transport`` Protocol + module-level factory so tests/dev never
    touch real Gmail;
  * recipients and sender must belong to the selected workspace's verified
    domains or exact user roster;
  * a global auto-send flag (default off) so the feature ships dark;
  * best-effort delivery — transport errors are logged and returned, never
    raised into the caller. Email is a convenience channel, not a critical path.
"""

from __future__ import annotations

import base64
import email.message
import email.utils
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.operations import get_operations_config
from rubik_core.config import settings

logger = logging.getLogger(__name__)

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_MAILBOX = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z"
)


@dataclass
class EmailMessageSpec:
    """One outbound email: recipients, subject, both bodies, optional PDF."""

    to: list[str]
    subject: str
    text_body: str
    html_body: str
    cc: list[str] = field(default_factory=list)
    pdf: bytes = b""
    pdf_filename: str = ""


@dataclass
class SendResult:
    # "sent" | "failed" | "disabled" (autosend off) | "no_recipients"
    status: str
    message_id: str | None = None
    error: str | None = None
    # Retained for older callers; recipient redirection is no longer supported.
    redirected_to: list[str] | None = None


class Transport(Protocol):
    def send(self, *, sender: str, impersonate: str, spec: EmailMessageSpec) -> str:
        """Send the message and return the provider message id. May raise."""
        ...


class GmailSendTransport:
    """Real transport: shared workspace SA → impersonate → users.messages.send."""

    def send(self, *, sender: str, impersonate: str, spec: EmailMessageSpec) -> str:
        from googleapiclient.discovery import build

        from rubik_core.tools._workspace_auth import (
            load_workspace_credentials,
            safe_refresh,
        )

        creds = load_workspace_credentials([SEND_SCOPE])
        if creds is None:
            raise RuntimeError(
                "Workspace DWD credentials are not configured; cannot send."
            )
        creds = creds.with_subject(impersonate)
        safe_refresh(creds)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        msg = email.message.EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(spec.to)
        if spec.cc:
            msg["Cc"] = ", ".join(spec.cc)
        msg["Subject"] = spec.subject
        msg.set_content(spec.text_body)
        msg.add_alternative(spec.html_body, subtype="html")  # multipart/alternative
        if spec.pdf:
            msg.add_attachment(
                spec.pdf,
                maintype="application",
                subtype="pdf",
                filename=spec.pdf_filename or "attachment.pdf",
            )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = (
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
        )
        return result["id"]


# ── Swappable transport factory ─────────────────────────────────────────────
# Production never constructs a transport directly; it goes through
# build_transport() so a test/dev harness can inject a fake via
# set_transport_factory() and guarantee no real Gmail call escapes.
_transport_factory: Callable[[], Transport] | None = None


def build_transport() -> Transport:
    if _transport_factory is not None:
        return _transport_factory()
    return GmailSendTransport()


def set_transport_factory(factory: Callable[[], Transport]) -> None:
    global _transport_factory
    _transport_factory = factory


def reset_transport_factory() -> None:
    global _transport_factory
    _transport_factory = None


def _workspace_address_allowed(email: str) -> bool:
    """Live control domain or exact non-support user in this tenant."""
    from sqlalchemy import func, select

    from rubik_core.control.repository import domain_belongs_to_workspace, get_workspace
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import User
    from rubik_core.tenancy.context import current_workspace

    if not isinstance(email, str) or not _MAILBOX.fullmatch(email):
        return False
    normalized = email.casefold()
    slug = current_workspace()
    workspace = get_workspace(slug)
    if workspace is None or workspace.status != "active":
        return False
    domain = normalized.rsplit("@", 1)[1]
    if domain_belongs_to_workspace(slug, domain):
        return True
    with SessionLocal() as db:
        return (
            db.scalar(
                select(User.id).where(
                    func.lower(User.email) == normalized,
                    User.is_platform_support.is_(False),
                )
            )
            is not None
        )


def send_email(
    spec: EmailMessageSpec,
    *,
    impersonate: str,
    sender_name: str | None = None,
    autosend: bool | None = None,
) -> SendResult:
    """Best-effort system send. Never raises.

    ``impersonate`` is the Workspace mailbox the SA sends *as*; it must be a
    real, existing mailbox (DWD impersonation of a missing mailbox fails at
    token exchange with ``invalid_grant``). Deliver a user their own artifact by
    impersonating the recipient; use a dedicated sender mailbox otherwise.

    ``autosend`` lets a caller supply its own kill switch. The caller must
    choose recipients under its feature-specific ACL; this function also
    refuses addresses outside the active workspace as a final isolation gate.
    """
    enabled = settings.CUBIE_DEEP_PREP_EMAIL_AUTOSEND if autosend is None else autosend
    if not enabled:
        logger.info("system_email.disabled (autosend off) to=%s", spec.to)
        return SendResult(status="disabled")

    if not spec.to:
        return SendResult(status="no_recipients", error="no recipients resolved")
    try:
        config = get_operations_config()
        addresses = [impersonate, *spec.to, *spec.cc]
        if not all(_workspace_address_allowed(address) for address in addresses):
            logger.warning("system_email.refused address outside current workspace")
            return SendResult(
                status="failed", error="Address outside the current workspace"
            )
    except Exception:  # noqa: BLE001 — policy outage must never send mail
        logger.exception("system_email.workspace_policy_unavailable")
        return SendResult(
            status="failed", error="Workspace delivery policy unavailable"
        )

    name = sender_name or config.deep_prep_sender_name or PRODUCT_NAME
    if not isinstance(name, str) or any(ord(char) < 32 for char in name):
        return SendResult(status="failed", error="Invalid sender name")
    sender = email.utils.formataddr((name, impersonate))
    outgoing = EmailMessageSpec(
        to=spec.to,
        cc=spec.cc,
        subject=spec.subject,
        text_body=spec.text_body,
        html_body=spec.html_body,
        pdf=spec.pdf,
        pdf_filename=spec.pdf_filename,
    )
    try:
        message_id = build_transport().send(
            sender=sender, impersonate=impersonate, spec=outgoing
        )
        logger.info("system_email.sent id=%s to=%s", message_id, spec.to)
        return SendResult(status="sent", message_id=message_id)
    except Exception as exc:  # noqa: BLE001 — email is never on the critical path
        logger.exception("system_email.failed to=%s", spec.to)
        return SendResult(status="failed", error=str(exc))
