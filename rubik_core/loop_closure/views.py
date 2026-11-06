"""Read models for the Loose Ends page, the Team tab and the chat tool.

Two shapes, exactly as ``docs/loose_ends/README.md`` §5 documents them:

* :func:`mine_view` — the AE's own list. Titles and AI headlines appear here
  and nowhere else; under impersonation (``content_hidden``) the title is
  blanked, the headline is left out, and everything else — kind,
  counterparty, company, age — is CRM data and stays. What "Log it in
  HubSpot" will log is not on the row at all: the page asks for it
  (``GET …/log-preview``) when the AE clicks, read fresh from their mailbox.
* :func:`team_view` — the owner-attributed aging view for Sales leaders.
  **No title, headline, subject or snippet can appear in it, by
  construction**: each person's items are built by :func:`_team_item` from a
  whitelist of five fields, never by filtering a richer dict. A field added to
  ``loop_items`` later cannot leak into a cross-person read by accident.

No leaderboard (README §2): people are listed A–Z by name, items oldest first,
and nothing is ranked. Every number ships with its denominator — the noise
rate beside the flagged count it is a share of, "closed" beside "flagged".

Datetimes leave here as naive-UTC ISO strings (``2026-10-07T12:04:11``), the
shape the router's models parse and the chat tool prints.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.database.loop_closure import LoopItem, LoopScanRun
from rubik_core.loop_closure import constants
from rubik_core.loop_closure.store import mutes_for

logger = logging.getLogger(__name__)

#: Items per group on the AE's page (the count says how many there are).
MAX_ITEMS_PER_GROUP = 100
#: Items per person on the Team tab (README §5).
MAX_TEAM_ITEMS = 25
HEALTH_WINDOW_DAYS = 30
#: How many recent runs to look through for the last one that covered an AE.
_RUN_LOOKBACK = 20

_MINE_COVERAGE_KEYS = (
    "gmail",
    "slack",
    "hubspot",
    "ai",
    "threads_scanned",
    "slack_messages_scanned",
    "promises_scanned",
    "tasks_scanned",
)
_TEAM_COVERAGE_KEYS = ("gmail", "slack", "hubspot", "ai")


# ── Time helpers ─────────────────────────────────────────────────────────────


def _now(now: datetime | None) -> datetime:
    """Naive UTC, the shape every column holds."""
    if now is None:
        return datetime.utcnow()
    if now.tzinfo is None:
        return now
    return now.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    return _now(dt).isoformat(timespec="seconds") if dt else None


def _business_tz():
    from rubik_core.cockpit.business_hours import _business_tz as tz

    return tz()


def week_start(now: datetime | None = None) -> datetime:
    """Monday 00:00 of this week in the business timezone, as naive UTC.

    "This week" on every surface is the Monday-to-Sunday week the scorecard
    row belongs to, on the workspace business clock — not a rolling seven
    days, so Monday's numbers start over the way the scorecard's do.
    """
    local = _now(now).replace(tzinfo=timezone.utc).astimezone(_business_tz())
    monday = local.date() - timedelta(days=local.weekday())
    start = datetime.combine(monday, datetime.min.time(), _business_tz())
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def _local_monday(dt: datetime) -> date:
    local = _now(dt).replace(tzinfo=timezone.utc).astimezone(_business_tz())
    return local.date() - timedelta(days=local.weekday())


def days_open(item: Any, now: datetime) -> int:
    """Whole days since the underlying event, never negative.

    A HubSpot task counts calendar days in the business timezone, the way the
    scan flags it: due Oct 8, today Oct 13 is 5 days overdue at any hour, not
    "4 days" until the clock passes the task's due time. Everything else counts
    whole 24-hour days (a dated promise's event is local midnight of its date,
    so that is its calendar-day count too)."""
    if item.opened_at is None:
        return 0
    if getattr(item, "source", None) == "hubspot_task":
        tz = _business_tz()
        today = _now(now).replace(tzinfo=timezone.utc).astimezone(tz).date()
        due = _now(item.opened_at).replace(tzinfo=timezone.utc).astimezone(tz).date()
        return max(0, (today - due).days)
    return max(0, (_now(now) - _now(item.opened_at)).days)


def listed(item: Any, now: datetime, limits: dict) -> bool:
    """Whether an open item is on the list today.

    A to-do is listed once it is ``todo_grace_days`` overdue
    (``todo_undated_days`` for a promise that had no date) — the line the scan
    flags it by. So this only ever leaves out a to-do flagged under a lower
    grace before it was raised (3 → 5 days, say): it stays open, and
    comes back by itself, with its full age, the day it crosses the line.
    Every surface that counts open items reads through this, so the page, the
    Team tab, the digest, the chat answer and the scorecard agree.
    """
    if getattr(item, "kind", None) != "todo":
        return True
    detail = item.detail if isinstance(getattr(item, "detail", None), dict) else {}
    floor = (
        limits["todo_undated_days"]
        if detail.get("dated") is False
        else limits["todo_grace_days"]
    )
    return days_open(item, now) >= floor


def age_label(days: int) -> str:
    if days <= 0:
        return "under 1 day"
    return "1 day" if days == 1 else f"{days} days"


def age_chip(days: int, escalation_days: int) -> dict:
    """The cockpit ``AgeChip`` states: ``at_risk`` under the escalation days,
    ``breached`` at or over them."""
    return {
        "state": "breached" if days >= escalation_days else "at_risk",
        "label": age_label(days),
    }


# ── Runs and coverage ────────────────────────────────────────────────────────


def latest_run(db: Session) -> LoopScanRun | None:
    """The most recent completed scan. A dry run rolls back, so every row here
    is a real one."""
    return (
        db.query(LoopScanRun)
        .filter(LoopScanRun.status == "completed")
        .order_by(LoopScanRun.started_at.desc())
        .first()
    )


def _recent_runs(db: Session) -> list[LoopScanRun]:
    return (
        db.query(LoopScanRun)
        .filter(LoopScanRun.status == "completed")
        .order_by(LoopScanRun.started_at.desc())
        .limit(_RUN_LOOKBACK)
        .all()
    )


def _covering(runs: list[LoopScanRun], owner: str) -> LoopScanRun | None:
    """The most recent run whose coverage includes ``owner`` — a one-AE CLI
    run must not make everyone else look never-scanned."""
    for run in runs:
        if isinstance(run.coverage, dict) and owner in run.coverage:
            return run
    return None


def _run_covering(db: Session, owner: str) -> LoopScanRun | None:
    return _covering(_recent_runs(db), owner)


def _coverage(raw: Any, keys: tuple[str, ...]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {k: raw.get(k) for k in keys if raw.get(k) is not None}


# ── The AE's own list ────────────────────────────────────────────────────────


def _link_label(href: str) -> str:
    if "mail.google.com" in href:
        return "Open in Gmail"
    if "slack.com" in href or href.startswith("slack://"):
        return "Open in Slack"
    if "hubspot.com" in href:
        return "Open in HubSpot"
    if "drive.google.com" in href:
        return "Open the meeting record"
    if href.startswith("/eos/"):
        return "Open the L10"
    return "Open"


#: The owner's own copy of the source, labelled by where it lives.
_SOURCE_LINK_LABELS = {"gmail": "Open in Gmail", "slack": "Open in Slack"}


def _open_link(href: str | None, label: str | None = None) -> dict | None:
    if not href:
        return None
    return {"kind": "open_link", "label": label or _link_label(href), "href": href}


def _actions(
    item: LoopItem, *, content_hidden: bool, has_todos: bool = False
) -> tuple[dict, dict | None]:
    """``(primary, secondary)`` — one real action per row.

    "Log it in HubSpot" is offered only on an unlogged EMAIL with a HubSpot
    contact: it logs the AE's own sent email on that contact (``crm_log``), and
    an email engagement with no contact would show on no record. Without one,
    the row opens the HubSpot record (else Gmail) for the AE to log it there. A
    Slack item never gets a log button — there is no email to log, and Cubie
    writes no notes (Fathom owns call notes) — so it opens the company or deal
    in HubSpot, with the Slack message beside it.

    ``has_todos`` is the reader's ``todos`` verdict: the My To-Dos page is the
    fallback home of a promise only for someone who can open it (Feature Access
    can switch it off; for anyone else /todos redirects to the briefing)."""
    from rubik_core.loop_closure.crm_log import hubspot_url_for

    if item.kind == "unlogged":
        source_link = _open_link(item.link, _SOURCE_LINK_LABELS.get(item.source))
        if item.source == "gmail" and (item.hubspot_contact_id or "").strip():
            return {"kind": "log_in_hubspot", "label": "Log it in HubSpot"}, source_link
        hubspot = _open_link(hubspot_url_for(item), "Open in HubSpot")
        if hubspot:
            return hubspot, source_link
        return (source_link or _fallback(has_todos)), None
    if item.kind == "open_loop":
        return (
            {"kind": "draft_reply", "label": "Draft a reply"},
            _open_link(item.link, "Open in Gmail"),
        )
    if item.source == "hubspot_task":
        from rubik_core.sales.rhythm_checks import record_url

        primary = _open_link(item.link, "Open the task") or _fallback(has_todos)
        secondary = _open_link(
            record_url("company", item.hubspot_company_id), "Open the company"
        )
        return primary, secondary
    # A heard promise: where it was said, else the To-Dos ledger it lives in.
    return _open_link(item.link) or _fallback(has_todos), None


def _fallback(has_todos: bool = False) -> dict:
    """The action of a row with no link of its own. My To-Dos only for a
    reader who can open it; otherwise an ``open_link`` with no ``href``, which
    the page renders as a disabled button that says the link is missing —
    never a link that silently lands somewhere else."""
    if has_todos:
        return {"kind": "open_link", "label": "Open My To-Dos", "href": "/todos"}
    return {"kind": "open_link", "label": "No link to open"}


def loose_end(
    item: LoopItem,
    *,
    now: datetime,
    escalation_days: int,
    content_hidden: bool,
    has_todos: bool = False,
) -> dict:
    """One ``LooseEnd`` (README §5). Optional keys are left out, not null."""
    days = days_open(item, now)
    primary, secondary = _actions(
        item, content_hidden=content_hidden, has_todos=has_todos
    )
    out: dict[str, Any] = {
        "id": item.id,
        "kind": item.kind,
        "source": item.source,
        "title": "" if content_hidden else (item.title or ""),
        "opened_at": _iso(item.opened_at),
        "first_flagged_at": _iso(item.first_flagged_at),
        "days_open": days,
        "age": age_chip(days, escalation_days),
        "reopened": int(item.reopened_count or 0) > 0,
        "primary_action": primary,
    }
    if item.headline and not content_hidden:
        out["headline"] = item.headline
    if item.counterparty_label or item.counterparty_email:
        out["counterparty"] = item.counterparty_label or item.counterparty_email
    if item.company_label:
        out["company"] = item.company_label
    if secondary:
        out["secondary_action"] = secondary
    return out


def _thresholds(limits: dict) -> dict:
    """The four tunables every surface prints (README §5), one shape for both
    the AE's page and the Team tab."""
    return {
        "open_loop_business_days": limits["open_loop_business_days"],
        "todo_grace_days": limits["todo_grace_days"],
        "todo_undated_days": limits["todo_undated_days"],
        "escalation_days": limits["escalation_days"],
    }


def mine_view(
    db: Session,
    owner_email: str,
    *,
    now: datetime | None = None,
    content_hidden: bool = False,
    has_todos: bool = False,
) -> dict:
    """The AE's page: last scan, coverage + notes, totals, the three groups.

    ``has_todos`` is the reader's ``todos`` verdict (see :func:`_fallback`)."""
    owner = (owner_email or "").strip().lower()
    stamp = _now(now)
    limits = constants.thresholds()
    esc = limits["escalation_days"]

    run = _run_covering(db, owner)
    coverage = run.coverage.get(owner) if run is not None else None

    rows = [
        r
        for r in (
            db.query(LoopItem)
            .filter(LoopItem.owner_email == owner, LoopItem.status == "open")
            .order_by(LoopItem.opened_at.asc(), LoopItem.id.asc())
            .all()
        )
        if listed(r, stamp, limits)
    ]
    monday = week_start(stamp)
    flagged_this_week = (
        db.query(func.count(LoopItem.id))
        .filter(LoopItem.owner_email == owner, LoopItem.first_flagged_at >= monday)
        .scalar()
        or 0
    )
    closed_this_week = (
        db.query(func.count(LoopItem.id))
        .filter(
            LoopItem.owner_email == owner,
            LoopItem.status == "closed",
            LoopItem.closed_at >= monday,
            LoopItem.close_reason.in_(constants.FIXED_CLOSE_REASONS),
        )
        .scalar()
        or 0
    )

    groups = []
    for kind in constants.KINDS:
        items = [r for r in rows if r.kind == kind]
        groups.append(
            {
                "kind": kind,
                "label": constants.KIND_LABELS[kind],
                "hint": constants.kind_hint(kind, limits),
                "empty_line": constants.kind_empty_line(kind, limits),
                "count": len(items),
                "items": [
                    loose_end(
                        r,
                        now=stamp,
                        escalation_days=esc,
                        content_hidden=content_hidden,
                        has_todos=has_todos,
                    )
                    for r in items[:MAX_ITEMS_PER_GROUP]
                ],
            }
        )

    return {
        "scanned_at": _iso(run.finished_at or run.started_at) if run else None,
        "coverage": _coverage(coverage, _MINE_COVERAGE_KEYS),
        "notes": constants.coverage_notes(coverage),
        "totals": {
            "open": len(rows),
            "over_escalation": sum(1 for r in rows if days_open(r, stamp) >= esc),
            "flagged_this_week": int(flagged_this_week),
            "closed_this_week": int(closed_this_week),
        },
        "groups": groups,
        "thresholds": _thresholds(limits),
        "mutes": [
            {"id": m.id, "pattern": m.pattern, "created_at": _iso(m.created_at)}
            for m in mutes_for(db, owner)
        ],
        "content_hidden": bool(content_hidden),
    }


# ── The team view ────────────────────────────────────────────────────────────


def _who(item: LoopItem) -> str:
    """ "Jane Doe · Acme Corp" — HubSpot names ONLY.

    The contact name is the one the scan read off the HubSpot contact
    (``detail.crm_contact_name``), never ``counterparty_label``: that can be a
    mail-header display name, a raw external address, or a name the promise
    listener extracted from a meeting — mailbox content, which never reaches a
    team surface. The company is HubSpot's name for it."""
    detail = item.detail if isinstance(item.detail, dict) else {}
    contact = str(detail.get("crm_contact_name") or "").strip() or None
    parts = [p for p in (contact, item.company_label) if p]
    return " · ".join(parts) or "No contact named"


def _team_item(item: LoopItem, *, now: datetime, escalation_days: int) -> dict:
    """The ONLY constructor of a team-view item: five whitelisted fields."""
    days = days_open(item, now)
    return {
        "id": item.id,
        "kind": item.kind,
        "who": _who(item),
        "days_open": days,
        "age": age_chip(days, escalation_days),
    }


def _bucket(days: int) -> str:
    if days <= 2:
        return "d0_2"
    if days <= 7:
        return "d3_7"
    if days <= 14:
        return "d8_14"
    return "d15_plus"


def _names(db: Session, emails: list[str]) -> dict[str, str]:
    from rubik_core.database.models import User

    if not emails:
        return {}
    rows = (
        db.query(User.email, User.name).filter(func.lower(User.email).in_(emails)).all()
    )
    return {(e or "").lower(): (n or "").strip() for e, n in rows}


def _median_days(pairs: list[tuple[datetime, datetime]]) -> float | None:
    if not pairs:
        return None
    return round(median((b - a).total_seconds() / 86400 for a, b in pairs), 1)


def health(db: Session, emails: list[str], *, now: datetime) -> dict:
    """The spec's success metric, over the items FLAGGED in the window.

    One cohort, so every share has its denominator: ``dismissed`` and
    ``closed`` are counted out of ``flagged``, and ``noise_rate`` is
    ``dismissed / flagged``. ``aged_out`` is left out of "closed" — an item
    open for 60 days was not fixed — and so is ``rescheduled``, which appears
    in ``closed_by_reason`` (the page marks it "not counted as fixed") but
    never in ``closed`` or the median.
    """
    stamp = _now(now)
    since = stamp - timedelta(days=HEALTH_WINDOW_DAYS)
    rows = (
        db.query(LoopItem)
        .filter(LoopItem.owner_email.in_(emails), LoopItem.first_flagged_at >= since)
        .all()
        if emails
        else []
    )
    dismissed = [r for r in rows if r.status == "dismissed"]
    closed = [
        r
        for r in rows
        if r.status == "closed" and r.close_reason in constants.FIXED_CLOSE_REASONS
    ]
    # Rescheduled items left the list without being done: named in the reason
    # breakdown so the reader sees them, never added to ``closed``.
    rescheduled = [
        r for r in rows if r.status == "closed" and r.close_reason == "rescheduled"
    ]
    weeks: dict[date, list[LoopItem]] = {}
    first_monday = _local_monday(since)
    monday = first_monday
    while monday <= _local_monday(stamp):
        weeks[monday] = []
        monday += timedelta(days=7)
    for r in rows:
        weeks.setdefault(_local_monday(r.first_flagged_at), []).append(r)
    weekly = []
    for monday in sorted(weeks):
        cohort = weeks[monday]
        done = [
            r
            for r in cohort
            if r.status == "closed" and r.close_reason in constants.FIXED_CLOSE_REASONS
        ]
        weekly.append(
            {
                "week_start": monday.isoformat(),
                "flagged": len(cohort),
                "closed": len(done),
                "median_days_to_close": _median_days(
                    [(r.first_flagged_at, r.closed_at) for r in done if r.closed_at]
                ),
            }
        )
    return {
        "window_days": HEALTH_WINDOW_DAYS,
        "flagged": len(rows),
        "dismissed": len(dismissed),
        "noise_rate": round(len(dismissed) / len(rows), 3) if rows else None,
        "dismissed_by_reason": dict(Counter(r.dismiss_reason for r in dismissed)),
        "closed": len(closed),
        "closed_by_reason": dict(
            Counter(r.close_reason for r in [*closed, *rescheduled])
        ),
        "median_days_to_close": _median_days(
            [(r.first_flagged_at, r.closed_at) for r in closed if r.closed_at]
        ),
        "weekly": weekly,
    }


def team_view(
    db: Session, *, now: datetime | None = None, emails: list[str] | None = None
) -> dict:
    """Every rostered AE, A–Z, with their open items oldest first — metadata
    only. ``emails`` overrides the roster (tests, a single-team read)."""
    from rubik_core.loop_closure.roster import roster_emails

    stamp = _now(now)
    # A page read never asks Slack / Google who has left: that lookup is the
    # morning scan's (it decides whose mailbox is READ). Here it would put two
    # directory sweeps in front of every Team-tab load and team chat answer.
    roster = sorted(
        {
            (e or "").strip().lower()
            for e in (
                emails
                if emails is not None
                else roster_emails(db, deactivated_lookup=_no_directory_lookup)
            )
        }
        - {""}
    )
    limits = constants.thresholds()
    esc = limits["escalation_days"]
    names = _names(db, roster)
    runs = _recent_runs(db)
    run = runs[0] if runs else None
    monday = week_start(stamp)

    open_rows = [
        r
        for r in (
            db.query(LoopItem)
            .filter(LoopItem.owner_email.in_(roster), LoopItem.status == "open")
            .order_by(LoopItem.opened_at.asc(), LoopItem.id.asc())
            .all()
            if roster
            else []
        )
        if listed(r, stamp, limits)
    ]
    closed_counts = dict(
        db.query(LoopItem.owner_email, func.count(LoopItem.id))
        .filter(
            LoopItem.owner_email.in_(roster),
            LoopItem.status == "closed",
            LoopItem.closed_at >= monday,
            LoopItem.close_reason.in_(constants.FIXED_CLOSE_REASONS),
        )
        .group_by(LoopItem.owner_email)
        .all()
        if roster
        else []
    )

    people = []
    for email in roster:
        mine = [r for r in open_rows if r.owner_email == email]
        ages = [days_open(r, stamp) for r in mine]
        aging = {"d0_2": 0, "d3_7": 0, "d8_14": 0, "d15_plus": 0}
        for d in ages:
            aging[_bucket(d)] += 1
        covering = _covering(runs, email)
        coverage = (
            _coverage(covering.coverage.get(email), _TEAM_COVERAGE_KEYS)
            if covering is not None
            else None
        )
        people.append(
            {
                "email": email,
                "name": names.get(email) or email.split("@", 1)[0],
                "open": len(mine),
                "by_kind": {
                    k: sum(1 for r in mine if r.kind == k) for k in constants.KINDS
                },
                "aging": aging,
                "over_escalation": sum(1 for d in ages if d >= esc),
                "closed_this_week": int(closed_counts.get(email, 0)),
                "coverage": coverage,
                "items": [
                    _team_item(r, now=stamp, escalation_days=esc)
                    for r in mine[:MAX_TEAM_ITEMS]
                ],
            }
        )
    people.sort(key=lambda p: (p["name"].lower(), p["email"]))

    return {
        "scanned_at": _iso(run.finished_at or run.started_at) if run else None,
        "people": people,
        "roster_size": len(roster),
        "health": health(db, roster, now=stamp),
        "scorecard": _scorecard_status(db, roster),
        "thresholds": _thresholds(limits),
    }


def _scorecard_status(db: Session, roster: list[str]) -> dict | None:
    """Do not query EOS scorecard tables while the module is off."""
    from rubik_core.control.repository import module_enabled

    try:
        if not module_enabled("eos"):
            return None
    except Exception:  # noqa: BLE001 — control outage closes the EOS read
        logger.exception("EOS module lookup failed for Loose Ends scorecard")
        return None
    from rubik_core.loop_closure import scorecard

    return scorecard.status(db, roster)


def _no_directory_lookup(_emails: Any) -> set[str]:
    """The read path's stand-in for the Slack / Google "has left" lookup."""
    return set()


def unchecked_emails(db: Session, emails: list[str]) -> set[str]:
    """Roster people whose list the LATEST scan to cover them did not check.

    Never covered, their per-AE scan failed (``scan: failed``), or their
    mailbox could not be read (``gmail: unavailable``). For these a count of
    open items — or a zero — is a claim nobody checked, so the digest names
    them apart and the scorecard leaves their cell alone (README §2).
    """
    runs = _recent_runs(db)
    out: set[str] = set()
    for email in {(e or "").strip().lower() for e in emails} - {""}:
        covering = _covering(runs, email)
        coverage = covering.coverage.get(email) if covering is not None else None
        if (
            not isinstance(coverage, dict)
            or coverage.get("scan") == "failed"
            or coverage.get("gmail") == "unavailable"
        ):
            out.add(email)
    return out
