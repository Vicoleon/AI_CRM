"""Temporal context for the AI Priorities pipelines.

This module is the load-bearing fix for the *"surfaced a past event as
something to act on"* class of bug (e.g. the homepage saying "prep for the
L10 meeting" for a meeting that already happened the day before).

The root cause was temporal blindness: no LLM stage was ever told what
day it is, and nothing deterministically compared a signal's date against
"now". This module gives the pipeline:

  1. A single source of "now" anchored to the user's *local* day
     (:func:`resolve_user_now`), so every reasoning prompt can be told the
     date and timezone via :meth:`TemporalContext.block`.
  2. Deterministic, model-independent helpers (:func:`is_past`,
     :func:`is_future`, :func:`parse_to_absolute`) so the orchestrator can
     drop or down-rank a past-anchored priority *regardless of what the LLM
     says*. We never trust the model to know the date — we tell it, and we
     enforce the consequence ourselves.

Timezone resolution is per-user with a workspace fallback. When a caller
passes ``user_email`` (and not an explicit ``tz_name``),
:func:`resolve_user_now` reads that user's saved ``UserPreference.timezone``;
if it's unset the workspace's validated control-row IANA zone applies. An
explicit valid ``tz_name`` always wins. The
lookup is wrapped so it can never break a run — see :func:`_lookup_user_tz`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from rubik_core.availability.pto import PtoStatus

logger = logging.getLogger(__name__)

DEFAULT_LOOKAHEAD_HOURS = 72
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Report kinds. "daily" is the everyday briefing (72h lookahead); "next_week"
# is the Friday look-ahead run that surfaces prep for the *next* calendar week.
REPORT_KIND_DAILY = "daily"
REPORT_KIND_NEXT_WEEK = "next_week"


def next_week_window(now_local: datetime) -> tuple[date, date]:
    """Monday..Sunday of the user's NEXT calendar week (never the current one).

    Weekday is Mon=0 … Sun=6, so ``7 - weekday()`` is always 1..7 days to the
    upcoming Monday: Friday → +3, Saturday → +2, Sunday → +1, Monday → +7.
    Returns ``(week_start, week_end)`` as naive dates in the user's local tz.
    """
    days_to_next_monday = 7 - now_local.weekday()
    week_start = now_local.date() + timedelta(days=days_to_next_monday)
    return week_start, week_start + timedelta(days=6)


def _org_tz_name() -> str:
    """The current active workspace's business time zone, with no process fallback."""
    from rubik_core.tenancy.workspace_time import workspace_time_zone_name

    return workspace_time_zone_name()


def _lookup_user_tz(user_email: str | None) -> str | None:
    """Best-effort: the user's saved IANA timezone, or ``None``.

    Reads ``UserPreference.timezone`` for the user. Swallows EVERY error
    (no DB, no row, unset column, import failure) and returns ``None`` so a
    timezone lookup can never break a priorities run — the caller then falls
    back to the org default via :func:`resolve_tz`. Imports are lazy to avoid
    a domain-layer import cycle (``temporal`` ← ``database`` would be loaded
    at module import otherwise).
    """
    if not user_email:
        return None
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import UserPreference

        db = SessionLocal()
        try:
            tz = (
                db.query(UserPreference.timezone)
                .filter(UserPreference.user_email == user_email)
                .scalar()
            )
        finally:
            db.close()
        return (tz or "").strip() or None
    except Exception:  # noqa: BLE001 — a tz lookup must never break a run
        logger.debug(
            "priorities.temporal: per-user tz lookup failed for %r — org default",
            user_email,
            exc_info=True,
        )
        return None


def resolve_tz(tz_name: str | None = None) -> tuple[ZoneInfo, str]:
    """Resolve a timezone name to a ``ZoneInfo`` + its canonical name.

    An invalid per-user or explicit name falls back to the workspace's zone.
    The workspace zone itself must be available and valid; no UTC or process
    fallback may silently move a company's business day.
    """
    name = tz_name.strip() if isinstance(tz_name, str) else ""
    if not name:
        name = _org_tz_name()
    try:
        return ZoneInfo(name), name
    except Exception:  # noqa: BLE001
        logger.warning(
            "priorities.temporal: unknown timezone %r — using workspace zone", name
        )
        fallback = _org_tz_name()
        return ZoneInfo(fallback), fallback


@dataclass(frozen=True)
class TemporalContext:
    """The pinned "now" for one pipeline run, in the user's local timezone."""

    now_utc: datetime
    now_local: datetime
    tz_name: str
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS

    # Which report this run is building. Defaults keep every existing caller
    # (all daily) untouched; the next-week run sets report_kind + the window.
    report_kind: str = REPORT_KIND_DAILY
    week_start: date | None = None
    week_end: date | None = None

    # Out-of-office awareness (see rubik_core.availability.pto). None until the
    # orchestrator attaches it; when set with on_pto_today, block() tells every
    # reasoning stage the user is off so the run suppresses routine prep instead
    # of generating a normal work-day briefing. Kept optional + defaulted so
    # every existing TemporalContext construction (and all tests) stay valid.
    pto: PtoStatus | None = None

    @property
    def today(self) -> date:
        return self.now_local.date()

    @property
    def today_str(self) -> str:
        return self.now_local.strftime("%Y-%m-%d")

    @property
    def horizon_local(self) -> datetime:
        return self.now_local + timedelta(hours=self.lookahead_hours)

    def block(self) -> str:
        """The ``## TEMPORAL CONTEXT`` header injected into every prompt.

        Every reasoning stage in both pipelines prepends this so the model
        can distinguish a past date from a future one — the single thing it
        could not do before. The next-week variant swaps the horizon sentence
        for the Friday look-ahead framing but keeps the past-is-past clause.
        """
        past_clause = (
            "Treat any date/time STRICTLY BEFORE today as PAST. A past event "
            "is history — it is NEVER something to prepare for or schedule. "
            "Only future-dated items are actionable prep. If your only "
            "evidence for an item is a past date, either re-frame it around "
            "an upcoming moment or do not surface it."
        )
        # When the user is on PTO today, tell every stage plainly. Appended to
        # the shared header so BOTH the daily and next-week prompts carry it.
        pto_clause = ""
        pto = self.pto
        if pto is not None and pto.on_pto_today and pto.current is not None:
            pto_clause = (
                "\n## THE USER IS ON PTO — DO NOT BUILD A WORK-DAY BRIEFING\n"
                f"The user is OUT OF OFFICE today. They are on PTO from "
                f"{pto.current.start.strftime('%A, %Y-%m-%d')} through "
                f"{pto.current.end.strftime('%A, %Y-%m-%d')} and return "
                f"{pto.current.return_date.strftime('%A, %Y-%m-%d')}. They are NOT "
                "working today. Do NOT produce routine 'prepare for your day' prep. "
                "Surface an item ONLY if the user must personally act on it WHILE OFF "
                "— an external client meeting they deliberately kept, or a hard "
                "deadline that genuinely cannot wait for their return. Hold everything "
                "else for when they are back; an empty list is the correct, expected "
                "result for a full day off.\n"
            )
        header = (
            "## TEMPORAL CONTEXT\n"
            f"Today is {self.now_local.strftime('%A, %Y-%m-%d')} "
            f"({self.tz_name}). Current local time: "
            f"{self.now_local.strftime('%H:%M')}.\n"
        ) + pto_clause

        if (
            self.report_kind == REPORT_KIND_NEXT_WEEK
            and self.week_start
            and self.week_end
        ):
            return (
                header + "This is a NEXT-WEEK LOOK-AHEAD report generated on a Friday. "
                f"Focus window: Monday {self.week_start.isoformat()} through "
                f"Sunday {self.week_end.isoformat()} (the user's NEXT week).\n"
                "Surface commitments landing INSIDE that window and recommend "
                "the prep action to take BEFORE the week starts (today or over "
                "the weekend).\n"
                "PRIORITIZE external prospect/client meetings: for each one, "
                "check whether internal prep is already visible BEFORE it (an "
                "SME prep session, a strategy-alignment call, materials being "
                "drafted). If that prep is NOT visible, the top recommendation "
                "is to schedule it now — name who to pull in (the SME / account "
                "team) and cite the deal or company context. A Monday prospect "
                "call with no Friday prep on the calendar is the canonical thing "
                "to flag.\n"
                "Users without external calls still get a useful look-ahead: "
                "deadlines landing next week, meetings that need materials, and "
                "commitments coming due. Do NOT surface routine items for the "
                "remainder of today unless they unblock next week.\n" + past_clause
            )

        return (
            header + f"Lookahead horizon: the next {self.lookahead_hours}h "
            f"(through {self.horizon_local.strftime('%A, %Y-%m-%d')}).\n" + past_clause
        )


def resolve_user_now(
    user_email: str | None = None,
    *,
    tz_name: str | None = None,
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
    now_utc: datetime | None = None,
    report_kind: str = REPORT_KIND_DAILY,
) -> TemporalContext:
    """Build the :class:`TemporalContext` for a run.

    ``now_utc`` is injectable so tests can pin "now" deterministically.
    When ``tz_name`` is not given, the user's saved ``UserPreference.timezone``
    is used (if set); failing that, the org default. Passing ``tz_name``
    explicitly always wins (e.g. the orchestrator forwarding a request value).

    For ``report_kind="next_week"`` the window is Monday..Sunday of the next
    calendar week and ``lookahead_hours`` is derived to reach the END of that
    window, so the horizon sentence, ``horizon_local`` and the decision-maker's
    ``{lookahead}`` meta-rule substitution all stay coherent with the window.
    """
    if tz_name is None:
        tz_name = _lookup_user_tz(user_email)
    tz, name = resolve_tz(tz_name)
    base = now_utc or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    now_local = base.astimezone(tz)

    week_start: date | None = None
    week_end: date | None = None
    if report_kind == REPORT_KIND_NEXT_WEEK:
        week_start, week_end = next_week_window(now_local)
        # Horizon = midnight after week_end (i.e. start of the following
        # Monday), so anchors on Sunday of the target week are still "within".
        horizon = datetime.combine(week_end + timedelta(days=1), dt_time.min, tzinfo=tz)
        lookahead_hours = max(1, int((horizon - now_local).total_seconds() // 3600))

    return TemporalContext(
        now_utc=base.astimezone(timezone.utc),
        now_local=now_local,
        tz_name=name,
        lookahead_hours=lookahead_hours,
        report_kind=report_kind,
        week_start=week_start,
        week_end=week_end,
    )


def user_today_str(
    user_email: str | None = None,
    *,
    tz_name: str | None = None,
    now_utc: datetime | None = None,
) -> str:
    """The user's *local* date as ``YYYY-MM-DD`` — the HomeCard key.

    Keying on the local date (not UTC) stops the evening-Pacific bug where
    UTC has rolled to tomorrow but the user's day has not, so the read path
    looked up a date with no card and showed a stale/empty result.
    """
    return resolve_user_now(user_email, tz_name=tz_name, now_utc=now_utc).today_str


def parse_to_absolute(value, ctx: TemporalContext) -> date | None:
    """Best-effort resolve a date-shaped value to an absolute ``date``.

    Handles: an ISO datetime (``2026-06-24T15:00:00Z`` / with offset), an
    ISO date (``2026-06-24``), an ISO date embedded in a longer string, and
    the ``{"raw","resolved","tense"}`` dict the extractors now emit (prefers
    ``resolved``). Returns ``None`` for anything it cannot parse — including
    free-form relatives like "Friday EOD" that were never resolved.

    Crucially, an unparseable value yields ``None``, and callers treat
    ``None`` as *not past* — we only ever drop on a CONFIRMED past date,
    never on uncertainty.
    """
    if isinstance(value, dict):
        value = value.get("resolved") or value.get("raw")
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Full ISO datetime (with or without trailing Z / offset).
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(ctx.tz_name))
        return dt.astimezone(ZoneInfo(ctx.tz_name)).date()
    except ValueError:
        pass
    # An ISO date anywhere in the string ("due 2026-06-30 EOD").
    m = _ISO_DATE_RE.search(s)
    if m:
        try:
            return date.fromisoformat(m.group(0))
        except ValueError:
            return None
    return None


def is_past(value, ctx: TemporalContext) -> bool:
    """True ONLY when ``value`` parses to a date strictly before today.

    Same-day counts as not-past (a meeting earlier today is still today's
    business). Unparseable / missing → False (never drop on uncertainty).
    """
    d = parse_to_absolute(value, ctx)
    if d is None:
        return False
    return d < ctx.today


def is_future(value, ctx: TemporalContext) -> bool:
    """True when ``value`` parses to today or later (an actionable anchor)."""
    d = parse_to_absolute(value, ctx)
    if d is None:
        return False
    return d >= ctx.today


def parse_to_datetime(value, ctx: TemporalContext) -> datetime | None:
    """Resolve a value that carries a TIME-OF-DAY to an aware datetime in the
    user's tz. Date-only values (no ``T``/time component) return ``None`` —
    use :func:`parse_to_absolute` for those. Unparseable → ``None``.
    """
    if isinstance(value, dict):
        value = value.get("resolved") or value.get("raw")
    if not isinstance(value, str):
        return None
    s = value.strip()
    # No time component → this is a calendar date, not an instant.
    if not s or ("T" not in s and ":" not in s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(ctx.tz_name))
    return dt.astimezone(ZoneInfo(ctx.tz_name))


def is_elapsed(value, ctx: TemporalContext) -> bool:
    """True when ``value`` carries a time-of-day strictly before "now".

    This is the datetime-granularity check the date-level :func:`is_past`
    can't make: a meeting at 09:00 today is *not* past by date, but at 15:00
    it has already happened — you cannot prep for it. Date-only values have
    no time-of-day and are never "elapsed" (→ False)."""
    dt = parse_to_datetime(value, ctx)
    if dt is None:
        return False
    return dt < ctx.now_local


def tense_of(value, ctx: TemporalContext) -> str:
    """Classify a date-shaped value as ``past`` / ``today`` / ``future`` /
    ``unknown`` — used to stamp normalized ``mentioned_dates`` entries."""
    d = parse_to_absolute(value, ctx)
    if d is None:
        return "unknown"
    if d < ctx.today:
        return "past"
    if d == ctx.today:
        return "today"
    return "future"


def format_when_local(
    value, ctx: TemporalContext, *, all_day: bool = False
) -> str | None:
    """Deterministically format an event start as the reader's local wall clock.

    Returns e.g. ``"Thursday Jul 2, 11:00 AM PDT"`` for a timed event, or
    ``"Thursday Jul 2"`` for an all-day event. This exists to kill the class of
    bug where an LLM turned an offset-bearing ISO string (``…T11:00:00-05:00``)
    into a human clock time and drifted the hour (observed +2h). We NEVER let a
    model format a clock time — we format it here, once, against the reader's
    ``ctx.tz_name``, and hand the model a finished string to copy verbatim.

    ``value`` is an ISO datetime (with ``Z`` or an explicit offset) for timed
    events, or an ISO date (``YYYY-MM-DD``) for all-day events. A naive datetime
    is assumed to already be in the reader's timezone. Returns ``None`` when
    there is nothing time-like to format (missing / blank / unparseable) so
    callers omit the line rather than print a bogus midnight.

    Day and hour are built without the platform-specific ``%-d`` / ``%-I``
    strftime directives so the output is identical on Linux (Cloud Run) and
    macOS (dev).
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None

    tz = ZoneInfo(ctx.tz_name)

    # All-day events carry a date, not an instant — no clock time to show and
    # no timezone conversion (a bare date has no offset).
    if all_day or ("T" not in s and ":" not in s):
        try:
            d = date.fromisoformat(s[:10])
        except ValueError:
            return None
        return f"{d.strftime('%A %b')} {d.day}"

    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    local = dt.astimezone(tz)

    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    tz_abbr = local.strftime("%Z") or ctx.tz_name
    return (
        f"{local.strftime('%A %b')} {local.day}, "
        f"{hour12}:{local.strftime('%M')} {ampm} {tz_abbr}"
    )
