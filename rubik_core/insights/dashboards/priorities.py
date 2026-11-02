"""AI Priorities: what Cubie caught for people, and what they did with it.

Three sources, because no single one holds the whole story:

* **Surfaced** — the cards in each person's daily briefing
  (``home_cards`` rows of category ``daily_briefing``, ``metadata_json
  ["priorities"]``). A card's ``id`` can appear on several days and in a
  re-generated briefing the same day; it is counted once, on the first day it
  appeared. The bundle's ``priorities`` array still arrives whole from the
  database (JSON cannot be projected per element portably), but only for the
  people in view and the days in view, streamed in batches, and each card is
  cut down on the spot to its id, urgency, source channels, kind and entity
  ids (:data:`CARD_KEYS`). A title, reason or suggested action is never kept
  past the loop that reads the row, and nothing here could render one.
* **What happened to it** — ``priority_events`` (new with Insights) records
  each transition with who caused it. Before that table existed, the only
  record is ``priority_memory``, which keeps one row per item with its LATEST
  state. So an item's outcome comes from its events when the events cover the
  time it was surfaced, and from the memory row's latest state for anything
  older than the first event ever written (the "cutover"). The payload's
  ``notes`` say from which day card-by-card history exists.
* **Everything else** — ``priorities_runs`` (on-demand run failures),
  ``assistant_feedback`` (ratings), the briefing's own ``last_critique_score``.

Each surfaced card gets ONE outcome, the strongest that applies, in this
order: acted on (the HOLDER marked it done or used a card action: a Slack
follow-up — ``actor == user_email``) > closed when the work was seen done
(Cubie's ``auto_resolved`` evidence, or someone else's action on a shared
entity) > dismissed > snoozed > still open. ``reopened`` (an expired snooze
coming back) and ``delivered_slack`` do not change an outcome. A card that
covers several entities has one event per entity sharing its ``priority_id``;
it is still one card, keyed by ``(user_email, priority_id)``. That keeps the
funnel's steps adding up to what was surfaced. Outcomes are as of now, for the cards surfaced in the period.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from rubik_core.brand import PRODUCT_NAME
from rubik_core.insights.context import InsightsContext, fmt_day
from rubik_core.insights.filters import Window, local_day, local_midnight_utc
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
from rubik_core.priorities.privacy import (
    is_private_gmail_bundle,
    public_priority_ids_for_users,
)

SLUG = "priorities"
TITLE = "AI Priorities"
HREF = "/insights/priorities"
BRIEFING = "daily_briefing"

ACTED = "acted"
AUTO = "auto_resolved"
DISMISSED = "dismissed"
SNOOZED = "snoozed"
OPEN = "open"
_RANK = {ACTED: 4, AUTO: 3, DISMISSED: 2, SNOOZED: 1, OPEN: 0}

ACTION_KINDS = frozenset({"slack_followup"})

#: The only keys of a bundle card this dashboard keeps. Everything else on a
#: card (title, why_it_matters, suggested_action, links, attrs) is content.
CARD_KEYS = (
    "id",
    "urgency",
    "source_channels",
    "priority_kind",
    "entity_kind",
    "entity_id",
)

_CHANNEL_LABELS = {
    "gmail": "Gmail",
    "calendar": "Calendar",
    "drive": "Drive",
    "meet": "Meeting transcripts",
    "hubspot": "HubSpot",
    "eos": "EOS",
    "inherited": "Inherited work",
    "promises": "My To-Dos",
    "slack": "Slack",
    "clickup": "ClickUp",
}


def _label(value: str | None) -> str:
    if not value:
        return "Not recorded"
    return value.replace("_", " ").strip().capitalize()


@dataclass
class Item:
    """One surfaced card."""

    email: str
    pid: str
    first_day: date
    first_at: datetime | None
    urgency: str
    channels: tuple[str, ...]
    kind: str | None
    entity: tuple[str, str, str] | None
    outcome: str = OPEN
    acted_at: datetime | None = None
    started_at: datetime | None = None


@dataclass(frozen=True)
class Event:
    kind: str
    actor: str
    at: datetime


def _outcome_of_event(ev: Event, owner: str) -> str | None:
    """What one event means for the card's holder (``owner``).

    "Acted on" is the HOLDER doing it (actor == holder). A card action can
    also be recorded on a card because SOMEONE ELSE resolved the shared entity
    (the actor is then that other person), which is the work being
    done without the holder: it counts with Cubie's own evidence closes.
    """
    if ev.kind in ACTION_KINDS or ev.kind == "resolved":
        return ACTED if ev.actor == owner else AUTO
    if ev.kind == "auto_resolved":
        return AUTO
    if ev.kind == "dismissed":
        return DISMISSED
    if ev.kind == "snoozed":
        return SNOOZED
    return None


def _outcome_of_memory(state: str, by: str | None, owner: str) -> str:
    state = (state or "").lower()
    if state == "resolved":
        return ACTED if fold(by) == owner else AUTO
    if state in (DISMISSED, SNOOZED):
        return state
    return OPEN


# ── data ────────────────────────────────────────────────────────────────────


def _cutover(ctx: InsightsContext) -> datetime | None:
    def load():
        from sqlalchemy import func

        from rubik_core.database.insights import PriorityEvent

        return ctx.db.query(func.min(PriorityEvent.at)).scalar()

    return ctx.memo("prio.cutover", load)


def _tracked_from(ctx: InsightsContext) -> date | None:
    """First local day ``priority_events`` holds, or None before any — for
    the numbers that come from that table alone (Slack delivery)."""
    cut = _cutover(ctx)
    return local_day(cut) if cut is not None else None


def _slim(card: object) -> dict | None:
    """One bundle card cut down to :data:`CARD_KEYS` — nothing else survives."""
    if not isinstance(card, dict):
        return None
    return {k: card.get(k) for k in CARD_KEYS}


def _card_rows(
    ctx: InsightsContext,
) -> tuple[
    list[tuple[str, date, datetime | None, dict]],
    set[tuple[str, date]],
    dict[str, set[str]],
]:
    """Visible briefings and the owner-days whose briefing used private mail."""

    def load():
        from rubik_core.database.models import HomeCard

        win = ctx.compare_window
        out = []
        private_days: set[tuple[str, date]] = set()
        # The whole bundle is inspected before projection because a top-level
        # or nested private-mail marker makes the entire run owner-only.
        # Roster and window remain SQL filters; rows stream in batches.
        q = (
            ctx.db.query(
                HomeCard.user_email,
                HomeCard.date,
                HomeCard.category,
                HomeCard.created_at,
                HomeCard.metadata_json,
            )
            .filter(
                HomeCard.category.in_([BRIEFING, "next_week_report"]),
                HomeCard.date >= win.start.isoformat(),
                HomeCard.date <= win.end.isoformat(),
                ctx.roster_filter(HomeCard.user_email),
            )
            .yield_per(200)
        )
        for email, day, category, created_at, metadata in q:
            key = fold(email)
            d = local_day(day)
            if not (ctx.population.in_scope(key) and win.contains(d)):
                continue
            if not isinstance(metadata, dict):
                continue
            meta = metadata
            prios = meta.get("priorities")
            if not isinstance(prios, list) or any(
                not isinstance(card, dict) for card in prios
            ):
                continue
            cards = prios
            # The model could mix a personal-mail fact into a work-looking
            # card, so the entire briefing is absent from shared reporting.
            if is_private_gmail_bundle(meta):
                private_days.add((key, d))
                continue
            if category != BRIEFING:
                continue
            slim = [c for c in (_slim(p) for p in cards) if c]
            out.append(
                (
                    key,
                    d,
                    created_at,
                    {
                        "priorities": slim,
                        "last_critique_score": meta.get("last_critique_score"),
                    },
                )
            )
        out.sort(key=lambda r: (r[1], r[2] or datetime.min))
        # A card ID can recur across cached days. If any older bundle carrying
        # that ID was private, its later appearance cannot enter reporting.
        public_ids = public_priority_ids_for_users(
            ctx.db,
            ctx.population.people.keys(),
        )
        for email, _day, _created, metadata in out:
            metadata["priorities"] = [
                card
                for card in metadata["priorities"]
                if str(card.get("id") or "") in public_ids.get(email, set())
            ]
        return out, private_days, public_ids

    return ctx.memo("prio.card_rows", load)


def _cards(ctx: InsightsContext) -> list[tuple[str, date, datetime | None, dict]]:
    """Public daily briefings, projected into safe card fields."""
    return _card_rows(ctx)[0]


def _private_briefing_days(ctx: InsightsContext) -> set[tuple[str, date]]:
    return _card_rows(ctx)[1]


def _public_priority_ids(ctx: InsightsContext) -> dict[str, set[str]]:
    return _card_rows(ctx)[2]


def _events(ctx: InsightsContext):
    """priority_events by ``(email, pid)`` and by ``(email, entity)``, plus
    the Slack deliveries."""

    def load():
        from rubik_core.database.insights import PriorityEvent

        lo, _hi = ctx.compare_window.utc_bounds()
        private_days = _private_briefing_days(ctx)
        public_ids = _public_priority_ids(ctx)
        by_pid: dict[tuple, list[Event]] = defaultdict(list)
        by_entity: dict[tuple, list[Event]] = defaultdict(list)
        delivered: list[tuple[str, date]] = []
        for email, kind, pid, ek, eid, pk, actor, at in ctx.db.query(
            PriorityEvent.user_email,
            PriorityEvent.kind,
            PriorityEvent.priority_id,
            PriorityEvent.entity_kind,
            PriorityEvent.entity_id,
            PriorityEvent.priority_kind,
            PriorityEvent.actor,
            PriorityEvent.at,
        ).filter(
            PriorityEvent.at >= lo,
            ctx.roster_filter(PriorityEvent.user_email, folded=True),
        ):
            key = fold(email)
            if not ctx.population.in_scope(key) or at is None:
                continue
            if kind == "delivered_slack":
                day = local_day(at)
                if (key, day) not in private_days:
                    delivered.append((key, day))
                continue
            if not pid or pid not in public_ids.get(key, set()):
                continue
            ev = Event(kind or "", fold(actor), at)
            by_pid[(key, pid)].append(ev)
            if ek and eid:
                by_entity[(key, ek, eid, pk or "")].append(ev)
        return by_pid, by_entity, delivered

    return ctx.memo("prio.events", load)


def _memory(ctx: InsightsContext):
    def load():
        from sqlalchemy import or_

        from rubik_core.database.models import PriorityMemory

        lo, _hi = ctx.compare_window.utc_bounds()
        public_ids = _public_priority_ids(ctx)
        by_entity: dict[tuple, tuple] = {}
        by_pid: dict[tuple, tuple] = {}
        for email, ek, eid, pk, state, pid, first, changed, by in ctx.db.query(
            PriorityMemory.user_email,
            PriorityMemory.entity_kind,
            PriorityMemory.entity_id,
            PriorityMemory.priority_kind,
            PriorityMemory.state,
            PriorityMemory.last_priority_id,
            PriorityMemory.first_seen_at,
            PriorityMemory.last_state_change_at,
            PriorityMemory.last_state_change_by,
        ).filter(
            or_(
                PriorityMemory.last_seen_at >= lo,
                PriorityMemory.last_state_change_at >= lo,
            ),
            ctx.roster_filter(PriorityMemory.user_email),
        ):
            key = fold(email)
            if not ctx.population.in_scope(key):
                continue
            if not pid or pid not in public_ids.get(key, set()):
                continue
            row = (state, first, changed, by)
            by_entity[(key, ek or "", eid or "", pk or "")] = row
            by_pid[(key, pid)] = row
        return by_entity, by_pid

    return ctx.memo("prio.memory", load)


def items(ctx: InsightsContext) -> list[Item]:
    """Every card surfaced in the previous or current period, with its outcome."""

    def build():
        seen: dict[tuple[str, str], Item] = {}
        for email, day, created_at, meta in _cards(ctx):
            for p in meta.get("priorities") or []:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("id") or "").strip()
                if not pid or (email, pid) in seen:
                    continue
                ek, eid = p.get("entity_kind"), p.get("entity_id")
                kind = p.get("priority_kind") or None
                seen[(email, pid)] = Item(
                    email=email,
                    pid=pid,
                    first_day=day,
                    first_at=created_at,
                    urgency=str(p.get("urgency") or "").strip().lower() or "unknown",
                    channels=tuple(
                        str(c) for c in (p.get("source_channels") or []) if c
                    ),
                    kind=kind,
                    entity=(str(ek), str(eid), str(kind or "")) if ek and eid else None,
                )
        out = list(seen.values())
        _resolve_outcomes(ctx, out)
        return out

    return ctx.memo("prio.items", build)


def _resolve_outcomes(ctx: InsightsContext, rows: list[Item]) -> None:
    by_pid, by_entity, _delivered = _events(ctx)
    mem_entity, mem_pid = _memory(ctx)
    cutover = _cutover(ctx)
    for it in rows:
        start = local_midnight_utc(it.first_day)
        evs = list(by_pid.get((it.email, it.pid), []))
        if it.entity:
            evs += by_entity.get((it.email, *it.entity), [])
        best, best_at = None, None
        for ev in sorted(evs, key=lambda e: e.at):
            if ev.at < start:
                continue
            o = _outcome_of_event(ev, it.email)
            if o and (best is None or _RANK[o] > _RANK[best]):
                best, best_at = o, ev.at
        mem = mem_entity.get((it.email, *it.entity)) if it.entity else None
        mem = mem or mem_pid.get((it.email, it.pid))
        if best is None and mem is not None:
            state, _first, changed, by = mem
            if (
                changed is not None
                and changed >= start
                and (cutover is None or changed < cutover)
            ):
                o = _outcome_of_memory(state, by, it.email)
                if o != OPEN:
                    best, best_at = o, changed
        it.outcome = best or OPEN
        if it.outcome == ACTED:
            it.acted_at = best_at
            first_seen = mem[1] if mem is not None else None
            if first_seen is not None and best_at is not None and first_seen <= best_at:
                it.started_at = first_seen
            else:
                it.started_at = it.first_at or start


def _in(rows: list[Item], win: Window) -> list[Item]:
    return [r for r in rows if win.contains(r.first_day)]


def _briefings(ctx: InsightsContext) -> list[tuple[str, date, float | None]]:
    """One row per (person, day): the day's LAST briefing and its critic score
    (0–1). A briefing re-generated three times is one briefing."""

    def build():
        last: dict[tuple[str, date], float | None] = {}
        for email, day, _created, meta in _cards(ctx):
            raw = meta.get("last_critique_score")
            try:
                score = float(raw) / 10.0 if raw is not None else None
            except (TypeError, ValueError):
                score = None
            last[(email, day)] = score
        return [(e, d, s) for (e, d), s in last.items()]

    return ctx.memo("prio.briefings", build)


def _runs(ctx: InsightsContext) -> list[tuple[str, date, str]]:
    def load():
        from rubik_core.database.models import PrioritiesRun

        win = ctx.compare_window
        lo, hi = win.utc_bounds()
        out = []
        for email, at, status in ctx.db.query(
            PrioritiesRun.user_email, PrioritiesRun.queued_at, PrioritiesRun.status
        ).filter(
            PrioritiesRun.queued_at >= lo,
            PrioritiesRun.queued_at < hi,
            ctx.roster_filter(PrioritiesRun.user_email),
        ):
            key = fold(email)
            if ctx.population.in_scope(key):
                out.append((key, local_day(at), (status or "").lower()))
        return out

    return ctx.memo("prio.runs", load)


# ── measures ────────────────────────────────────────────────────────────────


def _surfaced(ctx):
    rows = items(ctx)
    return lambda win: Measure(len(_in(rows, win)))


def _count_outcome(ctx, outcome: str):
    rows = items(ctx)
    return lambda win: Measure(sum(1 for r in _in(rows, win) if r.outcome == outcome))


def _rate(ctx, outcome: str):
    rows = items(ctx)

    def fn(win):
        rs = _in(rows, win)
        return ratio(sum(1 for r in rs if r.outcome == outcome), len(rs))

    return fn


def _time_to_action(ctx):
    rows = items(ctx)

    def fn(win):
        spans = [
            (r.acted_at - r.started_at).total_seconds() / 86400.0
            for r in _in(rows, win)
            if r.outcome == ACTED and r.acted_at and r.started_at
        ]
        return Measure(median([max(0.0, s) for s in spans]))

    return fn


def _briefings_generated(ctx):
    rows = _briefings(ctx)
    return lambda win: Measure(sum(1 for r in rows if win.contains(r[1])))


def _briefings_per_person(ctx):
    rows = _briefings(ctx)
    eligible = ctx.population.eligible(None)
    return lambda win: ratio(sum(1 for r in rows if win.contains(r[1])), len(eligible))


def _run_failures(ctx):
    rows = _runs(ctx)

    def fn(win):
        rs = [r for r in rows if win.contains(r[1])]
        return ratio(sum(1 for r in rs if r[2] == "failed"), len(rs))

    return fn


def _slack_delivered(ctx):
    _p, _e, delivered = _events(ctx)
    return lambda win: Measure(sum(1 for _email, d in delivered if win.contains(d)))


def _critic(ctx):
    rows = _briefings(ctx)

    def fn(win):
        scores = [s for _e, d, s in rows if s is not None and win.contains(d)]
        return Measure(sum(scores) / len(scores) if scores else None)

    return fn


# ── public ──────────────────────────────────────────────────────────────────


def headline(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("priorities_acted_rate", _rate(ctx, ACTED), p, HREF),
        kpi_over("priorities_surfaced", _surfaced(ctx), p, HREF),
    ]


def person_kpis(ctx: InsightsContext) -> list[dict]:
    p = ctx.period
    return [
        kpi_over("priorities_surfaced", _surfaced(ctx), p, HREF),
        kpi_over("priorities_acted", _count_outcome(ctx, ACTED), p, HREF),
        kpi_over("priorities_acted_rate", _rate(ctx, ACTED), p, HREF),
        kpi_over("priorities_dismiss_rate", _rate(ctx, DISMISSED), p, HREF),
    ]


def coverage_note(ctx: InsightsContext) -> str:
    cut = _cutover(ctx)
    if cut is None:
        return (
            "Card-by-card history (acted on, dismissed, snoozed) and Slack delivery "
            "are tracked from this release on. Until then, each card shows only its "
            "latest state."
        )
    return (
        f"Card-by-card history (acted on, dismissed, snoozed) and Slack delivery "
        f"are tracked from {fmt_day(local_day(cut))}. Before that, each card shows "
        "only its latest state."
    )


def build(ctx: InsightsContext) -> dict:
    p = ctx.period
    kpis = [
        kpi_over("priorities_surfaced", _surfaced(ctx), p),
        kpi_over("priorities_acted", _count_outcome(ctx, ACTED), p),
        kpi_over("priorities_acted_rate", _rate(ctx, ACTED), p),
        kpi_over("priorities_dismiss_rate", _rate(ctx, DISMISSED), p),
        kpi_over("priorities_time_to_action_days", _time_to_action(ctx), p),
        kpi_over("briefings_generated", _briefings_generated(ctx), p),
        kpi_over("briefings_per_person", _briefings_per_person(ctx), p),
        kpi_over(
            "slack_dms_delivered",
            _slack_delivered(ctx),
            p,
            tracked_from=_tracked_from(ctx),
        ),
        kpi_over("priorities_run_failure_rate", _run_failures(ctx), p),
        kpi_over("critic_score", _critic(ctx), p),
        kpi_over("priorities_useful_rate", _useful(ctx), p),
    ]
    cur = _in(items(ctx), p.current)
    sections = [
        _funnel(cur),
        _trend(ctx),
        _channels(cur),
        _urgency(cur),
        _rate_by_department(ctx, cur),
        _rate_by_kind(cur, ACTED, "acted_by_kind", "Acted-on rate by kind of card"),
        _rate_by_kind(
            cur,
            DISMISSED,
            "dismissed_by_kind",
            "Dismiss rate by kind of card",
            subtitle=f"A high rate for one kind is a sign {PRODUCT_NAME} is surfacing noise.",
        ),
        _critic_trend(ctx),
        _ratings(ctx),
    ]
    if ctx.can_view_people:
        sections.append(_people(ctx, cur))
    return ctx.envelope(SLUG, TITLE, kpis, sections, [coverage_note(ctx)])


def _useful(ctx):
    rated = _rating_rows(ctx)

    def fn(win):
        rows = [row for row in rated if win.contains(row[1])]
        return ratio(sum(1 for row in rows if row[2] == "useful"), len(rows))

    return fn


def _rating_rows(ctx: InsightsContext) -> list[tuple[str, date, str]]:
    """Ratings only for IDs proven to belong to public priority bundles."""

    def load():
        from rubik_core.database.models import AssistantFeedback

        lo, hi = ctx.compare_window.utc_bounds()
        public_ids = _public_priority_ids(ctx)
        out = []
        for email, pid, at, rating in ctx.db.query(
            AssistantFeedback.user_email,
            AssistantFeedback.proposal_id,
            AssistantFeedback.updated_at,
            AssistantFeedback.rating,
        ).filter(
            AssistantFeedback.surface == "priorities",
            AssistantFeedback.updated_at >= lo,
            AssistantFeedback.updated_at < hi,
            ctx.roster_filter(AssistantFeedback.user_email),
        ):
            key = fold(email)
            if ctx.population.in_scope(key) and pid in public_ids.get(key, set()):
                out.append((key, local_day(at), (rating or "").strip().lower()))
        return out

    return ctx.memo("prio.ratings", load)


# ── sections ────────────────────────────────────────────────────────────────


def _funnel(cur: list[Item]) -> dict:
    c = Counter(r.outcome for r in cur)
    return section(
        "outcome_funnel",
        "funnel",
        "What happened to the cards surfaced",
        funnel(
            [
                ("surfaced", "Surfaced", len(cur)),
                (ACTED, "Acted on", c.get(ACTED, 0)),
                (AUTO, "Closed when the work was seen done", c.get(AUTO, 0)),
                (DISMISSED, "Dismissed", c.get(DISMISSED, 0)),
                (SNOOZED, "Snoozed", c.get(SNOOZED, 0)),
                (OPEN, "Still open", c.get(OPEN, 0)),
            ],
            mode="breakdown",
        ),
        subtitle="Each card counts once, under the strongest thing that happened to it.",
        empty_hint="No cards were surfaced in the period.",
    )


def _trend(ctx: InsightsContext) -> dict:
    rows = items(ctx)
    surfaced, acted = [], []
    for b in ctx.period.buckets:
        rs = _in(rows, b.window)
        surfaced.append(len(rs))
        acted.append(sum(1 for r in rs if r.outcome == ACTED))
    return section(
        "surfaced_trend",
        "timeseries",
        "Cards surfaced and acted on",
        timeseries(
            [b.key for b in ctx.period.buckets],
            [
                series("surfaced", "Surfaced", "count", surfaced),
                series("acted", "Acted on", "count", acted),
            ],
            chart="bar",
        ),
        subtitle="By the day each card first appeared.",
        empty_hint="No cards were surfaced in the period.",
    )


def _channels(cur: list[Item]) -> dict:
    c: Counter[str] = Counter()
    for r in cur:
        for ch in set(r.channels) or {"unknown"}:
            c[ch] += 1
    items_ = [
        bar_item(k, _CHANNEL_LABELS.get(k, _label(k)), v, "count") for k, v in c.items()
    ]
    return section(
        "by_channel",
        "bar",
        "Where the catches came from",
        bar(items_, "count"),
        subtitle="A card drawn from two sources counts under both.",
        empty_hint="No cards were surfaced in the period.",
    )


def _urgency(cur: list[Item]) -> dict:
    c = Counter(r.urgency for r in cur)
    order = {"high": 0, "medium": 1, "low": 2}
    items_ = sorted(
        (bar_item(k, _label(k), v, "count") for k, v in c.items()),
        key=lambda i: order.get(i["key"], 9),
    )
    return section(
        "by_urgency",
        "bar",
        "Cards by urgency",
        bar(items_, "count", sort=False),
        width="third",
        empty_hint="No cards were surfaced in the period.",
    )


def _rate_by_department(ctx: InsightsContext, cur: list[Item]) -> dict:
    by: dict[str, list[Item]] = defaultdict(list)
    for r in cur:
        dept = ctx.population.dept_of(r.email)
        if dept:
            by[dept].append(r)
    items_ = []
    for slug, label in ctx.departments():
        rs = by.get(slug, [])
        if not rs:
            continue
        # A department of one or two is close to a personal acted-on rate.
        if ctx.hide_small(len(ctx.dept_people()[slug])):
            items_.append(hidden_bar_item(slug, label))
            continue
        m = ratio(sum(1 for r in rs if r.outcome == ACTED), len(rs))
        items_.append(bar_item(slug, label, m.value, "pct", m.numerator, m.denominator))
    return section(
        "acted_by_department",
        "bar",
        "Acted-on rate by department",
        bar(items_, "pct"),
        drill="department",
        subtitle="Cards acted on, out of the cards surfaced to each department.",
        empty_hint="No cards were surfaced in the period.",
    )


def _rate_by_kind(
    cur: list[Item],
    outcome: str,
    section_id: str,
    title: str,
    subtitle: str | None = None,
) -> dict:
    by: dict[str, list[Item]] = defaultdict(list)
    for r in cur:
        by[r.kind or "unknown"].append(r)
    items_ = []
    for kind, rs in by.items():
        m = ratio(sum(1 for r in rs if r.outcome == outcome), len(rs))
        items_.append(
            bar_item(
                kind,
                _label(kind if kind != "unknown" else None),
                m.value,
                "pct",
                m.numerator,
                m.denominator,
            )
        )
    return section(
        section_id,
        "bar",
        title,
        bar(items_, "pct"),
        subtitle=subtitle,
        empty_hint="No cards were surfaced in the period.",
    )


def _critic_trend(ctx: InsightsContext) -> dict:
    fn = _critic(ctx)
    return section(
        "critic_trend",
        "timeseries",
        "Briefing quality score",
        timeseries(
            [b.key for b in ctx.period.buckets],
            [
                series(
                    "critic",
                    "Quality score (0–1)",
                    "score",
                    [fn(b.window).value for b in ctx.period.buckets],
                )
            ],
        ),
        subtitle=f"{PRODUCT_NAME}'s own check of each briefing before it is shown, averaged.",
        empty_hint="No briefings in the period.",
    )


def _ratings(ctx: InsightsContext) -> dict:
    cur = ctx.period.current
    rated = [row for row in _rating_rows(ctx) if cur.contains(row[1])]
    counts = Counter(row[2] for row in rated)
    total = len(rated)
    labels = (("useful", "Useful"), ("meh", "Meh"), ("not_useful", "Not useful"))
    entries = [
        bar_item(
            key,
            label,
            counts.get(key, 0) / total if total else None,
            "pct",
            counts.get(key, 0),
            total,
        )
        for key, label in labels
    ]
    return section(
        "priorities_ratings",
        "bar",
        "How cards were rated",
        bar(entries, "pct", sort=False),
        subtitle="Out of the items people rated in the period.",
        empty_hint="Nothing was rated in the period.",
    )


def _people(ctx: InsightsContext, cur: list[Item]) -> dict:
    by: dict[str, list[Item]] = defaultdict(list)
    for r in cur:
        by[r.email].append(r)
    briefings = Counter(
        e for e, d, _s in _briefings(ctx) if ctx.period.current.contains(d)
    )
    rows = []
    for email, person in ctx.population.people.items():
        rs = by.get(email, [])
        acted = [r for r in rs if r.outcome == ACTED]
        last = max((r.acted_at for r in acted if r.acted_at), default=None)
        rows.append(
            {
                "key": email,
                "cells": {
                    "name": person.name,
                    "department": person.dept_label,
                    "surfaced": len(rs),
                    "acted": len(acted),
                    "acted_rate": (len(acted) / len(rs)) if rs else None,
                    "dismissed": sum(1 for r in rs if r.outcome == DISMISSED),
                    "briefings": briefings.get(email, 0),
                    "last_action": local_day(last).isoformat() if last else None,
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
                ("surfaced", "Cards surfaced", "count"),
                ("acted", "Acted on", "count"),
                ("acted_rate", "Acted-on rate", "pct"),
                ("dismissed", "Dismissed", "count"),
                ("briefings", "Briefings", "count"),
                ("last_action", "Last acted on a card", "date"),
            ],
            rows,
            default_sort=("surfaced", "desc"),
        ),
        width="full",
        drill="person",
        empty_hint="No one in the selected departments yet.",
    )
