"""Slack slash commands — capture EOS To-Dos and Issues (IDS) from Slack.

Two commands, each opening a Block Kit modal:
  ``/todo`` → create an EOS **To-Do** (owner + team + due date)
  ``/ids``  → raise an EOS **Issue** for IDS (title + team + priority + owner)

Slack's two-step modal flow:
  1. Slack POSTs the slash command to ``POST /api/slack/commands``. We verify the
     request signature, resolve the Slack user → Cubie user, and open a modal via
     ``views.open`` (using the command's short-lived ``trigger_id``).
  2. On submit Slack POSTs the interaction to ``POST /api/slack/interactivity``.
     We verify the signature, read the modal's field values, create the
     Todo/Issue as the resolved user, and return a success view.

These endpoints are UNAUTHENTICATED at the Cubie layer — Slack sends no bearer
token — so they are verified by the Slack **signing secret** and mounted on the
UNGATED router (never behind ``require_full_app_access``; see backend/main.py +
backend/auth.py, same rule as the Cloud-Scheduler endpoints). Identity is derived
from the Slack ``user_id`` and a personal OAuth credential whose Google-verified
hosted domain still belongs to this workspace. An unlinked Slack account must
connect in Cubie before it can act.

The blocking database and Slack Web API work runs in a threadpool, with a short
single-attempt ``views.open`` call for Slack's 3-second trigger deadline. A
submission marker and the new item commit together so Slack retries are safe.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.jobs.eos_ingest_hooks import enqueue_issue_ingest
from backend.jobs.launcher import reject_support_background_work
from backend.models.eos.issues import derive_issue_title
from backend.models.eos.traction import truncate_todo_text
from backend.routers.eos._authz import (
    require_exec_scope_allowed,
    roster_dept_names,
)
from backend.services.slack_service import (
    SlackAPIError,
    SlackBotClient,
    credential_domain_active,
)
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.control.repository import (
    module_enabled,
    slack_route_for,
)
from rubik_core.database.db import SessionLocal
from rubik_core.database.eos import (
    Issue,
    IssuePriority,
    IssueStatus,
    Person,
    Todo,
    TodoStatus,
)
from rubik_core.database.models import SlackCredential, SlackViewSubmission
from rubik_core.database.repository import SQLUserRegistry
from rubik_core.database.schemas import UserProfile
from rubik_core.rag.config import slugify_department
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.secrets import read_secret_ref, ws_secret_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack-commands"])

# Slack rejects a request signature whose timestamp is older than 5 minutes —
# our replay-protection window too.
_SIGNATURE_MAX_SKEW_SECONDS = 60 * 5
_MAX_INBOUND_BODY_BYTES = 128 * 1024
_SLACK_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")

# Modal callback ids + option-value encodings (kept as constants so the build
# and submit sides can't drift).
_TODO_CALLBACK = "eos_todo_create"
_ISSUE_CALLBACK = "eos_issue_create"
_EXEC_SCOPE_VALUE = "exec"
_DEPT_SCOPE_PREFIX = "dept:"
_NO_TEAM_VALUE = "team:none"  # company-wide (department_name=None, is_exec=False)
# User-facing name of the Exec scope. Must match EXEC_SCOPE_LABEL in
# backend/routers/eos/me.py so Slack and the app never show two names for
# the same scope.
_EXEC_SCOPE_LABEL = "Executive View"
_PERSON_VALUE_PREFIX = "person:"
_UNASSIGNED = "none"
# /todo "Where" choice. "My list" is the personal promises ledger (My To-Dos);
# "Team L10" is the EOS To-Do the command has always created. Default is
# My list (a product decision) for anyone who has the feature; without it
# the block is absent and the modal behaves exactly as before.
_WHERE_MINE = "mine"
_WHERE_TEAM = "team"


# ─── Request-signature verification ──────────────────────────────────────────


def _verify_signature(raw_body: bytes, headers, *, secret: str | None) -> bool:
    """Verify Slack's ``v0`` request signature over the RAW body.

    ``signature = "v0=" + HMAC_SHA256(signing_secret, f"v0:{ts}:{body}")``.
    Rejects a missing signing secret, a stale/blank timestamp (replay), or a
    mismatch, using a constant-time compare. The body MUST be the untouched
    request bytes — parse the form only after this returns True.
    See https://api.slack.com/authentication/verifying-requests-from-slack.
    """
    secret = (secret or "").strip()
    if not secret:
        logger.error(
            "Workspace Slack signing secret is unavailable — rejecting request."
        )
        return False
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not (timestamp and signature):
        return False
    try:
        if abs(time.time() - int(timestamp)) > _SIGNATURE_MAX_SKEW_SECONDS:
            return False
    except ValueError:
        return False
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_form(raw_body: bytes) -> dict[str, str]:
    """Flatten an ``application/x-www-form-urlencoded`` body to a dict.

    Slack sends both slash commands and interaction payloads this way (the
    interaction JSON arrives in a single ``payload`` field)."""
    parsed = parse_qs(raw_body.decode("utf-8"), max_num_fields=64)
    if any(len(values) != 1 for values in parsed.values()):
        raise ValueError("Duplicate Slack form field")
    return {k: v[0] for k, v in parsed.items() if v}


async def _verified_inbound(
    request: Request, *, interaction: bool
) -> tuple[str, str, dict[str, str], dict[str, Any] | None] | Response:
    """Resolve one signed installation before any tenant database is opened."""
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_INBOUND_BODY_BYTES:
            return Response(status_code=413, content="Slack payload too large")
        raw.extend(chunk)
    body = bytes(raw)
    try:
        form = _parse_form(body)
        payload = json.loads(form.get("payload") or "{}") if interaction else None
        if interaction:
            if not isinstance(payload, dict) or not isinstance(
                payload.get("team"), dict
            ):
                raise ValueError("Malformed interaction payload")
            app_id = payload.get("api_app_id")
            team_id = payload["team"].get("id")
        else:
            app_id = form.get("api_app_id")
            team_id = form.get("team_id")
        if not isinstance(app_id, str) or not _SLACK_ID.fullmatch(app_id):
            raise ValueError("Invalid Slack app ID")
        if not isinstance(team_id, str) or not _SLACK_ID.fullmatch(team_id):
            raise ValueError("Invalid Slack team ID")
    except (UnicodeDecodeError, ValueError, TypeError):
        return Response(status_code=400, content="bad Slack payload")

    try:
        route = await run_in_threadpool(slack_route_for, app_id, team_id)
    except Exception:
        logger.error("Slack installation lookup failed")
        return Response(status_code=503, content="Slack routing unavailable")
    if route is None:
        return Response(status_code=401, content="invalid signature")

    slug = route.workspace_slug
    with use_workspace(slug):
        try:
            # The installation row cannot redirect reads to another bound
            # connector field, even within the same workspace.
            if route.signing_secret_ref != ws_secret_id("slack", "signing_secret"):
                return Response(status_code=401, content="invalid signature")
            secret = await run_in_threadpool(read_secret_ref, route.signing_secret_ref)
        except Exception:
            logger.error("Workspace Slack signing secret is unavailable")
            return Response(status_code=503, content="Slack signing unavailable")
        if not secret or not _verify_signature(body, request.headers, secret=secret):
            return Response(status_code=401, content="invalid signature")
        try:
            if not await run_in_threadpool(module_enabled, "eos", slug):
                return Response(status_code=404, content="EOS is not enabled")
        except Exception:
            logger.error("Workspace EOS switch lookup failed for Slack")
            return Response(status_code=503, content="Workspace access unavailable")

    request.state.workspace = slug
    return slug, team_id, form, payload


# ─── Identity ────────────────────────────────────────────────────────────────


def _resolve_user(
    db: Session, slack_user_id: str, slack_team_id: str
) -> UserProfile | None:
    """Slack user id → Cubie ``UserProfile``.

    A linked credential proves the Slack member ID and the Google-verified
    hosted domain. Bot ``users.info`` email alone cannot prove membership,
    especially when Cubie uses alias email addresses."""
    cred = (
        db.query(SlackCredential)
        .filter(
            SlackCredential.slack_user_id == slack_user_id,
            SlackCredential.slack_team_id == slack_team_id,
        )
        .first()
    )
    if cred is None or not credential_domain_active(cred):
        return None
    email = cred.user_email
    # The verified installation already selected this tenant DB. Google
    # hosted-domain members can have an alias email suffix, so membership is
    # the current tenant user row, not the email's domain text.
    user = SQLUserRegistry(db).get_user(email)
    if user is None or getattr(user, "is_platform_support", False):
        return None
    return user


# ─── Option builders (team / people) ─────────────────────────────────────────


def _option(label: str, value: str) -> dict[str, Any]:
    return {
        "text": {"type": "plain_text", "text": (label or value)[:75]},
        "value": value,
    }


def _team_options(user: UserProfile, db: Session) -> list[dict[str, Any]]:
    """Block Kit options for the team an item is filed under.

    Mirrors ``/api/eos/me/meeting-scopes`` (backend/routers/eos/me.py): admins →
    Exec + every department; execs → Exec + own + managed (all depts on the
    org-wide ``*`` wildcard); team managers → own + managed (no Exec); everyone
    else → their own department. Everyone additionally gets the teams whose L10
    they're on the calendar roster of (``_authz.roster_dept_names``), so
    somebody who sits in two L10s can actually choose between them instead of
    silently filing everything to their home team. Always non-empty — falls
    back to a company-wide option so ``views.open`` never fails on an empty
    select."""
    from rubik_core.database.models import Department

    opts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(label: str, value: str) -> None:
        if value in seen or not label:
            return
        seen.add(value)
        opts.append(_option(label, value))

    def all_departments() -> None:
        for (name,) in db.query(Department.name).order_by(Department.name.asc()).all():
            if name:
                push(name, f"{_DEPT_SCOPE_PREFIX}{name}")

    is_admin = user.is_admin
    is_exec = bool(getattr(user, "is_exec", False))
    own = (getattr(user, "department", None) or "").strip()
    managed = list(getattr(user, "managed_departments", None) or [])

    if is_admin:
        push(_EXEC_SCOPE_LABEL, _EXEC_SCOPE_VALUE)
        all_departments()
    else:
        if is_exec:
            push(_EXEC_SCOPE_LABEL, _EXEC_SCOPE_VALUE)
        if own and own.lower() != "general":
            push(own, f"{_DEPT_SCOPE_PREFIX}{own}")
        if is_exec and "*" in managed:
            all_departments()
        else:
            for dept in managed:
                d = (dept or "").strip()
                if d and d != "*" and d.lower() != "general":
                    push(d, f"{_DEPT_SCOPE_PREFIX}{d}")

    # Teams the caller sits on the L10 roster of without belonging to them on
    # paper — the Slack half of the same fix as ``me._team_scopes``. Without
    # it, someone on two L10s gets a single-option select here and every
    # ``/todo`` / ``/ids`` they file lands silently on their home team. Pushed
    # last so a team they also belong to keeps its original label spelling.
    for dept in sorted(roster_dept_names(user, db), key=lambda x: x.lower()):
        push(dept, f"{_DEPT_SCOPE_PREFIX}{dept}")

    if not opts:
        push("Company-wide (no team)", _NO_TEAM_VALUE)
    return opts


def _default_team_value(
    user: UserProfile, team_opts: list[dict[str, Any]]
) -> str | None:
    """Pre-select the user's own department when it's an option, else the first."""
    own = (getattr(user, "department", None) or "").strip()
    if own:
        want = f"{_DEPT_SCOPE_PREFIX}{own}"
        if any(o["value"] == want for o in team_opts):
            return want
    return team_opts[0]["value"] if team_opts else None


def _people_options(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    """Owner-picker options: every real EOS person, value ``person:<uuid>``.

    Lazily syncs ``eos_people`` from the users table first (same as the EOS
    People picker) so newly-added users appear without a manual step. Then:

    - **filters to real users** via ``_is_real_user`` — sync only retires
      pseudo-user rows that still exist in ``users``, so impersonation/`.local`
      and test-created ``eos_people`` rows otherwise leak into the picker;
    - **de-dupes by email** (duplicate Person rows for the same person);
    - **caps at ``limit``** — Slack rejects a ``static_select`` with more than
      100 options. The Issue modal prepends an "(unassigned)" entry, so it passes
      ``limit=99`` to stay within 100 (this was the ``/ids`` ``invalid_arguments``
      failure: unassigned + 100 people = 101).
    """
    from backend.routers.eos.people import _is_real_user, _sync_people_from_users

    try:
        _sync_people_from_users(db)
    except Exception:
        logger.exception(
            "People sync failed for Slack owner picker; using existing rows"
        )
    people = (
        db.query(Person)
        .filter(Person.is_deleted == False)  # noqa: E712
        .order_by(Person.first_name.asc(), Person.last_name.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in people:
        email = (p.email or "").lower()
        if not _is_real_user(p.email) or email in seen:
            continue
        seen.add(email)
        name = f"{p.first_name} {p.last_name}".strip() or p.email
        out.append(_option(name, f"{_PERSON_VALUE_PREFIX}{p.id}"))
        if len(out) >= limit:
            break
    return out


def _default_person_value(db: Session, email: str) -> str | None:
    p = (
        db.query(Person)
        .filter(Person.email == email, Person.is_deleted == False)  # noqa: E712
        .first()
    )
    return f"{_PERSON_VALUE_PREFIX}{p.id}" if p else None


# ─── Block Kit builders ──────────────────────────────────────────────────────


def _select_block(
    block_id: str,
    label: str,
    options: list[dict[str, Any]],
    initial_value: str | None,
    *,
    optional: bool = False,
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "static_select",
        "action_id": "value",
        "placeholder": {"type": "plain_text", "text": f"Select {label.lower()}"},
        "options": options,
    }
    initial = next((o for o in options if o["value"] == initial_value), None)
    if initial:
        element["initial_option"] = initial
    block: dict[str, Any] = {
        "type": "input",
        "block_id": block_id,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }
    if optional:
        block["optional"] = True
    return block


def _text_block(
    block_id: str,
    label: str,
    *,
    multiline: bool = False,
    optional: bool = False,
    placeholder: str = "",
    initial_value: str = "",
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "value",
        "multiline": multiline,
    }
    if placeholder:
        element["placeholder"] = {"type": "plain_text", "text": placeholder}
    if initial_value:
        element["initial_value"] = initial_value[:2900]
    block: dict[str, Any] = {
        "type": "input",
        "block_id": block_id,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }
    if optional:
        block["optional"] = True
    return block


def _todos_enabled_for(user: UserProfile) -> bool:
    """May this person file into My To-Dos? Kill switch + roster, fail closed."""
    if not getattr(settings, "TODOS_ENABLED", False):
        return False
    try:
        from rubik_core.features import feature_allows

        return bool(feature_allows("todos", user))
    except Exception:  # noqa: BLE001 — an unresolvable gate hides the option
        logger.warning(
            "slack /todo: todos gate failed for %s", user.email, exc_info=True
        )
        return False


def _build_todo_view(
    user: UserProfile, db: Session, *, prefill: str, private_metadata: str
) -> dict[str, Any]:
    team_opts = _team_options(user, db)
    people_opts = _people_options(db)
    where_blocks: list[dict[str, Any]] = []
    if _todos_enabled_for(user):
        where_blocks.append(
            _select_block(
                "where",
                "Where",
                [
                    _option("My list (My To-Dos)", _WHERE_MINE),
                    _option("Team L10 (EOS To-Do)", _WHERE_TEAM),
                ],
                _WHERE_MINE,
            )
        )
    return {
        "type": "modal",
        "callback_id": _TODO_CALLBACK,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "New To-Do"},
        "submit": {"type": "plain_text", "text": "Add To-Do"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            _text_block(
                "desc",
                "What needs to get done?",
                multiline=True,
                placeholder="e.g. Send the renewal paperwork to the vendor",
                initial_value=prefill,
            ),
            *where_blocks,
            _select_block(
                "team", "Team", team_opts, _default_team_value(user, team_opts)
            ),
            _select_block(
                "owner", "Owner", people_opts, _default_person_value(db, user.email)
            ),
            {
                "type": "input",
                "block_id": "due",
                "optional": True,
                "label": {"type": "plain_text", "text": "Due date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "value",
                    "placeholder": {"type": "plain_text", "text": "Select a date"},
                },
            },
        ],
    }


def _build_issue_view(
    user: UserProfile, db: Session, *, prefill: str, private_metadata: str
) -> dict[str, Any]:
    team_opts = _team_options(user, db)
    priority_opts = [
        _option("Low", "low"),
        _option("Medium", "medium"),
        _option("High", "high"),
    ]
    # limit=99 leaves room for the "(unassigned)" entry — Slack caps a
    # static_select at 100 options (1 + 99 = 100).
    owner_opts = [_option("(unassigned)", _UNASSIGNED), *_people_options(db, limit=99)]
    return {
        "type": "modal",
        "callback_id": _ISSUE_CALLBACK,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Raise an Issue (IDS)"},
        "submit": {"type": "plain_text", "text": "Add to IDS"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            _text_block(
                "title",
                "Short headline",
                optional=True,
                placeholder="e.g. Deploy window keeps getting missed",
                initial_value=derive_issue_title(prefill) if prefill else "",
            ),
            _text_block(
                "desc",
                "Describe the issue",
                multiline=True,
                placeholder="What's the problem to Identify, Discuss, and Solve?",
                initial_value=prefill,
            ),
            _select_block(
                "team", "Team", team_opts, _default_team_value(user, team_opts)
            ),
            _select_block("priority", "Priority", priority_opts, "medium"),
            _select_block("owner", "Owner", owner_opts, _UNASSIGNED, optional=True),
        ],
    }


# ─── Modal-state readers ─────────────────────────────────────────────────────


def _input_value(state: dict[str, Any], block_id: str) -> str:
    return (((state.get(block_id) or {}).get("value") or {}).get("value") or "").strip()


def _select_value(state: dict[str, Any], block_id: str) -> str | None:
    sel = ((state.get(block_id) or {}).get("value") or {}).get("selected_option")
    return sel.get("value") if sel else None


def _date_value(state: dict[str, Any], block_id: str) -> str | None:
    return ((state.get(block_id) or {}).get("value") or {}).get("selected_date")


def _parse_team(value: str | None) -> tuple[str | None, bool]:
    """Team option value → (department_name, is_exec)."""
    if value == _EXEC_SCOPE_VALUE:
        return None, True
    if value and value.startswith(_DEPT_SCOPE_PREFIX):
        return (value[len(_DEPT_SCOPE_PREFIX) :] or None), False
    return None, False  # _NO_TEAM_VALUE / anything else → company-wide


def _authorized_team_value(
    user: UserProfile, db: Session, submitted: str | None
) -> str | None:
    """Resolve a submitted option against the caller's current filing scopes.

    Slack can deliver a modal after its roster or admin grant changed. Return
    the current option spelling for a department whose RAG slug still matches;
    never let an unknown value fall through to the company-wide parse default.
    """
    if not isinstance(submitted, str):
        return None
    options = [option["value"] for option in _team_options(user, db)]
    if submitted in options:
        return submitted
    if not submitted.startswith(_DEPT_SCOPE_PREFIX):
        return None
    wanted = slugify_department(submitted[len(_DEPT_SCOPE_PREFIX) :])
    if not wanted:
        return None
    for option in options:
        if (
            option.startswith(_DEPT_SCOPE_PREFIX)
            and slugify_department(option[len(_DEPT_SCOPE_PREFIX) :]) == wanted
        ):
            return option
    return None


def _parse_person(db: Session, value: str | None) -> Person | None:
    if not value or value == _UNASSIGNED or not value.startswith(_PERSON_VALUE_PREFIX):
        return None
    try:
        pid = UUID(value[len(_PERSON_VALUE_PREFIX) :])
    except ValueError:
        return None
    return (
        db.query(Person)
        .filter(Person.id == pid, Person.is_deleted == False)  # noqa: E712
        .first()
    )


def _team_label(department_name: str | None, is_exec: bool) -> str:
    if is_exec:
        return _EXEC_SCOPE_LABEL
    return department_name or "Company-wide"


def _person_label(person: Person) -> str:
    return f"{person.first_name} {person.last_name}".strip() or person.email


# ─── Slack responses ─────────────────────────────────────────────────────────


def _ephemeral(text: str) -> JSONResponse:
    """A message only the invoking user sees, in the channel they typed in."""
    return JSONResponse({"response_type": "ephemeral", "text": text})


def _submission_error(block_id: str, message: str) -> JSONResponse:
    """Keep the modal open and show a validation error under one field."""
    return JSONResponse({"response_action": "errors", "errors": {block_id: message}})


def _success_view(title: str, headline: str, lines: list[str]) -> JSONResponse:
    """Replace the modal with a compact confirmation the user dismisses."""
    body = "\n".join(lines)
    text = f"*{headline}*\n{body}" if headline else body
    view = {
        "type": "modal",
        "callback_id": "eos_capture_done",
        "title": {"type": "plain_text", "text": title[:24]},
        "close": {"type": "plain_text", "text": "Done"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"View and edit this in {PRODUCT_NAME} → EOS.",
                    }
                ],
            },
        ],
    }
    return JSONResponse({"response_action": "update", "view": view})


# ─── Create paths (mirror backend/routers/eos/{todos,issues}.py) ─────────────


def _commit_submission(db: Session, view_id: str) -> bool:
    """Commit the view marker and its business row in one transaction.

    A competing delivery of the same Slack view blocks on the unique key. If
    the first delivery committed, the second rolls back its business row and
    acknowledges the retry without creating another item.
    """
    db.add(SlackViewSubmission(view_id=view_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.get(SlackViewSubmission, view_id) is not None:
            return False
        raise
    return True


def _create_promise_from_modal(
    db: Session, user: UserProfile, state: dict[str, Any], desc: str, view_id: str
):
    """``Where = My list`` → one row in the caller's own My To-Dos ledger.

    The owner picker stays on the modal (Slack cannot hide a block on a radio
    change without a round trip), so a different owner is refused with a
    pointer at the Team L10 option rather than silently re-homed.
    """
    if not _todos_enabled_for(user):
        return _submission_error(
            "where", "My To-Dos is not switched on for you — pick Team L10."
        )
    owner = _parse_person(db, _select_value(state, "owner"))
    if (
        owner is not None
        and (owner.email or "").strip().lower() != user.email.strip().lower()
    ):
        return _submission_error(
            "owner",
            "Your own list is yours alone — pick Team L10 to give a to-do to someone else.",
        )
    due_local = None
    due_raw = _date_value(state, "due")
    if due_raw:
        try:
            due_local = date.fromisoformat(due_raw)
        except ValueError:
            due_local = None

    from rubik_core.promises import store

    row = store.create_promise(
        db,
        owner_email=user.email,
        title=desc,
        due_local=due_local,
        source="slack_cmd",
        source_ref={"channel": "slack", "command": "/todo"},
        actor=user.email,
    )
    if not _commit_submission(db, view_id):
        return Response(status_code=200)
    logger.info("Slack /todo created promise for %s", user.email)
    lines = ["*Where:* My To-Dos"]
    if due_local:
        lines.append(f"*Due:* {due_local:%b %d, %Y}")
    lines.append(f"Open it in {PRODUCT_NAME} under My Work → My To-Dos.")
    return _success_view("Added to your To-Dos ✅", row.title, lines)


def _create_todo_from_modal(
    db: Session, user: UserProfile, state: dict[str, Any], view_id: str
):
    desc = _input_value(state, "desc")
    if not desc:
        return _submission_error("desc", "Please describe the to-do.")
    if _select_value(state, "where") == _WHERE_MINE:
        return _create_promise_from_modal(db, user, state, desc, view_id)
    team_value = _authorized_team_value(user, db, _select_value(state, "team"))
    if team_value is None:
        return _submission_error("team", "Your team access changed. Reopen the form.")
    dept, is_exec = _parse_team(team_value)
    try:
        require_exec_scope_allowed(user, True if is_exec else None)
    except HTTPException:
        return _submission_error(
            "team", "Only Exec-team members can file to the Exec team."
        )
    owner = _parse_person(db, _select_value(state, "owner"))
    if owner is None:
        return _submission_error("owner", "Please choose an owner.")
    due_date: datetime | None = None
    due_raw = _date_value(state, "due")
    if due_raw:
        try:
            due_date = datetime.strptime(due_raw, "%Y-%m-%d")
        except ValueError:
            due_date = None

    todo = Todo(
        owner_id=owner.id,
        # Slack's multiline input has no length limit; a to-do row does.
        description=truncate_todo_text(desc),
        department_name=dept,
        is_exec=is_exec,
        due_date=due_date,
        status=TodoStatus.PENDING,
        created_by=user.email,
    )
    db.add(todo)
    if not _commit_submission(db, view_id):
        return Response(status_code=200)
    logger.info("Slack /todo created todo for %s (owner=%s)", user.email, owner.email)

    lines = [
        f"*Owner:* {_person_label(owner)}",
        f"*Team:* {_team_label(dept, is_exec)}",
    ]
    if due_date:
        lines.append(f"*Due:* {due_date:%b %d, %Y}")
    return _success_view("To-Do added ✅", desc, lines)


def _create_issue_from_modal(
    db: Session,
    user: UserProfile,
    state: dict[str, Any],
    view_id: str,
    background_tasks: BackgroundTasks,
):
    desc = _input_value(state, "desc")
    if not desc:
        return _submission_error("desc", "Please describe the issue.")
    team_value = _authorized_team_value(user, db, _select_value(state, "team"))
    if team_value is None:
        return _submission_error("team", "Your team access changed. Reopen the form.")
    dept, is_exec = _parse_team(team_value)
    try:
        require_exec_scope_allowed(user, True if is_exec else None)
    except HTTPException:
        return _submission_error(
            "team", "Only Exec-team members can file to the Exec team."
        )
    owner = _parse_person(db, _select_value(state, "owner"))
    try:
        priority = IssuePriority(_select_value(state, "priority") or "medium")
    except ValueError:
        priority = IssuePriority.MEDIUM
    title = _input_value(state, "title") or derive_issue_title(desc)

    issue = Issue(
        title=title,
        description=desc,
        department_name=dept,
        is_exec=is_exec,
        priority=priority,
        status=IssueStatus.OPEN,
        owner_id=owner.id if owner else None,
        created_by=user.email,
    )
    db.add(issue)
    if not _commit_submission(db, view_id):
        return Response(status_code=200)
    logger.info(
        "Slack /ids created issue for %s (team=%s)",
        user.email,
        _team_label(dept, is_exec),
    )

    # Best-effort RAG re-index, exactly like the REST issue-create path.
    try:
        enqueue_issue_ingest(background_tasks)
    except Exception:
        logger.exception("enqueue_issue_ingest failed for Slack-created issue")

    lines = [
        f"*Team:* {_team_label(dept, is_exec)}",
        f"*Priority:* {priority.value.title()}",
    ]
    if owner:
        lines.append(f"*Owner:* {_person_label(owner)}")
    return _success_view("Issue added to IDS ✅", title, lines)


# ─── Endpoints ───────────────────────────────────────────────────────────────


def _handle_command(slug: str, team_id: str, form: dict[str, str]) -> Response:
    """Run blocking user lookup, modal construction, and Slack API off-loop."""
    command = (form.get("command") or "").strip().lower()
    text = (form.get("text") or "").strip()
    slack_user_id = form.get("user_id") or ""
    trigger_id = form.get("trigger_id") or ""

    with use_workspace(slug):
        db = SessionLocal()
        try:
            user = _resolve_user(db, slack_user_id, team_id)
            if user is None:
                return _ephemeral(
                    f"Connect Slack under {PRODUCT_NAME} → Settings → Slack, "
                    "then try the command again."
                )
            metadata = json.dumps({"email": user.email, "workspace": slug})
            if command == "/ids":
                view = _build_issue_view(
                    user, db, prefill=text, private_metadata=metadata
                )
            else:  # "/todo" (default)
                view = _build_todo_view(
                    user, db, prefill=text, private_metadata=metadata
                )
        finally:
            db.close()

        bot = SlackBotClient(team_id=team_id, timeout=2.0, max_retries=1)
        if not bot.is_available:
            return _ephemeral(
                f"Slack isn't fully connected to {PRODUCT_NAME} yet — ask an admin to finish the "
                "Slack setup in Settings."
            )
        try:
            bot.open_view(trigger_id, view)
        except SlackAPIError as e:
            logger.warning("views.open failed for %s: %s", slack_user_id, e.error)
            return _ephemeral(
                "Sorry — I couldn't open the form. Please try the command again."
            )
        return Response(status_code=200)


@router.post("/commands")
async def slack_command(request: Request):
    """Slash-command entrypoint (``/todo``, ``/ids``) — opens the capture modal."""
    routed = await _verified_inbound(request, interaction=False)
    if isinstance(routed, Response):
        return routed
    slug, team_id, form, _ = routed
    return await run_in_threadpool(_handle_command, slug, team_id, form)


def _handle_submission(
    slug: str,
    team_id: str,
    slack_user_id: str,
    email: str,
    callback_id: str | None,
    state: dict[str, Any],
    view_id: str,
    background_tasks: BackgroundTasks,
) -> Response:
    """Resolve the signed submitter and write one item per Slack view."""
    with use_workspace(slug):
        reject_support_background_work()
        db = SessionLocal()
        try:
            user = _resolve_user(db, slack_user_id, team_id)
            if user is None or user.email.lower() != email.strip().lower():
                return Response(status_code=401, content="invalid modal user")
            if callback_id not in (_ISSUE_CALLBACK, _TODO_CALLBACK):
                return Response(status_code=200)
            if db.get(SlackViewSubmission, view_id) is not None:
                return Response(status_code=200)
            if callback_id == _ISSUE_CALLBACK:
                return _create_issue_from_modal(
                    db, user, state, view_id, background_tasks
                )
            return _create_todo_from_modal(db, user, state, view_id)
        finally:
            db.close()


@router.post("/interactivity")
async def slack_interactivity(request: Request, background_tasks: BackgroundTasks):
    """Interaction entrypoint — modal submissions (``view_submission``)."""
    routed = await _verified_inbound(request, interaction=True)
    if isinstance(routed, Response):
        return routed
    slug, team_id, _, payload = routed
    assert payload is not None

    # We only act on modal submissions; acknowledge anything else
    # (other block_actions, etc.) with an empty 200 so Slack doesn't retry.
    if payload.get("type") != "view_submission":
        return Response(status_code=200)

    view = payload.get("view", {}) or {}
    if not isinstance(view, dict):
        return Response(status_code=400, content="bad view")
    view_id = view.get("id")
    if not isinstance(view_id, str) or not _SLACK_ID.fullmatch(view_id):
        return Response(status_code=400, content="bad view id")
    callback_id = view.get("callback_id")
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        return Response(status_code=401, content="invalid modal workspace")
    if not isinstance(meta, dict) or meta.get("workspace") != slug:
        return Response(status_code=401, content="invalid modal workspace")
    email = meta.get("email")
    if not isinstance(email, str) or not email.strip():
        return Response(status_code=401, content="invalid modal user")
    signed_user = payload.get("user")
    if not isinstance(signed_user, dict) or not isinstance(signed_user.get("id"), str):
        return Response(status_code=401, content="invalid Slack user")
    view_state = view.get("state") or {}
    if not isinstance(view_state, dict):
        return Response(status_code=400, content="bad view state")
    state = view_state.get("values") or {}
    if not isinstance(state, dict):
        return Response(status_code=400, content="bad view state")

    return await run_in_threadpool(
        _handle_submission,
        slug,
        team_id,
        signed_user["id"],
        email,
        callback_id,
        state,
        view_id,
        background_tasks,
    )
