"""The commitment state machine — open, confirm, age, escalate, close.

The clock Cubie owns. Read ``rubik_core/cockpit/__init__.py`` for the one rule
this package exists to enforce, and for what happens when it is broken: **no
HubSpot stage timestamp is ever the aging clock.** There is no
live stage-sourced opener and no stage-sourced close here, and there must never
be one. ``hs_v2_date_entered_{stageId}`` may seed ``opened_at`` exactly once,
from the one-off go-live backfill script, which calls :func:`open_commitment`
with ``source="stage_backfill"`` like any other caller — that is the only place
a stage date reaches this module, and it is not a recurring path.

Four properties carry the design, and each one is the fix for a specific way
this feature could fail on its first Monday:

* **``sla_hours`` is a snapshot.** Resolved once at open and written to the
  row, never re-read at display time. Retuning an SLA in September must not
  retroactively breach August's commitments; a system that looks the SLA up at
  render time produces one morning where every row turns red at once, and a
  cockpit that cried wolf never gets its credibility back.
* **Opening is idempotent per live scope.** The two partial unique constraints
  on ``commitments`` are the mechanism, not caller discipline. When two
  sources fire for the same SOW, the first creates the row, the second
  attaches its evidence, and the EARLIEST ``opened_at`` wins. Back-dating is
  the point — a later source arrives after the call, and the call is when the
  client actually asked, which is the age that matters.
* **``provisional`` runs the clock with escalation disarmed.** An AI-extracted
  commitment nobody has confirmed ages on its own AE's screen and pages
  nobody. :func:`confirm` arms the ladder. That is what stops an unreviewed
  guess reaching the sales manager without making the clock wait for someone
  to remember.
* **A NULL ``opened_at`` is an unknown age, not a zero age.** It does not age,
  does not escalate, and ``breach_at`` stays NULL. A five-month-old stalled
  deal displayed as "0h old" is a lie the cockpit would be telling on day one.
* **Lateness is STAMPED, not derived.** ``breached_at`` is written once, at the
  first crossing, and never moved. It cannot be recomputed later because
  ``breach_at`` moves with every ETA revision and ``state`` is overwritten on
  close — so a breach that was revised and then closed would report as
  never-late, silently deleting the exact miss this feature exists to count.

All elapsed-time arithmetic goes through ``rubik_core.cockpit.business_hours``.
No ``timedelta`` on a commitment clock, anywhere, ever.

Session convention: ``db`` is the first positional argument and the caller owns
the transaction. Everything here ``flush()``es and nothing commits.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.cockpit.business_hours import (
    add_business_hours,
    as_naive_utc,
    business_hours_between,
    pin_business_time_zone,
)
from rubik_core.config import settings
from rubik_core.database.commitments import (
    COMMITMENT_CLOSED_SOURCES,
    COMMITMENT_KINDS,
    COMMITMENT_OPENED_SOURCES,
    Commitment,
    CommitmentSlaConfig,
)

logger = logging.getLogger(__name__)

#: ``Commitment.sow_kind`` values. There is no CHECK on that column, so an
#: unrecognised value does not raise — it just misses the SLA lookup and lands
#: on the generic row. The extractor passes model output straight through
#: (``(item.payload or {}).get("kind")``), so this is
#: validated here and normalized to NULL rather than trusted.
SOW_KINDS: tuple[str, ...] = ("standard", "non_standard")

#: ``commitment_sla_configs.sow_kind`` is NOT NULL and uses this sentinel for
#: the kinds that have no SOW variant, because SQL treats NULLs as distinct and
#: the table's unique constraint would not hold with NULLs permitted.
SLA_ANY = "any"

#: Fraction of the SLA at which a commitment turns amber.
AT_RISK_FRACTION = 0.7

#: The states the aging sweep walks, in order. ``provisional``, ``accepted``
#: and ``closed`` are deliberately absent: they are not tiers, they are exits.
ESCALATION_LADDER: tuple[str, ...] = (
    "open",
    "at_risk",
    "breached",
    "escalated",
    "exec_visible",
)
_LADDER_INDEX = {state: i for i, state in enumerate(ESCALATION_LADDER)}

#: The tiers that MEAN late. Used to stamp ``breached_at`` on the crossing, and
#: as :func:`was_breached`'s backstop for a row whose stamp is NULL because it
#: predates the column.
_BREACH_TIER_STATES: frozenset[str] = frozenset(
    ("breached", "escalated", "exec_visible")
)

# ── The two escalation steps, in BUSINESS DAYS after breach_at ────────────────
# The ladder reads "Breach + 48h" then "Breach + 5 days", and the first
# design restated the first as "+48 BUSINESS hours". It cannot mean that:
# at COCKPIT_BUSINESS_DAY_HOURS=8, 48 business hours is SIX business days, which
# is LATER than the 5-business-day exec tier it is supposed to precede. Taken
# literally, every breach would reach exec_visible before escalated and the
# sales manager's escalated view would ship permanently empty — a whole
# persona's screen, blank, with nothing erroring.
#
# The SLA table supplies the conversion the first design dropped: it stores
# "24 clock hours" as 8 and "48 clock hours" as 16, i.e. one business day per 24
# clock hours. "Breach + 48h" is therefore two business days for the sales
# manager, and five for the sales director, which is also the only reading in
# which the ladder is a ladder.
#
# Constants rather than config rows: ideally every threshold is a row in a
# config table, but the shipped schema only configures SLA *hours* per
# (kind, sow_kind) — commitment_sla_configs has no row shape for a ladder step.
# Making these configurable is a migration somebody else owns; until then they
# are named, here, in business days so they track COCKPIT_BUSINESS_DAY_HOURS.
ESCALATE_AFTER_BUSINESS_DAYS = 2
EXEC_VISIBLE_AFTER_BUSINESS_DAYS = 5

#: Fallback SLA snapshots, in BUSINESS hours, from the default SLA table — used
#: only when ``commitment_sla_configs`` has no matching row, and logged loudly
#: when it happens. A missing config row must not break the CCS pipeline: an
#: unseeded table would otherwise mean every extraction fails at the last step.
#: Seeders should import this rather than re-typing the numbers, because the
#: business-hours conversion is where they get typed wrong ("3 business days" is
#: 24, not 72).
DEFAULT_SLA_HOURS: dict[tuple[str, str], int] = {
    ("sow", "standard"): 24,  # 3 business days
    ("sow", "non_standard"): 80,  # 10 business days
    ("sow", SLA_ANY): 24,
    ("wrapup", SLA_ANY): 8,  # 1 business day
    ("lead_touch", SLA_ANY): 16,  # 2 business days
}

# Accumulated opening evidence is capped so a sweep that re-reports the same
# call every 15 minutes for a 24-hour lookback cannot grow a Text column
# without bound. Exact-line dedupe below is the primary guard; this is the
# backstop for near-duplicate quotes off a re-run extraction.
_MAX_EVIDENCE_CHARS = 8000


def _norm_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _norm_id(value: str | None) -> str | None:
    """HubSpot ids arrive as ints, blanks and stray whitespace from three
    different callers. A blank string is NOT an id — stored as one it would
    make every scopeless commitment collide on ``""``."""
    text = str(value).strip() if value is not None else ""
    return text or None


def _normalize_sow_kind(value: str | None) -> str | None:
    """Coerce an unrecognised ``sow_kind`` to NULL, loudly.

    Raising instead would let one bad LLM payload fail the whole extraction
    run, and the SOW commitment — the highest-value row in the feature — is the
    one that would be lost. NULL falls through to the generic ``"any"`` SLA,
    which is wrong-but-open rather than absent.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in SOW_KINDS:
        logger.warning(
            "[cockpit] unrecognised sow_kind %r — storing NULL and using the "
            "generic SLA; expected one of %s",
            value,
            SOW_KINDS,
        )
        return None
    return text


def _resolve_sla_hours(db: Session, kind: str, sow_kind: str | None) -> int:
    """The SLA to SNAPSHOT onto a new row, in business hours.

    Looked up on (kind, sow_kind) with a fall-through to the kind's generic
    ``"any"`` row, then to :data:`DEFAULT_SLA_HOURS`. Called exactly once per
    commitment, at open — never at display time. See the module docstring.
    """
    key = sow_kind or SLA_ANY
    row = (
        db.query(CommitmentSlaConfig)
        .filter(CommitmentSlaConfig.kind == kind, CommitmentSlaConfig.sow_kind == key)
        .first()
    )
    if row is None and key != SLA_ANY:
        row = (
            db.query(CommitmentSlaConfig)
            .filter(
                CommitmentSlaConfig.kind == kind,
                CommitmentSlaConfig.sow_kind == SLA_ANY,
            )
            .first()
        )
    if row is not None and row.hours and row.hours > 0:
        return int(row.hours)

    fallback = DEFAULT_SLA_HOURS.get((kind, key)) or DEFAULT_SLA_HOURS.get(
        (kind, SLA_ANY)
    )
    if fallback is None:
        raise ValueError(f"no SLA configured or defaulted for kind={kind!r}")
    logger.warning(
        "[cockpit] no commitment_sla_configs row for (%s, %s) — snapshotting the "
        "built-in default of %sh; seed the table so the number is the sales "
        "manager's",
        kind,
        key,
        fallback,
    )
    return int(fallback)


def compute_breach_at(
    opened_at: datetime | None,
    sla_hours: int,
    promised_at: datetime | None,
) -> datetime | None:
    """When this commitment is late, in naive UTC.

    ``None`` when ``opened_at`` is None: an unknown age has no breach instant,
    which is what makes "unknown age does not escalate" a property of the data
    rather than a branch every reader has to remember.

    ``promised_at`` wins **only when it is later** than the SLA. The sales
    director's rule: a
    three-week RFP is not late on day four. An ETA EARLIER than the SLA does
    not shorten the clock — an AE over-promising to a client must not
    manufacture an internal breach, or the honest thing (telling the client a
    tight date) becomes the thing that gets you a red row.
    """
    if opened_at is None:
        return None
    breach_at = add_business_hours(as_naive_utc(opened_at), max(int(sla_hours or 0), 0))
    if promised_at is None:
        return breach_at
    promised = as_naive_utc(promised_at)
    return promised if promised > breach_at else breach_at


def was_breached(commitment: Commitment, *, now: datetime | None = None) -> bool:
    """Whether this commitment was ever late — still true after it closes.

    The canonical predicate for reporting, and it reads a PERSISTED fact:
    ``breached_at``, stamped write-once by :func:`_advance` the first time the
    clock crossed ``breach_at``.

    It has to be persisted rather than derived, because both of the row's other
    records of lateness are destroyed in normal operation. ``breach_at`` is a
    moving target — the ETA endpoint recomputes it whenever an AE revises a
    promise, so a commitment that breached in August can carry a September
    ``breach_at`` — and ``state`` is overwritten with ``"closed"`` when the work
    lands. Deriving lateness from those two used to mean that a breach, revised
    and then closed, reported as never-late: a real miss deleting itself from
    the reporting this feature justifies itself with.

    Everything after the stamp is a backstop for a row the stamp never touched:

    * the two escalation timestamps, which only a real breach can have set;
    * a LIVE row currently sitting on a breach tier, or one whose clock has
      already crossed ``breach_at`` but which no sweep has visited yet. Both are
      safe here precisely because the row is live — nothing has overwritten its
      state or moved its deadline out from under a close.

    A closed row therefore answers from the stamp and the escalation timestamps
    alone. That is the whole fix: closing still clears nothing (see
    :func:`close_commitment`), and nothing is re-derived from a deadline that
    may have moved after the breach.
    """
    if commitment.breached_at is not None:
        return True
    if commitment.escalated_at is not None or commitment.exec_visible_at is not None:
        return True
    if commitment.closed_at is not None:
        return False
    if commitment.state in _BREACH_TIER_STATES:
        return True
    if commitment.breach_at is None:
        return False
    return as_naive_utc(now or datetime.utcnow()) >= commitment.breach_at


def _target_state(commitment: Commitment, now: datetime) -> str:
    """The tier this commitment's clock puts it in, right now.

    Monotone **by construction**: each tier is nested inside the one before it,
    so a row can never reach ``exec_visible`` without passing through
    ``escalated``. That is the rule of one escalation state per commitment
    made structural, and it is also the guard against a mis-tuned
    pair of ladder constants silently skipping a persona's whole view.
    """
    day_hours = float(getattr(settings, "COCKPIT_BUSINESS_DAY_HOURS", 8) or 8)
    sla_hours = int(commitment.sla_hours or 0)

    state = "open"
    if sla_hours > 0 and commitment.opened_at is not None:
        elapsed = business_hours_between(commitment.opened_at, now)
        if elapsed >= AT_RISK_FRACTION * sla_hours:
            state = "at_risk"

    breach_at = commitment.breach_at
    if breach_at is not None and now >= breach_at:
        state = "breached"
        if now >= add_business_hours(
            breach_at, ESCALATE_AFTER_BUSINESS_DAYS * day_hours
        ):
            state = "escalated"
            if now >= add_business_hours(
                breach_at, EXEC_VISIBLE_AFTER_BUSINESS_DAYS * day_hours
            ):
                state = "exec_visible"
    return state


def _stamp_breached_at(commitment: Commitment, now: datetime) -> None:
    """Record, once and forever, that this commitment's clock went past its due.

    WRITE-ONCE. Called on the crossing and never again: the ``is not None``
    guard is the whole contract, because a later ``promised_at`` legitimately
    moves ``breach_at`` and a re-stamp would let the row's history follow the
    deadline around. ``state`` cannot carry this fact either — it is overwritten
    with ``"closed"`` when the work lands.

    Stored value is ``breach_at``, the deadline that was actually missed, NOT
    ``now``. The sweep observes a crossing up to 15 minutes late, and an outage
    can make that hours or a day, so stamping the observation time would date an
    August breach to whenever the sweep next ran and would understate every
    "how late were we" number computed from it. ``now`` is only the evidence
    that the crossing has happened.

    Stamps only a row whose ``state`` is ON the escalation ladder, which is the
    same rule :func:`recompute_states` skips by and is enforced here so it holds
    at every call site: a ``provisional`` row is an unconfirmed AI guess and
    must not manufacture a breach in the sales manager's numbers, an
    ``accepted`` row was already stamped when it breached, and a ``closed``
    row's crossing is history. An unknown age needs no rule of its own —
    ``opened_at`` NULL means ``breach_at`` NULL, so there is no crossing to
    observe.
    """
    if commitment.breached_at is not None:
        return
    if commitment.state not in _LADDER_INDEX:
        return
    if commitment.breach_at is None or now < commitment.breach_at:
        return
    commitment.breached_at = commitment.breach_at


def _advance(commitment: Commitment, now: datetime) -> str | None:
    """Move one commitment FORWARD along the ladder, or not at all.

    Returns the new state, or ``None`` when nothing changed. Never moves
    backwards: a commitment that was late stays historically late even after
    the ETA moves or it closes. Un-breaching on a recompute would let a row
    that already reached somebody's worklist quietly vanish from it.

    This is also the single place a commitment becomes late, so it is where
    ``breached_at`` is stamped. The off-ladder guard below runs FIRST and is
    what keeps ``recompute_states``'s three skips honest: a ``provisional``,
    ``accepted`` or ``closed`` row returns here before anything is written, so
    an unconfirmed AI guess or an accepted miss is never stamped. (An unknown
    age needs no guard — with ``opened_at`` NULL, ``breach_at`` is NULL too and
    the crossing can never be observed.) The stamp is deliberately OUTSIDE the
    forward-only return: a row already sitting on a breach tier with no stamp
    still gets one, so the fact is not lost to a state the sweep cannot move.
    """
    current = commitment.state
    if current not in _LADDER_INDEX:
        return None
    target = _target_state(commitment, now)
    if target in _BREACH_TIER_STATES:
        _stamp_breached_at(commitment, now)
    if _LADDER_INDEX[target] <= _LADDER_INDEX[current]:
        return None

    commitment.state = target
    # escalated_to stays for whoever resolves the manager chain (EOS scope) —
    # this module has no opinion on who owns an escalation, only when it starts.
    if target in ("escalated", "exec_visible") and commitment.escalated_at is None:
        commitment.escalated_at = now
    if target == "exec_visible" and commitment.exec_visible_at is None:
        commitment.exec_visible_at = now
    return target


def _evidence_line(source: str, ref: str | None, text: str | None) -> str | None:
    """One accumulated evidence block, or None when there is nothing to record."""
    body = (text or "").strip()
    if not body and not ref:
        return None
    head = f"[{source}" + (f" · {ref}" if ref else "") + "]"
    return f"{head} {body}".strip()


def _append_evidence(
    commitment: Commitment,
    *,
    source: str,
    ref: str | None,
    text: str | None,
) -> None:
    """Attach a second source's evidence to a commitment that already exists.

    Exact-line dedupe is load-bearing, not tidiness: the conference-record
    sweep re-reads a 24-hour lookback every 15 minutes and calls
    :func:`open_commitment` again each time, so an append that did not dedupe
    would write the same quote ~96 times per call.
    """
    line = _evidence_line(source, ref, text)
    if line is None:
        return
    existing = commitment.opened_evidence_text or ""
    if line in existing:
        return
    if len(existing) + len(line) + 1 > _MAX_EVIDENCE_CHARS:
        logger.debug(
            "[cockpit] evidence cap reached on commitment %s — dropping a block",
            commitment.id,
        )
        return
    commitment.opened_evidence_text = f"{existing}\n{line}".strip()


def _recompute_breach(commitment: Commitment) -> None:
    """Re-derive ``breach_at`` from the row's OWN snapshot.

    ``commitment.sla_hours`` — never a fresh config read. Back-dating an
    ``opened_at`` must not also re-price the SLA, or a September retune leaks
    into an August row through the back door.
    """
    commitment.breach_at = compute_breach_at(
        commitment.opened_at, commitment.sla_hours, commitment.promised_at
    )


def _adopt_earlier_opened_at(
    commitment: Commitment, opened_at: datetime | None
) -> bool:
    """Back-date to a genuinely earlier open, or when the age becomes known.

    Returns whether anything changed. Never moves ``opened_at`` forward: the
    earliest evidence is the truth about when the client asked, and a later
    source overwriting it would reset the age of the exact stall this feature
    exists to catch.
    """
    if opened_at is None:
        return False
    if commitment.opened_at is not None and commitment.opened_at <= opened_at:
        return False
    commitment.opened_at = opened_at
    _recompute_breach(commitment)
    return True


def _live_deal_scoped(db: Session, kind: str, deal_id: str) -> Commitment | None:
    return (
        db.query(Commitment)
        .filter(
            Commitment.kind == kind,
            Commitment.hubspot_deal_id == deal_id,
            Commitment.closed_at.is_(None),
        )
        .order_by(Commitment.created_at.asc())
        .first()
    )


def _live_company_scoped(db: Session, kind: str, company_id: str) -> Commitment | None:
    """The live commitment for a company that has NO deal id yet.

    Deliberately excludes rows that already carry a deal: those live under the
    deal-scoped constraint, and folding a company match into one of them here
    would merge two different deals at the same company.
    """
    return (
        db.query(Commitment)
        .filter(
            Commitment.kind == kind,
            Commitment.hubspot_company_id == company_id,
            Commitment.hubspot_deal_id.is_(None),
            Commitment.closed_at.is_(None),
        )
        .order_by(Commitment.created_at.asc())
        .first()
    )


def _live_by_evidence(
    db: Session, kind: str, owner_email: str, evidence_ref: str
) -> Commitment | None:
    """Last-resort dedupe for a commitment with NO deal and NO company.

    Both scope constraints are NULL on such a row, so the database permits
    unlimited duplicates — and the ad-hoc-call wrap-up commitment
    (``company_id`` explicitly "may be None") is exactly that shape. The
    15-minute sweep over a 24-hour lookback would otherwise open ~96 wrap-up
    commitments for one ad-hoc call, and the AE's My Day would fill with copies
    of one obligation — the failure the two partial constraints exist to
    prevent, arriving through the gap between them.
    """
    return (
        db.query(Commitment)
        .filter(
            Commitment.kind == kind,
            func.lower(Commitment.owner_email) == owner_email,
            Commitment.opened_evidence_ref == evidence_ref,
            Commitment.hubspot_deal_id.is_(None),
            Commitment.hubspot_company_id.is_(None),
            Commitment.closed_at.is_(None),
        )
        .order_by(Commitment.created_at.asc())
        .first()
    )


def _fold(db: Session, loser: Commitment, *, into: Commitment) -> None:
    """Merge a company-scoped commitment into the deal-scoped one that appeared.

    Runs when a deal id arrives for work that already had BOTH a company-scoped
    live row and a deal-scoped one — two sources that turn out to be the same
    obligation. The deal-scoped row survives (it is the one the pipeline and
    every deal-keyed read can find), takes the earlier ``opened_at`` and the
    other's evidence, and the company-scoped row closes as ``superseded``
    rather than being deleted: it is somebody's audit trail.
    """
    _adopt_earlier_opened_at(into, loser.opened_at)
    _append_evidence(
        into,
        source=loser.opened_source,
        ref=loser.opened_evidence_ref,
        text=loser.opened_evidence_text,
    )
    if into.promised_at is None and loser.promised_at is not None:
        into.promised_at = loser.promised_at
        into.promised_reason = loser.promised_reason
        _recompute_breach(into)
    close_commitment(db, loser, source="superseded", evidence_ref=into.id)
    logger.info(
        "[cockpit] folded company-scoped commitment %s into deal-scoped %s (%s)",
        loser.id,
        into.id,
        into.kind,
    )


def open_commitment(
    db: Session,
    *,
    kind: str,
    owner_email: str,
    company_id: str | None = None,
    deal_id: str | None = None,
    opened_at: datetime | None = None,
    source: str,
    evidence_ref: str | None = None,
    evidence_text: str | None = None,
    sow_kind: str | None = None,
    promised_at: datetime | None = None,
    provisional: bool = False,
    sla_hours: int | None = None,
) -> Commitment:
    """Open a commitment, or attach this evidence to the live one that exists.

    The canonical opener. Every source — CCS extraction, the
    wrap-up, lead first touch, the one-off stage backfill, manual entry — calls
    exactly this, and no caller passes a keyword that is
    not listed above.

    ``sla_hours``, when not given, is resolved from ``commitment_sla_configs``
    on (kind, sow_kind) and SNAPSHOTTED onto the row.

    Idempotent by (deal_id, kind, live) and (company_id, kind, live-without-
    deal), matching the table's two partial unique constraints. When a live
    commitment already exists:

    * an incoming ``opened_at`` EARLIER than the stored one back-dates it and
      recomputes ``breach_at`` — a later source usually arrives after the
      call, and the call is when the client actually asked;
    * the incoming evidence is appended either way;
    * a ``deal_id`` arriving for a company-scoped row is back-filled onto it,
      moving the row from one constraint to the other;
    * a non-provisional open on a ``provisional`` row promotes it to ``open``.
      A human-sourced open is not an AI guess, so its arrival is what the
      ladder was waiting to be armed by;
    * a second live row is NEVER created.

    ``provisional=True`` runs the clock with escalation disarmed.
    ``opened_at=None`` means the age is unknown: the row does not age, does not
    escalate, and ``breach_at`` stays NULL.

    Raises ``ValueError`` on an unknown ``kind`` or ``source``, a blank owner,
    or a non-positive ``sla_hours`` — all caller bugs that would otherwise
    surface as an ``IntegrityError`` from a CHECK constraint at flush, several
    frames away from whoever caused them.
    """
    kind = (kind or "").strip()
    if kind not in COMMITMENT_KINDS:
        raise ValueError(f"kind must be one of {COMMITMENT_KINDS}, got {kind!r}")
    source = (source or "").strip()
    if source not in COMMITMENT_OPENED_SOURCES:
        raise ValueError(
            f"source must be one of {COMMITMENT_OPENED_SOURCES}, got {source!r}"
        )
    owner = _norm_email(owner_email)
    if not owner:
        raise ValueError("owner_email is required")

    deal_id = _norm_id(deal_id)
    company_id = _norm_id(company_id)
    evidence_ref = _norm_id(evidence_ref)
    sow_kind = _normalize_sow_kind(sow_kind)
    opened_at = as_naive_utc(opened_at) if opened_at is not None else None
    promised_at = as_naive_utc(promised_at) if promised_at is not None else None

    existing = _find_live(
        db,
        kind=kind,
        owner_email=owner,
        deal_id=deal_id,
        company_id=company_id,
        evidence_ref=evidence_ref,
    )
    if existing is not None:
        return _attach_to_existing(
            db,
            existing,
            deal_id=deal_id,
            company_id=company_id,
            opened_at=opened_at,
            source=source,
            evidence_ref=evidence_ref,
            evidence_text=evidence_text,
            promised_at=promised_at,
            provisional=provisional,
        )

    snapshot = (
        int(sla_hours)
        if sla_hours is not None
        else _resolve_sla_hours(db, kind, sow_kind)
    )
    if snapshot <= 0:
        raise ValueError(f"sla_hours must be positive, got {sla_hours!r}")

    commitment = Commitment(
        id=uuid.uuid4().hex,
        kind=kind,
        hubspot_deal_id=deal_id,
        hubspot_company_id=company_id,
        owner_email=owner,
        opened_at=opened_at,
        opened_source=source,
        opened_evidence_ref=evidence_ref,
        opened_evidence_text=_evidence_line(source, evidence_ref, evidence_text),
        sow_kind=sow_kind,
        promised_at=promised_at,
        sla_hours=snapshot,
        breach_at=compute_breach_at(opened_at, snapshot, promised_at),
        state="provisional" if provisional else "open",
    )
    # Mandatory after any write touching closed_at / deal id / company id: the
    # flags ARE the partial unique constraints, and the consistency CHECKs
    # reject the row rather than letting a duplicate live commitment through.
    commitment.refresh_live_scope_flags()
    db.add(commitment)
    db.flush()
    logger.info(
        "[cockpit] opened %s commitment %s for %s (source=%s deal=%s company=%s "
        "sla=%sh opened_at=%s state=%s)",
        kind,
        commitment.id,
        owner,
        source,
        deal_id,
        company_id,
        snapshot,
        opened_at.isoformat() if opened_at else "unknown",
        commitment.state,
    )
    return commitment


def _find_live(
    db: Session,
    *,
    kind: str,
    owner_email: str,
    deal_id: str | None,
    company_id: str | None,
    evidence_ref: str | None,
) -> Commitment | None:
    """The live commitment this open should attach to, if there is one.

    Deal scope first, then the company-scoped row that has no deal yet, then —
    only when there is no scope key at all — the evidence ref. When BOTH a
    deal-scoped and a company-scoped live row exist for the same work, the
    company-scoped one is folded into the deal-scoped one here, before either
    is returned, so the caller never sees the duplicate.
    """
    deal_row = _live_deal_scoped(db, kind, deal_id) if deal_id else None
    company_row = _live_company_scoped(db, kind, company_id) if company_id else None

    if deal_row is not None:
        if company_row is not None:
            _fold(db, company_row, into=deal_row)
        return deal_row
    if company_row is not None:
        return company_row
    if deal_id is None and company_id is None and evidence_ref:
        return _live_by_evidence(db, kind, owner_email, evidence_ref)
    return None


def _attach_to_existing(
    db: Session,
    commitment: Commitment,
    *,
    deal_id: str | None,
    company_id: str | None,
    opened_at: datetime | None,
    source: str,
    evidence_ref: str | None,
    evidence_text: str | None,
    promised_at: datetime | None,
    provisional: bool,
) -> Commitment:
    """Fold a second source into a live commitment. Never creates a row."""
    scope_changed = False
    if deal_id and not commitment.hubspot_deal_id:
        # The row moves from the company-scoped constraint to the deal-scoped
        # one; the flags below are what actually move it.
        commitment.hubspot_deal_id = deal_id
        scope_changed = True
    if company_id and not commitment.hubspot_company_id:
        commitment.hubspot_company_id = company_id
        scope_changed = True
    if scope_changed:
        commitment.refresh_live_scope_flags()

    back_dated = _adopt_earlier_opened_at(commitment, opened_at)
    if back_dated and evidence_ref:
        # The earlier evidence is the real opener, so it becomes the cited one.
        commitment.opened_evidence_ref = evidence_ref
    _append_evidence(commitment, source=source, ref=evidence_ref, text=evidence_text)

    if promised_at is not None and commitment.promised_at is None:
        # Fill an empty ETA only. Never overwrite one: this cannot tell a human
        # commitment made in the UI from a date an extractor guessed, and the
        # human's is the one a client was told.
        commitment.promised_at = promised_at
        _recompute_breach(commitment)

    if not provisional and commitment.state == "provisional":
        commitment.state = "open"
        # Land it in the right tier now rather than letting it show as freshly
        # open until the next 15-minute sweep — a three-week-old request is
        # not "open, 0% elapsed" on anybody's screen.
        _advance(commitment, as_naive_utc(datetime.utcnow()))

    db.flush()
    logger.info(
        "[cockpit] attached %s evidence to existing %s commitment %s "
        "(back_dated=%s state=%s)",
        source,
        commitment.kind,
        commitment.id,
        back_dated,
        commitment.state,
    )
    return commitment


def confirm(db: Session, commitment: Commitment) -> Commitment:
    """``provisional`` → ``open``. Arms the escalation ladder.

    Called from the wrap-up approval path when the AE accepts a
    ``sow_request`` item — NOT from extraction, where
    nothing has been accepted yet.

    Idempotent and non-raising by design: confirming an already-open or already
    closed commitment logs and returns it unchanged. A wrap-up approval must
    not 500 because a sweep closed the commitment a second earlier, and the AE
    has no way to act on that error if it did.
    """
    if commitment.closed_at is not None:
        logger.warning(
            "[cockpit] confirm() on already-closed commitment %s — ignored",
            commitment.id,
        )
        return commitment
    if commitment.state != "provisional":
        logger.debug(
            "[cockpit] confirm() on commitment %s already in state %s — no-op",
            commitment.id,
            commitment.state,
        )
        return commitment

    commitment.state = "open"
    # Confirmation can arrive days after the call, so apply the clock at once:
    # arming a ladder and then reporting "open" until the next sweep would show
    # the AE a commitment that is actually already breached.
    _advance(commitment, as_naive_utc(datetime.utcnow()))
    db.flush()
    logger.info(
        "[cockpit] confirmed commitment %s — state=%s, escalation armed",
        commitment.id,
        commitment.state,
    )
    return commitment


@pin_business_time_zone
def recompute_states(db: Session, now: datetime | None = None) -> dict:
    """Age every live commitment one step along the ladder. Idempotent.

    The 15-minute sweep. Safe to re-run: transitions only ever move FORWARD, so
    a re-run is a no-op and nothing un-breaches. A commitment that was late
    stays historically late — the crossing stamps ``breached_at`` write-once
    (:func:`_stamp_breached_at`), which is what keeps that sentence true after
    the ETA is revised and after the row closes.

    Skips, each of which is load-bearing:

    * ``provisional`` — the clock runs, escalation is disarmed. This is the
      whole mechanism by which an unconfirmed AI extraction never pages the
      sales manager.
    * ``opened_at IS NULL`` — an unknown age does not age.
    * ``accepted`` — TERMINAL until closed. Without this skip, an accepted
      breach re-escalates every 15 minutes forever, and the one feature the
      sales supervisor asked for (accept a miss, with a reason, and have it
      stay accepted) becomes the loudest row on their screen.

    Returns per-outcome buckets, never a bare count: a scheduled endpoint that
    reports "42" cannot tell "42 rows checked, nothing moved" from "42 rows
    skipped because every one of them is provisional".
    """
    now = as_naive_utc(now or datetime.utcnow())
    buckets: dict[str, int] = {
        "scanned": 0,
        "to_at_risk": 0,
        "to_breached": 0,
        "to_escalated": 0,
        "to_exec_visible": 0,
        "unchanged": 0,
        "skipped_provisional": 0,
        "skipped_accepted": 0,
        "skipped_unknown_age": 0,
        "skipped_off_ladder": 0,
    }

    rows = db.query(Commitment).filter(Commitment.closed_at.is_(None)).all()
    for commitment in rows:
        buckets["scanned"] += 1
        if commitment.state == "provisional":
            buckets["skipped_provisional"] += 1
            continue
        if commitment.state == "accepted":
            buckets["skipped_accepted"] += 1
            continue
        if commitment.opened_at is None:
            buckets["skipped_unknown_age"] += 1
            continue
        if commitment.state not in _LADDER_INDEX:
            # A live row whose state is off the ladder entirely — e.g. "closed"
            # with a NULL closed_at, which only a bad write produces. Counted
            # rather than coerced: the sweep is not the place to guess.
            buckets["skipped_off_ladder"] += 1
            logger.warning(
                "[cockpit] live commitment %s has off-ladder state %r",
                commitment.id,
                commitment.state,
            )
            continue

        moved = _advance(commitment, now)
        if moved is None:
            buckets["unchanged"] += 1
        else:
            buckets[f"to_{moved}"] += 1

    db.flush()
    logger.info("[cockpit] recompute_states: %s", buckets)
    return {**buckets, "as_of": now.isoformat()}


def close_commitment(
    db: Session,
    commitment: Commitment,
    *,
    source: str,
    evidence_ref: str | None = None,
    actor: str | None = None,
) -> Commitment:
    """Close a commitment, preserving whether it breached first.

    ``source`` is ONLY ``"manual"`` or ``"superseded"``. There is deliberately
    no ``"email"`` and no ``"stage"`` source: sent-mail evidence SUGGESTS a
    close and a human confirms it, which records ``"manual"``. A wrong
    auto-close hides a real breach, which is precisely the failure this feature
    exists to fix — and a deal advancing past a stage cannot close anything,
    because nothing knows *when* it advanced.

    **Clears nothing.** ``breached_at``, ``breach_at``, ``escalated_at`` and
    ``exec_visible_at`` all survive the close, because ``state`` becomes
    ``"closed"`` and stops being able to say this was ever late. ``breached_at``
    is the one that answers it — write-once, set on the crossing, and stamped
    here as a last resort for a commitment closed before any sweep saw it cross.
    Read it back through :func:`was_breached`.

    ``actor`` is logged, not stored: there is no ``closed_by`` column, and
    ``backend.audit.record_audit`` **commits and rolls back the session it is
    given**, which would commit this close early and undo it on an audit
    failure. The audit row belongs to the router, where the transaction is
    already the request's.
    """
    if source not in COMMITMENT_CLOSED_SOURCES:
        raise ValueError(
            f"closed_source must be one of {COMMITMENT_CLOSED_SOURCES}, got "
            f"{source!r} — sent-mail evidence records 'manual' after a human "
            "confirms it, and there is no stage-sourced close"
        )
    if commitment.closed_at is not None:
        logger.debug(
            "[cockpit] commitment %s already closed at %s — no-op",
            commitment.id,
            commitment.closed_at,
        )
        return commitment

    now = as_naive_utc(datetime.utcnow())
    # The last chance to persist a crossing no sweep ever observed: a
    # commitment that breached at 17:00 and was closed at 17:05 is late, and
    # the 15-minute sweep had not run yet. Write-once, so this is a no-op for
    # every row the sweep already stamped.
    _stamp_breached_at(commitment, now)
    breached = was_breached(commitment)
    commitment.closed_at = now
    commitment.closed_source = source
    if evidence_ref:
        commitment.closed_evidence_ref = evidence_ref
    commitment.state = "closed"
    commitment.refresh_live_scope_flags()
    db.flush()
    logger.info(
        "[cockpit] closed %s commitment %s (source=%s actor=%s was_breached=%s)",
        commitment.kind,
        commitment.id,
        source,
        actor or "system",
        breached,
    )
    return commitment
