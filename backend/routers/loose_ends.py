"""Sales → Loose Ends: what the morning scan found, and the one action per row.

    GET    /api/sales/loose-ends                         the AE's own list
    GET    /api/sales/loose-ends/{id}/log-preview        the sent emails a log would write
    POST   /api/sales/loose-ends/{id}/log                log those emails in HubSpot
    POST   /api/sales/loose-ends/{id}/draft-reply        a Gmail draft in the thread
    POST   /api/sales/loose-ends/{id}/dismiss            "Not a real miss"
    DELETE /api/sales/loose-ends/mutes/{mute_id}         stop muting a sender/domain
    GET    /api/sales/loose-ends/team                    the owner-attributed aging view
    POST   /api/sales/loose-ends/team/scorecard-setup    add the missing measurables

The contract (columns, reason codes, JSON shapes) is
``docs/loose_ends/README.md`` §5; the scan that fills ``loop_items`` is
``rubik_core/loop_closure/``. This router reads what the scan already decided
and performs the two writes an AE can ask for. It never scans a mailbox itself:
a page load must not cost a Gmail sweep, a HubSpot engagement search and a
Flash call per AE, and "last scanned at" is only honest if there is one scan.

**Gates, in order** (house convention, as ``backend/routers/promises.py``): the
``LOOP_CLOSURE_ENABLED`` kill switch answers 404 (no such surface here), then
the roster answers 403 (it exists and you are not on it yet). The AE endpoints
read the ``loop_closure`` roster; the team endpoints read ``loop_closure_team``,
which is a DIFFERENT audience (Sales managers plus named leaders by email)
and deliberately does not require ``loop_closure`` — a leader is not scanned
just because they can read the roll-up. The base dependency is
``get_current_user``, not ``require_cockpit``: AEs are not on the cockpit
roster, and Loose Ends is theirs.

**Mailbox content never leaves its owner** (README §2). Titles and AI
headlines exist only in the AE's own response. The team response is built from
``TeamItem``, which has no title or headline field at all, so even a view that
handed one over by mistake would have it dropped here by pydantic — the schema
is the last line of that rule, not the only one. Under impersonation the AE
view is fetched with ``content_hidden=True`` (titles blank, headlines omitted),
and the two writes refuse with a 409: an admin viewing as an AE must never log
an email in HubSpot or put a draft in a mailbox under that AE's name. (The
impersonation middleware already refuses every non-GET; the in-endpoint check
is belt to its braces, and gives the sentence the page shows.) The log PREVIEW
is a GET that reads the AE's mailbox, so it refuses under impersonation too —
the middleware would let it through.

**"Log it in HubSpot" logs the AE's own sent email, never a note and never
model text** (Fathom owns call notes). The preview reads
the thread from the AE's mailbox and shows each sent email; the POST names
which ones by Gmail message id, and the server re-reads them — the browser
never supplies the content that reaches HubSpot.

**Closure is detected, never self-reported.** There is no "mark done". The only
manual exit is dismissing an item as "Not a real miss" with a reason, which
the team view counts as noise. Logging from the app does close the item
(``LOOP_CLOSE_LOGGED_FROM_ASSISTANT``), because the emails it logs ARE the
HubSpot evidence the scan would otherwise look for.

Blocking work (HubSpot, Gmail, the read models' queries) runs in the
threadpool so a slow HubSpot write does not stall the event loop for every
other request on the instance.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import get_current_user, require_eos_module
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.identifiers import LOOP_CLOSE_LOGGED_FROM_ASSISTANT
from rubik_core.logsafe import safe_error

logger = logging.getLogger(__name__)

# The ``rubik_core.loop_closure`` modules are imported inside each endpoint, not
# here: the router is imported by ``backend/main.py`` at boot, and the scan's
# leaves (Gmail, HubSpot, Vertex) have no business deciding whether the app
# starts. Every call goes through the module attribute (``views.mine_view``),
# so a test patch by string target reaches it.

#: The two kinds an AE action applies to (README §5 "Mutations").
_LOG_KIND = "unlogged"
_DRAFT_KIND = "open_loop"

#: The sentences the page shows. Plain, no codes.
_IMPERSONATION_WRITE = (
    f"Not available while viewing {PRODUCT_NAME} as someone else. Loose Ends never "
    "writes to HubSpot, drafts an email or changes a list on another "
    "person's behalf."
)
_ALREADY_CLOSED = "This loose end is already closed, so there is nothing to do."


def _email(user: UserProfile) -> str:
    return (user.email or "").strip().lower()


def require_loose_ends(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """404 when switched off, 403 when the caller is not on the roster."""
    if not settings.LOOP_CLOSURE_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows("loop_closure", user):
        raise HTTPException(
            status_code=403,
            detail="Loose Ends is not switched on for your team yet.",
        )
    return user


def require_loose_ends_team(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """The team roll-up's own gate: same switch, the ``loop_closure_team`` roster."""
    if not settings.LOOP_CLOSURE_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows("loop_closure_team", user):
        raise HTTPException(
            status_code=403,
            detail="The Loose Ends team view is limited to Sales leaders.",
        )
    return user


def _refuse_while_impersonating() -> None:
    from rubik_core.auth.impersonation import impersonation_active

    if impersonation_active():
        raise HTTPException(status_code=409, detail=_IMPERSONATION_WRITE)


router = APIRouter(prefix="/sales/loose-ends", tags=["loose-ends"])


# ── Schemas (README §5, field for field) ─────────────────────────────────────


class Age(BaseModel):
    """The cockpit ``AgeChip`` states: ``at_risk`` under the escalation days,
    ``breached`` at or over them."""

    state: str
    label: str


class LooseEndAction(BaseModel):
    #: Closed set, mirrored in ``frontend/src/api/looseEnds.ts``:
    #: ``log_in_hubspot`` | ``draft_reply`` | ``open_link``.
    kind: str
    label: str
    #: Only for ``open_link`` (opens in a new tab).
    href: str | None = None


class LooseEnd(BaseModel):
    id: str
    kind: str
    source: str
    #: Owner-only. ``""`` while impersonating.
    title: str = ""
    #: The AI one-liner. Absent when there is none, and always while
    #: impersonating.
    headline: str | None = None
    counterparty: str | None = None
    company: str | None = None
    opened_at: datetime | None = None
    first_flagged_at: datetime | None = None
    days_open: int = 0
    age: Age | None = None
    reopened: bool = False
    primary_action: LooseEndAction | None = None
    secondary_action: LooseEndAction | None = None


class LooseEndGroup(BaseModel):
    kind: str
    label: str
    hint: str
    empty_line: str
    count: int
    items: list[LooseEnd]


class Coverage(BaseModel):
    """What the last scan could and could not read for this AE.

    A status other than ``ok`` is a named "could not look" state — never an
    all-clear (README §2)."""

    gmail: str | None = None
    slack: str | None = None
    hubspot: str | None = None
    ai: str | None = None
    #: The denominators: what the scan actually looked at.
    threads_scanned: int | None = None
    slack_messages_scanned: int | None = None
    promises_scanned: int | None = None
    tasks_scanned: int | None = None


class Totals(BaseModel):
    open: int
    over_escalation: int
    flagged_this_week: int
    closed_this_week: int


class Thresholds(BaseModel):
    open_loop_business_days: int
    todo_grace_days: int
    todo_undated_days: int
    escalation_days: int


class Mute(BaseModel):
    id: str
    pattern: str
    created_at: datetime | None = None


class MineResponse(BaseModel):
    #: Naive UTC. Absent (``exclude_none``) until the first scan ran.
    scanned_at: datetime | None = None
    #: Absent until the first scan covered this AE.
    coverage: Coverage | None = None
    notes: list[str] = []
    totals: Totals
    groups: list[LooseEndGroup]
    thresholds: Thresholds
    mutes: list[Mute] = []
    content_hidden: bool = False


class TeamCoverage(BaseModel):
    gmail: str | None = None
    slack: str | None = None
    hubspot: str | None = None
    ai: str | None = None


class TeamItem(BaseModel):
    """Metadata only, BY CONSTRUCTION: no title, headline, subject or snippet
    field exists here, so none can reach a cross-person read."""

    id: str
    kind: str
    #: "Jane Doe · Acme Corp" — CRM names, never message text.
    who: str
    days_open: int = 0
    age: Age | None = None


class TeamPerson(BaseModel):
    email: str
    name: str
    open: int
    by_kind: dict[str, int]
    aging: dict[str, int]
    over_escalation: int
    closed_this_week: int
    coverage: TeamCoverage | None = None
    #: Oldest first, at most 25.
    items: list[TeamItem]


class WeeklyHealth(BaseModel):
    week_start: date
    flagged: int
    closed: int
    median_days_to_close: float | None = None


class Health(BaseModel):
    """The spec's success metric. ``noise_rate`` is ``dismissed`` out of
    ``flagged`` over ``window_days`` — both numbers ship beside it."""

    window_days: int
    flagged: int
    dismissed: int
    noise_rate: float | None = None
    dismissed_by_reason: dict[str, int] = {}
    closed: int
    closed_by_reason: dict[str, int] = {}
    median_days_to_close: float | None = None
    weekly: list[WeeklyHealth] = []


class ScorecardStatus(BaseModel):
    metric_name: str
    metrics_found: int
    people_without_metric: int
    #: Missing ones with no Accountability Chart seat: setup cannot add a row
    #: for them, so ``can_setup`` ignores them (emails; the page names them).
    people_without_seat: list[str] = []
    can_setup: bool


class TeamResponse(BaseModel):
    scanned_at: datetime | None = None
    #: A–Z by name. No ranking, no "worst AE" (README §2).
    people: list[TeamPerson]
    roster_size: int
    health: Health | None = None
    scorecard: ScorecardStatus | None = None
    #: Same shape as the AE view's, so the Team tab can say "past 7 days".
    thresholds: Thresholds | None = None


class LogPreviewEmail(BaseModel):
    """One email the AE sent, as "Log it in HubSpot" will log it."""

    message_id: str
    subject: str = ""
    to: list[str] = []
    cc: list[str] = []
    #: Naive UTC.
    sent_at: datetime | None = None
    #: The first 600 characters of the text that will be logged.
    excerpt: str = ""
    #: How many characters of text will be logged in full.
    chars: int = 0
    #: True when the text was cut at 65,000 characters (``crm_log.
    #: MAX_BODY_CHARS``, under HubSpot's 65,536-character property limit).
    truncated: bool = False
    #: True when the formatted (HTML) version is too long for HubSpot and the
    #: email is logged as its plain text.
    plain_text_only: bool = False
    #: To or Cc holds the item's own contact. The page ticks only these by
    #: default; anything else (a forward to a partner) is the AE's call.
    to_contact: bool = False
    #: Already in HubSpot (the Gmail extension, or an earlier click): shown,
    #: never sent again.
    already_logged: bool = False


class LogPreview(BaseModel):
    emails: list[LogPreviewEmail]
    #: The HubSpot names the emails will be logged on. ``deal`` only when the
    #: deal is in one of the company's pipelines (the write gate refuses the rest).
    contact: str | None = None
    company: str | None = None
    deal: str | None = None
    hubspot_url: str | None = None


class LogIn(BaseModel):
    #: Gmail message ids from the preview — which of the AE's sent emails to
    #: log. Never their content: the server re-reads it.
    message_ids: list[str] = []
    #: True when the page will send another batch (more than 20 ticked): the
    #: item stays open and remembers what this batch logged; the last batch
    #: (``more`` false) closes it.
    more: bool = False


class LogResponse(BaseModel):
    #: ``logged`` (the item is closed) or ``in_progress`` (a batch with
    #: ``more``: logged, the item stays open for the next one).
    status: str
    hubspot_url: str | None = None
    #: Emails written now.
    logged: int = 0
    #: Emails that were already in HubSpot (not written again).
    already: int = 0


class DraftResponse(BaseModel):
    status: str
    draft_url: str | None = None


class StatusResponse(BaseModel):
    status: str


class DismissIn(BaseModel):
    reason: str
    #: ``"sender"`` | ``"domain"`` | ``None`` — also stop flagging this
    #: sender / domain for this AE.
    mute: str | None = None


class ScorecardSetupResponse(BaseModel):
    created: int
    skipped_no_person: list[str]
    already: int


# ── Helpers ──────────────────────────────────────────────────────────────────


def _owned_or_404(
    db: Session, owner_email: str, item_id: str, *, for_update: bool = False
):
    """The caller's own item, or 404 — the SAME 404 for someone else's id as
    for an id that does not exist, so ids cannot be probed. ``for_update``
    locks the row for the rest of the request (see ``store.get_owned``)."""
    from rubik_core.loop_closure import store

    if for_update:
        item = store.get_owned(db, owner_email, item_id, for_update=True)
    else:
        item = store.get_owned(db, owner_email, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return item


def _already_logged(item) -> bool:
    """ "Log it in HubSpot" already logged this item's emails."""
    if (
        item.status == "closed"
        and item.close_reason == LOOP_CLOSE_LOGGED_FROM_ASSISTANT
    ):
        return True
    evidence = item.close_evidence if isinstance(item.close_evidence, dict) else {}
    return evidence.get("type") == "emails" and bool(evidence.get("ids"))


def _logged_count(item) -> int:
    """How many emails the first "Log it in HubSpot" put on the record."""
    evidence = item.close_evidence if isinstance(item.close_evidence, dict) else {}
    return len(evidence.get("message_ids") or evidence.get("ids") or [])


def _remember_logged(db: Session, item, rows: list[dict], item_id: str) -> None:
    """Store ``{message id: HubSpot id}`` on the still-open item and commit.

    After a write that failed partway, or a batch sent with ``more``: the
    emails ARE in HubSpot, but HubSpot's search can take a minute to show
    them, so the next preview and the next click read them from here
    (``crm_log.logged_by_cubie``) instead of logging them again. Ids only.
    A failure to store is logged and swallowed — the answer to the AE does
    not change, and the probe is still the backstop.
    """
    from rubik_core.loop_closure import store

    try:
        store.remember_logged_emails(
            item,
            {
                str(r.get("message_id") or ""): str(r.get("hubspot_id") or "")
                for r in rows
            },
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "[loose-ends] could not store the emails logged for item %s: %s",
            item_id,
            safe_error(exc),
        )


def _audit_actor(request: Request, user: UserProfile) -> tuple[str, dict]:
    """``(actor_email, extra details)`` for an audit row.

    Under person impersonation ``user`` is the person being viewed, not the
    admin reading as them; the row must name the admin who actually read it
    (``request.state.impersonation``, as ``/api/users/me`` records it)."""
    imp = getattr(request.state, "impersonation", None)
    if isinstance(imp, dict) and imp.get("actor_email"):
        return str(imp["actor_email"]), {"impersonating": imp.get("target_email")}
    return user.email, {}


def _logged_url(item) -> str | None:
    """Where the logged emails can be seen, for the idempotent second call.
    Prefers what the log step stored; falls back to the record they were
    logged on."""
    evidence = item.close_evidence if isinstance(item.close_evidence, dict) else {}
    url = evidence.get("hubspot_url")
    if url:
        return str(url)
    from rubik_core.sales.rhythm_checks import record_url  # local: heavy leaf

    return record_url("contact", item.hubspot_contact_id) or record_url(
        "company", item.hubspot_company_id
    )


# ── The AE's own list ────────────────────────────────────────────────────────


@router.get("", response_model=MineResponse, response_model_exclude_none=True)
async def mine(
    user: UserProfile = Depends(require_loose_ends),
    db: Session = Depends(get_db),
):
    """The caller's open loose ends, grouped by kind, oldest first.

    Reads what the last scan stored; never scans. While an admin impersonates
    the AE, titles are blank and headlines omitted (``content_hidden``): the
    rest — kind, counterparty, company, age — is CRM data and stays.
    """
    from rubik_core.auth.impersonation import impersonation_active
    from rubik_core.loop_closure import views

    hidden = impersonation_active()
    # A promise with no link of its own falls back to My To-Dos only for a
    # reader who can open that page (a Systems pilot; /todos redirects others).
    has_todos = bool(feature_allows("todos", user))
    return await run_in_threadpool(
        views.mine_view, db, _email(user), content_hidden=hidden, has_todos=has_todos
    )


_NOT_LOGGABLE = (
    "Only an email that is not in HubSpot can be logged from here. For a Slack "
    "conversation, open the record in HubSpot and log it there."
)


def _loggable_or_409(item) -> None:
    """An open, unlogged EMAIL item — anything else is a 409 with a sentence."""
    if item.kind != _LOG_KIND or item.source != "gmail":
        raise HTTPException(status_code=409, detail=_NOT_LOGGABLE)
    if item.status != "open":
        raise HTTPException(status_code=409, detail=_ALREADY_CLOSED)


@router.get(
    "/{item_id}/log-preview",
    response_model=LogPreview,
    response_model_exclude_none=True,
)
async def log_preview(
    item_id: str,
    user: UserProfile = Depends(require_loose_ends),
    db: Session = Depends(get_db),
):
    """The emails "Log it in HubSpot" would log, read from the AE's mailbox.

    Each email the AE sent in the thread since the item opened, with the first
    600 characters of its text, how much text will be logged, and whether
    HubSpot already has it. Own items only (404); an open unlogged email item
    only (409). Refused under impersonation (409): this GET reads the AE's
    mailbox, and the impersonation middleware lets GETs through. Gmail or
    HubSpot unreadable → 502 with a sentence. Nothing is written.
    """
    _refuse_while_impersonating()
    email = _email(user)
    item = _owned_or_404(db, email, item_id)
    _loggable_or_409(item)
    from rubik_core.loop_closure import crm_log

    try:
        return await run_in_threadpool(crm_log.log_preview, item, owner_email=email)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except crm_log.LogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — a sentence, never a 500
        logger.warning(
            "[loose-ends] log preview failed for item %s: %s",
            item_id,
            safe_error(exc),
        )
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} could not read that email just now. Try again in a minute.",
        ) from exc


@router.post("/{item_id}/log", response_model=LogResponse)
async def log_in_hubspot(
    item_id: str,
    body: LogIn | None = None,
    user: UserProfile = Depends(require_loose_ends),
    db: Session = Depends(get_db),
):
    """Log the AE's chosen sent emails in HubSpot, as emails, with their text.

    Body ``{"message_ids": [...], "more": false}`` — 1 to 20 Gmail message ids
    from the preview (the page sends more than 20 in batches, ``more`` true on
    all but the last). The thread is re-read server-side
    (``crm_log.log_emails``); an id that is not one of the AE's loggable sent
    emails is a 400. Each email is probed first and one HubSpot already has is
    not written again. The write goes through ``retrieval/hubspot_write.py``
    (``create_email``) with the Service's write token, in this request — jobs
    never write HubSpot.

    On success the item closes ``LOOP_CLOSE_LOGGED_FROM_ASSISTANT`` with ids
    only as evidence (HubSpot email ids and Gmail message ids, never a subject
    or a body) — or,
    for a batch with ``more``, stays open with what it logged remembered.
    Idempotent: the row is claimed with ``SELECT … FOR UPDATE`` before the
    write, so a double click's second request waits and then answers with the
    first result. If a write fails the item stays open and the answer is a 502
    sentence; the ids of any emails written before the failure are stored on
    the item first, so a retry skips them even before HubSpot's search shows
    them.
    """
    _refuse_while_impersonating()
    from rubik_core.loop_closure import crm_log, store
    from rubik_core.retrieval.hubspot_write import (
        MODE_ALREADY_EXISTS,
        friendly_write_error,
    )

    requested = body.message_ids if body is not None else []
    ids = list(dict.fromkeys(str(m).strip() for m in requested if str(m).strip()))
    if not ids:
        raise HTTPException(status_code=400, detail="Pick at least one email to log.")
    if len(ids) > crm_log.MAX_EMAILS_PER_LOG:
        raise HTTPException(
            status_code=400,
            detail=f"Log at most {crm_log.MAX_EMAILS_PER_LOG} emails at a time.",
        )

    email = _email(user)
    item = _owned_or_404(db, email, item_id, for_update=True)
    if item.kind != _LOG_KIND:
        raise HTTPException(
            status_code=400,
            detail="Only a 'Not in HubSpot' loose end can be logged from here.",
        )
    if _already_logged(item):
        url = _logged_url(item)
        already = _logged_count(item)
        db.rollback()  # release the row
        return LogResponse(status="logged", hubspot_url=url, logged=0, already=already)
    _loggable_or_409(item)

    try:
        result = await run_in_threadpool(
            crm_log.log_emails, item, owner_email=email, message_ids=ids
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except crm_log.LogError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except crm_log.PartialLogError as exc:
        # Some emails ARE in HubSpot. Remember them on the (still open) item
        # before answering: HubSpot's search lags a fresh write, so a quick
        # retry's probe alone would log them a second time.
        _remember_logged(db, item, exc.created, item_id)
        logger.warning(
            "[loose-ends] HubSpot email log failed for item %s after %d written: %s",
            item_id,
            len(exc.created),
            safe_error(exc.cause),
        )
        raise HTTPException(
            status_code=502, detail=friendly_write_error("emails", exc.cause)
        ) from exc
    except Exception as exc:  # noqa: BLE001 — every failure is a sentence, not a 500
        # Nothing was written to HubSpot in this call, so the item stays open
        # for a retry. friendly_write_error passes a refusal through verbatim
        # and turns a raw 403/429/400 into something an AE can act on.
        db.rollback()
        logger.warning(
            "[loose-ends] HubSpot email log failed for item %s: %s",
            item_id,
            safe_error(exc),
        )
        raise HTTPException(
            status_code=502, detail=friendly_write_error("emails", exc)
        ) from exc

    rows = list(result.get("logged") or [])
    already = sum(1 for r in rows if r.get("mode") == MODE_ALREADY_EXISTS)
    if body is not None and body.more:
        _remember_logged(db, item, rows, item_id)
        return LogResponse(
            status="in_progress",
            hubspot_url=result.get("hubspot_url") or None,
            logged=len(rows) - already,
            already=already,
        )
    # Evidence names every email this item put on the record: earlier batches
    # (or a click that failed partway) as well as this one.
    every = {
        **crm_log.logged_by_cubie(item),
        **{
            str(r.get("message_id") or ""): str(r.get("hubspot_id") or "") for r in rows
        },
    }
    try:
        store.mark_logged_from_assistant(
            db,
            item,
            email_ids=list(every.values()),
            message_ids=list(every),
            actor=email,
        )
        db.commit()
    except ValueError:
        # The emails ARE in HubSpot; the item changed under us (a scan closed
        # it between the read and the write). Telling the AE it failed would
        # invite a retry, so this still answers "logged" and the next scan
        # sees the emails as ordinary HubSpot evidence.
        db.rollback()
        logger.info(
            "[loose-ends] item %s changed during the HubSpot write; emails kept",
            item_id,
        )
    return LogResponse(
        status="logged",
        hubspot_url=result.get("hubspot_url") or None,
        logged=len(rows) - already,
        already=already,
    )


@router.post("/{item_id}/draft-reply", response_model=DraftResponse)
async def draft_reply(
    item_id: str,
    user: UserProfile = Depends(require_loose_ends),
    db: Session = Depends(get_db),
):
    """Create a Gmail draft in the client's thread. Never sends.

    Only an ``open`` item of kind ``open_loop``. Reads the thread from the
    caller's OWN mailbox, one Flash call (``loop_closure`` cost category), then
    ``GmailClient.create_draft(..., thread_id=…)``. The item stays open: it
    closes when the scan sees the reply actually went out.
    """
    _refuse_while_impersonating()
    email = _email(user)
    item = _owned_or_404(db, email, item_id)
    if item.kind != _DRAFT_KIND:
        raise HTTPException(
            status_code=400,
            detail="Only a 'Waiting on your reply' loose end can be drafted.",
        )
    if item.status != "open":
        raise HTTPException(status_code=409, detail=_ALREADY_CLOSED)

    from rubik_core.loop_closure import drafts

    try:
        result = await run_in_threadpool(drafts.draft_reply, item, owner_email=email)
    except drafts.DraftError as exc:
        # DraftError messages are written for the AE ("Gmail could not be
        # read…"), so they are shown as they are.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — a sentence, never a 500
        logger.warning(
            "[loose-ends] draft failed for item %s: %s", item_id, safe_error(exc)
        )
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} could not draft that reply. Try again in a minute.",
        ) from exc
    return DraftResponse(status="drafted", draft_url=result.get("draft_url"))


@router.post("/{item_id}/dismiss", response_model=StatusResponse)
async def dismiss(
    item_id: str,
    body: DismissIn,
    user: UserProfile = Depends(require_loose_ends),
    db: Session = Depends(get_db),
):
    """The one manual exit, "Not a real miss", always with a reason.

    Counted as noise on the team view — the spec's success metric. With
    ``mute`` set, the sender (or their whole domain) is not flagged again for
    this AE; the mute is listed on the page and can be removed. A dismissed
    item is never re-opened or re-flagged.
    """
    _refuse_while_impersonating()
    from rubik_core.loop_closure import constants, store

    reason = (body.reason or "").strip().lower()
    mute = (body.mute or "").strip().lower() or None
    if reason not in constants.DISMISS_REASONS:
        raise HTTPException(
            status_code=400,
            detail="Pick one of the reasons offered for 'Not a real miss'.",
        )
    if mute is not None and mute not in constants.MUTE_SCOPES:
        raise HTTPException(
            status_code=400,
            detail="A mute is either this sender or their whole domain.",
        )
    email = _email(user)
    item = _owned_or_404(db, email, item_id)
    if item.status != "open":
        raise HTTPException(status_code=409, detail=_ALREADY_CLOSED)
    try:
        store.dismiss(db, item, reason=reason, actor=email, mute=mute)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return StatusResponse(status="dismissed")


@router.delete("/mutes/{mute_id}", response_model=StatusResponse)
async def remove_mute(
    mute_id: str,
    user: UserProfile = Depends(require_loose_ends),
    db: Session = Depends(get_db),
):
    """Stop muting a sender or domain. Own mutes only (404 otherwise)."""
    _refuse_while_impersonating()
    from rubik_core.loop_closure import store

    if not store.remove_mute(db, _email(user), mute_id):
        raise HTTPException(status_code=404, detail="Not found.")
    db.commit()
    return StatusResponse(status="removed")


# ── The team view (Sales leaders) ────────────────────────────────────────────


@router.get("/team", response_model=TeamResponse)
async def team(
    request: Request,
    user: UserProfile = Depends(require_loose_ends_team),
    db: Session = Depends(get_db),
):
    """The L10 artifact: every scanned AE, A–Z, with their items oldest first.

    Metadata only — owner, kind, CRM company / contact name, days open and the
    coverage states. No titles, headlines, subjects or snippets (see
    ``TeamItem``). Every read writes an audit row: it is a cross-person read of
    what a scan of other people's mailboxes found.
    """
    from rubik_core.loop_closure import views

    body = await run_in_threadpool(views.team_view, db)
    actor, extra = _audit_actor(request, user)
    record_audit(
        db,
        actor_email=actor,
        action="LOOSE_ENDS_TEAM_VIEW",
        target="loose-ends:team",
        details={
            "roster_size": body.get("roster_size"),
            "people": len(body.get("people") or []),
            **extra,
        },
    )
    return body


@router.post(
    "/team/scorecard-setup",
    response_model=ScorecardSetupResponse,
    dependencies=[Depends(require_eos_module)],
)
async def scorecard_setup(
    request: Request,
    user: UserProfile = Depends(require_loose_ends_team),
    db: Session = Depends(get_db),
):
    """Create the missing per-AE "Loose ends over 7 days" measurables.

    In the Sales scorecard, owned by each AE's ``eos_people`` row; an AE with
    no such row is listed back rather than guessed at. ``<= 0``, weekly, with a
    description saying Cubie fills it. Safe to press twice: rows that exist are
    counted under ``already``.
    """
    _refuse_while_impersonating()
    from rubik_core.loop_closure import scorecard

    try:
        result = await run_in_threadpool(scorecard.setup_metrics, db, actor=user.email)
    except scorecard.ScorecardMetricUnconfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    actor, extra = _audit_actor(request, user)
    record_audit(
        db,
        actor_email=actor,
        action="LOOSE_ENDS_SCORECARD_SETUP",
        target="loose-ends:scorecard",
        details={
            "created": result.get("created"),
            "already": result.get("already"),
            "skipped_no_person": len(result.get("skipped_no_person") or []),
            **extra,
        },
    )
    return result
