"""Deal defers — "snooze this deal until a follow-up date, with a note".

Long-term deals sit in the pipeline for months with no real close date, and
they keep showing up as noise on My Day, in the daily-rhythm checks and on the
morning priorities card. A defer takes a deal off every list Cubie shows the
REP until the follow-up date, when it comes back on its own. The manager views
do not hide it: they list every deferred deal with its owner, date and note, so
a defer cannot be used to bury a bad deal.

A row is one defer of one deal. The row is never deleted and never reused:

* ``ended_at`` NULL and ``follow_up_on`` after the reader's today → **active**.
* ``ended_at`` NULL and ``follow_up_on`` on or before today → **due**: the deal
  is back on the lists already. Nothing has to run for that to happen — every
  reader compares the date itself — so a missed job can never keep a deal
  hidden past its date.
* ``ended_reason`` says how an open row was closed: ``undeferred`` (somebody
  pressed Undefer), ``replaced`` (deferred again with a new date or note) or
  ``expired`` (a due row closed when the deal was deferred again).

**Why a new table, not ``priority_memory.snoozed_until`` or the promises
snooze.** Both were checked first. A priority
snooze is per person, per card kind, has no note, and is the denominator of the
Insights priority-engagement dashboards — a deal defer written there would be
counted as a priority snooze and would silence only one card kind for one
person. A promise snooze moves a to-do's due date. A deal defer is per DEAL
(every rep-facing list hides it, whoever pressed the button), carries a
required note and a HubSpot task, and must be readable across owners by the
manager views. None of that fits either column.

House rules, as in ``commitments.py`` and ``loop_closure.py``:

* Emails are lower-folded and have NO foreign key to ``users``: a defer made by
  a rep who later leaves is still the manager's evidence.
* ``follow_up_on`` is a DATE (the rep's calendar day), never a timestamp: a
  due date read as a UTC instant moves a day west of Greenwich.
* Instants are naive UTC.
* Vocabularies are strings with a CHECK, never DB enums.

The HubSpot task is written through the call wrap-up ledger
(``call_hubspot_writes``, keyed by ``idempotency_key``), so deferring twice
writes one task. ``hubspot_task_*`` here only remember the outcome for the UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base

# Duplicated as literals in alembic/versions/0001_cubie_baseline.py
# (a migration is a record of the shape that shipped).
END_REASONS: tuple[str, ...] = ("undeferred", "replaced", "expired")
TASK_STATUSES: tuple[str, ...] = ("pending", "written", "failed")


class DealDefer(Base):
    """One defer of one HubSpot deal. See the module docstring."""

    __tablename__ = "deal_defers"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    hubspot_deal_id = Column(String, nullable=False)
    #: The deal's name when it was deferred, so a manager view still reads
    #: when HubSpot cannot be reached.
    deal_name = Column(String, nullable=True)
    #: The deal's HubSpot owner (whose lists it leaves, whose task it is).
    owner_email = Column(String, nullable=False)
    #: Who pressed Defer. Usually the owner; an admin can act for them.
    deferred_by = Column(String, nullable=False)
    follow_up_on = Column(Date, nullable=False)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    ended_at = Column(DateTime, nullable=True)
    ended_by = Column(String, nullable=True)
    ended_reason = Column(String, nullable=True)

    hubspot_task_status = Column(String, nullable=False, default="pending")
    hubspot_task_id = Column(String, nullable=True)
    #: The friendly sentence from ``friendly_write_error`` — never a raw body.
    hubspot_task_error = Column(String, nullable=True)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        # At most one open defer per deal. The arbiter for a double-click: the
        # second insert fails here and re-reads the first, rather than a
        # pre-check both requests would pass.
        Index(
            "uq_deal_defers_open_deal",
            "hubspot_deal_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_deal_defers_owner_open", "owner_email", "ended_at"),
        Index("ix_deal_defers_follow_up_on", "follow_up_on"),
        CheckConstraint(
            in_check("ended_reason", END_REASONS, nullable=True),
            name="ck_deal_defers_ended_reason",
        ),
        CheckConstraint(
            in_check("hubspot_task_status", TASK_STATUSES),
            name="ck_deal_defers_task_status",
        ),
        CheckConstraint(lower_check("owner_email"), name="ck_deal_defers_owner_lower"),
        CheckConstraint(
            lower_check("deferred_by"), name="ck_deal_defers_deferred_by_lower"
        ),
    )

    @validates("owner_email", "deferred_by", "ended_by")
    def _fold(self, _key, value):
        return value.strip().lower() if isinstance(value, str) else value
