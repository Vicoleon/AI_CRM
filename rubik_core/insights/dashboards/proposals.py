"""Proposals & Prep: whether proposals and meeting preparation get produced.

Sources, each read for an email, a timestamp and a status or kind only:

* ``proposals`` + ``proposal_versions`` — a proposal is started, its brief
  confirmed, then drafted (Cubie cannot draft an unconfirmed brief). Each
  revision appends a version. The brief, the prose, the instruction and the
  change summary are never selected; ``hubspot_deal_id`` is read only as
  "is there one".
* ``meeting_prep_runs`` — one row per on-screen brief version (``change_kind``
  generated / regenerated / section_rewritten / section_edited / restored /
  failed, recorded since Meeting Prep versions shipped) and one per deep prep
  (``mode='deep'``, emailed, with its ``delivery_status``). The meeting title,
  the context bundle, the response and the rewrite instruction are never
  selected. Rows from before versions shipped carry no ``change_kind``: they
  count as briefs, but the edit and failure rates start with versions.
* ``xray_snapshots`` — counted, with kind; failed runs are left out. Company
  names and sections are never selected.
* ``call_session_scripts`` — the call scripts attached to recorded calls.
  Department level only (Google Meet's terms), and left out for a department
  where fewer than 3 people use call capture.

Proposals are a cohort: the ones started in the period, with where each
stands now. Meeting Prep and X-Ray count what happened in the period, except
the edit rate, whose cohort is the briefs generated in it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from rubik_core.brand import PRODUCT_NAME
from rubik_core.insights.context import InsightsContext
from rubik_core.insights.filters import Window, local_day
from rubik_core.insights.payload import (
    Measure,
    bar,
    bar_item,
    cell,
    funnel,
    hidden_bar_item,
    hidden_cell,
    kpi_over,
    matrix,
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

SLUG = "proposals"
TITLE = "Proposals & Prep"
HREF = "/insights/proposals"

#: On-screen change kinds that are a new brief. ``None`` is a brief written
#: before versions were tracked.
NEW_BRIEF = frozenset({"generated", "regenerated", None})
EDITS = frozenset({"section_rewritten", "section_edited"})

_PREP_LABELS = {
    "generated": "Brief generated",
    "regenerated": "Brief regenerated",
    "section_rewritten": f"Section rewritten by {PRODUCT_NAME}",
    "section_edited": "Section edited by hand",
    "restored": "Earlier version restored",
    "failed": "Generation failed",
    "deep": "Deep prep (emailed)",
    "untracked": "Brief (before versions were tracked)",
}


@dataclass(frozen=True)
class ProposalRow:
    email: str
    day: date
    created_at: datetime
    status: str
    confirmed: bool
    linked: bool
    versions: int
    first_version_at: datetime | None

    @property
    def drafted(self) -> bool:
        return self.versions > 0 or self.status == "drafted"


@dataclass(frozen=True)
class PrepRow:
    email: str
    day: date
    deep: bool
    kind: str | None  # change_kind for on-screen rows
    failed: bool
    chain: tuple[str, str]  # (email, meeting id) — the brief a version belongs to


# ── data ────────────────────────────────────────────────────────────────────


def proposals(ctx: InsightsContext) -> list[ProposalRow]:
    def load():
        from sqlalchemy import func

        from rubik_core.database.proposals import Proposal, ProposalVersion

        lo, hi = ctx.compare_window.utc_bounds()
        in_view = (
            Proposal.created_at >= lo,
            Proposal.created_at < hi,
            ctx.roster_filter(Proposal.owner_email, folded=True),
        )
        versions = {
            pid: (int(n or 0), first)
            for pid, n, first in ctx.db.query(
                ProposalVersion.proposal_id,
                func.count(ProposalVersion.id),
                func.min(ProposalVersion.created_at),
            )
            .join(Proposal, Proposal.id == ProposalVersion.proposal_id)
            .filter(*in_view)
            .group_by(ProposalVersion.proposal_id)
        }
        out = []
        for pid, email, created, status, confirmed_at, deal in ctx.db.query(
            Proposal.id,
            Proposal.owner_email,
            Proposal.created_at,
            Proposal.status,
            Proposal.brief_confirmed_at,
            Proposal.hubspot_deal_id,
        ).filter(*in_view):
            key = fold(email)
            if not ctx.population.in_scope(key) or created is None:
                continue
            n, first = versions.get(pid, (0, None))
            out.append(
                ProposalRow(
                    key,
                    local_day(created),
                    created,
                    (status or "").lower(),
                    confirmed_at is not None,
                    bool(deal),
                    n,
                    first,
                )
            )
        return out

    return ctx.memo("proposals.rows", load)


def prep(ctx: InsightsContext) -> list[PrepRow]:
    def load():
        from rubik_core.database.models import MeetingPrepRun as M

        lo, hi = ctx.compare_window.utc_bounds()
        out = []
        for rid, email, at, mode, kind, delivery, event_id in ctx.db.query(
            M.id,
            M.user_email,
            M.created_at,
            M.mode,
            M.change_kind,
            M.delivery_status,
            M.event_id,
        ).filter(
            M.created_at >= lo, M.created_at < hi, ctx.roster_filter(M.user_email)
        ):
            key = fold(email)
            if not ctx.population.in_scope(key) or at is None:
                continue
            deep = (mode or "").lower() == "deep"
            kind = (kind or "").lower() or None
            failed = (delivery or "").lower() == "failed" if deep else kind == "failed"
            out.append(
                PrepRow(key, local_day(at), deep, kind, failed, (key, event_id or rid))
            )
        return out

    return ctx.memo("proposals.prep", load)


def _versions_from(ctx: InsightsContext) -> date | None:
    """First day Meeting Prep recorded what kind of change each version was."""

    def load():
        from sqlalchemy import func

        from rubik_core.database.models import MeetingPrepRun as M

        value = (
            ctx.db.query(func.min(M.created_at))
            .filter(M.change_kind.isnot(None))
            .scalar()
        )
        return local_day(value)

    return ctx.memo("proposals.versions_from", load)


def _xray(ctx: InsightsContext) -> list[tuple[str, date, str]]:
    def load():
        from rubik_core.database.models import XraySnapshot as X

        lo, hi = ctx.compare_window.utc_bounds()
        out = []
        for email, at, kind in ctx.db.query(X.created_by, X.created_at, X.kind).filter(
            X.created_at >= lo,
            X.created_at < hi,
            X.status != "failed",
            ctx.roster_filter(X.created_by),
        ):
            key = fold(email)
            if ctx.population.in_scope(key) and at is not None:
                out.append((key, local_day(at), (kind or "fast").lower()))
        return out

    return ctx.memo("proposals.xray", load)


def _scripts(ctx: InsightsContext) -> list[tuple[str, date]]:
    """``(AE email, day attached)`` for call scripts attached to recorded
    calls. Department level only."""

    def load():
        from rubik_core.database.call_capture import CallSession
        from rubik_core.database.call_scripts import CallSessionScript as S

        lo, hi = ctx.compare_window.utc_bounds()
        out = []
        for email, at in (
            ctx.db.query(CallSession.ae_email, S.attached_at)
            .join(CallSession, CallSession.id == S.call_session_id)
            .filter(
                S.attached_at >= lo,
                S.attached_at < hi,
                ctx.roster_filter(CallSession.ae_email, folded=True),
            )
        ):
            key = fold(email)
            if ctx.population.in_scope(key) and at is not None:
                out.append((key, local_day(at)))
        return out

    return ctx.memo("proposals.scripts", load)


# ── measures ────────────────────────────────────────────────────────────────


def _cohort(rows, win: Window):
    return [r for r in rows if win.contains(r.day)]


def _started(ctx):
    rows = proposals(ctx)
    return lambda win: Measure(len(_cohort(rows, win)))


def _drafted(ctx):
    rows = proposals(ctx)
    return lambda win: Measure(sum(1 for r in _cohort(rows, win) if r.drafted))


def draft_rate(rows: list[ProposalRow]) -> Measure:
    return ratio(sum(1 for r in rows if r.drafted), len(rows))


def _draft_rate(ctx):
    rows = proposals(ctx)
    return lambda win: draft_rate(_cohort(rows, win))


def _revisions(ctx):
    rows = proposals(ctx)

    def fn(win):
        drafted = [r for r in _cohort(rows, win) if r.drafted]
        return ratio(sum(max(r.versions - 1, 0) for r in drafted), len(drafted))

    return fn


def _linked(ctx):
    rows = proposals(ctx)

    def fn(win):
        rs = _cohort(rows, win)
        return ratio(sum(1 for r in rs if r.linked), len(rs))

    return fn


def _days_to_draft(ctx):
    rows = proposals(ctx)

    def fn(win):
        return Measure(
            median(
                [
                    max(
                        0.0, (r.first_version_at - r.created_at).total_seconds() / 86400
                    )
                    for r in _cohort(rows, win)
                    if r.first_version_at is not None
                ]
            )
        )

    return fn


def _is_brief(r: PrepRow) -> bool:
    return r.deep or r.kind in NEW_BRIEF


def _briefs(ctx):
    rows = prep(ctx)
    return lambda win: Measure(sum(1 for r in _cohort(rows, win) if _is_brief(r)))


def _deep_share(ctx):
    rows = prep(ctx)

    def fn(win):
        briefs = [r for r in _cohort(rows, win) if _is_brief(r)]
        return ratio(sum(1 for r in briefs if r.deep), len(briefs))

    return fn


def _edit_rate(ctx):
    rows = prep(ctx)
    edited = {r.chain for r in rows if not r.deep and r.kind in EDITS}

    def fn(win):
        made = {
            r.chain
            for r in _cohort(rows, win)
            if not r.deep and r.kind in ("generated", "regenerated")
        }
        return ratio(len(made & edited), len(made))

    return fn


def _failure_rate(ctx):
    rows = prep(ctx)

    def fn(win):
        tried = [
            r
            for r in _cohort(rows, win)
            if r.deep or r.kind in ("generated", "regenerated", "failed")
        ]
        return ratio(sum(1 for r in tried if r.failed), len(tried))

    return fn


def _xray_count(ctx):
    rows = _xray(ctx)
    return lambda win: Measure(sum(1 for r in rows if win.contains(r[1])))


def scripts_visible(ctx: InsightsContext) -> frozenset[str]:
    return ctx.memo(
        "proposals.scripts_visible",
        lambda: visible_departments(ctx, {e for e, _d in _scripts(ctx)}),
    )


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("prep_briefs", _briefs(ctx), p, HREF),
        kpi_over("proposals_draft_rate", _draft_rate(ctx), p, HREF),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("proposals_started", _started(ctx), p, HREF),
        kpi_over("proposals_drafted", _drafted(ctx), p, HREF),
        kpi_over("prep_briefs", _briefs(ctx), p, HREF),
        kpi_over("xray_snapshots", _xray_count(ctx), p, HREF),
    ]


def overview_columns(ctx: InsightsContext) -> list[tuple[str, str, dict]]:
    """Overview's department comparison: proposals that reached a draft."""
    by: dict[str, list[ProposalRow]] = defaultdict(list)
    for r in _cohort(proposals(ctx), ctx.period.current):
        dept = ctx.population.dept_of(r.email)
        if dept:
            by[dept].append(r)
    return [
        (
            "proposals_draft_rate",
            "Proposals drafted (of started)",
            {slug: draft_rate(rows) for slug, rows in by.items()},
        )
    ]


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    versions_from = _versions_from(ctx)
    kpis = [
        kpi_over("proposals_started", _started(ctx), p),
        kpi_over("proposals_drafted", _drafted(ctx), p),
        kpi_over("proposals_draft_rate", _draft_rate(ctx), p),
        kpi_over("proposal_revisions_per_proposal", _revisions(ctx), p),
        kpi_over("proposals_deal_linked_rate", _linked(ctx), p),
        kpi_over("proposals_days_to_draft", _days_to_draft(ctx), p),
        kpi_over("prep_briefs", _briefs(ctx), p),
        kpi_over("prep_deep_share", _deep_share(ctx), p),
        kpi_over("prep_edit_rate", _edit_rate(ctx), p, tracked_from=versions_from),
        kpi_over(
            "prep_failure_rate", _failure_rate(ctx), p, tracked_from=versions_from
        ),
        kpi_over("xray_snapshots", _xray_count(ctx), p),
    ]
    cur_props = _cohort(proposals(ctx), p.current)
    sections = [
        _trend(ctx),
        _proposal_funnel(cur_props),
        _prep_kinds(ctx),
        _by_department(ctx),
        _scripts_by_department(ctx),
    ]
    if ctx.can_view_people:
        sections.append(_people(ctx))
    return ctx.envelope(SLUG, TITLE, kpis, sections, _notes(ctx, versions_from))


def _notes(ctx: InsightsContext, versions_from: date | None) -> list[str]:
    from rubik_core.database.proposals import Proposal

    notes = []
    note = history_note(
        ctx, "The Proposal Generator", earliest(ctx.db, Proposal.created_at)
    )
    if note:
        notes.append(note)
    note = history_note(
        ctx, "Which Meeting Prep briefs were edited, rewritten or failed", versions_from
    )
    if note:
        notes.append(note)
    if any(
        ctx.population.dept_of(e) not in scripts_visible(ctx) for e, _d in _scripts(ctx)
    ):
        notes.append(CALL_HIDDEN_NOTE)
    return notes


# ── sections ────────────────────────────────────────────────────────────────


def _trend(ctx: InsightsContext) -> dict:
    fns = (
        ("proposals", "Proposals started", _started(ctx)),
        ("briefs", "Meeting Prep briefs", _briefs(ctx)),
        ("xray", "Customer X-Ray snapshots", _xray_count(ctx)),
    )
    return section(
        "produced_trend",
        "timeseries",
        "What got produced",
        timeseries(
            [b.key for b in ctx.period.buckets],
            [
                series(
                    k, lbl, "count", [fn(b.window).value for b in ctx.period.buckets]
                )
                for k, lbl, fn in fns
            ],
            chart="bar",
        ),
        width="full",
        empty_hint="Nothing was produced in the period.",
    )


def _proposal_funnel(cur: list[ProposalRow]) -> dict:
    confirmed = sum(1 for r in cur if r.confirmed or r.drafted)
    return section(
        "proposal_funnel",
        "funnel",
        "From proposal to draft",
        funnel(
            [
                ("started", "Started", len(cur)),
                ("confirmed", "Facts confirmed", confirmed),
                ("drafted", "Drafted", sum(1 for r in cur if r.drafted)),
            ]
        ),
        subtitle="Proposals started in the period, and how far each has got.",
        empty_hint="No proposals were started in the period.",
    )


def _prep_kinds(ctx: InsightsContext) -> dict:
    c: Counter[str] = Counter()
    for r in _cohort(prep(ctx), ctx.period.current):
        c["deep" if r.deep else (r.kind or "untracked")] += 1
    items = [
        bar_item(k, plain_label(k, _PREP_LABELS), c[k], "count")
        for k in _PREP_LABELS
        if c.get(k)
    ]
    items += [
        bar_item(k, plain_label(k), n, "count")
        for k, n in c.items()
        if k not in _PREP_LABELS
    ]
    return section(
        "prep_by_change_kind",
        "bar",
        "Meeting Prep: briefs and changes",
        bar(items, "count", sort=False),
        subtitle="Every brief, edit, rewrite, restore and failure in the period.",
        empty_hint="No Meeting Prep in the period.",
    )


def _by_department(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    cols = [
        ("proposals", "Proposals started"),
        ("drafted", "Proposals drafted"),
        ("briefs", "Meeting Prep briefs"),
        ("xray", "X-Ray snapshots"),
    ]
    counts: dict[str, Counter[str]] = defaultdict(Counter)

    def add(email, col):
        dept = ctx.population.dept_of(email)
        if dept:
            counts[dept][col] += 1

    for r in _cohort(proposals(ctx), cur):
        add(r.email, "proposals")
        if r.drafted:
            add(r.email, "drafted")
    for r in _cohort(prep(ctx), cur):
        if _is_brief(r):
            add(r.email, "briefs")
    for e, d, _k in _xray(ctx):
        if cur.contains(d):
            add(e, "xray")
    rows = ctx.departments()
    cells = []
    for slug, _label in rows:
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            cells.append([hidden_cell() for _ in cols])
            continue
        cells.append([cell(Measure(counts[slug].get(k, 0)), "count") for k, _l in cols])
    return section(
        "by_department",
        "matrix",
        "Produced, by department",
        matrix(rows, cols, cells, unit="count"),
        width="full",
        drill="department",
        subtitle="Click a department to filter every number to it.",
        empty_hint="No one in the selected departments yet.",
    )


def _scripts_by_department(ctx: InsightsContext) -> dict:
    vis = scripts_visible(ctx)
    cur = ctx.period.current
    counts = Counter(
        ctx.population.dept_of(e) for e, d in _scripts(ctx) if cur.contains(d)
    )
    items = []
    for slug, label in ctx.departments():
        if not counts.get(slug):
            continue
        if slug not in vis or ctx.hide_small(len(ctx.dept_people()[slug])):
            items.append(hidden_bar_item(slug, label))
            continue
        items.append(bar_item(slug, label, counts[slug], "count"))
    return section(
        "call_scripts_by_department",
        "bar",
        "Call scripts used on recorded calls",
        bar(items, "count"),
        drill="department",
        subtitle="Department totals only: recorded calls are never shown per person.",
        empty_hint="No call scripts were attached to calls in the period.",
    )


def _people(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    props: dict[str, list[ProposalRow]] = defaultdict(list)
    for r in _cohort(proposals(ctx), cur):
        props[r.email].append(r)
    briefs: Counter[str] = Counter()
    deep: Counter[str] = Counter()
    edits: Counter[str] = Counter()
    for r in _cohort(prep(ctx), cur):
        if _is_brief(r):
            briefs[r.email] += 1
        if r.deep:
            deep[r.email] += 1
        if not r.deep and r.kind in EDITS:
            edits[r.email] += 1
    xray = Counter(e for e, d, _k in _xray(ctx) if cur.contains(d))
    rows = []
    for email, person in ctx.population.people.items():
        ps = props.get(email, [])
        drafted = [r for r in ps if r.drafted]
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "proposals": len(ps),
                    "drafted": len(drafted),
                    "revisions": sum(max(r.versions - 1, 0) for r in drafted),
                    "briefs": briefs.get(email, 0),
                    "deep": deep.get(email, 0),
                    "edits": edits.get(email, 0),
                    "xray": xray.get(email, 0),
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
                ("proposals", "Proposals started", "count"),
                ("drafted", "Drafted", "count"),
                ("revisions", "Revisions", "count"),
                ("briefs", "Meeting Prep briefs", "count"),
                ("deep", "Deep preps", "count"),
                ("edits", "Brief edits and rewrites", "count"),
                ("xray", "X-Ray snapshots", "count"),
            ],
            rows,
            default_sort=("briefs", "desc"),
        ),
        width="full",
        drill="person",
        subtitle="Call scripts are not in this table: recorded calls are counted per department only.",
        empty_hint="No one in the selected departments yet.",
    )
