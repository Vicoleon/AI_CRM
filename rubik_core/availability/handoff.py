"""Pre-vacation handoff brief — "what falls through while you're out, and who covers it".

The gap this closes: PTO awareness so far has been about the days you're *out*
(:mod:`rubik_core.availability.pto` reframes the briefing to "you're off, nothing
needs you"). Nothing warned you *before* you left. So an L10 you facilitate lands
inside your absence with no one driving it, your to-dos go quietly overdue, and
your scorecard week is never entered — and the team discovers it on the day,
which is exactly when it's too late to hand anything over (the meeting's manager
can't be reached to re-point it; see ``backend/routers/eos/_manager_access``).

This module produces the missing warning, the week before you go:

1. **Detect** the next OOO period starting inside the lead window
   (:func:`upcoming_handoff_period`) — read from ``PtoStatus.upcoming``, which
   already looks 45 days ahead, so no extra calendar traffic.
2. **Inventory** (deterministic SQL, no LLM) everything of yours with a deadline
   *inside* the absence: L10s you manage that fall on those days, To-Dos and Rock
   milestones due then, Rocks already off-track or stale, and the scorecard
   numbers you're the one who enters.
3. **Assign** (one LLM call) a suggested delegate per item from a real candidate
   pool — your direct reports, your own manager, and teammates on your team(s) —
   with a one-line why, plus a short brief you can paste to your team.

Two deliberate constraints on the inventory:

* **Deadline-inside-the-absence, not everything you own.** A brief listing all
  40 of your open items is noise nobody reads; the useful set is the work whose
  clock runs out while nobody's at your desk.
* **Candidates who are actually there.** Anyone whose own OOO overlaps your
  absence is dropped from the pool before the model sees it — suggesting a
  delegate who is also on vacation is worse than suggesting nobody.

The LLM step is *advisory*: if it fails, the inventory still ships with no
suggested delegates rather than the whole brief disappearing. The reminder's job
is to make you aware in time, and the list alone does that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

__all__ = [
    "HANDOFF_LEAD_DAYS",
    "HandoffBrief",
    "HandoffItem",
    "build_handoff_brief",
    "upcoming_handoff_period",
]

# How far ahead we start nagging. The reminder belongs in "the previous
# week" — close enough that hand-off is concrete and people are still
# in the office to accept it, far enough that there's time to actually do it.
HANDOFF_LEAD_DAYS = 7

# A Rock with no weekly update in this long is "stale" — mirrors
# ``EosChannelAgent.STALE_ROCK_DAYS`` so the two surfaces agree on the word.
_STALE_ROCK_DAYS = 10

# Per-kind caps. The brief has to stay readable on a phone; the deadline filter
# already does most of the trimming.
_MAX_PER_KIND = 6
_MAX_CANDIDATES = 12

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@dataclass(frozen=True)
class HandoffItem:
    """One thing that needs an owner while the user is away."""

    kind: str  # "l10" | "todo" | "milestone" | "rock" | "scorecard"
    entity_id: str
    label: str
    detail: str
    # ISO date the clock runs out, or None for cadence work (a weekly scorecard
    # number, a Rock that needs its update) with no single hard date.
    due: str | None = None
    # Filled by the LLM pass; all three stay None when it's unavailable.
    delegate_name: str | None = None
    delegate_email: str | None = None
    delegate_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "entity_id": self.entity_id,
            "label": self.label,
            "detail": self.detail,
            "due": self.due,
            "delegate_name": self.delegate_name,
            "delegate_email": self.delegate_email,
            "delegate_reason": self.delegate_reason,
        }


@dataclass(frozen=True)
class HandoffBrief:
    """Everything the pre-vacation reminder needs to render itself."""

    user_email: str
    start: date
    end: date
    return_date: date
    days_until: int
    items: tuple[HandoffItem, ...] = ()
    # One or two sentences for the card body. Falls back to a deterministic
    # sentence when the LLM pass is unavailable.
    summary: str = ""

    @property
    def l10_items(self) -> tuple[HandoffItem, ...]:
        return tuple(i for i in self.items if i.kind == "l10")

    def as_dict(self) -> dict:
        return {
            "user_email": self.user_email,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "return_date": self.return_date.isoformat(),
            "days_until": self.days_until,
            "summary": self.summary,
            "items": [i.as_dict() for i in self.items],
            "item_count": len(self.items),
            "l10_count": len(self.l10_items),
        }


def upcoming_handoff_period(status, today: date, *, lead_days: int = HANDOFF_LEAD_DAYS):
    """The OOO period to warn about, or None.

    Only periods that **start in the future** within ``lead_days`` qualify. A
    period already under way is not a handoff problem — it's the "you're on PTO"
    case the orchestrator already handles, and by then the warning is moot.
    """
    if status is None:
        return None
    horizon = today + timedelta(days=max(0, lead_days))
    upcoming = [p for p in (status.upcoming or ()) if today < p.start <= horizon]
    if not upcoming:
        return None
    return min(upcoming, key=lambda p: p.start)


def _window_days(start: date, end: date) -> list[date]:
    """Every calendar day in the inclusive absence, bounded defensively."""
    if end < start:
        return [start]
    span = min((end - start).days, 60)
    return [start + timedelta(days=i) for i in range(span + 1)]


def _fmt_date(value) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _person_label(person) -> str:
    if person is None:
        return "(unassigned)"
    name = f"{getattr(person, 'first_name', '') or ''} {getattr(person, 'last_name', '') or ''}".strip()
    return name or (getattr(person, "email", "") or "(unnamed)")


def _enum_value(value) -> str:
    return getattr(value, "value", value) if value is not None else ""


def build_handoff_brief(
    user_email: str,
    period,
    *,
    today: date,
    db=None,
    with_ai: bool = True,
) -> HandoffBrief | None:
    """Inventory what needs covering during ``period`` and suggest delegates.

    Returns None when there's nothing to hand over — no card, no nag. Manages
    its own DB session unless one is passed (tests pass theirs).

    Best-effort by contract: any failure logs and returns None. A pre-vacation
    reminder is a nice-to-have on top of the briefing; it must never be the
    reason a briefing fails to generate.
    """
    from rubik_core.control.repository import module_enabled

    try:
        if not module_enabled("eos"):
            return None
    except Exception:  # noqa: BLE001 — no EOS inventory on failed control read
        logger.warning("[handoff] EOS module lookup failed", exc_info=True)
        return None
    owns_session = db is None
    try:
        if db is None:
            from rubik_core.database.db import SessionLocal

            db = SessionLocal()
        return _build(user_email, period, today=today, db=db, with_ai=with_ai)
    except Exception:  # noqa: BLE001 — never break a briefing over this
        logger.warning("[handoff] brief failed for %s", user_email, exc_info=True)
        return None
    finally:
        if owns_session and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def _build(user_email: str, period, *, today: date, db, with_ai: bool):
    from backend.routers.eos._authz import person_by_email
    from rubik_core.database.eos import (
        L10Meeting,
        Rock,
        RockStatus,
        RockWeeklyUpdate,
        ScorecardMetric,
        Todo,
        TodoStatus,
    )
    from rubik_core.eos.calendar_sync import is_calendar_cancelled

    me = person_by_email(db, user_email)
    if me is None:
        # No EOS identity → nothing owned in EOS to hand over.
        return None

    days = _window_days(period.start, period.end)
    day_set = set(days)
    weekdays_in_window = {d.weekday() for d in days}
    has_workday = any(d.weekday() < 5 for d in days)

    items: list[HandoffItem] = []

    # ── L10s the user facilitates that land inside the absence ──────────────
    # The headline case: a recurring meeting whose one designated manager is
    # away. Matched on the recurring ``day_of_week`` because an L10 stores a
    # weekly pattern, not dated occurrences.
    try:
        meetings = (
            db.query(L10Meeting)
            .filter(
                L10Meeting.is_deleted.is_(False),
                L10Meeting.manager_id == me.id,
            )
            .all()
        )
        for m in meetings:
            # Don't ask somebody to hand over a meeting that isn't happening.
            # The weekly pattern keeps producing occurrences long after the
            # calendar series was cancelled; only a RECONCILED cancellation
            # suppresses it, so hand-scheduled L10s are untouched.
            if is_calendar_cancelled(m):
                continue
            dow = m.day_of_week
            if dow is None or int(dow) not in weekdays_in_window:
                continue
            hit = next((d for d in days if d.weekday() == int(dow)), None)
            when = _WEEKDAY_NAMES[int(dow)]
            at = f" at {m.start_time}" if m.start_time else ""
            items.append(
                HandoffItem(
                    kind="l10",
                    entity_id=str(m.id),
                    label=m.name,
                    detail=(
                        f"You're the manager of this L10, which meets {when}{at} "
                        "while you're out. Nobody else can start it or drive the "
                        "agenda until the manager role is handed over."
                    ),
                    due=_fmt_date(hit),
                )
            )
    except Exception:  # noqa: BLE001 — one source failing must not drop the rest
        logger.warning("[handoff] L10 scan failed for %s", user_email, exc_info=True)

    # ── To-Dos due inside the absence ───────────────────────────────────────
    try:
        todos = (
            db.query(Todo)
            .filter(
                Todo.is_deleted.is_(False),
                Todo.status == TodoStatus.PENDING,
                Todo.owner_id == me.id,
                Todo.due_date.isnot(None),
            )
            .order_by(Todo.due_date.asc())
            .limit(_MAX_PER_KIND * 4)
            .all()
        )
        for t in todos:
            d = _as_date(t.due_date)
            if d is None or d not in day_set:
                continue
            items.append(
                HandoffItem(
                    kind="todo",
                    entity_id=str(t.id),
                    label=t.description or "(untitled to-do)",
                    detail=f"Due {d.isoformat()}, while you're out.",
                    due=d.isoformat(),
                )
            )
            if sum(1 for i in items if i.kind == "todo") >= _MAX_PER_KIND:
                break
    except Exception:  # noqa: BLE001
        logger.warning("[handoff] To-Do scan failed for %s", user_email, exc_info=True)

    # ── Rock milestones due inside the absence ──────────────────────────────
    # Defensive import: the Milestone table lands independently of this module.
    try:
        from rubik_core.database.eos.rocks import RockMilestone

        my_rock_ids = [
            r[0]
            for r in db.query(Rock.id)
            .filter(Rock.is_deleted.is_(False), Rock.owner_id == me.id)
            .all()
        ]
        if my_rock_ids:
            milestones = (
                db.query(RockMilestone)
                .filter(
                    RockMilestone.is_deleted.is_(False),
                    RockMilestone.rock_id.in_(my_rock_ids),
                )
                .limit(_MAX_PER_KIND * 4)
                .all()
            )
            for ms in milestones:
                d = _as_date(getattr(ms, "due_date", None))
                if d is None or d not in day_set:
                    continue
                if bool(getattr(ms, "completed", False)):
                    continue
                items.append(
                    HandoffItem(
                        kind="milestone",
                        entity_id=str(ms.id),
                        label=getattr(ms, "description", "") or "(untitled milestone)",
                        detail=f"Rock milestone due {d.isoformat()}, while you're out.",
                        due=d.isoformat(),
                    )
                )
                if sum(1 for i in items if i.kind == "milestone") >= _MAX_PER_KIND:
                    break
    except Exception:  # noqa: BLE001 — table may not exist yet
        logger.debug("[handoff] milestone scan skipped for %s", user_email)

    # ── Rocks already needing attention ─────────────────────────────────────
    # Not every Rock — only ones already off-track or without a recent update.
    # Those are the ones that get worse unattended; a healthy Rock survives a
    # week on its own.
    try:
        rocks = (
            db.query(Rock)
            .filter(
                Rock.is_deleted.is_(False),
                Rock.owner_id == me.id,
                Rock.status != RockStatus.DONE,
            )
            .limit(_MAX_PER_KIND * 4)
            .all()
        )
        stale_cutoff = today - timedelta(days=_STALE_ROCK_DAYS)
        for r in rocks:
            last = (
                db.query(RockWeeklyUpdate)
                .filter(
                    RockWeeklyUpdate.rock_id == r.id,
                    RockWeeklyUpdate.is_deleted.is_(False),
                )
                .order_by(RockWeeklyUpdate.update_date.desc())
                .first()
            )
            last_d = _as_date(getattr(last, "update_date", None)) if last else None
            off_track = _enum_value(r.status) == RockStatus.OFF_TRACK.value
            stale = last_d is None or last_d < stale_cutoff
            if not (off_track or stale):
                continue
            why = "off track" if off_track else "no weekly update in over a week"
            items.append(
                HandoffItem(
                    kind="rock",
                    entity_id=str(r.id),
                    label=r.description or "(untitled rock)",
                    detail=(
                        f"This Rock is {why} and needs its weekly update while "
                        "you're out."
                    ),
                )
            )
            if sum(1 for i in items if i.kind == "rock") >= _MAX_PER_KIND:
                break
    except Exception:  # noqa: BLE001
        logger.warning("[handoff] Rock scan failed for %s", user_email, exc_info=True)

    # ── Scorecard numbers the user is the one who enters ────────────────────
    # Only when the absence covers a working day: a Sat–Sun trip needs no cover.
    if has_workday:
        try:
            metrics = (
                db.query(ScorecardMetric)
                .filter(
                    ScorecardMetric.is_deleted.is_(False),
                    ScorecardMetric.owner_id == me.id,
                )
                .limit(_MAX_PER_KIND * 2)
                .all()
            )
            for sm in metrics[:_MAX_PER_KIND]:
                items.append(
                    HandoffItem(
                        kind="scorecard",
                        entity_id=str(sm.id),
                        label=getattr(sm, "name", "") or "(untitled measurable)",
                        detail=(
                            "You own this measurable — someone needs to enter "
                            "the weekly number while you're out."
                        ),
                    )
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[handoff] scorecard scan failed for %s", user_email, exc_info=True
            )

    if not items:
        return None

    days_until = (period.start - today).days
    brief = HandoffBrief(
        user_email=user_email,
        start=period.start,
        end=period.end,
        return_date=period.return_date,
        days_until=days_until,
        items=tuple(items),
        summary=_fallback_summary(items, period, days_until),
    )

    if not with_ai:
        return brief

    candidates = _delegate_candidates(db, me, days)
    if not candidates:
        return brief
    return _assign_delegates(brief, candidates)


def _fallback_summary(items: list[HandoffItem], period, days_until: int) -> str:
    """Deterministic summary — used as-is when the LLM pass can't run."""
    n_l10 = sum(1 for i in items if i.kind == "l10")
    when = "tomorrow" if days_until <= 1 else f"in {days_until} days"
    lead = (
        f"You're out {period.start.isoformat()}–{period.end.isoformat()} "
        f"(starting {when}), back {period.return_date.isoformat()}."
    )
    n = len(items)
    tail = f"{n} thing needs cover" if n == 1 else f"{n} things need cover"
    if n_l10:
        tail += f", including {n_l10} L10 you manage"
    return f"{lead} {tail}."


def _delegate_candidates(db, me, days: list[date]) -> list[dict]:
    """People who could cover: direct reports, the user's own manager, teammates.

    Anyone whose own OOO overlaps the absence is dropped — a delegate who is
    also away is worse than no suggestion, because it reads as handled.
    """
    from backend.routers.eos._authz import (
        _direct_report_person_ids,
        _person_ids_in_depts,
        _seat_for_person,
    )
    from rubik_core.availability.store import get_user_ooo_on
    from rubik_core.database.eos import Person, Seat

    seat = _seat_for_person(me, db)
    pids: set = set(_direct_report_person_ids(seat, db))

    # The user's own manager (the holder of the seat theirs reports to).
    if seat is not None and getattr(seat, "reports_to_id", None):
        boss_seat_id = seat.reports_to_id
        boss = (
            db.query(Person)
            .filter(Person.seat_id == boss_seat_id, Person.is_deleted.is_(False))
            .first()
        )
        if boss is not None:
            pids.add(boss.id)

    # Teammates in the same department.
    if seat is not None and getattr(seat, "department_name", None):
        pids |= _person_ids_in_depts({seat.department_name}, db)

    pids.discard(me.id)
    if not pids:
        return []

    people = (
        db.query(Person)
        .filter(Person.id.in_(list(pids)), Person.is_deleted.is_(False))
        .all()
    )

    seat_by_id = {}
    seat_ids = [p.seat_id for p in people if p.seat_id]
    if seat_ids:
        seat_by_id = {
            s.id: s for s in db.query(Seat).filter(Seat.id.in_(seat_ids)).all()
        }

    out: list[dict] = []
    for p in people:
        email = (p.email or "").strip().lower()
        if not email:
            continue
        # Drop anyone out during the same window.
        if any(get_user_ooo_on(db, email, d) is not None for d in days):
            continue
        s = seat_by_id.get(p.seat_id) if p.seat_id else None
        out.append(
            {
                "name": _person_label(p),
                "email": email,
                "seat": getattr(s, "title", None) if s is not None else None,
                "is_leadership": bool(getattr(s, "is_leadership", False))
                if s is not None
                else False,
            }
        )
    # Leadership first — the most likely facilitator for a handed-off L10.
    out.sort(key=lambda c: (not c["is_leadership"], c["name"]))
    return out[:_MAX_CANDIDATES]


def _assign_delegates(brief: HandoffBrief, candidates: list[dict]) -> HandoffBrief:
    """One LLM call: pick a delegate per item and write the brief's summary.

    Advisory only — on any failure the brief is returned untouched (inventory
    intact, no suggestions), because the reminder's value is the awareness, not
    the assignment.
    """
    try:
        return _assign_delegates_llm(brief, candidates)
    except Exception:  # noqa: BLE001 — suggestions are a bonus, never a blocker
        logger.warning("[handoff] delegate suggestion failed for %s", brief.user_email)
        return brief


def _handoff_prompt(brief: HandoffBrief, candidates: list[dict]) -> str:
    items_json = json.dumps(
        [
            {
                "entity_id": i.entity_id,
                "kind": i.kind,
                "label": i.label,
                "detail": i.detail,
                "due": i.due,
            }
            for i in brief.items
        ],
        indent=2,
    )
    people_json = json.dumps(candidates, indent=2)
    return f"""You are helping someone hand their work over before time off.

They are out of office {brief.start.isoformat()} through {brief.end.isoformat()}
and back {brief.return_date.isoformat()} — that starts in {brief.days_until} day(s).

WORK THAT NEEDS COVER (each has an entity_id you must echo back):
{items_json}

PEOPLE WHO COULD COVER IT (all confirmed to be working during the absence):
{people_json}

For EVERY item, pick the single best person from the candidate list and say why
in at most 12 words. Rules:
- An L10 the user manages should go to someone senior enough to facilitate it —
  prefer a candidate with is_leadership true, or the most plausible stand-in.
- Spread the load: don't put everything on one person unless there is only one
  sensible candidate.
- Use ONLY emails from the candidate list. Never invent a person.
- If no candidate genuinely fits an item, use null for delegate_email and say
  why in the reason.

Also write "summary": two sentences max, addressed to the user as "you", stating
the absence dates and what most needs handing over before they go. Be concrete
and calm — no exclamation marks, no "don't forget".

Return ONLY this JSON:
{{
  "summary": "...",
  "assignments": [
    {{"entity_id": "...", "delegate_email": "..." or null, "reason": "..."}}
  ]
}}"""


def _assign_delegates_llm(brief: HandoffBrief, candidates: list[dict]) -> HandoffBrief:
    from rubik_core.cost.usage import attribute_to
    from rubik_core.priorities.agents.base import LLMJsonClient

    # Decorated at the point of work (not the caller) so the cost attribution
    # survives a thread hand-off — see rubik_core/cost/usage.attribute_to.
    @attribute_to("ooo_handoff")
    def _run(prompt: str):
        client = LLMJsonClient(model_key="gemini-flash", temperature=0.2)
        return client.invoke_json(prompt, fallback={}, attempts=2)

    data = _run(_handoff_prompt(brief, candidates)) or {}
    by_email = {c["email"]: c for c in candidates}
    assignments = {}
    for row in data.get("assignments") or []:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("entity_id") or "")
        email = (row.get("delegate_email") or "").strip().lower() or None
        # Hallucination guard: an email the model invented is dropped, keeping
        # the item unassigned rather than pointing at a person who doesn't exist.
        if email and email not in by_email:
            logger.info(
                "[handoff] dropped invented delegate %r for %s", email, brief.user_email
            )
            email = None
        assignments[eid] = (email, str(row.get("reason") or "").strip() or None)

    items = []
    for item in brief.items:
        email, reason = assignments.get(item.entity_id, (None, None))
        cand = by_email.get(email) if email else None
        items.append(
            HandoffItem(
                kind=item.kind,
                entity_id=item.entity_id,
                label=item.label,
                detail=item.detail,
                due=item.due,
                delegate_name=cand["name"] if cand else None,
                delegate_email=email,
                delegate_reason=reason,
            )
        )

    summary = str(data.get("summary") or "").strip() or brief.summary
    return HandoffBrief(
        user_email=brief.user_email,
        start=brief.start,
        end=brief.end,
        return_date=brief.return_date,
        days_until=brief.days_until,
        items=tuple(items),
        summary=summary,
    )
