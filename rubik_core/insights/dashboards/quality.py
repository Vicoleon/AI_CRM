"""Quality & Trust: where Cubie's AI is strongest and weakest, surface by surface.

One trust scorecard (a table, one row per measure) across the surfaces that
produce AI output, plus the ratings behind it. Every measure says what it is
out of, this period and the one before:

=================  =========================================  ===================
surface            measure                                    source
=================  =========================================  ===================
each rated one     rated useful, of items rated               ``assistant_feedback``
Ask Cubie          answers that failed, of questions          ``interaction_logs``
AI Priorities      dismissed, of cards surfaced; the          the AI Priorities
                   briefing quality score                     dashboard's own code
Meeting Prep       meetings whose brief was rewritten, of     ``meeting_prep_runs``
                   meetings briefed                           versions
Proposals          revised after the first draft, of drafted  ``proposal_versions``
CRM Sync           approved, of suggestions decided           ``crm_sync_suggestions``
Loose Ends         dismissed as noise, of loose ends flagged  ``loop_items``
=================  =========================================  ===================

Ask Cubie questions and the AI Priorities numbers are read through those
dashboards' modules (the same memoised rows), so a figure here never
disagrees with the dashboard that owns it. Loose Ends and CRM Sync are
counted directly with narrow grouped queries, with the same definitions as
their own pages (``loop_closure.views.health``: dismissed ÷ flagged). Counts
and statuses only: no answer, card, brief, suggestion or email is read.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, case, func

from rubik_core.brand import PRODUCT_NAME
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

SLUG = "quality"
TITLE = "Quality & Trust"
HREF = "/insights/quality"

#: ``assistant_feedback.surface`` → the name people know it by, in table order.
RATED_SURFACES: tuple[tuple[str, str], ...] = (
    ("chat", f"Ask {PRODUCT_NAME}"),
    ("priorities", "AI Priorities"),
    ("meeting_prep", "Meeting Prep"),
    ("crm_sync", "CRM Sync"),
    ("xray", "Customer X-Ray"),
)
SURFACE_LABELS = dict(RATED_SURFACES)
#: A brief version that means the person was not happy with what Cubie wrote.
REWRITE_KINDS = ("regenerated", "section_rewritten", "section_edited")


# ── data ────────────────────────────────────────────────────────────────────


def _ratings(ctx: InsightsContext) -> list[tuple[str, date, str, str]]:
    """``(person, day, surface, rating)`` for public settled ratings."""

    def load():
        from rubik_core.database.models import AssistantFeedback as F
        from rubik_core.priorities.privacy import public_priority_ids_for_users

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        public_priorities = public_priority_ids_for_users(
            ctx.db, ctx.population.people.keys()
        )
        out = []
        for email, at, surface, proposal_id, rating in ctx.db.query(
            F.user_email, F.updated_at, F.surface, F.proposal_id, F.rating
        ).filter(
            F.updated_at >= lo,
            F.updated_at < hi,
            ctx.roster_filter(F.user_email),
        ):
            key = fold(email)
            day = local_day(at)
            normalized_surface = (surface or "").strip().lower()
            if (
                normalized_surface == "priorities"
                and proposal_id not in public_priorities.get(key, set())
            ):
                continue
            if ctx.population.in_scope(key) and win.contains(day):
                out.append(
                    (
                        key,
                        day,
                        normalized_surface,
                        (rating or "").strip().lower(),
                    )
                )
        return out

    return ctx.memo("quality.ratings", load)


def _briefs(ctx: InsightsContext) -> list[tuple[str, date, bool]]:
    """``(person, day of the meeting's first brief, rewritten?)`` per meeting
    (person × calendar event), grouped in SQL over brief versions."""

    def load():
        from rubik_core.database.models import MeetingPrepRun as M

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        first = func.min(M.created_at)
        rewrites = func.sum(case((M.change_kind.in_(REWRITE_KINDS), 1), else_=0))
        q = (
            ctx.db.query(M.user_email, M.event_id, first, rewrites)
            .filter(
                M.version.isnot(None),
                M.event_id.isnot(None),
                ctx.roster_filter(M.user_email),
            )
            .group_by(M.user_email, M.event_id)
            .having(and_(first >= lo, first < hi))
        )
        merged: dict[tuple[str, str], list] = {}
        for email, event, at, n in q:
            key = (fold(email), event)
            day = local_day(at)
            cur = merged.get(key)
            if cur is None:
                merged[key] = [day, int(n or 0)]
            else:  # a case-variant email for the same meeting
                cur[0] = min(cur[0], day)
                cur[1] += int(n or 0)
        return [
            (email, day, n > 0)
            for (email, _event), (day, n) in merged.items()
            if ctx.population.in_scope(email) and win.contains(day)
        ]

    return ctx.memo("quality.briefs", load)


def _briefs_tracked_from(ctx: InsightsContext) -> date | None:
    def load():
        from rubik_core.database.models import MeetingPrepRun as M

        first = (
            ctx.db.query(func.min(M.created_at)).filter(M.version.isnot(None)).scalar()
        )
        return local_day(first)

    return ctx.memo("quality.briefs_from", load)


def _loose_ends(ctx: InsightsContext) -> list[tuple[str, date, str]]:
    """``(owner, day first flagged, status)``."""

    def load():
        from rubik_core.database.loop_closure import LoopItem as L

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        out = []
        for email, at, status in ctx.db.query(
            L.owner_email, L.first_flagged_at, L.status
        ).filter(
            L.first_flagged_at >= lo,
            L.first_flagged_at < hi,
            ctx.roster_filter(L.owner_email, folded=True),
        ):
            key = fold(email)
            day = local_day(at)
            if ctx.population.in_scope(key) and win.contains(day):
                out.append((key, day, status or ""))
        return out

    return ctx.memo("quality.loose_ends", load)


def _crm(ctx: InsightsContext) -> list[tuple[str, date, bool]]:
    """``(owner, day decided, approved?)`` for suggestions approved or
    dismissed."""

    def load():
        from rubik_core.database.models import CrmSyncSuggestion as C

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        decided_at = func.coalesce(C.applied_at, C.updated_at)
        out = []
        for email, status, at in ctx.db.query(
            C.user_email, C.status, decided_at
        ).filter(
            C.status.in_(("applied", "dismissed")),
            decided_at >= lo,
            decided_at < hi,
            ctx.roster_filter(C.user_email),
        ):
            key = fold(email)
            day = local_day(at)
            if ctx.population.in_scope(key) and win.contains(day):
                out.append((key, day, status == "applied"))
        return out

    return ctx.memo("quality.crm", load)


# ── measures ────────────────────────────────────────────────────────────────


def _useful(ctx: InsightsContext, surface: str | None = None):
    rows = _ratings(ctx)

    def fn(win: Window) -> Measure:
        rs = [
            r
            for _e, d, s, r in rows
            if win.contains(d) and (surface is None or s == surface)
        ]
        return ratio(sum(1 for r in rs if r == "useful"), len(rs))

    return fn


def _chat_errors(ctx: InsightsContext):
    from rubik_core.insights.dashboards.ask_cubie import questions

    rows = questions(ctx)

    def fn(win: Window) -> Measure:
        rs = [r for r in rows if win.contains(r.day)]
        return ratio(sum(1 for r in rs if not r.successful), len(rs))

    return fn


def _dismissed(ctx: InsightsContext):
    from rubik_core.insights.dashboards import priorities

    return priorities._rate(ctx, priorities.DISMISSED)


def _critic(ctx: InsightsContext):
    from rubik_core.insights.dashboards import priorities

    return priorities._critic(ctx)


def _critic_count(ctx: InsightsContext):
    from rubik_core.insights.dashboards import priorities

    rows = priorities._briefings(ctx)
    return lambda win: sum(1 for _e, d, s in rows if s is not None and win.contains(d))


def _flag_rate(rows: list[tuple[str, date, bool]]):
    def fn(win: Window) -> Measure:
        rs = [flag for _e, d, flag in rows if win.contains(d)]
        return ratio(sum(1 for f in rs if f), len(rs))

    return fn


def _rewrite(ctx: InsightsContext):
    return _flag_rate(_briefs(ctx))


def _noise(ctx: InsightsContext):
    rows = _loose_ends(ctx)
    return _flag_rate([(e, d, s == "dismissed") for e, d, s in rows])


def _acceptance(ctx: InsightsContext):
    return _flag_rate(_crm(ctx))


def _redraft(ctx: InsightsContext):
    from rubik_core.insights.dashboards.ai_spend import proposals_drafted

    return _flag_rate([(e, d, v >= 2) for e, d, v in proposals_drafted(ctx)])


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("useful_rating_rate", _useful(ctx), p, HREF),
        kpi_over("chat_error_rate", _chat_errors(ctx), p, HREF),
    ]


def overview_columns(ctx: InsightsContext) -> list[tuple[str, str, dict]]:
    """Overview's department comparison: items rated useful, out of the items
    each department's people rated."""
    cur = ctx.period.current
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e, d, _s, r in _ratings(ctx):
        if cur.contains(d):
            dept = ctx.population.dept_of(e)
            if dept:
                by[dept][1] += 1
                if r == "useful":
                    by[dept][0] += 1
    cells = {slug: ratio(u, n) for slug, (u, n) in by.items() if n}
    return [("useful_rating_rate", "Rated useful (of items rated)", cells)]


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    kpis = [
        kpi_over("useful_rating_rate", _useful(ctx), p),
        kpi_over("chat_error_rate", _chat_errors(ctx), p),
        kpi_over("priorities_dismiss_rate", _dismissed(ctx), p),
        kpi_over("critic_score", _critic(ctx), p),
        kpi_over(
            "quality_meeting_prep_rewrite_rate",
            _rewrite(ctx),
            p,
            tracked_from=_briefs_tracked_from(ctx),
        ),
        kpi_over("quality_proposal_redraft_rate", _redraft(ctx), p),
        kpi_over("quality_crm_sync_acceptance_rate", _acceptance(ctx), p),
        kpi_over("quality_loose_ends_noise_rate", _noise(ctx), p),
    ]
    sections = [
        _scorecard(ctx),
        _ratings_by_surface(ctx),
        _useful_by_department(ctx),
        _ratings_trend(ctx),
    ]
    notes = [
        "Ratings are people's settled verdicts: re-rating an item replaces the "
        "earlier rating, so each item counts once, on the day it was last rated.",
    ]
    start = _briefs_tracked_from(ctx)
    notes.append(
        f"Meeting brief versions are kept from {fmt_day(start)}, so rewrites "
        "before then are not known."
        if start
        else "Meeting brief versions are kept from the Meeting Prep versions "
        "release on, so rewrites before then are not known."
    )
    return ctx.envelope(SLUG, TITLE, kpis, sections, notes)


# ── sections ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Row:
    key: str
    surface: str
    measure: str
    fn: Callable[[Window], Measure]
    noun: str
    good: str  # "up" | "down"
    of: Callable[[Measure, Window], str | None] | None = None


def _of_text(m: Measure, noun: str) -> str | None:
    if m.denominator is None or not m.denominator:
        return None
    return f"{int(m.numerator or 0):,} of {int(m.denominator):,} {noun}"


def _rows(ctx: InsightsContext) -> list[_Row]:
    critic_n = _critic_count(ctx)
    rows: list[_Row] = []
    for surface, label in RATED_SURFACES:
        rows.append(
            _Row(
                f"{surface}_useful",
                label,
                "Rated useful",
                _useful(ctx, surface),
                "rated",
                "up",
            )
        )
        if surface == "chat":
            rows.append(
                _Row(
                    "chat_errors",
                    label,
                    "Answers that failed",
                    _chat_errors(ctx),
                    "questions",
                    "down",
                )
            )
        elif surface == "priorities":
            rows.append(
                _Row(
                    "priorities_dismissed",
                    label,
                    "Cards dismissed",
                    _dismissed(ctx),
                    "cards surfaced",
                    "down",
                )
            )
            rows.append(
                _Row(
                    "priorities_critic",
                    label,
                    "Briefing quality score (share of 10)",
                    _critic(ctx),
                    "briefings",
                    "up",
                    lambda _m, win: (
                        f"average of {critic_n(win):,} briefings"
                        if critic_n(win)
                        else None
                    ),
                )
            )
        elif surface == "meeting_prep":
            rows.append(
                _Row(
                    "meeting_prep_rewrites",
                    label,
                    "Briefs rewritten",
                    _rewrite(ctx),
                    "meetings briefed",
                    "down",
                )
            )
            rows.append(
                _Row(
                    "proposals_redraft",
                    "Proposals",
                    "Revised after the first draft",
                    _redraft(ctx),
                    "drafted",
                    "down",
                )
            )
        elif surface == "crm_sync":
            rows.append(
                _Row(
                    "crm_sync_accepted",
                    label,
                    "Suggestions approved",
                    _acceptance(ctx),
                    "decided",
                    "up",
                )
            )
            rows.append(
                _Row(
                    "loose_ends_noise",
                    "Loose Ends",
                    "Dismissed as noise",
                    _noise(ctx),
                    "flagged",
                    "down",
                )
            )
    return rows


def _scorecard(ctx: InsightsContext) -> dict:
    p = ctx.period
    out = []
    for row in _rows(ctx):
        cur, prev = row.fn(p.current), row.fn(p.previous)
        if not cur.denominator and not prev.denominator and cur.value is None:
            continue  # nothing to judge, this period or the last
        of = row.of(cur, p.current) if row.of else _of_text(cur, row.noun)
        change = (
            cur.value - prev.value
            if cur.value is not None and prev.value is not None
            else None
        )
        out.append(
            {
                "key": row.key,
                "cells": {
                    "surface": row.surface,
                    "measure": row.measure,
                    "value": cur.value,
                    "of": of,
                    "prev": prev.value,
                    "change": change,
                    "better": "Higher is better"
                    if row.good == "up"
                    else "Lower is better",
                },
            }
        )
    return section(
        "trust_scorecard",
        "table",
        "Trust scorecard",
        table(
            [
                ("surface", f"Part of {PRODUCT_NAME}", "text"),
                ("measure", "Measure", "text"),
                ("value", "This period", "pct"),
                ("of", "Out of", "text"),
                ("prev", "Previous period", "pct"),
                ("change", "Change", "pct"),
                ("better", "Which way is better", "text"),
            ],
            out,
            row_link=None,
        ),
        width="full",
        subtitle=(
            f"One row per measure. The briefing quality score is {PRODUCT_NAME}'s own check "
            "of each briefing, shown as a share of 10 (80% is 8 out of 10). "
            "Change is in percentage points."
        ),
        empty_hint="Nothing was rated or measured in the period.",
    )


def _ratings_by_surface(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for _e, d, s, r in _ratings(ctx):
        if cur.contains(d):
            by[s][1] += 1
            if r == "useful":
                by[s][0] += 1
    items = []
    for s, (useful, total) in by.items():
        m = ratio(useful, total)
        label = SURFACE_LABELS.get(s, s.replace("_", " ").capitalize())
        items.append(bar_item(s, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "useful_by_surface",
        "bar",
        f"Rated useful, by part of {PRODUCT_NAME}",
        bar(items, "pct"),
        subtitle="Out of the items people rated on each.",
        empty_hint="Nothing was rated in the period.",
    )


def _useful_by_department(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e, d, _s, r in _ratings(ctx):
        if cur.contains(d):
            dept = ctx.population.dept_of(e)
            if dept:
                by[dept][1] += 1
                if r == "useful":
                    by[dept][0] += 1
    items = []
    for slug, label in ctx.departments():
        useful, total = by.get(slug, (0, 0))
        if not total:
            continue
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        m = ratio(useful, total)
        items.append(bar_item(slug, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "useful_by_department",
        "bar",
        "Rated useful, by department",
        bar(items, "pct"),
        drill="department",
        subtitle="Out of the items each department's people rated.",
        empty_hint="Nothing was rated in the period.",
    )


def _ratings_trend(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    rated = Counter(s for _e, d, s, _r in _ratings(ctx) if cur.contains(d))
    buckets = ctx.period.buckets
    overall = _useful(ctx)
    series_list = [
        series(
            "all",
            f"All parts of {PRODUCT_NAME}",
            "pct",
            [overall(b.window).value for b in buckets],
        )
    ]
    for surface, label in RATED_SURFACES:
        if not rated.get(surface):
            continue
        fn = _useful(ctx, surface)
        series_list.append(
            series(surface, label, "pct", [fn(b.window).value for b in buckets])
        )
    return section(
        "useful_trend",
        "timeseries",
        "Rated useful over time",
        timeseries([b.key for b in buckets], series_list),
        width="full",
        subtitle="Out of the items rated in each day or week; gaps are days with no ratings.",
        empty_hint="Nothing was rated in the period.",
    )
