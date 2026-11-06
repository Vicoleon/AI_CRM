"""Proposal Generator — draft a client proposal from a captured sales call.

Mounted on the pilot-gated ``core`` parent in ``backend/main.py`` (which adds
the ``/api`` prefix). On top of that, every endpoint carries
:func:`require_proposals`: a 404 while ``PROPOSALS_ENABLED`` is off (a feature
that is off is not a permission problem, and must not advertise itself), then
a 403 unless Admin → Feature Access admits the caller (registry key
``proposal_generator``).

Ownership is per row and per call. Only the AE on a call may start a proposal
from it, and only the proposal's owner (or an admin, for support) may read or
change it. A manager reads their team's wrap-ups; they do not draft their
team's proposals — the document goes out under the AE's name.

The model calls run behind ``run_in_threadpool``: a brief extraction over a
long transcript with the PRO model blocks for tens of seconds and must not
hold the event loop. They are still inside the request (well under Cloud Run's
300s cap and the client's 105s axios ceiling for a discovery call), which
keeps this feature off the worker Job for its first release; a queue is the
follow-up if a real transcript ever pushes past that.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import get_current_user, user_can_access_proposals
from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.profile import get_company_profile
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession
from rubik_core.database.db import get_db
from rubik_core.database.proposals import Proposal, ProposalVersion
from rubik_core.database.schemas import UserProfile
from rubik_core.proposals import render, service, templates
from rubik_core.proposals.brand import letterhead
from rubik_core.proposals.brief import brief_is_complete


def require_proposals(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """WHETHER (kill switch, 404) then WHO (roster, 403)."""
    if not settings.PROPOSALS_ENABLED:
        raise HTTPException(
            status_code=404, detail="Proposal drafting is not enabled here."
        )
    if not user_can_access_proposals(user):
        raise HTTPException(
            status_code=403,
            detail="Proposal drafting is not open to your team yet.",
        )
    return user


router = APIRouter(
    prefix="/proposals", tags=["proposals"], dependencies=[Depends(require_proposals)]
)


# ── Bodies ───────────────────────────────────────────────────────────────────


class BriefBody(BaseModel):
    brief: dict[str, Any]


class ReviseBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=service.MAX_INSTRUCTION_CHARS)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _version_out(row: ProposalVersion, *, with_content: bool = True) -> dict:
    out = {
        "id": row.id,
        "version_no": row.version_no,
        "instruction": row.instruction,
        "change_summary": row.change_summary,
        "model": row.model,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
    }
    if with_content:
        out["brief"] = row.brief
        out["content"] = row.content
    return out


def _proposal_summary(db: Session, row: Proposal) -> dict:
    """A list row: no brief, no version bodies."""
    count = (
        db.query(ProposalVersion.id)
        .filter(ProposalVersion.proposal_id == row.id)
        .count()
    )
    pf = (row.brief or {}).get("prepared_for") or {}
    return {
        "id": row.id,
        "owner_email": row.owner_email,
        "call_session_id": row.call_session_id,
        "company_name": row.company_name or pf.get("company"),
        "prepared_for": pf.get("name"),
        "title": row.title,
        "status": row.status,
        "brief_confirmed_at": _iso(row.brief_confirmed_at),
        "versions": count,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _proposal_out(db: Session, row: Proposal) -> dict:
    versions = (
        db.query(ProposalVersion)
        .filter(ProposalVersion.proposal_id == row.id)
        .order_by(ProposalVersion.version_no.asc())
        .all()
    )
    return {
        "id": row.id,
        "owner_email": row.owner_email,
        "call_session_id": row.call_session_id,
        "hubspot_company_id": row.hubspot_company_id,
        "hubspot_deal_id": row.hubspot_deal_id,
        "company_name": row.company_name,
        "title": row.title,
        "status": row.status,
        # Briefs stored before templates existed have no "template" field.
        "brief": {"template": {"key": ""}, **(row.brief or {})},
        "brief_confirmed_at": _iso(row.brief_confirmed_at),
        # Advisory gaps the form shows above the Generate button.
        "gaps": brief_is_complete(row.brief or {}),
        "versions": [_version_out(v) for v in versions],
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _own_session(session_id: str, user: UserProfile, db: Session) -> CallSession:
    session = db.get(CallSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="call session not found")
    if _fold(session.ae_email) != _fold(user.email) and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="only the AE on a call can draft its proposal"
        )
    if not service.session_sources_visible(session):
        raise HTTPException(status_code=404, detail="call session not found")
    return session


def _own_proposal(proposal_id: str, user: UserProfile, db: Session) -> Proposal:
    row = db.get(Proposal, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if _fold(row.owner_email) != _fold(user.email) and not user.is_admin:
        raise HTTPException(status_code=403, detail="not your proposal")
    if not service.proposal_visible(row):
        raise HTTPException(status_code=404, detail="proposal not found")
    return row


def _map_error(exc: service.ProposalError) -> HTTPException:
    if isinstance(exc, service.SourceAccessChanged):
        return HTTPException(status_code=404, detail="proposal not found")
    if isinstance(exc, service.SelectedProviderFailed):
        return HTTPException(
            status_code=502,
            detail="The selected workspace AI provider is unavailable or returned invalid JSON. Ask your admin to check its credential and model.",
        )
    if isinstance(exc, service.ModelFailed):
        return HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} could not reach the model to draft this. Try again in a moment.",
        )
    if isinstance(exc, (service.BriefNotConfirmed, service.NothingToRevise)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, service.NoTranscript):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("")
async def list_proposals(
    all: bool = Query(False, description="Admins only: every AE's proposals."),
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """The caller's proposals, newest first. ``?all=true`` is honoured for
    admins only — anyone else silently gets their own list."""
    include_all = bool(all and user.is_admin)
    rows = await run_in_threadpool(
        service.list_proposals,
        db,
        actor_email=_fold(user.email),
        include_all=include_all,
    )
    return {
        "proposals": [_proposal_summary(db, r) for r in rows],
        "scope": "all" if include_all else "mine",
    }


@router.get("/templates")
async def list_templates(
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """The workspace's active proposal templates, for the brief form: each
    one's headings, and the catalog list prices that become pricing lines.
    Empty until an admin writes one in Admin → Company setup."""
    return {
        "templates": [
            templates.as_api(db, t)
            for t in templates.list_templates(db, active_only=True)
        ]
    }


@router.get("/calls")
async def list_captured_calls(
    days: int = Query(14, ge=1, le=90),
    all: bool = Query(False, description="Admins only: every AE's calls."),
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """Captured calls a proposal can be drafted from, with whether one exists.

    No transcript text crosses this endpoint — company, AE, times, status and
    the Fathom title only — so an admin's ``?all=true`` view is metadata, the
    same class of data the pending wrap-up list already shows.
    """
    include_all = bool(all and user.is_admin)
    return {
        "calls": await run_in_threadpool(
            service.recent_captured_calls,
            db,
            actor_email=_fold(user.email),
            include_all=include_all,
            days=days,
        ),
        "scope": "all" if include_all else "mine",
        "days": days,
    }


@router.get("/for-call/{session_id}")
async def for_call(
    session_id: str,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """The proposal already drafted from this call, if any — so the wrap-up
    can offer "Open proposal" instead of "Draft proposal"."""
    await run_in_threadpool(_own_session, session_id, user, db)
    row = service.existing_for_call(db, session_id)
    if row is not None and not await run_in_threadpool(service.proposal_visible, row):
        row = None
    return {
        "proposal_id": row.id if row else None,
        "status": row.status if row else None,
    }


@router.post("/from-call/{session_id}")
async def from_call(
    session_id: str,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """Step 1: start a proposal from a call and fill its brief. Idempotent."""
    session = await run_in_threadpool(_own_session, session_id, user, db)
    try:
        row = await run_in_threadpool(
            service.start_from_call, db, session, actor_email=_fold(user.email)
        )
    except service.ProposalError as exc:
        raise _map_error(exc) from exc
    record_audit(
        db,
        actor_email=_fold(user.email),
        action="PROPOSAL_STARTED",
        target=row.id,
        details={"call_session_id": session.id, "company": row.company_name},
    )
    return {"proposal": _proposal_out(db, row)}


@router.post("/from-fathom/{recording_id}")
async def from_fathom(
    recording_id: str,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """Step 1 by Fathom recording id — the Sales Coach's "Continue in Cubie".

    Resolves (or attaches on demand) the captured call behind the recording,
    then behaves exactly like ``from-call``. Both model steps — the attach's
    extraction and the brief — can run here, so this is the slowest endpoint
    in the router; still a discovery call's worth of transcript, still inside
    the request budget.
    """

    def _work():
        session = service.session_for_fathom(
            db,
            recording_id,
            actor_email=_fold(user.email),
            is_admin=bool(user.is_admin),
        )
        return session, service.start_from_call(
            db, session, actor_email=_fold(user.email)
        )

    try:
        session, row = await run_in_threadpool(_work)
    except service.RecordingNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.NotYourCall as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except service.CouldNotAttach as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.ProposalError as exc:
        raise _map_error(exc) from exc
    record_audit(
        db,
        actor_email=_fold(user.email),
        action="PROPOSAL_STARTED",
        target=row.id,
        details={
            "call_session_id": session.id,
            "fathom_recording_id": recording_id,
            "company": row.company_name,
        },
    )
    return {"proposal": _proposal_out(db, row), "session_id": session.id}


@router.get("/{proposal_id}")
async def get_proposal(
    proposal_id: str,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    row = await run_in_threadpool(_own_proposal, proposal_id, user, db)
    return {"proposal": _proposal_out(db, row)}


@router.put("/{proposal_id}/brief")
async def put_brief(
    proposal_id: str,
    body: BriefBody,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """The AE confirms (or corrects) the brief. Nothing is generated here."""
    row = await run_in_threadpool(_own_proposal, proposal_id, user, db)
    try:
        await run_in_threadpool(
            service.save_brief, db, row, body.brief, actor_email=_fold(user.email)
        )
    except service.ProposalError as exc:
        raise _map_error(exc) from exc
    return {"proposal": _proposal_out(db, row)}


@router.post("/{proposal_id}/generate")
async def generate(
    proposal_id: str,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """Step 2: a new version straight from the confirmed brief."""
    row = await run_in_threadpool(_own_proposal, proposal_id, user, db)
    try:
        version = await run_in_threadpool(
            service.generate, db, row, actor_email=_fold(user.email)
        )
    except service.ProposalError as exc:
        raise _map_error(exc) from exc
    record_audit(
        db,
        actor_email=_fold(user.email),
        action="PROPOSAL_GENERATED",
        target=row.id,
        details={"version_no": version.version_no},
    )
    return {"proposal": _proposal_out(db, row), "version": _version_out(version)}


@router.post("/{proposal_id}/revise")
async def revise(
    proposal_id: str,
    body: ReviseBody,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """Step 3: apply one instruction to the latest version; append the result."""
    row = await run_in_threadpool(_own_proposal, proposal_id, user, db)
    try:
        version = await run_in_threadpool(
            service.revise, db, row, body.instruction, actor_email=_fold(user.email)
        )
    except service.ProposalError as exc:
        raise _map_error(exc) from exc
    record_audit(
        db,
        actor_email=_fold(user.email),
        action="PROPOSAL_REVISED",
        target=row.id,
        details={
            "version_no": version.version_no,
            "instruction": body.instruction[:200],
        },
    )
    return {"proposal": _proposal_out(db, row), "version": _version_out(version)}


@router.get("/{proposal_id}/versions/{version_no}/download")
async def download(
    proposal_id: str,
    version_no: int,
    format: str = Query("pdf", pattern="^(pdf|docx)$"),
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    """The document itself. Rendered on demand from the stored version — the
    bytes are never persisted, so a renderer fix reaches old versions too."""
    row = await run_in_threadpool(_own_proposal, proposal_id, user, db)
    version = (
        db.query(ProposalVersion)
        .filter(
            ProposalVersion.proposal_id == row.id,
            ProposalVersion.version_no == version_no,
        )
        .first()
    )
    if version is None:
        raise HTTPException(status_code=404, detail="version not found")
    brief, content = version.brief or {}, version.content or {}
    looks = {
        "letterhead": letterhead(get_company_profile(db)),
        # Active or retired: switching a template off keeps it on proposals
        # that already carry it. Only a deleted template stops printing.
        "template": templates.get_by_key(db, (brief.get("template") or {}).get("key")),
    }
    if format == "docx":
        data = await run_in_threadpool(render.render_docx, brief, content, **looks)
        media, ext = render.DOCX_MIME, "docx"
    else:
        data = await run_in_threadpool(render.render_pdf, brief, content, **looks)
        media, ext = render.PDF_MIME, "pdf"
    name = render.filename(brief, version.version_no, ext)
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/{proposal_id}/archive")
async def archive(
    proposal_id: str,
    user: UserProfile = Depends(require_proposals),
    db: Session = Depends(get_db),
):
    row = await run_in_threadpool(_own_proposal, proposal_id, user, db)
    row.status = "archived"
    db.commit()
    return {"proposal": _proposal_out(db, row)}
