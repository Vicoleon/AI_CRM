"""Sales Cockpit → My Day → "Daily rhythm": the checklist's three routines.

    GET /api/sales/rhythm/{part}      part = morning | midday | eod

A sales team's daily checklist of AI prompts lives in Cubie, in two halves.
This is the My Day half: one tab per routine, each a short worklist of
HubSpot checks on the rep's own deals. The other half is the matching row of
Ask Cubie starter prompts (``frontend/src/pages/chatSuggestions.ts``), and the
two share one definition per check (``rubik_core/sales/rhythm_checks.py``), so
a card and the chat answer behind its "+N more" link always agree.

    morning  closing_focus
    midday   waiting_on_decision_maker, close_date_slippage, single_threaded,
             reengage_lost
    eod      missing_fields, overdue  (+ the Ask Cubie prompt that drafts the
             manager update from today's logged activity)

**It sits under the AE's five blocks, not in place of them.** My Day's order is
load-bearing (a parallel ritual will not survive), so this is a section below
the existing worklist, loaded per tab on demand, and
``backend/routers/cockpit.py`` is untouched.

**The cockpit's worklist rules hold here too**: three items per
block with the overflow as a count (the count links to the Ask Cubie question
that lists everything), and exactly one primary action per row. Both action
kinds are links, and both lead to a write: ``open_hubspot`` opens the record
where the rep books the next step or fills the field (the same reasoning as the
cockpit's ``log_outreach``), and ``ask_cubie`` opens Ask Cubie with the draft
request ready to run, which ends in a Gmail draft.

Gates, in order: the cockpit (``require_cockpit`` — COCKPIT_ENABLED plus the
My Day roster), then ``SALES_RHYTHM_ENABLED``, then the ``sales_rhythm``
roster. Personal view only: every check reads the caller's own deals.

**Deferred deals are left out** until their follow-up date, with a note saying
how many, and every deal row carries a Defer handle for people on the
``deal_defer`` roster (``backend/routers/deal_defer.py``).
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.auth import require_cockpit
from backend.routers.cockpit import Action, DeferTarget, Item
from rubik_core.cockpit.digest import MAX_INAPP_ITEMS
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows

logger = logging.getLogger(__name__)


def require_sales_rhythm(user: UserProfile = Depends(require_cockpit)) -> UserProfile:
    """404 when switched off, 403 when the caller is not on the roster."""
    if not settings.SALES_RHYTHM_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows("sales_rhythm", user):
        raise HTTPException(
            status_code=403,
            detail="The daily rhythm is not switched on for your team yet.",
        )
    return user


router = APIRouter(prefix="/sales/rhythm", tags=["sales-rhythm"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class RhythmBlock(BaseModel):
    """One check, as a capped worklist block."""

    key: str
    label: str
    hint: str
    items: list[Item]
    suppressed_count: int
    #: The denominator — what the qualifying rows are out of.
    scanned: int
    scanned_label: str
    #: "Cubie could not look", as a named code: crm_unavailable,
    #: crm_read_failed, no_hubspot_owner or no_hubspot_user. Absent when the
    #: block is fine.
    unavailable_reason: str | None = None
    #: A caveat the rep should read with the rows (half a check ran, a field
    #: that does not exist in this portal, what "still there" rests on).
    note: str | None = None
    #: The starter-prompt wording of this check (the same text as the Ask
    #: Cubie chip).
    ask_prompt: str
    #: The Ask Cubie question behind "+N more": it lists EVERY qualifying row.
    #: Differs from ``ask_prompt`` where the checklist's own wording caps the
    #: list ("which 3 deals…", "find 5 deals…").
    more_prompt: str
    #: What an empty, available block means, in this check's own words — the
    #: same sentence the chat answer uses (rhythm_checks.CHAT_COPY).
    empty_line: str


class RhythmResponse(BaseModel):
    part: str
    blocks: list[RhythmBlock]
    #: False when HubSpot could not be reached at all.
    crm_available: bool
    #: End of day only: the Ask Cubie question that drafts the manager update.
    recap_prompt: str | None = None


# ── Copy ─────────────────────────────────────────────────────────────────────

#: Every ``ask_prompt`` below is ALSO a starter prompt in
#: ``frontend/src/pages/chatSuggestions.ts`` (SALES_RHYTHM_PROMPTS.rep);
#: ``frontend/src/api/salesRhythm.test.ts`` fails if the two drift.
_BLOCKS: dict[str, dict[str, str]] = {
    "closing_focus": {
        "label": "Closing this month — needs action",
        "hint": "Closing this month with no recent contact (logged, emailed or met), no next step booked, or a date that keeps moving.",
        "scanned_label": "open deals closing this month",
        "ask_prompt": "Which 3 deals closing this month need action from me today to keep them from slipping?",
        "more_prompt": "List all of my deals closing this month that need action from me today.",
    },
    "waiting_on_decision_maker": {
        "label": "Waiting on the client",
        "hint": "Proposal-stage deals where your last email or logged contact is 3+ business days old and the decision-maker has not replied.",
        "scanned_label": "open deals in a proposal, SOW, legal or contract stage",
        "ask_prompt": "Which of my deals in a proposal or negotiation stage have been waiting on the decision-maker for more than 3 business days?",
    },
    "close_date_slippage": {
        "label": "Slipping",
        "hint": "Close date pushed 3+ times, or 7 days with no client contact — logged, emailed or met.",
        "scanned_label": "open deals",
        "ask_prompt": "Show me my active deals whose close date has been pushed more than twice, or with no activity logged in the last 7 days.",
    },
    "single_threaded": {
        "label": "Single-threaded",
        "hint": "Open deals of $30,000 or more that hang on one contact.",
        "scanned_label": "open deals of $30,000 or more",
        "ask_prompt": "List my active deals over $30k that only have one contact.",
    },
    "reengage_lost": {
        "label": "Worth a check-in",
        "hint": "Lost 12–24 months ago over timing or budget, and the contact looks to still be there.",
        "scanned_label": "deals lost 12–24 months ago",
        "ask_prompt": "Find 5 deals I lost 12–24 months ago over timing or budget where the main contact is still at the company.",
        "more_prompt": "List all the deals I lost 12–24 months ago over timing or budget where the main contact is still at the company.",
    },
    "missing_fields": {
        "label": "Missing fields",
        "hint": "Contacts and deals you touched today without a job title, buying role or next step (a booked meeting counts as one).",
        "scanned_label": "contacts and open deals you created or updated today",
        "ask_prompt": "Which contacts or deals I created or updated today are missing a job title, buying role or next step?",
    },
    "overdue": {
        "label": "Overdue",
        "hint": "HubSpot tasks past their due date, and deals past their close date.",
        "scanned_label": "open tasks and open deals",
        "ask_prompt": "Show me my overdue HubSpot tasks and any of my deals whose close date has already passed.",
    },
}

#: One literal on one line, so the client drift test can read it verbatim.
RECAP_PROMPT = "Summarize my calls, emails, meetings and stage changes from today as a short bulleted update for my manager."

PARTS: dict[str, tuple[str, ...]] = {
    "morning": ("closing_focus",),
    "midday": (
        "waiting_on_decision_maker",
        "close_date_slippage",
        "single_threaded",
        "reengage_lost",
    ),
    "eod": ("missing_fields", "overdue"),
}


# ── Rows → items ─────────────────────────────────────────────────────────────


def _ask_href(prompt: str) -> str:
    """An Ask Cubie link that runs ``prompt`` on arrival (Chat reads ``?q=``)."""
    return f"/chat?q={quote(prompt, safe='')}"


def _open(label: str, row) -> Action:
    return Action(label=label, kind="open_hubspot", href=row.url)


def _ask(label: str, prompt: str) -> Action:
    return Action(label=label, kind="ask_cubie", href=_ask_href(prompt))


def _check_in_prompt(row) -> str:
    extras = row.extras or {}
    who = extras.get("contact_name") or "the main contact"
    email = extras.get("contact_email")
    company = extras.get("company_name")
    target = (
        who + (f" ({email})" if email else "") + (f" at {company}" if company else "")
    )
    why = extras.get("loss_kind") or "timing"
    lost_on = extras.get("lost_on") or ""
    when = f" in {lost_on[:7]}" if lost_on else ""
    return (
        f"Draft a friendly 3-sentence check-in email to {target}, referencing the "
        f'"{row.title}" proposal we discussed before it paused over {why}{when}. '
        "Save it as a Gmail draft."
    )


def _follow_up_prompt(row) -> str:
    extras = row.extras or {}
    who = extras.get("contact_name") or "the decision-maker"
    email = extras.get("contact_email")
    target = who + (f" ({email})" if email else "")
    return (
        f"Draft a friendly 3-sentence follow-up email to {target} about the "
        f'"{row.title}" proposal, referencing our last conversation. '
        "Save it as a Gmail draft."
    )


def _deal_check_in_prompt(row) -> str:
    return (
        f'Draft a short check-in email to keep the "{row.title}" deal moving, '
        "using our last logged conversation in HubSpot for context. "
        "Save it as a Gmail draft."
    )


def _item(check: str, row, *, can_defer: bool = False) -> Item:
    """One flagged record as a worklist row with exactly one primary action.

    A deal row also carries the Defer handle when the reader may defer deals.
    """
    subtitle = " · ".join(str(r) for r in row.reasons if r)
    primary: Action
    secondary: Action | None = None
    if check == "closing_focus":
        primary = _open("Book the next step in HubSpot", row)
        secondary = _ask("Draft a check-in", _deal_check_in_prompt(row))
    elif check == "waiting_on_decision_maker":
        primary = _ask("Draft a follow-up", _follow_up_prompt(row))
        secondary = _open("Open in HubSpot", row)
    elif check == "close_date_slippage":
        primary = _open("Re-date or log it in HubSpot", row)
        secondary = _ask("Draft a check-in", _deal_check_in_prompt(row))
    elif check == "single_threaded":
        primary = _open("Add a contact in HubSpot", row)
    elif check == "reengage_lost":
        primary = _ask("Draft a check-in", _check_in_prompt(row))
        secondary = _open("Open in HubSpot", row)
    elif check == "missing_fields":
        primary = _open("Fill it in HubSpot", row)
    elif row.object_type == "task":  # overdue
        primary = _open("Open my HubSpot tasks", row)
    else:
        primary = _open("Re-date it in HubSpot", row)
    title = row.title
    if row.amount is not None and row.object_type == "deal":
        title = f"{row.title} · ${row.amount:,.0f}"
    return Item(
        id=f"{row.object_type}-{row.record_id}",
        title=title,
        subtitle=subtitle or None,
        primary_action=primary,
        secondary_action=secondary,
        defer=(
            DeferTarget(deal_id=str(row.record_id), deal_name=row.title)
            if can_defer and row.object_type == "deal" and row.record_id
            else None
        ),
    )


def _empty_line(check: str) -> str:
    """My Day is always the rep's OWN view, so its wording counts their email
    and calendar too (rhythm_checks.chat_copy with ``own=True``)."""
    from rubik_core.sales.rhythm_checks import chat_copy  # local: heavy leaf

    return chat_copy(check, own=True)[2]


def _block(check: str, result, *, can_defer: bool = False) -> RhythmBlock:
    copy = _BLOCKS[check]
    items = [_item(check, row, can_defer=can_defer) for row in result.rows]
    shown = items[:MAX_INAPP_ITEMS]
    return RhythmBlock(
        key=check,
        label=copy["label"],
        hint=copy["hint"],
        items=shown,
        suppressed_count=max(len(items) - len(shown), 0),
        scanned=result.scanned,
        scanned_label=copy["scanned_label"],
        unavailable_reason=None
        if result.available
        else (result.reason or "crm_read_failed"),
        note=" ".join(result.notes) or None,
        ask_prompt=copy["ask_prompt"],
        more_prompt=copy.get("more_prompt", copy["ask_prompt"]),
        empty_line=_empty_line(check),
    )


def _empty_block(check: str, reason: str) -> RhythmBlock:
    copy = _BLOCKS[check]
    return RhythmBlock(
        key=check,
        label=copy["label"],
        hint=copy["hint"],
        items=[],
        suppressed_count=0,
        scanned=0,
        scanned_label=copy["scanned_label"],
        unavailable_reason=reason,
        ask_prompt=copy["ask_prompt"],
        more_prompt=copy.get("more_prompt", copy["ask_prompt"]),
        empty_line=_empty_line(check),
    )


def _build(
    part: str, email: str, *, own_mailbox: bool = False, can_defer: bool = False
) -> RhythmResponse:
    """Run the part's checks for one caller. Synchronous; never raises.

    Deals the caller deferred are dropped from every block until their
    follow-up date, and the block's note says how many
    (``rubik_core/sales/deal_defer.py``). Applied to a copy of each answer,
    after the five-minute cache, so a defer takes effect on the next load.
    """
    from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
    from rubik_core.sales import deal_defer
    from rubik_core.sales import rhythm_checks as rc

    checks = PARTS[part]
    recap = RECAP_PROMPT if part == "eod" else None
    try:
        client = HubSpotAPIClient()
    except Exception as exc:  # noqa: BLE001 — an unset token is the normal local state
        logger.info("[rhythm] HubSpot unavailable: %s", exc)
        return RhythmResponse(
            part=part,
            blocks=[_empty_block(c, "crm_unavailable") for c in checks],
            crm_available=False,
            recap_prompt=recap,
        )
    try:
        # ``own_mailbox`` only from the rep's OWN My Day request — never while an
        # admin is impersonating them, which must stay HubSpot-only.
        scope = rc.resolve_scope(client, email, own_mailbox=own_mailbox)
    except Exception:  # noqa: BLE001
        logger.warning("[rhythm] owner directory read failed", exc_info=True)
        return RhythmResponse(
            part=part,
            blocks=[_empty_block(c, "crm_unavailable") for c in checks],
            crm_available=False,
            recap_prompt=recap,
        )
    if scope is None:
        logger.warning(
            "[rhythm] hubspot_owner_miss: no HubSpot owner matches %s", email
        )
        return RhythmResponse(
            part=part,
            blocks=[_empty_block(c, "no_hubspot_owner") for c in checks],
            crm_available=True,
            recap_prompt=recap,
        )
    results = {c: rc.run_check(c, client, scope) for c in checks}
    defers = deal_defer.active_by_deal(
        {
            str(row.record_id)
            for result in results.values()
            for row in result.rows
            if row.object_type == "deal"
        },
        reader=email,
    )
    blocks = [
        _block(
            c,
            deal_defer.apply_to_check(result, defers, hide=True),
            can_defer=can_defer,
        )
        for c, result in results.items()
    ]
    return RhythmResponse(
        part=part, blocks=blocks, crm_available=True, recap_prompt=recap
    )


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.get("/{part}", response_model=RhythmResponse, response_model_exclude_none=True)
async def rhythm(part: str, user: UserProfile = Depends(require_sales_rhythm)):
    """One routine's checks for the signed-in rep, three rows per block."""
    if part not in PARTS:
        raise HTTPException(
            status_code=404,
            detail="Unknown part of the day — use morning, midday or eod.",
        )
    from rubik_core.auth.impersonation import impersonation_active

    email = (user.email or "").strip().lower()
    # An admin impersonating the rep sees their HubSpot checks, never their
    # inbox or calendar (the Workspace credential lock refuses it anyway).
    own_mailbox = not impersonation_active()
    return await run_in_threadpool(
        _build,
        part,
        email,
        own_mailbox=own_mailbox,
        can_defer=feature_allows("deal_defer", user),
    )
