from __future__ import annotations

# backend/routers/users.py
from functools import partial
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import (
    admin_required,
    get_current_user,
    require_eos_module,
    user_registry,
)
from backend.jobs.launcher import (
    reject_support_background_work,
    submit_background_work,
)
from backend.models.role import ResourcePermissions, Role
from rubik_core.auth.permissions import visible_permissions
from rubik_core.auth.registry import UserProfile
from rubik_core.automation import automations_enabled
from rubik_core.config import AppMode, settings
from rubik_core.control.repository import is_platform_admin
from rubik_core.database.db import get_db
from rubik_core.scripts.sync_meet_transcripts import run_sync

router = APIRouter(prefix="/users", tags=["users"])


class UserUpdate(BaseModel):
    name: str | None = None
    roles: list[str] | None = None
    department: str | None = None
    is_exec: bool | None = None
    # EOS Accountability Chart — departments this user owns. Used by
    # /api/eos/me/meeting-scopes to expand the L10 scope dropdown.
    managed_departments: list[str] | None = None


class EosTeamsUpdate(BaseModel):
    """Which additional EOS teams to grant this user (Admin → Users).

    ``departments`` REPLACES the stored list; send the full desired set. Values
    outside the user's currently-eligible teams are dropped and reported back
    in ``ignored`` rather than stored, so the row can never hold a grant that
    looks live but isn't.
    """

    departments: list[str] = []


class UserCreate(BaseModel):
    email: str
    name: str
    roles: list[str] = ["user"]
    department: str = "general"
    is_exec: bool = False
    managed_departments: list[str] = []


class RoleCreate(BaseModel):
    name: str
    description: str = ""
    permissions: dict[str, ResourcePermissions] = {}


class RoleUpdate(BaseModel):
    description: str | None = None
    permissions: dict[str, ResourcePermissions] | None = None


class WorkspaceSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_sync_enabled: bool | None = None
    # Workspace admins opt in before users can connect personal mailboxes.
    multi_gmail_enabled: bool | None = None
    # App session length in hours before a forced re-login (15 min – 30 days).
    # None/absent = fall back to settings.SESSION_LIFETIME_HOURS (5h). Only
    # affects sessions minted AFTER the change — already-issued tokens keep
    # the expiry they were signed with.
    session_lifetime_hours: float | None = Field(default=None, ge=0.25, le=720)


class WorkspaceTimeZoneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_zone: str


class MeResponse(UserProfile):
    """``/users/me`` — the profile plus what this user is allowed to do.

    ``permissions`` rides along so the UI can disable a control it knows the
    server will reject, instead of letting the user fill in a form and hit a
    403 on submit. Additive: every field of ``UserProfile`` is unchanged.

    ``features`` is the server's verdict for every gated feature (keyed by
    ``rubik_core.features.registry`` key — ``copilot``, ``customer_xray``,
    ``crm_sync``, the cockpit views, ``smart_teams`` …). The client used to
    mirror each roster as a constant and a pytest kept the two in step; now the
    rail, the route guards and the Help page read this map, so an admin's
    change in Admin → Feature Access reaches the UI on the next profile load
    with no deploy. Roster verdicts, except for the surfaces the client
    cannot probe (the sales-rhythm chips, the voice-input mic, the voice
    commands button, the Loose Ends nav item and My Day panel, and the What's
    New button and drawer), whose env kill switches are folded in below; the others (``COCKPIT_ENABLED`` …) are
    still probed per surface, as before.
    """

    permissions: dict[str, ResourcePermissions] = {}
    features: dict[str, bool] = {}
    modules: dict[str, bool] = {}
    #: Platform automation pause, independent of feature access rosters.
    automations_enabled: bool = True
    #: May read ANOTHER person's raw Smart Teams answers and scores for support
    #: (``rubik_core.smart_teams.authz.is_support_admin``). A per-subject
    #: privacy rule that stays in code, not a feature roster; shipped so the
    #: client reads the server's verdict instead of mirroring the rule.
    #: ``features["smart_teams_support"]`` carries the same verdict.
    smart_teams_support: bool = False
    #: Set while an admin is impersonating a PERSON (read-only): who is acting,
    #: and as whom, so the UI can say so and offer the way back. None otherwise.
    impersonation: dict[str, str] | None = None
    #: A live, audited RubikSoft platform support visit. Distinct from the
    #: Smart Teams support-admin verdict above.
    support: dict[str, str] | None = None
    #: The workspace this person is in: ``company_name`` is the company
    #: profile's name ("" until an admin saves one), for copy such as
    #: "outside {company}" that falls back to "your company" when empty.
    workspace: dict[str, str] = {"slug": "", "name": "", "company_name": ""}


@router.get("/me", response_model=MeResponse)
async def get_me(
    request: Request,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get current user's full profile, effective permissions and feature verdicts."""
    from rubik_core.control.repository import get_workspace, module_flags
    from rubik_core.features import resolve_all  # local: keep the leaf light
    from rubik_core.tenancy.context import current_workspace
    from rubik_core.tenancy.support_context import current_support_session

    slug = current_workspace()
    modules = await run_in_threadpool(module_flags, slug)
    # Compute once from control data without blocking the event loop, then use
    # the same switch snapshot for the feature verdicts and nav module flags.
    # The user is post-impersonation, matching the endpoint gates.
    features = resolve_all(user, db, modules=modules)
    # An exception to "roster verdicts only": the sales-rhythm chips in
    # Ask Cubie have no endpoint of their own for the client to probe, so their
    # kill switch is folded in here — otherwise SALES_RHYTHM_ENABLED=false would
    # leave the chips on screen with every answer saying "switched off".
    if not settings.SALES_RHYTHM_ENABLED:
        features["sales_rhythm"] = False
        features["sales_rhythm_team"] = False
    # Same for the voice-input mic: the client cannot probe its endpoint without
    # a recording, so with VOICE_INPUT_ENABLED=false the button would stay on
    # screen and the person would talk for two minutes into a 404.
    if not settings.VOICE_INPUT_ENABLED:
        features["voice_input"] = False
    # And for voice commands: the Voice button and its keyboard shortcut have
    # nothing to probe until someone speaks.
    if not settings.VOICE_COMMANDS_ENABLED:
        features["voice_commands"] = False
    # And for files in Ask Cubie: the paperclip has nothing to probe until a
    # file is picked, which would then 404.
    if not settings.CHAT_FILE_UPLOAD_ENABLED:
        features["chat_file_upload"] = False
    # And for Loose Ends: the Sales nav item and the My Day panel read these
    # two keys, and the page they lead to answers 404 with LOOP_CLOSURE_ENABLED
    # off — a roster member would otherwise get a nav link to "Not found".
    if not settings.LOOP_CLOSURE_ENABLED:
        features["loop_closure"] = False
        features["loop_closure_team"] = False
    # And for Insights: the rail area and its routes read these two keys, and
    # every /api/insights route answers 404 with INSIGHTS_ENABLED off.
    if not settings.INSIGHTS_ENABLED:
        features["insights"] = False
        features["insights_people"] = False
    # And for Cubie for Chrome: the Connect page and the Settings card read this
    # key, and every /api/browser-extension route answers 404 with
    # CHROME_EXTENSION_ENABLED off.
    if not settings.CHROME_EXTENSION_ENABLED:
        features["chrome_extension"] = False
    impersonation = getattr(request.state, "impersonation", None)
    # And for What's New: the sidebar button, the drawer and the Daily
    # Briefing bar read this key on every page, and /api/whats-new answers 404
    # with WHATS_NEW_ENABLED off. Person impersonation folds it off too: the
    # drawer's read is not on the read-only allowlist and its click count must
    # never be, so the shell simply doesn't show it while looking as someone.
    if not settings.WHATS_NEW_ENABLED or impersonation:
        features["whats_new"] = False
    if impersonation:
        # One row per page load: the app reads /users/me once on boot.
        record_audit(
            db,
            actor_email=impersonation["actor_email"],
            action="IMPERSONATE_USER",
            target=impersonation["target_email"],
            details={"mode": "read_only"},
        )
    from rubik_core.company.profile import (  # local: leaf
        company_name_or_default,
        get_company_profile,
    )
    from rubik_core.smart_teams.authz import is_support_admin  # local: leaf

    company_name = company_name_or_default(get_company_profile(db), "")
    # One verdict for the support view, in both places the client may read it:
    # the raw ``smart_teams_support`` roster alone admits a non-admin a row
    # names, while the real gate also needs the admin role and Smart Teams.
    smart_support = bool(features.get("smart_teams")) and is_support_admin(user)
    features["smart_teams_support"] = smart_support
    support_scope = current_support_session()
    workspace_row = (
        None if support_scope else await run_in_threadpool(get_workspace, slug)
    )
    return MeResponse(
        **user.model_dump(),
        permissions=visible_permissions(user),
        features=features,
        modules=modules,
        automations_enabled=automations_enabled(),
        smart_teams_support=smart_support,
        impersonation=impersonation,
        support=(
            {
                "actor": support_scope.actor,
                "workspace_name": support_scope.workspace_name,
            }
            if support_scope
            else None
        ),
        workspace={
            "slug": slug,
            "name": support_scope.workspace_name
            if support_scope
            else (workspace_row.name if workspace_row else ""),
            "company_name": company_name,
        },
    )


@router.post("/", response_model=UserProfile)
async def create_user(
    user_data: UserCreate,
    background_tasks: BackgroundTasks,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Manually register a new user (Admin only)."""
    existing = user_registry.get_user(user_data.email)
    if existing:
        if getattr(existing, "is_platform_support", False):
            raise HTTPException(
                status_code=409, detail="Platform support identity is reserved"
            )
        raise HTTPException(status_code=400, detail="User already exists")

    reject_support_background_work()
    user = user_registry.register_user(
        email=user_data.email,
        name=user_data.name,
        id="manual",
        roles=user_data.roles,
        department=user_data.department,
    )

    # The Exec flag is a follow-up update — the register_user signature is
    # frozen across many callers; layering it on after creation keeps the
    # signature stable and the column write isolated. Same logic applies
    # to managed_departments which rides on top of is_exec.
    follow_up: dict = {}
    if user_data.is_exec:
        follow_up["is_exec"] = True
    if user_data.managed_departments:
        follow_up["managed_departments"] = user_data.managed_departments
    if follow_up:
        user = user_registry.update_user(user_data.email, follow_up) or user

    record_audit(
        db,
        actor_email=admin.email,
        action="USER_CREATE",
        target=user_data.email,
        details={
            "roles": user_data.roles,
            "department": user_data.department,
            "is_exec": user_data.is_exec,
            "managed_departments": user_data.managed_departments,
        },
    )

    # A mock-admin preview can create local demo users but must never touch
    # real Workspace transcripts through a historical sync.
    if settings.APP_MODE != AppMode.DEV_MOCK_AUTH and automations_enabled():
        # Run the 90-day import on cubie-worker; it outlives a web request.
        submit_background_work(
            background_tasks,
            module="rubik_core.scripts.sync_meet_transcripts",
            args=["--users", user.email, "--days", "90"],
            fallback=partial(run_sync, user_emails=[user.email], days_ago=90),
            label=f"user-create-transcript-sync:{user.email}",
        )

    return user


@router.get("/", response_model=list[UserProfile])
async def list_users(admin: UserProfile = Depends(admin_required)):
    """List all users (Admin only)."""
    return user_registry.list_users()


@router.patch("/{email}", response_model=UserProfile)
async def update_user(
    email: str,
    update: UserUpdate,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Update user name, roles, or department (Admin only)."""
    user = user_registry.get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if getattr(user, "is_platform_support", False):
        raise HTTPException(
            status_code=409, detail="Platform support identity is reserved"
        )

    # Capture the pre-change state of the security-relevant fields so the audit
    # row records old → new (privilege escalation / department moves).
    changes = update.model_dump(exclude_unset=True)
    before = {
        k: getattr(user, k, None)
        for k in changes
        if k in {"roles", "department", "is_exec", "managed_departments"}
    }

    updated_user = user_registry.update_user(email, changes)
    if not updated_user:
        record_audit(
            db,
            actor_email=admin.email,
            action="USER_UPDATE",
            status="failure",
            target=email,
            details={"changes": changes},
        )
        raise HTTPException(status_code=500, detail="Failed to update user")

    record_audit(
        db,
        actor_email=admin.email,
        action="USER_UPDATE",
        target=email,
        details={"before": before, "changes": changes},
    )
    return updated_user


# ---- additional EOS teams (L10-roster opt-in) ------------------------------
#
# Someone on two L10s has only ONE ``users.department``, so without a grant
# here everything they file outside a meeting lands on their home team and the
# standalone Issues / To-Dos / Rocks / Scorecard pages show them only that team
# — while the other team's L10, which they sit in weekly, shows them all of it.
#
# The grant is deliberately NOT automatic from the roster. A roster is a
# snapshot of one calendar invite, so a single guest appearance at another
# team's L10 would otherwise put that team in their picker forever (a
# one-time guest appearance should not show forever). Admin
# grants; the roster keeps the grant honest. See ``_authz.roster_dept_names``.


def _eos_team_state(profile: UserProfile, db: Session) -> dict:
    """Eligible teams for `profile`, and which of them are granted.

    "Additional" is literal: a team the user already reaches through their own
    department, a managed department, or their EOS seat is filtered out. Most
    people sit on their OWN team's L10, so without this the picker would offer
    to grant somebody the department they're already in — a no-op that reads
    like a missing permission.
    """
    from backend.routers.eos._authz import (
        eligible_roster_dept_names,
        person_by_email,
        roster_dept_names,
        seat_for_person,
        user_depts,
    )

    seat = seat_for_person(person_by_email(db, profile.email), db)
    already = {d.lower() for d in user_depts(profile, seat)}
    eligible = sorted(
        (
            d
            for d in eligible_roster_dept_names(profile, db)
            if d.lower() not in already
        ),
        key=lambda d: (d.lower(), d),
    )
    granted = {d.lower() for d in roster_dept_names(profile, db)}
    return {
        "email": profile.email,
        "own_department": profile.department,
        # Teams whose L10 roster currently carries this person. Only these can
        # be granted — the list shrinks by itself when they leave an invite.
        "eligible": [
            {"department": d, "granted": d.lower() in granted} for d in eligible
        ],
    }


@router.get("/{email}/eos-teams", dependencies=[Depends(require_eos_module)])
async def get_user_eos_teams(
    email: str,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """The additional EOS teams this user is eligible for, and which are on."""
    profile = user_registry.get_user(email)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    return _eos_team_state(profile, db)


@router.put("/{email}/eos-teams", dependencies=[Depends(require_eos_module)])
async def set_user_eos_teams(
    email: str,
    update: EosTeamsUpdate,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Replace the user's additional-EOS-team grants (Admin only).

    Filtered to what they're currently eligible for, so the stored list is
    always a set of real grants. This widens what they can READ and FILE — it
    confers no mutation rights and no Exec access (``compute_scope`` unions it
    in after ``led_depts``; Exec meetings never make anyone eligible) — but it
    is still a permission change, so it is audited old → new like a role edit.
    """
    from backend.routers.eos._authz import eligible_roster_dept_names, team_optins
    from rubik_core.database.models import UserPreference

    profile = user_registry.get_user(email)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")

    eligible = eligible_roster_dept_names(profile, db)
    by_slug = {d.lower(): d for d in eligible}
    wanted, ignored = [], []
    seen: set[str] = set()
    for raw in update.departments or []:
        key = (raw or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        # Store the ROSTER's spelling, not the caller's — it came from
        # ``departments.name`` via the meeting FK, so it matches how rows are
        # actually tagged and survives a case-variant department row.
        if key in by_slug:
            wanted.append(by_slug[key])
        else:
            ignored.append(raw)

    before = sorted(team_optins(profile, db))

    row = (
        db.query(UserPreference)
        .filter(UserPreference.user_email == profile.email)
        .first()
    )
    if row is None:
        row = UserPreference(user_email=profile.email)
        db.add(row)
    row.eos_team_optins = wanted
    db.commit()

    record_audit(
        db,
        actor_email=admin.email,
        action="USER_EOS_TEAMS_UPDATE",
        target=profile.email,
        details={"before": before, "after": wanted, "ignored": ignored},
    )
    state = _eos_team_state(profile, db)
    # Names that matched no current roster. Surfaced rather than dropped in
    # silence: "I granted Sales and nothing happened" is unanswerable.
    state["ignored"] = ignored
    return state


@router.delete("/{email}")
async def delete_user(
    email: str,
    force: bool = False,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Delete a user (Admin only) — for junk/test accounts.

    A person with real usage (priorities history, owned docs) should leave via
    ``POST /api/offboarding/offboard`` instead, which records a successor,
    reassigns their docs and sweeps their open work. This guard makes the bare
    delete refuse such users unless ``?force=true`` — without it, history
    would silently lose its owner and owned assets would block the delete on
    the FK anyway.
    """
    from sqlalchemy import func

    from rubik_core.database.models import Asset, HomeCard

    target = (email or "").strip().lower()
    profile = user_registry.get_user(target)
    if profile is not None and getattr(profile, "is_platform_support", False):
        raise HTTPException(
            status_code=409, detail="Platform support identity is reserved"
        )
    if not force:
        cards = (
            db.query(HomeCard).filter(func.lower(HomeCard.user_email) == target).count()
        )
        assets = db.query(Asset).filter(func.lower(Asset.owner_email) == target).count()
        if cards or assets:
            raise HTTPException(
                status_code=409,
                # Shown verbatim in the Admin → Users "delete anyway?" prompt,
                # so it speaks to an admin; API callers pass ?force=true.
                detail=(
                    f"{email} has {cards} priorities card(s) and {assets} owned "
                    "doc(s). If this is a person, offboard them instead (Admin → "
                    "Users → Offboard) so a successor inherits their work."
                ),
            )
    success = user_registry.delete_user(email)
    if success:
        # Their private Ask Cubie files go with the account (see offboarding).
        from starlette.concurrency import run_in_threadpool

        from rubik_core.chat_files.service import purge_owner

        try:
            await run_in_threadpool(purge_owner, db, email)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "user delete: private chat files not purged"
            )
    record_audit(
        db,
        actor_email=admin.email,
        action="USER_DELETE",
        status="success" if success else "failure",
        target=email,
    )
    if success:
        return {"message": "User deleted successfully"}
    else:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/workspace/identity")
async def get_workspace_identity(admin: UserProfile = Depends(admin_required)):
    """Read the platform-owned domains and module switches for this workspace."""
    from rubik_core.control.repository import domains_for_workspace, module_flags
    from rubik_core.tenancy.context import current_workspace

    slug = current_workspace()
    rows = await run_in_threadpool(domains_for_workspace, slug)
    modules = await run_in_threadpool(module_flags, slug)
    return {
        "primary_domain": next((domain for domain, primary in rows if primary), ""),
        "domains": [domain for domain, _ in rows],
        "modules": modules,
    }


@router.get("/workspace/crm-settings")
async def get_workspace_crm_settings(
    admin: UserProfile = Depends(admin_required),
) -> dict[str, Any]:
    """Read this workspace's non-secret HubSpot connector configuration."""
    from rubik_core.tenancy.hubspot_config import (
        HubSpotConfigUnavailable,
        get_hubspot_config,
    )

    try:
        config = await run_in_threadpool(get_hubspot_config)
    except HubSpotConfigUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="HubSpot settings unavailable"
        ) from exc
    try:
        pipeline_ids_editable = await run_in_threadpool(is_platform_admin, admin.email)
    except Exception as exc:  # noqa: BLE001 — never infer platform authority
        raise HTTPException(
            status_code=503, detail="HubSpot settings unavailable"
        ) from exc
    return {**config.to_dict(), "pipeline_ids_editable": pipeline_ids_editable}


@router.patch("/workspace/crm-settings")
async def update_workspace_crm_settings(
    changes: dict[str, Any],
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Patch validated connector values without losing concurrent admin edits."""
    from rubik_core.tenancy.hubspot_config import (
        HubSpotConfigInvalid,
        HubSpotConfigUnavailable,
        patch_hubspot_config,
    )

    if "pipeline_ids" in changes:
        try:
            allowed = await run_in_threadpool(is_platform_admin, admin.email)
        except Exception as exc:  # noqa: BLE001 — a control outage cannot grant scope
            raise HTTPException(
                status_code=503, detail="HubSpot settings unavailable"
            ) from exc
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail="Only platform support can change deal pipeline access",
            )
    try:
        config = await run_in_threadpool(patch_hubspot_config, changes)
    except HubSpotConfigInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HubSpotConfigUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="HubSpot settings unavailable"
        ) from exc
    record_audit(
        db,
        actor_email=admin.email,
        action="HUBSPOT_CONFIG_UPDATE",
        details={"fields": sorted(changes)},
    )
    return config.to_dict()


@router.get("/workspace/settings")
async def get_workspace_settings(admin: UserProfile = Depends(admin_required)):
    """Get Google Workspace integration settings."""
    return {
        "settings": user_registry.get_workspace_settings(),
        "mappings": user_registry.get_group_mappings(),
    }


_PLATFORM_OPERATIONS_FIELDS = frozenset(
    {"impersonation_allowed_emails", "user_impersonation_targets"}
)


@router.get("/workspace/operations")
async def get_workspace_operations(
    admin: UserProfile = Depends(admin_required),
) -> dict[str, Any]:
    """Read the company operations settings available to workspace admins."""
    from rubik_core.company.operations import (
        OperationsConfigUnavailable,
        get_operations_config,
    )

    try:
        config = await run_in_threadpool(get_operations_config)
    except OperationsConfigUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Workspace operations unavailable"
        ) from exc
    return {
        key: value
        for key, value in config.to_dict().items()
        if key not in _PLATFORM_OPERATIONS_FIELDS
    }


@router.patch("/workspace/operations")
async def update_workspace_operations(
    changes: dict[str, Any],
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Patch workspace policy; cross-person grants remain platform-only."""
    from rubik_core.company.operations import (
        OperationsConfigInvalid,
        OperationsConfigUnavailable,
        patch_operations_config,
    )

    if _PLATFORM_OPERATIONS_FIELDS.intersection(changes):
        raise HTTPException(status_code=403, detail="Platform authority required")
    try:
        config = await run_in_threadpool(patch_operations_config, changes)
    except OperationsConfigInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OperationsConfigUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Workspace operations unavailable"
        ) from exc
    record_audit(
        db,
        actor_email=admin.email,
        action="WORKSPACE_OPERATIONS_UPDATE",
        details={"fields": sorted(changes)},
    )
    return {
        key: value
        for key, value in config.to_dict().items()
        if key not in _PLATFORM_OPERATIONS_FIELDS
    }


@router.get("/workspace/time-zone")
async def get_workspace_time_zone(
    admin: UserProfile = Depends(admin_required),
) -> dict[str, str]:
    from rubik_core.tenancy.workspace_time import (
        WorkspaceTimeUnavailable,
        workspace_time_zone_name,
    )

    try:
        name = await run_in_threadpool(workspace_time_zone_name)
    except WorkspaceTimeUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Workspace time zone unavailable"
        ) from exc
    return {"time_zone": name}


@router.patch("/workspace/time-zone")
async def update_workspace_time_zone(
    update: WorkspaceTimeZoneUpdate,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    from rubik_core.tenancy.workspace_time import (
        WorkspaceTimeInvalid,
        WorkspaceTimeUnavailable,
        set_workspace_time_zone,
    )

    try:
        name = await run_in_threadpool(set_workspace_time_zone, update.time_zone)
    except WorkspaceTimeInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorkspaceTimeUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Workspace time zone unavailable"
        ) from exc
    record_audit(
        db,
        actor_email=admin.email,
        action="WORKSPACE_TIME_ZONE_UPDATE",
        details={"time_zone": name},
    )
    return {"time_zone": name}


@router.patch("/workspace/settings")
async def update_workspace_settings(
    settings: WorkspaceSettingsUpdate,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Update Google Workspace integration settings."""
    changes = settings.model_dump(exclude_unset=True)
    user_registry.update_workspace_settings(changes)
    record_audit(
        db,
        actor_email=admin.email,
        action="WORKSPACE_SETTINGS_UPDATE",
        details={"changes": changes},
    )
    return user_registry.get_workspace_settings()


@router.put("/workspace/mappings")
async def update_group_mappings(
    mappings: dict[str, list[str]],
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Update Group-to-Role mappings."""
    user_registry.update_group_mappings(mappings)
    record_audit(
        db,
        actor_email=admin.email,
        action="GROUP_MAPPINGS_UPDATE",
        details={"mappings": mappings},
    )
    return user_registry.get_group_mappings()


@router.get("/workspace/groups", response_model=list[dict[str, Any]])
async def list_workspace_groups(admin: UserProfile = Depends(admin_required)):
    """List all available Google Workspace groups."""
    return user_registry.list_workspace_groups()


@router.get("/roles/list", response_model=list[Role])
async def list_roles(admin: UserProfile = Depends(admin_required)):
    """List all available roles with permissions (Admin only)."""
    return user_registry.get_roles()


@router.get("/roles/names", response_model=list[str])
async def list_role_names(admin: UserProfile = Depends(admin_required)):
    """List role names only (for backward compatibility)."""
    return user_registry.get_role_names()


@router.post("/roles", response_model=list[Role])
async def add_role(
    role_data: RoleCreate,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Add a new role with permissions (Admin only)."""
    role = Role(
        name=role_data.name,
        description=role_data.description,
        permissions=role_data.permissions,
    )
    result = user_registry.add_role(role)
    record_audit(
        db,
        actor_email=admin.email,
        action="ROLE_CREATE",
        target=role_data.name,
        details={
            "description": role_data.description,
            "permissions": {
                k: v.model_dump() for k, v in role_data.permissions.items()
            },
        },
    )
    return result


@router.patch("/roles/{role_name}", response_model=Role)
async def update_role(
    role_name: str,
    updates: RoleUpdate,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Update a role's description or permissions (Admin only)."""
    changes = updates.model_dump(exclude_unset=True)
    updated_role = user_registry.update_role(role_name, changes)
    if not updated_role:
        raise HTTPException(status_code=404, detail="Role not found")
    record_audit(
        db,
        actor_email=admin.email,
        action="ROLE_UPDATE",
        target=role_name,
        details={"changes": changes},
    )
    return updated_role


@router.delete("/roles/{role_name}", response_model=list[Role])
async def delete_role(
    role_name: str,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Delete a role (Admin only)."""
    result = user_registry.delete_role(role_name)
    record_audit(
        db,
        actor_email=admin.email,
        action="ROLE_DELETE",
        target=role_name,
    )
    return result


@router.post("/{email}/sync", response_model=UserProfile)
async def sync_user_workspace(email: str, admin: UserProfile = Depends(admin_required)):
    """Manually sync user roles from Google Workspace (Admin only)."""
    user = user_registry.get_user(email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    synced_user = user_registry.sync_user_workspace_roles(email)
    if not synced_user:
        raise HTTPException(status_code=500, detail="Failed to sync user roles")

    return synced_user
