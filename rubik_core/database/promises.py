"""My To-Dos — the ``promises`` ledger.

A row here is a PROMISE: something a person said they would do, for someone,
by when. The user-facing name is "To-Do" (Homefeed, Chat, Slack, the /todos
page); the code name is *promise* because ``eos_todos`` / ``EntityKind.TODO``
already mean an EOS To-Do. Design: ``docs/todos_promises_design.md``.

Three rules inherited from ``rubik_core/database/commitments.py``, on purpose:

* ``owner_email`` is lower-folded and has NO foreign key. A departed teammate's
  ledger must survive their ``users`` row so the offboard sweep can hand the
  open promises to a successor.
* Dates are DATES. ``promised_for`` / ``due_local`` are calendar days in the
  owner's ``timezone`` (copied onto the row at creation, like
  ``scheduled_tasks``), never instants — reading a due date as UTC shifts it a
  day west of Greenwich, the bug the scorecard already paid for.
* Nothing here closes itself. ``evidence_found`` is as far as the system may
  move a row; ``kept`` is always a human's tap (Phase 1 adds the evidence
  matchers; Phase 0 ships the states so the vocabulary is settled).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship, validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base

# ── Vocabularies ─────────────────────────────────────────────────────────────

#: Lifecycle. ``suggested`` rows (Phase 1, heard from a transcript) never count
#: toward "waiting on you" until the owner accepts them.
PROMISE_STATUSES: tuple[str, ...] = (
    "suggested",
    "open",
    "slipping",
    "evidence_found",
    "kept",
    "dropped",
    "handed_off",
)

#: The statuses a list of "things I still owe" includes.
OPEN_PROMISE_STATUSES: tuple[str, ...] = ("open", "slipping", "evidence_found")

#: Where a promise came from. ``heard_*`` are Phase 1 extractor sources.
PROMISE_SOURCES: tuple[str, ...] = (
    "heard_transcript",
    "heard_email",
    "heard_slack",
    "priority_card",
    "chat",
    "slack_cmd",
    "page",
    "handed_off",
)

COUNTERPARTY_KINDS: tuple[str, ...] = ("person", "company", "deal")

OBJECT_KINDS: tuple[str, ...] = (
    "thread",
    "document",
    "deal",
    "event",
    "transcript",
    "person",
    "company",
)

PROMISE_EVENT_KINDS: tuple[str, ...] = (
    "created",
    "suggested",
    "accepted",
    "rejected",
    "due_moved",
    "snoozed",
    "slipping",
    "headsup_drafted",
    "headsup_sent",
    "renegotiated",
    "evidence_found",
    "kept_confirmed",
    "kept_denied",
    "reopened",
    "dropped",
    "handed_off",
    "promoted_eos",
    "made_private",
    "made_shared",
    "edited",
)


def _new_id() -> str:
    return uuid.uuid4().hex


class Promise(Base):
    """One thing the owner said they would do."""

    __tablename__ = "promises"

    id = Column(String, primary_key=True, default=_new_id)
    owner_email = Column(String, nullable=False, index=True)

    title = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="open", index=True)

    # The ORIGINAL promise, set once. ``due_local`` is the working date and may
    # move (every move is a ``due_moved`` event). Both are calendar days in
    # ``timezone``.
    promised_for = Column(Date, nullable=True)
    due_local = Column(Date, nullable=True)
    timezone = Column(String, nullable=False)  # IANA name, e.g. America/New_York

    # Who is waiting. NULL = "just you".
    counterparty_kind = Column(String, nullable=True)  # see COUNTERPARTY_KINDS
    counterparty_id = Column(String, nullable=True)  # email / hubspot id
    counterparty_label = Column(String, nullable=True)

    # What the promise is about — drives the card's primary verb.
    object_kind = Column(String, nullable=True)  # see OBJECT_KINDS
    object_id = Column(String, nullable=True)
    object_label = Column(String, nullable=True)

    source = Column(String, nullable=False)  # see PROMISE_SOURCES
    # {entity_kind, entity_id, quote, span, at, priority_id, session_id, ...}
    source_ref = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=True)  # heard_* only

    # Phase 1: what "kept" looks like, and what was seen. Kept nullable and
    # unused in Phase 0 so the vocabulary is settled before the matchers land.
    done_rule = Column(JSON, nullable=True)
    evidence = Column(JSON, nullable=True)

    snooze_count = Column(Integer, nullable=False, default=0)
    slipping_since = Column(DateTime, nullable=True)
    is_private = Column(Boolean, nullable=False, default=False)
    visible_to_counterparty = Column(Boolean, nullable=False, default=False)

    # Bridge to the EOS ledger when the owner makes it a team To-Do. Soft link
    # on purpose: an EOS row can be soft-deleted without touching this one.
    eos_todo_id = Column(String, nullable=True, index=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    kept_at = Column(DateTime, nullable=True)
    dropped_at = Column(DateTime, nullable=True)

    events = relationship(
        "PromiseEvent",
        back_populates="promise",
        cascade="all, delete-orphan",
        order_by="PromiseEvent.at",
    )

    __table_args__ = (
        # The list page's two reads: "what do I still owe" and "what is due".
        Index("ix_promises_owner_status", "owner_email", "status"),
        Index("ix_promises_owner_due", "owner_email", "due_local"),
        CheckConstraint(
            in_check("status", PROMISE_STATUSES), name="ck_promises_status"
        ),
        CheckConstraint(in_check("source", PROMISE_SOURCES), name="ck_promises_source"),
        CheckConstraint(
            in_check("counterparty_kind", COUNTERPARTY_KINDS, nullable=True),
            name="ck_promises_counterparty_kind",
        ),
        CheckConstraint(
            in_check("object_kind", OBJECT_KINDS, nullable=True),
            name="ck_promises_object_kind",
        ),
        CheckConstraint(lower_check("owner_email"), name="ck_promises_owner_lower"),
    )

    @validates("owner_email")
    def _fold_owner(self, _key: str, value: str) -> str:
        """Fold on write. ``users.email`` is stored verbatim from the Google
        token, so every join back to it must fold BOTH sides."""
        return (value or "").strip().lower()

    @validates("counterparty_id")
    def _fold_counterparty(self, _key: str, value: str | None) -> str | None:
        """A person counterparty is an email; fold it so two-sided lookups
        (Phase 3) match. Non-email ids are unaffected by lower()."""
        if value is None:
            return None
        value = value.strip()
        return value.lower() if "@" in value else value


class PromiseEvent(Base):
    """The ledger: every transition, who made it, and why.

    This is what the weekly kept-rate and the acceptance-rate kill rule read.
    Append-only by convention — nothing updates or deletes a row here.
    """

    __tablename__ = "promise_events"

    id = Column(String, primary_key=True, default=_new_id)
    promise_id = Column(
        String,
        ForeignKey("promises.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # A user's email (lower-folded) or the literal "system".
    actor = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # see PROMISE_EVENT_KINDS
    payload = Column(JSON, nullable=True)

    promise = relationship("Promise", back_populates="events")

    __table_args__ = (
        Index("ix_promise_events_promise_at", "promise_id", "at"),
        CheckConstraint(
            in_check("kind", PROMISE_EVENT_KINDS), name="ck_promise_events_kind"
        ),
    )

    @validates("actor")
    def _fold_actor(self, _key: str, value: str) -> str:
        value = (value or "").strip()
        return value.lower() if "@" in value else value


class PromiseScan(Base):
    """The memory of every source the promise listener has already read.

    One row per (owner, source): a meeting transcript, an L10 session's notes,
    a day of sent email, a day of Slack. The nightly pass reads a source once
    and never again — this is what keeps the cost to one cheap call per
    meeting, and what stops a rejected suggestion from being re-heard the next
    night. ``found`` is how many promises that read produced (zero is the
    common, useful answer).
    """

    __tablename__ = "promise_scans"

    id = Column(String, primary_key=True, default=_new_id)
    owner_email = Column(String, nullable=False)
    # "transcript:<doc_id>" | "l10:<session_id>" | "gmail:<thread_id>"
    # | "slack:<channel>:<ts>"
    source_key = Column(String, nullable=False)
    scanned_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    found = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index(
            "uq_promise_scans_owner_source", "owner_email", "source_key", unique=True
        ),
        CheckConstraint(
            lower_check("owner_email"), name="ck_promise_scans_owner_lower"
        ),
    )

    @validates("owner_email")
    def _fold_owner(self, _key: str, value: str) -> str:
        return (value or "").strip().lower()
