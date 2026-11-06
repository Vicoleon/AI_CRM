"""Insights telemetry — ``activity_daily`` and ``priority_events``.

The Insights dashboards (``docs/insights/README.md``) read almost everything
from tables other features already write. These two exist because nothing
else records the facts they hold:

* ``activity_daily`` — one row per (person, business calendar day, product
  area), counting the authenticated API requests that person made in that
  area. Before it there was no usage log at all: ``login_events`` is written
  only on the Google sign-in path and is debounced to one row per 30 minutes,
  so "who used Cubie, for what, on which day" could not be answered. Written by
  the buffered recorder in ``rubik_core/insights/activity.py`` (an HTTP
  middleware), never per request: the recorder folds a burst of requests into
  one upsert per row.
* ``priority_events`` — an append-only log of what happened to an AI Priorities
  card: resolved, dismissed, snoozed, reopened, auto-resolved by evidence, the
  "acted on" action (a Slack follow-up) and
  Slack DM delivery. ``priority_memory`` is upserted and keeps only the latest
  state, so without this log there is no history of the funnel.

House rules, the same as ``loop_closure.py`` and ``promises.py``:

* Emails are lower-folded and have NO foreign key to ``users``: a person who
  leaves keeps their history, and offboarding never cascades through here.
* Instants are naive UTC. ``activity_daily.day`` is the calendar day in the
  company's business time zone (UTC until an admin sets one), computed at
  write time, because that is the day the
  dashboards bucket by.
* ``department_slug`` is a SNAPSHOT of the person's department at write time
  (``slugify_department``), so a later department change does not rewrite
  history. Older sources resolve department at read time instead.
* ``area`` and ``kind`` are deliberately NOT CHECK-constrained. They are
  telemetry vocabularies that grow with the product; a CHECK would turn every
  new area into an ``ALTER TABLE`` on a table that is written on every
  request, which is the lock risk that can take the whole service down. The
  vocabularies live in code (``rubik_core/insights/activity.py``
  ``AREAS`` and ``rubik_core/insights/priority_events.py`` ``KINDS``) and
  the writers reject anything else.
* No content. Neither table holds a title, a message, a prompt or a reply,
  only ids, counts and timestamps.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import validates

from rubik_core.database.constraints import lower_check
from rubik_core.database.models import Base


class ActivityDaily(Base):
    """Requests per person, per business day, per product area."""

    __tablename__ = "activity_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_email = Column(String, nullable=False)
    day = Column(Date, nullable=False)
    area = Column(String, nullable=False)
    department_slug = Column(String, nullable=True)
    requests = Column(Integer, nullable=False, default=0)
    first_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "user_email", "day", "area", name="uq_activity_daily_user_day_area"
        ),
        CheckConstraint(
            lower_check("user_email"), name="ck_activity_daily_email_lower"
        ),
        # The dashboards' dominant read is "every row in a date window".
        Index("ix_activity_daily_day", "day"),
    )

    @validates("user_email")
    def _fold_email(self, _key: str, value: str) -> str:
        return (value or "").strip().lower()


class PriorityEvent(Base):
    """One thing that happened to one AI Priorities card."""

    __tablename__ = "priority_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_email = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    # The card's stable id inside the day's bundle (``priorities[].id``) when
    # the event came from a card action; NULL for entity-wide transitions.
    priority_id = Column(String, nullable=True)
    entity_kind = Column(String, nullable=True)
    entity_id = Column(String, nullable=True)
    priority_kind = Column(String, nullable=True)
    # Who caused it: the person's email, or "system" for auto-resolution and
    # delivery.
    actor = Column(String, nullable=True)
    department_slug = Column(String, nullable=True)
    at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            lower_check("user_email"), name="ck_priority_events_email_lower"
        ),
        Index("ix_priority_events_at", "at"),
        Index("ix_priority_events_user_at", "user_email", "at"),
    )

    @validates("user_email")
    def _fold_email(self, _key: str, value: str) -> str:
        return (value or "").strip().lower()
