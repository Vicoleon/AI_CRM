"""Auth-failure detection shared by the Google-backed channel agents.

Turns a broken Google feed from a SILENT "0 signals" (which the pipeline reads
as a quiet day) into an explicit ``fetch_failed`` error that
:meth:`BaseChannelAgent.run` records — so the bundle ``audit_trail`` shows
``[ERROR]`` for that channel instead of an indistinguishable "0 signals".

Why this matters: when a Workspace
account is deactivated, the gmail/calendar/drive/meet tool clients fail to build
their delegated service (``service is None``) and then quietly ``return []``. For
an OFFBOARDED user the roster gate now drops them before generation — but for an
ACTIVE user whose DWD grant silently breaks, that same swallow hides a real
outage behind a "quiet day". This helper makes that outage visible.

Deliberately narrow: an auth failure is only declared when a service account IS
configured (so we genuinely expected impersonation to work). In local dev with
no service account, ``service is None`` for everyone by design and is NOT treated
as an error — the channel reports empty exactly as before.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Substrings that mark a Google auth / domain-wide-delegation failure (as opposed
# to a transient error or a genuinely empty mailbox). Matched case-insensitively
# against the exception's class name and message.
_AUTH_ERROR_MARKERS = (
    "invalid_grant",
    "unauthorized_client",
    "access_denied",
    "refresherror",
    "service not initialized",
)


class ChannelAuthError(RuntimeError):
    """A Google channel could not authenticate for this user.

    Raised from a channel agent's ``fetch_signals`` so ``base.run`` records
    ``error='fetch_failed: …'`` rather than a silent empty result.
    """


def workspace_configured() -> bool:
    """True when the selected workspace has Google delegation configured.

    When it is, an impersonated service that fails to build is a real per-user
    auth failure. When it isn't (local dev), a missing service is expected and
    must not be reported as an error.
    """
    from rubik_core.tools._workspace_auth import google_workspace_configured

    return google_workspace_configured()


def is_auth_error(exc: BaseException) -> bool:
    """Whether ``exc`` looks like a Google auth / DWD failure.

    Recognises ``HttpError`` 401/403, ``google.auth`` ``RefreshError``, and the
    ``invalid_grant`` / ``unauthorized_client`` OAuth errors — the failure modes
    a deactivated account or a revoked delegation produce. A transient 5xx or an
    empty result is NOT an auth error.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (401, 403):
        return True
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _AUTH_ERROR_MARKERS)


def require_live_service(service, *, channel: str, user_email: str) -> None:
    """Raise :class:`ChannelAuthError` when an expected Workspace service is dead.

    A no-op unless a service account is configured AND ``service is None`` — i.e.
    we expected to impersonate ``user_email`` but the delegated service failed to
    build (the deactivated-account / revoked-delegation signature).
    """
    if service is None and workspace_configured():
        raise ChannelAuthError(
            f"{channel} auth/DWD unavailable for {user_email} — Workspace service "
            "account is configured but the delegated service failed to build "
            "(deactivated account or revoked delegation)"
        )


# Sentinel so a client stub with NO ``service`` attribute (test doubles that
# return canned data) is treated as "service present", not as ``None`` (a dead
# service). Only an EXPLICIT ``service is None`` is the auth-failure signal.
_SERVICE_PRESENT = object()


def require_live_client(client, *, channel: str, user_email: str) -> None:
    """Client-taking wrapper around :func:`require_live_service`.

    Reads ``client.service`` defensively: a client that never set the attribute
    (a lightweight test stub) is treated as live, so only a real client that
    built a ``None`` service — the deactivated / DWD-broken case — trips the
    auth error.
    """
    require_live_service(
        getattr(client, "service", _SERVICE_PRESENT),
        channel=channel,
        user_email=user_email,
    )
