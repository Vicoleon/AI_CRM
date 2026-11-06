"""Smart Teams — official-report upload, parse review, and merge (ST-5).

Its own module rather than more endpoints on ``backend/routers/smart_teams.py``,
for the reason that file gives for existing at all: that one is the interview,
which every employee walks through, and this one is an **optional enrichment**
almost nobody uses. Only the Executive Team has official DISC results (plan.md
Decision 1). Mixing the two would make it very easy for a future change to give
the interview a "you still have not uploaded your report" shape, which is exactly
what must not happen — no endpoint here nags, and every read returns an empty
list rather than a gap.

Mounted on ``app`` beside the interview router, NOT on the pilot-gated ``core``
parent, and carrying ``require_smart_teams_access`` on the ``APIRouter`` itself so
no endpoint can be added without it. Same reasoning: the audience is everybody
who has a profile.

**The subject is always the authenticated caller.** No endpoint below reads an
email out of a body, a header, or a query string. The two functions that take an
identifier at all are:

* every own-data path, which passes ``user.email`` into
  ``uploads.get_upload`` / ``uploads.list_uploads`` — and ``get_upload`` puts the
  owner into the SQL predicate rather than checking it after the read, so a
  person holding somebody else's upload id gets a 404 and is not even told the id
  exists; and
* the support read, which takes the subject from the PATH and hands it
  to ``authz.can_view_raw_profile`` rather than trusting it. A parsed report is
  raw psychometric material, so that predicate — the person, plus a support
  admin, and nobody else, not a manager and not an exec — is the right one.

A ``framework`` in the confirm body is not an exception to this. It is an echo of
the proposal the person read, checked for equality against the stored one, so a
proposal that changed under them cannot be confirmed by accident. It never
selects which layer the numbers land in.

**Parsing runs on cubie-worker, never here.** Reading a PDF and making a Vertex
call in the request would be starved by Cloud Run's post-response CPU throttle
and the upload would sit ``pending`` forever. ``POST /uploads`` commits the row
first, then dispatches ``backend.jobs.smart_teams_parse_job`` through
``submit_background_work``.

**Merging runs here, deliberately.** It is deterministic arithmetic over four
integers plus one append-only insert — milliseconds, no model call — and it has
to be synchronous because its whole point is that a person pressed a button and
needs to be told what changed. Putting it on a job would mean answering "did my
report merge?" with a poll.

Nothing in this module is a performance surface. A merged layer informs
collaboration and team staffing, and never performance review, promotion, or
compensation.
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import require_smart_teams_access
from backend.jobs.launcher import (
    reject_support_background_work,
    require_automations_http,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.smart_teams import uploads as up
from rubik_core.smart_teams.authz import RAW_PROFILE_DENIED, can_view_raw_profile
from rubik_core.smart_teams.instruments.disc import ADVISORY_NOTICE

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/smart-teams",
    tags=["smart-teams"],
    dependencies=[Depends(require_smart_teams_access)],
)

#: The cubie-worker module that parses one uploaded report. Named as a constant
#: so the dispatch call and the report of what was dispatched cannot drift, and
#: registered in ``backend/jobs/__init__.py``'s ``WORKER_JOBS`` so the command to
#: run it by hand is not only in a docstring.
PARSE_JOB_MODULE = "backend.jobs.smart_teams_parse_job"

#: Said on every read of this section, because the whole feature is optional and
#: an empty list has to read as "fine" rather than "missing". Not a nag and not a
#: prompt to upload anything.
OPTIONAL_NOTICE = (
    "Uploading an official assessment report is optional. Most people do not "
    "have one, your profile is complete without it, and nothing here is waiting "
    "on you."
)

#: What confirming actually does, in the person's terms. Shown with the proposal
#: so the consequence is on the screen where the decision is made rather than
#: discovered afterwards.
MERGE_NOTICE = (
    "Confirming replaces just this one part of your profile with the numbers "
    "from your report and leaves everything else exactly as it is. It creates a "
    "new version of your profile, which means your working guide has to be "
    "written again and approved by you again before colleagues can see it."
)


class ConfirmRequest(BaseModel):
    """The confirm body. One optional field, and it is not an identity.

    ``framework`` is an ECHO of the framework the person saw proposed. When
    supplied it must match the stored proposal, which makes a confirmation a
    statement about the thing they actually read: if a re-parse changed the
    proposal under them, the confirm is refused and they look again. It never
    chooses which layer the numbers go into — pointing a vendor's DISC scores at,
    say, the motivators layer is precisely what the review step exists to
    prevent, and the target comes from the parse.
    """

    framework: str | None = None


def _fold(email: str | None) -> str:
    """Fold an address for comparison. ``users.email`` is stored verbatim from
    the Google token, so every Smart Teams surface folds — see
    ``database/smart_teams.py``'s "Email case" note."""
    return (email or "").strip().lower()


def _payload(row) -> dict:
    return {**up.upload_dict(row), "advisory_notice": ADVISORY_NOTICE}


@router.get("/uploads")
def list_own_uploads(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller's own uploads, newest first. ``[]`` is the normal answer.

    An empty list is the state almost every employee is in and is reported as an
    ordinary, complete answer — no ``missing``, no ``incomplete``, nothing a UI
    could render as a to-do.
    """
    rows = up.list_uploads(db, user.email)
    return {
        "uploads": [_payload(row) for row in rows],
        "optional_notice": OPTIONAL_NOTICE,
        "mergeable_frameworks": sorted(up.MERGE_ADAPTERS),
    }


@router.post(
    "/uploads",
    status_code=202,
    dependencies=[
        Depends(reject_support_background_work),
        Depends(require_automations_http),
    ],
)
def create_own_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Store one official report of the caller's own and queue the parse.

    202, not 201: the row exists but the proposal does not yet. The response
    carries the row (``review_status: "pending"``, ``framework: null``) plus what
    was dispatched, so the UI polls ``GET /uploads/{id}`` rather than guessing.

    The bytes are read in full here — they have to be, to bound the size and to
    store them — but nothing is *parsed* here. See the module docstring.
    """
    require_automations_http()
    if not file.filename:
        raise HTTPException(status_code=400, detail="That file had no name.")
    data = file.file.read()
    try:
        row = up.create_upload(db, user.email, filename=file.filename, data=data)
    except up.UploadError as exc:
        # Every UploadError raised by create_upload is a sentence written for the
        # person (wrong extension, empty file, over the size bound), so it is
        # safe and useful to return verbatim.
        raise HTTPException(status_code=400, detail=str(exc)) from None

    from backend.jobs.launcher import submit_background_work

    upload_id = row.id
    executor, execution_name = submit_background_work(
        background_tasks,
        module=PARSE_JOB_MODULE,
        args=["--upload-id", upload_id],
        fallback=lambda: _parse_inline(upload_id),
        label=f"smart-teams-parse:{_fold(user.email)}",
    )
    logger.info(
        "[smart-teams] upload %s queued for parsing via %s (%s)",
        upload_id,
        executor,
        execution_name,
    )
    return {
        **_payload(row),
        "optional_notice": OPTIONAL_NOTICE,
        "parse": {
            "status": "pending",
            "job_module": PARSE_JOB_MODULE,
            "executor": executor,
            "execution_name": execution_name,
        },
    }


def _parse_inline(upload_id: str) -> None:
    """Off-GCP fallback for the parse dispatch. Never runs on the deployed app.

    ``submit_background_work`` reaches this only when ``APP_MODE`` is not GCP
    (local development) or when the Cloud Run Job launch itself failed, and in
    the second case it has already logged that loudly. Opens its own session,
    because the request's is closed by the time a BackgroundTask runs.
    """
    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        up.parse_upload(db, upload_id)
    except Exception:  # noqa: BLE001 — a fallback must not take the request down
        logger.exception("[smart-teams] inline parse failed for upload %s", upload_id)
    finally:
        db.close()


@router.get("/uploads/{upload_id}")
def get_own_upload(
    upload_id: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """One of the caller's own uploads, with the proposal if the parse has run.

    404 for an id the caller does not own — the ownership test is inside the
    query (``uploads.get_upload``), so this endpoint has no branch that could
    return somebody else's row, and it does not confirm that the id exists.
    """
    row = up.get_upload(db, upload_id, user.email)
    if row is None:
        raise HTTPException(status_code=404, detail="No such upload.")
    body = _payload(row)
    if body["mergeable"] and row.review_status == up.REVIEW_PENDING:
        body["merge_notice"] = MERGE_NOTICE
    return body


@router.post("/uploads/{upload_id}/confirm")
def confirm_own_upload(
    upload_id: str,
    body: ConfirmRequest | None = None,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The person confirms the mapping, and the merge happens. One new version.

    409 rather than 400 for a refusal that is about *state* — already merged,
    already rejected, nothing mergeable proposed — because the request was
    well-formed and the answer is "not from here". The detail is the sentence
    ``uploads`` wrote for that case (``PARSE_REASONS``), so the person is told
    which of the several "we cannot use this report" outcomes they hit.
    """
    row = up.get_upload(db, upload_id, user.email)
    if row is None:
        raise HTTPException(status_code=404, detail="No such upload.")
    try:
        outcome = up.confirm_upload(
            db,
            upload_id,
            user.email,
            framework=(body.framework if body is not None else None),
        )
    except up.UploadError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    db.refresh(row)
    return {
        **_payload(row),
        "merged": {
            "framework": outcome.framework,
            "profile_id": outcome.profile_id,
            "profile_version": outcome.profile_version,
            "provenance": dict(outcome.provenance),
            "untouched_frameworks": list(outcome.untouched_frameworks),
        },
        "guide": {
            "unpublished_document_ids": list(outcome.guides.unpublished),
            # Split on purpose: a guide can be out of company view and still be
            # answerable in Chat until its chunks are gone. One count would let
            # the second failure hide behind the first success.
            "purged_document_ids": list(outcome.guides.purged),
            "still_ingested_document_ids": list(outcome.guides.purge_failed),
            "regeneration": outcome.guides.regeneration,
            # Said plainly because it is a consequence the person has to act on:
            # their profile advanced, so the guide colleagues could read has been
            # taken out of view until it is written again and re-approved.
            "message": (
                "Your working guide has been taken out of company view because "
                "your profile changed. It needs to be written again and "
                "approved by you before colleagues can see it."
            )
            if outcome.guides.unpublished
            else None,
        },
    }


@router.post("/uploads/{upload_id}/reject")
def reject_own_upload(
    upload_id: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The person says no. Nothing about their profile changes.

    The row and the stored file are kept rather than deleted — they uploaded
    something, an admin may be asked what happened to it, and a rejected parse is
    the case a support request is most likely to be about. Rejecting twice is a
    no-op, not an error.
    """
    row = up.get_upload(db, upload_id, user.email)
    if row is None:
        raise HTTPException(status_code=404, detail="No such upload.")
    try:
        row = up.reject_upload(db, upload_id, user.email)
    except up.UploadError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _payload(row)


@router.get("/support/uploads/{subject_email}")
def support_read_uploads(
    subject_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """One person's uploads, for a support admin debugging a parse with them.

    The path value names the SUBJECT and is never trusted as the caller:
    ``authz.can_view_raw_profile`` decides, and it grants exactly two viewers —
    the person themselves and a support admin. An exec is refused, a team manager
    is refused, and an admin outside the support set is refused. The refusal copy comes
    from that module too (:data:`RAW_PROFILE_DENIED`), so every surface says the
    same thing.

    403 rather than 404, matching the interview support read: that a colleague
    uploaded a report is not the secret; what is in it is.

    Read-only on purpose. A support admin can see a bad parse and re-run the job,
    and cannot confirm a merge on somebody's behalf — a merge is a consent, and
    consent is not a support action.
    """
    subject = _fold(subject_email)
    if not can_view_raw_profile(user, subject):
        raise HTTPException(status_code=403, detail=RAW_PROFILE_DENIED)
    # Audited: a parsed report carries the scores off an official assessment,
    # which is the same class of private data as the interview answers.
    logger.info(
        "[smart-teams][uploads] support read of %s's uploads by %s",
        subject,
        _fold(user.email),
    )
    rows = up.list_uploads(db, subject)
    return {
        "subject_email": subject,
        "uploads": [_payload(row) for row in rows],
        "parse_job_module": PARSE_JOB_MODULE,
        "optional_notice": OPTIONAL_NOTICE,
    }
