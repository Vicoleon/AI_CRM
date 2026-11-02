"""Adoption & Engagement: who uses Cubie, how often, which parts, who stopped.

"Active" is the union of recorded uses described in
``rubik_core/insights/signals.py``. Every rate is over the CURRENT people in
the selected departments; a departed person's past use still shows in volumes
on other dashboards but never inflates a rate here.

Feature adoption matrix (department × part of Cubie): a cell is the people who
used that part ÷ the people in that department who CAN use it, judged by
Admin → Feature Access today. A part with no Feature Access key of its own
(Ask Cubie, AI Priorities, Briefing, Meeting Prep, Assets) counts everyone the
Cubie copilot admits; EOS counts everyone. A column nobody in view can reach
is dropped rather than shown empty. Sales Call Capture appears here, at
department level, and in no per-person table.
"""

from __future__ import annotations

from datetime import date, timedelta

from rubik_core.brand import PRODUCT_NAME
from rubik_core.insights.context import InsightsContext, fmt_day
from rubik_core.insights.filters import Window, local_day, to_local, week_start
from rubik_core.insights.payload import (
    Measure,
    bar,
    bar_item,
    cell,
    hidden_bar_item,
    hidden_cell,
    kpi_over,
    matrix,
    ratio,
    section,
    series,
    table,
    timeseries,
)
from rubik_core.insights.signals import PERSON_SURFACES, SURFACE_BY_KEY, SURFACES

SLUG = "adoption"
TITLE = "Adoption & Engagement"
HREF = "/insights/adoption"

DORMANT_DAYS = 14
#: Retention cohorts: at least this many weeks, at most the second number.
COHORT_MIN_WEEKS = 6
COHORT_MAX_WEEKS = 12
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hour_label(h: int) -> str:
    suffix = "a" if h < 12 else "p"
    return f"{(h % 12) or 12}{suffix}"


# ── measures ────────────────────────────────────────────────────────────────


def _active_people(ctx: InsightsContext):
    return lambda win: Measure(len(ctx.active_in(win)))


def _active_rate(ctx: InsightsContext):
    n = ctx.people_count()
    return lambda win: ratio(len(ctx.active_in(win)), n)


def _avg_dau(ctx: InsightsContext):
    def fn(win: Window) -> Measure:
        days = win.dates()
        if not days:
            return Measure(None)
        return Measure(sum(len(ctx.active_on(d)) for d in days) / len(days))

    return fn


def _stickiness(ctx: InsightsContext):
    def fn(win: Window) -> Measure:
        days = win.dates()
        avg = sum(len(ctx.active_on(d)) for d in days) / len(days) if days else 0
        mau = len(ctx.active_in(win.trailing(30)))
        return Measure(avg / mau if mau else None, round(avg, 1), mau)

    return fn


def _first_seen(ctx: InsightsContext) -> dict[str, date]:
    return {
        email: fl[0]
        for email, fl in ctx.first_last().items()
        if email in ctx.population.people
    }


def _new_people(ctx: InsightsContext):
    first = _first_seen(ctx)
    return lambda win: Measure(sum(1 for d in first.values() if win.contains(d)))


def _is_dormant(ctx: InsightsContext, email: str, as_of: date) -> bool:
    last = ctx.last_active_as_of(email, as_of)
    return last is None or (as_of - last).days >= DORMANT_DAYS


def _dormant_people(ctx: InsightsContext):
    def fn(win: Window) -> Measure:
        return Measure(
            sum(1 for e in ctx.population.people if _is_dormant(ctx, e, win.end))
        )

    return fn


def _active_days(ctx: InsightsContext):
    def fn(win: Window) -> Measure:
        return Measure(
            sum(
                1
                for e in ctx.population.people
                for d in ctx.person_days().get(e, [])
                if win.contains(d)
            )
        )

    return fn


def _surfaces_used(ctx: InsightsContext):
    def fn(win: Window) -> Measure:
        used = ctx.surfaces_used(win, person_level=True)
        return Measure(
            sum(len(v) for e, v in used.items() if e in ctx.population.people)
        )

    return fn


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("active_rate", _active_rate(ctx), p, HREF),
        kpi_over("active_people", _active_people(ctx), p, HREF),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    """The person profile's adoption card (a population of one)."""
    p = ctx.period
    return [
        kpi_over("active_days", _active_days(ctx), p, HREF),
        kpi_over("surfaces_used", _surfaces_used(ctx), p, HREF),
    ]


def coverage_note(ctx: InsightsContext) -> str:
    from rubik_core.database.insights import ActivityDaily
    from rubik_core.insights.signals import earliest

    first = earliest(ctx.db, ActivityDaily.day)
    if first is None:
        return (
            f"Which parts of {PRODUCT_NAME} people open is tracked from this release on. "
            "Until then, 'active' comes from sign-ins, questions and actions only."
        )
    return (
        f"Which parts of {PRODUCT_NAME} people open is tracked from {fmt_day(first)}. "
        "Before that, 'active' comes from sign-ins, questions and actions only."
    )


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    kpis = [
        kpi_over("active_people", _active_people(ctx), p),
        kpi_over("active_rate", _active_rate(ctx), p),
        kpi_over("avg_dau", _avg_dau(ctx), p),
        kpi_over("stickiness", _stickiness(ctx), p),
        kpi_over("new_people", _new_people(ctx), p),
        kpi_over("dormant_people", _dormant_people(ctx), p),
    ]
    sections = [
        _trend_section(ctx),
        _by_department_section(ctx),
        _heatmap_section(ctx),
        _feature_matrix_section(ctx),
        _cohort_section(ctx),
    ]
    if ctx.can_view_people:
        sections.append(_people_section(ctx))
        sections.append(_dormant_section(ctx))
    return ctx.envelope(SLUG, TITLE, kpis, sections, [coverage_note(ctx)])


# ── sections ────────────────────────────────────────────────────────────────


def _trend_section(ctx: InsightsContext) -> dict:
    dau, wau, mau = [], [], []
    for b in ctx.period.buckets:
        days = b.window.dates()
        dau.append(sum(len(ctx.active_on(d)) for d in days) / len(days))
        wau.append(len(ctx.active_in(b.window.trailing(7))))
        mau.append(len(ctx.active_in(b.window.trailing(30))))
    weekly = ctx.period.bucket == "week"
    return section(
        "active_trend",
        "timeseries",
        "Active people over time",
        timeseries(
            [b.key for b in ctx.period.buckets],
            [
                series(
                    "dau",
                    "Daily active (average)" if weekly else "Daily active",
                    "ratio" if weekly else "count",
                    dau,
                ),
                series("wau", "Active in the last 7 days", "count", wau),
                series("mau", "Active in the last 30 days", "count", mau),
            ],
        ),
        width="full",
        subtitle="Each point counts people active on that day, in the 7 days or in the 30 days ending then.",
    )


def _by_department_section(ctx: InsightsContext) -> dict:
    active = ctx.active_in(ctx.period.current)
    items = []
    for slug, label in ctx.departments():
        members = ctx.dept_people()[slug]
        if ctx.hide_small(len(members)):
            items.append(hidden_bar_item(slug, label))
            continue
        m = ratio(len(active & set(members)), len(members))
        items.append(bar_item(slug, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "active_by_department",
        "bar",
        "Active people by department",
        bar(items, "pct"),
        drill="department",
        empty_hint="No one in the selected departments yet.",
    )


def _feature_matrix_section(ctx: InsightsContext) -> dict:
    used = ctx.surfaces_used(ctx.period.current)
    by_dept = ctx.dept_people()
    rows = ctx.departments()
    cols = []
    eligible_by_surface = {}
    for s in SURFACES:
        eligible = ctx.population.eligible(s.feature)
        if eligible:
            cols.append((s.key, s.label))
            eligible_by_surface[s.key] = eligible
    cells = []
    for slug, _label in rows:
        members = set(by_dept[slug])
        row = []
        for key, _l in cols:
            can = members & eligible_by_surface[key]
            if not can:
                row.append(None)
                continue
            # A department-only surface (Call Capture) is never shown over
            # fewer than 3 people, to anyone: "1 of 1" would be one person's
            # call use, which Google Meet's terms forbid tracking.
            dept_only = not SURFACE_BY_KEY[key].person_level
            if ctx.hide_small(len(can), always=dept_only):
                row.append(hidden_cell())
                continue
            did = {e for e in can if key in used.get(e, set())}
            row.append(cell(ratio(len(did), len(can)), "pct"))
        cells.append(row)
    return section(
        "feature_adoption",
        "matrix",
        f"Which parts of {PRODUCT_NAME} each department uses",
        matrix(rows, cols, cells, unit="pct"),
        width="full",
        drill="department",
        subtitle=(
            "Each cell: people who used that part in the period, out of the people "
            "in the department who can reach it (Feature Access). Blank: nobody "
            "there can reach it. Figures over fewer than 3 people are hidden "
            "where they could single someone out."
        ),
        empty_hint="No one in the selected departments yet.",
    )


def _cohort_section(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    last_week = week_start(cur.end)
    first_week = week_start(
        min(cur.start, cur.end - timedelta(days=7 * COHORT_MIN_WEEKS - 1))
    )
    first_week = max(first_week, last_week - timedelta(days=7 * (COHORT_MAX_WEEKS - 1)))
    n_weeks = (last_week - first_week).days // 7 + 1
    first_seen = _first_seen(ctx)
    cohorts: dict[date, set[str]] = {}
    for email, d in first_seen.items():
        w = week_start(d)
        if first_week <= w <= last_week:
            cohorts.setdefault(w, set()).add(email)
    rows, cells = [], []
    for i in range(n_weeks):
        w = first_week + timedelta(days=7 * i)
        members = cohorts.get(w)
        if not members:
            continue
        if ctx.hide_small(len(members)):
            rows.append((w.isoformat(), f"Week of {w:%b} {w.day}"))
            cells.append(
                [
                    hidden_cell() if w + timedelta(days=7 * k) <= last_week else None
                    for k in range(n_weeks)
                ]
            )
            continue
        rows.append((w.isoformat(), f"Week of {w:%b} {w.day} ({len(members)})"))
        row = []
        for k in range(n_weeks):
            wk = w + timedelta(days=7 * k)
            if wk > last_week:
                row.append(None)
                continue
            win = Window(wk, min(wk + timedelta(days=6), cur.end))
            active = ctx.active_in(win) & members
            row.append(cell(ratio(len(active), len(members)), "pct"))
        cells.append(row)
    cols = [(str(k), "First week" if k == 0 else f"+{k} wk") for k in range(n_weeks)]
    return section(
        "retention_cohorts",
        "matrix",
        "Do new users keep coming back?",
        matrix(rows, cols, cells, unit="pct"),
        # Full width: a column per week since first use, which in half a row
        # scrolled sideways next to empty space.
        width="full",
        subtitle=(
            f"People grouped by the week they first used {PRODUCT_NAME}. Each cell: the share "
            "of that group active N weeks later."
        ),
        empty_hint=f"Nobody in view started using {PRODUCT_NAME} in these weeks.",
    )


def _heatmap_section(ctx: InsightsContext) -> dict:
    counts = [[0] * 24 for _ in range(7)]
    cur = ctx.period.current
    people = set(ctx.population.people)
    for s in ctx.signals():
        if s.source not in ("login", "chat") or s.at is None or s.email not in people:
            continue
        if not cur.contains(local_day(s.at)):
            continue
        local = to_local(s.at)
        counts[local.weekday()][local.hour] += 1
    rows = [(d.lower(), d) for d in _WEEKDAYS]
    cols = [(str(h), _hour_label(h)) for h in range(24)]
    cells = [[{"value": v} for v in row] for row in counts]
    return section(
        "weekday_hour",
        "matrix",
        f"When people use {PRODUCT_NAME}",
        matrix(rows, cols, cells, unit="count"),
        subtitle=(
            f"Sign-ins and Ask {PRODUCT_NAME} questions by weekday and hour, in the company's "
            "business time zone."
        ),
        empty_hint="No sign-ins or questions in the period.",
    )


def _last_active(ctx: InsightsContext, email: str) -> date | None:
    return ctx.last_active_as_of(email, ctx.period.current.end)


def _people_section(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    questions = ctx.signal_counts(cur, {"chat"})
    actions = ctx.signal_counts(cur, {"priority_memory", "priority_events"})
    used = ctx.surfaces_used(cur, person_level=True)
    rows = []
    for email, person in ctx.population.people.items():
        days = [d for d in ctx.person_days().get(email, []) if cur.contains(d)]
        last = _last_active(ctx, email)
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "last_active": last.isoformat() if last else None,
                    "active_days": len(days),
                    "questions": questions.get(email, 0),
                    "priority_actions": actions.get(email, 0),
                    "surfaces_used": len(used.get(email, set()) & PERSON_SURFACES),
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
                ("last_active", "Last active", "date"),
                ("active_days", "Active days", "count"),
                ("questions", "Questions asked", "count"),
                ("priority_actions", "Priority actions", "count"),
                ("surfaces_used", f"Parts of {PRODUCT_NAME} used", "count"),
            ],
            rows,
            default_sort=("active_days", "desc"),
        ),
        width="full",
        drill="person",
        empty_hint="No one in the selected departments yet.",
    )


def _dormant_section(ctx: InsightsContext) -> dict:
    end = ctx.period.current.end
    rows = []
    for email, person in ctx.population.people.items():
        if not _is_dormant(ctx, email, end):
            continue
        last = _last_active(ctx, email)
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "last_active": last.isoformat() if last else None,
                    "days_inactive": (end - last).days if last else None,
                },
            }
        )
    return section(
        "dormant",
        "table",
        f"Not active for {DORMANT_DAYS} days or more",
        table(
            [
                ("name", "Person", "text"),
                ("department", "Department", "text"),
                ("last_active", "Last active", "date"),
                ("days_inactive", "Days since last use", "days"),
            ],
            rows,
            default_sort=("days_inactive", "desc"),
        ),
        width="full",
        drill="person",
        subtitle="Blank 'last active' means no recorded use at all.",
        empty_hint=f"Everyone in view used {PRODUCT_NAME} in the last two weeks.",
    )
