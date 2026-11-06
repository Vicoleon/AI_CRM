"""When is an open L10 session actually over?

An ``eos_l10_sessions`` row stays open (``actual_end IS NULL``) until a
facilitator clicks Conclude, and in practice almost nobody does, so most
open sessions have had no write for days or weeks. That leaks into the
product twice:

* ``GET /api/eos/l10/meetings`` offers "Rejoin" for any open session, so
  the board advertises meetings that ended weeks ago.
* ``POST /api/eos/l10/sessions/start`` reuses an open session instead of
  creating one, so the next occurrence of that meeting silently resumes a
  month-old row — inheriting its ``current_step`` cursor, its attendance
  rows and its ratings.

This module owns the one definition of "abandoned" that all three call
sites (the board, the start path, the nightly sweep) share, so they can
never disagree about whether a meeting is still going.

Two things have to be true before a session is abandoned:

1. **It is well past its box.** A meeting's box is
   ``actual_start + duration_minutes`` (default 90); we wait
   :data:`BOX_GRACE` beyond that, because L10s routinely run over.
2. **Nothing has written to it for :data:`INACTIVITY_WINDOW`.**

Requiring BOTH is deliberate. Neither arm is safe alone: a meeting that
starts an hour late would be closed mid-sentence by the box arm, and the
inactivity arm alone would close a meeting parked on IDS — where people
argue for forty minutes and the database sees nothing.

**Why the box is anchored on ``actual_start`` and not the meeting's
scheduled ``day_of_week``/``start_time``.** The scheduled slot is a naive
weekly ``"HH:MM"`` string with no timezone column, so turning it into a
concrete instant for *this* occurrence means guessing a week and a DST
offset — to land within minutes of ``actual_start``, which is a recorded
fact. ``actual_start`` also moves with ``POST /sessions/{id}/restart``,
which genuinely re-declares when the meeting began. Unscheduled meetings
(``day_of_week IS NULL``) have no slot at all and would need this
fallback anyway.

**What counts as a write.** Polling does not write, so the signals are
the ones a live meeting actually leaves behind: the session row itself
(``updated_at``, ``step_started_at``), attendance (a check-in or a
rating), and in-meeting signals (the rabbit-hole button). Note the gap —
solving an issue or ticking a to-do during IDS writes ``eos_issues`` /
``eos_todos``, *not* the session — which is exactly why
:data:`INACTIVITY_WINDOW` is hours rather than minutes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func

from rubik_core.database.eos import L10Attendance, L10Meeting, L10Session, L10Signal

logger = logging.getLogger(__name__)

# A meeting with no ``duration_minutes`` gets the EOS-standard 90-minute box
# (the column's own default, restated here for rows that predate it).
DEFAULT_MEETING_MINUTES = 90

# How long past the end of its box a meeting may still be running. Two hours
# is generous on purpose: the cost of closing a live meeting (the board drops
# its Rejoin mid-session) is far worse than the cost of leaving a dead one
# open a little longer.
BOX_GRACE = timedelta(hours=2)

# How long the session may go without a single write before we call it over.
# Four hours clears the worst realistic case — a 90-minute L10 whose
# facilitator advanced to IDS early and never touched the agenda again.
INACTIVITY_WINDOW = timedelta(hours=4)

# Stamped into ``updated_by`` so the audit trail reads
# "the system ended this", not the last human who happened to touch the row.
# The ``auto_closed`` boolean is the queryable form; this is for humans
# reading the row.
AUTO_CLOSE_ACTOR = "system:auto-close"


@dataclass(frozen=True)
class OpenSession:
    """One still-open session, with everything needed to judge it."""

    session_id: UUID
    meeting_id: UUID
    meeting_name: str
    department_name: str | None
    is_exec_meeting: bool
    actual_start: datetime
    current_step: int
    # Latest of every write signal below — our best estimate of when the
    # meeting actually stopped happening.
    last_activity: datetime
    # ``actual_start + duration_minutes``: when the meeting was due to end.
    box_end: datetime
    # The instant this session becomes abandoned. Both arms must be satisfied,
    # so it is the later of the two deadlines.
    abandoned_at: datetime

    def is_abandoned(self, now: datetime) -> bool:
        return now >= self.abandoned_at

    def describe(self, now: datetime) -> str:
        """One line for a dry run / log — why this row is or is not stale."""
        idle_h = (now - self.last_activity).total_seconds() / 3600.0
        verdict = "ABANDONED" if self.is_abandoned(now) else "keep"
        return (
            f"{verdict:9} {self.meeting_name[:34]:34} "
            f"started={self.actual_start:%Y-%m-%d %H:%M} "
            f"last_write={self.last_activity:%Y-%m-%d %H:%M} "
            f"idle={idle_h:6.1f}h step={self.current_step} "
            f"session={self.session_id}"
        )


@dataclass(frozen=True)
class CloseReport:
    """Outcome of a sweep. ``closed`` is empty on a dry run."""

    scanned: list[OpenSession]
    abandoned: list[OpenSession]
    closed: list[UUID]
    dry_run: bool
    now: datetime

    @property
    def still_live(self) -> list[OpenSession]:
        stale = {s.session_id for s in self.abandoned}
        return [s for s in self.scanned if s.session_id not in stale]

    def summary(self) -> str:
        verb = "would close" if self.dry_run else "closed"
        return (
            f"{len(self.scanned)} open session(s); "
            f"{verb} {len(self.abandoned)}; "
            f"{len(self.scanned) - len(self.abandoned)} still live"
        )


def _bulk_max(db, model, column, session_ids: list[UUID]) -> dict[UUID, datetime]:
    """``{session_id: max(column)}`` in one query — no N+1 over sessions."""
    if not session_ids:
        return {}
    rows = (
        db.query(model.session_id, func.max(column))
        .filter(model.session_id.in_(session_ids))
        .group_by(model.session_id)
        .all()
    )
    return {sid: stamp for sid, stamp in rows if stamp is not None}


def open_sessions(db, *, meeting_ids: list[UUID] | None = None) -> list[OpenSession]:
    """Every open, non-deleted session, annotated with its last write.

    ``meeting_ids`` scopes the scan to the meetings a caller already has in
    hand (the board passes the page it is rendering); omit it to sweep
    everything. The predicate matches the app's own definition of open —
    ``actual_end IS NULL AND NOT is_deleted`` — so this can never disagree
    with what the UI calls live.
    """
    query = (
        db.query(L10Session, L10Meeting)
        .join(L10Meeting, L10Meeting.id == L10Session.meeting_id)
        .filter(
            L10Session.actual_end.is_(None),
            L10Session.is_deleted == False,  # noqa: E712
        )
    )
    if meeting_ids is not None:
        if not meeting_ids:
            return []
        query = query.filter(L10Session.meeting_id.in_(meeting_ids))
    rows = query.all()
    if not rows:
        return []

    session_ids = [s.id for s, _ in rows]
    # Attendance rows are updated in place on re-check-in, so ``updated_at``
    # is the live signal; it defaults to ``created_at`` on insert.
    last_attendance = _bulk_max(
        db, L10Attendance, L10Attendance.updated_at, session_ids
    )
    # Signals are append-only — they have no meaningful ``updated_at``.
    last_signal = _bulk_max(db, L10Signal, L10Signal.created_at, session_ids)

    out: list[OpenSession] = []
    for session, meeting in rows:
        stamps = [
            session.actual_start,
            session.updated_at,
            session.step_started_at,
            last_attendance.get(session.id),
            last_signal.get(session.id),
        ]
        last_activity = max(t for t in stamps if t is not None)
        box_end = session.actual_start + timedelta(
            minutes=meeting.duration_minutes or DEFAULT_MEETING_MINUTES
        )
        out.append(
            OpenSession(
                session_id=session.id,
                meeting_id=meeting.id,
                meeting_name=meeting.name or "(unnamed)",
                department_name=meeting.department_name,
                is_exec_meeting=bool(meeting.is_exec_meeting),
                actual_start=session.actual_start,
                current_step=session.current_step or 0,
                last_activity=last_activity,
                box_end=box_end,
                abandoned_at=max(
                    last_activity + INACTIVITY_WINDOW, box_end + BOX_GRACE
                ),
            )
        )
    out.sort(key=lambda s: s.last_activity, reverse=True)
    return out


def live_session_ids(
    db, meeting_ids: list[UUID], *, now: datetime | None = None
) -> dict[UUID, UUID]:
    """``{meeting_id: session_id}`` for meetings with a session still going.

    The read-side half of auto-closure, used by the meetings board. It
    FILTERS rather than closes: a GET stays a GET, and the nightly sweep is
    what actually stamps ``actual_end``. The effect is the same for the user
    — an abandoned session stops offering Rejoin the moment it goes stale,
    without waiting for the sweep.

    Newest open session wins when a meeting somehow has more than one.
    """
    now = now or datetime.utcnow()
    by_meeting: dict[UUID, OpenSession] = {}
    for row in open_sessions(db, meeting_ids=meeting_ids):
        if row.is_abandoned(now):
            continue
        current = by_meeting.get(row.meeting_id)
        if current is None or row.actual_start > current.actual_start:
            by_meeting[row.meeting_id] = row
    return {mid: row.session_id for mid, row in by_meeting.items()}


def close_abandoned_sessions(
    db,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    meeting_ids: list[UUID] | None = None,
    commit: bool = True,
) -> CloseReport:
    """Stamp ``actual_end`` on every abandoned session. Idempotent.

    ``actual_end`` is set to the session's LAST ACTIVITY, not to ``now``.
    Stamping ``now`` would be easier but it lies: a meeting that stopped at
    10:30 and gets swept at 02:00 would report a 15-hour duration and poison
    every average built on it. Last activity is the honest estimate of when
    the room emptied.

    ``auto_closed`` marks the row so nothing downstream mistakes it for a
    real Conclude. A human Conclude also collects per-person ratings and
    derives ``average_rating``; a sweep cannot invent those, so an
    auto-closed session keeps a NULL rating that means "never collected"
    rather than "the team scored this meeting zero".
    """
    now = now or datetime.utcnow()
    scanned = open_sessions(db, meeting_ids=meeting_ids)
    abandoned = [s for s in scanned if s.is_abandoned(now)]

    if dry_run or not abandoned:
        return CloseReport(
            scanned=scanned, abandoned=abandoned, closed=[], dry_run=dry_run, now=now
        )

    closed: list[UUID] = []
    for row in abandoned:
        # Re-read under the same transaction and re-check ``actual_end``: a
        # facilitator may have concluded between the scan and now, and their
        # Conclude (with real ratings) must win over our estimate.
        session = (
            db.query(L10Session)
            .filter(
                L10Session.id == row.session_id,
                L10Session.actual_end.is_(None),
            )
            .first()
        )
        if session is None:
            continue
        session.actual_end = row.last_activity
        session.auto_closed = True
        session.updated_by = AUTO_CLOSE_ACTOR
        # Bump the shared cursor version so any tab still polling
        # /sessions/{id}/state notices on its next ~2s tick; the non-null
        # ``actual_end`` in that payload is what tells it the meeting is over.
        session.state_version = (session.state_version or 0) + 1
        closed.append(row.session_id)
        logger.info(
            "[l10-auto-close] %s meeting=%s ended_at=%s idle_hours=%.1f",
            row.session_id,
            row.meeting_name,
            row.last_activity,
            (now - row.last_activity).total_seconds() / 3600.0,
        )

    # ALWAYS flush, even when the caller owns the commit. This project's
    # sessionmaker sets ``autoflush=False``, so without this the pending
    # ``actual_end`` stays invisible to the very next query — which is how
    # ``/sessions/start`` would close a session and then immediately re-read
    # it as still open and rejoin it.
    db.flush()
    if commit:
        db.commit()
    return CloseReport(
        scanned=scanned, abandoned=abandoned, closed=closed, dry_run=False, now=now
    )
