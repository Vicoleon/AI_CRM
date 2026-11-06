"""Workspace Domain-Wide Delegation through keyless IAM signing.

Each request or Job selects a workspace before obtaining credentials. Its
control row supplies the target service-account email and admin subject.
Cloud Run signs through its metadata-backed runtime identity; no Workspace
key file is loaded or exported as Application Default Credentials.
"""

from __future__ import annotations

import logging
import threading

import google.auth
from google.auth import compute_engine, iam
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from rubik_core.tenancy.platform_secrets import is_production_runtime

logger = logging.getLogger(__name__)

_REFRESH_LOCK = threading.Lock()
_IAM_SCOPE = ["https://www.googleapis.com/auth/cloud-platform"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _workspace_google_row(*, require_service_account: bool = True):
    """Return an active, configured control row after all access fences."""
    from rubik_core.auth.impersonation import impersonation_active
    from rubik_core.tenancy.support_context import current_support_session

    if current_support_session() is not None:
        logger.warning("Workspace credentials refused: platform support active")
        return None
    if impersonation_active():
        logger.warning("Workspace credentials refused: person impersonation active")
        return None

    from rubik_core.control.repository import get_workspace
    from rubik_core.tenancy.context import NoWorkspaceContext, current_workspace

    try:
        row = get_workspace(current_workspace())
        if (
            row is None
            or row.status != "active"
            or (require_service_account and not row.google_service_account)
            or not row.google_subject
        ):
            return None
        return row
    except NoWorkspaceContext:
        return None
    except Exception:
        logger.exception("Workspace Google configuration lookup failed")
        return None


def google_workspace_configured() -> bool:
    """Whether the current live workspace has both DWD account and subject."""
    return _workspace_google_row() is not None


def workspace_dwd_subject() -> str | None:
    """Admin subject configured in the current workspace's control row."""
    row = _workspace_google_row(require_service_account=False)
    return row.google_subject if row is not None else None


def load_workspace_credentials(
    scopes: list[str],
) -> service_account.Credentials | None:
    """Build DWD credentials that callers can delegate with ``.with_subject()``.

    The target service account comes from the current workspace. IAM signs its
    assertion using the Cloud Run runtime identity or the local developer ADC.
    Construction makes no network call; ``safe_refresh`` serializes the
    subsequent OAuth token exchange across parallel channel agents.
    """
    row = _workspace_google_row()
    if row is None:
        return None

    try:
        if is_production_runtime():
            # Metadata credentials cannot be redirected by an environment
            # variable containing a different service-account JSON key.
            runtime_credentials = compute_engine.Credentials(scopes=_IAM_SCOPE)
        else:
            runtime_credentials, _ = google.auth.default(scopes=_IAM_SCOPE)
        signer = iam.Signer(Request(), runtime_credentials, row.google_service_account)
        return service_account.Credentials(
            signer=signer,
            service_account_email=row.google_service_account,
            token_uri=_TOKEN_URI,
            scopes=scopes,
        )
    except Exception:
        logger.exception("Failed to construct keyless Workspace DWD credentials")
        return None


def safe_refresh(creds) -> None:
    """Serialize OAuth refresh across concurrent Workspace channel agents."""
    with _REFRESH_LOCK:
        creds.refresh(Request())
