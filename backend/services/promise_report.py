"""Chat answer for "who did my team promise something to, and is it done?".

Reads the promises ledger (``rubik_core/promises``) — the promises Cubie heard
people make in meetings, L10s, sent email and Slack, plus the To-Dos they
typed — for the asker, or for the people they manage, and renders a report
grouped by CONTACT: who promised what, when and where, by when, and whether
there is any sign it happened.

Who may be read is the org graph, never a roster in code: ``email_scope``
(own + direct reports + teams they lead; the org-wide Exec sees everyone).
A Cubie admin is an operator, not an executive, and is NOT widened here — the
same rule ``EmailScope`` documents for transcript content, since every line of
this report is a quote from somebody's meeting or mail.

No model call: the ledger already holds the judgement (the nightly evidence
check), so this is a deterministic read the chat model relays as-is.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from rubik_core.brand import PRODUCT_NAME

logger = logging.getLogger(__name__)

MAX_LINES = 60


@dataclass
class ReportLine:
    owner_email: str
    owner_name: str
    contact: str
    contact_email: str | None
    title: str
    quote: str | None
    said_on: date | None
    where: str | None
    due: date | None
    state: str  # no_sign | not_checked | due_later | looks_done
    checked: list[str]
    link: str | None


def _said_at(row: Any) -> datetime | None:
    raw = (row.source_ref or {}).get("at")
    try:
        dt = datetime.fromisoformat(raw) if raw else row.created_at
    except (TypeError, ValueError):
        dt = row.created_at
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify(row: Any, *, today: date, now: datetime) -> str:
    """Where one promise stands, in the words the report uses."""
    from rubik_core.promises import evidence

    ev = row.evidence or {}
    if row.status == "evidence_found" or ev.get("verdict") == "kept":
        return "looks_done"
    when = _said_at(row)
    moment_passed = (
        row.due_local < today
        if row.due_local is not None
        else when is not None
        and (now - when) >= timedelta(days=evidence.UNDATED_GRACE_DAYS)
    )
    if not moment_passed:
        return "due_later"
    if ev.get("verdict") in ("not_found", "unclear") and ev.get("checked"):
        return "no_sign"
    return "not_checked"


def in_window(row: Any, *, week_start: date, week_end: date, since: datetime) -> bool:
    """Said in the window, or due this week (a promise made last month for
    this Friday is this week's business)."""
    when = _said_at(row)
    if when is not None and when >= since:
        return True
    return row.due_local is not None and week_start <= row.due_local <= week_end


def resolve_owners(
    db: Any, user: Any, *, whose: str, team: str
) -> tuple[list[str], str | None]:
    """The owner emails this asker may read, and a refusal when there are none.

    ``whose="me"`` → just the asker. ``whose="team"`` → everyone in their
    scope (minus themselves unless no one else is), optionally narrowed to one
    department by RAG slug.
    """
    from sqlalchemy import func

    from rubik_core.database.models import User

    me = (user.email or "").strip().lower()
    if whose != "team":
        return [me], None

    from backend.routers.eos._authz import email_scope, team_scope_problem

    org_wide = bool(getattr(user, "sees_all_departments", False))
    if org_wide:
        q = db.query(User.email, User.department).filter(
            User.is_platform_support.is_(False)
        )
        rows = [(e.lower(), d) for e, d in q.all() if e]
    else:
        scope = email_scope(db, user)
        problem = team_scope_problem(scope)
        if problem:
            return [], (
                f"{PRODUCT_NAME} can't tell who is on your team: your seat on the "
                "Accountability Chart isn't set up, so there is no team to read. "
                "An admin can fix this in EOS → Accountability Chart."
            )
        emails = sorted(
            {e.strip().lower() for e in scope.owner_emails if e and e.strip()}
        )
        registered = [
            ((e or "").lower(), d, is_support)
            for e, d, is_support in db.query(
                User.email, User.department, User.is_platform_support
            )
            .filter(func.lower(User.email).in_(emails))
            .all()
        ]
        rows = [(e, d) for e, d, is_support in registered if not is_support]
        # People in scope with no users row still own ledger rows.
        # A support row is known, but must not be re-added as a missing user.
        known = {e for e, _, _ in registered}
        rows += [(e, None) for e in emails if e not in known]

    if team.strip():
        from rubik_core.rag.config import slugify_department

        want = slugify_department(team)
        rows = [(e, d) for e, d in rows if d and slugify_department(d) == want]
        if not rows:
            return [], (
                f"No one you manage is in the {team.strip()} team, so there is "
                f"nothing of theirs {PRODUCT_NAME} can show you."
            )
    owners = sorted({e for e, _ in rows if e and e != me}) or [me]
    return owners, None


def build_report(
    db: Any,
    user: Any,
    *,
    whose: str = "team",
    team: str = "",
    days: int = 7,
    include_done: bool = False,
    now: datetime | None = None,
) -> dict:
    from sqlalchemy import func

    from rubik_core.database.models import User
    from rubik_core.database.promises import Promise, PromiseScan
    from rubik_core.promises import store

    now = now or datetime.now(timezone.utc)
    days = max(1, min(int(days or 7), 30))
    owners, refusal = resolve_owners(db, user, whose=whose, team=team)
    if refusal:
        return {"status": "error", "report": refusal}

    tz = store.owner_timezone(db, user.email)
    today = store.local_today(tz, now)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    since = now - timedelta(days=days)

    names = {
        (e or "").lower(): (n or e)
        for e, n in db.query(User.email, User.name)
        .filter(func.lower(User.email).in_(owners))
        .all()
    }
    rows = (
        db.query(Promise)
        .filter(
            Promise.owner_email.in_(owners),
            Promise.status.in_(("suggested", "open", "slipping", "evidence_found")),
            Promise.is_private.is_(False)
            | Promise.owner_email.in_([user.email.lower()]),
        )
        .all()
    )
    lines: list[ReportLine] = []
    for r in rows:
        if not in_window(r, week_start=week_start, week_end=week_end, since=since):
            continue
        if r.source.startswith("heard_") and (r.confidence or 0) < 0.75:
            continue
        state = classify(r, today=today, now=now)
        if state == "looks_done" and not include_done:
            continue
        ref = r.source_ref or {}
        when = _said_at(r)
        lines.append(
            ReportLine(
                owner_email=r.owner_email,
                owner_name=names.get(r.owner_email, r.owner_email),
                contact=r.counterparty_label or "(no one named)",
                contact_email=r.counterparty_id
                if r.counterparty_id and "@" in r.counterparty_id
                else None,
                title=r.title,
                quote=ref.get("quote"),
                said_on=when.date() if when else None,
                where=ref.get("where"),
                due=r.due_local,
                state=state,
                checked=list((r.evidence or {}).get("checked") or []),
                link=ref.get("link"),
            )
        )

    scanned = (
        db.query(func.count(PromiseScan.id))
        .filter(PromiseScan.owner_email.in_(owners))
        .scalar()
        or 0
    )
    return {
        "status": "success",
        "report": render(
            lines,
            people=len(owners),
            whose=whose,
            team=team,
            days=days,
            week_start=week_start,
            week_end=week_end,
            never_read=scanned == 0,
        ),
        "promise_count": len(lines),
        "people": len(owners),
    }


_STATE_TEXT = {
    "no_sign": "no sign it happened yet",
    "not_checked": "not checked yet",
    "due_later": "not due yet",
    "looks_done": "looks done (waiting for them to confirm)",
}
_STATE_ORDER = {"no_sign": 0, "not_checked": 1, "due_later": 2, "looks_done": 3}


def _fmt(d: date | None) -> str:
    return d.strftime("%b %-d") if d else "?"


def render(
    lines: list[ReportLine],
    *,
    people: int,
    whose: str,
    team: str,
    days: int,
    week_start: date,
    week_end: date,
    never_read: bool,
) -> str:
    who = (
        (f"the {team.strip()} team" if team.strip() else "your team")
        if whose == "team"
        else "you"
    )
    window = (
        f"promises made in the last {days} days or due this week "
        f"({_fmt(week_start)}–{_fmt(week_end)})"
    )
    footer = (
        f"\n\n_Read from {people} {'person' if people == 1 else 'people'}'s "
        "meetings, L10 notes, sent email and Slack (overnight — anything said "
        f'today appears tomorrow). "No sign" means {PRODUCT_NAME} found nothing in their '
        "sent email, Slack, Drive or EOS To-Dos; calls and other tools are "
        "invisible to it, so it is a prompt to ask, not proof._"
    )
    if never_read:
        return (
            f"{PRODUCT_NAME} hasn't read {who if whose == 'team' else 'your'} "
            "conversations for promises yet. The first overnight read covers "
            "the last two weeks, so this question will have an answer tomorrow "
            "morning." + footer
        )
    if not lines:
        return f"Nothing outstanding: no open {window} for {who}." + footer

    by_contact: dict[str, list[ReportLine]] = defaultdict(list)
    for ln in lines:
        by_contact[ln.contact_email or ln.contact.lower()].append(ln)

    def contact_rank(item):
        _, group = item
        return (
            min(_STATE_ORDER[g.state] for g in group),
            min((g.said_on or date.max) for g in group),
        )

    from rubik_core.company.domain import workspace_domains

    internal = workspace_domains()
    external = sum(
        1 for key in by_contact if "@" in key and key.rsplit("@", 1)[-1] not in internal
    )
    no_sign = sum(1 for ln in lines if ln.state == "no_sign")
    out = [
        f"**{len(lines)} open promise{'s' if len(lines) != 1 else ''} to "
        f"{len(by_contact)} contact{'s' if len(by_contact) != 1 else ''}** "
        f"({external} external) — {window}. "
        f"{no_sign} of {len(lines)} show no sign of being done yet.\n"
    ]
    shown = 0
    for _key, group in sorted(by_contact.items(), key=contact_rank):
        if shown >= MAX_LINES:
            break
        head = group[0]
        label = head.contact + (
            f" <{head.contact_email}>" if head.contact_email else ""
        )
        out.append(f"\n**{label}**")
        for ln in sorted(
            group, key=lambda g: (_STATE_ORDER[g.state], g.said_on or date.max)
        ):
            if shown >= MAX_LINES:
                break
            shown += 1
            bits = [f"{ln.owner_name}: {ln.title}"]
            if ln.quote:
                bits.append(f"“{ln.quote[:160]}”")
            meta = f"said {_fmt(ln.said_on)}"
            if ln.where:
                meta += f" in {ln.where}"
            if ln.due:
                meta += f", due {_fmt(ln.due)}"
            bits.append(meta)
            status = _STATE_TEXT[ln.state]
            if ln.state == "no_sign" and ln.checked:
                status += f" (checked {', '.join(ln.checked)})"
            bits.append(f"**{status}**")
            link = f" [open]({ln.link})" if ln.link else ""
            out.append("- " + " · ".join(bits) + link)
    if shown < len(lines):
        out.append(f"\n…and {len(lines) - shown} more.")
    return "\n".join(out) + footer
