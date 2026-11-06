"""Cockpit mutations — the six buttons in the sales cockpit that write something.

Mounted on the pilot-gated ``core`` parent in ``backend/main.py`` (which supplies
the ``/api`` prefix), so every endpoint here carries ``require_full_app_access``
on top of this router's own ``require_cockpit``. That gate is also where
``COCKPIT_ENABLED`` is enforced — **404 when off**, not 403: a feature that is
not provisioned is not a permission problem, and a half-provisioned environment
must not half-run the cockpit.

Two path families, one module, because this owns both halves of "correct the
record": the commitment clock (``/commitments/...``) and the stage-lag exception
(``/stage-lag/...``). The access table lists them together for that reason.

**Authorization is per-row, and it is a compare-and-swap rather than a
read-then-check.** Every mutation is an ``UPDATE … WHERE id = … AND <owner is in
the caller's reach> AND <state precondition>``, modelled on
``rubik_core/crm_sync/apply.py::_claim``. Two properties come out of that one
statement: the ownership test lives in the WHERE clause, so there is no code
path where it can be forgotten (the IDOR guard), and it is also the exactly-once
guard — a double-click, a second browser tab and a retry after a timeout all
resolve to one write, because the second statement finds the precondition no
longer true. Reach:

* **AE** — their own commitments, and nothing else.
* **Manager** — their EOS team scope, resolved through
  ``backend/routers/eos/_authz.py``. An unresolvable team fails LOUDLY:
  the 409 ``org_record_missing`` / ``team_unresolved`` from ``require_team_scope``
  rather than a mysterious 403.
* **Supervisor** — may **nudge** any AE on the pipeline and do nothing
  else. Their worklist spans every AE by design, so ``/nudge`` is the one
  endpoint that drops the owner filter; they cannot set an ETA, close, or
  accept a breach on somebody else's commitment.
* **Admin / org-wide exec** — unfiltered, and audited like everything else.

**HubSpot writes happen here, in the request path, never in a job.**
The write client resolves the active workspace's bound ``hubspot/write_token``
secret and refuses Cloud Run Jobs in code, even if that workspace has a token.
The two endpoints that write to HubSpot (``/nudge``, ``/stage-lag/{id}/apply``)
run the call through ``run_in_threadpool``: one write plus its live-state gates, well
inside Cloud Run's 300s request ceiling.

**Unlike CRM Sync, the claim is not committed separately.** There the queue row
outlives the request and a stale ``applying`` claim needs a reaper. Here the
whole action is one request, so claim → external write → commit runs in a single
transaction and a failure is compensated by ``rollback()``: no half-claimed row
can survive, at the price of holding the row lock across the HubSpot call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import (
    get_current_user,
    require_cockpit,
    require_cockpit_manager,
    require_cockpit_supervisor,
    user_can_read_transcript_text,
)
from rubik_core.brand import PRODUCT_NAME
from rubik_core.cockpit import commitment_engine
from rubik_core.cockpit.business_hours import as_naive_utc
from rubik_core.database.call_capture import CallSession
from rubik_core.database.commitments import Commitment, CommitmentStageLag
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile

logger = logging.getLogger(__name__)

# ``require_cockpit`` on the router itself: whole-router gates
# belong in the declaration, not at the top of each handler.
router = APIRouter(tags=["cockpit"], dependencies=[Depends(require_cockpit)])

# The tiers at which "accept this breach" is an offer. The escalation ladder
# introduces the action at Breached and it stays available above; accepting an
# on-time commitment is not a judgement call, it is a way to stop the clock —
# and because ``recompute_states`` skips ``accepted`` rows for the (correct)
# reason that an accepted breach must not re-escalate every 15 minutes, an
# acceptance granted early would freeze a live obligation permanently.
_ACCEPTABLE_STATES: tuple[str, ...] = ("breached", "escalated", "exec_visible")


# ── Schemas ──────────────────────────────────────────────────────────────────


class EtaRequest(BaseModel):
    promised_at: datetime
    reason: str


class CloseRequest(BaseModel):
    note: str | None = None


class AcceptRequest(BaseModel):
    reason: str


class RejectRequest(BaseModel):
    reason: str | None = None


def _commitment_out(row: Commitment) -> dict:
    """The decision-relevant fields, plus the one thing ``state`` cannot say.

    ``was_breached`` is included because closing overwrites the tier and the row
    has no ``breached_at`` column, so a client reading ``state == "closed"``
    cannot tell a clean delivery from a late one.
    """
    return {
        "id": row.id,
        "kind": row.kind,
        "owner_email": row.owner_email,
        "hubspot_deal_id": row.hubspot_deal_id,
        "hubspot_company_id": row.hubspot_company_id,
        "state": row.state,
        "sla_hours": row.sla_hours,
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "promised_at": row.promised_at.isoformat() if row.promised_at else None,
        "promised_reason": row.promised_reason,
        "breach_at": row.breach_at.isoformat() if row.breach_at else None,
        "escalated_at": row.escalated_at.isoformat() if row.escalated_at else None,
        "escalated_to": row.escalated_to,
        "accepted_reason": row.accepted_reason,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "closed_source": row.closed_source,
        "was_breached": commitment_engine.was_breached(row),
    }


def _stage_lag_call_host(db: Session, row: CommitmentStageLag) -> str | None:
    """Whose CALL the exception's quote came from, or None if unattributable.

    Delegates to the cockpit reader's batch resolver rather than keeping a second
    copy: a transcript rule with two implementations is a transcript rule with
    one that will drift. Imported inside the function because ``cockpit.py``
    pulls in the whole EOS authz leaf.
    """
    from backend.routers.cockpit import call_hosts_for_evidence_refs

    ref = str(row.evidence_ref or "").strip()
    if not ref:
        return None
    return call_hosts_for_evidence_refs(db, [ref]).get(ref)


def _stage_lag_out(
    row: CommitmentStageLag,
    *,
    db: Session,
    user: UserProfile,
    team_emails: frozenset[str],
) -> dict:
    """The exception, with ``evidence_text`` **omitted unless the caller may read it**.

    ``evidence_text`` on a stage-lag row is a verbatim client sentence off
    somebody's call, so it obeys the access table exactly like the My Day quote does:
    the AE who hosted the call, their manager, nobody else. An org-wide exec
    reaches both mutations — ``_reach_for`` returns ``unrestricted`` for them and
    ``_authorize_deal`` waves them through — and the access table gives them no
    transcript text at all, so a response that always carried the quote handed it
    to the one role designed never to see it.

    The caller arguments are keyword-only and have **no defaults**: this is the
    serializer half of the same structural trick ``cockpit.supervisor`` plays
    with ``_METADATA_COLUMNS``. There, the column is never SELECTed; here, the
    key is never built. A new call site cannot compile its way past the gate by
    forgetting an argument, and there is no path that assembles the field and
    then filters it — a filter can be dropped, an ``if`` that never adds the key
    cannot.
    """
    out = {
        "id": row.id,
        "hubspot_deal_id": row.hubspot_deal_id,
        "hubspot_stage_id": row.hubspot_stage_id,
        "evidence_stage_id": row.evidence_stage_id,
        "evidence_ref": row.evidence_ref,
        "confidence": row.confidence,
        "status": row.status,
        "resolved_by": row.resolved_by,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }
    if user_can_read_transcript_text(
        user,
        call_owner_email=_stage_lag_call_host(db, row),
        team_emails=team_emails,
    ):
        out["evidence_text"] = row.evidence_text
    return out


# ── Reach: whose rows this caller may mutate ──────────────────────────────────


@dataclass(frozen=True)
class _Reach:
    """The owner addresses a caller may mutate on this request.

    ``emails is None`` means "apply no owner filter" — an admin, an org-wide
    exec, or the supervisor on the one endpoint (``/nudge``) whose whole purpose
    is chasing other people's commitments. It is never derived from seniority
    anywhere else in this module, and it never widens access to transcript TEXT:
    that is a separate, field-level question answered by
    ``backend.auth.user_can_read_transcript_text``.
    """

    emails: frozenset[str] | None
    label: str
    # Set only for a caller who IS a cockpit manager but whose team could not be
    # resolved from the Accountability Chart. Carried this far so the failure can
    # be reported as the 409 it is, at the moment it actually blocks something,
    # instead of as a 403 that reads like "you are not a manager".
    problem: str | None = None

    @property
    def transcript_team_emails(self) -> frozenset[str]:
        """The reach as a TEAM set for ``user_can_read_transcript_text``.

        ``emails is None`` — admin, org-wide exec, the supervisor on ``/nudge`` —
        becomes the EMPTY set, never "everyone". Widening the rows a caller may
        act on must not widen the text they may read: that is the same rule
        ``backend/auth.py`` states about ``EmailScope.unrestricted``, and this
        property is where this module honours it rather than each call site
        remembering to.
        """
        return frozenset() if self.emails is None else self.emails


def _is_cockpit_manager(user: UserProfile, db: Session) -> bool:
    """Both of ``require_cockpit_manager``'s admitting paths, as a predicate.

    Delegated rather than re-derived: the EOS Accountability-Chart seat path
    lives inside that dependency and a second copy here would drift out of
    agreement with it. The 404 that ``COCKPIT_ENABLED`` raises cannot reach this
    point — the router-level ``require_cockpit`` already raised it — so
    swallowing the exception here only ever means "not a manager".
    """
    try:
        require_cockpit_manager(user, db)
    except HTTPException:
        return False
    return True


def _reach_for(db: Session, user: UserProfile, *, org_wide: bool = False) -> _Reach:
    """Resolve this caller's mutation reach. ``org_wide`` is the nudge carve-out."""
    from backend.routers.eos import _authz  # local: heavy leaf, avoids a cycle

    own = (user.email or "").strip().lower()
    if not own:  # pragma: no cover — get_current_user always supplies one
        raise HTTPException(status_code=403, detail="No identity on this request.")

    scope = _authz.email_scope(db, user)
    if scope.unrestricted:
        return _Reach(emails=None, label="unrestricted")
    if org_wide:
        return _Reach(emails=None, label="pipeline")
    if _is_cockpit_manager(user, db):
        # ``owner_emails`` already contains the caller; the union is belt and
        # braces for a manager with no eos_people row, who must still be able to
        # act on their own commitments.
        return _Reach(
            emails=frozenset(scope.owner_emails) | {own},
            label="team",
            problem=_authz.team_scope_problem(scope),
        )
    return _Reach(emails=frozenset({own}), label="own")


def _require_nudger(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserProfile:
    """Supervisor OR manager — neither public gate alone expresses the union.

    Delegates to both real dependencies instead of re-testing the department
    rosters, so the EOS-seat manager path and the admin carve-outs stay in
    ``backend/auth.py``. A non-403 HTTPException — the 404 ``COCKPIT_ENABLED``
    raises — is re-raised untouched: a feature that is off must never surface as
    a permission problem.
    """
    attempts = (
        lambda: require_cockpit_supervisor(user),
        lambda: require_cockpit_manager(user, db),
    )
    for gate in attempts:
        try:
            return gate()
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    raise HTTPException(
        status_code=403,
        detail=("Nudging is limited to the Business Services Team and sales managers."),
    )


# ── The claim ─────────────────────────────────────────────────────────────────


def _explain_claim_miss(
    db: Session,
    user: UserProfile,
    commitment_id: str,
    *,
    scoped,
    reach: _Reach,
    conflict: str,
) -> None:
    """Raise the honest status for a claim that matched no row.

    Three different failures land here and each needs a different sentence. The
    row is in reach but out of state (409 — the precondition is what failed);
    the row exists and is somebody else's (403, not 404: the supervisor and the
    manager both legitimately SEE rows they may not close, and "nothing
    happened" would be a mystery to them); or there is no such row (404).

    A manager whose team never resolved is reported here as the 409
    ``require_team_scope`` raises, not as the 403: an empty EOS scope must fail
    loudly rather than silently revoke access.
    """
    if scoped.count():
        raise HTTPException(status_code=409, detail=conflict)

    exists = (
        db.query(Commitment.id).filter(Commitment.id == commitment_id).first()
        is not None
    )
    if exists and reach.problem:
        from backend.routers.eos import _authz  # local: heavy leaf

        _authz.require_team_scope(db, user)  # raises 409 with the code + fix
    if exists:
        raise HTTPException(
            status_code=403, detail="That commitment belongs to another AE."
        )
    raise HTTPException(status_code=404, detail="commitment not found")


def _claim_commitment(
    db: Session,
    user: UserProfile,
    commitment_id: str,
    *,
    reach: _Reach,
    values: dict,
    preconditions: tuple,
    conflict: str,
) -> Commitment:
    """CAS one commitment into ``values``, or raise. The IDOR guard.

    ``values`` is written by the same statement that tests ownership and the
    precondition, which is what makes the transition exactly-once. Anything
    derived from the new values — a recomputed ``breach_at``, the close's
    ``refresh_live_scope_flags()`` — happens on the row returned here, inside
    the same transaction and under the row lock this UPDATE took.
    """
    base = db.query(Commitment).filter(Commitment.id == commitment_id)
    # Reads fold with ``func.lower()`` to use ix_commitments_owner_email_lower;
    # the column is lower-folded on write too, so this is belt and braces.
    scoped = (
        base
        if reach.emails is None
        else base.filter(func.lower(Commitment.owner_email).in_(sorted(reach.emails)))
    )
    claimed = scoped.filter(*preconditions).update(
        # ``updated_at``'s onupdate does not fire on a bulk UPDATE, so it is set
        # explicitly — same as crm_sync's _claim.
        {**values, "updated_at": datetime.utcnow()},
        synchronize_session=False,
    )
    if not claimed:
        _explain_claim_miss(
            db, user, commitment_id, scoped=scoped, reach=reach, conflict=conflict
        )

    # ``synchronize_session=False`` leaves the identity map holding the
    # pre-UPDATE attribute values. Without this expire the engine would
    # recompute from stale columns — e.g. close_commitment would read the old
    # ``closed_source`` and the ETA path the old ``promised_at``.
    db.expire_all()
    row = db.query(Commitment).filter(Commitment.id == commitment_id).first()
    if row is None:  # pragma: no cover — the claim just matched it
        raise HTTPException(status_code=404, detail="commitment not found")
    return row


# ── Commitment mutations ──────────────────────────────────────────────────────


@router.post("/commitments/{commitment_id}/eta")
async def set_eta(
    commitment_id: str,
    body: EtaRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record the AE's own delivery date and recompute the breach instant.

    The rule this endpoint exists for is the exec team's, and it belongs to
    ``commitment_engine.compute_breach_at`` rather than to this router: an ETA
    wins **only when it is later** than ``opened_at + sla_hours``, so a
    three-week RFP is not late on day four, and an ETA EARLIER than the SLA does
    not shorten the clock — an AE over-promising to a client must not
    manufacture an internal breach. Do not re-derive that comparison here; call
    the engine and let it decide. A back-dated ETA is therefore harmless, which
    is why one is accepted rather than rejected.

    A reason is required. "Set ETA with a reason" is the action the ladder
    offers at the at-risk tier, and an ETA with no reason is indistinguishable
    from someone pushing a deadline to clear an amber row.

    The state is not walked backwards: the engine forbids un-breaching, so an
    ETA set AFTER a breach moves the deadline for the escalation steps still
    ahead but leaves the row historically late.
    """
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=422, detail="A reason is required to set or revise an ETA."
        )

    reach = _reach_for(db, user)
    row = _claim_commitment(
        db,
        user,
        commitment_id,
        reach=reach,
        values={
            "promised_at": as_naive_utc(body.promised_at),
            "promised_reason": reason,
        },
        preconditions=(Commitment.closed_at.is_(None),),
        conflict="That commitment is already closed — its ETA can't be changed.",
    )

    before = row.breach_at
    row.breach_at = commitment_engine.compute_breach_at(
        row.opened_at, row.sla_hours, row.promised_at
    )
    db.commit()

    # After the commit, never before: record_audit commits the session it is
    # given and rolls it back on failure, which would undo this write.
    record_audit(
        db,
        actor_email=user.email,
        action="COMMITMENT_ETA_SET",
        target=row.id,
        details={
            "owner_email": row.owner_email,
            "reach": reach.label,
            "promised_at": row.promised_at.isoformat() if row.promised_at else None,
            "reason": reason,
            "before": {"breach_at": before.isoformat() if before else None},
            "after": {
                "breach_at": row.breach_at.isoformat() if row.breach_at else None
            },
        },
    )
    return {"commitment": _commitment_out(row)}


@router.post("/commitments/{commitment_id}/close")
async def close(
    commitment_id: str,
    body: CloseRequest | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Close a commitment. Source is always ``"manual"``, and that is the point.

    ``COMMITMENT_CLOSED_SOURCES`` has exactly two members and neither is
    "email" or "stage". Sent-mail evidence merely SUGGESTS a close; a human
    confirms it here and the row records ``"manual"``. That is the gate-4
    guarantee — a wrong auto-close hides a real breach, which is the precise
    failure this feature exists to fix. ``"superseded"`` belongs to the engine's
    merge path and is not reachable from an endpoint.

    The claim CASes ``closed_source`` (legal on its own — no constraint ties it
    to ``closed_at``) so a second click finds it already set and gets a 409;
    ``close_commitment`` then stamps ``closed_at``, the state, and
    ``refresh_live_scope_flags()``, whose biconditional CHECKs would raise an
    IntegrityError at flush if it were skipped. The engine does call it — this
    endpoint deliberately does not duplicate the call.

    ``note`` has no column on ``commitments`` (the eleven tables are final), so
    it is recorded in the audit row, which is in any case the durable answer to
    "who closed this, and why".
    """
    body = body or CloseRequest()
    reach = _reach_for(db, user)
    row = _claim_commitment(
        db,
        user,
        commitment_id,
        reach=reach,
        values={"closed_source": "manual"},
        preconditions=(
            Commitment.closed_at.is_(None),
            Commitment.closed_source.is_(None),
        ),
        conflict="That commitment is already closed.",
    )

    state_before = row.state
    commitment_engine.close_commitment(db, row, source="manual", actor=user.email)
    db.commit()

    record_audit(
        db,
        actor_email=user.email,
        action="COMMITMENT_CLOSED",
        target=row.id,
        details={
            "owner_email": row.owner_email,
            "reach": reach.label,
            "source": "manual",
            "note": (body.note or "").strip() or None,
            "was_breached": commitment_engine.was_breached(row),
            "before": {"state": state_before},
        },
    )
    return {"commitment": _commitment_out(row)}


@router.post("/commitments/{commitment_id}/accept")
async def accept_breach(
    commitment_id: str,
    body: AcceptRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accept a breach, with a reason. 422 on a blank one.

    The blank-reason rejection is enforced HERE and not only in the form, and it
    is checked in the handler rather than by a pydantic constraint so ``detail``
    stays a sentence the cockpit can render (a validation error's ``detail`` is
    a list of dicts). Whitespace counts as blank. This is the audit trail
    a supervisor would otherwise keep by hand, so an acceptance with no reason would
    lose the only column that explains a miss.

    ``accepted`` is TERMINAL until close: ``recompute_states`` skips it, which
    is what stops an accepted breach re-escalating every 15 minutes forever.
    That same skip is why only an already-breached commitment may be accepted —
    granting it earlier would stop a live clock rather than record a judgement.

    ``state`` and ``accepted_reason`` are set by ONE statement on purpose:
    ``ck_commitments_accepted_needs_reason`` fires the moment the two disagree.
    """
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=422, detail="A reason is required to accept a breach."
        )

    reach = _reach_for(db, user)
    row = _claim_commitment(
        db,
        user,
        commitment_id,
        reach=reach,
        values={"state": "accepted", "accepted_reason": reason},
        preconditions=(
            Commitment.closed_at.is_(None),
            Commitment.state.in_(_ACCEPTABLE_STATES),
        ),
        conflict=(
            "Only a breached commitment can be accepted, and only once — this "
            "one is already accepted, closed, or still inside its SLA."
        ),
    )
    db.commit()

    record_audit(
        db,
        actor_email=user.email,
        action="COMMITMENT_BREACH_ACCEPTED",
        target=row.id,
        details={
            "owner_email": row.owner_email,
            "reach": reach.label,
            "reason": reason,
            "breach_at": row.breach_at.isoformat() if row.breach_at else None,
        },
    )
    return {"commitment": _commitment_out(row)}


def _is_supervisor_only(user: UserProfile, db: Session) -> bool:
    """True for a caller admitted to ``/nudge`` as supervisor and not as manager.

    The supervisor's worklist spans every AE (every AE on one screen), so
    theirs is the only reach in this module with no owner filter that is not
    an admin's. A manager keeps the narrower team reach even if they
    are also on the supervisor roster: the tighter of two applicable scopes
    wins, which is the same direction ``cockpit_transcript_policy`` errs in.
    """
    try:
        require_cockpit_manager(user, db)
    except HTTPException:
        return True
    return False


@router.post("/commitments/{commitment_id}/nudge")
async def nudge_owner(
    commitment_id: str,
    user: UserProfile = Depends(_require_nudger),
    db: Session = Depends(get_db),
):
    """The supervisor's one button: HubSpot task + Slack DM + a ``commitment_nudges`` row.

    Runs in the request path because the write client refuses Cloud Run Jobs.
    One task write plus its live-state gates and one
    Slack DM, through ``run_in_threadpool`` — comfortably inside Cloud Run's
    300s request ceiling.

    The claim here is a lock-and-authorize rather than a state transition: a
    nudge has no column to CAS, so the UPDATE exists to test the caller's reach
    in the WHERE clause and to serialise concurrent nudges on the same row.
    ``nudge()`` commits it, along with the nudge row, as soon as the external
    writes land — neither can be rolled back, so the record of them must not be.

    **Two unacknowledged nudges is an escalation trigger, not a third nudge**
    (product rule 4: escalation changes who is accountable, it does not
    increase volume). That state comes back as a 200 with
    ``{"sent": false, "reason": "escalation_required"}`` rather than as an
    error, because it is information the worklist should render — the same
    applies to "already closed" and "no HubSpot record yet". A genuinely FAILED
    external write is a 502 with a message the user can act on, so the two are
    never confused for one another.
    """
    from rubik_core.cockpit import nudge as nudge_mod

    reach = _reach_for(db, user, org_wide=_is_supervisor_only(user, db))
    row = _claim_commitment(
        db,
        user,
        commitment_id,
        reach=reach,
        values={},
        preconditions=(Commitment.closed_at.is_(None),),
        conflict="That commitment is already closed — there is nothing to chase.",
    )
    owner_email = row.owner_email

    try:
        result = await run_in_threadpool(
            nudge_mod.nudge,
            db,
            row,
            from_email=user.email,
            to_email=owner_email,
        )
    except ValueError as exc:
        # The HubSpot task failed. ``nudge()`` has already turned the raw error
        # into a sentence through friendly_write_error; nothing was committed,
        # so the claim rolls back with it.
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    record_audit(
        db,
        actor_email=user.email,
        action="COMMITMENT_NUDGED",
        target=row.id,
        status="success" if result.get("sent") else "failure",
        details={
            "owner_email": owner_email,
            "reach": reach.label,
            "sent": result.get("sent"),
            "reason": result.get("reason"),
            "channel": result.get("channel"),
            "hubspot_task_id": result.get("hubspot_task_id"),
            "unacknowledged_nudges": result.get("unacknowledged_nudges"),
        },
    )
    return {"nudge": result, "commitment": _commitment_out(row)}


# ── Stage-lag exceptions ──────────────────────────────────────────────────────


def _deal_owner_emails(db: Session, deal_id: str) -> list[str]:
    """Cubie's own answer to "whose deal is this?", most authoritative first.

    A ``commitment_stage_lags`` row has no owner column — it is keyed on a
    HubSpot deal — so per-row authorization has to resolve one. Both sources are
    Cubie-local on purpose: the HubSpot owner is a runtime lookup with a TTL
    cache, and putting a CRM read in the authorization path would make a
    403 depend on HubSpot being up.
    """
    rows = (
        db.query(Commitment.owner_email)
        .filter(Commitment.hubspot_deal_id == str(deal_id))
        .order_by(Commitment.created_at.desc())
        .all()
    )
    rows += (
        db.query(CallSession.ae_email)
        .filter(CallSession.hubspot_deal_id == str(deal_id))
        .order_by(CallSession.created_at.desc())
        .all()
    )
    out: list[str] = []
    for (email,) in rows:
        folded = (email or "").strip().lower()
        if folded and folded not in out:
            out.append(folded)
    return out


def _authorize_deal(
    db: Session, user: UserProfile, reach: _Reach, owners: list[str]
) -> None:
    """The stage-lag equivalent of the claim's WHERE clause."""
    if reach.emails is None:
        return
    if not owners:
        # No commitment and no call session ties this deal to an AE, so there is
        # nothing to check the caller against. Refusing is the safe direction:
        # an unattributed deal is exactly the case where "I can see it, so it
        # must be mine" is wrong.
        raise HTTPException(
            status_code=403,
            detail=(
                f"{PRODUCT_NAME} can't tell whose deal this is yet, so it won't write the "
                "stage change. Ask an admin to apply it."
            ),
        )
    if set(owners) & reach.emails:
        return
    if reach.problem:
        from backend.routers.eos import _authz  # local: heavy leaf

        _authz.require_team_scope(db, user)  # raises 409 with the code + fix
    raise HTTPException(status_code=403, detail="That deal belongs to another AE.")


def _claim_stage_lag(
    db: Session, exception_id: str, *, status: str, actor: str
) -> CommitmentStageLag:
    """CAS one exception ``open`` → ``status``. The exactly-once guard.

    The status transition is written before the HubSpot call in ``/apply``, so a
    double-click cannot produce two stage writes. The row lock this takes is
    held across that call and released by the commit, and a failed write is
    compensated by ``rollback()`` — ``STAGE_LAG_STATUSES`` has no intermediate
    "applying" member, and inventing one would mean a new migration and a
    reaper for a claim that cannot outlive its request.
    """
    now = datetime.utcnow()
    claimed = (
        db.query(CommitmentStageLag)
        .filter(
            CommitmentStageLag.id == exception_id,
            CommitmentStageLag.status == "open",
        )
        .update(
            {
                "status": status,
                # A bulk UPDATE bypasses the @validates hook that folds this
                # column, and ck_commitment_stage_lags_resolved_by_lower would
                # reject a mixed-case address from the Google token.
                "resolved_by": (actor or "").strip().lower(),
                "resolved_at": now,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail="Somebody already handled this exception — refresh to see it.",
        )
    db.expire_all()
    row = (
        db.query(CommitmentStageLag)
        .filter(CommitmentStageLag.id == exception_id)
        .first()
    )
    if row is None:  # pragma: no cover — the claim just matched it
        raise HTTPException(status_code=404, detail="stage-lag exception not found")
    return row


@router.post("/stage-lag/{exception_id}/apply")
async def apply_stage_lag(
    exception_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One click writes the corrected ``dealstage``. This is the forecast repair.

    The write goes through the EXISTING ``HubSpotWriteClient.update_deal`` —
    ``DEAL_UPDATE_WRITABLE`` already whitelists ``dealstage`` and ``_stage_gate``
    already checks the stage belongs to the deal's pipeline. There is
    deliberately no second write path (stage_lag.py's docstring says so too).

    ``acting_owner_email`` is the DEAL's AE, not the person who clicked:
    ``update_deal`` re-reads the deal and refuses unless that address resolves
    to the live ``hubspot_owner_id``, which is a consistency check on the deal
    rather than a permission check on the caller. Permission was already settled
    by ``_authorize_deal``, and the human who clicked is recorded in
    ``resolved_by`` and in the audit row.

    The read comes before the claim here, unlike the commitment endpoints,
    because the row carries neither the owner nor the target stage in a form the
    WHERE clause could test. The CAS still runs before the write, so it is still
    what makes the HubSpot call exactly-once.
    """
    from rubik_core.retrieval.hubspot_write import (
        HubSpotWriteClient,
        friendly_write_error,
    )

    row = (
        db.query(CommitmentStageLag)
        .filter(CommitmentStageLag.id == exception_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="stage-lag exception not found")
    target_stage = (row.evidence_stage_id or "").strip()
    if not target_stage:
        # "verbal agreement" maps to no stage on this portal, which is why the
        # column is nullable. The exception is still worth surfacing; it just
        # cannot offer a one-click apply.
        raise HTTPException(
            status_code=422,
            detail=(
                "This exception has no target stage in HubSpot, so there is "
                "nothing to apply — move the deal by hand if it needs it."
            ),
        )

    deal_id = str(row.hubspot_deal_id)
    owners = _deal_owner_emails(db, deal_id)
    reach = _reach_for(db, user)
    _authorize_deal(db, user, reach, owners)

    # The caller when they are the AE, else Cubie's most authoritative owner for
    # the deal. Falls back to the caller only for an unrestricted admin on an
    # unattributed deal, where update_deal's own owner check will refuse with a
    # message that says exactly that.
    caller = (user.email or "").strip().lower()
    acting_owner = caller if caller in owners or not owners else owners[0]

    row = _claim_stage_lag(db, exception_id, status="applied", actor=user.email)
    stage_before = row.hubspot_stage_id
    try:
        await run_in_threadpool(
            _write_dealstage,
            HubSpotWriteClient(),
            deal_id,
            target_stage,
            acting_owner,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is compensated alike
        # A refusal, a 429, a 400, an unset token: all of them must leave the
        # exception OPEN so it can be retried or rejected. Nothing was
        # committed, so the rollback IS the compensation — there is no
        # intermediate "applying" status to reap.
        db.rollback()
        logger.warning(
            "[cockpit] stage-lag apply failed for exception %s (deal %s): %s",
            exception_id,
            deal_id,
            exc,
        )
        # friendly_write_error passes a HubSpotWriteRefused through verbatim, and
        # update_deal's ownership refusal is worded for CRM Sync ("only updates
        # deals you own") — which sends a manager who clicked this looking for
        # the wrong problem, since the acting owner is the deal's AE and not
        # them. Naming the address Cubie acted as makes the real cause readable
        # without rewording a message CRM Sync also depends on.
        detail = friendly_write_error("the deal stage", exc)
        raise HTTPException(
            status_code=502,
            detail=f"{detail} ({PRODUCT_NAME} applies the stage as {acting_owner}.)",
        ) from exc

    db.commit()
    record_audit(
        db,
        actor_email=user.email,
        action="STAGE_LAG_APPLIED",
        target=row.id,
        details={
            "hubspot_deal_id": deal_id,
            "acting_owner_email": acting_owner,
            "reach": reach.label,
            "before": {"dealstage": stage_before},
            "after": {"dealstage": target_stage},
            "confidence": row.confidence,
        },
    )
    return {
        "exception": _stage_lag_out(
            row, db=db, user=user, team_emails=reach.transcript_team_emails
        )
    }


def _write_dealstage(
    client, deal_id: str, stage_id: str, acting_owner_email: str
) -> dict:
    """The single sanctioned stage write. Kept separate only to be threadpooled."""
    return client.update_deal(
        deal_id, {"dealstage": stage_id}, acting_owner_email=acting_owner_email
    )


@router.post("/stage-lag/{exception_id}/reject")
async def reject_stage_lag(
    exception_id: str,
    body: RejectRequest | None = None,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """ "Not right" — a LABELLED SIGNAL, not a dismiss.

    The rejection rate per signal type is the detector's tuning input and the
    only honest answer to "is this helping?", so the rejection has to be
    computable, not just cleared off a screen. What makes it computable is
    already on the row: ``status="rejected"`` plus ``evidence_stage_id``, which
    ``stage_lag.not_right_rates`` maps back to a signal name through
    ``signal_for_stage`` (reporting an unmappable stage under its own id rather
    than folding it into a bucket that would misattribute it). ``resolved_at``
    is what ``_is_suppressed_by_rejection`` compares newer evidence against, so
    a rejection suppresses the finding it predates and not the deal — saying
    "not right" to the 12 Aug quote must not bury a stronger signal from the
    20th.

    The free-text ``reason`` has no column (the eleven tables are final), so it
    lands in the audit row. The RATE does not depend on it; the reason is
    colour for whoever tunes ``COCKPIT_STAGE_LAG_MIN_CONFIDENCE``.
    """
    body = body or RejectRequest()
    row = (
        db.query(CommitmentStageLag)
        .filter(CommitmentStageLag.id == exception_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="stage-lag exception not found")

    deal_id = str(row.hubspot_deal_id)
    reach = _reach_for(db, user)
    _authorize_deal(db, user, reach, _deal_owner_emails(db, deal_id))

    row = _claim_stage_lag(db, exception_id, status="rejected", actor=user.email)
    db.commit()

    record_audit(
        db,
        actor_email=user.email,
        action="STAGE_LAG_REJECTED",
        target=row.id,
        details={
            "hubspot_deal_id": deal_id,
            "reach": reach.label,
            "reason": (body.reason or "").strip() or None,
            "hubspot_stage_id": row.hubspot_stage_id,
            "evidence_stage_id": row.evidence_stage_id,
            "confidence": row.confidence,
        },
    )
    return {
        "exception": _stage_lag_out(
            row, db=db, user=user, team_emails=reach.transcript_team_emails
        )
    }
