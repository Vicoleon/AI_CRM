"""Follow-through: whether loose ends and promises are closing.

Three sources:

* **Loose Ends** (``loop_items``) — the AE loop-closure ledger. Its success
  metric is the noise rate, "Not a real miss" ÷ flagged, over the items
  FLAGGED in the period (one cohort, exactly as the feature's own health view
  computes it, ``rubik_core/loop_closure/views.py::health``). "Closed" counts
  only the close reasons that mean the work was done
  (``constants.FIXED_CLOSE_REASONS``): ``aged_out`` and ``rescheduled`` left
  the list without being done. Read: the owner, kind, source, status, the
  timestamps and the close / dismiss reason codes. NEVER the title, the AI
  headline, the counterparty, the company, the link or the evidence: those
  are the owner's alone (KB decision 0024), and this dashboard is a team view.
* **My To-Dos** (``promises`` + ``promise_events``) — kept rate is
  kept ÷ (kept + dropped) by when each was kept or dropped. Titles and
  details are never selected.
* **Sales Cockpit commitments** (``commitments``) — missed ÷ due. DEPARTMENT
  LEVEL ONLY: call wrap-up commitments come from recorded calls (Google Meet's
  terms), so this never reaches a people table or a person's card, and a
  department where fewer than 3 people use call capture is left out for
  everyone.

No leaderboard (KB 0024): the people table is A–Z by name. Its columns can
still be sorted by whoever reads it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from rubik_core.brand import PRODUCT_NAME
from rubik_core.identifiers import LOOP_CLOSE_LOGGED_FROM_ASSISTANT
from rubik_core.insights.context import InsightsContext
from rubik_core.insights.filters import Window, local_day, local_midnight_utc
from rubik_core.insights.payload import (
    Measure,
    bar,
    bar_item,
    funnel,
    hidden_bar_item,
    kpi,
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

SLUG = "follow-through"
TITLE = "Follow-through"
HREF = "/insights/follow-through"

#: Open-item age buckets, in days since the email, message or due date.
AGE_BUCKETS: tuple[tuple[str, str, int, int | None], ...] = (
    ("d0_2", "0–2 days", 0, 2),
    ("d3_6", "3–6 days", 3, 6),
    ("d7_13", "7–13 days", 7, 13),
    ("d14_plus", "14 days or more", 14, None),
)

_PROMISE_SOURCE_LABELS = {
    "heard_transcript": "Heard on a call",
    "heard_email": "Heard in an email",
    "heard_slack": "Heard in Slack",
    "priority_card": "From an AI Priorities card",
    "chat": f"Ask {PRODUCT_NAME}",
    "slack_cmd": "Slack /todo",
    "page": "Typed on the My To-Dos page",
    "handed_off": "Handed over by a colleague",
}
_COMMITMENT_KIND_LABELS = {
    "sow": "Statement of work owed",
    "wrapup": "Call wrap-up",
    "lead_touch": "First touch on a new lead",
}
_CLOSE_REASON_LABELS = {
    "logged_in_hubspot": "Logged in HubSpot",
    LOOP_CLOSE_LOGGED_FROM_ASSISTANT: f"Logged from {PRODUCT_NAME}",
    "replied": "Replied",
    "logged_follow_up": "Follow-up logged",
    "task_completed": "HubSpot task completed",
    "task_gone": "HubSpot task removed",
    "promise_kept": "Promise kept",
    "promise_withdrawn": "Promise withdrawn",
    "rescheduled": "Rescheduled (not counted as fixed)",
    "aged_out": "Aged out (not counted as fixed)",
}


def _fixed_reasons() -> frozenset[str]:
    from rubik_core.loop_closure.constants import FIXED_CLOSE_REASONS

    return frozenset(FIXED_CLOSE_REASONS)


@dataclass(frozen=True)
class Loop:
    email: str
    kind: str
    status: str
    opened_at: datetime | None
    flagged_at: datetime
    day: date  # local day first flagged — the cohort key
    closed_at: datetime | None
    close_reason: str | None
    dismissed_at: datetime | None
    dismiss_reason: str | None
    fixed: bool

    def open_at(self, instant: datetime) -> bool:
        if self.flagged_at >= instant:
            return False
        if self.status == "open":
            return True
        ended = self.closed_at if self.status == "closed" else self.dismissed_at
        return ended is not None and ended >= instant


@dataclass(frozen=True)
class PromiseRow:
    id: str
    email: str
    status: str
    source: str
    created_day: date | None
    kept_day: date | None
    dropped_day: date | None


# ── data ────────────────────────────────────────────────────────────────────


def loops(ctx: InsightsContext) -> list[Loop]:
    def load():
        from sqlalchemy import or_

        from rubik_core.database.loop_closure import LoopItem as L

        lo, _hi = ctx.compare_window.utc_bounds()
        fixed = _fixed_reasons()
        out = []
        for row in ctx.db.query(
            L.owner_email,
            L.kind,
            L.status,
            L.opened_at,
            L.first_flagged_at,
            L.closed_at,
            L.close_reason,
            L.dismissed_at,
            L.dismiss_reason,
        ).filter(
            or_(
                L.status == "open",
                L.first_flagged_at >= lo,
                L.closed_at >= lo,
                L.dismissed_at >= lo,
            ),
            ctx.roster_filter(L.owner_email, folded=True),
        ):
            email, kind, status, opened, flagged, closed, reason, dismissed, why = row
            key = fold(email)
            if not ctx.population.in_scope(key) or flagged is None:
                continue
            status = (status or "").lower()
            out.append(
                Loop(
                    key,
                    kind or "",
                    status,
                    opened,
                    flagged,
                    local_day(flagged),
                    closed,
                    reason,
                    dismissed,
                    why,
                    status == "closed" and reason in fixed,
                )
            )
        return out

    return ctx.memo("follow.loops", load)


def promises(ctx: InsightsContext) -> list[PromiseRow]:
    def load():
        from sqlalchemy import or_

        from rubik_core.database.promises import Promise as P

        lo, _hi = ctx.compare_window.utc_bounds()
        out = []
        for pid, email, status, source, created, kept, dropped in ctx.db.query(
            P.id,
            P.owner_email,
            P.status,
            P.source,
            P.created_at,
            P.kept_at,
            P.dropped_at,
        ).filter(
            or_(
                P.status == "slipping",
                P.created_at >= lo,
                P.kept_at >= lo,
                P.dropped_at >= lo,
            ),
            ctx.roster_filter(P.owner_email, folded=True),
        ):
            key = fold(email)
            if not ctx.population.in_scope(key):
                continue
            out.append(
                PromiseRow(
                    pid,
                    key,
                    (status or "").lower(),
                    source or "",
                    local_day(created),
                    local_day(kept),
                    local_day(dropped),
                )
            )
        return out

    return ctx.memo("follow.promises", load)


def _promoted(ctx: InsightsContext) -> list[tuple[str, str, date]]:
    """``(owner, promise id, day)`` for each promise made an EOS To-Do."""

    def load():
        from rubik_core.database.promises import Promise, PromiseEvent

        lo, hi = ctx.compare_window.utc_bounds()
        out = []
        for email, pid, at in (
            ctx.db.query(Promise.owner_email, PromiseEvent.promise_id, PromiseEvent.at)
            .join(Promise, Promise.id == PromiseEvent.promise_id)
            .filter(
                PromiseEvent.kind == "promoted_eos",
                PromiseEvent.at >= lo,
                PromiseEvent.at < hi,
                ctx.roster_filter(Promise.owner_email, folded=True),
            )
        ):
            key = fold(email)
            if ctx.population.in_scope(key) and at is not None:
                out.append((key, pid, local_day(at)))
        return out

    return ctx.memo("follow.promoted", load)


def _commitments(ctx: InsightsContext) -> list[tuple[str, str, date, bool]]:
    """``(owner, kind, day due, missed)`` for Cockpit commitments that came
    due (or were missed) in the compare window. Department level only."""

    def load():
        from sqlalchemy import and_, or_

        from rubik_core.database.commitments import Commitment as C

        lo, hi = ctx.compare_window.utc_bounds()
        now = ctx.now
        out = []
        for email, kind, breach_at, breached_at in ctx.db.query(
            C.owner_email, C.kind, C.breach_at, C.breached_at
        ).filter(
            or_(
                and_(C.breach_at >= lo, C.breach_at < hi),
                and_(C.breached_at >= lo, C.breached_at < hi),
            ),
            ctx.roster_filter(C.owner_email, folded=True),
        ):
            key = fold(email)
            if not ctx.population.in_scope(key):
                continue
            if breached_at is not None:
                due = breached_at
            elif breach_at is not None and breach_at <= now:
                due = breach_at
            else:
                continue  # its deadline has not come yet
            out.append((key, kind or "", local_day(due), breached_at is not None))
        return out

    return ctx.memo("follow.commitments", load)


def commitments_visible(ctx: InsightsContext) -> frozenset[str]:
    return ctx.memo(
        "follow.commitments_visible",
        lambda: visible_departments(ctx, {r[0] for r in _commitments(ctx)}),
    )


def _visible_commitments(ctx: InsightsContext):
    vis = commitments_visible(ctx)
    return [r for r in _commitments(ctx) if ctx.population.dept_of(r[0]) in vis]


def _loose_ends_start(ctx: InsightsContext) -> date | None:
    def load():
        from rubik_core.database.loop_closure import LoopItem

        return earliest(ctx.db, LoopItem.first_flagged_at)

    return ctx.memo("loose_ends.first", load)


def _promises_start(ctx: InsightsContext) -> date | None:
    def load():
        from rubik_core.database.promises import Promise

        return earliest(ctx.db, Promise.created_at)

    return ctx.memo("promises.first", load)


# ── measures ────────────────────────────────────────────────────────────────


def _cohort(rows: list[Loop], win: Window) -> list[Loop]:
    return [r for r in rows if win.contains(r.day)]


def _flagged(ctx):
    rows = loops(ctx)
    return lambda win: Measure(len(_cohort(rows, win)))


def noise_rate(rows: list[Loop]) -> Measure:
    return ratio(sum(1 for r in rows if r.status == "dismissed"), len(rows))


def _noise(ctx):
    rows = loops(ctx)
    return lambda win: noise_rate(_cohort(rows, win))


def _closed_rate(ctx):
    rows = loops(ctx)

    def fn(win):
        rs = _cohort(rows, win)
        return ratio(sum(1 for r in rs if r.fixed), len(rs))

    return fn


def _close_days(rows: list[Loop]) -> float | None:
    return median(
        [
            max(0.0, (r.closed_at - r.flagged_at).total_seconds() / 86400.0)
            for r in rows
            if r.fixed and r.closed_at is not None
        ]
    )


def _days_to_close(ctx):
    rows = loops(ctx)
    return lambda win: Measure(_close_days(_cohort(rows, win)))


def _open(ctx):
    rows = loops(ctx)

    def fn(win):
        end = local_midnight_utc(win.end + timedelta(days=1))
        return Measure(sum(1 for r in rows if r.open_at(end)))

    return fn


def _kept_rate(ctx):
    rows = promises(ctx)

    def fn(win):
        kept = sum(1 for r in rows if r.status == "kept" and win.contains(r.kept_day))
        dropped = sum(
            1 for r in rows if r.status == "dropped" and win.contains(r.dropped_day)
        )
        return ratio(kept, kept + dropped)

    return fn


def _promoted_count(ctx):
    rows = _promoted(ctx)
    return lambda win: Measure(len({pid for _e, pid, d in rows if win.contains(d)}))


def _slipping_now(ctx) -> Measure:
    return Measure(
        sum(
            1
            for r in promises(ctx)
            if r.status == "slipping" and r.email in ctx.population.people
        )
    )


def breach_rate(rows) -> Measure:
    return ratio(sum(1 for r in rows if r[3]), len(rows))


def _breach(ctx):
    rows = _visible_commitments(ctx)
    return lambda win: breach_rate([r for r in rows if win.contains(r[2])])


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    start = _loose_ends_start(ctx)
    return [
        kpi_over("loose_ends_noise_rate", _noise(ctx), p, HREF, tracked_from=start),
        kpi_over("promises_kept_rate", _kept_rate(ctx), p, HREF),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    """A person's card. Cockpit commitments are never in it (README §1)."""
    p = ctx.period
    start = _loose_ends_start(ctx)
    return [
        kpi_over("loose_ends_flagged", _flagged(ctx), p, HREF, tracked_from=start),
        kpi_over(
            "loose_ends_closed_rate", _closed_rate(ctx), p, HREF, tracked_from=start
        ),
        kpi_over("loose_ends_noise_rate", _noise(ctx), p, HREF, tracked_from=start),
        kpi_over("promises_kept_rate", _kept_rate(ctx), p, HREF),
    ]


def overview_columns(ctx: InsightsContext) -> list[tuple[str, str, dict]]:
    """Overview's department comparison: the Loose Ends noise rate."""
    by: dict[str, list[Loop]] = defaultdict(list)
    for r in _cohort(loops(ctx), ctx.period.current):
        dept = ctx.population.dept_of(r.email)
        if dept:
            by[dept].append(r)
    return [
        (
            "loose_ends_noise_rate",
            "Loose Ends marked not a real miss (of flagged)",
            {slug: noise_rate(rows) for slug, rows in by.items()},
        )
    ]


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    start = _loose_ends_start(ctx)
    kpis = [
        kpi_over("loose_ends_flagged", _flagged(ctx), p, tracked_from=start),
        kpi_over("loose_ends_noise_rate", _noise(ctx), p, tracked_from=start),
        kpi_over("loose_ends_closed_rate", _closed_rate(ctx), p, tracked_from=start),
        kpi_over(
            "loose_ends_days_to_close", _days_to_close(ctx), p, tracked_from=start
        ),
        kpi_over("loose_ends_open", _open(ctx), p, tracked_from=start),
        kpi_over("promises_kept_rate", _kept_rate(ctx), p),
        # A snapshot: "now" has no previous period and no trend.
        kpi("promises_slipping_now", _slipping_now(ctx)),
        kpi_over("promises_promoted_eos", _promoted_count(ctx), p),
    ]
    commitments_hidden = _commitments_hidden(ctx)
    if not (commitments_hidden and not _visible_commitments(ctx)):
        kpis.append(kpi_over("cockpit_breach_rate", _breach(ctx), p))
    cur = _cohort(loops(ctx), p.current)
    sections = [
        _trend(ctx),
        _outcomes(cur),
        _closed_by_reason(ctx),
        _aging(ctx),
        _noise_by_department(ctx, cur),
        _by_kind(cur),
        _dismiss_reasons(cur),
        _promise_sources(ctx),
        _commitments_by_department(ctx),
        _commitments_by_kind(ctx),
    ]
    if ctx.can_view_people:
        sections.append(_people(ctx, cur))
    notes = []
    for what, first in (
        ("Loose Ends", start),
        ("My To-Dos", _promises_start(ctx)),
    ):
        note = history_note(ctx, what, first)
        if note:
            notes.append(note)
    notes.append(
        "Sales Cockpit commitments are shown per department only, never per "
        "person: call wrap-up commitments come from recorded calls."
    )
    if commitments_hidden:
        notes.append(CALL_HIDDEN_NOTE)
    return ctx.envelope(SLUG, TITLE, kpis, sections, notes)


def _commitments_hidden(ctx: InsightsContext) -> bool:
    vis = commitments_visible(ctx)
    return any(ctx.population.dept_of(r[0]) not in vis for r in _commitments(ctx))


# ── sections ────────────────────────────────────────────────────────────────


def _trend(ctx: InsightsContext) -> dict:
    rows = loops(ctx)
    prom = promises(ctx)
    buckets = ctx.period.buckets
    flagged, closed, dismissed, kept, dropped = [], [], [], [], []
    for b in buckets:
        w = b.window
        flagged.append(sum(1 for r in rows if w.contains(r.day)))
        closed.append(
            sum(
                1
                for r in rows
                if r.fixed and r.closed_at and w.contains(local_day(r.closed_at))
            )
        )
        dismissed.append(
            sum(
                1
                for r in rows
                if r.status == "dismissed"
                and r.dismissed_at
                and w.contains(local_day(r.dismissed_at))
            )
        )
        kept.append(
            sum(1 for r in prom if r.status == "kept" and w.contains(r.kept_day))
        )
        dropped.append(
            sum(1 for r in prom if r.status == "dropped" and w.contains(r.dropped_day))
        )
    return section(
        "follow_through_trend",
        "timeseries",
        "Loose ends and promises over time",
        timeseries(
            [b.key for b in buckets],
            [
                series("flagged", "Loose ends flagged", "count", flagged),
                series("closed", "Loose ends closed", "count", closed),
                series("dismissed", "Marked not a real miss", "count", dismissed),
                series("kept", "Promises kept", "count", kept),
                series("dropped", "Promises dropped", "count", dropped),
            ],
            chart="bar",
        ),
        width="full",
        subtitle="Each on the day it happened.",
        empty_hint="No loose ends or promises in the period.",
    )


def _outcomes(cur: list[Loop]) -> dict:
    fixed = sum(1 for r in cur if r.fixed)
    unfixed = sum(1 for r in cur if r.status == "closed" and not r.fixed)
    dismissed = sum(1 for r in cur if r.status == "dismissed")
    return section(
        "loose_ends_outcomes",
        "funnel",
        "What happened to the loose ends flagged",
        funnel(
            [
                ("flagged", "Flagged", len(cur)),
                ("closed", "Closed: the record shows it done", fixed),
                ("unfixed", "Left the list without being done", unfixed),
                ("dismissed", "Marked not a real miss", dismissed),
                ("open", "Still open", len(cur) - fixed - unfixed - dismissed),
            ],
            mode="breakdown",
        ),
        subtitle=(
            "Loose ends flagged in the period, and where each stands now. 'Left "
            "the list' is an item that aged out or was rescheduled."
        ),
        empty_hint="No loose ends were flagged in the period.",
    )


def _closed_by_reason(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    c = Counter(
        r.close_reason or "unknown"
        for r in loops(ctx)
        if r.status == "closed" and r.closed_at and cur.contains(local_day(r.closed_at))
    )
    items = [
        bar_item(
            k,
            plain_label(k if k != "unknown" else None, _CLOSE_REASON_LABELS),
            n,
            "count",
        )
        for k, n in c.items()
    ]
    return section(
        "closed_by_reason",
        "bar",
        "How loose ends closed",
        bar(items, "count"),
        subtitle="Loose ends that closed in the period, by what the record showed.",
        empty_hint="No loose ends closed in the period.",
    )


def _aging(ctx: InsightsContext) -> dict:
    today = ctx.period.current.end
    c: Counter[str] = Counter()
    for r in loops(ctx):
        if r.status != "open" or r.email not in ctx.population.people:
            continue
        opened = local_day(r.opened_at or r.flagged_at)
        age = max(0, (today - opened).days) if opened else 0
        for key, _label, lo, hi in AGE_BUCKETS:
            if age >= lo and (hi is None or age <= hi):
                c[key] += 1
                break
    items = [bar_item(k, lbl, c.get(k, 0), "count") for k, lbl, _lo, _hi in AGE_BUCKETS]
    return section(
        "open_aging",
        "bar",
        "Open loose ends by age",
        bar(items, "count", sort=False),
        width="third",
        subtitle="Open now, by days since the email, message or due date.",
        empty_hint="No loose ends are open.",
    )


def _noise_by_department(ctx: InsightsContext, cur: list[Loop]) -> dict:
    by: dict[str, list[Loop]] = defaultdict(list)
    for r in cur:
        dept = ctx.population.dept_of(r.email)
        if dept:
            by[dept].append(r)
    items = []
    for slug, label in ctx.departments():
        rows = by.get(slug)
        if not rows:
            continue
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        m = noise_rate(rows)
        items.append(bar_item(slug, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "noise_by_department",
        "bar",
        "Marked 'not a real miss', by department",
        bar(items, "pct"),
        drill="department",
        subtitle="Out of the loose ends flagged for each department. Lower is better.",
        empty_hint="No loose ends were flagged in the period.",
    )


def _by_kind(cur: list[Loop]) -> dict:
    from rubik_core.loop_closure.constants import KIND_LABELS

    c = Counter(r.kind for r in cur)
    items = [bar_item(k, plain_label(k, KIND_LABELS), n, "count") for k, n in c.items()]
    return section(
        "flagged_by_kind",
        "bar",
        "Loose ends flagged, by kind",
        bar(items, "count"),
        width="third",
        empty_hint="No loose ends were flagged in the period.",
    )


def _dismiss_reasons(cur: list[Loop]) -> dict:
    from rubik_core.loop_closure.constants import DISMISS_REASON_LABELS

    dismissed = [r for r in cur if r.status == "dismissed"]
    c = Counter(r.dismiss_reason or "other" for r in dismissed)
    total = len(dismissed)
    items = [
        bar_item(k, plain_label(k, DISMISS_REASON_LABELS), n / total, "pct", n, total)
        for k, n in c.items()
    ]
    return section(
        "dismiss_reasons",
        "bar",
        "Why flags were 'not a real miss'",
        bar(items, "pct"),
        width="third",
        subtitle="Out of the flags marked not a real miss in the period.",
        empty_hint="Nothing was marked not a real miss in the period.",
    )


def _promise_sources(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    c = Counter(r.source for r in promises(ctx) if cur.contains(r.created_day))
    items = [
        bar_item(k or "unknown", plain_label(k, _PROMISE_SOURCE_LABELS), n, "count")
        for k, n in c.items()
    ]
    return section(
        "promises_by_source",
        "bar",
        "Where promises came from",
        bar(items, "count"),
        subtitle=f"My To-Dos promises made in the period: heard by {PRODUCT_NAME}, or added by the person.",
        empty_hint="No promises were made in the period.",
    )


def _commitments_by_department(ctx: InsightsContext) -> dict:
    vis = commitments_visible(ctx)
    cur = ctx.period.current
    by: dict[str, list] = defaultdict(list)
    for r in _commitments(ctx):
        if cur.contains(r[2]):
            dept = ctx.population.dept_of(r[0])
            if dept:
                by[dept].append(r)
    items = []
    for slug, label in ctx.departments():
        rows = by.get(slug)
        if not rows:
            continue
        if slug not in vis or ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        m = breach_rate(rows)
        items.append(bar_item(slug, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "commitments_by_department",
        "bar",
        "Sales commitments missed, by department",
        bar(items, "pct"),
        drill="department",
        subtitle="Out of the commitments due in the period. Department totals only.",
        empty_hint="No Sales Cockpit commitments came due in the period.",
    )


def _commitments_by_kind(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    by: dict[str, list] = defaultdict(list)
    for r in _visible_commitments(ctx):
        if cur.contains(r[2]):
            by[r[1]].append(r)
    items = []
    for kind, rows in by.items():
        m = breach_rate(rows)
        items.append(
            bar_item(
                kind or "unknown",
                plain_label(kind, _COMMITMENT_KIND_LABELS),
                m.value,
                "pct",
                m.numerator,
                m.denominator,
            )
        )
    return section(
        "commitments_by_kind",
        "bar",
        "Sales commitments missed, by kind",
        bar(items, "pct"),
        subtitle="Out of the commitments of each kind due in the period.",
        empty_hint="No Sales Cockpit commitments came due in the period.",
    )


def _people(ctx: InsightsContext, cur: list[Loop]) -> dict:
    by: dict[str, list[Loop]] = defaultdict(list)
    for r in cur:
        by[r.email].append(r)
    open_now = Counter(r.email for r in loops(ctx) if r.status == "open")
    period = ctx.period.current
    kept: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    for r in promises(ctx):
        if r.status == "kept" and period.contains(r.kept_day):
            kept[r.email] += 1
        elif r.status == "dropped" and period.contains(r.dropped_day):
            dropped[r.email] += 1
    eligible = ctx.population.eligible("loop_closure") | ctx.population.eligible(
        "todos"
    )
    rows = []
    for email, person in ctx.population.people.items():
        rs = by.get(email, [])
        if not (
            email in eligible
            or rs
            or open_now.get(email)
            or kept.get(email)
            or dropped.get(email)
        ):
            continue
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "open": open_now.get(email, 0),
                    "flagged": len(rs),
                    "closed": sum(1 for r in rs if r.fixed),
                    "dismissed": sum(1 for r in rs if r.status == "dismissed"),
                    "days_to_close": _close_days(rs),
                    "kept": kept.get(email, 0),
                    "dropped": dropped.get(email, 0),
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
                ("open", "Loose ends open now", "count"),
                ("flagged", "Flagged", "count"),
                ("closed", "Closed", "count"),
                ("dismissed", "Not a real miss", "count"),
                ("days_to_close", "Typical days to close", "days"),
                ("kept", "Promises kept", "count"),
                ("dropped", "Promises dropped", "count"),
            ],
            rows,
            default_sort=("name", "asc"),
        ),
        width="full",
        drill="person",
        subtitle=(
            "Listed A–Z, not ranked. Flagged, closed and not-a-real-miss count the "
            "loose ends flagged in the period. Sales commitments are not here: "
            "they are counted per department only."
        ),
        empty_hint="No one in view has Loose Ends or My To-Dos.",
    )
