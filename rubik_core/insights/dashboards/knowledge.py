"""Knowledge & Content: is the knowledge base growing, and are answers using it?

COUNTS ONLY. Three sources, each read for who, when and a status — never a
title, description, file name, draft or answer:

* ``assets`` — documents added (owner, topic, created). Meeting transcripts
  are the topic "Meeting Transcripts" and are counted apart, per department
  only: a transcript can be a recorded client call, and calls are never
  counted against a person (README §1), so no per-person figure here
  includes them and a department of fewer than 3 is hidden from everyone.
* ``asset_versions`` — new versions of an existing document (version 2 on;
  version 1 is the upload itself).
* ``chat_messages`` — whether an Ask Cubie answer cited a document. Only the
  LENGTH of ``metadata_json["citations"]`` is computed, in SQL; the message,
  the citation titles and the rest of the metadata never leave the
  database. The JSON function differs by dialect (``json_array_length`` with
  a path on SQLite; ``json_typeof`` + ``json_array_length`` on Postgres,
  where a scalar would raise).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import case, extract, func, select

from rubik_core.brand import PRODUCT_NAME
from rubik_core.insights.context import InsightsContext
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
from rubik_core.insights.signals import hourly_counts

SLUG = "knowledge"
TITLE = "Knowledge & Content"
HREF = "/insights/knowledge"
TRANSCRIPTS = "Meeting Transcripts"
#: Topics named in the by-topic bar; the rest are "Other topics".
TOPICS_SHOWN = 12


@dataclass(frozen=True)
class Added:
    email: str
    day: date
    topic: str
    n: int


# ── data ────────────────────────────────────────────────────────────────────


def _assets(ctx: InsightsContext) -> list[Added]:
    """Documents and transcripts added, per owner, topic and hour."""

    def load():
        from rubik_core.database.models import Asset

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        out = []
        for email, topic, hour, n in hourly_counts(
            ctx.db,
            Asset.owner_email,
            Asset.created_at,
            Asset.created_at >= lo,
            Asset.created_at < hi,
            func.coalesce(Asset.status, "approved") != "failed",
            ctx.roster_filter(Asset.owner_email),
            extra=(Asset.topic,),
        ):
            key = fold(email)
            day = local_day(hour)
            if ctx.population.in_scope(key) and win.contains(day):
                out.append(Added(key, day, (topic or "").strip(), n))
        return out

    return ctx.memo("knowledge.assets", load)


def _versions(ctx: InsightsContext) -> list[tuple[str, date, int]]:
    """``(uploader, day, n)`` new versions of documents (not transcripts)."""

    def load():
        from rubik_core.database.models import Asset, AssetVersion

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        transcripts = select(Asset.doc_id).where(Asset.topic == TRANSCRIPTS)
        out = []
        for email, hour, n in hourly_counts(
            ctx.db,
            AssetVersion.uploaded_by,
            AssetVersion.created_at,
            AssetVersion.created_at >= lo,
            AssetVersion.created_at < hi,
            AssetVersion.version_number > 1,
            AssetVersion.doc_id.notin_(transcripts),
            ctx.roster_filter(AssetVersion.uploaded_by),
        ):
            key = fold(email)
            day = local_day(hour)
            if ctx.population.in_scope(key) and win.contains(day):
                out.append((key, day, n))
        return out

    return ctx.memo("knowledge.versions", load)


def citation_count(db):
    """SQL: how many citations an assistant message carries (0 when none)."""
    from rubik_core.database.models import ChatMessage

    col = ChatMessage.metadata_json
    if db.get_bind().dialect.name == "postgresql":
        node = col.op("->")("citations")
        return case(
            (func.json_typeof(node) == "array", func.json_array_length(node)),
            else_=0,
        )
    return func.coalesce(func.json_array_length(col, "$.citations"), 0)


def _answers(ctx: InsightsContext) -> list[tuple[str, date, int, int]]:
    """``(person, day, answers, answers citing a document)`` per person-hour,
    for Ask Cubie conversations people started (scheduled runs excluded)."""

    def load():
        from rubik_core.database.models import ChatMessage, ChatSession
        from rubik_core.insights.signals import _scheduled_session_ids

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        cited = func.sum(case((citation_count(ctx.db) > 0, 1), else_=0))
        day_col = func.date(ChatMessage.created_at)
        hour_col = extract("hour", ChatMessage.created_at)
        q = (
            ctx.db.query(ChatSession.user_email, day_col, hour_col, func.count(), cited)
            .select_from(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .filter(
                ChatMessage.role == "assistant",
                ChatMessage.created_at >= lo,
                ChatMessage.created_at < hi,
                ChatSession.id.notin_(_scheduled_session_ids()),
                ctx.roster_filter(ChatSession.user_email),
            )
            .group_by(ChatSession.user_email, day_col, hour_col)
        )
        out = []
        for email, day, hour, n, c in q:
            d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
            at = datetime(d.year, d.month, d.day, int(hour or 0))
            key = fold(email)
            local = local_day(at)
            if ctx.population.in_scope(key) and win.contains(local):
                out.append((key, local, int(n or 0), int(c or 0)))
        return out

    return ctx.memo("knowledge.answers", load)


# ── measures ────────────────────────────────────────────────────────────────


def _docs(rows: list[Added]) -> list[Added]:
    return [r for r in rows if r.topic != TRANSCRIPTS]


def _transcripts(rows: list[Added]) -> list[Added]:
    return [r for r in rows if r.topic == TRANSCRIPTS]


def _count_added(rows: list[Added]):
    return lambda win: Measure(sum(r.n for r in rows if win.contains(r.day)))


def _count_versions(ctx: InsightsContext):
    rows = _versions(ctx)
    return lambda win: Measure(sum(n for _e, d, n in rows if win.contains(d)))


def _citing_rate(ctx: InsightsContext):
    rows = _answers(ctx)

    def fn(win: Window) -> Measure:
        rs = [(n, c) for _e, d, n, c in rows if win.contains(d)]
        return ratio(sum(c for _n, c in rs), sum(n for n, _c in rs))

    return fn


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("documents_added", _count_added(_docs(_assets(ctx))), p, HREF),
        kpi_over("answers_citing_documents_rate", _citing_rate(ctx), p, HREF),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    """Documents the person added. Never transcripts (README §1)."""
    p = ctx.period
    return [
        kpi_over("documents_added", _count_added(_docs(_assets(ctx))), p, HREF),
        kpi_over("answers_citing_documents_rate", _citing_rate(ctx), p, HREF),
    ]


def overview_columns(ctx: InsightsContext) -> list[tuple[str, str, dict]]:
    """Overview's department comparison: answers citing a document, out of
    the Ask Cubie answers each department was given."""
    cur = ctx.period.current
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for email, d, n, c in _answers(ctx):
        if cur.contains(d):
            dept = ctx.population.dept_of(email)
            if dept:
                by[dept][0] += c
                by[dept][1] += n
    cells = {slug: ratio(c, n) for slug, (c, n) in by.items() if n}
    return [
        (
            "answers_citing_documents_rate",
            "Answers citing documents (of answers)",
            cells,
        )
    ]


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    rows = _assets(ctx)
    kpis = [
        kpi_over("documents_added", _count_added(_docs(rows)), p),
        kpi_over("document_versions_added", _count_versions(ctx), p),
        kpi_over("transcripts_ingested", _count_added(_transcripts(rows)), p),
        kpi_over("answers_citing_documents_rate", _citing_rate(ctx), p),
    ]
    sections = [
        _trend(ctx),
        _citing_trend(ctx),
        _docs_by_department(ctx),
        _by_topic(ctx),
        _transcripts_by_department(ctx),
        _citing_by_department(ctx),
    ]
    if ctx.can_view_people:
        sections.append(_people(ctx))
    notes = [
        "Counts only: no document, transcript or answer is read "
        "or shown here. Meeting transcripts are counted per department, never "
        "per person.",
        "Documents are counted for the department of the person who added them.",
    ]
    return ctx.envelope(SLUG, TITLE, kpis, sections, notes)


# ── sections ────────────────────────────────────────────────────────────────


def _trend(ctx: InsightsContext) -> dict:
    rows = _assets(ctx)
    docs, trans = _docs(rows), _transcripts(rows)
    versions = _versions(ctx)
    buckets = ctx.period.buckets
    return section(
        "content_trend",
        "timeseries",
        "Content added over time",
        timeseries(
            [b.key for b in buckets],
            [
                series(
                    "documents",
                    "Documents added",
                    "count",
                    [
                        sum(r.n for r in docs if b.window.contains(r.day))
                        for b in buckets
                    ],
                ),
                series(
                    "versions",
                    "New versions",
                    "count",
                    [
                        sum(n for _e, d, n in versions if b.window.contains(d))
                        for b in buckets
                    ],
                ),
                series(
                    "transcripts",
                    "Meeting transcripts",
                    "count",
                    [
                        sum(r.n for r in trans if b.window.contains(r.day))
                        for b in buckets
                    ],
                ),
            ],
            chart="bar",
        ),
        width="full",
        empty_hint="Nothing was added in the period.",
    )


def _citing_trend(ctx: InsightsContext) -> dict:
    fn = _citing_rate(ctx)
    return section(
        "citing_trend",
        "timeseries",
        "Answers citing documents",
        timeseries(
            [b.key for b in ctx.period.buckets],
            [
                series(
                    "citing",
                    "Answers citing a document",
                    "pct",
                    [fn(b.window).value for b in ctx.period.buckets],
                )
            ],
        ),
        subtitle=f"Out of the Ask {PRODUCT_NAME} answers given in each period.",
        empty_hint="No answers in the period.",
    )


def _per_department(ctx: InsightsContext, rows: list[Added]) -> Counter:
    cur = ctx.period.current
    counts: Counter[str] = Counter()
    for r in rows:
        if cur.contains(r.day):
            dept = ctx.population.dept_of(r.email)
            if dept:
                counts[dept] += r.n
    return counts


def _docs_by_department(ctx: InsightsContext) -> dict:
    counts = _per_department(ctx, _docs(_assets(ctx)))
    items = []
    for slug, label in ctx.departments():
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        items.append(bar_item(slug, label, counts.get(slug, 0), "count"))
    return section(
        "documents_by_department",
        "bar",
        "Documents added by department",
        bar(items, "count"),
        drill="department",
        empty_hint="No one in the selected departments yet.",
    )


def _transcripts_by_department(ctx: InsightsContext) -> dict:
    counts = _per_department(ctx, _transcripts(_assets(ctx)))
    items = []
    for slug, label in ctx.departments():
        # A transcript can be a recorded client call: never over fewer than
        # 3 people, to anyone (as for Call Capture).
        if ctx.hide_small(len(ctx.dept_people()[slug]), always=True):
            items.append(hidden_bar_item(slug, label))
            continue
        items.append(bar_item(slug, label, counts.get(slug, 0), "count"))
    return section(
        "transcripts_by_department",
        "bar",
        "Meeting transcripts added by department",
        bar(items, "count"),
        drill="department",
        subtitle="Department totals only; transcripts are never counted per person.",
        empty_hint="No one in the selected departments yet.",
    )


def _by_topic(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    counts: Counter[str] = Counter()
    for r in _docs(_assets(ctx)):
        if cur.contains(r.day):
            counts[r.topic or "No topic"] += r.n
    top = [t for t, _n in counts.most_common(TOPICS_SHOWN)]
    items = [bar_item(t.lower().replace(" ", "_"), t, counts[t], "count") for t in top]
    other = sum(n for t, n in counts.items() if t not in top)
    if other:
        items.append(bar_item("other_topics", "Other topics", other, "count"))
    return section(
        "documents_by_topic",
        "bar",
        "Documents added by topic",
        bar(items, "count", sort=False),
        empty_hint="No documents were added in the period.",
    )


def _citing_by_department(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for email, d, n, c in _answers(ctx):
        if cur.contains(d):
            dept = ctx.population.dept_of(email)
            if dept:
                by[dept][0] += c
                by[dept][1] += n
    items = []
    for slug, label in ctx.departments():
        cited, total = by.get(slug, (0, 0))
        if not total:
            continue
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        m = ratio(cited, total)
        items.append(bar_item(slug, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "citing_by_department",
        "bar",
        "Answers citing documents, by department",
        bar(items, "pct"),
        drill="department",
        subtitle=f"Out of the Ask {PRODUCT_NAME} answers each department was given.",
        empty_hint="No answers in the period.",
    )


def _people(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    docs: Counter[str] = Counter()
    for r in _docs(_assets(ctx)):
        if cur.contains(r.day):
            docs[r.email] += r.n
    versions: Counter[str] = Counter()
    for e, d, n in _versions(ctx):
        if cur.contains(d):
            versions[e] += n
    answers: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e, d, n, c in _answers(ctx):
        if cur.contains(d):
            answers[e][0] += n
            answers[e][1] += c
    rows = []
    for email, person in ctx.population.people.items():
        n, c = answers.get(email, (0, 0))
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "documents": docs.get(email, 0),
                    "versions": versions.get(email, 0),
                    "citing_rate": (c / n) if n else None,
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
                ("documents", "Documents added", "count"),
                ("versions", "New versions", "count"),
                ("citing_rate", "Answers citing documents", "pct"),
            ],
            rows,
            default_sort=("documents", "desc"),
        ),
        width="full",
        drill="person",
        subtitle="Meeting transcripts are never counted per person.",
        empty_hint="No one in the selected departments yet.",
    )
