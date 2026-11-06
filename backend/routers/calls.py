"""Sales call capture — start a call, list pending wrap-ups, approve what gets written.

Mounted on the pilot-gated ``core`` parent in ``backend/main.py`` (which adds the
``/api`` prefix), so every endpoint here already carries
``require_full_app_access``. On top of that each one checks
``settings.CCS_ENABLED`` and 404s when it is off: three of this feature's
external dependencies are admin actions outside this repo, so the normal state of
a fresh environment is *partly* provisioned, and a half-provisioned environment
must not half-run the feature.

``POST /{id}/wrapup/approve`` is the endpoint that matters. It is the only path
from an AI proposal to a client's HubSpot record, and it is modelled on
``rubik_core/crm_sync/apply.py``, which already solved this exact problem:

* **A compare-and-swap claim per item that doubles as the IDOR guard.** The
  claim matches the session's ``ae_email`` in the same UPDATE that advances the
  item's status, so another AE's item id simply does not resolve and there is no
  code path where an ownership check can be forgotten. It is also what makes a
  double-click, a second browser tab and a retry-after-timeout resolve to one
  decision.
* **An edited value is not more trusted than the model's.** A hand-typed vendor
  value goes back through the same live HubSpot option closure the extractor
  passed; the edit box is not a way around the gate.
* **A drift check before writing.** ``current_value`` was read when the proposal
  was made. If HubSpot has moved since, the AE is shown what is actually there
  and asked again, rather than silently overwriting a colleague.
* **One ``call_hubspot_writes`` row per intended mutation, keyed on a
  deterministic hash.** HubSpot has no idempotency key of its own, so that
  UNIQUE column is the entire mechanism behind "approving twice writes
  once".

Writes run **synchronously in the request path**, deliberately:
``HUBSPOT_WRITE_TOKEN`` is mounted only on the main service, so a write
attempted from a Cloud Run Job raises ``HubSpotWriteRefused`` today.
They are short and bounded — a handful of requests, far
under Cloud Run's 300s cap — and run inside ``run_in_threadpool`` so the
blocking HTTP calls never occupy the event loop.

Transcript text is a FIELD-level permission, not a router-level one. Every quote
this module serializes passes ``backend.auth.user_can_read_transcript_text``
per row: an AE reads their own calls, a manager reads their team's, and the
supervisor reads none — no exception, including deals they are working.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import require_ccs, user_can_read_transcript_text
from rubik_core.brand import PRODUCT_NAME
from rubik_core.call_capture import transcript_ingest as _ingest
from rubik_core.call_capture.provenance import (
    ATTR_KEY as CRM_PROVENANCE_ATTR,
)
from rubik_core.call_capture.provenance import (
    session_source_visible,
    stamp_session_source,
    visible_session_ids,
)
from rubik_core.call_capture.review import (
    CLAIMABLE_ITEM_STATUSES,
    failed_analysis_marker_exists,
    reviewable_items_exist,
)
from rubik_core.call_capture.scripts import remember_products, scripts_for_products
from rubik_core.cockpit.business_hours import pin_business_time_zone
from rubik_core.config import settings
from rubik_core.database.call_capture import (
    CallExtraction,
    CallExtractionItem,
    CallHubspotWrite,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.database.call_scripts import CallSessionScript
from rubik_core.database.db import get_db
from rubik_core.database.models import User
from rubik_core.database.schemas import UserProfile
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT
from rubik_core.retrieval.hubspot_write import (
    HubSpotWriteClient,
    HubSpotWriteError,
    HubSpotWriteRefused,
    company_writable,
    friendly_write_error,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calls", tags=["calls"])


# ── Constants ────────────────────────────────────────────────────────────────

# Double-click window for POST /start. Wall clock on purpose — this is a
# de-duplication window, not an SLA clock, so the business-hours calculator
# has nothing to say about it. The 60 seconds are about quota: Meet allows 100
# ``spaces.create``/min/project and only 10/min/user, so a UI that burns two
# spaces per click runs an AE out of them in five clicks.
_START_DEDUPE_SECONDS = 60

# The AE's wrap-up backlog. A cap rather than pagination because a list this
# long is already a process failure, not a browsing problem.
_PENDING_LIMIT = 50

# Transcript entries shown either side of a cited quote. Two is enough to see
# who asked the question the quote answers, which is the usual reason an AE
# doubts a proposal.
_EVIDENCE_CONTEXT_RADIUS = 2

# One wrap-up screen's worth of decisions. Enforced by pydantic so an oversized
# body is rejected before any claim runs.
_MAX_DECISIONS = 200

# How long a claimed-but-unfinished ``call_hubspot_writes`` row is treated as
# held by whoever claimed it. Comfortably longer than any approval can run —
# Cloud Run caps a request at 300s and a claim covers a single HubSpot call —
# so a row older than this belongs to a process that died, not to one still
# working. Retrying inside the lease is what would double-write a client's CRM
# record; refusing to retry after it would leave a call permanently unlogged.
_WRITE_CLAIM_LEASE = timedelta(minutes=15)

# Items an approval may act on. ``write_failed`` is included so a transient
# HubSpot error is retryable from the wrap-up screen without re-extracting —
# safe precisely because every write is keyed in ``call_hubspot_writes``.
# Per-item outcomes the wrap-up screen renders. Stable strings, because the UI
# branches on them:
#   written            — reached HubSpot on this request
#   already_written    — a ``call_hubspot_writes`` row already succeeded (AC 6)
#   recorded           — accepted; there is no HubSpot write for this kind
#   triage             — ``needs_new_option``: a value HubSpot has no option for
#   rejected           — the AE said no
#   skipped            — unknown item, or already decided by someone/something
#   needs_confirmation — ``is_overwrite`` without the per-item confirmation
#   invalid            — the (possibly edited) value failed the vocabulary gate
#   drift              — HubSpot moved since the proposal; re-anchored, ask again
#   write_failed       — HubSpot refused or errored; retryable, and named to
#                        match both the item's own status and the frontend union
DECISION_RESULTS: tuple[str, ...] = (
    "written",
    "already_written",
    "recorded",
    "triage",
    "rejected",
    "skipped",
    "needs_confirmation",
    "invalid",
    "drift",
    "write_failed",
)

# The order the wrap-up screen reviews kinds in: what changes the CRM record
# first, then what starts a clock, then what creates new work. NOT
# ``ITEM_KINDS`` order — that tuple is a database vocabulary, and reusing it
# here would tie the screen's layout to a CHECK constraint.
_GROUP_ORDER: tuple[str, ...] = (
    "vendor_field",
    "sow_request",
    "lead",
    "task",
    "stage_signal",
    "contract_expiry",
)

# Where the SPA renders a wrap-up. Interpolated into the HubSpot call
# engagement's body so anyone reading the CRM record can get back to the
# evidence. Must agree with the route registered in ``frontend/src/App.tsx``;
# a mismatch is a dead link inside a client's CRM, not a 404 anyone notices.
_WRAPUP_PATH = "/calls/{call_session_id}/wrapup"

# Rows the manual company picker offers. Matches ``/api/xray/customers``, which
# is the typeahead this one exists to replace for a Sales AE — X-Ray's gate
# (``auth.user_can_access_xray``) is a different audience that moves for its own
# reasons (it was Systems-only for the whole soft launch, and every non-admin AE
# got a 403 on the one control that unmatched and ambiguous calendar rows depend
# on). Delegating to the same ``search_companies`` behind ``require_ccs`` keeps
# this picker answering to call capture's own audience, whatever X-Ray's is.
_COMPANY_SEARCH_LIMIT = 10

# Products one call may be started against. Not a pydantic ``max_length``, on
# purpose: an oversized list is a broken client, and a 422 there would refuse to
# start a real call that is about to begin. The list is truncated with a warning
# instead — the AE gets their call, and the log says what was dropped. Twenty is
# far above any real discovery call (a whole product set is usually smaller) and
# far below anything that would make the ``IN`` lookup expensive.
_MAX_START_PRODUCT_KEYS = 20


# ── Schemas ──────────────────────────────────────────────────────────────────


class StartCallRequest(BaseModel):
    """Launch a captured call, from the calendar or ad hoc.

    ``hubspot_company_id`` is optional because the ad-hoc path exists: a call
    the sweep discovers from a conference record has no company binding either,
    and requiring one here would only push AEs back to opening Meet directly —
    where transcription cannot be enabled at all.

    ``calendar_event_id`` is what decides which of the two starts this is, and
    the two are genuinely different operations rather than one with a flag:

    * WITHOUT it (``origin=CALL_ORIGIN_ASSISTANT``) Cubie creates a Meet space of
      its own with ``artifactConfig`` set at creation.
    * WITH it (``origin="calendar"``) Cubie creates NO space. The calendar
      already made one, the AE is going to join it from the invite, and the
      only thing left to do is arm transcription on it — best-effort, because
      a client-hosted space is in another tenant, a conference already running
      cannot be armed, and a recurring series shares one space. The company
      binding is the part that always works, and it is the part the wrap-up
      needs to write to the right CRM record without asking.

    ``meet_meeting_code`` and ``meet_uri`` are the calendar's own values, passed
    back rather than re-fetched: ``GET /scheduled`` already read the event, and
    a second Calendar round trip per Start would add a failure mode to the one
    action that must not have one. The code is REQUIRED alongside
    ``calendar_event_id`` (422 without it) because it is the only address
    ``spaces.get`` accepts for a space Cubie did not create — without it there
    is nothing to arm and nothing to explain.

    ``product_keys`` is what the AE picked on the calendar row: which products
    this call is about, and therefore which checklists it is running against. A
    LIST, not a field, because a call that surfaces three products should
    produce three leads (AEs are supposed to split them) — one product per call
    is the failure this workstream exists to fix, not the shape to encode. It is
    honoured on the CALENDAR path only: the picker lives on the calendar row,
    which is the screen that has a company to remember the pick against and the
    time to render a checklist before the call connects. The ad-hoc path accepts
    the field and ignores it rather than pretending otherwise.
    """

    hubspot_company_id: str | None = None
    hubspot_contact_id: str | None = None
    hubspot_deal_id: str | None = None
    calendar_event_id: str | None = None
    meet_meeting_code: str | None = None
    meet_uri: str | None = None
    # OMITTED and EMPTY are different requests and the difference is load-bearing
    # — see ``_attach_products``. Sent-and-empty is the AE deselecting every
    # product, which CLEARS their remembered pick for this client; omitted is an
    # older UI (or a retry that dropped the field), which must leave last week's
    # pick alone. ``model_fields_set`` is what tells them apart.
    product_keys: list[str] = Field(default_factory=list)
    # "suppressed" is a first-class outcome, not an error: the space is created
    # with artifact generation OFF and the call_sessions row still exists, so
    # the FACT of the call stays visible to the cockpit even though its CONTENT
    # is never captured. That distinction is the whole point — "a call is never
    # invisible even when its content is" — and it is why a
    # confidential call is a consent value rather than a reason not to record a
    # session.
    capture_consent: str = Field(default="granted", pattern="^(granted|suppressed)$")


class WrapupDecision(BaseModel):
    item_id: str
    accept: bool
    edited_value: str | None = None
    # Never blind-overwrite a non-null HubSpot value. An item
    # flagged ``is_overwrite`` needs this per-item flag as well as ``accept``,
    # so "approve all" can never quietly replace a colleague's data.
    confirm_overwrite: bool = False


class WrapupApproveRequest(BaseModel):
    decisions: list[WrapupDecision] = Field(
        default_factory=list, max_length=_MAX_DECISIONS
    )
    # The AE reviewed the wrap-up and there was genuinely nothing to file. A
    # COMPLETE outcome, not a skipped one, which is why
    # it is persisted on the row and counted in the audit trail.
    nothing_here: bool = False
    # Both optional and both only for measurement: the feature's bar is a
    # median wrap-up under 90 seconds, and that is the measure that decides
    # whether any of this gets used.
    wrap_opened_at: datetime | None = None
    client_elapsed_ms: int | None = Field(default=None, ge=0)


# ── Small helpers ────────────────────────────────────────────────────────────


def _require_ccs_enabled() -> None:
    """404 unless ``CCS_ENABLED``.

    404 rather than 403: a feature that is off is not a permission problem, and
    an environment where the Meet scopes have not been granted must not offer a
    "Start call" button that creates rows nothing will ever ingest.
    """
    if not settings.CCS_ENABLED:
        raise HTTPException(
            status_code=404, detail="Sales call capture is not enabled here."
        )


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _epoch_ms(value: datetime) -> int:
    """Milliseconds since the epoch, treating a naive datetime as UTC.

    Every ``DateTime`` column in this repo is naive UTC (``rg 'timezone=True'``
    returns nothing), and HubSpot timestamps are unambiguous integers. Mirrors
    the private helper of the same name in ``hubspot_write.py`` rather than
    importing it, so a change there cannot silently reinterpret these stamps.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _naive_utc(value: datetime | None) -> datetime | None:
    """Normalize a client-supplied datetime to the naive-UTC column convention."""
    if value is None:
        return None
    from rubik_core.cockpit.business_hours import as_naive_utc

    return as_naive_utc(value)


def _duration_ms(session: CallSession) -> int | None:
    if not (session.started_at and session.ended_at):
        return None
    delta = (session.ended_at - session.started_at).total_seconds()
    return int(delta * 1000) if delta > 0 else None


def _age_hours(session: CallSession) -> float | None:
    """BUSINESS hours since the call ended, through the cockpit's one clock.

    Not ``(now - ended_at)``. A call that ends Friday afternoon is not 60 hours
    old on Monday morning, and a backlog chip that says it is trains the AE to
    ignore the chip — the exact false positive the business-hours clock exists
    to prevent. ``business_hours.py`` is the single place elapsed time is computed,
    so this asks it rather than reaching for a ``timedelta``.

    ``ended_at`` is stamped by the sweep from the conference record, so a
    session that reached ``no_transcript`` through the "ended >30 min ago with
    zero entries" rule always has one. ``created_at`` is the fallback rather
    than ``None`` because an age of "unknown" renders as an empty chip, and an
    ageless row in the AE's backlog is one nobody feels any pressure to clear.
    """
    from rubik_core.cockpit.business_hours import business_hours_between

    anchor = session.ended_at or session.created_at
    if anchor is None:
        return None
    return round(business_hours_between(anchor, datetime.utcnow()), 1)


def _vendor_label(prop: str | None) -> str:
    """A vendor field's row heading, title-cased from its internal name.

    No label lookup: the property-definition fetch in
    ``call_capture/hubspot_schema.py`` returns option VALUES only, and a second
    live lookup to render a row heading would cost a request per wrap-up.
    """
    if not prop:
        return "Company field"
    return prop.replace("_", " ").title()


def _item_label(item: CallExtractionItem) -> str:
    """One human-readable line for the row heading.

    Built here rather than at extraction time so a wording change is a UI
    change: the stored row keeps the machine-readable target and value, which
    is what the write layer and any later accuracy measurement need.
    """
    payload = item.payload or {}
    if item.kind == "vendor_field":
        return _vendor_label(item.target_property)
    if item.kind == "lead":
        name = payload.get("lead_name") or item.proposed_value or "New lead"
        product = payload.get("product")
        return f"New lead: {name}" + (f" — {product}" if product else "")
    if item.kind == "task":
        return payload.get("subject") or item.proposed_value or "Follow-up task"
    if item.kind == "sow_request":
        kind = (payload.get("kind") or "").replace("_", "-")
        return f"SOW requested{f' ({kind})' if kind else ''}"
    if item.kind == "stage_signal":
        signal = (payload.get("signal") or item.proposed_value or "").replace("_", " ")
        return f"Deal stage signal: {signal}".strip()
    if item.kind == "contract_expiry":
        vendor = payload.get("vendor") or "Vendor"
        expires = payload.get("expires_on") or item.proposed_value
        return f"{vendor} contract expires {expires}" if expires else vendor
    return item.proposed_value or item.kind


def _idempotency_key(
    call_session_id: str,
    target_object: str | None,
    target_id: str | None,
    target_property: str | None,
) -> str:
    """The deterministic key behind "approving twice writes once" (AC 6).

    The tuple is session, object, record, property, with
    ``None`` folded to an empty string rather than the literal ``"None"``, so
    the key of a create (no ``target_id``) stays legible in the ledger. Changing
    this formula re-opens every past write for a second attempt, which is why it
    lives in one function.
    """
    raw = "|".join(
        [
            call_session_id,
            target_object or "",
            target_id or "",
            target_property or "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ae_name(db: Session, ae_email: str) -> str:
    """Display name for the call's AE, falling back to the address.

    A missing ``users`` row is expected, not an error: offboarding DELETES it,
    and a call that happened does not stop having happened when its AE leaves.
    """
    row = db.query(User).filter(func.lower(User.email) == ae_email).first()
    return (row.name if row and row.name else ae_email) or ae_email


# ── Reading a session: rows, and separately, text ────────────────────────────


def _team_emails(db: Session, user: UserProfile) -> frozenset[str]:
    """The caller's resolved team, or just themselves.

    ``_authz.email_scope`` is the one org graph in this codebase
    (``eos_seats.reports_to_id`` bridged to ``users`` by ``eos_people.email``);
    imported locally because it pulls in the whole EOS model layer. A caller
    with no ``eos_people`` row resolves to a set containing only their own
    address, which is the safe direction here — it grants nothing.
    """
    from backend.routers.eos import _authz

    try:
        return frozenset(_authz.email_scope(db, user).owner_emails)
    except Exception:  # noqa: BLE001 — a scope failure must not become an outage
        logger.warning(
            "[ccs] could not resolve a team scope for %s; treating as own-only",
            getattr(user, "email", None),
            exc_info=True,
        )
        return frozenset({_fold(getattr(user, "email", None))})


def _session_for_reader(
    session_id: str, user: UserProfile, db: Session
) -> tuple[CallSession, bool, bool]:
    """Resolve a session for a READ. Returns (session, is_own, may_read_text).

    Three separate questions, deliberately answered separately:

    1. Does the row exist — 404.
    2. May this caller see the row at all — 403. Their own call, a call by
       someone in their resolved team, or (for support) an admin.
    3. May they see the transcript TEXT — a field-level rule delegated to
       ``backend.auth.user_can_read_transcript_text``. The supervisor and the
       exec pass question 2 on a team call and still fail question 3, with no
       exception, so the redaction MUST happen in the serializer and not here.
    """
    session = db.get(CallSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="call session not found")

    caller = _fold(getattr(user, "email", None))
    ae = _fold(session.ae_email)
    is_own = bool(caller) and caller == ae

    team = frozenset({caller}) if is_own else _team_emails(db, user)
    if not (is_own or ae in team or user.is_admin):
        # 403 rather than 404: the caller is a legitimate Cubie user asking
        # about a real call, and "not yours" is the honest answer.
        raise HTTPException(status_code=403, detail="not your call")

    may_read_text = user_can_read_transcript_text(
        user, call_owner_email=ae, team_emails=team
    )
    return session, is_own, may_read_text


def _session_for_ae(session_id: str, user: UserProfile, db: Session) -> CallSession:
    """Resolve a session the caller may ACT on. Only the AE may approve.

    A manager reads their team's wrap-ups; nobody else approves them. Approval
    writes to a client's CRM record and logs the call under the AE's HubSpot
    owner id, so it is not a supervision action — and the CAS claim below
    re-checks the same ownership inside the UPDATE, so this is a friendly error
    message rather than the security boundary.
    """
    session = db.get(CallSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="call session not found")
    if _fold(session.ae_email) != _fold(getattr(user, "email", None)):
        raise HTTPException(
            status_code=403, detail="only the AE on a call can approve its wrap-up"
        )
    return session


# ── Serialization ────────────────────────────────────────────────────────────


def _evidence_context(
    item: CallExtractionItem,
    entries_by_id: dict[str, CallTranscriptEntry],
    entries_by_sequence: dict[int, CallTranscriptEntry],
) -> list[dict]:
    """The cited utterance plus the two either side, cited line marked.

    Empty is a normal answer twice over: a Fathom item legitimately has no
    citation (Fathom has no per-utterance identifiers),
    and a caller who may not read transcript text never gets here at all.
    """
    if not item.evidence_entry_id:
        return []
    cited = entries_by_id.get(item.evidence_entry_id)
    if cited is None:
        # SET NULL on the FK means a purged transcript leaves the id dangling
        # only if the row was deleted some other way; either way the quote in
        # ``evidence_text`` survives and is what the AE actually reads.
        return []
    low = cited.sequence - _EVIDENCE_CONTEXT_RADIUS
    high = cited.sequence + _EVIDENCE_CONTEXT_RADIUS
    out = []
    for seq in range(low, high + 1):
        entry = entries_by_sequence.get(seq)
        if entry is None:
            continue
        out.append(
            {
                "sequence": entry.sequence,
                "speaker_name": entry.speaker_name,
                "text": entry.text,
                "is_cited": entry.id == cited.id,
            }
        )
    return out


def _item_out(
    item: CallExtractionItem,
    *,
    may_read_text: bool,
    entries_by_id: dict[str, CallTranscriptEntry],
    entries_by_sequence: dict[int, CallTranscriptEntry],
) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "target_object": item.target_object,
        "target_id": item.target_id,
        "target_property": item.target_property,
        "label": _item_label(item),
        "proposed_value": item.proposed_value,
        "current_value": item.current_value,
        "edited_value": item.edited_value,
        "is_overwrite": bool(item.is_overwrite),
        "needs_new_option": item.needs_new_option,
        "confidence": item.confidence,
        # Both of these are transcript text, so both are gated on the same
        # field-level policy. Redacting one and not the other would leak the
        # quote through the context window.
        "evidence_text": item.evidence_text if may_read_text else None,
        "evidence_context": (
            _evidence_context(item, entries_by_id, entries_by_sequence)
            if may_read_text
            else []
        ),
        "payload": item.payload or {},
        # Extras beyond the agreed shape, both load-bearing for the screen: the
        # status is how a retry of a failed write is offered, and
        # ``low_confidence`` is the server's own threshold so the UI does not
        # have to hard-code 0.5 (settings.CCS_MIN_CONFIDENCE_SHOW).
        "status": item.status,
        "low_confidence": (item.confidence or 0.0) < settings.CCS_MIN_CONFIDENCE_SHOW,
    }


#: The closed vocabulary of ``transcript_coverage["reason"]`` codes this router
#: will emit. It is a FIXED SET rather than free text for one reason: coverage is
#: the one block on this payload that a supervisor sees while every quote beside
#: it is redacted, so a ``reason`` that could carry arbitrary strings would be a
#: hole straight through the field-level transcript fence — one
#: sentence of explanation from the ingest side and the supervisor is reading the
#: call. Anything outside this set is replaced with ``unknown`` below, which
#: costs a label and leaks nothing.
#:
#: Built from the PRODUCER's own constants rather than restated as literals here.
#: ``transcript_ingest.compute_coverage`` is the only thing that ever writes this
#: key, so a second spelling of its vocabulary in this file is a mismatch waiting
#: to happen — and it fails in the quietest possible way: a renamed code stops
#: being recognized, every wrap-up degrades to ``unknown``, and nothing breaks
#: loudly enough to notice. Naming the constants makes a rename a NameError at
#: import instead. Adding a code stays a deliberate two-line change (define it
#: there, admit it here), which is the friction worth keeping.
COVERAGE_REASONS = frozenset(
    {
        _ingest.COVERAGE_COVERED,  # the transcript runs to the end of the call
        _ingest.COVERAGE_TRUNCATED,  # a low ratio AND a materially long gap
        _ingest.COVERAGE_WITHIN_RATIO,  # a real gap, but proportionally most is here
        _ingest.COVERAGE_SHORT_GAP,  # under the ratio, but the tail is minutes
        _ingest.COVERAGE_CLOCK_SKEW,  # an utterance past the end of the call
        _ingest.COVERAGE_CALL_WINDOW_UNKNOWN,  # no started_at/ended_at, no ratio
        _ingest.COVERAGE_NO_OFFSETS,  # entries exist, none carries an end offset
        _ingest.COVERAGE_FATHOM_NO_OFFSETS,  # the common Fathom case
    }
)

#: What an unrecognized code degrades to. Never the code itself.
_COVERAGE_REASON_FALLBACK = "unknown"


def _coverage_out(session: CallSession) -> dict | None:
    """How much of the call the transcript actually covers, or ``None``.

    ``artifactConfig.autoTranscriptionGeneration`` is a DEFAULT, not a lock —
    Google's own words are that hosts and co-hosts can still override it during
    the meeting — and the Meet API has no method that stops or starts
    transcription on a running conference, so Cubie can neither prevent nor
    observe a host switching it off mid-call. What comes back afterwards is
    indistinguishable from a complete transcript: the conference reports ENDED,
    the sweep ingests it correctly, and the AE gets a wrap-up that looks entirely
    normal built from part of the call.

    That is worth a field on the payload because of WHEN people stop
    transcription — mid-call, when the conversation turns to pricing, a
    competitor or a complaint, which is exactly where the SOW ask tends to live.
    A partial result that looks complete is the failure mode this whole feature
    spends its code avoiding.

    ``None`` means "no information", NOT "fully covered". Every session that
    predates this change is in that state, and rendering those as 100% captured
    would be inventing a reassurance nobody measured. The two cases are
    different and the screen says different things about them.

    NOT gated on ``may_read_text``. Coverage is metadata — durations and a code —
    and a supervisor whose job is chasing the work is entitled to know a call was
    only partly captured even though they may never read a word of it. Gating it
    would hide a data-quality problem from the one person looking for it. The
    fence is kept on the other side instead: ``reason`` is closed to
    ``COVERAGE_REASONS``, so no content can ride out through this block.

    ``truncated`` is read, not recomputed. The rule (both a low ratio *and* a
    real gap, never either alone) lives at ingest; evaluating it a second time
    here would be two implementations of one threshold, and they would drift.
    """
    raw = (session.attrs or {}).get("transcript_coverage")
    if not isinstance(raw, dict):
        return None

    covered_ms = raw.get("covered_ms")
    if not isinstance(covered_ms, (int, float)) or isinstance(covered_ms, bool):
        # Without a numerator there is nothing to say, and a half-written block
        # is worse than a missing one — it renders as a confident zero.
        return None

    call_ms = raw.get("call_ms")
    gap_ms = raw.get("gap_ms")

    reason = raw.get("reason")
    if reason not in COVERAGE_REASONS:
        # Logged so a writer that drifts from this vocabulary is discoverable
        # rather than silently degraded — but the offending value is NOT logged.
        # The whole reason this field is closed is that it might contain the
        # call, and a leak into Cloud Logging is still a leak.
        logger.warning(
            "[ccs] session %s carries a transcript_coverage reason outside "
            "COVERAGE_REASONS; serving %r",
            session.id,
            _COVERAGE_REASON_FALLBACK,
        )
        reason = _COVERAGE_REASON_FALLBACK

    return {
        # Rounded the same way ``duration_minutes`` is, so the two figures on the
        # header can be read against each other. Zero is NOT folded to None the
        # way ``duration_minutes`` folds it: "0.0 minutes of this call were
        # captured" is the single most important thing this block can say.
        "covered_minutes": round(covered_ms / 60000.0, 1),
        "call_minutes": (
            round(call_ms / 60000.0, 1) if isinstance(call_ms, (int, float)) else None
        ),
        "gap_minutes": (
            round(gap_ms / 60000.0, 1) if isinstance(gap_ms, (int, float)) else None
        ),
        "truncated": bool(raw.get("truncated")),
        "reason": reason,
    }


def _session_out(
    db: Session,
    session: CallSession,
    *,
    may_read_text: bool,
    needs_review: bool = False,
) -> dict:
    duration_ms = _duration_ms(session)
    return {
        "call_session_id": session.id,
        "company_name": (session.attrs or {}).get("company_name"),
        "ae_name": _ae_name(db, _fold(session.ae_email)),
        "duration_minutes": round(duration_ms / 60000.0, 1) if duration_ms else None,
        "ended_at": _iso(session.ended_at),
        "status": session.status,
        # A previous "nothing here" was true at the first review, but late
        # transcript suggestions still need a decision on this visit.
        "nothing_here": bool(session.nothing_here) and not needs_review,
        # Extra, and stated rather than implied: it lets the screen label itself
        # honestly as metadata-only instead of rendering empty quote boxes.
        "can_read_transcript": may_read_text,
        # Derived per-session bookkeeping, so it lives on ``attrs`` beside
        # ``transcript_ingest`` and ``client_elapsed_ms`` rather than in a
        # migration. If capture coverage ever becomes a REPORTING dimension —
        # the exec view already wants "capture coverage per AE" — it needs to be
        # promoted to a real column, because that is a query nobody should be
        # running against a JSON blob.
        "transcript_coverage": _coverage_out(session),
    }


def _extraction_out(
    session: CallSession, extraction: CallExtraction | None = None
) -> dict:
    """A closed, content-free verdict for the AE's wrap-up.

    The retry marker contains no provider exception. The persisted extraction
    may contain transcript and model output, so only validated chunk counts
    cross this boundary.
    """
    if session.status == "no_transcript":
        return {"status": "not_applicable", "chunks_failed": 0, "chunks_total": 0}
    if session.status == "wrapped" and isinstance(
        (session.attrs or {}).get("extraction_retry"), dict
    ):
        return {"status": "retrying", "chunks_failed": 0, "chunks_total": 0}
    if session.status in ("scheduled", "in_progress", "ended", "transcript_ready"):
        status = (
            "retrying"
            if session.status == "transcript_ready"
            and isinstance((session.attrs or {}).get("extraction_retry"), dict)
            else "pending"
        )
        return {"status": status, "chunks_failed": 0, "chunks_total": 0}
    raw = extraction.raw_output if extraction is not None else None
    if isinstance(raw, dict):
        total = raw.get("chunks_total")
        failed = raw.get("chunks_failed")
        if (
            type(total) is int
            and type(failed) is int
            and total > 0
            and 0 < failed <= total
        ):
            return {"status": "partial", "chunks_failed": failed, "chunks_total": total}
    return {"status": "complete", "chunks_failed": 0, "chunks_total": 0}


# ── POST /start ──────────────────────────────────────────────────────────────


def _recent_duplicate(
    db: Session, *, ae_email: str, company_id: str | None, consent: str
) -> CallSession | None:
    """The session a double-click would otherwise duplicate.

    Keyed on (AE, company, consent) inside a 60-second window, and NOT on
    consent-agnostic identity on purpose: an AE who starts a captured call and
    immediately restarts it as confidential means the second one, and handing
    back the transcribing space would capture content they just asked not to be
    captured.

    Only ``scheduled`` sessions match — once a call is in progress or ended,
    "start another call with this company" is a real second call.

    This is a read, so two genuinely simultaneous requests can still both miss
    it. That is accepted: the case being defended against is one human clicking
    twice, and the alternative (a unique constraint on a time bucket) would
    reject the legitimate second call at second 61 of a bad minute.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=_START_DEDUPE_SECONDS)
    query = (
        db.query(CallSession)
        .filter(func.lower(CallSession.ae_email) == ae_email)
        .filter(CallSession.origin == CALL_ORIGIN_ASSISTANT)
        .filter(CallSession.status == "scheduled")
        .filter(CallSession.capture_consent == consent)
        .filter(CallSession.created_at >= cutoff)
        .filter(CallSession.meet_space_name.isnot(None))
    )
    query = (
        query.filter(CallSession.hubspot_company_id == company_id)
        if company_id
        else query.filter(CallSession.hubspot_company_id.is_(None))
    )
    return query.order_by(CallSession.created_at.desc()).first()


def _company_name(company_id: str | None) -> str | None:
    """Resolve the company's name once, at start, with the READ token.

    Stored on ``call_sessions.attrs`` because three consumers want it and none
    of them should pay for a live lookup: the extraction prompt reads
    ``attrs["company_name"]``, the wrap-up header shows it, and the pending list
    would otherwise need one HubSpot request per row. A failure is not fatal —
    the wrap-up renders without a company name.
    """
    if not company_id:
        return None
    from rubik_core.tenancy.crm_visibility import visible_source_ids

    owned, _, _ = visible_source_ids({company_id}, set(), set())
    if company_id not in owned:
        return None
    try:
        record = HubSpotWriteClient().get_object("companies", company_id, ["name"])
    except Exception as exc:  # noqa: BLE001 — a name is a nicety, not the call
        logger.warning(
            "[ccs] could not read the name of company %s: %s", company_id, exc
        )
        return None
    return ((record.get("properties") or {}).get("name") or None) if record else None


# ── The arming verdict, and why every failure here is still a 200 ────────────
#
# ``calendar_scan.ARMING_STATES`` / ``ARMING_REASONS`` are the closed
# vocabularies both surfaces render, and this module produces the half of them
# that only the WRITE path can know: ``armed_by_cubie``, ``already_on``,
# ``arming_unsupported``. The read path (``_arming_precondition``) produces the
# free ones. Anything outside those frozensets is dropped by
# ``_stored_arming`` on the way back out and the row silently reverts to a
# precondition guess, so the strings below are load-bearing, not labels.
#
# Nothing here raises. A call Cubie cannot transcribe is still a call Cubie
# should know about: the session row carries the company binding, which is what
# lets the wrap-up write to the right CRM record, and the AE has the calendar's
# own Meet link either way. Turning an arming failure into a 502 would throw
# away the working half of the feature to report the best-effort half — and
# would do it at the moment the AE is trying to join a client call.


def _verdict(state: str, reason: str, detail: str | None = None) -> dict:
    """One arming verdict. Literals in, so the vocabulary tests can pin them."""
    return {"state": state, "reason": reason, "detail": detail}


def _suppressed_arming() -> dict:
    """The verdict for a confidential call: Cubie never asked Meet for anything.

    ``ARMING_REASONS`` has no consent member and this is not the place to add
    one — the frozenset is shared with the calendar screen and the sweep. So the
    honest reading inside the existing vocabulary is ``unknown`` /
    ``not_checked_yet``: nothing was armed, nothing was checked, and the detail
    says why. It is stored rather than omitted precisely so the calendar row
    replays THIS sentence instead of re-deriving a precondition that would
    describe arming as merely pending.

    Never ``armed``, obviously, and never ``not_armable`` either: the space is
    perfectly armable and Cubie declined on purpose, which is a different fact
    from Google refusing.
    """
    return _verdict(
        "unknown",
        "not_checked_yet",
        f"This call is marked confidential, so {PRODUCT_NAME} did not ask Google to "
        "transcribe it and will capture no transcript, recording or notes.",
    )


def _arm_calendar_space(ae_email: str, meeting_code: str) -> tuple[dict, dict | None]:
    """Arm the calendar's existing space. Returns ``(verdict, arming result)``.

    Synchronous and blocking — call it through ``run_in_threadpool``.

    Every branch is a named outcome, and which exception arrived is the whole
    diagnosis (``rubik_core/tools/meet.py`` classifies by message as well as
    status, so a scope 403 and a licence 400 do not land in the same bucket):

    * :class:`MeetSpaceNotFound` → ``external_host``. A 404 on ``spaces.get``
      means the code is wrong/expired OR the space belongs to another tenant.
      For a meeting that came off the AE's own calendar with a live Meet link,
      a client-hosted space is overwhelmingly the likely cause, and it is also
      the reading that produces the right advice.
    * :class:`MeetSpaceNotPatchable` → ``not_owner``. This subject asked and
      Meet said no. NOT a claim about who is permitted to patch a space they
      did not create — that is unverified on this tenant.
    * :class:`MeetArtifactsUnsupported` → ``arming_unsupported``. Tenant-wide
      (the edition/licence answer), so it will be the same for every other row.
    * :class:`MeetSpaceWriteDisabled` → ``write_disabled``. Cubie's own switch.
    * ``ValueError`` → ``lookup_failed``. The code did not parse, which is
      caught before any HTTP.
    * anything else → ``not_armable`` / ``google_error``. Its own reason, not
      ``lookup_failed``: the space usually WAS found (the typical failure is
      a 400 on the patch, after the lookup succeeded), so "Meet space not found"
      would send the AE looking for the wrong problem. The detail names the HTTP
      status when there is one, and says "could not reach Google" only when
      there is not. ``not_armable`` rather than ``unknown`` on purpose: nothing
      was armed, this flow has no retry, and the call is starting now —
      "unknown" would imply something may still happen. The safe direction is
      never to look armed.

    A successful patch is still not proof of a transcript (Meet has been
    observed to accept an artifact config and ignore it), which is why the
    sweep goes on treating "no transcript arrived" as a normal outcome with a
    reason of its own.
    """
    from rubik_core.tools.meet import (
        GoogleMeetClient,
        MeetArtifactsUnsupported,
        MeetSpaceNotFound,
        MeetSpaceNotPatchable,
        MeetSpaceWriteDisabled,
    )

    try:
        result = GoogleMeetClient(ae_email).arm_space_transcription(
            meeting_code=meeting_code
        )
    except MeetSpaceNotFound as exc:
        logger.info(
            "[ccs] no reachable Meet space for code %s as %s: %s",
            meeting_code,
            ae_email,
            exc,
        )
        return (
            _verdict(
                "not_armable",
                "external_host",
                f"{PRODUCT_NAME} cannot see this meeting's Meet space, which is what a "
                "client-hosted meeting looks like — their space lives in their "
                "Workspace, not ours. Join with the calendar link; the company "
                "binding still works and the call can still be logged.",
            ),
            None,
        )
    except MeetSpaceNotPatchable as exc:
        logger.info("[ccs] Meet refused the arming patch: %s", exc)
        return (
            _verdict(
                "not_armable",
                "not_owner",
                f"Google would not let {PRODUCT_NAME} change this meeting's transcription "
                "setting as you. Ask whoever created the meeting to turn "
                f"transcription on, or start the call from {PRODUCT_NAME} instead.",
            ),
            None,
        )
    except MeetArtifactsUnsupported as exc:
        logger.warning("[ccs] Meet artifact config unsupported on this tenant: %s", exc)
        return (
            _verdict(
                "not_armable",
                "arming_unsupported",
                "Google reported that automatic transcription is not available "
                "for this Workspace edition, so no meeting can be armed — this "
                "is an account-level answer, not something about this call.",
            ),
            None,
        )
    except MeetSpaceWriteDisabled as exc:
        # NOT a 503 here, unlike the ad-hoc path. There, the write IS the
        # feature — no space, no call. Here the session row and the company
        # binding are unaffected, so refusing the start would remove working
        # behaviour to report a switch.
        logger.info("[ccs] Meet writes disabled, so no arming attempted: %s", exc)
        return (
            _verdict(
                "not_armable",
                "write_disabled",
                f"Meet transcription arming is switched off in {PRODUCT_NAME} right now "
                "(CCS_MEET_ENABLED), so no meeting can be armed. Binding the "
                "company still works.",
            ),
            None,
        )
    except ValueError as exc:
        # ``arm_space_transcription`` validates the code before any HTTP, so
        # this is a malformed value rather than a Google failure — and it has a
        # readable message of its own, which "ValueError" would hide.
        logger.warning("[ccs] unusable meeting code %r: %s", meeting_code, exc)
        return (
            _verdict(
                "not_armable",
                "lookup_failed",
                f"'{meeting_code}' is not a Meet meeting code {PRODUCT_NAME} can look up, "
                "so its space could not be found. Join with the calendar link — "
                "the call is still bound to the company and can still be logged.",
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — never fail the start over arming
        logger.exception(
            "[ccs] arming failed for code %s as %s", meeting_code, ae_email
        )
        status = getattr(getattr(exc, "resp", None), "status", None)
        what = (
            f"Google refused {PRODUCT_NAME}'s request to turn transcription on for this "
            f"meeting (HTTP {status})."
            if status
            else f"{PRODUCT_NAME} could not reach Google to turn transcription on for this "
            f"meeting ({exc.__class__.__name__})."
        )
        return (
            _verdict(
                "not_armable",
                "google_error",
                f"{what} Join with the calendar link and turn transcription on "
                "from inside Meet if you want one — the call is still bound to "
                "the company and can still be logged.",
            ),
            None,
        )

    already = str((result or {}).get("state") or "") == "already_armed"
    return (
        _verdict(
            "armed",
            "already_on" if already else "armed_by_cubie",
            "Transcription was already on for this meeting's Meet space, so "
            f"{PRODUCT_NAME} changed nothing."
            if already
            else None,
        ),
        result or {},
    )


def _calendar_session(db: Session, ae_email: str, event_id: str) -> CallSession | None:
    """This AE's existing session for one calendar occurrence, or ``None``.

    The indexed read behind ``UNIQUE(ae_email, calendar_event_id)``, and the
    reason ``calendar_event_id`` is a real column rather than an ``attrs`` key.
    Checked before the write so the common double-click never even reaches
    Google, and again after an ``IntegrityError`` so the genuinely simultaneous
    pair resolves to one session instead of a 500.
    """
    return (
        db.query(CallSession)
        .filter(func.lower(CallSession.ae_email) == ae_email)
        .filter(CallSession.calendar_event_id == event_id)
        .first()
    )


def _space_name_free(db: Session, space_name: str | None) -> bool:
    """True when no session already claims ``space_name``.

    ``call_sessions.meet_space_name`` is UNIQUE, and a recurring series reuses
    ONE Meet space (``rubik_core/tools/meet.py``) — so the second occurrence of
    a weekly client call resolves to the SAME space name as the first, and
    storing it again would violate that constraint and lose the session row.
    The row matters more than the denormalised name: the sweep's correlation
    falls back to ``meet_meeting_code`` (indexed, not unique, newest unbound
    session first), which is exactly the right occurrence.
    """
    if not space_name:
        return False
    return (
        db.query(CallSession.id)
        .filter(CallSession.meet_space_name == space_name)
        .first()
        is None
    )


def _start_out(session: CallSession, arming: dict) -> dict:
    """The ``POST /start`` response. Same shape for both origins.

    ``origin`` is in it so the UI never has to infer which start it got, and
    ``arming`` so a best-effort outcome is visible at the moment the AE acts on
    it rather than only on the next calendar render.
    """
    return {
        "call_session_id": session.id,
        "meet_uri": session.meet_uri,
        "meeting_code": session.meet_meeting_code,
        "origin": session.origin,
        "arming": arming,
    }


def _stored_verdict(session: CallSession) -> dict | None:
    stored = (getattr(session, "attrs", None) or {}).get("capture_arming")
    return stored if isinstance(stored, dict) else None


def _adhoc_arming(consent: str) -> dict:
    """The verdict for ``origin=CALL_ORIGIN_ASSISTANT``, where arming is not best-effort.

    ``create_space(auto_transcribe=True)`` sets ``artifactConfig`` at creation,
    so a space that exists is a space that was armed — there is no partial
    outcome to report. ``armed_by_cubie`` is therefore the truth about what
    happened, and whether Meet accepted the config and then ignored it remains
    a transcript-arrival question the sweep answers, not an arming one.
    """
    if consent != "granted":
        return _suppressed_arming()
    return _verdict("armed", "armed_by_cubie", None)


def _picked_products(body: StartCallRequest) -> list[str]:
    """The AE's product picks — folded, deduped, order kept, bounded.

    Folded with exactly ``strip().lower()``, which is what the ``@validates``
    hooks on ``CallScript.product_key`` and ``CallSessionScript.product_key``
    apply on every write and what ``call_capture/scripts.py`` folds with on every
    read. A reader that normalized differently would not raise — it would report
    a fact as *not discussed* when it was discussed, which is the one false alarm
    that teaches an AE to ignore the gap list.

    Order is the AE's pick order, and the panel renders its sections in it.
    """
    out: list[str] = []
    for value in body.product_keys or []:
        if not isinstance(value, str):
            continue
        key = value.strip().lower()
        if key and key not in out:
            out.append(key)
        if len(out) >= _MAX_START_PRODUCT_KEYS:
            logger.warning(
                "[ccs] truncating an oversized product_keys list (%d sent) — "
                "keeping the first %d",
                len(body.product_keys or []),
                _MAX_START_PRODUCT_KEYS,
            )
            break
    return out


def _link_scripts(db: Session, session: CallSession, keys: list[str]) -> dict:
    """Find-or-create one ``call_session_scripts`` row per resolvable product.

    **Find-or-create, never blind insert.** A repeat POST — the double-click, the
    second tab, the AE who reopened the row to add a product — must not duplicate
    an attachment. ``UNIQUE(call_session_id, product_key)`` is the backstop, but
    the read makes the ordinary repeat a no-op instead of an exception, and it is
    what lets a second Start ADD a newly-picked product to a call that was
    already started with one.

    A key with no live script is SKIPPED and returned, never a 422. Two ways that
    happens and both are ordinary: the product is real but the supervisor has
    not authored its checklist yet (these tables ship empty on purpose), or a draft
    exists but nobody has approved it — and an unapproved script is a machine's
    guess, which never reaches an AE. Neither is a reason to refuse to start a
    call that is about to begin, and both are reported at the wrap-up anyway, as
    a product discussed with no script loaded.

    Flushes nothing and commits nothing; the caller owns the transaction.
    """
    resolved = {
        (script.product_key or ""): script for script in scripts_for_products(db, keys)
    }
    attached_already = {
        (row.product_key or "")
        for row in db.query(CallSessionScript)
        .filter(CallSessionScript.call_session_id == session.id)
        .all()
    }
    attached: list[str] = []
    for key, script in resolved.items():
        if key in attached_already:
            continue
        db.add(
            CallSessionScript(
                id=uuid.uuid4().hex,
                call_session_id=session.id,
                script_id=script.id,
                product_key=key,
            )
        )
        attached.append(key)
    return {
        "attached": attached,
        "skipped": [key for key in keys if key not in resolved],
    }


def _attach_products(
    db: Session, session: CallSession, body: StartCallRequest, *, ae_email: str
) -> dict:
    """Point this call at the products the AE picked, and remember the pick.

    **Never raises, and never blocks the start.** The call is happening; a
    checklist that could not be attached costs the AE a checklist, and a 500 here
    would cost them the capture, the company binding and the transcript. So every
    failure is logged, rolled back and reported as "nothing attached".

    Two writes, in one transaction:

    * ``call_company_product_defaults`` — so the same client defaults to the same
      products next time. Written ONLY when the client actually sent the field:
      ``remember_products`` treats an empty list as *clear the pick*, which is
      right for an AE who deselected everything and catastrophic for an older UI
      that never sends the field at all. ``model_fields_set`` is the only thing
      that can tell those two apart, which is why the decision lives here rather
      than in ``scripts.py``.
    * ``call_session_scripts`` — one row per resolvable APPROVED script.

    The remembered pick is what the AE CHOSE, not what resolved. Those are
    different facts: a product whose script was retired should still come back
    selected next week, with no section, rather than silently unpicking itself.

    The keying company is the request's, falling back to the session's. The
    request's is what the calendar row was bound to, and the calendar row is what
    ``calendar_scan`` will look the memory up by — keying on anything else would
    write a memory that the next render cannot find.

    An ``IntegrityError`` is retried exactly once. It means a concurrent Start
    inserted the same attachment between our read and our flush; the constraint
    did its job, and the second pass sees the winner's rows and adds only what is
    still missing. A second failure is left alone rather than looped over.
    """
    keys = _picked_products(body)
    remember = "product_keys" in body.model_fields_set
    company_id = (body.hubspot_company_id or "").strip() or (
        session.hubspot_company_id or ""
    )
    if not keys and not remember:
        return {"attached": [], "skipped": []}

    for attempt in (1, 2):
        try:
            outcome: dict = {"attached": [], "skipped": []}
            if remember:
                remember_products(db, ae_email, company_id, keys)
            if keys:
                outcome = _link_scripts(db, session, keys)
            db.commit()
            if outcome["attached"] or outcome["skipped"]:
                logger.info(
                    "[ccs] session %s scripted for %s (no live script for %s)",
                    session.id,
                    outcome["attached"] or "nothing",
                    outcome["skipped"] or "nothing",
                )
            return outcome
        except IntegrityError:
            db.rollback()
            logger.warning(
                "[ccs] script attach raced on session %s (attempt %d)",
                session.id,
                attempt,
            )
        except Exception:  # noqa: BLE001 — a checklist never costs the call
            db.rollback()
            logger.exception(
                "[ccs] could not attach products %s to session %s", keys, session.id
            )
            break
    return {"attached": [], "skipped": keys}


def _requested_crm_sources_visible(body: StartCallRequest) -> bool:
    """Prove selected CRM IDs before a Meet write or session insert."""
    from rubik_core.tenancy.crm_visibility import visible_source_ids

    requested = (
        {body.hubspot_company_id} if body.hubspot_company_id else set(),
        {body.hubspot_contact_id} if body.hubspot_contact_id else set(),
        {body.hubspot_deal_id} if body.hubspot_deal_id else set(),
    )
    return visible_source_ids(*requested) == requested


async def _start_from_calendar(
    body: StartCallRequest,
    *,
    user: UserProfile,
    ae_email: str,
    event_id: str,
    db: Session,
) -> dict:
    """``origin="calendar"`` — bind the company, then try to arm. Never 502s.

    Creates no Meet space: the calendar made one, and the AE will join it from
    the invite. Order is deliberate — arm FIRST, then write the row with the
    verdict on it, so a session never exists claiming an arming that was never
    attempted, and the sweep can tell never-armed from armed-but-empty.

    A second Start on the same row returns the FIRST session and its stored
    verdict rather than re-arming. That is idempotency, not a missed retry: by
    the time an AE clicks twice the call is usually starting, and Meet cannot
    arm a conference already in progress — so the second attempt would burn a
    quota'd write to replace one honest verdict with another. Re-arming a
    meeting is what tomorrow's occurrence is for.

    The products the AE picked are attached on EVERY return path, including the
    two idempotent ones — see :func:`_attach_products`. Attaching is
    find-or-create rather than insert, so a repeat Start adds nothing, while an
    AE who reopened the row to add a second product gets that product attached to
    the call they already started.
    """
    meeting_code = (body.meet_meeting_code or "").strip()
    if not meeting_code:
        # 422, not a silent ad-hoc fallback. Falling through to
        # ``create_space`` would hand the AE a SECOND Meet link for a meeting
        # that already has one, and the client would be sitting in the other.
        raise HTTPException(
            status_code=422,
            detail=(
                "meet_meeting_code is required when calendar_event_id is given "
                "— it is the only address Google accepts for a meeting space "
                f"{PRODUCT_NAME} did not create."
            ),
        )

    existing = _calendar_session(db, ae_email, event_id)
    if existing is not None:
        if not await run_in_threadpool(session_source_visible, existing):
            raise HTTPException(status_code=404, detail="call session not found")
        logger.info(
            "[ccs] reusing session %s for %s (calendar event %s already started)",
            existing.id,
            ae_email,
            event_id,
        )
        _attach_products(db, existing, body, ae_email=ae_email)
        return _start_out(
            existing,
            _stored_verdict(existing) or _verdict("unknown", "not_checked_yet", None),
        )

    if body.capture_consent == "granted":
        arming, armed = await run_in_threadpool(
            _arm_calendar_space, ae_email, meeting_code
        )
    else:
        # Consent gates EVERY artifact, and that is the promise the confidential
        # modal makes: "nothing is captured. This cannot be undone." A
        # suppressed call is not armed at all — not armed-and-then-ignored.
        arming, armed = _suppressed_arming(), None

    space_name = str((armed or {}).get("space_name") or "") or None
    attrs = {
        "company_name": await run_in_threadpool(_company_name, body.hubspot_company_id),
        CRM_PROVENANCE_ATTR: stamp_session_source(
            body.hubspot_company_id, body.hubspot_contact_id, body.hubspot_deal_id
        ),
        # The verdict the calendar screen replays and the sweep reads. Assigned
        # as part of a freshly built dict — never mutated in place, because
        # SQLAlchemy does not track in-place changes to a JSON column and the
        # write would vanish with no error.
        "capture_arming": arming,
        # Evidence, and the reason ``arm_space_transcription`` returns both
        # configs: the patch preserves the host's recording and smart-notes
        # settings, and "Cubie turned my Gemini notes off" is an argument that
        # should be settleable from the record rather than from memory.
        "arming_evidence": {
            "artifact_config_before": (armed or {}).get("artifact_config_before"),
            "artifact_config_after": (armed or {}).get("artifact_config_after"),
        }
        if armed
        else None,
        # Kept whether or not it made it onto the column (see
        # ``_space_name_free``) so a recurring occurrence still records which
        # space it resolved to.
        "meet_space_name_resolved": space_name,
    }

    def _build(*, with_space_name: bool) -> CallSession:
        return CallSession(
            id=uuid.uuid4().hex,
            ae_email=ae_email,
            ae_google_user_id=getattr(user, "id", None),
            hubspot_company_id=body.hubspot_company_id,
            hubspot_contact_id=body.hubspot_contact_id,
            hubspot_deal_id=body.hubspot_deal_id,
            meet_space_name=space_name if with_space_name else None,
            # Both come from the calendar, not from a space Cubie made.
            meet_meeting_code=meeting_code,
            meet_uri=(body.meet_uri or "").strip() or None,
            capture_consent=body.capture_consent,
            status="scheduled",
            origin="calendar",
            calendar_event_id=event_id,
            attrs=attrs,
        )

    session = _build(with_space_name=_space_name_free(db, space_name))
    try:
        db.add(session)
        db.commit()
    except IntegrityError:
        db.rollback()
        # Two candidates, and they need different answers. The one this
        # constraint exists for is UNIQUE(ae_email, calendar_event_id): the AE
        # double-clicked, or two tabs posted. Return the session that won.
        winner = _calendar_session(db, ae_email, event_id)
        if winner is not None:
            if not await run_in_threadpool(session_source_visible, winner):
                raise HTTPException(status_code=404, detail="call session not found")
            logger.info(
                "[ccs] calendar start for %s raced on event %s; returning %s",
                ae_email,
                event_id,
                winner.id,
            )
            _attach_products(db, winner, body, ae_email=ae_email)
            return _start_out(winner, _stored_verdict(winner) or arming)
        # The other is UNIQUE(meet_space_name) — a recurring series whose space
        # was claimed between the check and the insert. Retry once without it;
        # the sweep correlates on the meeting code.
        logger.warning(
            "[ccs] retrying calendar start for %s on event %s without the "
            "shared space name %s",
            ae_email,
            event_id,
            space_name,
        )
        session = _build(with_space_name=False)
        db.add(session)
        db.commit()

    # After the session is committed, so a checklist failure can never cost the
    # capture, and before the audit, so the audit records what was attached.
    products = _attach_products(db, session, body, ae_email=ae_email)

    record_audit(
        db,
        actor_email=ae_email,
        action="CALL_CAPTURE_STARTED",
        target=session.id,
        details={
            "origin": "calendar",
            "calendar_event_id": event_id,
            "capture_consent": session.capture_consent,
            "hubspot_company_id": session.hubspot_company_id,
            "hubspot_deal_id": session.hubspot_deal_id,
            "meet_space_name": session.meet_space_name,
            "arming_state": arming.get("state"),
            "arming_reason": arming.get("reason"),
            # What the call is scripted for, and what it asked for and did not
            # get — the second is how "the wrap-up never mentioned that product"
            # gets answered from the record instead of from memory.
            "scripts_attached": products["attached"],
            "scripts_skipped": products["skipped"],
        },
    )
    logger.info(
        "[ccs] session %s started by %s from calendar event %s "
        "(consent=%s arming=%s/%s)",
        session.id,
        ae_email,
        event_id,
        session.capture_consent,
        arming.get("state"),
        arming.get("reason"),
    )
    return _start_out(session, arming)


@router.post("/start")
async def start_call(
    body: StartCallRequest,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Start a captured call. Two origins, decided by ``calendar_event_id``.

    **With one** (``origin="calendar"``, and the entry point AEs actually use)
    Cubie creates no space — see :func:`_start_from_calendar`. Idempotent at the
    DATABASE on ``UNIQUE(ae_email, calendar_event_id)``, which is what a real
    identity buys over the ad-hoc path's time window.

    **Without one** (``origin=CALL_ORIGIN_ASSISTANT``) Cubie creates a Meet space with
    ``artifactConfig`` set at creation. That path is unchanged, and it still has
    a reason to exist: there is no API to start transcription on a conference
    already in progress, so a call that has to be captured and has no calendar
    invite has to be started from a space Cubie made. An AE who opens Meet
    directly still gets a session row from the polling sweep — the call is never
    invisible — but it will have no transcript.

    Idempotent per (AE, company, consent) inside a 60-second window so a
    double-click does not burn two ``spaces.create`` calls against a quota of
    ten per minute per user.

    ``product_keys`` is honoured on the calendar path only. The picker lives on
    the calendar row — it is the screen with a company binding to remember the
    pick against and the only one that exists before the call connects — and the
    ad-hoc path is left exactly as it was.
    """
    _require_ccs_enabled()
    ae_email = _fold(getattr(user, "email", None))
    if not ae_email:
        raise HTTPException(status_code=403, detail="no authenticated identity")

    if not await run_in_threadpool(_requested_crm_sources_visible, body):
        raise HTTPException(
            status_code=409,
            detail="Selected CRM records are no longer available in this workspace. Refresh the call and choose current records.",
        )

    event_id = (body.calendar_event_id or "").strip()
    if event_id:
        return await _start_from_calendar(
            body, user=user, ae_email=ae_email, event_id=event_id, db=db
        )

    existing = _recent_duplicate(
        db,
        ae_email=ae_email,
        company_id=body.hubspot_company_id,
        consent=body.capture_consent,
    )
    if existing is not None:
        if not await run_in_threadpool(session_source_visible, existing):
            raise HTTPException(status_code=404, detail="call session not found")
        logger.info(
            "[ccs] reusing session %s for %s (double-click inside %ss)",
            existing.id,
            ae_email,
            _START_DEDUPE_SECONDS,
        )
        return _start_out(
            existing,
            _stored_verdict(existing) or _adhoc_arming(existing.capture_consent),
        )

    # A suppressed call still gets a space, and still gets a row. Artifact
    # generation is simply off, so ingest performs none of its three writes
    # and there is nothing to extract — while the cockpit
    # can still see that this AE was on a call with this company.
    capture = body.capture_consent == "granted"

    from rubik_core.tools.meet import GoogleMeetClient, MeetSpaceWriteDisabled

    try:
        space = await run_in_threadpool(
            lambda: GoogleMeetClient(ae_email).create_space(
                auto_transcribe=capture,
                # EVERY artifact is gated on consent, recording included.
                # ``MEET_AUTO_RECORD_DEFAULT`` is a flippable knob, and with it
                # on, an ungated recording would leave the confidential modal —
                # "No transcript is captured, nothing is extracted... This
                # cannot be undone" — making a promise the space does not keep,
                # on the one path where the promise is the entire point. A
                # suppressed call produces NO artifact of any kind.
                auto_record=settings.MEET_AUTO_RECORD_DEFAULT and capture,
                auto_smart_notes=settings.MEET_AUTO_SMART_NOTES and capture,
            )
        )
    except MeetSpaceWriteDisabled as exc:
        # 503 with the operator-facing reason, never a 500: the message names
        # the ONE thing anyone can act on, which once both space-write scopes
        # are granted on the DWD client id is Cubie's own ``CCS_MEET_ENABLED``
        # switch and not a Workspace grant. A 500 would
        # send the AE to support and support to the logs for the same sentence.
        #
        # Note the asymmetry with the calendar path, which turns this same
        # exception into a 200 carrying ``arming.reason == "write_disabled"``.
        # Here the space write IS the feature — no space, no call to capture.
        # There the session row and the company binding are unaffected by it.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Google failures are not our bug
        logger.exception("[ccs] Meet space creation failed for %s", ae_email)
        raise HTTPException(
            status_code=502,
            detail=(
                "Google Meet could not create the meeting space. Try again in a "
                "moment; if it keeps failing, start the call in Meet directly — "
                f"{PRODUCT_NAME} will still see that it happened."
            ),
        ) from exc

    session = CallSession(
        id=uuid.uuid4().hex,
        ae_email=ae_email,
        # ``users.id`` is the Google ``sub`` stored at login, which is what the
        # sweep needs to impersonate this AE through domain-wide delegation.
        ae_google_user_id=getattr(user, "id", None),
        hubspot_company_id=body.hubspot_company_id,
        hubspot_contact_id=body.hubspot_contact_id,
        hubspot_deal_id=body.hubspot_deal_id,
        meet_space_name=space.get("name"),
        meet_meeting_code=space.get("meetingCode"),
        meet_uri=space.get("meetingUri"),
        capture_consent=body.capture_consent,
        status="scheduled",
        origin=CALL_ORIGIN_ASSISTANT,
        # ``started_at`` stays NULL: the sweep owns it, and it reads the real
        # conference start from the conference record rather than guessing from
        # when the button was pressed.
        attrs={
            "company_name": await run_in_threadpool(
                _company_name, body.hubspot_company_id
            ),
            CRM_PROVENANCE_ATTR: stamp_session_source(
                body.hubspot_company_id, body.hubspot_contact_id, body.hubspot_deal_id
            ),
            "space_config": space.get("config"),
            # Recorded on this path too, so "was this call ever armed?" has one
            # answer shape whatever the origin — that is the question the sweep
            # asks to tell never-armed from armed-but-empty.
            "capture_arming": _adhoc_arming(body.capture_consent),
        },
    )
    db.add(session)
    db.commit()

    record_audit(
        db,
        actor_email=ae_email,
        action="CALL_CAPTURE_STARTED",
        target=session.id,
        details={
            "capture_consent": session.capture_consent,
            "hubspot_company_id": session.hubspot_company_id,
            "hubspot_deal_id": session.hubspot_deal_id,
            "meet_space_name": session.meet_space_name,
        },
    )
    logger.info(
        "[ccs] session %s started by %s (consent=%s space=%s)",
        session.id,
        ae_email,
        session.capture_consent,
        session.meet_space_name,
    )
    return _start_out(session, _adhoc_arming(session.capture_consent))


# ── GET /pending ─────────────────────────────────────────────────────────────


@router.get("/pending")
@pin_business_time_zone
async def pending_wrapups(
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Calls awaiting a first wrap-up, late review, or failed analysis retry.

    ``transcript_ready`` also stays visible while extraction retries, so a
    broken model key cannot make a call disappear from the AE's worklist.
    Oldest first, because Google deletes
    Meet transcripts and conference records 30 days after the call — the oldest
    pending wrap-up is the one whose evidence is closest to being gone.
    """
    _require_ccs_enabled()
    ae_email = _fold(getattr(user, "email", None))
    ordered = (
        db.query(CallSession)
        .filter(func.lower(CallSession.ae_email) == ae_email)
        .filter(
            or_(
                and_(
                    CallSession.status.in_(
                        ("transcript_ready", "extracted", "no_transcript")
                    ),
                    CallSession.wrapped_at.is_(None),
                ),
                and_(
                    CallSession.status == "wrapped",
                    or_(reviewable_items_exist(), failed_analysis_marker_exists()),
                ),
            )
        )
        .order_by(
            CallSession.ended_at.asc(),
            CallSession.created_at.asc(),
            CallSession.id.asc(),
        )
    )
    rows: list[CallSession] = []
    offset = 0
    while len(rows) < _PENDING_LIMIT:
        page = ordered.offset(offset).limit(_PENDING_LIMIT).all()
        if not page:
            break
        offset += len(page)
        visible = await run_in_threadpool(visible_session_ids, page)
        rows.extend(session for session in page if session.id in visible)
    rows = rows[:_PENDING_LIMIT]
    latest_extractions = {
        s.id: (
            db.query(CallExtraction)
            .filter(CallExtraction.call_session_id == s.id)
            .order_by(CallExtraction.created_at.desc(), CallExtraction.id.desc())
            .first()
        )
        for s in rows
        if s.status in ("extracted", "wrapped")
    }
    return {
        "sessions": [
            {
                "call_session_id": s.id,
                "company_name": (s.attrs or {}).get("company_name"),
                "ended_at": _iso(s.ended_at),
                "age_hours": _age_hours(s),
                "status": s.status,
                "extraction": _extraction_out(s, latest_extractions.get(s.id)),
            }
            for s in rows
        ]
    }


# ── GET /scheduled ───────────────────────────────────────────────────────────


def _unavailable_day(ae_email: str) -> dict:
    """The "we could not read your calendar" envelope, when even the scan died.

    ``calendar_scan.scan_day`` is documented never to raise and swallows every
    Google and HubSpot failure into ``calendar_available: False`` itself. This
    exists for the case that contract is broken by a bug, because the shape of
    the lie matters: ``available: False`` with no meetings is "we could not
    look", and the UI says so out loud. ``available: True`` with no meetings is
    "you have no client calls today", which the AE BELIEVES — and would then
    not capture a call they actually have.
    """
    tz_name = "UTC"
    local_date = datetime.now(timezone.utc).date()
    try:
        from rubik_core.priorities.temporal import resolve_user_now

        ctx = resolve_user_now(ae_email)
        tz_name, local_date = ctx.tz_name, ctx.today
    except Exception:  # noqa: BLE001 — a fallback that raises is not a fallback
        logger.debug("[ccs] could not resolve a timezone for %s", ae_email)
    return {
        "calendar_available": False,
        "timezone": tz_name,
        "local_date": local_date.isoformat(),
        "meetings": [],
        # The same key ``scan_day`` always sends, and the value it sends on its
        # own outage paths: the checklists were never read.
        "script_catalogue": None,
    }


@router.get("/scheduled")
async def scheduled_meetings(
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Today's capturable client meetings, bound to HubSpot companies.

    The entry point that replaced "create a space from Cubie". A PURE READ: no
    Meet writes, no session rows, no HubSpot mutations — the AE has not chosen
    anything yet, and a screen that armed meetings just by being opened would
    arm every occurrence of every recurring series an AE scrolled past (one
    space is shared by a whole series).

    So ``arming`` here is a PRECONDITION verdict, never an outcome: an external
    organizer, a conference already started, or a meeting somebody else owns are
    all knowable for free, and everything else is ``unknown`` until
    ``POST /start`` actually asks Google. A row that already has a session
    replays that session's stored verdict instead, so an armed meeting never
    renders as unchecked.

    Never 500s on a Google or HubSpot outage — ``calendar_available`` carries
    that, and ``company.match == "error"`` carries the HubSpot half separately
    from ``"not_found"``. "We could not ask" and "there is no such company" lead
    to different actions, so they are never collapsed.

    All the I/O is synchronous (Calendar and HubSpot are both blocking clients),
    so the whole scan runs in the threadpool rather than on the event loop.
    """
    _require_ccs_enabled()
    ae_email = _fold(getattr(user, "email", None))
    if not ae_email:
        raise HTTPException(status_code=403, detail="no authenticated identity")

    from rubik_core.call_capture import calendar_scan

    try:
        return await run_in_threadpool(calendar_scan.scan_day, ae_email, db=db)
    except Exception:  # noqa: BLE001 — see _unavailable_day
        logger.exception("[ccs] calendar scan failed for %s", ae_email)
        return _unavailable_day(ae_email)


# ── GET /companies ───────────────────────────────────────────────────────────


def _company_options(query: str) -> list[dict]:
    """HubSpot company typeahead rows for the manual picker. Fail-soft to ``[]``.

    Delegates to the same ``rubik_core/xray/fetchers.search_companies`` that
    ``/api/xray/customers`` uses — one implementation of "search companies",
    with its own backoff and its own fail-soft — and drops the ``csm`` field,
    which is the only reason that function makes a second call for the owner
    directory. The picker binds a company; it does not profile one.
    """
    try:
        from rubik_core.xray import fetchers

        rows = fetchers.search_companies(query, limit=_COMPANY_SEARCH_LIMIT)
    except Exception:  # noqa: BLE001 — a typeahead never breaks the screen
        logger.warning("[ccs] company search failed for %r", query, exc_info=True)
        return []
    return [
        {
            # Renamed from ``company_id`` on purpose: everything else this
            # router sends and receives says ``hubspot_company_id``, and the
            # value goes straight back to ``POST /start`` under that name.
            "hubspot_company_id": str(row.get("company_id") or ""),
            "name": row.get("name") or "",
            "domain": row.get("domain") or "",
            "client_status": row.get("client_status") or "",
        }
        for row in rows or []
        if str(row.get("company_id") or "")
    ]


@router.get("/companies")
async def search_bindable_companies(
    q: str = Query("", description="company name or domain fragment"),
    user: UserProfile = Depends(require_ccs),
):
    """The manual company picker behind ``require_ccs``, not behind X-Ray's gate.

    This endpoint exists because of a bug the calendar screen would otherwise
    inherit: the only other company typeahead in Cubie is ``/api/xray/customers``,
    and it sits behind ``auth.require_sales_or_admin`` → ``user_can_access_xray``
    — a DIFFERENT audience that moves for its own reasons. While Customer X-Ray
    was Systems-only it 403'd every non-admin Sales AE on the one control that
    unmatched and ambiguous rows depend on, and the redesign makes that control
    MORE load-bearing, not less. Same search, same fail-soft, this feature's own
    audience gate — so the next time X-Ray's audience changes, this picker
    doesn't notice.

    An empty query returns an empty list rather than the first page of every
    company, exactly as ``/api/xray/customers`` does — a picker that answers
    before it is asked is a way to hand an AE a plausible wrong company.
    """
    _require_ccs_enabled()
    if not _fold(getattr(user, "email", None)):
        raise HTTPException(status_code=403, detail="no authenticated identity")
    query = (q or "").strip()
    if not query:
        return {"companies": []}
    return {"companies": await run_in_threadpool(_company_options, query)}


# ── GET /{id}/wrapup ─────────────────────────────────────────────────────────


def _script_gaps(
    db: Session,
    session: CallSession,
    extraction: CallExtraction | None,
) -> tuple[list[dict], list[str]]:
    """What each attached checklist did and did not surface, and what was missed.

    Returns ``(script_coverage, unscripted_products)``. This is the half of
    the feature that pays for the other half: extraction can only find what was
    said, so a perfect extractor over an incomplete call still produces an
    incomplete CRM record — and the AE cannot tell, because the wrap-up looks
    clean. Here it stops looking clean.

    **Coverage is per product and never merged.** An AE who does not split a
    call's leads into one per product is the failure this workstream exists to
    fix, and one merged percentage across three products is wrong for all three.
    One section per attached script, each with its own items and its own
    ``missing_required``.

    ``unscripted_products`` is the valuable case: a product the call discussed
    that nobody loaded a checklist for. The product rule is that lead↔deal is
    1:1, so that product wants its own lead, and this is the list the UI turns
    into one click instead of the manual step AEs skip.

    Two item populations feed it, and both are needed. The persisted
    ``call_extraction_items`` carry the vendor fields and leads, matched to a
    checklist fact through ``hubspot_target``; the script facts in
    ``raw_output`` carry the answers that have no HubSpot home at all — the
    scope, the rate, the start date, the facts a supervisor would otherwise
    reverse-engineer out of a Fathom summary to build an SOW. Statuses are NOT
    filtered to ``CLAIMABLE_ITEM_STATUSES`` here: a fact the AE already accepted
    was still covered by the call, and ``coverage_for`` drops the two statuses
    that genuinely un-cover a fact (``superseded``, ``rejected``) itself.

    **Fail-soft.** ``scripts.py`` deliberately does not swallow database errors,
    on the grounds that the caller who knows the screen still has to render is
    the one that should. This is that caller: an AE with a wrap-up to close must
    not get a 500 because the script tables are unhappy, so a failure costs the
    coverage block and is logged, and the proposals still render.
    """
    from rubik_core.call_capture import extraction as _extraction
    from rubik_core.call_capture import scripts as call_scripts
    from rubik_core.database.call_scripts import CallScript, CallSessionScript

    try:
        attached = (
            db.query(CallSessionScript)
            .filter(CallSessionScript.call_session_id == session.id)
            .order_by(CallSessionScript.attached_at.asc())
            .all()
        )
        coverage_items: list = []
        if extraction is not None:
            coverage_items = list(
                db.query(CallExtractionItem)
                .filter(CallExtractionItem.extraction_id == extraction.id)
                .order_by(CallExtractionItem.created_at.asc())
                .all()
            )
            coverage_items += _extraction.script_fact_items(extraction)

        sections: list[dict] = []
        if attached:
            # Loaded by the id the call was STARTED against, not by re-resolving
            # each product's newest approved script: the AE is graded on the
            # checklist they were shown, and a script approved between the call
            # and the wrap-up must not invent a gap after the fact.
            scripts_by_id = {
                row.id: row
                for row in db.query(CallScript)
                .filter(CallScript.id.in_([row.script_id for row in attached]))
                .all()
            }
            for row in attached:
                script = scripts_by_id.get(row.script_id)
                if script is None:
                    continue
                items = call_scripts.coverage_for(script.items, coverage_items)
                sections.append(
                    {
                        "script_id": script.id,
                        "product_key": row.product_key,
                        "name": script.name or row.product_key,
                        "items": items,
                        # Named, not counted. "2 gaps" tells the AE they missed
                        # something; ["rate", "commencement_date"] tells them
                        # what to go and ask, which is the difference between a
                        # metric and a next action.
                        "missing_required": [
                            item["fact_key"]
                            for item in items
                            if item["required"] and not item["found"]
                        ],
                    }
                )

        unscripted = call_scripts.unscripted_products(
            db, coverage_items, [row.product_key for row in attached]
        )
        return sections, unscripted
    except Exception as exc:  # noqa: BLE001 — a checklist miss must not cost the wrap-up
        logger.warning(
            "[ccs] could not compute script coverage for session %s: %s",
            session.id,
            exc,
        )
        return [], []


@router.get("/{session_id}/wrapup")
async def get_wrapup(
    session_id: str,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Current proposals and all retryable writes, grouped, with evidence.

    An empty proposal set is NORMAL — transcription was off, the
    call was 40 seconds, the call was confidential, or the model genuinely heard
    nothing worth filing. It returns 200 with ``groups: []`` and the AE logs the
    call from the same screen.

    ``script_coverage`` and ``unscripted_products`` are the gaps block — see
    :func:`_script_gaps`. Both are empty lists on a call nobody attached a
    checklist to, which is every call until the script tables have approved
    content in them.

    ``wrap_opened_at`` is stamped on the FIRST read by the AE only. It is the
    numerator of the feature's bar (median wrap-up under 90 seconds, the
    measure that decides whether this feature gets used), so a manager
    browsing a team wrap-up must not start the AE's clock, and a re-read must
    not restart it.
    """
    _require_ccs_enabled()
    session, is_own, may_read_text = _session_for_reader(session_id, user, db)
    if not await run_in_threadpool(session_source_visible, session):
        raise HTTPException(status_code=404, detail="call session not found")

    latest_extraction = (
        db.query(CallExtraction)
        .filter(CallExtraction.call_session_id == session.id)
        .order_by(CallExtraction.created_at.desc(), CallExtraction.id.desc())
        .first()
    )
    extraction_state = _extraction_out(session, latest_extraction)
    extraction_state["can_retry"] = is_own and extraction_state["status"] == "retrying"
    # A new transcript can move a previously extracted session back to
    # transcript_ready. Do not present the old run's proposals as current.
    # Previously failed HubSpot writes remain retryable across every run.
    extraction = (
        latest_extraction
        if extraction_state["status"] not in ("pending", "retrying")
        else None
    )

    items: list[CallExtractionItem] = (
        db.query(CallExtractionItem)
        .filter(CallExtractionItem.call_session_id == session.id)
        .filter(
            or_(
                CallExtractionItem.status == "write_failed",
                and_(
                    CallExtractionItem.status == "proposed",
                    CallExtractionItem.extraction_id
                    == (extraction.id if extraction is not None else None),
                ),
            )
        )
        .order_by(CallExtractionItem.created_at.asc(), CallExtractionItem.id.asc())
        .all()
    )

    # Loaded only when the caller may actually read them. A supervisor's request
    # must not pull a call's transcript into this process at all.
    entries_by_id: dict[str, CallTranscriptEntry] = {}
    entries_by_sequence: dict[int, CallTranscriptEntry] = {}
    if items and may_read_text:
        entries = (
            db.query(CallTranscriptEntry)
            .filter(CallTranscriptEntry.call_session_id == session.id)
            .order_by(CallTranscriptEntry.sequence.asc())
            .all()
        )
        entries_by_id = {e.id: e for e in entries}
        entries_by_sequence = {e.sequence: e for e in entries}

    by_kind: dict[str, list[dict]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(
            _item_out(
                item,
                may_read_text=may_read_text,
                entries_by_id=entries_by_id,
                entries_by_sequence=entries_by_sequence,
            )
        )
    groups = [
        {"kind": kind, "items": by_kind[kind]}
        for kind in _GROUP_ORDER
        if kind in by_kind
    ]

    # The gaps block. Computed before the audit and the clock stamp so the two
    # things that record "the AE looked at this wrap-up" record the wrap-up they
    # actually saw. Both values are safe for a supervisor: a checklist question
    # is authored by the supervisor, and a fact's value and entry id are the same class
    # of data ``_item_out`` already serializes past the transcript fence.
    if extraction_state["status"] in ("pending", "retrying"):
        script_coverage, unscripted = [], []
    else:
        script_coverage, unscripted = _script_gaps(db, session, extraction)

    if not is_own:
        # A cross-person read of someone else's call. A gate is not an audit
        # trail (backend/auth.py, cockpit access block).
        record_audit(
            db,
            actor_email=_fold(getattr(user, "email", None)),
            action="CALL_WRAPUP_VIEW",
            target=session.id,
            details={
                "ae_email": _fold(session.ae_email),
                "transcript_text": may_read_text,
                "items": len(items),
            },
        )

    # Stamped last, so a serialization failure cannot start a clock on a screen
    # the AE never saw.
    if (
        is_own
        and session.wrap_opened_at is None
        and extraction_state["status"] not in ("pending", "retrying")
    ):
        session.wrap_opened_at = datetime.utcnow()
        db.commit()

    return {
        "session": _session_out(
            db,
            session,
            may_read_text=may_read_text,
            needs_review=bool(items) or extraction_state["status"] == "retrying",
        ),
        "extraction": extraction_state,
        "groups": groups,
        "script_coverage": script_coverage,
        "unscripted_products": unscripted,
    }


@router.post("/{session_id}/wrapup/retry-extraction")
async def retry_wrapup_extraction(
    session_id: str,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Let the AE retry a failed analysis after an admin fixes its model key.

    This runs only the extractor. CRM writes still require the separate
    wrap-up approval endpoint and explicit item decisions.
    """
    _require_ccs_enabled()
    session = _session_for_ae(session_id, user, db)
    if not await run_in_threadpool(session_source_visible, session):
        raise HTTPException(status_code=404, detail="call session not found")
    if session.status not in ("transcript_ready", "wrapped") or not isinstance(
        (session.attrs or {}).get("extraction_retry"), dict
    ):
        raise HTTPException(status_code=409, detail="No failed analysis to retry")

    from rubik_core.call_capture.sweep import _run_extraction

    report = {
        "extractions_run": 0,
        "extractions_empty": 0,
        "extractions_failed": 0,
        "extractions_deferred": 0,
    }
    await run_in_threadpool(
        _run_extraction, db, session, report, False, manual_retry=True
    )
    if report["extractions_deferred"]:
        raise HTTPException(
            status_code=409,
            detail="Analysis is already running or was retried recently",
        )
    latest = (
        db.query(CallExtraction)
        .filter(CallExtraction.call_session_id == session.id)
        .order_by(CallExtraction.created_at.desc(), CallExtraction.id.desc())
        .first()
    )
    return {"extraction": _extraction_out(session, latest)}


# ── POST /{id}/wrapup/approve ────────────────────────────────────────────────
#
# Modelled on rubik_core/crm_sync/apply.py. The stages are deliberately
# separate and ordered: claim everything (one commit), then write in an order
# where a partial failure degrades sensibly, then stamp the
# session, then audit.


def _claim_item(
    db: Session,
    *,
    session_id: str,
    actor: str,
    item_id: str,
    target_status: str,
    edited_value: str | None = None,
) -> bool:
    """Compare-and-swap one item into ``target_status``. True iff we claimed it.

    This single UPDATE is BOTH guards at once, which is why there is no separate
    ownership check anywhere in the approval path:

    * **IDOR.** ``call_session_id IN (session owned by actor)`` is part of the
      WHERE clause, so another AE's item id matches zero rows and comes back as
      "already handled" rather than as somebody else's data.
    * **Exactly-once.** ``status IN CLAIMABLE`` means a double-click, a second
      tab and a retry after a timeout all resolve to one decision.

    Note the bulk UPDATE bypasses the ``@validates`` hook that folds
    ``decided_by`` — that is exactly the writer the ``lower_check`` CHECK
    exists for, so ``actor`` must already be lower-folded (it is; every caller
    goes through ``_fold``).
    """
    owned = (
        select(CallSession.id)
        .where(CallSession.id == session_id)
        .where(func.lower(CallSession.ae_email) == actor)
    )
    now = datetime.utcnow()
    values: dict = {
        "status": target_status,
        "decided_by": actor,
        "decided_at": now,
        "updated_at": now,
    }
    if edited_value is not None:
        values["edited_value"] = edited_value
    updated = (
        db.query(CallExtractionItem)
        .filter(CallExtractionItem.id == item_id)
        .filter(CallExtractionItem.call_session_id.in_(owned))
        .filter(CallExtractionItem.status.in_(CLAIMABLE_ITEM_STATUSES))
        .update(values, synchronize_session=False)
    )
    return bool(updated)


def _unclaimed_result(item: CallExtractionItem) -> dict:
    """What to tell the AE when the CAS claim found nothing to move.

    Distinguished from a bare "skipped" because the common cause is a resubmit
    of a wrap-up that already went through, and "already written" is a reassuring
    answer where "skipped" reads like something was lost.
    """
    if item.status == "written":
        return {"status": "already_written"}
    return {
        "status": "skipped",
        "message": f"Already handled ({item.status}).",
    }


def _vendor_options(memo: dict, prop: str) -> list[str] | None:
    """Live HubSpot enumeration options for one company property.

    ``None`` means the schema could not be read, and the edit then fails closed.
    That is the same trade-off extraction makes: proposing free text into an
    enumeration property is the misheard-spelling pollution ("Akme" next to
    "Acme") the option closure exists to prevent, and a value nobody could
    verify is worse than a value nobody wrote. An empty LIST is different and
    legitimate — that property is free text on this portal.

    Memoized per request on top of ``hubspot_schema``'s own hour-long process
    cache, so a wrap-up with six vendor fields costs at most one HubSpot
    property fetch.
    """
    if prop in memo:
        return memo[prop]
    from rubik_core.call_capture.hubspot_schema import allowed_values_for

    try:
        fetched = allowed_values_for([prop])
        memo[prop] = fetched.get(prop)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ccs] options for %s unavailable at approve time: %s", prop, exc
        )
        memo[prop] = None
    return memo[prop]


def _gate_vendor_value(
    item: CallExtractionItem, decision: WrapupDecision, memo: dict
) -> tuple[str | None, str | None]:
    """Resolve the value this vendor field will write. Returns (value, refusal).

    The AE's edit goes through the SAME closure the model's proposal passed —
    ``validators._constrain_value``, reached deliberately rather than
    re-implemented, because two copies of a case-folding match are two chances
    to diverge and the divergence would let free text into an enumeration
    property. An edit box is a convenience, not a trust escalation.
    """
    from rubik_core.call_capture.validators import _constrain_value

    raw = (decision.edited_value or item.proposed_value or "").strip()
    if not raw:
        return None, "There is no value to write for this field."

    prop = item.target_property or ""
    if prop not in company_writable():
        # Belt to the write layer's braces: ``update_company`` refuses a
        # non-approved property too, but refusing here means the whole PATCH is
        # not abandoned because of one bad row.
        return None, f"{_vendor_label(prop)} is not a field {PRODUCT_NAME} may write."

    options = _vendor_options(memo, prop)
    if options is None:
        return None, (
            f"{PRODUCT_NAME} could not verify this property's options in HubSpot just now, "
            "so it will not write a value it cannot check. Try again shortly."
        )

    constrained, needs_new = _constrain_value(raw, options)
    if constrained is None:
        sample = ", ".join(options[:8]) or "none configured"
        return None, (
            f"“{needs_new}” is not an existing option for {_vendor_label(prop)}. "
            f"Existing options include: {sample}."
        )
    return constrained, None


def _claim_phase(
    db: Session,
    session: CallSession,
    body: WrapupApproveRequest,
    actor: str,
) -> tuple[dict[str, dict], dict[str, list[tuple[CallExtractionItem, str | None]]]]:
    """Decide and claim every item. Returns (results by item id, accepted by kind).

    Every refusal here leaves the item ``proposed``, so the AE can fix the
    problem (confirm the overwrite, correct the value) and submit again. Only a
    decision that is actually going to be honoured moves the row.
    """
    known = {
        row.id: row
        for row in db.query(CallExtractionItem)
        .filter(CallExtractionItem.call_session_id == session.id)
        .all()
    }
    results: dict[str, dict] = {}
    accepted: dict[str, list[tuple[CallExtractionItem, str | None]]] = {}
    memo: dict = {}

    for decision in body.decisions:
        item = known.get(decision.item_id)
        if item is None:
            results[decision.item_id] = {
                "status": "skipped",
                "message": "That proposal is not part of this call.",
            }
            continue

        if not decision.accept:
            claimed = _claim_item(
                db,
                session_id=session.id,
                actor=actor,
                item_id=item.id,
                target_status="rejected",
                edited_value=decision.edited_value,
            )
            results[item.id] = (
                {"status": "rejected"} if claimed else _unclaimed_result(item)
            )
            continue

        # An item HubSpot has no option for can never be written — no exception,
        # and not even when the AE types a valid-looking replacement. It is
        # accepted (so the decision is recorded and the value is kept) and it
        # becomes vocabulary the CRM is missing, for a human to add in HubSpot.
        if item.needs_new_option:
            claimed = _claim_item(
                db,
                session_id=session.id,
                actor=actor,
                item_id=item.id,
                target_status="accepted",
                edited_value=decision.edited_value,
            )
            results[item.id] = (
                {
                    "status": "triage",
                    "message": (
                        f"“{item.needs_new_option}” isn't an option in HubSpot yet. "
                        "Sent for triage — nothing was written."
                    ),
                }
                if claimed
                else _unclaimed_result(item)
            )
            continue

        if item.is_overwrite and not decision.confirm_overwrite:
            results[item.id] = {
                "status": "needs_confirmation",
                "message": (
                    f"This replaces “{item.current_value}” in HubSpot. Confirm "
                    "that specific change to write it."
                ),
            }
            continue

        value: str | None = decision.edited_value or item.proposed_value
        claim_edited = decision.edited_value
        if item.kind == "vendor_field":
            value, refusal = _gate_vendor_value(item, decision, memo)
            if refusal is not None:
                results[item.id] = {"status": "invalid", "message": refusal}
                continue
            # Persist the value that will actually be written, not the raw
            # keystrokes: an edit is matched case-insensitively against the
            # HubSpot option, and "what did we write" is the question this
            # column gets asked later.
            if decision.edited_value:
                claim_edited = value

        claimed = _claim_item(
            db,
            session_id=session.id,
            actor=actor,
            item_id=item.id,
            target_status="accepted",
            edited_value=claim_edited,
        )
        if not claimed:
            results[item.id] = _unclaimed_result(item)
            continue
        accepted.setdefault(item.kind, []).append((item, value))

    # One commit for every claim: from here on the decisions are durable, so a
    # crash mid-write leaves accepted items that can be retried rather than
    # decisions the AE has to make again.
    db.commit()
    return results, accepted


def _already_written(db: Session, key: str) -> bool:
    """True iff this exact mutation already succeeded: approving twice writes once."""
    row = (
        db.query(CallHubspotWrite)
        .filter(CallHubspotWrite.idempotency_key == key)
        .first()
    )
    return row is not None and row.succeeded_at is not None


def _reserve_write(
    db: Session,
    *,
    key: str,
    session_id: str,
    item_id: str | None,
    method: str,
    path: str,
    body: dict,
) -> CallHubspotWrite | None:
    """Claim the idempotency key BEFORE the HTTP call. None if someone else holds it.

    A CLAIM, never a read-modify-write. The distinction is the whole of
    "approving twice writes once" under concurrency: a read-then-write would let
    request B read the row request A inserted a millisecond ago, see
    ``succeeded_at`` still NULL because A is blocked inside ``create_call``, and
    conclude the write is retryable — two call engagements on the client's CRM
    record for one call, from two tabs or one impatient retry.

    So the INSERT is always attempted first and the UNIQUE constraint is the
    arbiter. Only when it rejects does the genuinely retryable case get a look,
    and that look is itself a compare-and-swap
    (``rubik_core/crm_sync/apply.py::_claim`` is the precedent): a conditional
    UPDATE that matches only a reservation nobody holds — one whose attempt
    already ENDED in a recorded failure (``response_body`` written by
    ``_stamp_failure``), or one so old that no request could still be running
    against it. ``response_body`` is cleared as part of the reclaim, so the
    retry is itself in flight and a third caller cannot pile onto it.

    The known limit, stated rather than hidden: a process that dies between the
    claim and the response leaves ``succeeded_at`` NULL for a write that may
    well have landed. Such a row is now held for ``_WRITE_CLAIM_LEASE`` rather
    than being retried instantly; after that a retry may still write twice,
    because HubSpot offers no idempotency key to close the window.
    ``attempts`` is what makes the ambiguity visible.
    """
    # The claim below rolls back on the contended path. Anything the caller
    # already decided (an item marked ``written`` because its key was found
    # done) is committed first, so a rollback cannot silently discard it.
    db.commit()

    now = datetime.utcnow()
    row = CallHubspotWrite(
        id=uuid.uuid4().hex,
        idempotency_key=key,
        extraction_item_id=item_id,
        call_session_id=session_id,
        method=method,
        path=path,
        request_body=body,
        attempts=1,
        updated_at=now,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    else:
        return row

    # Somebody already holds this key. Take it over ONLY if their attempt is
    # over and it failed — decided by the UPDATE's WHERE clause, not by a
    # branch on a value read moments earlier.
    claimed = (
        db.query(CallHubspotWrite)
        .filter(CallHubspotWrite.idempotency_key == key)
        .filter(CallHubspotWrite.succeeded_at.is_(None))
        .filter(
            or_(
                CallHubspotWrite.response_body.isnot(None),
                CallHubspotWrite.updated_at < now - _WRITE_CLAIM_LEASE,
            )
        )
        .update(
            {
                "method": method,
                "path": path,
                "request_body": body,
                # Back to "in flight": the previous failure is no longer the
                # outcome, and leaving it would make this very row look
                # reclaimable to a concurrent third request.
                "response_body": None,
                "response_status": None,
                "attempts": func.coalesce(CallHubspotWrite.attempts, 0) + 1,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not claimed:
        logger.info("[ccs] write key %s is held by another attempt; skipping", key[:12])
        return None
    return (
        db.query(CallHubspotWrite)
        .filter(CallHubspotWrite.idempotency_key == key)
        .first()
    )


def _stamp_success(db: Session, rows: list[CallHubspotWrite], response: dict) -> None:
    object_id = str(response.get("id") or "") or None
    now = datetime.utcnow()
    for row in rows:
        row.response_status = 200
        row.response_body = response
        row.hubspot_object_id = object_id
        row.succeeded_at = now
        row.updated_at = now
    db.commit()


def _stamp_failure(db: Session, rows: list[CallHubspotWrite], message: str) -> None:
    """Record a failed attempt. ``succeeded_at`` stays NULL — that is the retry signal.

    ``response_status`` is left NULL because the write client raises a message
    rather than the response; the status is inside ``response_body["error"]``
    and the useful part of a HubSpot failure is its nested validation text
    anyway.
    """
    now = datetime.utcnow()
    for row in rows:
        row.response_body = {"error": message}
        row.updated_at = now
    db.commit()


def _log_call_engagement(
    db: Session,
    session: CallSession,
    client: HubSpotWriteClient,
    *,
    has_proposals: bool,
) -> dict:
    """Write order step 1 — ALWAYS, even for "nothing here".

    The record that a call took place is the point; the extraction is the
    bonus. This runs for a suppressed call too: the FACT of the call is
    not confidential, only its content, and the body carries no transcript text
    in any case.
    """
    stamp = session.ended_at or session.started_at or datetime.utcnow()
    company_name = (session.attrs or {}).get("company_name") or "a client"
    ae_name = _ae_name(db, _fold(session.ae_email))
    link = (settings.FRONTEND_URL or "").rstrip("/") + _WRAPUP_PATH.format(
        call_session_id=session.id
    )
    lines = [
        f"Logged by {PRODUCT_NAME} from a captured Google Meet call.",
        f"Wrap-up: {link}",
    ]
    if session.capture_consent == "suppressed":
        lines.insert(1, "Marked confidential — no transcript was captured.")
    elif not has_proposals:
        lines.insert(1, "The AE reviewed the wrap-up and had nothing to file.")

    properties: dict = {
        "hs_timestamp": _epoch_ms(stamp),
        "hs_call_title": f"Call with {company_name} — {ae_name}",
        "hs_call_body": "\n".join(lines),
        "hs_call_direction": "OUTBOUND",
        "hs_call_status": "COMPLETED",
        "hs_call_source": "INTEGRATIONS_PLATFORM",
    }
    duration_ms = _duration_ms(session)
    if duration_ms:
        # MILLISECONDS. Seconds here logs every call as under a second and
        # nothing on either side rejects it.
        properties["hs_call_duration"] = duration_ms

    owner_id = client.resolve_owner_id(_fold(session.ae_email))
    if owner_id:
        properties["hubspot_owner_id"] = owner_id
    else:
        # A named bucket: an AE whose HubSpot address differs from their
        # Workspace address is a known live case, and the symptom is silence.
        logger.warning(
            "[ccs] hubspot_owner_unresolved: %s is not a HubSpot owner; the call "
            "engagement for session %s will be unassigned",
            _fold(session.ae_email),
            session.id,
        )

    key = _idempotency_key(session.id, "calls", None, "engagement")
    if _already_written(db, key):
        return {"status": "already_written"}
    row = _reserve_write(
        db,
        key=key,
        session_id=session.id,
        item_id=None,
        method="POST",
        path="/crm/v3/objects/calls",
        body=properties,
    )
    if row is None:
        return {"status": "already_written"}
    try:
        response = client.create_call(
            properties,
            contact_id=session.hubspot_contact_id,
            company_id=session.hubspot_company_id,
            deal_id=session.hubspot_deal_id,
        )
    except (HubSpotWriteRefused, HubSpotWriteError) as exc:
        message = friendly_write_error("the call log", exc)
        _stamp_failure(db, [row], message)
        logger.warning("[ccs] call engagement failed for %s: %s", session.id, exc)
        return {"status": "write_failed", "message": message}
    _stamp_success(db, [row], response)
    return {
        "status": "written",
        "hubspot_object_id": str(response.get("id") or "") or None,
    }


def _write_company_fields(
    db: Session,
    session: CallSession,
    client: HubSpotWriteClient,
    pairs: list[tuple[CallExtractionItem, str | None]],
    results: dict[str, dict],
) -> None:
    """Write order step 2 — one PATCH carrying every accepted vendor field.

    One request, never one per field: a dozen configured properties at a request
    each is exactly the pattern that burns the 190-per-10s budget. Each item
    still gets its OWN ledger row, so "was this proposal written?" stays a
    per-item question — which is what a retry and any accuracy measurement ask.
    """
    company_id = session.hubspot_company_id
    if not company_id:
        for item, _ in pairs:
            results[item.id] = {
                "status": "write_failed",
                "message": "This call isn't linked to a HubSpot company yet.",
            }
        return

    todo: list[tuple[CallExtractionItem, str, str]] = []
    for item, value in pairs:
        key = _idempotency_key(
            session.id, "companies", company_id, item.target_property
        )
        if _already_written(db, key):
            results[item.id] = {"status": "already_written"}
            item.status = "written"
            continue
        todo.append((item, value or "", key))
    if not todo:
        db.commit()
        return

    # Drift check before writing, exactly as crm_sync does: ``current_value``
    # was read when the proposal was made, and approving a change is a judgement
    # about what is in HubSpot NOW.
    props = sorted({i.target_property for i, _, _ in todo if i.target_property})
    try:
        live_record = client.get_object("companies", company_id, props)
        live = (live_record.get("properties") or {}) if live_record else {}
    except (HubSpotWriteRefused, HubSpotWriteError) as exc:
        # Refuse rather than write blind. A read failure means we cannot tell an
        # empty field from a colleague's value, and the no-blind-overwrite rule
        # forbids guessing in that direction.
        message = friendly_write_error("the company", exc)
        for item, _, _ in todo:
            item.status = "write_failed"
            results[item.id] = {"status": "write_failed", "message": message}
        db.commit()
        return

    writable: list[tuple[CallExtractionItem, str, str]] = []
    for item, value, key in todo:
        current = (
            (live.get(item.target_property) or None) if item.target_property else None
        )
        stored = item.current_value or None
        if current == value:
            # Already exactly what we were going to write — our own earlier
            # attempt, or a colleague making the same change. Not drift, and not
            # worth a second PATCH.
            results[item.id] = {"status": "already_written"}
            item.status = "written"
            continue
        if current != stored:
            # Re-anchor the row on reality and put it back in front of the AE,
            # rather than overwriting a value they never saw.
            item.current_value = current
            item.is_overwrite = current is not None
            item.status = "proposed"
            results[item.id] = {
                "status": "drift",
                "message": (
                    f"{_vendor_label(item.target_property)} changed in HubSpot since "
                    f"{PRODUCT_NAME} proposed this (it now reads “{current}”). Take another look."
                ),
            }
            continue
        writable.append((item, value, key))
    db.commit()
    if not writable:
        return

    # Reserve every key BEFORE deciding the body, so the PATCH carries exactly
    # the fields this request owns. A key a concurrent approval already claimed
    # drops out here rather than riding along in the body — otherwise the
    # ledger would record a request that mutated a field its row did not claim.
    reserved: list[tuple[CallExtractionItem, str, CallHubspotWrite]] = []
    for item, value, key in writable:
        row = _reserve_write(
            db,
            key=key,
            session_id=session.id,
            item_id=item.id,
            method="PATCH",
            path=f"/crm/v3/objects/companies/{company_id}",
            body={item.target_property: value},
        )
        if row is None:
            results[item.id] = {"status": "already_written"}
            continue
        reserved.append((item, value, row))
    if not reserved:
        return

    properties = {i.target_property: v for i, v, _ in reserved}
    rows = [r for _, _, r in reserved]
    for row in rows:
        row.request_body = properties
    db.commit()

    try:
        response = client.update_company(company_id, properties)
    except (HubSpotWriteRefused, HubSpotWriteError) as exc:
        message = friendly_write_error("the company", exc)
        _stamp_failure(db, rows, message)
        for item, _, _ in reserved:
            item.status = "write_failed"
            results[item.id] = {"status": "write_failed", "message": message}
        db.commit()
        logger.warning("[ccs] company PATCH failed for %s: %s", session.id, exc)
        return

    _stamp_success(db, rows, response)
    for item, _, _ in reserved:
        item.status = "written"
        results[item.id] = {"status": "written"}
    db.commit()


def _open_lead_touch(
    db: Session, session: CallSession, item: CallExtractionItem
) -> None:
    """Start the lead first-touch clock for a lead that just reached HubSpot.

    Without this opener, the cockpit's "Lead first touch" SLA row has nothing
    to apply to and a worklist row like "lead untouched · 5 days" can never
    appear — the supervisor's worklist ships two-thirds empty.

    Guarded on ``CCS_LEADS_ENABLED`` like the rest of the lead path (the writer
    refuses without it, so reaching here already implies it — this is the belt
    to that braces), and non-fatal by construction: the lead is IN the client's
    CRM by the time this runs, and a commitments-table failure must not undo or
    fail a write that already succeeded. A missing clock is visible on the
    cockpit as an untracked row; a rolled-back item status is not visible
    anywhere.
    """
    if not settings.CCS_LEADS_ENABLED:
        return
    from rubik_core.cockpit import commitment_engine

    try:
        commitment_engine.open_commitment(
            db,
            kind="lead_touch",
            owner_email=_fold(session.ae_email),
            company_id=session.hubspot_company_id,
            opened_at=datetime.utcnow(),
            source="call",
            evidence_ref=item.evidence_entry_id,
            evidence_text=item.evidence_text,
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 — never fatal to a landed write
        db.rollback()
        logger.warning(
            "[ccs] lead written for session %s but the lead_touch clock did not "
            "open (item %s): %s",
            session.id,
            item.id,
            exc,
        )


# HubSpot-defined options of `hs_lead_label`. HubSpot owns this enumeration,
# so it is not routed through the live option cache the way the company's own
# picklists are.
_LEAD_LABELS = frozenset({"HOT", "WARM", "COLD"})


def _write_leads(
    db: Session,
    session: CallSession,
    client: HubSpotWriteClient,
    pairs: list[tuple[CallExtractionItem, str | None]],
    results: dict[str, dict],
) -> None:
    """Write order step 3 — leads.

    Needs the HubSpot Lead scopes granted; the Lead→Contact PRIMARY association
    is HubSpot-defined type 578, so the writer creates the lead attached to the
    call's contact. ``hs_lead_label`` is sent only when the
    extractor's temperature is exactly one of HubSpot's own HOT / WARM / COLD;
    anything else is left for the AE to pick in HubSpot. ``product_interest`` is
    NOT sent yet — its options must be matched live (see ``LEAD_WRITABLE``).

    A lead that DOES land opens a ``lead_touch`` commitment immediately — the
    first-touch clock starts when the lead exists, not when somebody notices it.
    """
    for item, value in pairs:
        payload = item.payload or {}
        # The item id occupies the property slot: a create has no target
        # property, and two leads from one call are two legitimate records.
        key = _idempotency_key(session.id, "leads", None, item.id)
        if _already_written(db, key):
            results[item.id] = {"status": "already_written"}
            item.status = "written"
            continue
        properties = {"hs_lead_name": payload.get("lead_name") or value or ""}
        temperature = str(payload.get("temperature") or "").strip().upper()
        if temperature in _LEAD_LABELS:
            properties["hs_lead_label"] = temperature
        row = _reserve_write(
            db,
            key=key,
            session_id=session.id,
            item_id=item.id,
            method="POST",
            path="/crm/v3/objects/leads",
            body=properties,
        )
        if row is None:
            results[item.id] = {"status": "already_written"}
            continue
        try:
            response = client.create_lead(
                properties,
                contact_id=session.hubspot_contact_id or "",
                owner_email=_fold(session.ae_email),
            )
        except (HubSpotWriteRefused, HubSpotWriteError) as exc:
            message = friendly_write_error("the lead", exc)
            _stamp_failure(db, [row], message)
            item.status = "write_failed"
            results[item.id] = {"status": "write_failed", "message": message}
            continue
        _stamp_success(db, [row], response)
        item.status = "written"
        results[item.id] = {"status": "written"}
        # Durable BEFORE the clock opens: ``_open_lead_touch`` rolls its own
        # transaction back on failure, and this write must not be inside it.
        db.commit()
        _open_lead_touch(db, session, item)
    db.commit()


@pin_business_time_zone
def _write_tasks(
    db: Session,
    session: CallSession,
    client: HubSpotWriteClient,
    pairs: list[tuple[CallExtractionItem, str | None]],
    results: dict[str, dict],
) -> None:
    """Write order step 4 — tasks, last because they are the most replaceable.

    ``hs_timestamp`` on a task is the DUE DATE, not a creation time: stamping
    "now" produces a task that is overdue the instant it exists. When the call
    named no date, the default is one business day out through the cockpit's
    business-hours calculator — never a raw ``timedelta``, so a Friday
    follow-up is due Monday rather than Saturday.
    """
    from rubik_core.cockpit.business_hours import add_business_hours

    for item, value in pairs:
        payload = item.payload or {}
        # Item id in the property slot — see ``_write_leads``.
        key = _idempotency_key(session.id, "tasks", None, item.id)
        if _already_written(db, key):
            results[item.id] = {"status": "already_written"}
            item.status = "written"
            continue

        due = None
        raw_due = payload.get("due_date")
        if raw_due:
            try:
                due = datetime.strptime(str(raw_due)[:10], "%Y-%m-%d")
            except (TypeError, ValueError):
                due = None
        if due is None:
            due = add_business_hours(
                datetime.utcnow(), float(settings.COCKPIT_BUSINESS_DAY_HOURS)
            )

        properties = {
            "hs_timestamp": _epoch_ms(due),
            "hs_task_subject": payload.get("subject") or value or "Follow up",
            "hs_task_status": "NOT_STARTED",
        }
        body_text = payload.get("body")
        if body_text:
            properties["hs_task_body"] = body_text

        row = _reserve_write(
            db,
            key=key,
            session_id=session.id,
            item_id=item.id,
            method="POST",
            path="/crm/v3/objects/tasks",
            body=properties,
        )
        if row is None:
            results[item.id] = {"status": "already_written"}
            continue
        try:
            response = client.create_task(
                properties,
                owner_email=_fold(session.ae_email),
                contact_id=session.hubspot_contact_id,
                company_id=session.hubspot_company_id,
                deal_id=session.hubspot_deal_id,
            )
        except (HubSpotWriteRefused, HubSpotWriteError) as exc:
            message = friendly_write_error("the task", exc)
            _stamp_failure(db, [row], message)
            item.status = "write_failed"
            results[item.id] = {"status": "write_failed", "message": message}
            continue
        _stamp_success(db, [row], response)
        item.status = "written"
        results[item.id] = {"status": "written"}
    db.commit()


def _confirm_sow_commitments(
    db: Session,
    session: CallSession,
    pairs: list[tuple[CallExtractionItem, str | None]],
    results: dict[str, dict],
) -> int:
    """The CCS → cockpit handoff, and it lives HERE.

    Extraction opened this commitment ``provisional`` — the clock runs so the
    client's request ages from when they asked, but escalation is disarmed
    because at extraction time nothing has been accepted and an unreviewed AI
    guess must never page a manager. Accepting the wrap-up item is the human
    confirmation, so this is where ``confirm()`` arms the ladder. It cannot live
    upstream for exactly that reason.

    ``_find_live`` is the engine's own resolver and is reached deliberately: it
    encodes the commitments table's two partial unique constraints (deal-scoped,
    then company-scoped-without-a-deal, then evidence for the ad-hoc case), and
    a hand-written query here would be a second place to get that scoping wrong.
    """
    from rubik_core.cockpit import commitment_engine

    confirmed = 0
    for item, _ in pairs:
        commitment = commitment_engine._find_live(
            db,
            kind="sow",
            owner_email=_fold(session.ae_email),
            deal_id=session.hubspot_deal_id,
            company_id=session.hubspot_company_id,
            evidence_ref=item.evidence_entry_id,
        )
        if commitment is None:
            # Extraction's provisional open is best-effort (it swallows its own
            # failures so a wrap-up still renders), so a missing commitment is
            # possible. Not fatal, and not silently ignored either: the item is
            # accepted, and the SOW clock is simply not running.
            logger.warning(
                "[ccs] accepted sow_request %s has no live commitment to confirm "
                "(session %s)",
                item.id,
                session.id,
            )
            results[item.id] = {
                "status": "recorded",
                "message": "Recorded. No SOW clock was found to start.",
            }
            continue
        commitment_engine.confirm(db, commitment)
        confirmed += 1
        results[item.id] = {
            "status": "recorded",
            "message": "SOW clock confirmed — escalation is armed.",
        }
    if confirmed:
        db.commit()
    return confirmed


def _close_wrapup_commitment(db: Session, session: CallSession, actor: str) -> bool:
    """Stop the wrap-up clock the sweep started when this call ended.

    The other half of the ``wrapup`` commitment opened at
    ``conference.v2.ended``. A clock nobody can stop is worse
    than no clock: it escalates and lands on the supervisor's worklist as work
    that is already done, which is exactly the noise that teaches a supervisor
    to ignore the worklist.

    Completing the wrap-up IS the human action, so the close records
    ``"manual"`` — the only two close sources are ``manual`` and ``superseded``
    and there is deliberately no automatic one. Reached through the engine's own
    ``_find_live`` for the same reason ``_confirm_sow_commitments`` is: the
    scoping rules are the table's two partial unique constraints and a
    hand-written query here would be a second place to get them wrong.

    Non-fatal: the review happened and the session is wrapped either way.
    """
    from rubik_core.cockpit import commitment_engine

    try:
        commitment = commitment_engine._find_live(
            db,
            kind="wrapup",
            owner_email=_fold(session.ae_email),
            deal_id=session.hubspot_deal_id,
            company_id=session.hubspot_company_id,
            evidence_ref=session.id,
        )
        if commitment is None:
            # Normal for a session whose sweep never opened one (an approval
            # that beat the 15-minute sweep, or capture started before the
            # opener shipped). Nothing to stop.
            return False
        commitment_engine.close_commitment(
            db,
            commitment,
            source="manual",
            evidence_ref=session.id,
            actor=actor,
        )
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — the wrap-up itself still stands
        db.rollback()
        logger.warning(
            "[ccs] session %s wrapped but its wrapup commitment did not close: %s",
            session.id,
            exc,
        )
        return False


def _apply_wrapup(
    db: Session,
    session: CallSession,
    body: WrapupApproveRequest,
    actor: str,
) -> dict:
    """Claim, write, stamp, audit. Synchronous — the caller threadpools it."""
    if not session_source_visible(session):
        raise HTTPException(status_code=404, detail="call session not found")
    if session.status not in ("extracted", "no_transcript", "wrapped"):
        raise HTTPException(
            status_code=409,
            detail="Call analysis is still pending. Retry or wait before approving.",
        )
    results, accepted = _claim_phase(db, session, body, actor)
    accepted_count = sum(len(v) for v in accepted.values())

    # ``nothing_here`` and an accepted proposal are contradictory. The count of
    # "nothing here" wrap-ups is an operator-visible measure, so an approval
    # that filed something is not allowed to inflate it.
    nothing_here = bool(body.nothing_here) and accepted_count == 0
    if body.nothing_here and not nothing_here:
        logger.warning(
            "[ccs] session %s was submitted as 'nothing here' with %s accepted "
            "item(s); recording it as a normal wrap-up",
            session.id,
            accepted_count,
        )

    client = HubSpotWriteClient()

    # Step 1 always, even for "nothing here" and even for a suppressed call.
    call_result = _log_call_engagement(
        db, session, client, has_proposals=accepted_count > 0
    )

    if accepted.get("vendor_field"):
        _write_company_fields(db, session, client, accepted["vendor_field"], results)
    if accepted.get("lead"):
        _write_leads(db, session, client, accepted["lead"], results)
    if accepted.get("task"):
        _write_tasks(db, session, client, accepted["task"], results)

    confirmed = 0
    if accepted.get("sow_request"):
        confirmed = _confirm_sow_commitments(
            db, session, accepted["sow_request"], results
        )

    # Kinds with no HubSpot home. ``stage_signal`` is read by the hourly
    # stage-lag detector; ``contract_expiry`` has no HubSpot property at all
    # and is kept because the fact is worth having somewhere.
    for kind in ("stage_signal", "contract_expiry"):
        for item, _ in accepted.get(kind, []):
            results[item.id] = {"status": "recorded"}

    # The session is wrapped even if a write failed: the AE did the review, and
    # a failed item stays ``write_failed`` and retryable from the same screen.
    # Leaving it un-wrapped would keep it in the pending list with no way to
    # clear it.
    now = datetime.utcnow()
    opened_at = session.wrap_opened_at or _naive_utc(body.wrap_opened_at)
    # WRITE-ONCE, the same discipline ``wrap_opened_at`` already uses, and for
    # the same reason. This endpoint supports a multi-visit approval on purpose
    # (an AE decides three fields now and the rest after lunch), and the
    # feature's bar — the median wrap-up time, the measure that decides whether
    # this gets used — is a measure of the FIRST wrap-up.
    # Re-stamping on the second visit turns a 65-second wrap-up into a 7-hour
    # one and poisons the median with an outlier that describes nothing.
    first_wrap = session.wrapped_at is None
    if first_wrap:
        session.wrapped_at = now
    session.status = "wrapped"
    session.nothing_here = nothing_here
    if session.wrap_opened_at is None and opened_at is not None:
        # Adopt the client's timestamp only when the server never stamped one
        # (an approval that never went through GET /wrapup). The column stays
        # authoritative where it exists, so a client cannot shorten the measure.
        session.wrap_opened_at = opened_at
    if first_wrap:
        # Reassigned, never mutated in place: SQLAlchemy does not track mutation
        # of a plain JSON dict, so ``attrs["x"] = y`` emits no UPDATE.
        session.attrs = {
            **(session.attrs or {}),
            "wrap_elapsed_ms_client": body.client_elapsed_ms,
            "wrap_elapsed_ms_server": (
                int((now - opened_at).total_seconds() * 1000) if opened_at else None
            ),
        }
    db.commit()

    # The clock the sweep started when the call ended. Closed once the wrap-up
    # is done — an accepted proposal or an explicit "nothing here" are both a
    # completed review.
    wrapup_closed = _close_wrapup_commitment(db, session, actor)

    # Audit after the commit — ``record_audit`` commits its own row, so calling
    # it mid-transaction would publish half a wrap-up.
    written = [i for i, r in results.items() if r.get("status") == "written"]
    for item_id in written:
        item = db.get(CallExtractionItem, item_id)
        if item is None:  # pragma: no cover — just written
            continue
        record_audit(
            db,
            actor_email=actor,
            action="CALL_ITEM_APPROVED",
            target=item.id,
            details={
                "kind": item.kind,
                "target_object": item.target_object,
                "target_id": item.target_id,
                "target_property": item.target_property,
                "before": item.current_value,
                "after": item.edited_value or item.proposed_value,
                "edited": bool(item.edited_value),
            },
        )
    record_audit(
        db,
        actor_email=actor,
        action="CALL_WRAPUP_APPROVED",
        target=session.id,
        details={
            "nothing_here": nothing_here,
            "decisions": len(body.decisions),
            "accepted": accepted_count,
            "written": len(written),
            "commitments_confirmed": confirmed,
            "wrapup_commitment_closed": wrapup_closed,
            # The per-approval trail the write-once timing columns no longer
            # carry: a later visit is visible here, with its own client-measured
            # elapsed time, while ``wrapped_at`` keeps naming the first wrap-up.
            "first_visit": first_wrap,
            "wrapped_at": _iso(session.wrapped_at),
            "call_logged": call_result.get("status"),
            "client_elapsed_ms": body.client_elapsed_ms,
        },
    )
    logger.info(
        "[ccs] session %s wrapped by %s: %s decision(s), %s written, "
        "nothing_here=%s, call_log=%s, elapsed_ms=%s",
        session.id,
        actor,
        len(body.decisions),
        len(written),
        nothing_here,
        call_result.get("status"),
        body.client_elapsed_ms,
    )
    return {
        "results": [
            {"item_id": d.item_id, **results.get(d.item_id, {"status": "skipped"})}
            for d in body.decisions
        ],
        # Extras beyond the agreed shape. The call log is not a decision, but it
        # is the write that always happens and the AE has to be told whether it
        # landed.
        "call_logged": call_result,
        "nothing_here": nothing_here,
        "wrapped_at": _iso(session.wrapped_at),
    }


@router.post("/{session_id}/wrapup/approve")
async def approve_wrapup(
    session_id: str,
    body: WrapupApproveRequest,
    user: UserProfile = Depends(require_ccs),
    db: Session = Depends(get_db),
):
    """Apply the AE's decisions and write the accepted ones to HubSpot.

    Not idempotency-locked at the session level on purpose: an AE may come back
    and decide the rest of the items later, and a retry after a timeout must not
    be met with a 409. Exactly-once is guaranteed one layer down instead — by
    the per-item CAS claim and the ``call_hubspot_writes`` idempotency key.

    Runs in a threadpool because the HubSpot calls are blocking. Bounded: one
    call engagement, one company PATCH, and one request per accepted lead or
    task, well inside Cloud Run's 300s request ceiling.
    """
    _require_ccs_enabled()
    session = _session_for_ae(session_id, user, db)
    if not await run_in_threadpool(session_source_visible, session):
        raise HTTPException(status_code=404, detail="call session not found")
    actor = _fold(getattr(user, "email", None))
    return await run_in_threadpool(_apply_wrapup, db, session, body, actor)
