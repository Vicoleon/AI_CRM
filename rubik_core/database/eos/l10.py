"""L10 (Level 10) weekly meetings.

Hierarchy:
- L10Meeting: the recurring slot (day-of-week + start_time).
- L10Session: one occurrence of that meeting, with notes + ratings.
- L10Attendance: per-person rating + attended flag for a session.
- L10Input: pre-meeting headlines + ideas the team can drop in
  anytime before the session starts.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from rubik_core.database.models import Base

from ._base import AuditMixin, L10InputKind


class L10Meeting(Base, AuditMixin):
    __tablename__ = "eos_l10_meetings"

    department_name = Column(
        String, ForeignKey("departments.name", ondelete="SET NULL"), nullable=True
    )
    name = Column(String, nullable=False)
    day_of_week = Column(Integer)  # 0=Mon..6=Sun
    start_time = Column(String)  # "HH:MM"
    duration_minutes = Column(Integer, default=90, nullable=False)
    manager_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=True)
    # Cross-cutting Exec leadership team. When true the meeting belongs to
    # the Exec team rather than a single department; ``department_name`` is
    # typically NULL in that case.
    is_exec_meeting = Column(
        Boolean, default=False, nullable=False, server_default=sa.false()
    )
    # Configurable per-meeting link slot for transient initiatives
    # like "What Matters Most". Don't hard-code one-off initiatives into
    # the L10 agenda — give teams a slot they can repoint when something
    # else takes its place. Both nullable; the frontend treats the link
    # as visible only when both label + url are populated.
    secondary_link_label = Column(String, nullable=True)
    secondary_link_url = Column(String, nullable=True)
    # List of attendee emails copied from the imported
    # Google Calendar event. Acts as the meeting's access roster — only
    # users in this list (plus admins and the department they belong to)
    # can join the session. Nullable so legacy / manually-scheduled
    # meetings without a calendar source skip the roster check.
    roster_emails = Column(JSON, nullable=True)
    # Source calendar event id, set when the meeting was imported from
    # Google Calendar. Lets re-imports upsert instead of creating
    # duplicates of the same recurring slot.
    google_event_id = Column(String, nullable=True, index=True)
    # Calendar date of the specific occurrence most recently imported.
    # The meeting itself only stores a recurring day_of_week/start_time
    # pattern — this is purely informational so the UI can show which
    # occurrence a (re-)import last refreshed, since sessions are saved
    # per-occurrence and the pattern alone doesn't disambiguate which
    # week a card reflects.
    last_imported_date = Column(Date, nullable=True)
    # ── Reconciliation against the live Google Calendar ──────────────
    # day_of_week/start_time are a PATTERN, and a pattern never ends: rolled
    # forward it promises a meeting every week forever, even after the team
    # cancelled the calendar event. These three columns hold what Google
    # actually said, so the app can stop inventing dates.
    #
    # ``calendar_state`` is "active" | "cancelled", or NULL for a meeting that
    # has never been reconciled (every manually-scheduled L10, and imported
    # ones until their manager next loads the board). NULL is deliberately NOT
    # "cancelled" — see rubik_core/eos/calendar_sync.py on why an unknown
    # answer must never read as a cancellation.
    calendar_state = Column(String, nullable=True)
    # Start of the next surviving occurrence per Google, naive UTC. Unlike the
    # pattern this survives a moved meeting or a skipped week.
    next_occurrence_at = Column(DateTime, nullable=True)
    # When the reconciliation above last ran, naive UTC. Lets the UI say how
    # fresh the answer is instead of implying it is live.
    calendar_checked_at = Column(DateTime, nullable=True)

    sessions = relationship(
        "L10Session",
        back_populates="meeting",
        cascade="all, delete-orphan",
    )
    manager = relationship("Person", foreign_keys=[manager_id])


class L10Session(Base, AuditMixin):
    __tablename__ = "eos_l10_sessions"

    meeting_id = Column(
        sa.Uuid,
        ForeignKey("eos_l10_meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    actual_start = Column(DateTime, default=datetime.utcnow, nullable=False)
    actual_end = Column(DateTime, nullable=True)
    average_rating = Column(Integer, nullable=True)
    notes = Column(Text)
    # Cached AI-generated narrative recap of the finished session. Generated
    # lazily on first view (and cached here so repeat views cost no LLM tokens);
    # a manager can force a regenerate. Null until first generated / for meetings
    # concluded before the feature shipped.
    recap = Column(Text, nullable=True)

    # ── Live-session cursor (server-authoritative shared state) ──────────
    # So every participant's browser renders the SAME agenda step and the
    # SAME countdown instead of each running its own local clock. The
    # facilitator (manager) writes these via POST /sessions/{id}/state;
    # everyone else reads them on the existing 2s poll.
    #
    # The timer is stored as an ANCHOR, not a ticking number:
    # ``step_started_at`` is the moment the current step began, and each
    # client derives ``remaining = AGENDA[step].duration - (now - anchor)``
    # locally. The server holds zero in-memory clock state, so every read
    # stays a stateless GET that grabs and releases one pooled connection —
    # the only realtime shape that survives a small database's connection
    # budget without exhausting it.
    #
    # ``state_version`` bumps on every step/timer change so a poller can
    # cheaply tell "something moved". ``is_paused`` is reserved for a
    # future pause/resume control (clients may honor it by freezing the
    # display); the current advance endpoint always re-anchors instead.
    current_step = Column(Integer, nullable=False, default=0, server_default="0")
    step_started_at = Column(DateTime, nullable=True)
    is_paused = Column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    state_version = Column(Integer, nullable=False, default=0, server_default="0")

    # True when the nightly sweep ended this session instead of a human
    # clicking Conclude (see rubik_core/eos/session_lifecycle.py). Sessions
    # were staying open for weeks because nobody concludes them, which left
    # the board offering "Rejoin" for meetings that were long over and let
    # the next occurrence silently resume a month-old row.
    #
    # It has to be distinguishable from a real Conclude: that path also
    # collects each attendee's 1..10 rating and derives ``average_rating``,
    # and a sweep cannot invent those. Without this flag an auto-closed
    # session is indistinguishable from a concluded one whose team simply
    # never rated it, and every average built on ``actual_end`` quietly
    # includes meetings nobody ever finished.
    auto_closed = Column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )

    # ── Post-meeting summary email (the recording-grounded recap we mail to
    # whoever actually attended). See backend/services/l10_summary_email.py.
    #
    # ``summary_email_sent_at`` is the idempotency key AND the audit trail: the
    # sweep runs every 15 minutes over the same window (a Gemini/Fathom
    # transcript lands minutes-to-hours after the room empties, so one pass at
    # Conclude time would almost always find no recording), and this stamp is
    # the only thing that stops a team getting the same recap four times an
    # hour. Stamped ONLY on a real send: a failed attempt deliberately leaves
    # it NULL so the next pass retries, until the session ages out of the
    # lookback window.
    summary_email_sent_at = Column(DateTime, nullable=True)
    # ``doc_id`` of the transcript the summary was built from — the "was this
    # meeting recorded" answer, kept so a recap can be traced back to the
    # recording that grounds it.
    summary_email_doc_id = Column(String, nullable=True)
    # Exactly who it went to, after the transcript-presence filter AND the L10
    # meeting's own ACL. Recorded because "why didn't I get the recap?" is the
    # question this feature will generate, and the answer is always this list.
    summary_email_recipients = Column(JSON, nullable=True)

    meeting = relationship("L10Meeting", back_populates="sessions")
    attendance = relationship(
        "L10Attendance",
        back_populates="session",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_eos_l10_sessions_meeting_start", "meeting_id", "actual_start"),
        # At most one LIVE session per meeting: two people opening the meeting
        # in the same instant would otherwise both insert one, and each room
        # would then poll its own session. Postgres only. On SQLite (tests,
        # local) the guard is the IntegrityError retry in ``start_session``,
        # which the tests exercise directly.
        Index(
            "uq_eos_l10_sessions_one_live_per_meeting",
            "meeting_id",
            unique=True,
            postgresql_where=sa.text("actual_end IS NULL AND is_deleted = false"),
        ).ddl_if(dialect="postgresql"),
    )


class L10Attendance(Base, AuditMixin):
    __tablename__ = "eos_l10_attendance"

    session_id = Column(
        sa.Uuid,
        ForeignKey("eos_l10_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=False)
    rating = Column(Integer)  # 1–10 — per-person meeting rating set at Conclude.
    # Free-text survey comment that rides with the per-person rating
    # at Conclude ("what worked / what to improve"). Distinct from the
    # check-in good-news fields below (the start-of-meeting Segue).
    rating_comment = Column(Text, nullable=True)
    attended = Column(Boolean, default=True, nullable=False)
    # Structured Check-in / Segue fields. Each attendee shares TWO short
    # "good news" lines at the start of the L10 — one Personal and one
    # Professional — each with a 0..10 rating. They live in separate column
    # pairs (not a single ``checkin_type`` slot) so a person's Personal and
    # Professional entries coexist instead of overwriting each other.
    checkin_personal_content = Column(Text, nullable=True)
    checkin_personal_rating = Column(Integer, nullable=True)  # 0..10
    checkin_professional_content = Column(Text, nullable=True)
    checkin_professional_rating = Column(Integer, nullable=True)  # 0..10

    session = relationship("L10Session", back_populates="attendance")


class L10Input(Base, AuditMixin):
    """Pre-meeting input — headlines and ideas.

    Scoped to a specific L10Meeting (the recurring slot) so any team
    member can add an item at any time before the next session starts.
    During the session, items surface in the corresponding section
    (Headlines step). After the session they can be marked
    ``discussed=True`` to fade them out without deleting history.

    A single table with a ``kind`` discriminator keeps queries simple
    and avoids two near-identical CRUD surfaces. The author is captured
    via the inherited ``created_by`` audit column.
    """

    __tablename__ = "eos_l10_inputs"

    meeting_id = Column(
        sa.Uuid,
        ForeignKey("eos_l10_meetings.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind = Column(
        Enum(L10InputKind, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    text = Column(Text, nullable=False)
    discussed = Column(
        Boolean, default=False, nullable=False, server_default=sa.false()
    )

    meeting = relationship("L10Meeting")

    __table_args__ = (Index("ix_eos_l10_inputs_meeting_kind", "meeting_id", "kind"),)


class L10Signal(Base, AuditMixin):
    """Ephemeral in-meeting broadcast event.

    Scoped to a single ``L10Session`` (not the recurring meeting) so
    polling clients can ask "what signals fired in this live session
    after timestamp X". Used today for the rabbit-hole instigator —
    when anyone presses the button, a row lands here, and every other
    client polling /signals?since=... picks it up on the next tick and
    plays the local animation.

    ``kind`` is a free-form discriminator so future broadcast events
    ("user joined", "manager paused") don't need another migration.
    """

    __tablename__ = "eos_l10_signals"

    session_id = Column(
        sa.Uuid,
        ForeignKey("eos_l10_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind = Column(String, nullable=False)

    __table_args__ = (
        Index("ix_eos_l10_signals_session_created", "session_id", "created_at"),
    )
