import logging
import os

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.auth import admin_required
from backend.routers import (
    admin,
    agents,
    ai_providers,
    assets,
    auth_session,
    browser_extension,
    calendar,
    calls,
    chat,
    chat_admin,
    chat_files,
    chat_impersonated,
    cockpit,
    commitments,
    company,
    crm_sync,
    dam,
    deal_defer,
    department_folders,
    departments,
    directory,
    drive,
    email,
    fathom,
    feature_access,
    feedback,
    gmail_accounts,
    groups,
    home,
    impersonation,
    ingest,
    insights,
    jobs,
    labels,
    loose_ends,
    loose_ends_admin,
    meet_addon,
    meeting_skills,
    notifications,
    offboarding,
    platform_support,
    policies,
    preferences,
    priorities_admin,
    promises,
    proposals,
    rag,
    recent,
    sales_admin,
    sales_rhythm,
    saved,
    scheduled_tasks,
    scripts,
    slack,
    slack_commands,
    smart_teams,
    smart_teams_comparison,
    smart_teams_guides,
    smart_teams_skills,
    smart_teams_teams,
    smart_teams_uploads,
    topics,
    tours,
    transcripts,
    users,
    voice,
    voice_commands,
    whats_new,
    whats_new_admin,
    xray,
)
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import AppMode, settings
from rubik_core.logging_config import setup_logging
from rubik_core.telemetry import set_admin_context, setup_telemetry
from rubik_core.tenancy.middleware import WorkspaceMiddleware
from rubik_core.tenancy.platform_secrets import validate_platform_secrets

# Initialize Logging
setup_logging()
logger = logging.getLogger(__name__)

logger.debug("Starting backend.main import")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # This lifespan can be used to initialize other components in the future.
    # Note: DB initialization is handled via Alembic migrations in start.sh.
    #
    # Fail closed on the auth-bypass misconfiguration: mock auth must never be
    # enabled in GCP mode or any Cloud Run service (SOC 2 finding H-2). Refuse
    # to boot even when APP_MODE was accidentally set to local.
    if settings.ENABLE_MOCK_AUTH and (
        settings.APP_MODE == AppMode.GCP or os.environ.get("K_SERVICE")
    ):
        raise RuntimeError(
            "ENABLE_MOCK_AUTH must not be true on Cloud Run or in APP_MODE=gcp. "
            "Aborting startup."
        )
    validate_platform_secrets()
    logger.info("Application starting up...")
    yield
    logger.info("Application shutting down...")


app = FastAPI(title=f"{PRODUCT_NAME} API", version="2.0", lifespan=lifespan)

# Initialize Telemetry (OpenTelemetry + Cloud Trace)
setup_telemetry(app)


@app.middleware("http")
async def context_middleware(request: Request, call_next):
    """Resets the admin context after each request."""
    set_admin_context(False)
    try:
        return await call_next(request)
    finally:
        set_admin_context(False)


@app.middleware("http")
async def catch_exceptions_middleware(request: Request, call_next):
    try:
        response = await call_next(request)
        # Fix Google Login popup block: "COOP policy would block the window.closed call"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        return response
    except Exception:
        logger.exception("Unhandled server error")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error"},
        )


@app.middleware("http")
async def person_impersonation_middleware(request: Request, call_next):
    """Person impersonation is read-only and HubSpot/EOS-only — enforced here.

    Keyed on the HEADER alone, before and regardless of auth: a request that
    carries ``X-Impersonate-User`` is restricted whether or not the caller may
    actually impersonate. Off-allowlist routes and every write are refused, and
    the whole request runs with Google account access switched off. See
    ``rubik_core/auth/impersonation.py``.
    """
    from rubik_core.auth import impersonation as person_imp

    if getattr(request.state, "platform_support", None) is not None:
        # The signed support sid was checked against live control data by the
        # outer workspace middleware. User-supplied impersonation headers have
        # no effect during a support session.
        return await call_next(request)
    if not request.headers.get(person_imp.HEADER):
        return await call_next(request)
    if not person_imp.is_allowed(request.method, request.url.path):
        return JSONResponse(
            status_code=403,
            content={
                "detail": person_imp.BLOCKED_DETAIL,
                "code": "impersonation_blocked",
            },
        )
    token = person_imp.set_active(True)
    try:
        return await call_next(request)
    finally:
        person_imp.reset_active(token)


@app.middleware("http")
async def insights_activity_middleware(request: Request, call_next):
    """Count each person's requests per day and product area (Insights).

    Reads, AFTER the response, who ``get_current_user`` stashed on
    ``request.state`` and which route matched, and hands both to the buffered
    recorder in ``rubik_core/insights/activity.py``, which decides whether it
    counts (no person, impersonation, a non-2xx/3xx answer, polls and plumbing
    do not) and folds it into an in-memory batch. No database work happens
    here. An error in the call itself propagates untouched; an error in the
    recording is swallowed, so this can never fail or slow a request.
    """
    response = await call_next(request)
    try:
        from rubik_core.insights.activity import record_request

        record_request(request, response.status_code)
    except Exception:  # noqa: BLE001 — telemetry never breaks a request
        logger.debug("insights activity recording failed", exc_info=True)
    return response


# Workspace routing sits outside request telemetry and inside CORS, so early
# identity refusals still receive the browser's CORS headers.
app.add_middleware(WorkspaceMiddleware)

# CORS Hardening
# FastAPI/CORSMiddleware already blocks allow_credentials=True with allow_origins=["*"]
# but we add an explicit check for safety.
cors_origins = settings.ALLOWED_ORIGINS
if "*" in cors_origins:
    logger.warning(
        "Wildcard CORS detected with credentials enabled. Filtering wildcard for security."
    )
    cors_origins = [o for o in cors_origins if o != "*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
# ── Conditional Routing based on DEPLOYMENT_ROLE ──
role = settings.DEPLOYMENT_ROLE.lower()

if role in ("all", "api"):
    from fastapi import APIRouter, Depends

    from backend.auth import (
        bind_usage_user,
        require_eos_module,
        require_full_app_access,
    )

    # The Google redirect carries no Cubie bearer token. Register this direct
    # route before FastAPI's lazy included routers so telemetry can resolve
    # invalid-state responses without touching an _IncludedRouter lacking path.
    app.add_api_route(
        "/api/email/accounts/callback",
        gmail_accounts.callback,
        methods=["GET"],
        tags=["Email accounts"],
    )

    # ── The copilot (non-EOS) — behind the `copilot` roster ────────────────
    # The AI copilot (Chat, Daily Briefing, Productivity, Assets, Meeting Prep,
    # Settings, Help) ships to everyone, but an admin can narrow it in Feature
    # Access while EOS stays company-wide, and both share one URL. Mount every
    # copilot router under ONE parent carrying `require_full_app_access`, so
    # whoever is narrowed out gets 403 across all of it and is left with EOS
    # only. Identity/admin routers and EOS stay UNGATED below
    # so EOS-only users keep working. Any NEW copilot router belongs on `core`.
    #
    # `bind_usage_user` comes AFTER the gate (dependencies resolve in order, so
    # a refused caller never gets that far) and stamps the signed-in person on
    # every AI call the endpoint makes, for AI Cost and Insights' AI Spend.
    core = APIRouter(
        prefix="/api",
        dependencies=[Depends(require_full_app_access), Depends(bind_usage_user)],
    )
    core.include_router(chat.router)
    # Files attached in Ask Cubie, private to the person who attached them.
    core.include_router(chat_files.router)
    # Ask Cubie while impersonating a person: HubSpot-only, nothing saved.
    core.include_router(chat_impersonated.router)
    core.include_router(ingest.router)
    core.include_router(dam.router)
    core.include_router(topics.router)
    core.include_router(labels.router)
    core.include_router(assets.router)
    core.include_router(department_folders.router)
    core.include_router(email.router)
    core.include_router(gmail_accounts.router)
    core.include_router(calendar.router)
    core.include_router(drive.router)
    core.include_router(directory.router)
    core.include_router(home.router)
    # UC-45 Customer X-Ray — sales-or-admin gated at the router level (on top of
    # the copilot gate this `core` parent already applies).
    core.include_router(xray.router)
    # UC-46 CRM Sync — sales-or-admin gated plus a feature flag at the router
    # level (on top of the copilot gate this `core` parent already applies).
    core.include_router(crm_sync.router)
    # Call capture (/calls) and the Sales Cockpit (/sales, /commitments,
    # /stage-lag). Sales uses the copilot, so `core` is the right parent; each router
    # adds its own CCS_ENABLED / COCKPIT_ENABLED flag check plus a cockpit role gate
    # (require_cockpit*) on top of the copilot gate. Ingest is POLLED, not pushed, so
    # there is no ungated webhook receiver to mount alongside these.
    core.include_router(calls.router)
    # Call Scripts (/scripts) — the per-product checklists the calendar panel
    # renders and the wrap-up measures coverage against. Same audience and same
    # flag as call capture (`require_ccs` + `CCS_ENABLED`, declared on the
    # router itself); the four MUTATING endpoints add the authoring gate on top,
    # so the supervisor can add a product without a deploy.
    core.include_router(scripts.router)
    # Cubie in Google Meet (/meet-addon) — the same checklist, served to the
    # Meet side-panel add-on for the meeting code Meet hands it. A pure read of
    # the caller's own calendar and call sessions; its own audience
    # (`sales_meet_addon`) and kill switch (MEET_ADDON_ENABLED) ride on top of
    # the copilot gate this parent applies. The panel's page itself is served by
    # serve_frontend below, not by this router.
    core.include_router(meet_addon.router)
    core.include_router(cockpit.router)
    # The sales daily rhythm (/sales/rhythm/{part}) — My Day's Morning /
    # Mid-day / End-of-day section. Same parent and the same `require_cockpit`
    # gate as My Day, plus its own SALES_RHYTHM_ENABLED switch and the
    # `sales_rhythm` roster (backend/routers/sales_rhythm.py).
    core.include_router(sales_rhythm.router)
    # Defer a deal (/sales/deals/...) — set a deal aside until a follow-up
    # date, with a note and a HubSpot task. NOT behind require_cockpit: the
    # Defer button is also on the morning priorities card, which people
    # outside the cockpit rosters see. Its own `deal_defer` roster (403) rides on
    # top of the copilot gate this parent applies — see
    # backend/routers/deal_defer.py.
    core.include_router(deal_defer.router)
    # Loose Ends (/sales/loose-ends) — what the weekday-morning loop-closure
    # scan found per AE, with one real action per row, and the Sales leaders'
    # Team tab. NOT behind require_cockpit (AEs are not on the cockpit roster):
    # its own LOOP_CLOSURE_ENABLED switch (404) and the `loop_closure` /
    # `loop_closure_team` rosters (403) ride on top of the copilot gate this
    # parent applies — see backend/routers/loose_ends.py.
    core.include_router(loose_ends.router)
    core.include_router(commitments.router)
    # Proposal Generator (/proposals) — the client proposal drafted from a
    # captured call's wrap-up. Its own 404/403 dependency (PROPOSALS_ENABLED,
    # then the `proposal_generator` roster) rides on top of the copilot gate
    # `core` already applies — see backend/routers/proposals.py.
    core.include_router(proposals.router)
    # Admin → Company setup: the company profile every prompt reads, the
    # product/service catalog and the proposal templates. Reads ride the
    # copilot gate; each write also requires the admin role.
    core.include_router(company.router)
    # My To-Dos (the promises ledger). Its own 404/403 gate rides on top of
    # the copilot gate `core` already applies — see backend/routers/promises.py.
    core.include_router(promises.router)
    # Insights (/insights) — the BI dashboards. Its own 404/403 gate
    # (INSIGHTS_ENABLED, then the `insights` roster; `insights_people` for the
    # person page) rides on top of the copilot gate `core` already applies — see
    # backend/routers/insights.py.
    core.include_router(insights.router)
    # /fathom/calls and /fathom/ingest. The webhook is NOT here: see the
    # ungated mounts below.
    core.include_router(fathom.router)
    core.include_router(transcripts.router)
    core.include_router(rag.router)
    core.include_router(agents.router)
    core.include_router(preferences.router)
    # Scheduled tasks — the management surface for the recurring runs a
    # user set up in Chat. Creation is a chat tool, so this router is
    # list/edit/pause/run-now/delete; it carries its own feature gate on
    # top of the copilot gate this `core` parent applies.
    core.include_router(scheduled_tasks.router)
    # The "/" menu of Meeting Skills in the chat box (read-only).
    core.include_router(meeting_skills.router)
    core.include_router(slack.router)
    # WGLL feedback — ratings on Cubie's proposals and messages (priorities, chat,
    # meeting prep).
    core.include_router(feedback.router)
    # Saved & Recent
    core.include_router(saved.router)
    core.include_router(recent.router)
    # Notifications
    core.include_router(notifications.router)
    # Voice input (/voice/transcribe) — dictation into the chat box; its own
    # `voice_input` roster rides on top of the copilot gate `core` applies.
    core.include_router(voice.router)
    # Voice commands (/voice/commands) — the Voice button and its shortcut on
    # every page; their own `voice_commands` roster, on top of the copilot gate.
    core.include_router(voice_commands.router)
    from backend.routers import mcp

    core.include_router(mcp.router)
    app.include_router(core)

    # Scheduler-triggered agent endpoints (overnight/run, ingest/eos,
    # priorities/deliver-slack, manager-digest) — UNGATED: they authenticate
    # with an X-Scheduler-Key, not a user token, so they must live outside the
    # copilot gate (which would 401 the token-less Cloud Scheduler calls). Same
    # /api/agents URLs as the gated agents.router; each re-checks the key itself.
    app.include_router(agents.scheduler_router, prefix="/api")
    # Transcript sync has the same scheduler/admin gate and must be reachable
    # without the copilot parent dependency when Cloud Scheduler sends a key.
    app.include_router(ingest.scheduler_router, prefix="/api")

    # Slack slash commands (/todo, /ids → EOS To-Dos & Issues) — UNGATED for the
    # same reason: Slack POSTs to /api/slack/commands + /api/slack/interactivity
    # with NO bearer token (verified by the Slack signing secret instead), so
    # they must sit outside the copilot gate. Distinct paths from the gated
    # slack.router (/api/slack/status, /oauth-start, …) mounted on `core` above.
    app.include_router(slack_commands.router, prefix="/api")
    # Slack OAuth callback — UNGATED for the same reason: slack.com redirects
    # the browser here with no bearer token, so the copilot gate 401'd every
    # "Connect Slack". The handler identifies the user by the HMAC-signed state
    # that the gated /api/slack/oauth-start issued.
    app.include_router(slack.callback_router, prefix="/api")
    # The Google callback is registered as a direct route above.
    # Fathom's webhook (/api/fathom/w/{slug}/webhook) has no bearer token.
    # Middleware selects the active URL workspace before tenant dependencies;
    # this router verifies that workspace's signature. A repeat of an event
    # in the same workspace gets 200 "duplicate" and no work.
    # /fathom/calls and /fathom/ingest stay on `core` above.
    app.include_router(fathom.webhook_router, prefix="/api")
    app.add_exception_handler(
        fathom.DuplicateFathomEvent, fathom.duplicate_fathom_event
    )

    # ── Identity / admin — UNGATED by the copilot gate ─────────────────────
    # Needed by everyone, including EOS-only users (`/users/me` bootstraps auth;
    # `/departments` feeds pickers). Management endpoints keep their own
    # `admin_required` gates, and admins always pass the copilot gate.
    # `/auth/session` is the login-time Google-token → session-JWT exchange —
    # every user hits it before anything else, so it must stay ungated too.
    app.include_router(auth_session.router, prefix="/api")
    # Platform support uses live control authorization and strict audit intent
    # in WorkspaceMiddleware; it must not inherit the copilot roster.
    app.include_router(platform_support.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    # Offboard handoff — ungated by the copilot gate (successors can sit in any
    # department); every endpoint carries its own admin/successor/exec gate.
    app.include_router(offboarding.router, prefix="/api")
    app.include_router(departments.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")
    app.include_router(ai_providers.router, prefix="/api")
    # Admin → Feature Access: the per-feature rosters every gate above resolves
    # through (rubik_core.features). Admin-only per endpoint, like its siblings.
    app.include_router(feature_access.router, prefix="/api")
    app.include_router(priorities_admin.router, prefix="/api")
    app.include_router(chat_admin.router, prefix="/api")
    # Admin → Sales Models: per-role model routing for proposals, call
    # wrap-up, X-Ray and CRM Sync. Same runtime-swap insurance
    # chat and priorities already had; admin-only per endpoint.
    app.include_router(sales_admin.router, prefix="/api")
    # Admin → Loose Ends: the daily Slack post's on/off switch and its channel
    # (#sales, shipped off). Same runtime-edit insurance as Sales Models: no
    # env var, no deploy to turn it on; admin-only per endpoint.
    app.include_router(loose_ends_admin.router, prefix="/api")
    # Admin → What's New: the switch and Slack channel of the weekday morning
    # post of new Cubie release notes (posting shipped off) and the weekly
    # tease. Same runtime-edit insurance as Loose Ends: no env var, no deploy
    # to change them; admin-only per endpoint.
    app.include_router(whats_new_admin.router, prefix="/api")
    # The impersonation menu's people (read-only person impersonation). Not on
    # `core`: it resolves the admin's REAL identity, so it keeps answering while
    # they are impersonating someone outside the copilot roster.
    app.include_router(impersonation.router, prefix="/api")
    app.include_router(groups.router, prefix="/api")
    app.include_router(policies.router, prefix="/api")

    # Page tours ("Walk me") — UNGATED by the copilot gate: the EOS pages
    # carry tours too, and most of the company is EOS-only,
    # so a `core` mount would 403 exactly the people a tour helps most once the
    # roster widens. The router carries its own `require_page_tours` (the
    # `page_tours` roster in Admin → Feature Access) on the APIRouter itself, so
    # no endpoint can be added without it. It only ever reads and writes the
    # caller's own rows. `backend/tests/test_tours_router.py` is the tripwire.
    app.include_router(tours.router, prefix="/api")

    # Cubie for Chrome — UNGATED by the copilot gate, like the tours above.
    # The extension calls with its own per-browser key, not a session, so
    # `core`'s copilot gate would 401 it; and the roster is meant to reach
    # EOS-only teams, whose overdue L10 To-Dos are what it shows them. Every
    # route carries its own gate (`require_chrome_extension` for the web app's
    # connect/disconnect, `require_extension_user` for the extension), both
    # resolving the `chrome_extension` roster. backend/routers/browser_extension.py.
    app.include_router(browser_extension.router, prefix="/api")

    # Smart Teams (ST) — UNGATED by the copilot gate, for the same reason as
    # the tours block above and one of its own: the Work Style Interview is
    # how every employee gets a working guide, and a guide is only worth writing
    # if the whole company has one. The router carries its own
    # `require_smart_teams_access` (declared on the APIRouter itself, so no
    # endpoint can be added without it), which admits any signed-in
    # company Workspace account. Mounting this on `core` would 403 every team an admin
    # narrowed out of the copilot from an interview they are nudged weekly to
    # finish. This mount is the fix; `backend/tests/test_smart_teams_router.py`
    # is the tripwire.
    #
    # Comparison and Team Builder (ST-7/ST-9) are copilot work and will stack
    # `require_full_app_access` on top of this gate on their own router — NOT by
    # moving this one.
    app.include_router(smart_teams.router, prefix="/api")

    # ST-5 official-report upload / parse review / merge. A SEPARATE router, on
    # the same ungated mount and carrying the same `require_smart_teams_access`
    # on its own APIRouter. Separate because this one is an OPTIONAL enrichment
    # that almost nobody uses — only the ET has official DISC results — and
    # keeping it off the interview router is what stops a future change from
    # giving the interview a "you have not uploaded your report" shape.
    app.include_router(smart_teams_uploads.router, prefix="/api")

    # ST-6 the personal working guide: generate, approve (which publishes), read
    # a colleague's published one, unpublish, export. Same ungated mount, same
    # `require_smart_teams_access` declared on its own APIRouter — on `app` and
    # never on `core`, because a guide the whole company can read is only worth
    # writing if the whole company can also WRITE one, and a copilot mount would
    # leave most of the company able to be read about but never to consent.
    #
    # Prefix is "/smart-teams/guides", one segment deeper than the interview and
    # uploads routers, so its `GET /{subject_email}` catch-all cannot shadow
    # anything on "/api/smart-teams" itself. Mounted AFTER the two shallower
    # routers for the same reason — see test_smart_teams_wave1_integration.py,
    # which asserts the whole four-router table has no duplicate path+method.
    app.include_router(smart_teams_guides.router, prefix="/api")

    # ST-8 the skills directory: declare your own, answer a suggestion Cubie
    # made from your internal activity, deny one (which sticks), search the
    # curated vocabulary, and read a colleague's CONFIRMED skills to staff a
    # project. Same ungated mount and same `require_smart_teams_access` on its
    # own APIRouter, for the sibling reason: a skills directory the Team Builder
    # reads is only useful if everybody is in it, and a copilot mount would leave
    # most of the company readable but unable to declare anything about themselves.
    #
    # Prefix is "/smart-teams/skills", one segment deeper than the interview and
    # uploads routers so its "/{skill_key}/confirm" and "/{skill_key}/deny"
    # cannot shadow anything on "/api/smart-teams" itself, and mounted AFTER the
    # shallower routers for the same reason the guides router is.
    app.include_router(smart_teams_skills.router, prefix="/api")

    # ST-7 the PAIR working-relationship guide: generate the one written to me
    # about a colleague, read it, and a support read of the row state.
    # Same ungated mount and same `require_smart_teams_access` on its own
    # APIRouter.
    #
    # NOT `require_full_app_access`, which the Smart Teams block above
    # anticipated for ST-7. That anticipation was wrong for THIS surface and the
    # reason is specific: half of every document here is about the reader, so a
    # copilot mount would tell a colleague outside the copilot roster that a
    # working-relationship guide describing them exists and that they may not
    # open it. Consent is enforced per-document instead, by
    # `authz.can_view_multi_subject_guide` — both people must have published
    # their own guide — which is a stronger gate than the copilot roster and is the
    # one the feature actually needs. Team Builder (ST-9) is a different shape: a
    # staffing tool used ON other people, where the copilot gate is the right call.
    #
    # Prefix is "/smart-teams/comparison", one segment deeper than the interview
    # and uploads routers so its "/pair/{other_email}" catch-all cannot shadow
    # anything on "/api/smart-teams" itself, and mounted AFTER the shallower
    # routers for the same reason the guides router is.
    app.include_router(smart_teams_comparison.router, prefix="/api")

    # ST-9 the Team Builder: ask for a group by the skills and the mix of working
    # styles a piece of work needs. Mounted on `app` like its siblings, but its
    # own APIRouter carries `require_full_app_access` STACKED ON TOP of
    # `require_smart_teams_access` — plan.md Decision #4: "Compare + Team Builder
    # stay behind the existing full-app gate." Stacked and not swapped, so
    # the company boundary (off-domain, impersonation pseudo-user) is still
    # refused first, and NOT moved under `core`, which would take the interview
    # with it. See the router docstring.
    #
    # Prefix is "/smart-teams/teams", a segment deeper than the interview and
    # uploads routers and mounted after them, for the same reason the guides and
    # skills routers are: nothing here may shadow "/api/smart-teams" itself.
    app.include_router(smart_teams_teams.router, prefix="/api")

    # ── What's New ─────────────────────────────────────────────────────────
    # The drawer in the app shell: the Cubie changelog's release notes
    # (rubik_core/whats_new/changelog.py). COMPANY-WIDE, so mounted OUTSIDE `core` (the
    # copilot gate would hide it from anyone an admin narrowed out of the copilot). The
    # router carries its own gate (the WHATS_NEW_ENABLED 404, the Feature Access 403).
    app.include_router(whats_new.router, prefix="/api")

    # ── EOS subdomain — active workspace module required ──────────────────
    # All routes mount under /api/eos; sub-routers keep their own prefixes
    # ("/rocks", "/l10", …). NOTE: seats + people are READ-ONLY —
    # `eos_people` is the identity backbone, synced from `users` on read.
    from backend.routers.eos import (
        dashboard as eos_dashboard,
    )
    from backend.routers.eos import (
        events,
        issues,
        l10,
        organizations,
        processes,
        quarters,
        rocks,
        scorecard,
        seats,
        todos,
        vto,
    )
    from backend.routers.eos import (
        me as eos_me,
    )
    from backend.routers.eos import people as eos_people  # avoid shadowing ``people``

    eos = APIRouter(prefix="/api/eos", dependencies=[Depends(require_eos_module)])
    eos.include_router(organizations.router)
    eos.include_router(seats.router)
    eos.include_router(eos_people.router)
    eos.include_router(vto.router)
    eos.include_router(quarters.router)
    eos.include_router(rocks.router)
    eos.include_router(todos.router)
    eos.include_router(scorecard.router)
    eos.include_router(issues.router)
    eos.include_router(l10.router)
    eos.include_router(processes.router)
    eos.include_router(events.router)
    eos.include_router(eos_me.router)
    eos.include_router(eos_dashboard.router)
    app.include_router(eos)


if role in ("all", "worker"):
    app.include_router(jobs.router, prefix="/api")


@app.get("/health")
async def health_check():
    """Liveness probe + build identity.

    ``git_sha`` / ``git_branch`` are stamped onto the revision at deploy time;
    ``revision`` is Cloud Run's own ``K_REVISION``.
    Cloud Run revisions otherwise carry no git identity, so concurrent
    worktree deploys were indistinguishable — ``curl $URL/health`` now tells you
    exactly which commit a live revision is serving.
    """
    import os

    # Which ADK session backend is actually live. "database:cloudsql" is the
    # healthy prod state; "in_memory:fallback" means the DB backend failed to
    # build and HITL resume is now instance-sticky (see session_service). The
    # lookup is memoized and does not open a DB connection.
    try:
        from rubik_core.orchestration.session_service import get_session_backend

        session_backend = get_session_backend()
    except Exception:  # noqa: BLE001 — /health must never fail on a diagnostic
        session_backend = "error"

    # Alembic revision drift. When the database is stamped at a revision this
    # image doesn't ship, the boot preflight starts the app WITHOUT migrating
    # rather than killing the container. That trade is only safe if the drift is
    # visible, so it's reported here — read from the markers `scripts/start.sh`
    # exported, never a live DB read (uptime checks poll this endpoint, and a
    # database read per probe turns a slow database into an alert storm).
    migrations = {
        "skipped": os.environ.get("CUBIE_MIGRATIONS_SKIPPED", "0") == "1",
        "drift": os.environ.get("CUBIE_MIGRATION_DRIFT", "") or None,
        "db_revision": os.environ.get("CUBIE_MIGRATION_DB_REVISION", "") or None,
        "image_head": os.environ.get("CUBIE_MIGRATION_IMAGE_HEAD", "") or None,
        # Which read produced `db_revision`: "postmigrate" (re-read after `alembic
        # upgrade head` landed — trust it) or "preflight" (read BEFORE migrations, so on
        # a migration-carrying deploy it names the OLD revision). Without this, a
        # container can report a db revision one behind its image head for its whole
        # life, which reads exactly like a failed migration, while its own logs say
        # the upgrade succeeded. `start.sh` refreshes the markers, and this field says
        # whether that refresh actually ran.
        "stage": os.environ.get("CUBIE_MIGRATION_STAGE", "") or None,
    }

    return {
        "status": "ok",
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "git_branch": os.environ.get("GIT_BRANCH", "unknown"),
        "revision": os.environ.get("K_REVISION", "unknown"),
        "session_backend": session_backend,
        # Must be false in production. Surfaced so a post-deploy smoke check can
        # assert the auth-bypass flag is off on the serving revision (H-2).
        "mock_auth": settings.ENABLE_MOCK_AUTH,
        "migrations": migrations,
        # Read once by the release controller after the migration Job, then
        # stamped onto the Service. Uptime checks never query a tenant DB.
        "control_schema_revision": os.environ.get("CUBIE_CONTROL_SCHEMA_REVISION")
        or None,
        "workspace_schema_revision": os.environ.get("CUBIE_WORKSPACE_SCHEMA_REVISION")
        or None,
    }


@app.get("/api/health/models", dependencies=[Depends(admin_required)])
async def models_health_check():
    """Diagnostic: probe each configured Gemini tier (LITE/FLASH/PRO) and report
    whether it still serves. Catches silent Google model-id deprecations,
    ``-latest`` alias rotations, and starved-quota 429s before they reach users.
    Makes three tiny live calls — call on demand, not in a hot loop."""
    from rubik_core.models import check_configured_models

    return check_configured_models()


@app.get("/api/health/rag", dependencies=[Depends(admin_required)])
async def rag_health_check():
    """
    Diagnostic endpoint that reports whether the RAG vector backend
    is correctly configured and reachable, **and** how many chunks
    exist in Cloud SQL.  Useful for deployment validation and
    diagnosing "empty index" problems.
    """
    try:
        from rubik_core.config import AppMode

        if settings.APP_MODE == AppMode.GCP:
            from rubik_core.adapters.vertex_rag import VertexRagAdapter

            adapter = VertexRagAdapter()
        else:
            from rubik_core.adapters.chroma import ChromaAdapter

            adapter = ChromaAdapter()

        # Gather adapter status
        if hasattr(adapter, "get_status"):
            status = adapter.get_status()
        else:
            status = {
                "backend": adapter.__class__.__name__,
                "ready": True,
            }

        # Count chunks in SQL so the user can tell whether the index
        # *should* have data, even if queries return 0.
        sql_chunk_count = None
        try:
            from rubik_core.database.db import get_db
            from rubik_core.database.models import Chunk as DBChunk

            db = next(get_db())
            sql_chunk_count = db.query(DBChunk).count()
            db.close()
        except Exception:
            pass  # non-critical

        ready = status.get("ready", True)
        return {
            "status": "ok" if ready else "degraded",
            "app_mode": settings.APP_MODE.value,
            "vector_backend": status,
            "sql_chunk_count": sql_chunk_count,
            "hint": (
                "If sql_chunk_count > 0 but chat returns 0 results, "
                "call POST /api/admin/reindex to push vectors into the live index."
                if sql_chunk_count and sql_chunk_count > 0
                else None
            ),
        }
    except Exception as e:
        logger.error(f"RAG health-check failed: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "app_mode": settings.APP_MODE.value,
                "detail": str(e),
            },
        )


# Serve Frontend Static Files
# Mount the frontend/dist directory to root
# This must be the last route to avoid overriding API routes

from fastapi.responses import FileResponse


def _dist_file(root: str, full_path: str) -> str | None:
    """The file inside ``root`` (already resolved) that ``full_path`` names.

    ``full_path`` arrives percent-decoded, so ``%2e%2e`` and ``..%2f`` are plain
    ``..`` by now, and a doubled leading slash (``//etc/passwd``) makes it
    absolute, which ``os.path.join`` honours by dropping ``root`` altogether.
    So resolve it, dot segments and symlinks alike, and accept only what is
    still inside ``root``: ``commonpath``, not ``startswith``, so a sibling such
    as ``dist-evil/`` does not pass for ``dist/``. ``None`` means "not a file we
    serve", and the caller treats it like any other unknown route.
    """
    try:
        candidate = os.path.realpath(os.path.join(root, full_path))
    except ValueError:  # an embedded NUL byte, which isfile() used to swallow
        return None
    if os.path.commonpath([root, candidate]) != root:
        return None
    return candidate if os.path.isfile(candidate) else None


def mount_frontend(app: FastAPI, dist: str) -> None:
    """Serve the built SPA in ``dist`` on every path the API routes leave."""
    root = os.path.realpath(dist)

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        file_path = _dist_file(root, full_path)
        if file_path is not None:
            return FileResponse(file_path)

        # The Meet add-on's side panel is its OWN page (a second Vite entry,
        # frontend/src/meetAddon/), not a route in the Cubie SPA: it runs in an
        # iframe inside Google Meet and must not boot the whole app. Meet appends
        # its own query parameters (meet_sdk, …) to this URL; they reach the page
        # untouched because nothing here reads or rewrites the query string.
        if full_path == "meet-addon" or full_path.startswith("meet-addon/"):
            addon_file = os.path.join(root, "meet-addon.html")
            if os.path.exists(addon_file):
                return FileResponse(addon_file)

        # Fallback to index.html for SPA routing
        index_file = os.path.join(root, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return JSONResponse(status_code=404, content={"detail": "Not Found"})


frontend_dist = "/app/frontend/dist"
if os.path.exists(frontend_dist):
    mount_frontend(app, frontend_dist)
else:
    logger.warning(
        f"Frontend dist directory not found at {frontend_dist}. Frontend will not be served."
    )

logger.debug("backend.main import COMPLETE. Ready for deployment check.")
