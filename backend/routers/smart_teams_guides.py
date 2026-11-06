"""Smart Teams — the personal working guide API (ST-6).

Eight endpoints over one document: generate it, read your own draft, export it
(Word or PDF),
approve it (which publishes it and puts one copy in the companywide corpus),
unpublish it, read a colleague's published one, and — for support admins only — read
somebody's draft to answer a support question.

**A new module rather than more of ``routers/smart_teams.py``.** That router is
the interview: every path on it is about the caller's own answers and none of
them names a subject. This one is the opposite shape — most of its reads are
about *somebody else's* document — so the two have different authorization
questions and it is worth them not sharing a file. Both mount on ``app`` and
carry the same company-wide gate.

## The three rules this router does not decide

1. **Who may read what** is ``rubik_core.smart_teams.authz``, called by name.
   Nothing here compares a ``published`` flag itself, and nothing here reads a
   subject out of a body or a header: a path parameter is a *request*, checked
   against ``can_view_guide`` / ``can_view_raw_profile``, never a trusted
   identity. The one endpoint that names a subject and returns a draft is the
   support read, and ``can_view_raw_profile`` is what makes it the support admins'.
2. **Whether a sentence may be written** is ``rubik_core.smart_teams.guide``,
   which reads every layer through ``contracts`` and refuses the ones whose gate
   is closed. This router never inspects a payload.
3. **Publication is consent**, and only the subject can give it. ``guide.approve``
   is the only writer of ``published = True``, it sets ``approved_at`` in the
   same breath (a CHECK constraint refuses the row otherwise), and
   :func:`approve_guide` refuses before it even gets there for anyone but the
   subject — an admin included. There is deliberately no admin publish path.

## Generation runs on cubie-worker

``POST /generate`` writes a ``pending`` row, commits, and dispatches
:data:`GUIDE_JOB_MODULE` through ``submit_background_work``; it returns 202 with
the row id. Cloud Run throttles the service CPU to near-zero once a response
returns, so the Gemini call that gives the guide its voice would be starved in a
``BackgroundTask`` and the person would watch a spinner resolve to nothing.

A double-clicked Generate is harmless but not a no-op, and it is worth being
precise about which: ``uq_st_document_cache`` means both calls land on the SAME
row, so there is never a second document — but while that row is still
``pending`` the second call dispatches a second job, and the two overwrite the
same draft with the same content. Only once the row reaches ``ready`` does a
further call become a true cache hit that dispatches nothing. The wasted
execution is the price of not holding a lock; the thing that had to be prevented
was two guides, and it is.

The same job carries the two post-approval writes for the same reason — a Vertex
corpus import serialises behind other operations on that corpus and backs off
for minutes (``rag/manager._import_to_corpus`` retries up to 20 times). So
:func:`approve_guide` publishes synchronously, because consent must be recorded
in the request that gave it, and then dispatches ``--ingest`` for the corpus
copy. ``rag_ingested`` stays false until the job proves otherwise.

## Unpublish tells the truth about the corpus

:func:`unpublish_guide` clears ``published`` synchronously — which is what stops
every in-app read, immediately — and dispatches ``--purge``. Until that job
succeeds the guide is out of the app and *still answerable in Chat*, and the
response says so in ``rag_purge`` rather than reporting a clean retraction. The
purge itself lives in ``guide.purge_guide_vectors`` and passes both arguments
the DAM routing asymmetry needs; see its docstring.

## Two downloads, one gate, and no Save-to-Assets

``GET /{document_id}/docx`` and ``GET /{document_id}/pdf`` both carry
``can_view_guide``, so neither file can disagree with the JSON read about who may
have it — the check is written out in each rather than shared, because a future
edit that loosened one and not the other is exactly the failure to make
impossible to do by accident.

**The PDF is the copy meant to be sent.** The two source guides are distributed
as PDFs, and an editable ``.docx`` invites a colleague to fork the document
rather than read it. The ``.docx`` stays for the person who wants to adapt the
wording. Both render from the stored ``content_md`` through the same
``guide.parse_guide_blocks``, so they always say the same thing. There is deliberately **no**
endpoint that registers a guide as a DAM ``AssetMetadata`` row — the same fork
ST-5 took and for the same reason it wrote down (``smart_teams/uploads.py``,
"Storage, and why this is not an Asset"): an Asset is governed by
``rubik_core.auth.access.has_document_access``, which cannot express this
feature's rule.

Measured against the real app rather than argued (see
``backend/tests/test_smart_teams_guide_access.py``, which pins all of it):

* stamped the way generated docs are stamped — ``department=<the subject's>``,
  ``is_company_wide=False``, ``allowed_emails=[]`` — rule 6 grants a
  same-department colleague read of anything whose topic is not "Meeting
  Transcripts". With ``is_company_wide=False`` that department stamp is the
  GRANT, not a restriction.
* strip the department and the colleague is refused, but rule 1 (``is_admin``)
  and rule 1b (``sees_all_departments``) return True before any stamp is read.
  **No stamping denies an admin outside the support set or an org-wide exec.** Both are
  denied an unpublished draft by ``can_view_guide`` / ``is_support_admin``, on
  the rule that seniority must not buy somebody else's unapproved draft.

So the Asset ACL cannot hold this document under any configuration, and a copy
in it would also outlive consent: unpublish must revoke, and an Asset row is not
something ``unpublish_guide`` can retract. A person who wants the file downloads
it; a colleague reads the published guide through ``/{subject_email}``.

## Advisory only

Everything served here describes how somebody prefers to work, for the purpose
of working together. It is never a measure of competence, potential, or fitness
for a role, and nothing on this surface may be wired to performance review,
promotion, or compensation.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.auth import require_smart_teams_access
from backend.jobs.launcher import (
    reject_support_background_work,
    require_automations_http,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.db import get_db
from rubik_core.database.models import User
from rubik_core.database.smart_teams import SmartTeamsDocument, SmartTeamsProfile
from rubik_core.smart_teams import authz, guide
from rubik_core.smart_teams.instruments import disc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/smart-teams/guides",
    tags=["smart-teams"],
    dependencies=[Depends(require_smart_teams_access)],
)

#: The cubie-worker module that generates, ingests and purges. Named here, owned
#: by ``backend/jobs/smart_teams_guide_job.py``. A constant so the dispatch and
#: the report of what was dispatched cannot drift.
GUIDE_JOB_MODULE = "backend.jobs.smart_teams_guide_job"

#: The advisory promise, imported verbatim from the instrument that authored it
#: rather than re-typed, exactly as ``routers/smart_teams.py`` does. It rides
#: every response that carries guide prose, because a notice a page has to
#: hardcode is a notice that can silently go missing.
ADVISORY_NOTICE = disc.ADVISORY_NOTICE

#: Word MIME, from the service that owns the export contract.
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: PDF MIME. The PDF is the copy meant to be SENT — the two source guides are
#: distributed as PDFs, and an editable .docx invites a colleague to fork the
#: document rather than read it. The .docx stays for the person who wants to
#: adapt the wording.
PDF_MIME = "application/pdf"


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _display_name(db: Session, email: str) -> str:
    """The person's name for the title, or their local part.

    Folded compare because ``users.email`` is a case-sensitive primary key
    stored verbatim from the Google token — the same trap the interview router
    folds around. Falls back to the local part rather than to the raw address: a
    document titled "How to Be Successful Working with jdoe@acme.example" is
    worse than one titled "Jdoe", and never blocks a guide over a missing row.
    """
    folded = _fold(email)
    row = (
        db.query(User)
        .filter(func.lower(User.email) == folded, User.is_platform_support.is_(False))
        .order_by(User.email)
        .first()
    )
    name = (getattr(row, "name", None) or "").strip() if row is not None else ""
    if name:
        return name
    local = folded.split("@", 1)[0]
    return local.replace(".", " ").title() or folded


def _current_profile(db: Session, email: str) -> SmartTeamsProfile | None:
    """The person's live profile version. ``is_current`` is True or NULL, never
    False — so the filter is ``is_(True)``, matching ``routers/smart_teams.py``.
    """
    return (
        db.query(SmartTeamsProfile)
        .filter(func.lower(SmartTeamsProfile.user_email) == email)
        .filter(SmartTeamsProfile.is_current.is_(True))
        .order_by(SmartTeamsProfile.version.desc())
        .first()
    )


def _current_version(db: Session, email: str) -> int | None:
    profile = _current_profile(db, email)
    return int(profile.version) if profile is not None else None


def _document_dict(
    document: SmartTeamsDocument,
    *,
    display_name: str,
    current_version: int | None,
    include_body: bool,
) -> dict:
    """One row, as JSON.

    ``include_body`` is the whole reason this helper takes a flag rather than
    always sending ``content_md``: the list read of a colleague's guide and the
    "do I have one?" status read both want the metadata, and shipping the prose
    on every response makes it that much easier for a future caller to render a
    body it was not supposed to have.

    ``rag_ingested`` is reported as-is, deliberately. On an unpublished row that
    still says true, the guide is out of the app and still reachable by Chat, and
    a UI that wants to say so needs to be able to see it.
    """
    email = _fold(document.subject_key)
    versions = document.profile_versions or {}
    payload: dict = {
        "id": document.id,
        "kind": document.kind,
        "subject_email": email,
        "display_name": display_name,
        "title": document.title,
        "status": document.status,
        "error": document.error,
        "profile_version": versions.get(email),
        "current_profile_version": current_version,
        "stale": guide.is_stale(document, current_version),
        "published": bool(document.published),
        "approved_at": document.approved_at.isoformat()
        if document.approved_at
        else None,
        "approved_by": document.approved_by,
        "unpublished_at": document.unpublished_at.isoformat()
        if document.unpublished_at
        else None,
        "rag_ingested": bool(document.rag_ingested),
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
        "advisory_notice": ADVISORY_NOTICE,
    }
    if include_body:
        payload["content_md"] = document.content_md
    return payload


def _own_row(db: Session, email: str) -> SmartTeamsDocument | None:
    return guide.current_guide(db, email)


def _guide_error(exc: guide.GuideError) -> HTTPException:
    """A refusal from the library, as a 409.

    409 rather than 400: every :class:`~rubik_core.smart_teams.guide.GuideError`
    a request can provoke is about the *state* of the row (not ready, already
    published, still in the corpus), not about a malformed request, and the
    detail string is written to be shown to the person.
    """
    return HTTPException(status_code=409, detail=str(exc))


# ── Read ─────────────────────────────────────────────────────────────────────


@router.get("/me")
def get_own_guide(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller's own guide — draft or published — plus whether it is stale.

    Always a 200. No guide is a state ("none"), not a 404: this is the read the
    page loads with, and a 404 for the ordinary case of "you have not generated
    one yet" makes every client treat an error as normal.
    """
    email = _fold(user.email)
    version = _current_version(db, email)
    row = _own_row(db, email)
    if row is None:
        return {
            "state": "none",
            "guide": None,
            "current_profile_version": version,
            "can_generate": version is not None,
            "advisory_notice": ADVISORY_NOTICE,
        }
    return {
        "state": row.status,
        "guide": _document_dict(
            row,
            display_name=_display_name(db, email),
            current_version=version,
            include_body=True,
        ),
        "current_profile_version": version,
        "can_generate": version is not None,
        "advisory_notice": ADVISORY_NOTICE,
    }


@router.get("/{subject_email}")
def get_guide(
    subject_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """A colleague's PUBLISHED guide.

    The path value is a request, not an identity: it is folded and then run
    through ``authz.can_view_guide`` against the row's own ``published`` flag.
    Reading your own address here is allowed and returns your draft — the
    predicate says so, and refusing it would make the client special-case its
    own user for no gain.

    A subject with no published guide and a subject with no guide at all are the
    same 404 on purpose. Distinguishing them would let anybody enumerate who has
    generated a draft they have not published, which is a private state.
    """
    subject = _fold(subject_email)
    if not authz.is_employee(subject):
        raise HTTPException(
            status_code=404, detail="No published guide for that person."
        )

    if authz.is_subject(user, subject):
        row = _own_row(db, subject)
    else:
        row = guide.published_guide(db, subject)
        if row is None and authz.is_support_admin(user):
            # A support admin supporting a "where has my guide gone?" question. Still
            # gated below by can_view_guide, which grants the draft to a support
            # admin — this branch only decides which row to hand it.
            row = _own_row(db, subject)

    if row is None:
        raise HTTPException(
            status_code=404, detail="No published guide for that person."
        )
    if not authz.can_view_guide(user, subject, row.published):
        raise HTTPException(status_code=403, detail=authz.GUIDE_NOT_PUBLISHED_DENIED)

    return {
        "guide": _document_dict(
            row,
            display_name=_display_name(db, subject),
            current_version=_current_version(db, subject),
            include_body=True,
        ),
        "advisory_notice": ADVISORY_NOTICE,
    }


@router.get("/support/{subject_email}")
def support_read_guide(
    subject_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Support-admins-only: every generation for one person, drafts included.

    ``can_view_raw_profile`` rather than ``can_view_guide``, because this returns
    failed rows and error strings — support state, not a guide. That predicate is
    the narrowest gate in the app: admin AND the support row, with an exec and a team
    manager both explicitly denied. Bodies are omitted; this answers "why is
    their guide stuck", not "what does it say".
    """
    subject = _fold(subject_email)
    if not authz.can_view_raw_profile(user, subject):
        raise HTTPException(status_code=403, detail=authz.RAW_PROFILE_DENIED)

    version = _current_version(db, subject)
    name = _display_name(db, subject)
    rows = (
        db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.kind == guide.KIND_PERSONAL,
            func.lower(SmartTeamsDocument.subject_key) == subject,
        )
        .order_by(SmartTeamsDocument.created_at.desc())
        .all()
    )
    logger.info(
        "[smart-teams] support read of %s's guide rows by %s",
        subject,
        _fold(user.email),
    )
    return {
        "subject_email": subject,
        "display_name": name,
        "current_profile_version": version,
        "guides": [
            _document_dict(
                row,
                display_name=name,
                current_version=version,
                include_body=False,
            )
            for row in rows
        ],
    }


# ── Generate ─────────────────────────────────────────────────────────────────


@router.post(
    "/generate",
    status_code=202,
    dependencies=[Depends(reject_support_background_work)],
)
def generate_guide(
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Generate (or regenerate) the caller's own guide. Always for themselves.

    There is no subject parameter and there will not be one. A guide is written
    from somebody's private interview answers and published by their own
    consent, so "generate a guide for X" is not an operation this feature has.

    Regeneration is not a separate endpoint: the cache key is
    ``(kind, subject_key, profile_version_key)``, so calling this after a retake
    lands on a NEW row for the new profile version and calling it twice on the
    same version lands on the same one. That is also the staleness fix — the
    stale flag on the old row is what the UI shows a Regenerate button for.

    A ``ready`` row for the caller's current profile version is returned as-is
    with ``enqueued: false``; a ``pending`` or ``failed`` one is re-dispatched,
    which is how a job that died late gets retried without a second row.

    Refuses to overwrite a row the caller has published, with the same reasoning
    ``guide.generate_into_row`` gives: the published wording is what they
    consented to. Unpublish first.
    """
    require_automations_http()
    email = _fold(user.email)
    profile = _current_profile(db, email)
    if profile is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "There is no scored profile to write from yet. Finish the Work "
                f"Style Interview and {PRODUCT_NAME} will score it first."
            ),
        )

    try:
        row = guide.ensure_guide_row(db, profile)
    except guide.GuideError as exc:
        raise _guide_error(exc) from None

    document = row.document
    if bool(document.published):
        raise HTTPException(
            status_code=409,
            detail=(
                "Your guide for this version is published. Unpublish it first — "
                "regenerating over it would replace wording you approved."
            ),
        )

    already_ready = not row.created and document.status == "ready"
    if already_ready:
        # The cache hit. Nothing to generate: this row was built from the
        # profile version the person still has.
        db.commit()
        return {
            "enqueued": False,
            "reason": "cached",
            "guide": _document_dict(
                document,
                display_name=_display_name(db, email),
                current_version=int(profile.version),
                include_body=True,
            ),
            "advisory_notice": ADVISORY_NOTICE,
        }

    document.status = "pending"
    document.error = None
    document_id = document.id
    db.commit()

    from backend.jobs.launcher import submit_background_work

    executor, execution_name = submit_background_work(
        background_tasks,
        module=GUIDE_JOB_MODULE,
        args=["--document-id", document_id, "--generate"],
        fallback=lambda: _run_job_inline(["--document-id", document_id, "--generate"]),
        label=f"smart-teams-guide:{email}",
    )
    logger.info(
        "[smart-teams] guide %s for %s dispatched via %s (%s)",
        document_id,
        email,
        executor,
        execution_name,
    )
    db.refresh(document)
    return {
        "enqueued": True,
        "reason": "generating",
        "executor": executor,
        "execution_name": execution_name,
        "guide": _document_dict(
            document,
            display_name=_display_name(db, email),
            current_version=int(profile.version),
            include_body=False,
        ),
        "advisory_notice": ADVISORY_NOTICE,
    }


def _run_job_inline(args: list[str]) -> None:
    """Off-GCP fallback for every guide dispatch.

    ``submit_background_work`` reaches this only when ``APP_MODE`` is not GCP
    (local development) or when the Cloud Run Job launch itself failed — and in
    the second case it has already logged that loudly. Calls the job's own
    ``main`` so the local path and the deployed path run identical code rather
    than a second, thinner implementation of it that can drift.
    """
    try:
        from backend.jobs import smart_teams_guide_job

        code = smart_teams_guide_job.main(args)
    except Exception:  # noqa: BLE001 — a fallback must not take the request down
        logger.exception("[smart-teams] inline guide job failed for args %s", args)
        return
    if code:
        logger.error(
            "[smart-teams] inline guide job exited %s for args %s — the row "
            "carries the failure",
            code,
            args,
        )


# ── Approve, unpublish ───────────────────────────────────────────────────────


@router.post(
    "/{document_id}/approve",
    dependencies=[Depends(reject_support_background_work)],
)
def approve_guide(
    document_id: str,
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The subject approves their own guide, which publishes it.

    Only the subject, and the check is here as well as inside
    ``guide.approve``. Not redundancy for its own sake: this one produces the
    403 with the right copy, and that one is a belt on a function the job and a
    future caller also reach.

    Publication is synchronous — the consent is recorded in the request that
    gave it, and ``published`` and ``approved_at`` are set in one assignment
    behind ``ck_st_document_published_needs_approval``. The corpus copy is not:
    that is a Vertex import and it goes to cubie-worker.

    Approving a new version retires whatever the person published before, and
    each retired row's corpus copy is dispatched for purging — approving v3
    withdraws consent for the v2 wording, and a superseded guide left in the
    corpus keeps answering in Chat.
    """
    email = _fold(user.email)
    document = db.get(SmartTeamsDocument, document_id)
    if document is None or document.kind != guide.KIND_PERSONAL:
        raise HTTPException(status_code=404, detail="No such guide.")
    if not authz.is_subject(user, document.subject_key):
        raise HTTPException(
            status_code=403,
            detail="Only the person a guide is about can publish it.",
        )

    require_automations_http()
    try:
        superseded = guide.approve(db, document, approver_email=email)
    except guide.GuideError as exc:
        db.rollback()
        raise _guide_error(exc) from None
    db.commit()
    db.refresh(document)
    logger.info(
        "[smart-teams] guide %s published by its subject %s", document.id, email
    )

    from backend.jobs.launcher import submit_background_work

    dispatched: list[dict] = []
    plan: list[tuple[str, list[str]]] = [
        ("ingest", ["--document-id", document.id, "--ingest"]),
        *(
            ("purge", ["--document-id", row.id, "--purge"])
            for row in superseded
            if row.rag_ingested
        ),
    ]
    for action, args in plan:
        executor, execution_name = submit_background_work(
            background_tasks,
            module=GUIDE_JOB_MODULE,
            args=args,
            fallback=lambda a=args: _run_job_inline(a),
            label=f"smart-teams-guide-{action}:{email}",
        )
        dispatched.append(
            {
                "action": action,
                "document_id": args[1],
                "executor": executor,
                "execution_name": execution_name,
            }
        )

    return {
        "guide": _document_dict(
            document,
            display_name=_display_name(db, email),
            current_version=_current_version(db, email),
            include_body=True,
        ),
        "superseded": [row.id for row in superseded],
        "dispatched": dispatched,
        # Stated rather than assumed: the row says false until the job proves
        # otherwise, so the UI can say "publishing to Cubie Chat" instead of
        # claiming it is done.
        "rag_ingested": bool(document.rag_ingested),
        "advisory_notice": ADVISORY_NOTICE,
    }


@router.post(
    "/{document_id}/unpublish",
    dependencies=[Depends(reject_support_background_work)],
)
def unpublish_guide(
    document_id: str,
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The subject retracts their guide. In-app reads stop immediately.

    Two halves with different latencies, and the response reports both rather
    than averaging them into "unpublished":

    * ``published`` is cleared and committed here, so every reader that asks
      ``authz.can_view_guide`` is refused from this moment.
    * the corpus copy is removed by cubie-worker. Until it succeeds the guide is
      out of the app and Chat can still quote it, and ``rag_purge`` says
      ``pending`` — never ``done`` on the strength of having asked.

    Only the subject. A support admin cannot retract for somebody, for the same
    reason they cannot publish for them: this is a consent switch, and the person
    it belongs to is the only one who may throw it.
    """
    email = _fold(user.email)
    document = db.get(SmartTeamsDocument, document_id)
    if document is None or document.kind != guide.KIND_PERSONAL:
        raise HTTPException(status_code=404, detail="No such guide.")
    if not authz.is_subject(user, document.subject_key):
        raise HTTPException(
            status_code=403,
            detail="Only the person a guide is about can unpublish it.",
        )
    if not bool(document.published):
        # Idempotent by design: a retried POST is a no-op, not a 409. The
        # important state ("is it visible?") is already what the caller wanted.
        return {
            "guide": _document_dict(
                document,
                display_name=_display_name(db, email),
                current_version=_current_version(db, email),
                include_body=True,
            ),
            "rag_purge": "done" if not document.rag_ingested else "pending",
            "advisory_notice": ADVISORY_NOTICE,
        }

    if document.rag_ingested:
        require_automations_http()
    guide.unpublish(db, document)
    db.commit()
    db.refresh(document)
    logger.info("[smart-teams] guide %s unpublished by %s", document.id, email)

    purge = "done"
    dispatch: dict | None = None
    if document.rag_ingested:
        from backend.jobs.launcher import submit_background_work

        args = ["--document-id", document.id, "--purge"]
        executor, execution_name = submit_background_work(
            background_tasks,
            module=GUIDE_JOB_MODULE,
            args=args,
            fallback=lambda: _run_job_inline(args),
            label=f"smart-teams-guide-purge:{email}",
        )
        purge = "pending"
        dispatch = {"executor": executor, "execution_name": execution_name}

    return {
        "guide": _document_dict(
            document,
            display_name=_display_name(db, email),
            current_version=_current_version(db, email),
            include_body=True,
        ),
        "rag_purge": purge,
        "dispatched": dispatch,
        "advisory_notice": ADVISORY_NOTICE,
    }


# ── Export ───────────────────────────────────────────────────────────────────


@router.get("/{document_id}/docx")
def download_guide_docx(
    document_id: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> Response:
    """The branded Word export of a guide the caller may read.

    Same gate as the JSON read — ``can_view_guide`` against this row's own
    ``published`` flag — because a download that skips the check is the usual
    way an unpublished draft escapes. Rendered from the stored ``content_md``,
    so the file always matches what the person read and approved.
    """
    document = db.get(SmartTeamsDocument, document_id)
    if document is None or document.kind != guide.KIND_PERSONAL:
        raise HTTPException(status_code=404, detail="No such guide.")
    subject = _fold(document.subject_key)
    if not authz.can_view_guide(user, subject, document.published):
        raise HTTPException(status_code=403, detail=authz.GUIDE_NOT_PUBLISHED_DENIED)

    try:
        data, filename = guide.export_docx(
            document, display_name=_display_name(db, subject)
        )
    except guide.GuideError as exc:
        raise _guide_error(exc) from None
    return Response(
        content=data,
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{document_id}/pdf")
def download_guide_pdf(
    document_id: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> Response:
    """The PDF export — the copy meant to be sent rather than edited.

    Identical gate and identical source to the Word export: ``can_view_guide``
    against this row's own ``published`` flag, rendered from the stored
    ``content_md``. Two exports of one document must never disagree about who may
    read it, so the check is copied verbatim rather than factored into something
    a future edit could loosen for one and not the other.
    """
    document = db.get(SmartTeamsDocument, document_id)
    if document is None or document.kind != guide.KIND_PERSONAL:
        raise HTTPException(status_code=404, detail="No such guide.")
    subject = _fold(document.subject_key)
    if not authz.can_view_guide(user, subject, document.published):
        raise HTTPException(status_code=403, detail=authz.GUIDE_NOT_PUBLISHED_DENIED)

    try:
        data, filename = guide.export_pdf(
            document, display_name=_display_name(db, subject)
        )
    except guide.GuideError as exc:
        raise _guide_error(exc) from None
    return Response(
        content=data,
        media_type=PDF_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── No POST /{document_id}/save-to-assets, deliberately ──────────────────────
#
# There was one, and it was a hole: it registered ``AssetMetadata(department=
# user.department, is_company_wide=False, allowed_emails=[])`` for an
# UNPUBLISHED draft, and ``GET /api/assets/{doc_id}/download`` then served those
# exact bytes to the four readers the endpoint above refuses — measured at
# 38342 identical bytes for a same-department colleague, an admin outside the
# support set, an org-wide exec and a scoped exec over that department, each of whom
# got 404 on ``/{subject_email}`` and 403 on ``/{document_id}/docx`` in the same
# run.
#
# It is not fixable by stamping the row differently. ``has_document_access``
# rules 1 and 1b return True for ``is_admin`` and ``sees_all_departments``
# before any stamp is read, so an admin outside the support set and an org-wide
# exec can read *any* Asset — and both are people ``can_view_guide`` /
# ``is_support_admin`` deny an unapproved draft on purpose. Requiring
# ``published`` first would close the draft leak but leave a company-visible
# copy that ``unpublish_guide`` cannot retract, and unpublish must revoke.
#
# So a guide is never an Asset. ``GET /{document_id}/docx`` is the export, it
# carries ``can_view_guide``, and the person who wants the file downloads it.
# Same conclusion and same reasoning as ST-5's official-report upload
# (``rubik_core/smart_teams/uploads.py`` — "Storage, and why this is not an
# Asset"), which reached this fork first and declined it too.
#
# Pinned by ``backend/tests/test_smart_teams_guide_access.py``, which asserts
# both halves: no route, and no Asset row carrying a guide's bytes.
