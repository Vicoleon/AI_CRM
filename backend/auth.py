from __future__ import annotations

import logging
import os
import sys

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from rubik_core.auth.registry import UserProfile, UserRegistry
from rubik_core.auth.session_tokens import SessionExpiredError, decode_session_token
from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.operations import (
    OperationsConfig,
    OperationsConfigUnavailable,
    get_operations_config,
)
from rubik_core.config import DEFAULT_DEV_USER_EMAIL, AppMode, settings
from rubik_core.control.repository import domain_belongs_to_workspace
from rubik_core.database.db import get_db
from rubik_core.features import effective_policy, feature_allows
from rubik_core.telemetry import set_admin_context
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.support_context import current_support_session

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)
user_registry = UserRegistry()

# Removed global GOOGLE_CLIENT_ID as it is now in settings


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP. Behind Cloud Run the real caller is the first
    hop of ``X-Forwarded-For``; ``request.client.host`` is the proxy."""
    xff = request.headers.get("x-forwarded-for")
    if isinstance(xff, str) and xff.strip():
        return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return host if isinstance(host, str) else None


def _user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua if isinstance(ua, str) else None


def _impersonation_policy() -> OperationsConfig | None:
    """Read the tenant's platform-managed policy, refusing an unavailable row."""
    try:
        return get_operations_config()
    except OperationsConfigUnavailable:
        logger.warning("Workspace impersonation policy unavailable")
        return None


def _may_impersonate(email: str, policy: OperationsConfig | None = None) -> bool:
    """SOC 2 H-4: the actor must appear in this workspace's allowlist."""
    policy = policy if policy is not None else _impersonation_policy()
    return bool(
        policy and email.strip().casefold() in policy.impersonation_allowed_emails
    )


def _apply_user_impersonation(
    request: Request, user: UserProfile, target: str
) -> UserProfile:
    """Resolve an allowlisted admin to a named person's REAL profile.

    Read-only and without email, calendar, Drive, transcripts or chat — those
    locks live in ``rubik_core.auth.impersonation`` and the middleware in
    ``backend.main``, and they apply to any request carrying the header,
    whether or not this swap is granted. Refusals here leave the caller as
    themselves (still restricted), and 403 only for a named-but-unknown person.
    """
    want = target.strip().lower()
    if not settings.USER_IMPERSONATION_ENABLED:
        return user
    policy = _impersonation_policy()
    if (
        "admin" not in (user.roles or [])
        or policy is None
        or not _may_impersonate(user.email, policy)
    ):
        logger.warning(
            f"Person impersonation DENIED for {user.email} -> {want} "
            f"(not an allowlisted admin); acting as self, restricted."
        )
        return user
    if want not in policy.user_impersonation_targets:
        logger.warning(
            f"Person impersonation DENIED for {user.email} -> {want} "
            "(not in workspace person-impersonation policy)"
        )
        raise HTTPException(
            status_code=403, detail="That person cannot be impersonated."
        )
    profile = user_registry.get_user(want)
    if profile is None:
        raise HTTPException(
            status_code=403, detail=f"That person has no {PRODUCT_NAME} account yet."
        )
    logger.warning(
        f"IMPERSONATION: {user.email} is viewing {PRODUCT_NAME} as {want} "
        f"(read-only) — {request.method} {request.url.path}"
    )
    request.state.impersonation = {
        "mode": "user",
        "actor_email": user.email,
        "target_email": want,
        "target_name": profile.name or want,
    }
    set_admin_context(False)
    data = profile.model_dump()
    data["roles"] = [r for r in (data.get("roles") or []) if r != "admin"]
    return UserProfile(**data)


def _apply_impersonation(request: Request, user: UserProfile) -> UserProfile:
    from rubik_core.auth.impersonation import HEADER as USER_HEADER

    if current_support_session() is not None:
        # A live platform support grant has one immutable actor. Neither
        # department nor person impersonation headers may create a pseudo-user.
        return user
    target = request.headers.get(USER_HEADER)
    if target:
        # A person, not a department. The department header is ignored when
        # both are sent — never stacked.
        return _apply_user_impersonation(request, user, target)
    if "admin" in user.roles:
        impersonate_dept = request.headers.get("X-Impersonate-Department")
        if impersonate_dept and not _may_impersonate(user.email):
            # An admin outside the allowlist tried to impersonate. Deny the
            # header (act as themselves) and record the attempt.
            logger.warning(
                f"Impersonation DENIED for {user.email} (not in "
                "workspace impersonation allowlist); header ignored, acting as self."
            )
            return user
        if impersonate_dept:
            logger.info(
                f"Admin {user.email} is impersonating department: {impersonate_dept}"
            )
            user_dict = user.model_dump()
            user_dict["department"] = impersonate_dept

            # Mask the email so the admin truly bypasses 'owner' specific access
            impersonated_email = (
                f"impersonated_user@{impersonate_dept.replace(' ', '').lower()}.local"
            )
            user_dict["email"] = impersonated_email

            # ─── CRITICAL FIX: Ensure the impersonated user exists in the DB ───
            # Table 'chat_sessions' has a foreign key to 'users', so we must have a record.
            existing = user_registry.get_user(impersonated_email)
            if not existing:
                logger.info(
                    f"Registering virtual impersonation user: {impersonated_email}"
                )
                user_registry.register_user(
                    email=impersonated_email,
                    name=f"Impersonated {impersonate_dept} User",
                    id=f"impersonated-{impersonate_dept.lower()}",
                    department=impersonate_dept,
                    roles=["user"],
                )
            # ───────────────────────────────────────────────────────────────────

            if "admin" in user_dict["roles"]:
                user_dict["roles"].remove("admin")
            return UserProfile(**user_dict)
    return user


def _apply_access_gates(user: UserProfile, email: str) -> UserProfile:
    """Keep the role provisioned in this workspace; never grant it on login."""
    if "admin" in user.roles:
        set_admin_context(True)
    return user


def require_eos_module() -> None:
    """Control-side EOS switch; safe to run before tenant DB dependencies."""
    from rubik_core.control.repository import module_enabled

    if not module_enabled("eos"):
        raise HTTPException(status_code=404, detail="EOS is not enabled")


def _mock_developer_email() -> str:
    """The identity mock auth signs in as when no mock user is named.

    ``CUBIE_DEV_USER_EMAIL`` (default ``dev@acme.example``): a fictional
    developer, configured rather than derived, so no real address is ever
    minted into a local database.
    """
    configured = getattr(settings, "CUBIE_DEV_USER_EMAIL", "")
    email = configured.strip().lower() if isinstance(configured, str) else ""
    return email or DEFAULT_DEV_USER_EMAIL


def _authenticate_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> UserProfile:
    """Resolve a locally verified Cubie session inside its selected workspace.

    Google ID tokens are accepted only by POST /api/auth/session, which verifies
    them before selecting a workspace. Every ordinary API request uses our
    signed, workspace-bound JWT; opaque Google access tokens never reach the DB.
    """
    token = credentials.credentials if credentials else ""
    try:
        claims = decode_session_token(token) if token else None
    except SessionExpiredError:
        raise HTTPException(
            status_code=401, detail="Session expired. Please sign in again."
        )

    if (
        settings.ENABLE_MOCK_AUTH
        and settings.APP_MODE != AppMode.GCP
        and not os.environ.get("K_SERVICE")
        and not claims
    ):
        developer = _mock_developer_email()
        email = developer
        mock_user = request.headers.get("X-Mock-User")
        if mock_user:
            email = mock_user
        elif credentials and "@" in credentials.credentials:
            email = credentials.credentials

        workspace = current_workspace()
        if workspace != "test" or "pytest" not in sys.modules:
            domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
            if not domain or not domain_belongs_to_workspace(workspace, domain):
                raise HTTPException(status_code=401, detail="Mock workspace mismatch")

        logger.info("Using MOCK authentication for user: %s", email)
        user = user_registry.get_user(email)
        if not user:
            user = user_registry.register_user(
                email, f"Mock {email.split('@')[0]}", "mock-user-123"
            )
        return user

    if not credentials:
        raise HTTPException(
            status_code=401, detail="Missing authentication credentials"
        )

    if token.startswith(("atlx_", "cbx_")):
        raise HTTPException(
            status_code=401, detail=f"This key only works in {PRODUCT_NAME} for Chrome."
        )
    if claims is None:
        raise HTTPException(
            status_code=401, detail="Invalid session. Please sign in again."
        )
    if claims.ws != current_workspace():
        raise HTTPException(
            status_code=401,
            detail={"code": "workspace_mismatch", "message": "Sign in again"},
        )
    if claims.typ == "support":
        grant = current_support_session()
        if (
            grant is None
            or claims.sup != claims.sub
            or grant.actor != claims.sup
            or grant.sid != claims.sid
            or grant.workspace_slug != claims.ws
            or getattr(request.state, "platform_support", None) != grant
        ):
            raise HTTPException(status_code=401, detail="Support session is invalid")
        try:
            target_user = user_registry.get_user(grant.actor)
        except Exception:
            logger.exception("Database failure during support authentication")
            raise HTTPException(status_code=500, detail="Internal Server Error")
        if (
            target_user is None
            or not getattr(target_user, "is_platform_support", False)
            or bool(target_user.roles)
            or bool(target_user.is_exec)
            or bool(target_user.groups)
            or bool(target_user.workspace_groups)
            or bool(target_user.managed_departments)
            or target_user.department != "general"
        ):
            raise HTTPException(
                status_code=401, detail="Support identity is unavailable"
            )
        # This in-memory capability is justified by the middleware's live
        # control grant. The target User row stays role-free and non-admin.
        set_admin_context(True)
        return UserProfile(
            **{
                **target_user.model_dump(),
                "name": "RubikSoft platform support",
                "roles": ["admin", "user"],
                "is_exec": False,
                "department": "general",
                "managed_departments": [],
                "groups": [],
                "workspace_groups": [],
            }
        )
    if claims.typ != "user":
        raise HTTPException(
            status_code=401, detail="Invalid session. Please sign in again."
        )
    # The signed hosted domain, not the email suffix, owns the session. Check
    # its live control binding before opening the selected tenant database.
    if not (claims.ws == "test" and "pytest" in sys.modules):
        if not claims.hd or not domain_belongs_to_workspace(claims.ws, claims.hd):
            raise HTTPException(
                status_code=401,
                detail={"code": "workspace_mismatch", "message": "Sign in again"},
            )
    try:
        user = user_registry.get_user(claims.sub)
    except Exception:
        logger.exception("Database failure during session auth")
        raise HTTPException(
            status_code=500, detail="Internal Server Error during authentication"
        )
    if not user:
        raise HTTPException(status_code=401, detail="Unknown session user")
    if getattr(user, "is_platform_support", False):
        # A flagged FK row is usable only through its live control sid. Even
        # a valid normal JWT for the same email cannot inherit support access.
        raise HTTPException(
            status_code=401, detail="Support identity requires support session"
        )
    return _apply_access_gates(user, claims.sub)


def _stash_actor(
    request: Request, real: UserProfile, effective: UserProfile | None = None
) -> None:
    """Note on ``request.state`` who REALLY made this request, for Insights.

    The usage middleware in ``backend.main`` reads ``actor_email`` /
    ``actor_department`` after the response. Stashed from the authenticated
    person BEFORE impersonation masks them, and ``actor_impersonating`` is set
    when the effective identity differs (department impersonation leaves no
    ``request.state.impersonation`` of its own), so the middleware can skip the
    request: an admin looking as someone must not count as that person's usage.
    Never raises — telemetry must not fail authentication.
    """
    try:
        request.state.actor_email = (real.email or "").strip().lower() or None
        request.state.actor_department = real.department
        if current_support_session() is not None:
            request.state.actor_impersonating = True
        if (
            effective is not None
            and (effective.email or "").strip().lower()
            != (real.email or "").strip().lower()
        ):
            request.state.actor_impersonating = True
    except Exception:  # noqa: BLE001
        logger.debug("could not stash the request actor", exc_info=True)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> UserProfile:
    real = _authenticate_user(request, credentials)
    _stash_actor(request, real)
    user = _apply_impersonation(request, real)
    if user is not real:
        _stash_actor(request, real, user)
    return user


def get_current_user_unmasked(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> UserProfile:
    """Like get_current_user but WITHOUT admin department-impersonation
    masking. Used by the session mint (POST /api/auth/session), which must
    bind the session to the admin's real identity — an impersonation header
    left over in localStorage must never produce a session JWT whose subject
    is the `impersonated_user@….local` pseudo-user."""
    user = _authenticate_user(request, credentials)
    _stash_actor(request, user)
    return user


async def bind_usage_user(
    request: Request, user: UserProfile = Depends(get_current_user)
) -> None:
    """Stamp the signed-in person on every AI call this request makes.

    Mounted on the ``core`` parent in ``backend.main`` (after the copilot gate),
    so Admin → AI Cost and Insights' AI Spend can split request-path spend by
    person and department; before it, ``llm_usage_events.user_email`` was empty
    for nearly every call. The REAL person when an admin is impersonating: the
    spend is theirs.

    ASYNC on purpose. FastAPI runs a sync dependency in a worker thread on a
    COPY of the context, so a ContextVar set there never reaches the endpoint.
    An async dependency runs in the request's own task: the endpoint sees the
    value, and so does any work it hands to ``run_in_threadpool`` /
    ``asyncio.to_thread``, which copy the context. Never raises.
    """
    try:
        from rubik_core.cost.usage import current_user_email

        email = getattr(request.state, "actor_email", None) or getattr(
            user, "email", None
        )
        if email:
            current_user_email.set(str(email).strip().lower())
    except Exception:  # noqa: BLE001 — attribution never fails a request
        logger.debug("could not bind the usage user", exc_info=True)


def admin_required(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ---- The copilot (the non-EOS app) -----------------------------------------
#
# The AI copilot (Chat, Daily Briefing, Productivity, Assets, Meeting Prep,
# Settings) and EOS share one URL. Everyone in the workspace gets the copilot
# by default; an admin can narrow it by department from Admin → Feature Access
# (`feature_allows("copilot", user)`), and whoever is narrowed out keeps EOS.
# `/api/users/me` ships the server's verdicts as `features`, so the client
# mirrors no roster.
#
# Departments are matched by RAG slug (case-insensitive; "Sales"/"sales" are one
# team — see docs/ENGINEERING_NOTES.md, "A department's identity is its RAG slug").


def user_can_access_full_app(user: UserProfile) -> bool:
    """True iff ``user`` may use the non-EOS copilot.

    Everyone by default. The live roster is the ``copilot`` entry in Admin →
    Feature Access; whoever it leaves out is limited to EOS.
    """
    return feature_allows("copilot", user)


def require_full_app_access(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """FastAPI dependency — 403 unless the caller may use the non-EOS copilot.

    Mounted once on the parent `/api` router carrying the copilot routers (see
    ``backend/main.py``). EOS and identity endpoints (``/api/eos/*``,
    ``/api/users/me``, ``/api/departments``) stay ungated so EOS-only users keep
    working.

    NOTE: Cloud-Scheduler endpoints that authenticate with only an
    ``X-Scheduler-Key`` (no user token) must NOT sit behind this gate — it
    resolves a user and would 401 them. They are mounted on the ungated
    ``agents.scheduler_router`` / jobs router instead, so this human gate
    never sees them.
    """
    if not user_can_access_full_app(user):
        raise HTTPException(
            status_code=403,
            detail="This area is not turned on for you. Ask your admin.",
        )
    return user


def user_can_access_xray(user: UserProfile) -> bool:
    """True iff ``user`` may use Customer X-Ray (UC-45).

    Everyone by default; the live roster is the ``customer_xray`` entry in
    Admin → Feature Access. Narrowed to departments, team membership counts
    whether the team is the user's primary department OR a managed department
    (a Team Manager who sits elsewhere still qualifies), and any exec still
    qualifies — X-Ray is a seniority-scoped view of an account, not an action
    on one AE's own deals (contrast ``user_can_access_crm_sync``).
    """
    return feature_allows("customer_xray", user)


# ---- My To-Dos (the promises ledger) --------------------------------------
#
# Roster lives in Admin → Feature Access (registry key "todos"); the env kill
# switch TODOS_ENABLED is checked by the router itself (404). This
# predicate exists so ``resolve_all`` parity tests and the chat/Slack gates
# have one name to call.


def user_can_access_todos(user: UserProfile) -> bool:
    """True iff ``user`` may use My To-Dos (the personal promises ledger)."""
    return feature_allows("todos", user)


# ---- Proposal Generator ------------------------------------------------------
#
# Roster lives in Admin → Feature Access (registry key "proposal_generator");
# the env kill switch (PROPOSALS_ENABLED) is checked by the router's own
# dependency. Reached only from a call the person hosted, so the effective
# audience is this roster ∩ Sales Call Capture's.


def user_can_access_proposals(user: UserProfile) -> bool:
    """True iff ``user`` may draft client proposals from their sales calls."""
    return feature_allows("proposal_generator", user)


def require_sales_or_admin(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """FastAPI dependency — 403 unless the caller may use Customer X-Ray.

    Mounted on the ``/api/xray`` router (see ``backend/main.py``). The xray
    router ALSO sits under the copilot-gated ``core`` parent, so both gates
    apply; this narrower one is the effective rule and the single source of
    truth.
    """
    if not user_can_access_xray(user):
        raise HTTPException(
            status_code=403,
            detail="Customer X-Ray is not turned on for you. Ask your admin.",
        )
    return user


def user_can_access_crm_sync(user: UserProfile) -> bool:
    """True iff ``user`` may use CRM Sync (UC-46).

    Everyone by default; the live roster is the ``crm_sync`` entry in Admin →
    Feature Access. Department membership counts whether it's the user's
    primary department OR a managed one, so a Team Manager sitting elsewhere
    still qualifies. The client reads the server's verdict from
    ``/api/users/me``.

    Narrowed to departments, it stays narrower than X-Ray's gate: ``is_exec``
    grants nothing here. The feature acts on one AE's own deals and
    transcripts, so access follows the work rather than seniority.
    """
    return feature_allows("crm_sync", user)


def require_crm_sync(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """FastAPI dependency — 403 unless the caller may use CRM Sync.

    Also enforces the ``CUBIE_CRM_SYNC_ENABLED`` kill switch, so the flag check
    lives in exactly one place instead of at the top of every endpoint. The
    feature ships with the flag off: HubSpot writes stay unreachable until the
    write token and scopes have been verified against the live portal.
    """
    from rubik_core.config import settings  # local: keep the leaf import light

    if not settings.CUBIE_CRM_SYNC_ENABLED:
        raise HTTPException(
            status_code=403, detail="CRM Sync is not enabled for this workspace."
        )
    if not user_can_access_crm_sync(user):
        raise HTTPException(
            status_code=403,
            detail="CRM Sync is not turned on for you. Ask your admin.",
        )
    return user


# ---- Smart Teams — the company-wide surface --------------------------------
#
# Smart Teams gives every employee a working guide: they take the Work Style
# Interview, review and approve their own guide, and read anyone else's
# PUBLISHED guide. That audience is independent of the copilot's — a guide is
# only worth writing if the whole company has one, and the interview is how it
# gets written — so the Smart Teams routers are mounted straight on `app` with
# their own gate, NOT under the copilot-gated `core` parent (the `tours`
# pattern; see backend/main.py). Comparison and Team Builder stack
# `require_full_app_access` on top of this one: those read across people and
# staff projects, so a team narrowed out of the copilot loses them too.
#
# The membership test is "a signed-in employee on the Workspace domain", and
# this gate establishes that itself rather than inheriting it. Normal sessions
# are bound to a control-DB workspace and rechecked against its domain rows.
#
# Mock mode binds the workspace from its email before this gate, but its
# identity is intentionally synthetic. The Smart Teams employee rule remains
# an independent defense for private interview answers, scores and guides.
#
# The dependency also exists so the routers declare their audience instead of
# silently inheriting it, so a future narrowing has exactly one place to happen,
# and so the impersonation pseudo-user is refused once here rather than in every
# handler.
#
# WHOSE profile a caller may read is a different question, and not one a
# router-level dependency can answer — it turns on path/row data. That lives in
# rubik_core.smart_teams.authz (`can_view_raw_profile`, `can_view_guide`): raw
# interview answers and scores belong to the person plus support admins, while a
# guide goes company-visible only once its subject publishes it. Everything
# behind this gate is ADVISORY — collaboration and staffing only, never
# performance review, promotion, or compensation.


def user_can_access_smart_teams(user: UserProfile) -> bool:
    """True iff ``user`` may use the company-wide Smart Teams surface.

    Every employee on the Workspace domain qualifies. Two identities do not:

    * anything off the domain — enforced here too, including mock identities.
    * the pseudo-user minted by admin department impersonation
      (``impersonated_user@….local`` — see ``_apply_impersonation``), which is
      not a person and must not acquire an interview, a profile or a guide.

    Both live in ``rubik_core.smart_teams.authz.is_employee`` because the
    per-subject helpers there need the same rule, and a second copy of it would
    eventually disagree with this one.
    """
    from rubik_core.smart_teams.authz import is_employee  # local: keep leaf light

    if not is_employee(getattr(user, "email", None)):
        return False
    return _within_smart_teams_rollout(user)


def _within_smart_teams_rollout(user: UserProfile) -> bool:
    """True iff ``user`` is inside the current rollout.

    The interview is company-wide BY DESIGN (plan.md decision 4), and it ships
    open to everyone. The ``smart_teams`` entry in Admin → Feature Access is
    how an admin stages it instead — a dry run on one team before the
    company-wide announcement — with no config change and no redeploy.

    Deliberately NOT modelled on ``user_can_access_full_app``, which waves
    through ``is_admin`` and ``is_exec``. During a one-team dry run, "the
    Executive Team can see it" is not a footnote — they are precisely who it is
    being rehearsed for, and letting them in early is how a dry run turns into
    the launch by accident. Admins outside the rollout team are likewise not
    automatically in. An empty narrowed roster is a closed door, never an open
    one.
    """
    return feature_allows("smart_teams", user)


def require_smart_teams_access(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """FastAPI dependency — 403 unless the caller may use Smart Teams.

    Declared on the Smart Teams routers themselves so no endpoint can be added
    without it. Deliberately NOT ``require_full_app_access``: fencing the
    interview behind the copilot would lock out whoever an admin narrowed out
    of it — and they would still be nudged weekly to complete something they
    cannot open.

    Note that the caller is resolved ONLY from ``get_current_user``. No Smart
    Teams surface may take its subject from a header, query param or request
    body: this is the one feature holding people's private interview answers, so
    an accepted ``user_email`` would be a plain impersonation primitive over it.

    The two refusals get different copy because they need different actions: an
    impersonating admin switches back to themselves, while an off-domain caller
    (only reachable through local mock auth) has nothing to switch to.
    """
    if not user_can_access_smart_teams(user):
        from rubik_core.smart_teams.authz import (  # local: keep leaf light
            is_impersonation_identity,
        )

        if is_impersonation_identity(getattr(user, "email", None)):
            detail = (
                "Smart Teams works on your own account — switch out of "
                "department impersonation to use it."
            )
        else:
            from rubik_core.company.profile import (  # local: keep leaf light
                YOUR_COMPANY,
                company_possessive,
                get_company_profile,
            )

            company = company_possessive(get_company_profile(), YOUR_COMPANY)
            detail = f"Smart Teams is limited to {company} Workspace accounts."
        raise HTTPException(status_code=403, detail=detail)
    return user


# ---- Cubie Sales Cockpit — four role-scoped views ---------------------------
#
# The cockpit shows one thing four ways (the AUTHORITATIVE access table):
#
#   Role                 Commitments        Transcript text            Aggregates
#   AE                   own                own calls                  own
#   Manager              team               team calls                 team
#   Supervisor           team METADATA      NONE, no exception —       none
#                        (company, owner,   including deals they are
#                        age, state)        working
#   Exec                 counts only        NONE                       org
#
# The one reading of that table worth stating: "NONE" means none of ANOTHER
# person's calls. Nobody is denied the transcript of a call they hosted
# themselves, because hosting a call is being the AE on it — which is a
# different claim from being on the deal, and being on the deal is exactly the
# exception the table refuses the supervisor.
#
# WHY THESE GATES LOOK LIKE THIS. There is no role separation in Cubie to
# inherit. The `users` table has no `manager_id`, no `team_id` and no
# `hubspot_owner_id`; `managed_departments` is a JSON list of department NAME
# STRINGS with no FK (it says which teams you lead, never who reports to you);
# and `roles` rows are free-form strings of which only "admin" and "user" are
# special-cased anywhere. So:
#
#   * WHO you are is decided here, from department slug + `managed_departments`
#     + `is_exec` + admin — the same signals every other gate in this file uses.
#   * WHOSE ROWS you see is decided in `backend/routers/eos/_authz.py`
#     (`email_scope`, `require_team_scope`), because the only real
#     supervisor-of edge in the system is `eos_seats.reports_to_id` bridged to
#     `users` by `eos_people.email`. One org graph, promoted — NOT copied into
#     `rubik_core/cockpit/`.
#
# There is deliberately NO "seniority wins" rule, because this file does not
# have one to inherit: the copilot gate admits admin + `is_exec`, CRM Sync admits
# admin while `is_exec` grants nothing, and Smart Teams admits neither. Each
# gate below states its own choice in its docstring.
#
# DATA PRECONDITIONS — not code, and they will bite before any gate here runs:
#
#   1. The cockpit routers mount on the copilot-gated `core` parent, so a team
#      an admin has narrowed out of the copilot loses the cockpit with it,
#      whatever the cockpit rows say.
#   2. The sales manager must be recorded as the manager of Sales — "Sales" in their
#      `managed_departments`, or an Accountability-Chart seat flagged
#      `is_leadership` over Sales. Neither is inferrable: nothing in `users`
#      says who reports to whom. AND the AEs must exist as `eos_people` rows
#      SEATED in that department, because `eos_people.email` is the only
#      email→person bridge there is. Leading a department on paper is not
#      enough. When either half is unfinished the manager view answers 409
#      `team_unresolved` rather than an empty list — see
#      `_authz.team_scope_problem`, and never "fix" that by defaulting open.
#   3. `update_user()` silently DROPS unknown role names, so none of these
#      gates keys on a new role string. Do not add one without creating the
#      `roles` row in dev Cloud SQL, in CI, and in every test DB first.
#
# The admin department-impersonation pseudo-user (`impersonated_user@….local`)
# passes these gates when it is masked into a cockpit department, and that is
# survivable rather than a hole: it has no `eos_people` row, so any team view it
# opens answers 409 `org_record_missing` instead of showing a believable empty
# team. Its own My Day is empty because commitments key on a real address.
#
# Cross-person READS (the manager, supervisor and exec views all read other
# people's rows) must write an audit row — `backend.audit.record_audit`. A gate
# is not an audit trail.
#
# The frontend reads the four verdicts from `/api/users/me` (`features.
# sales_cockpit`, `…_manager`, `…_supervisor`, `…_exec`) as canAccessCockpit /
# canAccessCockpitManager / canAccessCockpitSupervisor / canAccessCockpitExec in
# `frontend/src/context/AuthContext.tsx` — no client-side roster, nothing to
# keep in step. What it still cannot see is `COCKPIT_ENABLED` (a server
# setting) and the EOS-seat fallback in `require_cockpit_manager` (it needs a
# session) — both fail CLOSED on the frontend, which hides a link rather than
# showing one that 403s.

# ── Who each view ships with ─────────────────────────────────────────────────
#
# The LIVE audience of each view is its own entry in Admin → Feature Access
# (`sales_call_capture`, `sales_cockpit`, `sales_cockpit_manager`,
# `sales_cockpit_supervisor`, `sales_cockpit_exec`); the registry defaults are:
#     call capture, My Day -> everyone
#     manager roll-up      -> admins; an admin adds the sales department
#                             (judged on MANAGED departments)
#     supervisor worklist  -> admins; an admin adds the delivery team
#     exec roll-up         -> the Exec-team flag (and admins), no department
#
# This is narrower than the feature flags, on purpose: CCS_ENABLED /
# COCKPIT_ENABLED decide whether the feature RUNS AT ALL, the registry decides
# who may reach it once it does.


def _require_cockpit_enabled() -> None:
    """404 unless ``COCKPIT_ENABLED``.

    404, not 403: a feature that is off is not a permission problem, and a
    half-provisioned environment must not half-run the cockpit. Checked in the
    gates so the flag is enforced once for every cockpit endpoint instead of at
    the top of each handler.
    """
    from rubik_core.config import settings  # local: keep the leaf import light

    if not settings.COCKPIT_ENABLED:
        raise HTTPException(
            status_code=404, detail="The Sales Cockpit is not enabled here."
        )


def user_can_access_cockpit(user: UserProfile) -> bool:
    """True iff ``user`` may open the Sales Cockpit at all (the AE surface).

    Everyone by default; the live roster is the ``sales_cockpit`` entry in
    Admin → Feature Access.

    Narrowed to departments: ``is_admin`` YES — they run and support the app,
    as in every gate here bar Smart Teams. ``is_exec`` YES — matching the
    copilot gate rather than CRM Sync,
    because the exec view is the thinnest surface in the feature (counts, no
    per-person drill) and it stacks on this one; an exec who lands on My Day
    sees only commitments they personally own, which for an exec is normally
    nothing. Access to a ROW is a separate question answered by
    ``_authz.email_scope``, and transcript TEXT is a third question answered by
    ``user_can_read_transcript_text`` — passing this gate settles none of them.
    """
    return feature_allows("sales_cockpit", user)


def user_can_access_cockpit_manager(user: UserProfile) -> bool:
    """True iff ``user`` may open the MANAGER roll-up (the manager's view).

    The mirrorable half of the manager gate: admins, anyone named on the
    ``sales_cockpit_manager`` entry in Admin → Feature Access, plus anyone whose
    ``managed_departments`` includes a department on it. It ships with admins
    only; an admin adds the sales department. A Team Manager who sits in another
    department still qualifies — that is what ``managed_departments`` is for.

    MANAGED departments only, never the primary one (the feature's membership
    is ``managed_only``): every AE sits in Sales, so matching the primary
    department would promote all of them. The sales manager therefore needs "Sales" in their
    ``managed_departments``, or an EOS leadership seat in Sales — precondition 2
    in the block above, and the reason the DB path in
    ``require_cockpit_manager`` exists at all.

    An exec who ALSO manages a cockpit team (say, an exec over five
    departments) passes — through the team they manage, not through their
    seniority. "Team Manager" is decoupled from ``is_exec`` everywhere in this
    codebase and stays decoupled here.

    ``is_exec`` grants NOTHING here, following CRM Sync rather than the copilot
    gate, and this one is load-bearing rather than stylistic: the manager view
    carries breaches by AE *with the reason* and team transcript text, while
    the access table gives the exec counts and no transcript text at all. Letting
    ``is_exec`` (or ``sees_all_departments``) in through this door would quietly
    hand EVERY exec the per-person drill-down the design removed on purpose —
    in effect a per-AE leaderboard — and it would do so as a side effect of a
    seniority flag nobody set with this view in mind.

    A person named by EMAIL on that entry does get in, and that is a named
    decision rather than a side effect. Naming an org-wide exec
    (``sees_all_departments``) is the sharp case: their ``_authz.email_scope``
    is ``unrestricted`` and this gate makes ``cockpit_transcript_policy`` return
    "team" — so they read every AE's call text, not one team's. That is the
    per-AE exposure a name buys, it is bought one name at a time, and removing
    the name withdraws it.

    ``require_cockpit_manager`` admits ONE further path this predicate cannot
    see, because it needs a DB session: an EOS Accountability-Chart leadership
    seat in a department on that entry. That path grants team ROWS but, by
    design, no team transcript text (see ``cockpit_transcript_policy``) — it
    fails closed, and the fix is to set ``managed_departments``.
    """
    return feature_allows("sales_cockpit_manager", user)


def user_can_access_cockpit_supervisor(user: UserProfile) -> bool:
    """True iff ``user`` may open the SUPERVISOR worklist (the supervisor's view).

    Admins by default; an admin adds the delivery team on the
    ``sales_cockpit_supervisor`` entry in Admin → Feature Access. ``is_exec``
    grants nothing: the
    worklist is operational work whose one action WRITES on someone else's
    behalf (a HubSpot task assigned to an AE, a Slack DM, a logged nudge), so
    access follows the work rather than seniority — the CRM Sync rule. Admins
    are in because the nudge path cannot otherwise be exercised or supported.

    This gate says WHO may open the worklist. It cannot say WHAT they see:
    "team metadata, never transcript text" is a row/field rule and belongs to
    the serializer via ``user_can_read_transcript_text``.
    """
    return feature_allows("sales_cockpit_supervisor", user)


def user_can_access_cockpit_exec(user: UserProfile) -> bool:
    """True iff ``user`` may open the EXEC roll-up (the executive's view).

    ``is_exec`` — the Exec-team flag — plus admins, which is how it ships
    (the ``sales_cockpit_exec`` entry in Admin → Feature Access). This is the
    one gate in the feature where ``is_exec`` IS the rule: the view is three
    numbers and a trend for the whole org, with no per-person drill-down, and
    keeping it that thin is what stops it becoming a per-AE leaderboard
    (Google's Meet API terms). Admins are in to verify the numbers; note that
    admin here buys aggregates only, and buys no transcript text anywhere in
    the cockpit.

    It ships with no department on purpose: there is no Exec department row to
    match, and an org-wide exec has no single home department by construction.
    """
    return feature_allows("sales_cockpit_exec", user)


def user_can_access_ccs(user: UserProfile) -> bool:
    """True iff ``user`` may reach Sales Call Capture.

    Everyone by default; the live roster is the ``sales_call_capture`` entry
    in Admin → Feature Access, where admins are always in to support the app.

    Narrowed to departments, ``is_exec`` grants nothing: CCS is the surface
    that CREATES a recording-adjacent
    artifact of a client conversation. Access should follow the people who host
    those calls and the people who support the tooling, never seniority. The
    exec surface in this feature is three aggregate numbers, and it lives on
    ``require_cockpit_exec``.

    Passing this settles nothing about a specific call: reading one is gated
    per-session in the router, and transcript TEXT is a third question answered
    by ``user_can_read_transcript_text``.
    """
    return feature_allows("sales_call_capture", user)


def require_ccs(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """FastAPI dependency — the Sales Call Capture audience gate.

    Declared on every ``/api/calls`` endpoint. That router also sits under the
    copilot-gated ``core`` parent, so both apply; this narrower one is the
    effective rule.

    A disabled feature answers 404 before the roster is checked. The feature
    policy also includes this kill switch, so checking it here keeps a disabled
    workspace from reporting a misleading permission error.
    """
    if not settings.CCS_ENABLED:
        raise HTTPException(
            status_code=404, detail="Sales call capture is not enabled here."
        )
    if not user_can_access_ccs(user):
        raise HTTPException(
            status_code=403,
            detail="Sales Call Capture is not turned on for you. Ask your admin.",
        )
    return user


def require_cockpit(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """FastAPI dependency — the AE-level cockpit gate.

    Declared on the cockpit / commitments routers themselves. Those routers
    also sit under the copilot-gated ``core`` parent, so both apply; this narrower
    one is the effective rule and the single source of truth. Also enforces
    ``COCKPIT_ENABLED`` (404 when off) so the flag lives in one place.
    """
    _require_cockpit_enabled()
    if not user_can_access_cockpit(user):
        raise HTTPException(
            status_code=403,
            detail="The Sales Cockpit is not turned on for you. Ask your admin.",
        )
    return user


def require_cockpit_manager(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserProfile:
    """FastAPI dependency — 403 unless the caller may open the manager roll-up.

    Two admitting paths. The pure one (``user_can_access_cockpit_manager``) is
    what the frontend mirrors. The second consults EOS: a caller holding an
    Accountability-Chart seat flagged ``is_leadership`` whose led departments
    include a department on the ``sales_cockpit_manager`` entry is a real
    manager of that team even
    with an empty ``managed_departments``, and refusing them would 403 the one
    person the view is built for over a missing JSON field. It takes a session
    for that reason alone; a permission check must not become an outage, so a
    DB failure on that secondary path is logged and treated as "not a manager"
    rather than a 500.

    Stacks on ``require_cockpit`` conceptually but does NOT call it — declare
    both on a manager-only router if you want both messages, or this one alone
    (it is strictly narrower for every path except an EOS-seat manager sitting
    outside the team, who legitimately manages it without belonging to it).
    """
    _require_cockpit_enabled()
    if not user_can_access_cockpit_manager(user) and not _eos_leads_a_cockpit_team(
        user, db
    ):
        raise HTTPException(
            status_code=403,
            detail="The team roll-up is not turned on for you. Ask your admin.",
        )
    return user


def _eos_leads_a_cockpit_team(user: UserProfile, db: Session) -> bool:
    """True iff the caller holds an EOS leadership SEAT over a cockpit team —
    a department on the live ``sales_cockpit_manager`` entry.

    The only manager signal that does not live on the user row. Reads the same
    org graph as the cockpit's row scoping — ``backend/routers/eos/_authz.py``,
    imported locally because ``_authz`` pulls in the whole EOS model layer and
    this module is imported by every router in the app.

    Gated on the seat flag first so this cannot become a second, quieter copy
    of the ``managed_departments`` rule: ``_authz.is_leadership`` deliberately
    returns True for admins, execs and Team Managers too, and re-checking those
    here would let an exec in through the manager door that
    ``user_can_access_cockpit_manager`` closes on purpose.
    """
    try:
        from backend.routers.eos import _authz  # local: heavy leaf, avoid cycles
        from rubik_core.rag.config import slugify_department

        manager_departments = effective_policy("sales_cockpit_manager", db)[
            0
        ].departments
        if not manager_departments:
            return False
        person = _authz.person_by_email(db, getattr(user, "email", None))
        seat = _authz.seat_for_person(person, db)
        if not bool(getattr(seat, "is_leadership", False)):
            return False
        led = _authz.led_depts(user, seat, _authz.user_depts(user, seat))
        return any(slugify_department(d or "") in manager_departments for d in led)
    except Exception:
        logger.warning(
            "Cockpit manager gate: EOS leadership lookup failed for "
            f"{getattr(user, 'email', None)}; treating as not-a-manager.",
            exc_info=True,
        )
        return False


def require_cockpit_supervisor(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """FastAPI dependency — 403 unless the caller may open the supervisor worklist.

    NECESSARY BUT NOT SUFFICIENT. It admits the supervisor to a surface whose
    defining rule — team metadata, never transcript text, no exception,
    including deals they are working — is per-FIELD and therefore unreachable
    from a dependency. Every serializer behind this gate must ask
    ``user_can_read_transcript_text`` per row.
    """
    _require_cockpit_enabled()
    if not user_can_access_cockpit_supervisor(user):
        raise HTTPException(
            status_code=403,
            detail="The nudge worklist is not turned on for you. Ask your admin.",
        )
    return user


def require_cockpit_exec(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """FastAPI dependency — 403 unless the caller may open the exec roll-up.

    The endpoint behind it returns AGGREGATES ONLY. That is a shape decision
    the gate cannot enforce: if a per-person breakdown is ever added to an
    exec-gated response it silently becomes the leaderboard the design
    refused. Drill-down belongs behind ``require_cockpit_manager``.
    """
    _require_cockpit_enabled()
    if not user_can_access_cockpit_exec(user):
        raise HTTPException(
            status_code=403,
            detail="The executive roll-up is not turned on for you. Ask your admin.",
        )
    return user


# ---- Cockpit transcript text: a FIELD-level rule ---------------------------
#
# The access table gives the AE their own calls' transcript text, the manager their
# team's, and the supervisor and exec none. No router-level dependency can
# express that — it turns on the row being serialized — so it lives here as a
# pure policy pair the serializers call per row, and the gates above say so in
# their docstrings.
#
# Note what is deliberately NOT a grant: `_authz.EmailScope.unrestricted`
# (Cubie admin, org-wide Exec) widens the ROW LIST and must never be passed as a
# team-email set. The precedent is EOS's own L10 rule — an admin is an operator,
# not an executive, and does not read another team's confidential content by
# holding the role.

COCKPIT_TRANSCRIPT_POLICIES: tuple[str, ...] = ("team", "own", "none")


def cockpit_transcript_policy(user: UserProfile) -> str:
    """How much transcript text ``user`` may read: "team", "own" or "none".

    The closed set is ``COCKPIT_TRANSCRIPT_POLICIES``; return the value in the
    response envelope so the UI can label a metadata-only view honestly instead
    of rendering an empty quote box.

    A manager admitted solely by the EOS-seat path in
    ``require_cockpit_manager`` reports "own" here, so they get team rows with
    no team transcript text. That under-grant is the safe direction and the fix
    is a data one (set ``managed_departments``) — never widen this by taking
    the verdict from the request path, which would make a field-level
    confidentiality rule depend on which URL was called.
    """
    from rubik_core.auth.impersonation import impersonation_active

    if impersonation_active():
        # Person impersonation never shows call transcript text.
        return "none"
    if not user_can_access_cockpit(user):
        return "none"
    if user_can_access_cockpit_manager(user):
        return "team"
    return "own"


def user_can_read_transcript_text(
    user: UserProfile,
    *,
    call_owner_email: str | None,
    team_emails: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """True iff ``user`` may see the TEXT of this call's transcript.

    ``call_owner_email`` is the call's host — the AE who was on it — not the
    deal owner, and ``team_emails`` is the manager's resolved team from
    ``_authz.email_scope(...).owner_emails``. Pass an empty set on every
    non-manager surface.

    The supervisor and exec rows resolve to False on both counts: their policy
    is not "team", so a team set passed here by mistake changes nothing. The one
    carve-out is a caller's OWN call, which every policy keeps — hosting a call
    is being the AE on it, and the access table's "no exception, including
    deals they are working" is about being on the DEAL, which grants nothing here.

    An unattributed row (no host email) yields False: unknown provenance is not
    a reason to show content.
    """
    policy = cockpit_transcript_policy(user)
    if policy == "none":
        return False
    owner = (call_owner_email or "").strip().lower()
    if not owner:
        return False
    if owner == (getattr(user, "email", "") or "").strip().lower():
        return True
    if policy != "team":
        return False
    return owner in {(e or "").strip().lower() for e in team_emails}
