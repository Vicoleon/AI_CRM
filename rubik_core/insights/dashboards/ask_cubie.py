"""Ask Cubie: how much people rely on chat, and whether it answers well.

COUNTS ONLY. The question, the answer and the conversation title are never
selected: ``interaction_logs`` is read for who asked, when, in which
conversation, in which mode, whether it succeeded and how long it took. The
intent and answer source are parsed from the short metadata line the chat
router writes into ``system_prompt`` (``Intent: X | Backend: Y | ReqID: Z``),
which carries no user text. Answers streamed to the page do not write that
line, so they show as "Not recorded".

A "question" is one ``interaction_logs`` row that a person sent. Scheduled
task runs also write a row (``mode='scheduled'``) but Cubie ran those on its
own, so they are counted separately as scheduled runs.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from rubik_core.brand import PRODUCT_NAME
from rubik_core.insights.context import InsightsContext
from rubik_core.insights.filters import Window, local_day
from rubik_core.insights.payload import (
    Measure,
    bar,
    bar_item,
    hidden_bar_item,
    kpi_over,
    percentile,
    ratio,
    section,
    series,
    table,
    timeseries,
)
from rubik_core.insights.population import fold

SLUG = "ask-cubie"
TITLE = f"Ask {PRODUCT_NAME}"
HREF = "/insights/ask-cubie"

_META = re.compile(r"Intent:\s*([^|]*?)\s*\|\s*Backend:\s*([^|]*?)\s*(?:\||$)")
_MODE_LABELS = {
    "ask": "Ask",
    "draft": "Draft",
    "investigate": "Investigate",
    None: "Not recorded",
}
NOT_RECORDED = "Not recorded"


@dataclass(frozen=True)
class Question:
    email: str
    day: date
    session_id: str | None
    mode: str | None
    successful: bool
    duration_ms: int
    intent: str
    backend: str


def parse_meta(system_prompt: str | None) -> tuple[str, str]:
    m = _META.search(system_prompt or "")
    if not m:
        return NOT_RECORDED, NOT_RECORDED
    intent = (m.group(1) or "").strip() or NOT_RECORDED
    backend = (m.group(2) or "").strip() or NOT_RECORDED
    return intent, backend


def _pretty(value: str) -> str:
    if value == NOT_RECORDED:
        return value
    return value.replace("_", " ").replace("-", " ").strip().capitalize()


# ── data ────────────────────────────────────────────────────────────────────


def questions(ctx: InsightsContext) -> list[Question]:
    def load():
        from sqlalchemy import or_

        from rubik_core.database.models import InteractionLog

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        out = []
        q = ctx.db.query(
            InteractionLog.user_email,
            InteractionLog.timestamp,
            InteractionLog.session_id,
            InteractionLog.mode,
            InteractionLog.successful,
            InteractionLog.duration_ms,
            InteractionLog.system_prompt,
        ).filter(
            InteractionLog.timestamp >= lo,
            InteractionLog.timestamp < hi,
            or_(InteractionLog.mode.is_(None), InteractionLog.mode != "scheduled"),
            ctx.roster_filter(InteractionLog.user_email),
        )
        for email, at, session_id, mode, ok, dur, meta in q:
            key = fold(email)
            if not ctx.population.in_scope(key):
                continue
            day = local_day(at)
            if not win.contains(day):
                continue
            intent, backend = parse_meta(meta)
            out.append(
                Question(
                    key,
                    day,
                    session_id,
                    (mode or None),
                    ok is not False,
                    int(dur or 0),
                    intent,
                    backend,
                )
            )
        return out

    return ctx.memo("ask.questions", load)


def _in(rows, win: Window):
    return [r for r in rows if win.contains(r.day)]


def _voice(ctx: InsightsContext) -> list[tuple[str, date, int]]:
    """``(email, day, transcriptions)``, counted per person-hour in SQL."""

    def load():
        from rubik_core.database.models import LLMUsageEvent
        from rubik_core.insights.signals import hourly_counts

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        out = []
        for email, hour, n in hourly_counts(
            ctx.db,
            LLMUsageEvent.user_email,
            LLMUsageEvent.timestamp,
            LLMUsageEvent.feature == "voice_input",
            LLMUsageEvent.timestamp >= lo,
            LLMUsageEvent.timestamp < hi,
            ctx.roster_filter(LLMUsageEvent.user_email),
        ):
            key = fold(email)
            day = local_day(hour)
            if ctx.population.in_scope(key) and win.contains(day):
                out.append((key, day, n))
        return out

    return ctx.memo("ask.voice", load)


def ratings(ctx: InsightsContext, surface: str) -> list[tuple[str, date, str]]:
    """``(email, day, rating)`` settled ratings on ``surface`` (re-rating an
    item replaces the row, so each item counts once, on its latest day)."""

    def load():
        from rubik_core.database.models import AssistantFeedback

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        out = []
        for email, at, rating in ctx.db.query(
            AssistantFeedback.user_email,
            AssistantFeedback.updated_at,
            AssistantFeedback.rating,
        ).filter(
            AssistantFeedback.surface == surface,
            AssistantFeedback.updated_at >= lo,
            AssistantFeedback.updated_at < hi,
            ctx.roster_filter(AssistantFeedback.user_email),
        ):
            key = fold(email)
            if ctx.population.in_scope(key):
                out.append((key, local_day(at), (rating or "").strip().lower()))
        return out

    return ctx.memo(f"ratings.{surface}", load)


def _scheduled(ctx: InsightsContext) -> list[tuple[str, date, str]]:
    def load():
        from rubik_core.database.models import ScheduledTaskRun

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        out = []
        for email, at, status in ctx.db.query(
            ScheduledTaskRun.user_email,
            ScheduledTaskRun.created_at,
            ScheduledTaskRun.status,
        ).filter(
            ScheduledTaskRun.created_at >= lo,
            ScheduledTaskRun.created_at < hi,
            ctx.roster_filter(ScheduledTaskRun.user_email),
        ):
            key = fold(email)
            if ctx.population.in_scope(key):
                out.append((key, local_day(at), status or ""))
        return out

    return ctx.memo("ask.scheduled", load)


# ── measures ────────────────────────────────────────────────────────────────


def _questions(ctx):
    rows = questions(ctx)
    return lambda win: Measure(len(_in(rows, win)))


def _conversations(ctx):
    rows = questions(ctx)
    return lambda win: Measure(
        len({r.session_id for r in _in(rows, win) if r.session_id})
    )


def _turns(ctx):
    rows = questions(ctx)

    def fn(win):
        rs = [r for r in _in(rows, win) if r.session_id]
        return ratio(len(rs), len({r.session_id for r in rs}))

    return fn


def _people_asking(ctx):
    rows = questions(ctx)
    people = set(ctx.population.people)
    return lambda win: Measure(len({r.email for r in _in(rows, win)} & people))


def _people_asking_rate(ctx):
    rows = questions(ctx)
    eligible = ctx.population.eligible(None)

    def fn(win):
        asked = {r.email for r in _in(rows, win)} & eligible
        return ratio(len(asked), len(eligible))

    return fn


def _success_rate(ctx):
    rows = questions(ctx)

    def fn(win):
        rs = _in(rows, win)
        return ratio(sum(1 for r in rs if r.successful), len(rs))

    return fn


def _latency(ctx, pct: float):
    rows = questions(ctx)

    def fn(win):
        return Measure(
            percentile(
                [r.duration_ms for r in _in(rows, win) if r.duration_ms > 0], pct
            )
        )

    return fn


def _voice_share(ctx):
    rows = questions(ctx)
    voice = _voice(ctx)

    def fn(win):
        spoken = sum(n for _e, d, n in voice if win.contains(d))
        return ratio(spoken, len(_in(rows, win)))

    return fn


def useful_rate(ctx, surface: str):
    rated = ratings(ctx, surface)

    def fn(win):
        rs = [r for r in rated if win.contains(r[1])]
        return ratio(sum(1 for r in rs if r[2] == "useful"), len(rs))

    return fn


def _scheduled_runs(ctx):
    runs = _scheduled(ctx)
    return lambda win: Measure(sum(1 for r in runs if win.contains(r[1])))


def _scheduled_success(ctx):
    runs = _scheduled(ctx)

    def fn(win):
        done = [
            r for r in runs if win.contains(r[1]) and r[2] in ("succeeded", "failed")
        ]
        return ratio(sum(1 for r in done if r[2] == "succeeded"), len(done))

    return fn


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("questions", _questions(ctx), p, HREF),
        kpi_over("chat_success_rate", _success_rate(ctx), p, HREF),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("questions", _questions(ctx), p, HREF),
        kpi_over("conversations", _conversations(ctx), p, HREF),
        kpi_over("chat_success_rate", _success_rate(ctx), p, HREF),
        kpi_over("chat_p50_ms", _latency(ctx, 50), p, HREF),
    ]


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    kpis = [
        kpi_over("questions", _questions(ctx), p),
        kpi_over("conversations", _conversations(ctx), p),
        kpi_over("turns_per_conversation", _turns(ctx), p),
        kpi_over("people_asking", _people_asking(ctx), p),
        kpi_over("people_asking_rate", _people_asking_rate(ctx), p),
        kpi_over("chat_success_rate", _success_rate(ctx), p),
        kpi_over("chat_p50_ms", _latency(ctx, 50), p),
        kpi_over("chat_p95_ms", _latency(ctx, 95), p),
        kpi_over("voice_share", _voice_share(ctx), p),
        kpi_over("chat_useful_rate", useful_rate(ctx, "chat"), p),
        kpi_over("scheduled_runs", _scheduled_runs(ctx), p),
        kpi_over("scheduled_success_rate", _scheduled_success(ctx), p),
    ]
    cur_rows = _in(questions(ctx), p.current)
    sections = [
        _trend(ctx),
        _by_department(ctx, cur_rows),
        _asking_by_department(ctx, cur_rows),
        _mix(
            "modes",
            "Ask, Draft or Investigate",
            [_MODE_LABELS.get(r.mode, _pretty(r.mode or "")) for r in cur_rows],
            "third",
        ),
        _mix(
            "intents",
            "What people asked about",
            [_pretty(r.intent) for r in cur_rows],
            "third",
        ),
        _mix(
            "backends",
            "Where answers came from",
            [_pretty(r.backend) for r in cur_rows],
            "third",
        ),
        _ratings_section(ctx, "chat", "How answers were rated"),
    ]
    if ctx.can_view_people:
        sections.append(_people(ctx, cur_rows))
    notes = [
        "Answers shown while they stream do not record what the question was "
        "about or where the answer came from, so they appear as 'Not recorded' "
        "in those two charts."
    ]
    return ctx.envelope(SLUG, TITLE, kpis, sections, notes)


# ── sections ────────────────────────────────────────────────────────────────


def _trend(ctx: InsightsContext) -> dict:
    rows = questions(ctx)
    q, people = [], []
    for b in ctx.period.buckets:
        rs = _in(rows, b.window)
        q.append(len(rs))
        people.append(len({r.email for r in rs} & set(ctx.population.people)))
    return section(
        "questions_trend",
        "timeseries",
        "Questions and people asking",
        timeseries(
            [b.key for b in ctx.period.buckets],
            [
                series("questions", "Questions asked", "count", q),
                series("people", "People asking", "count", people),
            ],
        ),
        width="full",
        empty_hint="No questions in the period.",
    )


def _by_department(ctx: InsightsContext, cur_rows: list[Question]) -> dict:
    counts: Counter[str] = Counter()
    for r in cur_rows:
        dept = ctx.population.dept_of(r.email)
        if dept:
            counts[dept] += 1
    items = [
        bar_item(slug, label, counts.get(slug, 0), "count")
        for slug, label in ctx.departments()
    ]
    return section(
        "questions_by_department",
        "bar",
        "Questions by department",
        bar(items, "count"),
        drill="department",
        empty_hint="No questions in the period.",
    )


def _asking_by_department(ctx: InsightsContext, cur_rows: list[Question]) -> dict:
    eligible = ctx.population.eligible(None)
    asked = {r.email for r in cur_rows}
    items = []
    for slug, label in ctx.departments():
        can = set(ctx.dept_people()[slug]) & eligible
        if not can:
            continue
        if ctx.hide_small(len(can)):
            items.append(hidden_bar_item(slug, label))
            continue
        m = ratio(len(can & asked), len(can))
        items.append(bar_item(slug, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "asking_by_department",
        "bar",
        "Share of people asking, by department",
        bar(items, "pct"),
        drill="department",
        subtitle=f"Out of the people in each department who have the {PRODUCT_NAME} copilot.",
        empty_hint=f"Nobody in view has the {PRODUCT_NAME} copilot.",
    )


def _mix(section_id: str, title: str, labels: list[str], width: str) -> dict:
    counts = Counter(labels)
    total = sum(counts.values())
    items = [
        bar_item(
            lbl.lower().replace(" ", "_"),
            lbl,
            n / total if total else None,
            "pct",
            n,
            total,
        )
        for lbl, n in counts.items()
    ]
    return section(
        section_id,
        "bar",
        title,
        bar(items, "pct"),
        width=width,
        empty_hint="No questions in the period.",
    )


def _ratings_section(ctx: InsightsContext, surface: str, title: str) -> dict:
    cur = ctx.period.current
    rated = [r for r in ratings(ctx, surface) if cur.contains(r[1])]
    counts = Counter(r[2] for r in rated)
    total = len(rated)
    labels = (("useful", "Useful"), ("meh", "Meh"), ("not_useful", "Not useful"))
    items = [
        bar_item(
            k,
            lbl,
            counts.get(k, 0) / total if total else None,
            "pct",
            counts.get(k, 0),
            total,
        )
        for k, lbl in labels
    ]
    return section(
        f"{surface}_ratings",
        "bar",
        title,
        bar(items, "pct", sort=False),
        subtitle="Out of the items people rated in the period.",
        empty_hint="Nothing was rated in the period.",
    )


def _people(ctx: InsightsContext, cur_rows: list[Question]) -> dict:
    by: dict[str, list[Question]] = defaultdict(list)
    for r in cur_rows:
        by[r.email].append(r)
    voice: Counter[str] = Counter()
    for e, d, n in _voice(ctx):
        if ctx.period.current.contains(d):
            voice[e] += n
    rows = []
    for email, person in ctx.population.people.items():
        rs = by.get(email, [])
        ok = sum(1 for r in rs if r.successful)
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "questions": len(rs),
                    "conversations": len({r.session_id for r in rs if r.session_id}),
                    "success_rate": (ok / len(rs)) if rs else None,
                    "typical_ms": percentile(
                        [r.duration_ms for r in rs if r.duration_ms > 0], 50
                    ),
                    "spoken": voice.get(email, 0),
                    "last_asked": max(r.day for r in rs).isoformat() if rs else None,
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
                ("questions", "Questions", "count"),
                ("conversations", "Conversations", "count"),
                ("success_rate", "Succeeded", "pct"),
                ("typical_ms", "Typical answer time", "ms"),
                ("spoken", "Spoken questions", "count"),
                ("last_asked", "Last asked", "date"),
            ],
            rows,
            default_sort=("questions", "desc"),
        ),
        width="full",
        drill="person",
        empty_hint="No one in the selected departments yet.",
    )
