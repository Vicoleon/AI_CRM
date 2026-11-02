"""Person impersonation — "see Cubie as Jordan", read-only, no private data.

An admin on ``IMPERSONATION_ALLOWED_EMAILS`` picks a person named in
``USER_IMPERSONATION_TARGETS`` from the impersonation menu. The client then
sends ``X-Impersonate-User: <email>`` on every API call, and
``backend.auth._apply_impersonation`` resolves the caller to that person's REAL
profile (their department, roles, exec flag, feature verdicts), so the app
renders exactly as it does for them.

Unlike department impersonation (a pseudo-user with no data of its own), this
is a real person with a real inbox, calendar, Drive and call transcripts. Three
locks keep those out, and none of them depends on the identity swap having
been authorised — any request that merely CARRIES the header is restricted:

1. **Read-only.** Every non-GET request is refused. Nothing is saved, sent,
   approved or written to HubSpot as the person. The one exception is
   ``ALLOWED_POST_PATHS``: Ask Cubie in a HubSpot-only form that saves nothing
   (two HubSpot tools, no chat history — see ``backend/routers/chat_impersonated.py``).
2. **A route allowlist, deny by default.** ``ALLOWED_GET_PREFIXES`` lists the
   surfaces whose data is HubSpot, EOS or the person's profile: My Day and the
   sales cockpit, EOS, call scripts. Everything else — email, calendar, Drive,
   transcripts, Fathom, call capture, chat, the home feed and briefing (both
   built from email), proposals and CRM Sync suggestions (built from calls and
   email) — is a 403. A new router is NOT reachable until it is added here.
3. **No Google account access at all.** While the header is present,
   ``rubik_core.tools._workspace_auth.load_workspace_credentials`` returns
   ``None``, so no code path in the request can open anyone's Gmail, Calendar,
   Drive or Meet — even one an allowed page calls indirectly.

On the sales cockpit, transcript TEXT (the quote behind a commitment) is also
withheld: ``backend.auth.cockpit_transcript_policy`` answers "none".
"""

from __future__ import annotations

from contextvars import ContextVar

from rubik_core.brand import PRODUCT_NAME

HEADER = "X-Impersonate-User"

#: Set by the HTTP middleware for the whole request when ``HEADER`` is present.
_active: ContextVar[bool] = ContextVar("user_impersonation_active", default=False)

#: GET-only surfaces open while impersonating. Prefix match on the URL path.
ALLOWED_GET_PREFIXES: tuple[str, ...] = (
    "/api/users/me",
    "/api/departments",
    "/api/sales/",  # My Day, daily rhythm, cockpit views
    "/api/eos/",  # rocks, to-dos, scorecard, issues, L10 meetings
    "/api/scripts",  # call scripts (drafted guides, not recordings)
    "/api/admin/impersonation/",  # the menu itself, to switch back
)

#: The only writes allowed: exact paths. Ask Cubie in its HubSpot-only,
#: nothing-saved form (backend/routers/chat_impersonated.py).
ALLOWED_POST_PATHS: frozenset[str] = frozenset({"/api/chat/impersonated"})

#: Any method. The session mint binds the session to the admin's REAL identity
#: (``get_current_user_unmasked``), so it must keep working mid-impersonation.
ALWAYS_ALLOWED_PREFIXES: tuple[str, ...] = ("/api/auth/",)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

BLOCKED_DETAIL = (
    "Not available while impersonating. Impersonation is read-only and leaves "
    f"out email, calendar, Drive and call transcripts; Ask {PRODUCT_NAME} answers from "
    "HubSpot only."
)


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """``/api/scripts`` matches itself and ``/api/scripts/…``, never
    ``/api/scriptsX``; a prefix ending in ``/`` matches what is under it."""
    for p in prefixes:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p or path.startswith(p + "/"):
            return True
    return False


def is_allowed(method: str, path: str) -> bool:
    """True iff a request carrying ``HEADER`` may proceed."""
    if _matches(path, ALWAYS_ALLOWED_PREFIXES):
        return True
    if "/api" not in path.lower():
        return True  # the SPA and its static assets — never an API route
    if method.upper() not in _READ_METHODS:
        return method.upper() == "POST" and path in ALLOWED_POST_PATHS
    return _matches(path, ALLOWED_GET_PREFIXES)


def set_active(value: bool):
    return _active.set(value)


def reset_active(token) -> None:
    _active.reset(token)


def impersonation_active() -> bool:
    """True while serving a request that carries ``HEADER``."""
    return _active.get()
