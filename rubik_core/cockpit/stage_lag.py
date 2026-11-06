"""Stage-lag detector — where Cubie's evidence disagrees with the HubSpot stage.

The other half of the cockpit, and arguably the higher-value half: the
commitment clock fixes how fast SOWs move, this fixes whether the pipeline
tells the truth about them. The typical case — an SOW that has sat in legal
review for two weeks while HubSpot still shows an earlier stage — is one deal
whose stage understates it, and the understatement flows straight into the
forecast someone currently scrubs by hand.

So the finding is the disagreement itself:

    ⚠ Globex Corp — stage says SOW Sent, evidence says Legal Review
       "our counsel will send redlines this week" — call 12 Aug
       [ Advance to Legal Review ]   [ Not right ]

**The silent failure this module is shaped around.** ``SIGNAL_TO_STAGE`` is an
exact-key lookup. If the extractor is ever allowed to emit free text — "legal
review", "in legal", "redlines" — every lookup misses, the detector produces
zero exceptions, and *nothing errors*. A feature that quietly returns nothing is
worse than one that breaks, because nobody goes looking. Three defences:
``STAGE_SIGNALS`` here is the single definition of the closed enum and
``rubik_core/call_capture/extractor/validators.py`` asserts equality against it
at import time; the assertions at the bottom of this module fail the import if
the constants here drift apart; and a signal value that reaches
``_ccs_signal_evidence`` outside the enum is logged at WARNING rather than
skipped quietly.

**The loud failure this module is shaped around.** A detector that cries wolf is
worse than no detector: it teaches the AE to dismiss without reading, and once
that habit forms the real exception goes past them too. Hence — emit only when
the evidence is *strictly ahead* of the record, one open exception per deal,
confidence floored at ``settings.COCKPIT_STAGE_LAG_MIN_CONFIDENCE``, and a
"Not right" rejection suppresses the finding it was rejecting rather than
returning an hour later. ``not_right_rates`` is the tuning input and the honest
measure of whether this is helping; it is a first-class output of the feature,
not diagnostics.

**Never infer won/lost from a stage string** (contract C9). A portal can
rename HubSpot's built-in stages over the years, so ``closedlost`` may carry a
label that reads like a win and a built-in id may carry the label of a custom
step; ``rubik_core/retrieval/hubspot_api.py`` records the cost of guessing on a
portal with custom stages, where ``dealstage == "closedwon"`` matched a handful
of the deals actually won. This module decides "is this deal closed?" from
``hs_is_closed``, the same computed flag ``_WON`` / ``_OPEN`` filter on, and
maps everything else by internal value only.

**Which stages mean what is configuration, not code.** Every company's pipeline
is its own, so the workspace HubSpot configuration lists the open stages of its
default pipeline in order and tags the ones the call signals stand for (see
:func:`configured_stages`). It ships empty, and with nothing configured the
detector finds nothing (reason ``not_configured``) and the worklist shows its
usual empty state; an admin sets the stages to turn it on.

Applying an exception is ``HubSpotWriteClient.update_deal(deal_id,
{"dealstage": …}, acting_owner_email=…)`` — ``DEAL_UPDATE_WRITABLE`` already
whitelists ``dealstage`` and ``_stage_gate`` already checks the stage belongs to
the deal's pipeline. Do NOT add a second write path, and do not write from a
job: ``HUBSPOT_WRITE_TOKEN`` is mounted on the ``cubie`` service only, so a
write attempted from ``cubie-worker`` raises ``HubSpotWriteRefused``. The
apply happens in the request a human clicked.
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.call_capture import CallExtractionItem, CallSession
from rubik_core.database.commitments import Commitment, CommitmentStageLag
from rubik_core.retrieval.hubspot_write import HubSpotWriteClient, _with_backoff

logger = logging.getLogger(__name__)

# ── The closed enum ───────────────────────────────────────────────────────────
# These keys are the contract between the extractor and this detector. The
# extractor's validators import STAGE_SIGNALS from here and assert equality, so
# there is exactly one definition and a drift is an ImportError rather than a
# detector that reports nothing and looks healthy.
STAGE_SIGNALS: tuple[str, ...] = (
    "sow_sent",
    "moved_to_legal_review",
    "ops_involved",
    "pending_signature",
    "verbal_agreement",
)


#: Signal → the configured stage id (``None`` when no stage is tagged with it).
#: Keys are always exactly ``STAGE_SIGNALS``, in order: that half is the
#: contract with the extractor, and stays code. The values are the company's
#: own pipeline, read from the workspace HubSpot configuration on every lookup (see
#: :func:`configured_stages`). A signal with no stage (``verbal_agreement``,
#: usually: most pipelines have no stage for it) is compatible with most open
#: stages, so it can only be a finding near the bottom of the pipeline and can
#: never offer a one-click apply — which is why ``evidence_stage_id`` is a
#: nullable column.
class _ConfiguredSignalStages(Mapping[str, "str | None"]):
    def __getitem__(self, signal: str) -> str | None:
        if signal not in STAGE_SIGNALS:
            raise KeyError(signal)
        return configured_stages().stage_for(signal)

    def __iter__(self) -> Iterator[str]:
        return iter(STAGE_SIGNALS)

    def __len__(self) -> int:
        return len(STAGE_SIGNALS)


SIGNAL_TO_STAGE: Mapping[str, str | None] = _ConfiguredSignalStages()

#: The one pipeline this detector operates in: HubSpot's built-in ``default``
#: pipeline. Stage ids are shared across a portal's deal pipelines, so a stage
#: id alone does not identify a stage — ``HubSpotWriteClient._stage_gate``
#: exists for exactly that reason. Every configured stage is a ``default``
#: pipeline id, so a deal in any other pipeline (including a **client**
#: pipeline that may also live in the portal) is skipped rather than mapped
#: against ids that mean something else there.
DEFAULT_PIPELINE_ID = "default"


@dataclass(frozen=True)
class StageLagStages:
    """The company's default-pipeline open stages, as the detector reads them.

    ``order`` is the progression, earliest first. CLOSED STAGES DO NOT BELONG IN
    IT: "is this deal closed?" is answered by ``hs_is_closed``, never by looking
    for a stage id that reads like a closing stage (C9). A deal whose stage is
    not in ``order`` produces no exception and lands in a named bucket
    (``unknown_stage``), so an unrecognised stage is visible rather than
    silently treated as stage zero.

    The ORDER is taken from configuration rather than an API:
    ``deal_pipeline_stages()`` returns {stageId: label} and drops displayOrder.
    A wrong relative order between two adjacent stages changes only whether one
    counts as "ahead" of the other, and the consequence is a suggestion offered
    or withheld — never a wrong write, because the apply is a human click behind
    ``update_deal``'s own gates.
    """

    order: tuple[str, ...] = ()
    #: (signal, stage id) pairs, one per tagged stage.
    tagged: tuple[tuple[str, str], ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.order)

    def stage_for(self, signal: str) -> str | None:
        return dict(self.tagged).get(signal)

    def index(self, stage_id: str | None) -> int | None:
        """Position in the open-stage progression, or None if not an open stage."""
        if stage_id is None:
            return None
        try:
            return self.order.index(str(stage_id))
        except ValueError:
            return None

    def unmapped_boundary(self) -> int | None:
        """Index of the last stage at which a signal with no stage still lags.

        A signal with no target stage (``verbal_agreement``) can only be a lag
        while the deal has not reached the stage ``sow_sent`` stands for. Any
        later stage is compatible with the client having verbally agreed, and
        raising it there would be pure noise — the cry-wolf failure this module
        is built to avoid. ``None`` when no stage is tagged ``sow_sent``.
        """
        sow_sent = self.index(self.stage_for("sow_sent"))
        if not sow_sent:  # None, or the first stage: nothing comes before it
            return None
        return sow_sent - 1


@functools.lru_cache(maxsize=8)
def _parse_stages(raw: str) -> StageLagStages:
    order: list[str] = []
    tagged: dict[str, str] = {}
    for entry in raw.split(","):
        stage, _, signal = entry.partition("=")
        stage, signal = stage.strip(), signal.strip()
        if not stage:
            continue
        if stage in order:
            logger.warning(
                "[stage-lag] workspace stage_lag_stages lists stage %r twice; "
                "keeping its first position",
                stage,
            )
            continue
        order.append(stage)
        if not signal:
            continue
        if signal not in STAGE_SIGNALS:
            logger.warning(
                "[stage-lag] workspace stage_lag_stages tags stage %r with %r, which "
                "is not a call signal (%s); ignored",
                stage,
                signal,
                ", ".join(STAGE_SIGNALS),
            )
            continue
        if signal in tagged:
            logger.warning(
                "[stage-lag] workspace stage_lag_stages tags %r on two stages; "
                "keeping %r",
                signal,
                tagged[signal],
            )
            continue
        tagged[signal] = stage
    return StageLagStages(
        order=tuple(order),
        tagged=tuple((sig, tagged[sig]) for sig in STAGE_SIGNALS if sig in tagged),
    )


def configured_stages() -> StageLagStages:
    """The workspace's configured HubSpot stage order (empty by default).

    A comma-separated list of the default pipeline's OPEN stage ids, earliest
    first; a stage that one of ``STAGE_SIGNALS`` stands for is written
    ``<stage id>=<signal>``, e.g. ``"quote,drafting,sowsent=sow_sent,
    legal=moved_to_legal_review,signoff=pending_signature"``. Read per call, so
    a workspace setting change takes effect without a restart.
    """
    from rubik_core.tenancy.hubspot_config import get_hubspot_config

    return _parse_stages(get_hubspot_config().stage_lag_stages)


# Properties read off the deal, all HubSpot built-ins: `hs_is_closed` /
# `hs_is_closed_won` are the computed flags `_WON` / `_OPEN` already filter on
# in hubspot_api.py. `hs_v2_date_entered_*` is deliberately
# NOT here — it is for diagnostics only, and a property listed
# in a detector is a property somebody will eventually compare to an SLA.
_DEAL_PROPERTIES = [
    "dealname",
    "pipeline",
    "dealstage",
    "hs_is_closed",
    "hs_is_closed_won",
    "hubspot_owner_id",
]

# How far back a call's stage signal still says something about the deal's stage
# TODAY. A signal from three months ago is not evidence about the current stage;
# the deal has almost certainly moved since, and raising it would put the AE in
# front of a decision they already made.
#
# Calendar days, and that does not break the one-clock rule: this is a query
# window, not elapsed-time arithmetic against an SLA. Every business-hours
# calculation in the cockpit goes through `rubik_core/cockpit/business_hours.py`;
# nothing in this module computes an age, a breach or an escalation step.
_SIGNAL_LOOKBACK_DAYS = 30

# Item statuses that count as evidence. "rejected" must never count — the AE
# looked at that proposal and said no, and re-reading it here would route their
# own rejection back to them through a different screen. "superseded" is a
# proposal that a re-extraction replaced.
_EVIDENCE_ITEM_STATUSES = ("proposed", "accepted", "written")

# Confidence for the evidence lane that is not model output.
#
# A human clicking "the SOW went out" is the strongest evidence in the system —
# it is the same click that closes the commitment — but not certainty, because
# what they confirmed is that the SOW was sent, not that the stage is wrong.
_CONFIDENCE_HUMAN_CONFIRMED_SEND = 0.9

#: Every outcome ``detect_outcome`` can report. A sweep should seed its bucket
#: dict from this tuple with zeros: a dict holding only the buckets that
#: happened cannot distinguish "nothing was skipped" from "we never looked",
#: which is the shape of report that hides an inert feature.
OUTCOME_REASONS: tuple[str, ...] = (
    "emitted",
    "updated",
    "unchanged",
    "no_evidence",
    "not_ahead",
    "below_threshold",
    "no_confidence",
    "suppressed_rejected",
    "not_configured",
    "deal_closed",
    "non_default_pipeline",
    "unknown_stage",
    "read_failed",
)

# When nothing was emitted and several evidence rows failed for different
# reasons, report the most informative one. "no_confidence" outranks the rest
# because it is the only entry here that means something upstream is broken
# rather than that the pipeline is simply accurate today.
_REASON_PRIORITY: tuple[str, ...] = (
    "no_confidence",
    "suppressed_rejected",
    "below_threshold",
    "not_ahead",
)


class StageLagOutcome(NamedTuple):
    """What one ``detect_outcome`` call did, and why.

    ``reason`` is one of ``OUTCOME_REASONS``. It exists because the interesting
    outcomes of this detector are the ones where it emits nothing, and a
    function that can only answer ``None`` gives the hourly sweep no way to
    report the difference between an accurate pipeline and a broken lookup.
    """

    exception: CommitmentStageLag | None
    reason: str


@dataclass(frozen=True)
class _Evidence:
    """One thing Cubie knows that the deal's HubSpot stage may disagree with."""

    #: Lane label. A ``STAGE_SIGNALS`` member for CCS-sourced evidence, a
    #: descriptive name for the other lane. Used for logging and the
    #: tie-break only — never as a lookup key, which is what keeps the non-CCS
    #: lane from having to pretend to be a signal the extractor can emit.
    signal: str
    stage_id: str | None
    text: str | None
    ref: str | None
    #: None when the source did not record one. Never defaulted to a number: a
    #: detector must not invent a confidence for evidence it is about to put in
    #: front of a person.
    confidence: float | None
    #: When the evidence came into being — the call's end or the human's
    #: confirmation. Load-bearing for rejection suppression:
    #: "Not right" means "the evidence you showed me is wrong", not "never
    #: mention this deal again", so a rejection only suppresses evidence that
    #: predates it.
    observed_at: datetime | None


def signal_for_stage(stage_id: str | None) -> str | None:
    """Reverse ``SIGNAL_TO_STAGE``: the signal name a stage id came from.

    Lossy by construction — ``commitment_stage_lags`` has no ``signal`` column
    (the eleven tables are final) and one of the two evidence lanes is not a
    CCS signal at all. Used only to label the tuning metric, where a stage id
    that maps to no signal is reported under its own key rather than folded into
    a bucket that would misattribute it.
    """
    if stage_id is None:
        return None
    for signal, mapped in SIGNAL_TO_STAGE.items():
        if mapped is not None and mapped == str(stage_id):
            return signal
    return None


# ── Evidence lanes ───────────────────────────────────────────────────────────


def _ccs_signal_evidence(
    db: Session, deal_id: str, *, since: datetime
) -> list[_Evidence]:
    """Stage signals the extractor heard on calls about this deal.

    The signal name is read from ``proposed_value`` and falls back to
    ``payload["signal"]``: ``call_extraction_items`` has no dedicated column for
    it and the extractor owns which of the two it writes, so reading both keeps
    this lane working either way. A name outside ``STAGE_SIGNALS`` is dropped
    **at WARNING** — that is the free-text failure, and it has to be visible in
    the logs or it is invisible everywhere.
    """
    rows = (
        db.query(CallExtractionItem, CallSession)
        .join(CallSession, CallSession.id == CallExtractionItem.call_session_id)
        .filter(
            CallExtractionItem.kind == "stage_signal",
            CallExtractionItem.status.in_(_EVIDENCE_ITEM_STATUSES),
            CallSession.hubspot_deal_id == str(deal_id),
            CallExtractionItem.created_at >= since,
        )
        .all()
    )

    out: list[_Evidence] = []
    for item, session in rows:
        signal = str(item.proposed_value or "").strip()
        if not signal and isinstance(item.payload, dict):
            signal = str(item.payload.get("signal") or "").strip()
        if signal not in STAGE_SIGNALS:
            logger.warning(
                "[stage-lag] deal %s: dropping stage_signal item %s — %r is not in "
                "STAGE_SIGNALS. The extractor's closed enum has drifted, and every "
                "SIGNAL_TO_STAGE lookup will now miss silently.",
                deal_id,
                item.id,
                signal,
            )
            continue
        out.append(
            _Evidence(
                signal=signal,
                stage_id=SIGNAL_TO_STAGE[signal],
                text=item.evidence_text,
                # The transcript entry when there is one, the item otherwise. A
                # Fathom-sourced item legitimately has no entry id (Fathom has
                # no per-utterance identifiers) and ``evidence_text`` is NOT
                # NULL, so the quote a human reads survives either way.
                ref=item.evidence_entry_id or item.id,
                confidence=(
                    float(item.confidence) if item.confidence is not None else None
                ),
                observed_at=session.ended_at or item.created_at,
            )
        )
    return out


def _confirmed_send_evidence(db: Session, deal_id: str) -> list[_Evidence]:
    """A human confirmed the SOW went out, and the stage has not caught up.

    This is the sent-mail lane, read from where it lands rather than from Gmail:
    ``sow_sent_detector`` raises a *suggestion*, a person clicks Yes, and that
    click closes the commitment with ``closed_source="manual"``. So a manually
    closed SOW commitment IS Cubie's recorded evidence that the SOW was sent,
    dated by a human rather than by a heuristic — and it costs no mailbox
    access, which matters because this runs hourly over every deal with
    evidence.
    """
    row = (
        db.query(Commitment)
        .filter(
            Commitment.kind == "sow",
            Commitment.hubspot_deal_id == str(deal_id),
            Commitment.closed_at.isnot(None),
            Commitment.closed_source == "manual",
        )
        .order_by(Commitment.closed_at.desc())
        .first()
    )
    if row is None:
        return []
    return [
        _Evidence(
            signal="sow_sent",
            stage_id=SIGNAL_TO_STAGE["sow_sent"],
            text=f"Somebody confirmed in {PRODUCT_NAME} that the SOW went out.",
            ref=row.closed_evidence_ref or row.id,
            confidence=_CONFIDENCE_HUMAN_CONFIRMED_SEND,
            observed_at=row.closed_at,
        )
    ]


def _gather_evidence(db: Session, deal_id: str, *, now: datetime) -> list[_Evidence]:
    since = now - timedelta(days=_SIGNAL_LOOKBACK_DAYS)
    return _ccs_signal_evidence(db, deal_id, since=since) + _confirmed_send_evidence(
        db, deal_id
    )


# ── Detection ────────────────────────────────────────────────────────────────


def _read_deal(deal_id: str, *, hs: HubSpotWriteClient) -> dict | None:
    """Live deal properties, or None on any read failure.

    ``get_object`` lives on the write client but reads with the READ token — it
    is what ``update_deal``'s own gates use to check live state, and it needs no
    ``HUBSPOT_WRITE_TOKEN``. It is also the repo's only fetch-one-by-id, which
    is why the detector borrows it instead of adding another HubSpot HTTP path.

    Never raises. This runs hourly across every deal with evidence, and one
    deal whose read 429s or 404s must cost that deal's exception, not the sweep.
    """
    try:
        deal = _with_backoff(hs.get_object, "deals", str(deal_id), _DEAL_PROPERTIES)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning("[stage-lag] deal %s: HubSpot read failed: %s", deal_id, exc)
        return None
    return deal or None


def _existing(db: Session, deal_id: str, status: str) -> list[CommitmentStageLag]:
    return (
        db.query(CommitmentStageLag)
        .filter(
            CommitmentStageLag.hubspot_deal_id == str(deal_id),
            CommitmentStageLag.status == status,
        )
        .order_by(CommitmentStageLag.created_at.desc())
        .all()
    )


def _is_suppressed_by_rejection(db: Session, deal_id: str, evidence: _Evidence) -> bool:
    """Has somebody already said "Not right" to this exact finding?

    A rejection suppresses the evidence it predates, not the deal. Saying "Not
    right" to a quote from the 12 Aug call must not also bury a stronger signal
    from a call on the 20th: that turns one correction into a permanent blind
    spot, which is a worse failure than the false positive it was correcting.
    """
    for row in _existing(db, deal_id, "rejected"):
        if str(row.evidence_stage_id or "") != str(evidence.stage_id or ""):
            continue
        rejected_at = row.resolved_at or row.updated_at or row.created_at
        if evidence.observed_at is None or rejected_at is None:
            # Undatable evidence against a standing rejection: stay quiet. The
            # AE has already had this conversation once.
            return True
        if evidence.observed_at <= rejected_at:
            return True
    return False


def detect_outcome(
    db: Session,
    deal_id: str,
    *,
    hs: HubSpotWriteClient | None = None,
    now: datetime | None = None,
) -> StageLagOutcome:
    """Compare one deal's HubSpot stage against Cubie's evidence.

    Emits at most ONE open exception per deal — a worklist offering two
    competing "advance this deal" buttons is not a worklist. When newer evidence
    lands further along the pipeline than the open exception's target, the
    existing row is updated in place rather than duplicated, so the AE sees the
    best current finding and the row keeps its id.

    Writes through ``db.add`` / ``db.flush`` and does NOT commit: callers own
    transaction scope, and the hourly sweep commits once per batch.

    The master-switch check is the CALLER's — the router 404s and the scheduled
    endpoint no-ops with a log line when ``COCKPIT_ENABLED`` is off.
    """
    stages = configured_stages()
    if not stages.configured:
        # Nothing to compare against: no HubSpot read, no exception, and the
        # worklist shows its usual empty state until an admin sets the stages.
        return StageLagOutcome(None, "not_configured")

    now = now or datetime.utcnow()
    hs = hs or HubSpotWriteClient()

    deal = _read_deal(deal_id, hs=hs)
    if deal is None:
        return StageLagOutcome(None, "read_failed")
    props = deal.get("properties") or {}

    # C9: closed-ness comes from the computed flag, never from the stage string.
    # On this portal `closedlost` is labelled "9 - Closed - Implemented/
    # Invoiced", so a stage-string check would misread it in both directions.
    if str(props.get("hs_is_closed") or "").lower() == "true":
        return StageLagOutcome(None, "deal_closed")

    pipeline = str(props.get("pipeline") or "")
    if pipeline != DEFAULT_PIPELINE_ID:
        logger.info(
            "[stage-lag] deal %s: skipped, pipeline %r is not the default sales "
            "pipeline (SIGNAL_TO_STAGE's ids mean something else there)",
            deal_id,
            pipeline,
        )
        return StageLagOutcome(None, "non_default_pipeline")

    current_stage = str(props.get("dealstage") or "")
    current_index = stages.index(current_stage)
    if current_index is None:
        logger.info(
            "[stage-lag] deal %s: skipped, stage %r is not an open stage of the "
            "default sales pipeline",
            deal_id,
            current_stage,
        )
        return StageLagOutcome(None, "unknown_stage")

    evidence = _gather_evidence(db, deal_id, now=now)
    if not evidence:
        return StageLagOutcome(None, "no_evidence")

    floor = float(settings.COCKPIT_STAGE_LAG_MIN_CONFIDENCE)
    rejected_reasons: set[str] = set()
    candidates: list[tuple[int, float, _Evidence]] = []

    for ev in evidence:
        if ev.confidence is None:
            # The extractor is required to emit a confidence, so a missing one
            # means something upstream went wrong. Named bucket, not a silent
            # skip: this is the reason a healthy-looking sweep can be inert.
            logger.info(
                "[stage-lag] deal %s: evidence %r carries no confidence — skipped",
                deal_id,
                ev.signal,
            )
            rejected_reasons.add("no_confidence")
            continue
        if ev.confidence < floor:
            rejected_reasons.add("below_threshold")
            continue

        target_index = stages.index(ev.stage_id)
        if target_index is None:
            # An unmapped signal cannot be ordered, so it is only a finding
            # while the deal sits before the stage SOW Sent stands for.
            boundary = stages.unmapped_boundary()
            if boundary is None or current_index > boundary:
                rejected_reasons.add("not_ahead")
                continue
            target_index = boundary
        elif target_index <= current_index:
            # Evidence at or behind the record is not a lag. Raising it would
            # fire on every deal the AE correctly advanced after the call, which
            # is precisely how a detector teaches people to stop reading it.
            rejected_reasons.add("not_ahead")
            continue

        if _is_suppressed_by_rejection(db, deal_id, ev):
            rejected_reasons.add("suppressed_rejected")
            continue
        candidates.append((target_index, ev.confidence, ev))

    if not candidates:
        for reason in _REASON_PRIORITY:
            if reason in rejected_reasons:
                return StageLagOutcome(None, reason)
        return StageLagOutcome(None, "no_evidence")

    # Furthest along the pipeline wins, confidence breaks a tie: the furthest
    # evidence is the one the forecast pays most for being wrong about.
    candidates.sort(key=lambda triple: (triple[0], triple[1]), reverse=True)
    best_index, _best_confidence, best = candidates[0]

    open_rows = _existing(db, deal_id, "open")
    if open_rows:
        row = open_rows[0]
        if (
            str(row.hubspot_stage_id) == current_stage
            and str(row.evidence_stage_id or "") == str(best.stage_id or "")
            and str(row.evidence_ref or "") == str(best.ref or "")
        ):
            return StageLagOutcome(row, "unchanged")
        existing_index = stages.index(row.evidence_stage_id)
        if (
            str(row.hubspot_stage_id) == current_stage
            and existing_index is not None
            and existing_index > best_index
        ):
            # The standing exception already points further along than today's
            # best evidence. Overwriting it would downgrade a stronger finding.
            return StageLagOutcome(row, "unchanged")
        row.hubspot_stage_id = current_stage
        row.evidence_stage_id = best.stage_id
        row.evidence_text = best.text
        row.evidence_ref = best.ref
        row.confidence = best.confidence
        db.flush()
        return StageLagOutcome(row, "updated")

    row = CommitmentStageLag(
        id=uuid.uuid4().hex,
        hubspot_deal_id=str(deal_id),
        hubspot_stage_id=current_stage,
        evidence_stage_id=best.stage_id,
        evidence_text=best.text,
        evidence_ref=best.ref,
        confidence=best.confidence,
        status="open",
    )
    db.add(row)
    db.flush()
    return StageLagOutcome(row, "emitted")


def detect(
    db: Session,
    deal_id: str,
    *,
    hs: HubSpotWriteClient | None = None,
    now: datetime | None = None,
) -> CommitmentStageLag | None:
    """The stage-lag exception for this deal, or None.

    The documented cross-module signature. Prefer :func:`detect_outcome` in a
    sweep, which also reports *why* nothing was emitted.
    """
    return detect_outcome(db, deal_id, hs=hs, now=now).exception


def deal_ids_with_evidence(db: Session, *, now: datetime | None = None) -> list[str]:
    """Deals Cubie has stage evidence for — the only deals ``detect`` can fire on.

    The hourly sweep should iterate this rather than the pipeline. The detector
    is structurally incapable of emitting anything for a deal with no evidence,
    so walking every open deal in HubSpot would spend the search quota (5 req/s
    per account, 10k results per query) proving that. Evidence-first keeps the
    read count proportional to what Cubie actually knows.
    """
    now = now or datetime.utcnow()
    since = now - timedelta(days=_SIGNAL_LOOKBACK_DAYS)

    signal_rows = (
        db.query(CallSession.hubspot_deal_id)
        .join(
            CallExtractionItem,
            CallExtractionItem.call_session_id == CallSession.id,
        )
        .filter(
            CallExtractionItem.kind == "stage_signal",
            CallExtractionItem.status.in_(_EVIDENCE_ITEM_STATUSES),
            CallSession.hubspot_deal_id.isnot(None),
            CallExtractionItem.created_at >= since,
        )
        .distinct()
        .all()
    )
    commitment_rows = (
        db.query(Commitment.hubspot_deal_id)
        .filter(
            Commitment.kind == "sow",
            Commitment.hubspot_deal_id.isnot(None),
            Commitment.closed_at.isnot(None),
            Commitment.closed_source == "manual",
        )
        .distinct()
        .all()
    )

    out: list[str] = []
    seen: set[str] = set()
    for (deal_id,) in list(signal_rows) + list(commitment_rows):
        key = str(deal_id or "")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ── Tuning ───────────────────────────────────────────────────────────────────


def not_right_rates(
    db: Session, *, since: datetime | None = None
) -> dict[str, dict[str, float | int]]:
    """The "Not right" rate per signal type — a first-class metric, not a debug aid.

    "Not right" is a LABELLED SIGNAL, not a dismiss. It is
    both the input that tunes ``COCKPIT_STAGE_LAG_MIN_CONFIDENCE`` and the only
    honest answer to "is this detector helping?", which is why it is computed
    from the rows rather than remembered.

    Keys are the signal name when the target stage maps back to one, and
    ``"stage:<id>"`` when it does not: ``commitment_stage_lags`` has no
    ``signal`` column and the eleven tables are final, so the derivation is
    lossy (see :func:`signal_for_stage`) and an unmappable stage is reported
    under its own id rather than folded into a bucket that would misattribute
    it. ``"unmapped"`` collects the rows with no target stage at all.

    ``rate`` is rejections over *decided* rows. Still-open exceptions are
    counted separately and excluded from the denominator: an exception nobody
    has looked at yet is not evidence either way, and including it would make
    the rate improve simply because the worklist got longer.
    """
    query = db.query(CommitmentStageLag)
    if since is not None:
        query = query.filter(CommitmentStageLag.created_at >= since)

    counts: dict[str, dict[str, int]] = {}
    for row in query.all():
        signal = signal_for_stage(row.evidence_stage_id)
        if signal is not None:
            key = signal
        elif row.evidence_stage_id:
            key = f"stage:{row.evidence_stage_id}"
        else:
            key = "unmapped"
        bucket = counts.setdefault(key, {"open": 0, "applied": 0, "rejected": 0})
        status = str(row.status)
        if status in bucket:
            bucket[status] += 1

    out: dict[str, dict[str, float | int]] = {}
    for key, bucket in counts.items():
        decided = bucket["applied"] + bucket["rejected"]
        out[key] = {
            **bucket,
            "decided": decided,
            "rate": round(bucket["rejected"] / decided, 3) if decided else 0.0,
        }
    return out


# ── Import-time invariants ───────────────────────────────────────────────────
# The extractor's validators assert their enum equals STAGE_SIGNALS. This covers
# the other half: a one-sided edit here would leave the detector mapping keys
# the extractor never emits (or the reverse), and the result is zero exceptions
# with no error anywhere — the silent no-op this module's docstring opens with.
# Fail the import instead. (Which STAGE each signal maps to is configuration;
# ``_parse_stages`` drops a tag that names no signal, with a warning.)
assert tuple(SIGNAL_TO_STAGE) == STAGE_SIGNALS, (
    "SIGNAL_TO_STAGE's keys must be exactly STAGE_SIGNALS, in order — "
    f"got {tuple(SIGNAL_TO_STAGE)!r} vs {STAGE_SIGNALS!r}"
)
