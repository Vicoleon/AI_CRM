"""Overview: is Cubie working for the company?

The tiles are each available dashboard's ``headline(ctx)``, so Overview never
re-derives a number another dashboard owns (and never disagrees with it); each
tile's ``href`` points at the dashboard that explains it. The shared reads sit
on the context, so asking four dashboards for headlines costs one load of the
activity signals, not four.

AI spend (total and per active person) is owned by the AI Spend dashboard
(``ai_spend.headline``), so its tiles open it. Before Insights, most AI spend
had no person on it (the nightly priorities run recorded none; the telemetry
slice of this release stamps it), so a department view can only count the
spend that IS tied to someone in those departments, and reads low for older
periods. The catalog definition says so in words, and the whole-company view
(an all-departments viewer, no filter) counts every dollar.
"""

from __future__ import annotations

from collections import defaultdict

from rubik_core.brand import PRODUCT_NAME
from rubik_core.insights.context import InsightsContext
from rubik_core.insights.dashboards import (
    adoption,
    ai_spend,
    ask_cubie,
    crm,
    follow_through,
    knowledge,
    priorities,
    proposals,
    quality,
)
from rubik_core.insights.payload import (
    cell,
    hidden_cell,
    matrix,
    ratio,
    section,
    series,
    timeseries,
)

SLUG = "overview"
TITLE = "Overview"
HREF = "/insights"

#: Dashboards whose headline tiles Overview shows, in order.
_HEADLINES = (
    adoption,
    ask_cubie,
    priorities,
    follow_through,
    crm,
    proposals,
    knowledge,
    quality,
)

#: Dashboards that add columns to the department comparison
#: (``overview_columns(ctx)`` → ``[(id, label, {dept slug: Measure | None})]``,
#: every value a 0–1 rate with its numerator and denominator). AI Spend adds
#: none: dollars are not a rate, and the matrix is rates only.
_COLUMNS = (follow_through, crm, proposals, knowledge, quality)


def headline(ctx: InsightsContext) -> list[dict]:
    # AI spend and spend per active person belong to AI Spend, which they open.
    return ai_spend.headline(ctx)


def build(ctx: InsightsContext) -> dict:
    kpis: list[dict] = []
    for module in _HEADLINES:
        kpis.extend(module.headline(ctx))
    kpis.extend(headline(ctx))
    sections = [_activity_trend(ctx), _department_matrix(ctx)]
    notes = [adoption.coverage_note(ctx), priorities.coverage_note(ctx)]
    if not ctx.scope.company_wide:
        notes.append(
            "AI spend here counts only what can be tied to a person in the "
            "selected departments. Before Insights launched, most AI spend "
            "(the nightly AI Priorities run included) was not tied to anyone, "
            "so figures for earlier periods read low."
        )
    return ctx.envelope(SLUG, TITLE, kpis, sections, notes)


def _activity_trend(ctx: InsightsContext) -> dict:
    qs = ask_cubie.questions(ctx)
    dau, questions = [], []
    for b in ctx.period.buckets:
        days = b.window.dates()
        dau.append(sum(len(ctx.active_on(d)) for d in days) / len(days))
        questions.append(sum(1 for r in qs if b.window.contains(r.day)))
    weekly = ctx.period.bucket == "week"
    return section(
        "activity_trend",
        "timeseries",
        "Daily active people and questions asked",
        timeseries(
            [b.key for b in ctx.period.buckets],
            [
                series(
                    "dau",
                    "Daily active (average)" if weekly else "Daily active",
                    "ratio" if weekly else "count",
                    dau,
                ),
                series("questions", "Questions asked", "count", questions),
            ],
        ),
        width="full",
    )


def _department_matrix(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    by_dept = ctx.dept_people()
    active = ctx.active_in(cur)
    copilot = ctx.population.eligible(None)
    qs = [r for r in ask_cubie.questions(ctx) if cur.contains(r.day)]
    asked = {r.email for r in qs}
    q_by: dict[str, list] = defaultdict(list)
    for r in qs:
        dept = ctx.population.dept_of(r.email)
        if dept:
            q_by[dept].append(r)
    p_by: dict[str, list] = defaultdict(list)
    for it in priorities.items(ctx):
        if cur.contains(it.first_day):
            dept = ctx.population.dept_of(it.email)
            if dept:
                p_by[dept].append(it)

    cols = [
        ("active_rate", "Active (of people)"),
        ("people_asking_rate", f"Asking {PRODUCT_NAME} (of people with the copilot)"),
        ("chat_success_rate", "Answers that succeeded (of questions)"),
        ("priorities_acted_rate", "Priorities acted on (of surfaced)"),
    ]
    extra: list[dict] = []
    for module in _COLUMNS:
        for key, label, values in module.overview_columns(ctx):
            cols.append((key, label))
            extra.append(values)
    rows = ctx.departments()
    cells = []
    for slug, _label in rows:
        members = set(by_dept[slug])
        can = members & copilot
        dq = q_by.get(slug, [])
        dp = p_by.get(slug, [])
        if ctx.hide_small(len(members)):
            # Every column of a tiny department is effectively one person's.
            cells.append([hidden_cell() for _ in cols])
            continue
        cells.append(
            [
                cell(ratio(len(members & active), len(members)), "pct"),
                cell(ratio(len(can & asked), len(can)) if can else None, "pct"),
                cell(
                    ratio(sum(1 for r in dq if r.successful), len(dq)) if dq else None,
                    "pct",
                ),
                cell(
                    ratio(sum(1 for r in dp if r.outcome == priorities.ACTED), len(dp))
                    if dp
                    else None,
                    "pct",
                ),
                *[cell(values.get(slug), "pct") for values in extra],
            ]
        )
    return section(
        "department_comparison",
        "matrix",
        "Departments side by side",
        matrix(rows, cols, cells, unit="pct"),
        width="full",
        drill="department",
        subtitle="Click a department to filter every number to it. Blank: nothing to measure yet.",
        empty_hint="No one in the selected departments yet.",
    )
