"""AI Spend: what Cubie's AI costs, and what it costs per person and per result.

One source, ``llm_usage_events``: a row per AI call with its ``feature``,
``model``, ``cost_usd`` (priced when the call was made) and, for work done on
someone's behalf, ``user_email``. Nothing here reads a row per call: every
number is a SQL ``GROUP BY`` (feature, model or person) with one
``SUM(CASE …)`` per window — the period, the one before it and each chart
bucket — so a year-to-date view is still a handful of grouped rows.

Two views of the same table, decided by :class:`SpendView`:

* **The whole company** (an all-departments viewer with no department
  filter): every dollar, tied to a person or not. Most background spend
  (ingestion, overnight jobs) carries no person.
* **A department view, or one person's page**: only spend tied to a person in
  view. The nightly AI Priorities run only started carrying a person with the
  Insights release, so those figures are partial before it; the KPIs drop
  their previous value and sparkline points from before that day
  (``kpi_over(..., tracked_from=)``) and a note says from when.

Features whose use is only ever reported for the whole company
(:data:`PRIVATE_FEATURE_PREFIXES`) are never named and never split: in the
company view their dollars sit in the total and in the generic "Other
features" line; in a department or person view they are left out entirely,
so no filter can isolate them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import and_, case, func, or_

from rubik_core.insights.context import InsightsContext, fmt_day
from rubik_core.insights.filters import Window, local_day
from rubik_core.insights.payload import (
    Measure,
    bar,
    bar_item,
    hidden_bar_item,
    kpi_over,
    ratio,
    section,
    series,
    table,
    timeseries,
)
from rubik_core.insights.population import fold

SLUG = "ai-spend"
TITLE = "AI Spend"
HREF = "/insights/ai-spend"

#: Feature keys whose spend is reported for the whole company only, folded
#: into "Other features" and never shown by name, department or person.
PRIVATE_FEATURE_PREFIXES = ("smart_teams", "st_")
OTHER_KEY = "other"
OTHER_LABEL = "Other features"
#: Named features in the by-feature bar and the stacked trend; the rest fold
#: into "Other features".
BAR_FEATURES = 10
TREND_FEATURES = 5

PRIORITIES_FEATURE = "ai_priorities"
CHAT_FEATURE = "chat"
PROPOSAL_FEATURE = "proposal_generator"
UNATTRIBUTED = "unattributed"


def is_private_feature(feature: str | None) -> bool:
    key = (feature or "").strip().lower()
    return any(key.startswith(p) for p in PRIVATE_FEATURE_PREFIXES)


def private_feature_clause(col):
    """SQL: ``col`` is a feature reported for the whole company only."""
    target = func.lower(col)
    return or_(
        *(
            target.like(p.replace("_", "\\_") + "%", escape="\\")
            for p in PRIVATE_FEATURE_PREFIXES
        )
    )


def feature_label(key: str) -> str:
    """The Admin → AI Cost label for a feature key (imported, not copied)."""
    if key == OTHER_KEY:
        return OTHER_LABEL
    try:
        from backend.routers.admin import _FEATURE_LABELS
    except Exception:  # noqa: BLE001 — a label must never fail a dashboard
        _FEATURE_LABELS = {}
    return _FEATURE_LABELS.get(key) or key.replace("_", " ").title()


# ── data ────────────────────────────────────────────────────────────────────


def _windows(ctx: InsightsContext) -> list[Window]:
    """The period, the previous period and every bucket, without repeats."""
    p = ctx.period
    out: list[Window] = []
    for w in (p.current, p.previous, *[b.window for b in p.buckets]):
        if w not in out:
            out.append(w)
    return out


def _bounds(windows: list[Window]):
    bounds = [w.utc_bounds() for w in windows]
    return min(b[0] for b in bounds), max(b[1] for b in bounds)


def _window_sums(ts, value, windows: list[Window], extra=None) -> list:
    cols = []
    for w in windows:
        lo, hi = w.utc_bounds()
        cond = and_(ts >= lo, ts < hi)
        if extra is not None:
            cond = and_(cond, extra)
        cols.append(func.sum(case((cond, value), else_=0)))
    return cols


def _company(ctx: InsightsContext) -> dict:
    """Every dollar, by feature, per window, plus the part tied to a person.
    One grouped query; only read for the whole-company view."""

    def load():
        from rubik_core.database.models import LLMUsageEvent as E

        wins = _windows(ctx)
        n = len(wins)
        lo, hi = _bounds(wins)
        tied = and_(E.user_email.isnot(None), E.user_email != "")
        cols = _window_sums(E.timestamp, E.cost_usd, wins) + _window_sums(
            E.timestamp, E.cost_usd, wins, tied
        )
        by_feature: dict[str, list[float]] = {}
        tied_total = [0.0] * n
        q = (
            ctx.db.query(E.feature, *cols)
            .filter(E.timestamp >= lo, E.timestamp < hi)
            .group_by(E.feature)
        )
        for feature, *vals in q:
            row = by_feature.setdefault(feature or UNATTRIBUTED, [0.0] * n)
            for i in range(n):
                row[i] += float(vals[i] or 0.0)
                tied_total[i] += float(vals[n + i] or 0.0)
        return {"by_feature": by_feature, "tied": tied_total}

    return ctx.memo("spend.company", load)


def _attributed(ctx: InsightsContext) -> dict:
    """Spend tied to someone in view, by person and feature, per window, plus
    each person's call count in the period. Never a whole-company-only
    feature."""

    def load():
        from rubik_core.database.models import LLMUsageEvent as E

        wins = _windows(ctx)
        n = len(wins)
        lo, hi = _bounds(wins)
        cur_lo, cur_hi = ctx.period.current.utc_bounds()
        calls = func.sum(
            case((and_(E.timestamp >= cur_lo, E.timestamp < cur_hi), 1), else_=0)
        )
        q = (
            ctx.db.query(
                E.user_email,
                E.feature,
                calls,
                *_window_sums(E.timestamp, E.cost_usd, wins),
            )
            .filter(
                E.timestamp >= lo,
                E.timestamp < hi,
                ctx.roster_filter(E.user_email),
                ~private_feature_clause(E.feature),
            )
            .group_by(E.user_email, E.feature)
        )
        spend: dict[str, dict[str, list[float]]] = defaultdict(dict)
        n_calls: dict[str, int] = defaultdict(int)
        for email, feature, c, *vals in q:
            key = fold(email)
            if not ctx.population.in_scope(key):
                continue
            row = spend[key].setdefault(feature or UNATTRIBUTED, [0.0] * n)
            for i in range(n):
                row[i] += float(vals[i] or 0.0)
            n_calls[key] += int(c or 0)
        return {"spend": dict(spend), "calls": dict(n_calls)}

    return ctx.memo("spend.attributed", load)


def _by_model(ctx: InsightsContext, company: bool) -> dict[str, float]:
    def load():
        from rubik_core.database.models import LLMUsageEvent as E

        lo, hi = ctx.period.current.utc_bounds()
        conds = [E.timestamp >= lo, E.timestamp < hi]
        if not company:
            conds += [
                ctx.roster_filter(E.user_email),
                ~private_feature_clause(E.feature),
            ]
        out: dict[str, float] = defaultdict(float)
        for model, cost in (
            ctx.db.query(E.model, func.sum(E.cost_usd)).filter(*conds).group_by(E.model)
        ):
            out[(model or "").strip() or "Not recorded"] += float(cost or 0.0)
        return dict(out)

    return ctx.memo(f"spend.models.{company}", load)


def attribution_from(ctx: InsightsContext) -> date | None:
    """The first local day the nightly AI Priorities run recorded whose
    briefing it was: from then on, spend tied to a person is complete."""

    def load():
        from rubik_core.database.models import LLMUsageEvent as E

        first = (
            ctx.db.query(func.min(E.timestamp))
            .filter(
                E.feature == PRIORITIES_FEATURE,
                E.user_email.isnot(None),
                E.user_email != "",
            )
            .scalar()
        )
        return local_day(first)

    return ctx.memo("spend.attribution_from", load)


def proposals_drafted(ctx: InsightsContext) -> list[tuple[str, date, int]]:
    """``(owner, day of the first document, versions)`` for proposals of people
    in view whose first document falls in the compare window. Grouped in SQL
    per proposal; no brief or document content is read."""

    def load():
        from rubik_core.database.proposals import Proposal, ProposalVersion

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        first = func.min(ProposalVersion.created_at)
        q = (
            ctx.db.query(
                Proposal.owner_email, first, func.max(ProposalVersion.version_no)
            )
            .select_from(ProposalVersion)
            .join(Proposal, Proposal.id == ProposalVersion.proposal_id)
            .filter(ctx.roster_filter(Proposal.owner_email, folded=True))
            .group_by(ProposalVersion.proposal_id, Proposal.owner_email)
            .having(and_(first >= lo, first < hi))
        )
        out = []
        for owner, at, versions in q:
            key = fold(owner)
            day = local_day(at)
            if ctx.population.in_scope(key) and win.contains(day):
                out.append((key, day, int(versions or 1)))
        return out

    return ctx.memo("spend.proposals_drafted", load)


class SpendView:
    """Spend per feature per window, for the whole company or for the people
    in view (see the module docstring)."""

    def __init__(self, ctx: InsightsContext, *, company: bool) -> None:
        self.ctx = ctx
        self.company = company
        self._idx = {w: i for i, w in enumerate(_windows(ctx))}
        if company:
            self._features = _company(ctx)["by_feature"]
        else:
            merged: dict[str, list[float]] = {}
            for per in _attributed(ctx)["spend"].values():
                for feature, vals in per.items():
                    row = merged.setdefault(feature, [0.0] * len(vals))
                    for i, v in enumerate(vals):
                        row[i] += v
            self._features = merged

    def _at(self, vals: list[float], win: Window) -> float:
        i = self._idx.get(win)
        return vals[i] if i is not None else 0.0

    def features(self, win: Window) -> dict[str, float]:
        return {f: self._at(v, win) for f, v in self._features.items()}

    def feature(self, key: str, win: Window) -> float:
        vals = self._features.get(key)
        return self._at(vals, win) if vals else 0.0

    def total(self, win: Window) -> float:
        return sum(self.features(win).values())

    def tied(self, win: Window) -> float:
        return self._at(_company(self.ctx)["tied"], win)

    def tracked_from(self) -> date | None | bool:
        """``kpi_over``'s ``tracked_from``: none for the whole company, the
        attribution start for a view that counts only spend tied to people."""
        return False if self.company else attribution_from(self.ctx)


def _fold_features(
    amounts: dict[str, float], keep: int
) -> tuple[list[str], dict[str, float]]:
    """The ``keep`` largest named features, and the rest (whole-company-only
    features always among them) summed as "Other features"."""
    named = sorted(
        (f for f, v in amounts.items() if not is_private_feature(f) and v > 0),
        key=lambda f: -amounts[f],
    )[:keep]
    other = sum(v for f, v in amounts.items() if f not in named)
    out = {f: amounts[f] for f in named}
    if other > 0:
        out[OTHER_KEY] = other
    return named, out


# ── measures ────────────────────────────────────────────────────────────────


def _total(view: SpendView):
    return lambda win: Measure(view.total(win))


def spend_per_active(ctx: InsightsContext, view: SpendView):
    def fn(win: Window) -> Measure:
        active = len(ctx.active_in(win))
        total = view.total(win)
        return Measure(total / active if active else None, round(total, 2), active)

    return fn


def _tied_share(view: SpendView):
    def fn(win: Window) -> Measure:
        return ratio(round(view.tied(win), 2), round(view.total(win), 2))

    return fn


def _per_unit(view: SpendView, feature: str, count):
    def fn(win: Window) -> Measure:
        n = count(win)
        cost = view.feature(feature, win)
        return Measure(cost / n if n else None, round(cost, 2), n)

    return fn


def _briefings(ctx: InsightsContext):
    from rubik_core.insights.dashboards import priorities

    rows = priorities._briefings(ctx)
    return lambda win: sum(1 for _e, d, _s in rows if win.contains(d))


def _questions(ctx: InsightsContext):
    from rubik_core.insights.dashboards import ask_cubie

    rows = ask_cubie.questions(ctx)
    return lambda win: sum(1 for r in rows if win.contains(r.day))


def _drafted(ctx: InsightsContext):
    rows = proposals_drafted(ctx)
    return lambda win: sum(1 for _e, d, _v in rows if win.contains(d))


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    view = SpendView(ctx, company=ctx.scope.company_wide)
    tf = view.tracked_from()
    p = ctx.period
    return [
        kpi_over("ai_spend_total", _total(view), p, HREF, tracked_from=tf),
        kpi_over(
            "ai_spend_per_active_person",
            spend_per_active(ctx, view),
            p,
            HREF,
            tracked_from=tf,
        ),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    """A person's own spend: only ever what is tied to them."""
    view = SpendView(ctx, company=False)
    p = ctx.period
    return [
        kpi_over(
            "ai_spend_total", _total(view), p, HREF, tracked_from=view.tracked_from()
        ),
        kpi_over(
            "ai_cost_per_answer",
            _per_unit(view, CHAT_FEATURE, _questions(ctx)),
            p,
            HREF,
        ),
    ]


def coverage_note(ctx: InsightsContext) -> str:
    start = attribution_from(ctx)
    when = f"from {fmt_day(start)}" if start else "from the Insights release on"
    return (
        f"Spend tied to a person is complete {when}. Before that the nightly "
        "AI Priorities run was not tied to anyone, so department and person "
        "figures for earlier days read low."
    )


def build(ctx: InsightsContext) -> dict:
    company = ctx.scope.company_wide
    view = SpendView(ctx, company=company)
    tf = view.tracked_from()
    p = ctx.period
    kpis = [
        kpi_over("ai_spend_total", _total(view), p, tracked_from=tf),
        kpi_over(
            "ai_spend_per_active_person",
            spend_per_active(ctx, view),
            p,
            tracked_from=tf,
        ),
    ]
    if company:
        kpis.append(kpi_over("ai_spend_attributed_share", _tied_share(view), p))
    kpis += [
        kpi_over(
            "ai_cost_per_briefing",
            _per_unit(view, PRIORITIES_FEATURE, _briefings(ctx)),
            p,
            tracked_from=tf,
        ),
        kpi_over(
            "ai_cost_per_answer", _per_unit(view, CHAT_FEATURE, _questions(ctx)), p
        ),
        kpi_over(
            "ai_cost_per_proposal",
            _per_unit(view, PROPOSAL_FEATURE, _drafted(ctx)),
            p,
        ),
    ]
    sections = [
        _trend(ctx, view),
        _by_feature(ctx, view),
        _models_section(ctx, company),
        _by_department(ctx),
    ]
    if ctx.can_view_people:
        sections.append(_people(ctx))
    notes = []
    if company:
        notes.append(
            "Every dollar of AI spend counts here, including background work no "
            "one asked for at that moment. 'Other features' groups the smaller "
            "features with those reported for the whole company only."
        )
    else:
        notes.append(
            "Only AI spend tied to a person in the selected departments counts "
            "here; background work tied to no one is in the whole-company view."
        )
    notes.append(coverage_note(ctx))
    notes.append(
        "Admins can see the same spend call by call, with the price list, in "
        "Admin → AI Cost."
    )
    return ctx.envelope(SLUG, TITLE, kpis, sections, notes)


# ── sections ────────────────────────────────────────────────────────────────


def _trend(ctx: InsightsContext, view: SpendView) -> dict:
    cur = view.features(ctx.period.current)
    named, _ = _fold_features(cur, TREND_FEATURES)
    buckets = ctx.period.buckets
    per_bucket = [view.features(b.window) for b in buckets]
    series_list = [
        series(
            f, feature_label(f), "usd", [amounts.get(f, 0.0) for amounts in per_bucket]
        )
        for f in named
    ]
    other = [
        sum(v for f, v in amounts.items() if f not in named) for amounts in per_bucket
    ]
    if any(v > 0 for v in other):
        series_list.append(series(OTHER_KEY, OTHER_LABEL, "usd", other))
    return section(
        "spend_trend",
        "timeseries",
        "AI spend over time, by feature",
        timeseries([b.key for b in buckets], series_list, chart="bar", stacked=True),
        width="full",
        subtitle=f"The {TREND_FEATURES} costliest features in the period; the rest are under '{OTHER_LABEL}'.",
        empty_hint="No AI spend in the period.",
    )


def _by_feature(ctx: InsightsContext, view: SpendView) -> dict:
    _named, amounts = _fold_features(view.features(ctx.period.current), BAR_FEATURES)
    items = [bar_item(f, feature_label(f), v, "usd") for f, v in amounts.items()]
    items.sort(key=lambda i: (i["key"] == OTHER_KEY, -(i["value"] or 0)))
    return section(
        "by_feature",
        "bar",
        "AI spend by feature",
        bar(items, "usd", sort=False),
        subtitle="Feature names as in Admin → AI Cost.",
        empty_hint="No AI spend in the period.",
    )


def _models_section(ctx: InsightsContext, company: bool) -> dict:
    items = [
        bar_item(m.lower().replace(" ", "_"), m, v, "usd")
        for m, v in _by_model(ctx, company).items()
        if v > 0
    ]
    return section(
        "by_model",
        "bar",
        "AI spend by model",
        bar(items, "usd"),
        empty_hint="No AI spend in the period.",
    )


def _by_department(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    i = _windows(ctx).index(cur)
    by_dept: dict[str, float] = defaultdict(float)
    for email, per in _attributed(ctx)["spend"].items():
        dept = ctx.population.dept_of(email)
        if dept:
            by_dept[dept] += sum(vals[i] for vals in per.values())
    items = []
    for slug, label in ctx.departments():
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        items.append(bar_item(slug, label, by_dept.get(slug, 0.0), "usd"))
    start = attribution_from(ctx)
    when = f"from {fmt_day(start)}" if start else "from the Insights release on"
    return section(
        "by_department",
        "bar",
        "AI spend by department",
        bar(items, "usd"),
        drill="department",
        subtitle=(
            "Only spend tied to a person, by that person's department; complete "
            f"{when}. Background work tied to no one is not in any department."
        ),
        empty_hint="No one in the selected departments yet.",
    )


def _people(ctx: InsightsContext) -> dict:
    wins = _windows(ctx)
    i_cur = wins.index(ctx.period.current)
    i_prev = wins.index(ctx.period.previous)
    data = _attributed(ctx)
    rows = []
    for email, person in ctx.population.people.items():
        per = data["spend"].get(email, {})
        cur = {f: vals[i_cur] for f, vals in per.items()}
        top = max(cur, key=lambda f: cur[f], default=None)
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "spend": sum(cur.values()),
                    "prev_spend": sum(vals[i_prev] for vals in per.values()),
                    "calls": data["calls"].get(email, 0),
                    "top_feature": feature_label(top) if top and cur[top] > 0 else None,
                },
            }
        )
    return section(
        "people",
        "table",
        "AI spend per person",
        table(
            [
                ("name", "Person", "text"),
                ("department", "Department", "text"),
                ("spend", "AI spend", "usd"),
                ("prev_spend", "Previous period", "usd"),
                ("calls", "AI calls", "count"),
                ("top_feature", "Costliest feature", "text"),
            ],
            rows,
            default_sort=("spend", "desc"),
        ),
        width="full",
        drill="person",
        subtitle="Only spend tied to the person; background work tied to no one is not here.",
        empty_hint="No one in the selected departments yet.",
    )
