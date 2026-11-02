"""Cubie Sales Cockpit tables — commitments, stage lag, nudges, SLAs, digests.

Persistence for the Sales Cockpit. The state machine
that drives these rows lives in ``rubik_core/commitments/``; this module is
columns and constraints only, in the same mainstream Cubie style as
``rubik_core/database/call_capture.py`` and
``rubik_core/database/smart_teams.py``: uuid4-hex ``String`` PKs generated at
the call site, Python-side ``datetime.utcnow`` defaults, ``JSON`` payloads, and
``String`` + a module-level constant tuple + a portable ``CheckConstraint``
rather than a native enum. No ``JSONB``, no ``DateTime(timezone=True)``, no
``postgresql_where`` — CI runs ``alembic upgrade head`` against
``sqlite:///./test.db`` and every test builds its schema on SQLite.

**Cubie owns the clock, and that is the whole point of the feature.** A HubSpot
stage timestamp may be read for diagnostics, and may seed ``opened_at`` exactly
once in the one-off go-live backfill. It is never the aging clock. Any
recurring code that computes ``now - date_entered_stage`` and compares it to an
SLA has rebuilt the thing this design exists to replace: the stage clock
restarts when a deal moves, so the oldest stalls are the ones that look
freshest. ``Commitment.opened_at`` is the clock, Cubie sets it, and it is
back-dated when better evidence arrives rather than overwritten forward.

**Silence is a feature.** ``digest_logs`` is not only a send guard, it is the
answer to "I never got told" — a record, per person, per kind, per local day.
No sales notification path may bypass it except the one carve-out the spec
names (the end-of-call wrap-up prompt to the call's own owner). That rule
erodes the moment somebody adds a "quick" second notifier, which is why the
guard is a database constraint rather than a convention.

**Email case.** ``users.email`` is stored verbatim from the Google ID token and
is not reliably lower-cased. Every identity column here is folded by a
``@validates`` hook and pinned by a ``lower_check`` CHECK for the writers that
bypass instance attributes. There is deliberately NO foreign key to
``users.email``: offboarding DELETES that row, and "no users row" is the
load-bearing departed signal. A commitment somebody left behind must survive
them leaving — that row is precisely what the successor handoff needs to
inherit.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship, validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base

# ── Column vocabularies, enforced in the database ─────────────────────────────
# Every read in the cockpit filters on exactly these values, so a value outside
# the tuple does not raise — it removes a row from the only view that would
# have shown it. A commitment with a misspelled state ages forever and appears
# on nobody's screen, which is the exact failure the feature was built to fix.

# Only these three kinds have an opener. A kind with no opener is a
# column value that can never appear, and a view that filters for it ships
# permanently empty.
COMMITMENT_KINDS: tuple[str, ...] = ("sow", "wrapup", "lead_touch")

# The escalation ladder, in order. "provisional" means the clock runs but
# escalation is DISARMED — an AI-extracted commitment nobody has confirmed
# never pages a manager. "accepted" is a recorded, reasoned acceptance of a
# breach and is TERMINAL until close: without treating it as terminal the
# recompute sweep re-escalates it every 15 minutes forever.
COMMITMENT_STATES: tuple[str, ...] = (
    "provisional",
    "open",
    "at_risk",
    "breached",
    "escalated",
    "exec_visible",
    "accepted",
    "closed",
)

# "stage_backfill" is the one-off go-live script ONLY. There is deliberately no
# live stage-sourced opener: a poller sees a deal's CURRENT stage and cannot
# know when it entered, so every already-stuck deal would open with an age of
# zero — hiding exactly the stall this feature exists to catch.
COMMITMENT_OPENED_SOURCES: tuple[str, ...] = (
    "call",
    "manual",
    "stage_backfill",
)

# Deliberately only two. There is no "email" close source and no "stage" close
# source, and that is the gate-4 guarantee: sent-mail evidence SUGGESTS a close
# and a human confirms it, which records "manual". A wrong auto-close is worse
# than no auto-close — it hides a real breach, which is the precise failure the
# first problem statement exists to fix. "superseded" is the merge case, where
# a company-scoped commitment folds into the deal-scoped one that later
# appeared for the same work.
COMMITMENT_CLOSED_SOURCES: tuple[str, ...] = ("manual", "superseded")

# A stage-lag exception's lifecycle. "rejected" is the tuning signal: the
# "Not right" rate per signal type is the honest measure of whether the
# detector is helping, and a detector that cries wolf teaches the AE to dismiss
# without reading.
STAGE_LAG_STATUSES: tuple[str, ...] = ("open", "applied", "rejected")

# "close_date_rollup" is its own kind on purpose rather than reusing "weekly":
# the guard is UNIQUE(recipient_email, kind, local_date), so two different
# weekly digests sharing a kind would suppress each other for any recipient who
# gets both on the same local day — non-deterministically, depending on which
# scheduler job ran first. That is the exact failure this column exists to
# prevent.
DIGEST_KINDS: tuple[str, ...] = ("daily", "weekly", "close_date_rollup")


class Commitment(Base):
    """One thing somebody owes somebody, with a clock Cubie owns.

    Opened by one of the sources in ``COMMITMENT_OPENED_SOURCES``, aged by the
    recompute sweep, closed by a human. See the module docstring for why the
    clock is not HubSpot's.

    **Idempotency is the two partial unique constraints below**, not caller
    discipline. When two sources fire for the same SOW, the first creates the
    row, the second attaches its evidence to it, and the EARLIEST
    ``opened_at`` wins — back-dating matters because a later source usually
    arrives after the call, and the call is when the client actually asked.

    ``state`` is the semantic field a reader should render.
    ``is_live_deal_scoped`` / ``is_live_company_scoped`` are enforced
    projections of "this row is live, at this scope" and exist only to make the
    dedupe constraints expressible in both SQLite and Postgres; call
    ``refresh_live_scope_flags()`` after any write that touches ``closed_at``,
    ``hubspot_deal_id`` or ``hubspot_company_id``, and the CHECK constraints
    will fail loudly if you forget rather than silently permitting a duplicate.
    """

    __tablename__ = "commitments"

    id = Column(String, primary_key=True)  # uuid4 hex
    kind = Column(String, nullable=False, index=True)  # see COMMITMENT_KINDS

    # Both nullable, and the deal one especially so: a commitment can precede
    # the deal. A CCS call-sourced commitment is opened from a conference record
    # before anything has resolved which company — let alone which deal — was on
    # the call. Requiring either would drop the highest-value rows.
    hubspot_deal_id = Column(String, nullable=True, index=True)
    hubspot_company_id = Column(String, nullable=True, index=True)

    # The AE. ``owner_email``, not ``owner_user_id``: the users PK *is* the
    # email. Lower-folded, and with no FK — see the module docstring on
    # offboarding. There is deliberately no ``hubspot_owner_id`` column either;
    # mapping a Cubie user to their HubSpot owner id is a runtime lookup with a
    # TTL cache, because a stored copy goes stale into
    # "this AE produces zero signals, with no error".
    owner_email = Column(String, nullable=False, index=True)

    # NULLABLE, deliberately. A backfilled commitment with no usable date shows
    # "age unknown — when did this start?" with a date picker, never age zero.
    # Unknown shown as unknown beats unknown shown as zero: an age of zero on a
    # deal that has been stuck for five months is a lie the cockpit would be
    # telling on its first day. A row with a NULL ``opened_at`` does not age and
    # does not escalate.
    opened_at = Column(DateTime, nullable=True)
    opened_source = Column(String, nullable=False)  # see COMMITMENT_OPENED_SOURCES
    # The transcript entry id / stage id this was opened from.
    opened_evidence_ref = Column(String, nullable=True)
    opened_evidence_text = Column(Text, nullable=True)

    # standard | non_standard | NULL. Half of the SLA lookup key, which is why
    # ``commitment_sla_configs.sow_kind`` uses an explicit "any" sentinel rather
    # than NULL — see that class.
    sow_kind = Column(String, nullable=True)
    # The AE's own ETA, and it OVERRIDES the generic SLA when it is later: a
    # three-week RFP is not late on day four. An ETA earlier than the SLA does
    # not shorten the clock, because an AE over-promising to a client should not
    # manufacture an internal breach.
    promised_at = Column(DateTime, nullable=True)
    promised_reason = Column(Text, nullable=True)

    # A SNAPSHOT taken at open, in BUSINESS hours, where one business day is 8
    # hours — so "3 business days" is 24 here, not 72. Getting that conversion
    # wrong flips "breached" to "fine" on the flagship commitment.
    #
    # NOT NULL and never re-read at display time. If an SLA is retuned in
    # September, August's open commitments keep the clock they were opened
    # under. A system that looks the SLA up at render time instead produces one
    # morning where every row turns red at once, and a cockpit that cried wolf
    # on its first Monday never recovers its credibility — which costs more than
    # any wrong number it might have been correcting.
    sla_hours = Column(Integer, nullable=False)
    # Computed at open from (opened_at, sla_hours, promised_at); recomputed when
    # promised_at changes or opened_at is back-dated. NULL exactly when
    # ``opened_at`` is NULL, which is what makes "age unknown does not
    # escalate" a data property rather than a branch every reader must remember.
    breach_at = Column(DateTime, nullable=True, index=True)
    # The FIRST instant this commitment's clock crossed ``breach_at``.
    # WRITE-ONCE: stamped by the aging sweep the first time it sees the crossing,
    # never cleared, never moved — not when ``promised_at`` later pushes
    # ``breach_at`` past it, not when ``opened_at`` is back-dated, and not on
    # close.
    #
    # It has to be its own column because nothing else in the row can answer
    # "was this ever late?" after the fact. ``breach_at`` is a MOVING TARGET: the
    # ETA endpoint recomputes it whenever an AE revises a promise, so a
    # commitment that breached in August and then had its ETA pushed to
    # September has a ``breach_at`` in the future. And ``state`` is OVERWRITTEN
    # with "closed" the moment the work lands, so the ladder tier is gone too.
    # Without this stamp, a real breach that was revised and then closed reports
    # as never-late — silently deleting the exact miss the reporting that
    # justifies this feature is built to count. See
    # ``rubik_core.cockpit.commitment_engine.was_breached``.
    breached_at = Column(DateTime, nullable=True)
    exec_visible_at = Column(DateTime, nullable=True)

    closed_at = Column(DateTime, nullable=True)
    closed_source = Column(String, nullable=True)  # see COMMITMENT_CLOSED_SOURCES
    closed_evidence_ref = Column(String, nullable=True)

    state = Column(
        String, nullable=False, default="open", index=True
    )  # see COMMITMENT_STATES
    # Required when a breach is accepted, and enforced below. "Accepted" is a
    # person taking responsibility for a missed commitment; an acceptance with
    # no reason is indistinguishable from someone clearing a red row off their
    # screen.
    accepted_reason = Column(Text, nullable=True)
    escalated_at = Column(DateTime, nullable=True)
    escalated_to = Column(String, nullable=True)

    # ── enforced projections; True or NULL only, never False ─────────────────
    # These are the portable partial-unique-index mechanism. SQL treats NULLs as
    # distinct, so a closed row (flag NULL) never collides with anything, and a
    # unique constraint over (scope id, kind, flag) becomes "at most one LIVE
    # commitment per scope per kind" in SQLite and Postgres alike, with no
    # ``postgresql_where``. Storing False instead of NULL would invert each
    # constraint into "at most one CLOSED row per scope", which starts rejecting
    # legitimate second commitments; the validator coerces falsy to NULL for
    # that reason.
    is_live_deal_scoped = Column(Boolean, nullable=True)
    is_live_company_scoped = Column(Boolean, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    nudges = relationship(
        "CommitmentNudge",
        back_populates="commitment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # ── TWO partial unique constraints, not one. ─────────────────────────
        # SQL treats NULLs as distinct, so a single constraint on
        # (hubspot_deal_id, kind) does NOT dedupe a commitment opened before a
        # deal exists — and that is the CCS call-sourced case, i.e. the most
        # important one. Without the second constraint, every re-run of the call
        # sweep opens another live SOW commitment for the same company and the
        # AE's My Day fills with copies of one obligation.
        #
        # Deal-scoped: at most one live commitment per (deal, kind).
        UniqueConstraint(
            "hubspot_deal_id",
            "kind",
            "is_live_deal_scoped",
            name="uq_commitments_live_deal_kind",
        ),
        # Company-scoped, and only for rows that have NO deal yet: at most one
        # live commitment per (company, kind). When a deal id later arrives, the
        # opener back-fills it and the row moves from this constraint to the one
        # above; if a deal-scoped live row already exists, the two merge and the
        # company-scoped one closes as "superseded".
        UniqueConstraint(
            "hubspot_company_id",
            "kind",
            "is_live_company_scoped",
            name="uq_commitments_live_company_kind",
        ),
        # The True-or-NULL rule the constraints above stand on.
        CheckConstraint(
            "is_live_deal_scoped IS NULL OR is_live_deal_scoped",
            name="ck_commitments_live_deal_true_or_null",
        ),
        CheckConstraint(
            "is_live_company_scoped IS NULL OR is_live_company_scoped",
            name="ck_commitments_live_company_true_or_null",
        ),
        # And the rule that makes the flags mean what their names say, in BOTH
        # directions. Permitting the flag to be absent on a live row would let
        # any writer that forgot ``refresh_live_scope_flags()`` slip a duplicate
        # past the constraints above — silently, since nothing would error.
        CheckConstraint(
            "(is_live_deal_scoped IS NOT NULL "
            "AND closed_at IS NULL AND hubspot_deal_id IS NOT NULL) "
            "OR (is_live_deal_scoped IS NULL "
            "AND (closed_at IS NOT NULL OR hubspot_deal_id IS NULL))",
            name="ck_commitments_live_deal_flag_consistent",
        ),
        CheckConstraint(
            "(is_live_company_scoped IS NOT NULL AND closed_at IS NULL "
            "AND hubspot_deal_id IS NULL AND hubspot_company_id IS NOT NULL) "
            "OR (is_live_company_scoped IS NULL "
            "AND (closed_at IS NOT NULL OR hubspot_deal_id IS NOT NULL "
            "OR hubspot_company_id IS NULL))",
            name="ck_commitments_live_company_flag_consistent",
        ),
        # An acceptance is a person taking the hit on the record. Without a
        # reason it is just a way to make a red row go away, and the reporting
        # that justifies this feature's existence loses the only column that
        # explains a miss.
        CheckConstraint(
            "state <> 'accepted' OR accepted_reason IS NOT NULL",
            name="ck_commitments_accepted_needs_reason",
        ),
        # A zero or negative snapshot makes a commitment breached the instant it
        # opens, and a bad SLA config row would do that to every commitment of
        # that kind at once.
        CheckConstraint("sla_hours > 0", name="ck_commitments_sla_hours_positive"),
        CheckConstraint(lower_check("owner_email"), name="ck_commitments_owner_lower"),
        CheckConstraint(
            lower_check("escalated_to"), name="ck_commitments_escalated_to_lower"
        ),
        CheckConstraint(in_check("kind", COMMITMENT_KINDS), name="ck_commitments_kind"),
        CheckConstraint(
            in_check("state", COMMITMENT_STATES), name="ck_commitments_state"
        ),
        CheckConstraint(
            in_check("opened_source", COMMITMENT_OPENED_SOURCES),
            name="ck_commitments_opened_source",
        ),
        CheckConstraint(
            in_check("closed_source", COMMITMENT_CLOSED_SOURCES, nullable=True),
            name="ck_commitments_closed_source",
        ),
        # My Day, and the manager roll-up once the EOS scope resolves a team to
        # a set of owner addresses.
        Index("ix_commitments_owner_state", "owner_email", "state"),
        # Reads fold with ``func.lower()``; on Postgres that expression cannot
        # use the plain column index, so My Day would degrade to a sequential
        # scan as history accumulates.
        Index("ix_commitments_owner_email_lower", func.lower(owner_email)),
    )

    def refresh_live_scope_flags(self) -> None:
        """Recompute the two enforced projections from the columns they mirror.

        Call this after any write that touches ``closed_at``,
        ``hubspot_deal_id`` or ``hubspot_company_id`` — opening, back-filling a
        deal id, closing, superseding. The flags cannot be derived by
        ``@validates`` because each depends on three columns whose assignment
        order the ORM does not fix, and a hook that fired mid-assignment would
        compute the flag from a half-built row.

        Forgetting to call this is safe in the only way that matters: the
        consistency CHECKs reject the row, so the failure is an
        ``IntegrityError`` at flush rather than a duplicate live commitment
        nobody notices until an AE reports seeing the same obligation twice.
        """
        live = self.closed_at is None
        self.is_live_deal_scoped = True if live and self.hubspot_deal_id else None
        self.is_live_company_scoped = (
            True
            if live and not self.hubspot_deal_id and self.hubspot_company_id
            else None
        )

    @validates("is_live_deal_scoped", "is_live_company_scoped")
    def _only_true_or_null(self, _key: str, value: object) -> bool | None:
        """Collapse anything falsy to ``NULL`` so the unique constraints hold.

        Both constraints rely on non-live rows carrying NULL. A writer that
        stored ``False`` would turn each one into "at most one non-live row per
        scope" and start rejecting the second legitimate commitment on a deal
        that has already had one.
        """
        return True if value else None

    @validates("owner_email", "escalated_to")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the identity columns to lower case on every write.

        ``users.email`` is stored verbatim from the Google token, so a
        commitment written with any other casing never appears in its owner's
        My Day and never reaches their manager's roll-up — it ages, unseen, in a
        table that exists to make things visible. Reads still use
        ``func.lower()``.
        """
        if value is None:
            return None
        return value.strip().lower()


class CommitmentStageLag(Base):
    """A deal whose HubSpot stage disagrees with what Cubie has evidence for.

    Emitted only on DISAGREEMENT, with the quote: a CCS ``stage_signal`` from a
    recent call, or sent-mail evidence that the SOW went out while the stage
    still says Drafting. This is the half of the cockpit that
    fixes the pipeline's accuracy rather than its speed, and it is the half that
    dies quietest — if the extractor is allowed to emit free-text signals
    instead of the closed enum the detector maps, every lookup misses, zero
    exceptions are produced, and nothing errors.

    ``evidence_stage_id`` is nullable because not every signal maps to a stage
    on this portal ("verbal agreement" has none). An exception with no target
    stage is still worth surfacing — it just cannot offer a one-click apply.

    There is deliberately NO uniqueness constraint here. The same deal can
    legitimately accumulate a second exception after a first is rejected, and
    the detector's own confidence threshold plus an "is one already open?" check
    is the right place for suppression. ``ix_commitment_stage_lags_deal_status``
    exists so that check is cheap enough that nobody is tempted to skip it.
    """

    __tablename__ = "commitment_stage_lags"

    id = Column(String, primary_key=True)  # uuid4 hex
    hubspot_deal_id = Column(String, nullable=False, index=True)
    hubspot_stage_id = Column(String, nullable=False)  # what HubSpot says
    evidence_stage_id = Column(String, nullable=True)  # what Cubie believes
    evidence_text = Column(Text, nullable=True)
    evidence_ref = Column(String, nullable=True)
    # Threshold starts at 0.7 and is tuned from the "Not right" rate. Stored per
    # row so the tuning can be done against real rejections rather than
    # remembered.
    confidence = Column(Float, nullable=True)

    status = Column(
        String, nullable=False, default="open", index=True
    )  # see STAGE_LAG_STATUSES
    resolved_by = Column(String, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_commitment_stage_lags_deal_status", "hubspot_deal_id", "status"),
        CheckConstraint(
            in_check("status", STAGE_LAG_STATUSES),
            name="ck_commitment_stage_lags_status",
        ),
        CheckConstraint(
            lower_check("resolved_by"),
            name="ck_commitment_stage_lags_resolved_by_lower",
        ),
    )

    @validates("resolved_by")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the resolver's address — see ``Commitment``."""
        if value is None:
            return None
        return value.strip().lower()


class CommitmentNudge(Base):
    """One "this is late, please look at it" sent about one commitment.

    A nudge is three writes and all three are required: a HubSpot task assigned
    to the AE and associated to the deal, a Slack DM linking to the cockpit row
    (never quoting transcript text — client-confidential content does not belong
    in Slack), and this row.

    This row is what makes the escalation rule enforceable: **two nudges with no
    acknowledgement is an escalation, not a third nudge.** Escalation changes
    who is accountable; it does not increase volume. Counting sent nudges per
    commitment is the query behind that rule, and it needs
    ``acknowledged_at`` — a nudge somebody acknowledged and then did not act on
    is a different situation from two nudges into the void.

    ``sent_at`` is stamped only after a confirmed send, for the same reason
    ``digest_logs`` does it: a row claiming a nudge that never left the building
    is worse than no row, because it is exactly the evidence someone will cite
    when the AE says nobody told them.
    """

    __tablename__ = "commitment_nudges"

    id = Column(String, primary_key=True)  # uuid4 hex
    commitment_id = Column(
        String,
        ForeignKey("commitments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_email = Column(String, nullable=False)
    to_email = Column(String, nullable=False)
    channel = Column(String, nullable=False)  # slack | hubspot_task | both
    hubspot_task_id = Column(String, nullable=True)

    sent_at = Column(DateTime, nullable=True)  # only after a confirmed send
    acknowledged_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    commitment = relationship("Commitment", back_populates="nudges")

    __table_args__ = (
        # "How many acknowledged nudges does this commitment have?" — the
        # two-nudges-then-escalate rule, as one indexed read.
        Index(
            "ix_commitment_nudges_commitment_ack", "commitment_id", "acknowledged_at"
        ),
        CheckConstraint(
            lower_check("from_email"), name="ck_commitment_nudges_from_lower"
        ),
        CheckConstraint(lower_check("to_email"), name="ck_commitment_nudges_to_lower"),
    )

    @validates("from_email", "to_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold both identity columns — see ``Commitment``."""
        if value is None:
            return None
        return value.strip().lower()


class CommitmentSlaConfig(Base):
    """The SLA table, in BUSINESS hours, with placeholders labelled as such.

    ``hours`` is business hours where one business day is 8 — "3 business days"
    is 24, not 72 — matching ``Commitment.sla_hours``, which snapshots from
    here. The two units must stay identical; a mismatch turns "breached" into
    "fine" on the flagship commitment and nothing errors.

    ``is_placeholder`` defaults TRUE and the UI must render placeholder-derived
    numbers as visibly provisional until an owner confirms them. Every number
    this table ships with came from a current-state map, not from a person
    agreeing to it, and a number nobody agreed to presented as agreed is how a
    working system gets called wrong in its first review.

    ``sow_kind`` is NOT NULL with an explicit ``"any"`` sentinel rather than
    NULL for the kinds that have no SOW variant. That is not cosmetic:
    ``uq_commitment_sla_configs_kind`` is only a real constraint when neither
    column can be NULL, because SQL treats NULLs as distinct — the same trap the
    two partial constraints on ``Commitment`` exist to dodge. With NULLs
    permitted here, two rows for ``("wrapup", NULL)`` coexist happily and the
    SLA a commitment snapshots depends on row order.
    """

    __tablename__ = "commitment_sla_configs"

    id = Column(String, primary_key=True)  # uuid4 hex
    kind = Column(String, nullable=False)  # see COMMITMENT_KINDS
    # standard | non_standard | any — see the class docstring on the sentinel.
    # Resolvers coerce a caller's ``None`` to "any" before looking a row up.
    sow_kind = Column(String, nullable=False, default="any")
    hours = Column(Integer, nullable=False)  # BUSINESS hours; 8 = one day
    is_placeholder = Column(Boolean, nullable=False, default=True)
    updated_by = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("kind", "sow_kind", name="uq_commitment_sla_configs_kind"),
        CheckConstraint(
            in_check("kind", COMMITMENT_KINDS), name="ck_commitment_sla_configs_kind"
        ),
        CheckConstraint("hours > 0", name="ck_commitment_sla_configs_hours_positive"),
        CheckConstraint(
            lower_check("updated_by"), name="ck_commitment_sla_configs_updated_lower"
        ),
    )

    @validates("updated_by")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the editor's address — see ``Commitment``."""
        if value is None:
            return None
        return value.strip().lower()


class DigestLog(Base):
    """One digest sent to one person, of one kind, on one local calendar day.

    The one-per-kind-per-person-per-day guard, and the audit trail for "I never
    got told". There is no other per-day send guard anywhere in the Cubie Slack
    path — the only thing preventing duplicates today is Cloud Scheduler firing
    each job once — so this table is the mechanism, not a convenience.

    **The ``kind`` column is load-bearing and it is in the unique key for a
    reason.** Without it, Monday's weekly roll-up and Monday's daily digest
    suppress each other, and which one survives depends on which scheduler job
    happens to run first. That is a bug that reproduces one day a week,
    non-deterministically, and looks like "the digest is flaky".

    ``local_date`` is an ISO ``YYYY-MM-DD`` STRING, not a ``Date``, derived
    from ``rubik_core.priorities.temporal.user_today_str``. The day that
    matters is the RECIPIENT's local today; keying on UTC gives people in the
    evening a second digest and people early in the morning none, with no
    timezone ambiguity to blame across SQLite and Postgres.

    **Treat the UNIQUE constraint as the guard.** Do not pre-check: two
    scheduler executions racing both pass a pre-check and both send. The write
    order that makes this exactly-once, mirroring
    ``eos_l10_sessions.summary_email_sent_at``: insert and FLUSH the row to
    claim the slot inside the transaction, send, stamp ``sent_at``, commit — and
    on a failed send, roll back, so the row is absent and the next sweep
    retries. ``sent_at`` is nullable to permit that ordering; a committed row
    with NULL ``sent_at`` means a caller committed before confirming the send.

    A ``send_dm`` that returns ``None`` because the recipient is not a Slack
    workspace member is a SKIP, not a failure. Count it in its own named bucket
    and leave no row — never a bare count, and never a rolled-up "errors".
    """

    __tablename__ = "digest_logs"

    id = Column(String, primary_key=True)  # uuid4 hex
    recipient_email = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)  # see DIGEST_KINDS
    local_date = Column(String, nullable=False)  # ISO "YYYY-MM-DD", recipient-local
    channel = Column(String, nullable=True)  # slack | email | inapp

    # What was actually shown, and how much was held back. The cap is a
    # server-side rule, not a UI preference, and ``suppressed_count`` is what
    # lets the digest say "and 6 more" honestly.
    item_count = Column(Integer, nullable=False, default=0)
    suppressed_count = Column(Integer, nullable=False, default=0)
    # Which commitments/wrap-ups made the cut, for the "I never got told"
    # answer. Links and ids only — a digest never quotes transcript text, so
    # neither does its log.
    detail = Column(JSON, nullable=True)

    sent_at = Column(DateTime, nullable=True)  # only after a confirmed send
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "recipient_email",
            "kind",
            "local_date",
            name="uq_digest_logs_recipient_kind_date",
        ),
        # Reads fold with ``func.lower()`` per the module docstring.
        Index("ix_digest_logs_recipient_email_lower", func.lower(recipient_email)),
        CheckConstraint(
            lower_check("recipient_email"), name="ck_digest_logs_recipient_lower"
        ),
        CheckConstraint(in_check("kind", DIGEST_KINDS), name="ck_digest_logs_kind"),
    )

    @validates("recipient_email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Fold the recipient address on every write.

        The guard is keyed on this column, so an unfolded write makes the same
        human two different people and hands them two digests — the precise
        outcome the anti-tsunami rules exist to prevent.
        """
        if value is None:
            return None
        return value.strip().lower()
