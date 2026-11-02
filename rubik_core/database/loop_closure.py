"""Loose Ends — the AE loop-closure ledger (``loop_items`` and friends).

A row in ``loop_items`` is a LOOSE END: something an account executive did or
was asked that the system of record does not show as finished. Three kinds
(``docs/loose_ends/README.md`` §1):

* ``unlogged`` — the AE emailed a client (or reported a client conversation in
  Slack) and nothing is in HubSpot for it.
* ``open_loop`` — a client wrote to the AE and is still waiting for an answer.
* ``todo`` — a promise Cubie heard, or a HubSpot task the AE owns, is past its
  date.

The list is DURABLE on purpose. The morning scan does not rebuild it from
scratch; it adds what is new and re-checks what is already open against
Gmail and HubSpot, so an item keeps its age across runs and nothing closes
because it merely fell out of the 14-day scan window. Closure is detected,
never self-reported: the only manual exit is "Not a real miss", which is
stored as a dismissal and counted as noise.

House rules inherited from ``rubik_core/database/promises.py`` and
``commitments.py``, on purpose:

* ``owner_email`` is lower-folded and has NO foreign key to ``users``. An AE
  who leaves keeps their history, and the offboarding sweep never has to
  cascade through this table.
* Instants are naive UTC (``datetime.utcnow``), like every other DateTime
  column in the repo.
* Vocabularies are strings with ``CHECK (col IN (...))``, never DB enums, so a
  new reason is a code change plus a migration rather than an ``ALTER TYPE``.
* **Message content never reaches a column anyone but the owner reads.**
  ``title`` (the subject / task subject / promise title) and ``headline`` (the
  AI one-liner) are shown only to the owner; ``detail`` and ``close_evidence``
  hold ids, flags and timestamps only — never a snippet, a body or a quote.
  The team view, the Slack digest and the scorecard read a whitelist of the
  other columns (``rubik_core/loop_closure/views.py``).

``rubik_core/loop_closure/store.py`` is the only writer of ``loop_items`` and
``loop_mutes``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base
from rubik_core.identifiers import LOOP_CLOSE_LOGGED_FROM_ASSISTANT

# ── Vocabularies ─────────────────────────────────────────────────────────────
# Duplicated as literals in alembic/versions/0001_cubie_baseline.py
# (a migration is a record of the shape that shipped), and re-exported by
# rubik_core/loop_closure/constants.py, which is what the rest of the code reads.

LOOP_KINDS: tuple[str, ...] = ("unlogged", "open_loop", "todo")
LOOP_SOURCES: tuple[str, ...] = ("gmail", "slack", "promise", "hubspot_task")
LOOP_STATUSES: tuple[str, ...] = ("open", "closed", "dismissed")

#: Why an item closed. Every one of them is DETECTED except
#: ``LOOP_CLOSE_LOGGED_FROM_ASSISTANT`` (the AE clicked "Log it in HubSpot",
#: which logged their own sent emails as HubSpot emails; a stored value, frozen
#: in ``rubik_core.identifiers``) — and ``aged_out``, which is
#: reported on its own and never counted as fixed. ``rescheduled`` (a promise
#: or HubSpot task whose date moved later, so it is no longer past its grace
#: days) is not counted as fixed either.
LOOP_CLOSE_REASONS: tuple[str, ...] = (
    "logged_in_hubspot",
    LOOP_CLOSE_LOGGED_FROM_ASSISTANT,
    "replied",
    "logged_follow_up",
    "task_completed",
    "task_gone",
    "promise_kept",
    "promise_withdrawn",
    "rescheduled",
    "aged_out",
)

#: What the judge said about a candidate it declined. One value today; a
#: string + CHECK so a second verdict is a code change, not an ALTER TYPE.
LOOP_JUDGEMENT_VERDICTS: tuple[str, ...] = ("declined",)

#: "Not a real miss" reasons. Each dismissal is noise, and the noise rate is
#: the pilot's success metric, so the reason is required.
LOOP_DISMISS_REASONS: tuple[str, ...] = (
    "not_a_client",
    "automated",
    "internal",
    "handled_elsewhere",
    "not_a_promise",
    "other",
)

LOOP_RUN_STATUSES: tuple[str, ...] = ("running", "completed", "failed")
LOOP_RUN_TRIGGERS: tuple[str, ...] = ("scheduler", "admin", "cli")


def _new_id() -> str:
    return uuid.uuid4().hex


def _fold(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower()


class LoopItem(Base):
    """One loose end, owned by one AE.

    ``(owner_email, kind, source_key)`` is the identity. ``source_key`` is
    shaped per kind (``gmail:<thread>`` for an unlogged email thread,
    ``gmail:<thread>:<message>`` for each unanswered inbound message,
    ``slack:<channel>:<ts>``, ``promise:<id>``, ``hstask:<id>``) so the same
    thing found again next morning lands on the same row. A dismissed row is
    never re-opened or re-flagged; a closed unlogged email thread re-opens
    only when a NEW unlogged AE email appears in it after it was closed
    (``reopened_count`` counts those).
    """

    __tablename__ = "loop_items"

    id = Column(String(32), primary_key=True, default=_new_id)
    owner_email = Column(String(320), nullable=False)

    kind = Column(String(16), nullable=False)
    source = Column(String(16), nullable=False)
    source_key = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False, default="open")

    # OWNER-ONLY. The subject / task subject / promise title, and the AI
    # one-liner. Never rendered on a team surface; hidden under impersonation.
    title = Column(String(300), nullable=False, default="")
    headline = Column(String(300), nullable=True)

    # Who and which account — CRM data and a person's name, which the team
    # surfaces may show.
    counterparty_email = Column(String(320), nullable=True)
    counterparty_label = Column(String(200), nullable=True)
    company_label = Column(String(200), nullable=True)
    hubspot_contact_id = Column(String(32), nullable=True)
    hubspot_company_id = Column(String(32), nullable=True)
    hubspot_deal_id = Column(String(32), nullable=True)

    thread_id = Column(String(64), nullable=True)  # Gmail thread id
    message_id = Column(String(128), nullable=True)  # open loop: the inbound one
    link = Column(String(500), nullable=True)  # Gmail / Slack / HubSpot, owner's

    # When the underlying thing happened (sent / received / due) — the age
    # clock. ``first_flagged_at`` is when a scan first put it on the list.
    opened_at = Column(DateTime, nullable=False)
    first_flagged_at = Column(DateTime, nullable=False)
    last_checked_at = Column(DateTime, nullable=False)

    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String(32), nullable=True)
    # Ids and a timestamp only: {"type": "notes", "id": "123", "at": "…"}.
    close_evidence = Column(JSON, nullable=True)

    dismissed_at = Column(DateTime, nullable=True)
    dismiss_reason = Column(String(32), nullable=True)
    dismissed_by = Column(String(320), nullable=True)

    reopened_count = Column(Integer, nullable=False, default=0)
    # Ids and flags only (judge mode, match kind, Slack channel id …) — never
    # message text.
    detail = Column(JSON, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_email", "kind", "source_key", name="uq_loop_items_owner_kind_key"
        ),
        # The page's read ("my open items") and the scan's re-check. Its
        # leading column also serves every owner-only lookup, so owner_email
        # carries no index of its own.
        Index("ix_loop_items_owner_status", "owner_email", "status"),
        # The health numbers and the aged-out sweep.
        Index("ix_loop_items_status_opened", "status", "opened_at"),
        CheckConstraint(in_check("kind", LOOP_KINDS), name="ck_loop_items_kind"),
        CheckConstraint(in_check("source", LOOP_SOURCES), name="ck_loop_items_source"),
        CheckConstraint(in_check("status", LOOP_STATUSES), name="ck_loop_items_status"),
        CheckConstraint(
            in_check("close_reason", LOOP_CLOSE_REASONS, nullable=True),
            name="ck_loop_items_close_reason",
        ),
        CheckConstraint(
            in_check("dismiss_reason", LOOP_DISMISS_REASONS, nullable=True),
            name="ck_loop_items_dismiss_reason",
        ),
        CheckConstraint(lower_check("owner_email"), name="ck_loop_items_owner_lower"),
        CheckConstraint(
            lower_check("counterparty_email", nullable=True),
            name="ck_loop_items_counterparty_lower",
        ),
        CheckConstraint(
            lower_check("dismissed_by", nullable=True),
            name="ck_loop_items_dismissed_by_lower",
        ),
    )

    @validates("owner_email")
    def _fold_owner(self, _key: str, value: str) -> str:
        """Fold on write. ``users.email`` is stored verbatim from the Google
        token, so every join back to it must fold BOTH sides."""
        return (value or "").strip().lower()

    @validates("counterparty_email", "dismissed_by")
    def _fold_optional(self, _key: str, value: str | None) -> str | None:
        return _fold(value) or None


class LoopScanRun(Base):
    """One morning scan: when, how it went, and what each AE's coverage was.

    Three jobs. "Last scanned at" on the page; the per-AE coverage that makes
    "could not look" visible instead of reading as "all clear"; and the run
    lock — a ``running`` row younger than 60 minutes blocks a second run, so
    a scheduler retry cannot scan everyone twice. ``report`` is the named
    buckets the run logged (counts and emails, never content). A dry run
    rolls back, so it never leaves a row here.
    """

    __tablename__ = "loop_scan_runs"

    id = Column(String(32), primary_key=True, default=_new_id)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(16), nullable=False, default="running")
    trigger = Column(String(16), nullable=False, default="cli")
    dry_run = Column(Boolean, nullable=False, default=False)
    report = Column(JSON, nullable=True)
    # {owner_email: Coverage} — see README §3.
    coverage = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_loop_scan_runs_status_started", "status", "started_at"),
        CheckConstraint(
            in_check("status", LOOP_RUN_STATUSES), name="ck_loop_scan_runs_status"
        ),
        CheckConstraint(
            in_check("trigger", LOOP_RUN_TRIGGERS), name="ck_loop_scan_runs_trigger"
        ),
    )


class LoopMute(Base):
    """ "Stop flagging this sender (or this domain) for me."

    Set from "Not a real miss" and removable from the page. ``pattern`` is a
    full address (``jane@acme.com``) or a domain with its at-sign
    (``@acme.com``), lower-folded. Per AE: one rep muting a vendor never hides
    it from another.
    """

    __tablename__ = "loop_mutes"

    id = Column(String(32), primary_key=True, default=_new_id)
    owner_email = Column(String(320), nullable=False)
    pattern = Column(String(320), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_by = Column(String(320), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_email", "pattern", name="uq_loop_mutes_owner_pattern"),
        CheckConstraint(lower_check("owner_email"), name="ck_loop_mutes_owner_lower"),
        CheckConstraint(lower_check("pattern"), name="ck_loop_mutes_pattern_lower"),
    )

    @validates("owner_email", "pattern")
    def _fold(self, _key: str, value: str) -> str:
        return (value or "").strip().lower()

    @validates("created_by")
    def _fold_actor(self, _key: str, value: str | None) -> str | None:
        return _fold(value) or None


class LoopJudgement(Base):
    """A candidate the judge already declined, so it is not asked about again.

    Without this, a declined candidate is "fresh" every morning and the oldest
    40 declined threads take every judge slot, so a newer real request waits
    days for a verdict. Keyed like ``loop_items`` (``owner_email, kind,
    source_key``); an open-loop key carries the inbound message id, so a NEW
    client message is a new key and is judged again. Holds ids and a time only
    — never the subject, snippet or the model's reasoning. Pruned by the scan
    once the candidate can no longer come back (older than the lookback).
    """

    __tablename__ = "loop_judgements"

    id = Column(String(32), primary_key=True, default=_new_id)
    owner_email = Column(String(320), nullable=False)
    kind = Column(String(16), nullable=False)
    source_key = Column(String(255), nullable=False)
    verdict = Column(String(16), nullable=False, default="declined")
    judged_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "owner_email", "kind", "source_key", name="uq_loop_judgements_owner_key"
        ),
        CheckConstraint(in_check("kind", LOOP_KINDS), name="ck_loop_judgements_kind"),
        CheckConstraint(
            in_check("verdict", LOOP_JUDGEMENT_VERDICTS),
            name="ck_loop_judgements_verdict",
        ),
        CheckConstraint(
            lower_check("owner_email"), name="ck_loop_judgements_owner_lower"
        ),
    )

    @validates("owner_email")
    def _fold_owner(self, _key: str, value: str) -> str:
        return (value or "").strip().lower()


class LoopDigestPost(Base):
    """The one-digest-a-day guard for the shared Slack channel.

    The UNIQUE ``(channel_key, local_date)`` IS the guard — never a pre-check,
    because two scheduler executions racing both pass a pre-check and both
    post. Write order, mirroring ``digest_logs``: insert and FLUSH to claim
    the slot, post, stamp ``posted_at`` / ``message_ts``, commit; on a failed
    post roll back, so the row is absent and a retry (or tomorrow) can post.
    A quiet day posts nothing and writes no row.

    ``channel_key`` keeps its case: Slack channel ids are upper-case and a
    folded id is a different string. ``local_date`` is an ISO ``YYYY-MM-DD``
    string in the org's business timezone (a channel has no recipient
    timezone of its own).
    """

    __tablename__ = "loop_digest_posts"

    id = Column(String(32), primary_key=True, default=_new_id)
    channel_key = Column(String(64), nullable=False)
    local_date = Column(String(10), nullable=False)
    posted_at = Column(DateTime, nullable=True)
    message_ts = Column(String(64), nullable=True)
    item_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "channel_key", "local_date", name="uq_loop_digest_posts_channel_date"
        ),
    )
