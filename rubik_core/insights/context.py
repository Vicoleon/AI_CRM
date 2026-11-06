"""One request's worth of Insights state: who is looking, at what, and the
rows every dashboard on the page shares.

A dashboard module receives an :class:`InsightsContext` and reads through it.
The context memoises the expensive, shared reads (the "active" signals, first
and last use per person) so Overview, which asks each dashboard for its
headline numbers, reads them once rather than once per dashboard.

The database session is the caller's: one short ``with SessionLocal()`` block
around a whole build (docs/ENGINEERING_NOTES.md, "Connection-pool budget"),
opened in a worker thread by the router.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.insights.filters import Period, Window
from rubik_core.insights.population import Population
from rubik_core.insights.scope import Scope
from rubik_core.insights.signals import (
    DEPARTMENT_ONLY_AREAS,
    PERSON_SURFACES,
    Signal,
    email_filter,
    load_first_last,
    load_signals,
)

#: Below this many people a department-level figure can single someone out
#: (``payload.HIDDEN_NOTE`` is what the reader sees instead).
MIN_GROUP = 3


class InsightsContext:
    def __init__(
        self,
        db: Session,
        *,
        viewer: Any,
        scope: Scope,
        period: Period,
        population: Population,
        can_view_people: bool,
        now: datetime | None = None,
    ) -> None:
        self.db = db
        self.viewer = viewer
        self.scope = scope
        self.period = period
        self.population = population
        self.can_view_people = can_view_people
        self.now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        self._memo: dict[str, Any] = {}

    # ── windows ─────────────────────────────────────────────────────────────

    @property
    def data_window(self) -> Window:
        """Everything any dashboard reads: the period, the one before it, the
        30 days before the period (for monthly actives on its first day) and
        the 90 days before its end (the person calendar, retention cohorts)."""
        cur, prev = self.period.current, self.period.previous
        lo = min(
            prev.start, cur.end - timedelta(days=89), cur.start - timedelta(days=29)
        )
        return Window(lo, cur.end)

    @property
    def compare_window(self) -> Window:
        """The previous period through the current one."""
        return Window(self.period.previous.start, self.period.current.end)

    def memo(self, key: str, compute):
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    # ── who ─────────────────────────────────────────────────────────────────

    @property
    def people(self) -> dict:
        return self.population.people

    def roster_filter(self, col, *, folded: bool = False):
        """SQL: ``col`` is someone this view covers (current or departed), so
        a department view never reads the rest of the company's rows."""
        return email_filter(col, self.population.all_emails(), folded=folded)

    def people_count(self) -> int:
        return len(self.population.people)

    def dept_people(self) -> dict[str, list[str]]:
        return self.population.by_dept()

    def departments(self) -> list[tuple[str, str]]:
        """``(slug, label)`` for every selected department that has people,
        in label order."""
        by = self.dept_people()
        return sorted(
            ((slug, self.scope.label(slug)) for slug in by), key=lambda t: t[1].lower()
        )

    # ── activity ────────────────────────────────────────────────────────────

    def signals(self) -> list[Signal]:
        return self.memo(
            "signals",
            lambda: load_signals(
                self.db, self.population.all_emails(), self.data_window
            ),
        )

    def person_signals(self) -> list[Signal]:
        """The signals that may be attributed to a person: everything except
        the department-only areas (Call Capture). EVERY per-person figure —
        active days, last active, the calendar, "active" itself — reads this,
        so a call-capture-only day never shows up against a name."""
        return self.memo(
            "person_signals",
            lambda: [s for s in self.signals() if s.area not in DEPARTMENT_ONLY_AREAS],
        )

    def active_map(self) -> dict[date, set[str]]:
        """Local day → every in-scope email active that day."""

        def build():
            out: dict[date, set[str]] = defaultdict(set)
            for s in self.person_signals():
                out[s.day].add(s.email)
            return out

        return self.memo("active_map", build)

    def active_in(self, win: Window, *, current_only: bool = True) -> set[str]:
        amap = self.active_map()
        out: set[str] = set()
        for d in win.dates():
            out |= amap.get(d, set())
        if current_only:
            out &= set(self.population.people)
        return out

    def active_on(self, d: date) -> set[str]:
        return self.active_map().get(d, set()) & set(self.population.people)

    def person_days(self) -> dict[str, list[date]]:
        """email → sorted distinct active days in the data window."""

        def build():
            days: dict[str, set[date]] = defaultdict(set)
            for s in self.person_signals():
                days[s.email].add(s.day)
            return {e: sorted(ds) for e, ds in days.items()}

        return self.memo("person_days", build)

    def first_last(self) -> dict[str, tuple[date, date]]:
        return self.memo(
            "first_last",
            lambda: load_first_last(self.db, self.population.all_emails()),
        )

    def last_active_as_of(self, email: str, d: date) -> date | None:
        days = [x for x in self.person_days().get(email, []) if x <= d]
        if days:
            return days[-1]
        fl = self.first_last().get(email)
        if fl and fl[1] <= d:
            return fl[1]
        if fl and fl[0] <= d:
            # Used before the data window and again after ``d``: the last use
            # on or before ``d`` is older than the window — report the day
            # before it, the most recent it can have been.
            return self.data_window.start - timedelta(days=1)
        return None

    def surfaces_used(
        self, win: Window, *, person_level: bool = False
    ) -> dict[str, set[str]]:
        """email → surfaces (areas) used in ``win``. ``person_level=True``
        keeps only surfaces that may be shown against a person; the default
        (all surfaces) is for department-level aggregates only."""
        out: dict[str, set[str]] = defaultdict(set)
        for s in self.signals() if not person_level else self.person_signals():
            if s.area and win.contains(s.day):
                if person_level and s.area not in PERSON_SURFACES:
                    continue
                out[s.email].add(s.area)
        return out

    def signal_counts(self, win: Window, sources: set[str]) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for s in self.person_signals():
            if s.source in sources and win.contains(s.day):
                out[s.email] += 1
        return out

    def hide_small(self, people: int, *, always: bool = False) -> bool:
        """Should a department-level figure over ``people`` people be hidden?

        Under ``MIN_GROUP`` people a "department" figure is close to a
        personal one. ``always`` is for figures that must never single anyone
        out (Call Capture); otherwise the rule applies to viewers without the
        people view, who would otherwise learn per-person facts from it.
        """
        if people <= 0 or people >= MIN_GROUP:
            return False
        return always or not self.can_view_people

    # ── envelope ────────────────────────────────────────────────────────────

    def envelope(
        self,
        slug: str,
        title: str,
        kpis: list[dict],
        sections: list[dict],
        notes: list[str] | None = None,
    ) -> dict:
        return {
            "dashboard": slug,
            "title": title,
            "generated_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "cached": False,
            "period": self.period.to_dict(),
            "scope": self.scope.to_dict(self.people_count()),
            "kpis": kpis,
            "sections": sections,
            "notes": list(notes or []),
        }


def fmt_day(d: date) -> str:
    """``Oct 9, 2026`` — how notes name a day."""
    return f"{d:%b} {d.day}, {d.year}"
