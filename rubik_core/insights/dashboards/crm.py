"""CRM & HubSpot: what Cubie wrote into HubSpot, and whether people trust it.

Cubie writes to HubSpot along five paths, every one of them only after a person
approves the specific change:

==================  =============================================  ============
path                a HubSpot update is                            attributed to
==================  =============================================  ============
CRM Sync            a suggestion ``applied`` (``applied_at``)       its owner
Call Capture        a wrap-up write HubSpot confirmed               DEPARTMENT
                    (``call_hubspot_writes.succeeded_at``)          only
Cockpit nudges      a nudge sent with its HubSpot task              the sender
Stage corrections   a stage-lag exception ``applied``               the resolver
Loose Ends          a loose end closed ``logged_from_assistant``    its owner
==================  =============================================  ============

Trust is CRM Sync's acceptance rate (approved ÷ decided, for the suggestions
made in the period) and how often a write fails (failed ÷ attempted, for the
paths that record a failure: CRM Sync and Call Capture).

COUNTS ONLY. Never selected: a suggestion's payload, evidence or HubSpot
response, a write's request or response body, a nudge's task text, a stage
lag's evidence, or a loose end's title, headline or counterparty. Each read
projects an email, a timestamp and a status or kind.

**Call Capture is department-level only** (Google Meet's terms): its writes
count in the totals, the trend and the department bar, never in the people
table or a person's card, and a department where fewer than 3 people use call
capture has its call figures left out for everyone
(``rubik_core/insights/sources.py``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from rubik_core.brand import PRODUCT_NAME
from rubik_core.identifiers import LOOP_CLOSE_LOGGED_FROM_ASSISTANT
from rubik_core.insights.context import InsightsContext
from rubik_core.insights.filters import Window, local_day
from rubik_core.insights.payload import (
    Measure,
    bar,
    bar_item,
    funnel,
    hidden_bar_item,
    kpi_over,
    median,
    ratio,
    section,
    series,
    table,
    timeseries,
)
from rubik_core.insights.population import fold
from rubik_core.insights.signals import earliest
from rubik_core.insights.sources import (
    CALL_HIDDEN_NOTE,
    history_note,
    plain_label,
    visible_departments,
)

SLUG = "crm"
TITLE = "CRM & HubSpot"
HREF = "/insights/crm"

CRM_SYNC = "crm_sync"
CALLS = "call_capture"
NUDGES = "nudges"
STAGES = "stage_lags"
LOOSE_ENDS = "loose_ends"

#: Path key → label, in the order the trend stacks them.
PATHS: tuple[tuple[str, str], ...] = (
    (CRM_SYNC, "CRM Sync"),
    (CALLS, "Call Capture wrap-ups"),
    (NUDGES, "Cockpit nudges (HubSpot tasks)"),
    (STAGES, "Deal stages corrected"),
    (LOOSE_ENDS, "Emails logged from Loose Ends"),
)
#: Paths whose failed attempts are recorded (the failure rate's scope).
FAILURE_PATHS = frozenset({CRM_SYNC, CALLS})
#: Paths that may be shown against a person.
PERSON_PATHS = frozenset(k for k, _l in PATHS if k != CALLS)

APPROVED = frozenset({"applied", "applying", "failed"})

_CHANGE_TYPE_LABELS = {
    "create_contact": "New contact",
    "update_contact": "Contact update",
    "create_deal": "New deal",
    "update_deal": "Deal update",
}
_CALL_ITEM_LABELS = {
    "written": "Written to HubSpot",
    "accepted": "Approved, not yet written",
    "write_failed": "Approved, write failed",
    "rejected": "Rejected",
    "superseded": "Replaced by a newer proposal",
    "proposed": "Still waiting",
}


@dataclass(frozen=True)
class Write:
    """One attempted HubSpot change."""

    email: str
    day: date
    path: str
    ok: bool


@dataclass(frozen=True)
class Suggestion:
    email: str
    day: date
    created_at: datetime
    status: str
    change_type: str
    applied_at: datetime | None


# ── data ────────────────────────────────────────────────────────────────────


def _bounds(ctx: InsightsContext):
    return ctx.compare_window.utc_bounds()


def _in_window(col, lo, hi):
    from sqlalchemy import and_

    return and_(col.isnot(None), col >= lo, col < hi)


def suggestions(ctx: InsightsContext) -> list[Suggestion]:
    """CRM Sync suggestions created, applied or last changed in the compare
    window, for the people in view."""

    def load():
        from sqlalchemy import or_

        from rubik_core.database.models import CrmSyncSuggestion as S

        lo, hi = _bounds(ctx)
        out = []
        for email, created, status, change_type, applied_at in ctx.db.query(
            S.user_email, S.created_at, S.status, S.change_type, S.applied_at
        ).filter(
            or_(
                _in_window(S.created_at, lo, hi),
                _in_window(S.applied_at, lo, hi),
                _in_window(S.updated_at, lo, hi),
            ),
            ctx.roster_filter(S.user_email),
        ):
            key = fold(email)
            if not ctx.population.in_scope(key) or created is None:
                continue
            out.append(
                Suggestion(
                    key,
                    local_day(created),
                    created,
                    (status or "").lower(),
                    change_type or "",
                    applied_at,
                )
            )
        return out

    return ctx.memo("crm.suggestions", load)


def _crm_sync_writes(ctx: InsightsContext) -> list[Write]:
    def load():
        from sqlalchemy import or_

        from rubik_core.database.models import CrmSyncSuggestion as S

        lo, hi = _bounds(ctx)
        out = []
        for email, status, applied_at, updated_at in ctx.db.query(
            S.user_email, S.status, S.applied_at, S.updated_at
        ).filter(
            S.status.in_(("applied", "failed")),
            or_(_in_window(S.applied_at, lo, hi), _in_window(S.updated_at, lo, hi)),
            ctx.roster_filter(S.user_email),
        ):
            key = fold(email)
            at = applied_at if status == "applied" else updated_at
            if at is None or not (lo <= at < hi) or not ctx.population.in_scope(key):
                continue
            out.append(Write(key, local_day(at), CRM_SYNC, status == "applied"))
        return out

    return ctx.memo("crm.sync_writes", load)


def _call_writes(ctx: InsightsContext) -> list[Write]:
    """Call Capture writes, by the AE whose call they came from. Department
    level only: filtered to the visible departments in :func:`writes`."""

    def load():
        from sqlalchemy import or_

        from rubik_core.database.call_capture import CallHubspotWrite as W
        from rubik_core.database.call_capture import CallSession

        lo, hi = _bounds(ctx)
        out = []
        q = (
            ctx.db.query(
                CallSession.ae_email, W.succeeded_at, W.response_status, W.updated_at
            )
            .join(CallSession, CallSession.id == W.call_session_id)
            .filter(
                or_(
                    _in_window(W.succeeded_at, lo, hi), _in_window(W.updated_at, lo, hi)
                ),
                ctx.roster_filter(CallSession.ae_email, folded=True),
            )
        )
        for email, ok_at, status, updated_at in q:
            key = fold(email)
            if not ctx.population.in_scope(key):
                continue
            if ok_at is not None:
                if lo <= ok_at < hi:
                    out.append(Write(key, local_day(ok_at), CALLS, True))
            elif status is not None and updated_at is not None:
                out.append(Write(key, local_day(updated_at), CALLS, False))
        return out

    return ctx.memo("crm.call_writes", load)


def _simple_writes(ctx: InsightsContext) -> list[Write]:
    """Nudges, stage corrections and Loose Ends emails."""

    def load():
        from rubik_core.database.commitments import (
            CommitmentNudge,
            CommitmentStageLag,
        )
        from rubik_core.database.loop_closure import LoopItem

        lo, hi = _bounds(ctx)
        out: list[Write] = []

        def add(email, at, path, ok=True):
            key = fold(email)
            if at is not None and lo <= at < hi and ctx.population.in_scope(key):
                out.append(Write(key, local_day(at), path, ok))

        for email, at in ctx.db.query(
            CommitmentNudge.from_email, CommitmentNudge.sent_at
        ).filter(
            CommitmentNudge.hubspot_task_id.isnot(None),
            _in_window(CommitmentNudge.sent_at, lo, hi),
            ctx.roster_filter(CommitmentNudge.from_email, folded=True),
        ):
            add(email, at, NUDGES)
        for email, at in ctx.db.query(
            CommitmentStageLag.resolved_by, CommitmentStageLag.resolved_at
        ).filter(
            CommitmentStageLag.status == "applied",
            _in_window(CommitmentStageLag.resolved_at, lo, hi),
            ctx.roster_filter(CommitmentStageLag.resolved_by, folded=True),
        ):
            add(email, at, STAGES)
        for email, at in ctx.db.query(LoopItem.owner_email, LoopItem.closed_at).filter(
            LoopItem.close_reason == LOOP_CLOSE_LOGGED_FROM_ASSISTANT,
            _in_window(LoopItem.closed_at, lo, hi),
            ctx.roster_filter(LoopItem.owner_email, folded=True),
        ):
            add(email, at, LOOSE_ENDS)
        return out

    return ctx.memo("crm.simple_writes", load)


def call_visible(ctx: InsightsContext) -> frozenset[str]:
    """Departments whose Call Capture figures this view may show."""

    def build():
        contributors = {w.email for w in _call_writes(ctx)}
        contributors |= {r[0] for r in _call_items(ctx)}
        return visible_departments(ctx, contributors)

    return ctx.memo("crm.call_visible", build)


def _calls_hidden(ctx: InsightsContext) -> bool:
    """Did any department's Call Capture rows get left out?"""
    vis = call_visible(ctx)
    rows = [w.email for w in _call_writes(ctx)] + [r[0] for r in _call_items(ctx)]
    return any(ctx.population.dept_of(e) not in vis for e in rows)


def writes(ctx: InsightsContext, *, include_calls: bool = True) -> list[Write]:
    """Every attempted write in the compare window. ``include_calls`` keeps
    the Call Capture rows of the visible departments; a person's view passes
    False and never sees one."""

    def build():
        out = list(_crm_sync_writes(ctx)) + list(_simple_writes(ctx))
        if include_calls:
            vis = call_visible(ctx)
            out += [
                w for w in _call_writes(ctx) if ctx.population.dept_of(w.email) in vis
            ]
        return out

    return ctx.memo(f"crm.writes.{include_calls}", build)


def _call_items(ctx: InsightsContext) -> list[tuple[str, date, str]]:
    """``(ae email, day proposed, status)`` for wrap-up proposals made in the
    compare window. Department level only."""

    def load():
        from rubik_core.database.call_capture import CallExtractionItem as I
        from rubik_core.database.call_capture import CallSession

        lo, hi = _bounds(ctx)
        out = []
        for email, at, status in (
            ctx.db.query(CallSession.ae_email, I.created_at, I.status)
            .join(CallSession, CallSession.id == I.call_session_id)
            .filter(
                _in_window(I.created_at, lo, hi),
                ctx.roster_filter(CallSession.ae_email, folded=True),
            )
        ):
            key = fold(email)
            if ctx.population.in_scope(key):
                out.append((key, local_day(at), (status or "").lower()))
        return out

    return ctx.memo("crm.call_items", load)


# ── measures ────────────────────────────────────────────────────────────────


def _cohort(rows: list[Suggestion], win: Window) -> list[Suggestion]:
    return [r for r in rows if win.contains(r.day)]


def _updates(ctx, *, include_calls: bool = True):
    rows = writes(ctx, include_calls=include_calls)
    return lambda win: Measure(sum(1 for w in rows if w.ok and win.contains(w.day)))


def _suggested(ctx):
    rows = suggestions(ctx)
    return lambda win: Measure(len(_cohort(rows, win)))


def acceptance(rows: list[Suggestion]) -> Measure:
    approved = sum(1 for r in rows if r.status in APPROVED)
    dismissed = sum(1 for r in rows if r.status == "dismissed")
    return ratio(approved, approved + dismissed)


def _acceptance(ctx):
    rows = suggestions(ctx)
    return lambda win: acceptance(_cohort(rows, win))


def _time_to_apply(ctx):
    rows = suggestions(ctx)

    def fn(win):
        spans = [
            max(0.0, (r.applied_at - r.created_at).total_seconds() / 86400.0)
            for r in _cohort(rows, win)
            if r.status == "applied" and r.applied_at is not None
        ]
        return Measure(median(spans))

    return fn


def _failure_rate(ctx, *, include_calls: bool = True):
    rows = [
        w for w in writes(ctx, include_calls=include_calls) if w.path in FAILURE_PATHS
    ]

    def fn(win):
        rs = [w for w in rows if win.contains(w.day)]
        return ratio(sum(1 for w in rs if not w.ok), len(rs))

    return fn


def _logged(ctx):
    rows = writes(ctx, include_calls=False)
    return lambda win: Measure(
        sum(1 for w in rows if w.path == LOOSE_ENDS and w.ok and win.contains(w.day))
    )


def _loose_ends_start(ctx: InsightsContext) -> date | None:
    def load():
        from rubik_core.database.loop_closure import LoopItem

        return earliest(ctx.db, LoopItem.first_flagged_at)

    return ctx.memo("loose_ends.first", load)


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("crm_hubspot_updates", _updates(ctx), p, HREF),
        kpi_over("crm_sync_acceptance_rate", _acceptance(ctx), p, HREF),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    """A person's card. Call Capture is never in it (README §1)."""
    p = ctx.period
    return [
        kpi_over("crm_hubspot_updates", _updates(ctx, include_calls=False), p, HREF),
        kpi_over("crm_sync_suggestions", _suggested(ctx), p, HREF),
        kpi_over("crm_sync_acceptance_rate", _acceptance(ctx), p, HREF),
        kpi_over(
            "loose_ends_emails_logged",
            _logged(ctx),
            p,
            HREF,
            tracked_from=_loose_ends_start(ctx),
        ),
    ]


def overview_columns(ctx: InsightsContext) -> list[tuple[str, str, dict]]:
    """Overview's department comparison: CRM Sync acceptance per department."""
    by: dict[str, list[Suggestion]] = defaultdict(list)
    for r in _cohort(suggestions(ctx), ctx.period.current):
        dept = ctx.population.dept_of(r.email)
        if dept:
            by[dept].append(r)
    cells = {}
    for slug, rows in by.items():
        m = acceptance(rows)
        cells[slug] = m if m.value is not None else None
    return [("crm_sync_acceptance_rate", "CRM Sync approved (of decided)", cells)]


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    kpis = [
        kpi_over("crm_hubspot_updates", _updates(ctx), p),
        kpi_over("crm_sync_suggestions", _suggested(ctx), p),
        kpi_over("crm_sync_acceptance_rate", _acceptance(ctx), p),
        kpi_over("crm_sync_time_to_apply_days", _time_to_apply(ctx), p),
        kpi_over("crm_write_failure_rate", _failure_rate(ctx), p),
        kpi_over(
            "loose_ends_emails_logged",
            _logged(ctx),
            p,
            tracked_from=_loose_ends_start(ctx),
        ),
    ]
    cur = _cohort(suggestions(ctx), p.current)
    sections = [
        _trend(ctx),
        _outcomes(cur),
        _by_change_type(cur),
        _by_department(ctx),
        _call_outcomes(ctx),
    ]
    if ctx.can_view_people:
        sections.append(_people(ctx, cur))
    return ctx.envelope(SLUG, TITLE, kpis, sections, _notes(ctx))


def _notes(ctx: InsightsContext) -> list[str]:
    from rubik_core.database.models import CrmSyncSuggestion

    notes = []
    for what, first in (
        ("CRM Sync", earliest(ctx.db, CrmSyncSuggestion.created_at)),
        ("Loose Ends", _loose_ends_start(ctx)),
    ):
        note = history_note(ctx, what, first)
        if note:
            notes.append(note)
    if _calls_hidden(ctx):
        notes.append(CALL_HIDDEN_NOTE)
    return notes


# ── sections ────────────────────────────────────────────────────────────────


def _trend(ctx: InsightsContext) -> dict:
    rows = [w for w in writes(ctx) if w.ok]
    show_calls = bool(call_visible(ctx) & set(ctx.dept_people()))
    out = []
    for key, label in PATHS:
        if key == CALLS and not show_calls:
            continue
        out.append(
            series(
                key,
                label,
                "count",
                [
                    sum(1 for w in rows if w.path == key and b.window.contains(w.day))
                    for b in ctx.period.buckets
                ],
            )
        )
    return section(
        "updates_trend",
        "timeseries",
        f"HubSpot updates by {PRODUCT_NAME}, by kind of change",
        timeseries([b.key for b in ctx.period.buckets], out, chart="bar", stacked=True),
        width="full",
        subtitle="Only changes a person approved and HubSpot confirmed.",
        empty_hint=f"{PRODUCT_NAME} made no HubSpot changes in the period.",
    )


def _outcomes(cur: list[Suggestion]) -> dict:
    c = Counter(r.status for r in cur)
    known = ("applied", "dismissed", "expired", "superseded", "failed")
    waiting = len(cur) - sum(c.get(k, 0) for k in known)
    return section(
        "crm_sync_outcomes",
        "funnel",
        "What happened to CRM Sync suggestions",
        funnel(
            [
                ("suggested", "Suggested", len(cur)),
                ("applied", "Approved and written to HubSpot", c.get("applied", 0)),
                ("dismissed", "Dismissed", c.get("dismissed", 0)),
                ("expired", "Expired unanswered", c.get("expired", 0)),
                (
                    "superseded",
                    "Replaced by a newer suggestion",
                    c.get("superseded", 0),
                ),
                ("failed", "Approved, but the write failed", c.get("failed", 0)),
                ("pending", "Still waiting", waiting),
            ],
            mode="breakdown",
        ),
        subtitle="Suggestions made in the period, and where each one stands now.",
        empty_hint="CRM Sync made no suggestions in the period.",
    )


def _by_change_type(cur: list[Suggestion]) -> dict:
    by: dict[str, list[Suggestion]] = defaultdict(list)
    for r in cur:
        by[r.change_type or "unknown"].append(r)
    items = []
    for kind, rows in by.items():
        m = acceptance(rows)
        if m.value is None:
            continue
        items.append(
            bar_item(
                kind,
                plain_label(kind if kind != "unknown" else None, _CHANGE_TYPE_LABELS),
                m.value,
                "pct",
                m.numerator,
                m.denominator,
            )
        )
    return section(
        "acceptance_by_change_type",
        "bar",
        "Approved, by kind of change",
        bar(items, "pct"),
        subtitle="Suggestions approved, out of those approved or dismissed.",
        empty_hint="No CRM Sync suggestion was decided in the period.",
    )


def _by_department(ctx: InsightsContext) -> dict:
    counts: Counter[str] = Counter()
    for w in writes(ctx):
        if w.ok and ctx.period.current.contains(w.day):
            dept = ctx.population.dept_of(w.email)
            if dept:
                counts[dept] += 1
    items = []
    for slug, label in ctx.departments():
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        items.append(bar_item(slug, label, counts.get(slug, 0), "count"))
    return section(
        "updates_by_department",
        "bar",
        "HubSpot updates by department",
        bar(items, "count"),
        drill="department",
        empty_hint="No one in the selected departments yet.",
    )


def _call_outcomes(ctx: InsightsContext) -> dict:
    vis = call_visible(ctx)
    cur = ctx.period.current
    c: Counter[str] = Counter(
        status
        for email, day, status in _call_items(ctx)
        if cur.contains(day) and ctx.population.dept_of(email) in vis
    )
    order = (
        "written",
        "accepted",
        "write_failed",
        "rejected",
        "superseded",
        "proposed",
    )
    items = [
        bar_item(k, _CALL_ITEM_LABELS[k], c.get(k, 0), "count")
        for k in order
        if c.get(k, 0)
    ]
    return section(
        "call_capture_outcomes",
        "bar",
        "Call Capture wrap-up suggestions",
        bar(items, "count", sort=False),
        subtitle=(
            f"What happened to the HubSpot changes {PRODUCT_NAME} proposed after recorded "
            "calls. Department totals only: never shown per person."
        ),
        empty_hint="No call wrap-up suggestions to show for the selected departments.",
    )


def _people(ctx: InsightsContext, cur: list[Suggestion]) -> dict:
    by: dict[str, list[Suggestion]] = defaultdict(list)
    for r in cur:
        by[r.email].append(r)
    path_counts: dict[str, Counter[str]] = defaultdict(Counter)
    total: Counter[str] = Counter()
    for w in writes(ctx, include_calls=False):
        if w.ok and ctx.period.current.contains(w.day):
            path_counts[w.email][w.path] += 1
            total[w.email] += 1
    rows = []
    for email, person in ctx.population.people.items():
        rs = by.get(email, [])
        m = acceptance(rs)
        pc = path_counts.get(email, Counter())
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "updates": total.get(email, 0),
                    "suggested": len(rs),
                    "approved": m.numerator or 0,
                    "dismissed": sum(1 for r in rs if r.status == "dismissed"),
                    "acceptance": m.value,
                    "nudges": pc.get(NUDGES, 0),
                    "stage_lags": pc.get(STAGES, 0),
                    "loose_ends": pc.get(LOOSE_ENDS, 0),
                },
            }
        )
    return section(
        "people",
        "table",
        "People",
        table(
            [
                ("name", "Person", "text"),
                ("department", "Department", "text"),
                ("updates", "HubSpot updates", "count"),
                ("suggested", "CRM Sync suggestions", "count"),
                ("approved", "Approved", "count"),
                ("dismissed", "Dismissed", "count"),
                ("acceptance", "Approved (of decided)", "pct"),
                ("nudges", "Nudges sent", "count"),
                ("stage_lags", "Deal stages corrected", "count"),
                ("loose_ends", "Emails logged from Loose Ends", "count"),
            ],
            rows,
            default_sort=("updates", "desc"),
        ),
        width="full",
        drill="person",
        subtitle=(
            "Call Capture is not in this table: recorded calls are counted per "
            "department only."
        ),
        empty_hint="No one in the selected departments yet.",
    )
