"""Smart Teams — the pair working-relationship guide API (ST-7).

Three endpoints over one document: generate the guide written to me about a
colleague, read it, and — for support admins only — look at the rows to answer "why is
theirs stuck". There is deliberately no approve, no unpublish, and no export.

## The three decisions this router makes, and where each one lives

1. **Who may READ a pair guide** is ``authz.can_view_multi_subject_guide``,
   reached through ``comparison.can_read_pair``, which builds the per-subject
   published map from each person's OWN personal guide row. Nothing here compares
   a ``published`` flag itself. The pair row's own ``published`` column is
   ignored on purpose and is never written: one flag cannot record two consents.
2. **Who may GENERATE one** is ``comparison.refuse_generation`` — every subject,
   the requester included, must have published their own personal guide — plus
   one thing only this file can enforce, below.
3. **Whether a sentence may be written** is ``rubik_core.smart_teams.comparison``,
   which reads every layer through ``contracts`` and refuses any framework whose
   gate is closed for EITHER person. This router never inspects a payload.

## Why there is no ``subject_a`` parameter, and why that is the whole answer to
## "may one employee generate a pair guide for two OTHER people?"

**No.** Not by a permission check that a future edit can loosen — by there being
no way to say it. Every path here takes ONE email, the colleague, and the reader
is always ``get_current_user``. So the requester is structurally one of the two
people in every document this router can produce.

Three reasons, and the first is the one that matters:

* A pair guide is written in the second person TO one of the two. A document
  addressed to somebody else is not a thing a third party should commission on
  their behalf, and the person it is addressed to would not know it exists.
* "Generate a pair guide for Sam and Jordan" is the shape of a manager building
  comparison dossiers on two reports. The standing rule for this feature is that
  the step from "get me the best team" to "rank my employees" must be
  structurally impossible rather than discouraged, and an endpoint that will
  describe any two colleagues to a third is that step with a form attached.
* It is not needed. A manager who wants the document asks one of the two to
  generate it, which also means one of the two knows it exists.

A support admin is not an exception. :func:`support_read_pair_guides` returns
row STATE — status, error, staleness, versions — and no prose, because the
question support gets asked is "why is it stuck", not "what does it say". A
support admin who genuinely needs the content reads both personal guides, which
is what ``can_view_raw_profile`` already grants them.

## Generation runs on cubie-worker

``POST /pair/{other_email}/generate`` writes a ``pending`` row, commits, and
dispatches :data:`COMPARISON_JOB_MODULE`; it returns 202 with the row id. Cloud
Run throttles the service CPU to near-zero once a response returns, so the one
Gemini call that gives the guide its voice would be starved in a
``BackgroundTask``. Same shape and same reasoning as
``routers/smart_teams_guides.py``, which documents the double-click behaviour
that applies here identically: ``uq_st_document_cache`` means both calls land on
the SAME row, so there is never a second document, but a second call against a
still-``pending`` row dispatches a second job that overwrites the same draft with
the same content.

## Staleness is per-subject and either subject triggers it

The cache identity is ``(kind, subject_key, profile_version_key)`` and the
version key holds BOTH people: ``"sam@…:2|jordan@…:1"``. When either of them
retakes the interview the key changes, the lookup misses, and the miss IS the
staleness signal — no JSON diff and no invalidation pass. The old row stays
findable by ``subject_key`` so this router can report ``stale: true`` and the UI
can offer Regenerate.

Note also that ``uploads.invalidate_stale_guides`` already sweeps pair rows on an
official-report merge; nothing here duplicates that.

## Never in the RAG corpus

``comparison.NO_RAG_REASON`` states it and there is no code path to change it.
The guide corpus is companywide and retrieval has no per-document ACL, so a pair
guide in it would be answerable in Chat to readers this router refuses.

## The company-wide gate, and why not the pilot gate

This router carries ``require_smart_teams_access`` on its own ``APIRouter``, the
same gate the interview, uploads, guides and skills routers carry, and NOT
``require_full_app_access``. ``backend/main.py`` used to anticipate the pilot gate
for ST-7; that anticipation was wrong for this surface, and the reason is
specific rather than general: half of every document here is about the reader.
Fencing it behind the copilot gate would tell a colleague outside the copilot
roster that a working-relationship guide describing them exists and
that they may not open it. The Team Builder (ST-9) is a different shape — a
staffing tool used ON other people — and the copilot gate is the right call there.

## Advisory only

Everything served here describes what two people said about how they prefer to
work, for the purpose of working together. It is never a measure of competence,
potential, or fitness for a role, nothing on this surface may be wired to
performance review, promotion, or compensation, and no response from it ranks or
grades either person against the other.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.auth import require_smart_teams_access
from backend.jobs.launcher import (
    reject_support_background_work,
    require_automations_http,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import User
from rubik_core.database.smart_teams import SmartTeamsDocument, SmartTeamsProfile
from rubik_core.smart_teams import authz, comparison, contracts, heatmap
from rubik_core.smart_teams import comparison_contracts as cc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/smart-teams/comparison",
    tags=["smart-teams"],
    dependencies=[Depends(require_smart_teams_access)],
)

#: The cubie-worker module that generates a pair guide. Named here, owned by
#: ``backend/jobs/smart_teams_comparison_job.py``. A constant so the dispatch and
#: the report of what was dispatched cannot drift.
COMPARISON_JOB_MODULE = "backend.jobs.smart_teams_comparison_job"

#: The advisory promise for a two-person document, imported verbatim from the
#: contract module rather than re-typed. It rides every response that carries
#: pair prose: a notice a page has to hardcode is a notice that can silently go
#: missing, and this one carries the sentence a reader is most likely to assume
#: the other way — that nobody watched these two people work.
ADVISORY_NOTICE = cc.PAIR_ADVISORY_NOTICE


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _display_name(db: Session, email: str) -> str:
    """The person's name for the title, or their local part.

    Folded compare because ``users.email`` is a case-sensitive primary key stored
    verbatim from the Google token. Falls back to the local part rather than to
    the raw address, and never blocks a guide over a missing row — the same
    helper shape ``routers/smart_teams_guides.py`` uses.
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
    False (``uq_st_profile_one_current``), so the filter is ``is_(True)``."""
    return (
        db.query(SmartTeamsProfile)
        .filter(func.lower(SmartTeamsProfile.user_email) == email)
        .filter(SmartTeamsProfile.is_current.is_(True))
        .order_by(SmartTeamsProfile.version.desc())
        .first()
    )


def _current_versions(db: Session, emails: list[str]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for email in emails:
        profile = _current_profile(db, email)
        out[email] = int(profile.version) if profile is not None else None
    return out


def _document_dict(
    document: SmartTeamsDocument,
    *,
    names: dict[str, str],
    current_versions: dict[str, int | None],
    include_body: bool,
) -> dict:
    """One pair row, as JSON.

    ``include_body`` is a flag rather than always-on for the reason the sibling
    router gives: the support read and any future status read want the metadata,
    and shipping the prose on every response makes it that much easier for a
    later caller to render a body it was not supposed to have.

    ``published`` is reported so a client can see it is false, and it always is —
    nothing in this feature writes it. ``rag_ingested`` likewise: a pair guide is
    never ingested, and a client that can see the field is false does not have to
    take that on trust.
    """
    emails = [part for part in (document.subject_key or "").split("|") if part]
    versions = document.profile_versions or {}
    return {
        "id": document.id,
        "kind": document.kind,
        "subject_emails": emails,
        "reader_email": emails[0] if emails else None,
        "about_email": emails[1] if len(emails) > 1 else None,
        "display_names": {email: names.get(email, email) for email in emails},
        "title": document.title,
        "status": document.status,
        "error": document.error,
        "profile_versions": {email: versions.get(email) for email in emails},
        "current_profile_versions": current_versions,
        "stale": comparison.is_stale_pair(document, current_versions),
        "published": bool(document.published),
        "rag_ingested": bool(document.rag_ingested),
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
        "advisory_notice": ADVISORY_NOTICE,
        **({"content_md": document.content_md} if include_body else {}),
    }


def _comparison_error(exc: comparison.ComparisonError) -> HTTPException:
    """A refusal from the library, as a 409.

    409 rather than 400 for the reason the sibling router gives: every
    ``ComparisonError`` a request can provoke is about the STATE of the row or
    the two profiles, not about a malformed request, and the detail string is
    written to be shown to the person.
    """
    return HTTPException(status_code=409, detail=str(exc))


def _resolve_pair(user: UserProfile, other_email: str) -> tuple[str, str]:
    """``(reader, about)``, both folded and both checked to be employees.

    The path value is a REQUEST, never an identity. The reader comes from
    ``get_current_user`` and from nowhere else, which is what makes "generate a
    pair guide for two other people" unsayable rather than merely refused.
    """
    reader = _fold(getattr(user, "email", None))
    about = _fold(other_email)
    if not authz.is_employee(reader):
        # Unreachable behind require_smart_teams_access, which uses the same
        # predicate. Kept because this function is also the belt for a future
        # caller that reaches it with something else.
        raise HTTPException(status_code=403, detail=authz.RAW_PROFILE_DENIED)
    if not authz.is_employee(about) or about == reader:
        raise HTTPException(
            status_code=404,
            detail="No working-relationship guide for that person.",
        )
    return reader, about


# ── Read ─────────────────────────────────────────────────────────────────────


@router.get("/pair/support/{reader_email}/{about_email}")
def support_read_pair_guides(
    reader_email: str,
    about_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Support-admins-only: every generation for one ORDERED pair, state without prose.

    ``can_view_raw_profile`` for BOTH people rather than
    ``can_view_multi_subject_guide``, and both halves of that matter:

    * It is the narrowest gate in the app — admin AND the support row, with an exec and a
      team manager both explicitly denied — and this endpoint returns failed rows
      and error strings, which is support state rather than a guide.
    * Requiring it for both subjects means support access to one person does not
      become support access to a document about somebody else.

    Bodies are omitted. The question this answers is "why is their pair guide
    stuck", and a support admin who needs the words reads the two personal
    guides, which ``can_view_raw_profile`` already grants.

    Declared BEFORE :func:`get_pair_guide` and with three path segments, so
    ``/pair/support/...`` can never be captured as a colleague's address.
    """
    reader = _fold(reader_email)
    about = _fold(about_email)
    for subject in (reader, about):
        if not authz.can_view_raw_profile(user, subject):
            raise HTTPException(status_code=403, detail=authz.RAW_PROFILE_DENIED)

    versions = _current_versions(db, [reader, about])
    names = {email: _display_name(db, email) for email in (reader, about)}
    subject_key = f"{reader}|{about}"
    rows = (
        db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.kind == comparison.KIND_PAIR,
            func.lower(SmartTeamsDocument.subject_key) == subject_key,
        )
        .order_by(SmartTeamsDocument.created_at.desc())
        .all()
    )
    logger.info(
        "[smart-teams] support read of the %s pair guide rows by %s",
        subject_key,
        _fold(user.email),
    )
    return {
        "reader_email": reader,
        "about_email": about,
        "display_names": names,
        "current_profile_versions": versions,
        "published_personal_guides": comparison.published_by_subject(
            db, [reader, about]
        ),
        "guides": [
            _document_dict(
                row, names=names, current_versions=versions, include_body=False
            )
            for row in rows
        ],
    }


@router.get("/pair/{other_email}")
def get_pair_guide(
    other_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The pair guide written TO the caller ABOUT ``other_email``.

    Always a 200 when the pair is readable. No guide is a state (``"none"``), not
    a 404: this is the read the page loads with, and a 404 for the ordinary case
    of "nobody has generated one yet" makes every client treat an error as
    normal.

    The consent gate runs BEFORE the row is looked at, and it runs even when the
    answer is going to be "none". Gating only the body would let anybody discover
    which pairs have documents, and a colleague's participation in one is not
    ours to disclose.

    Reading the flipped pair means reading a different document: ``(A, B)`` and
    ``(B, A)`` are separate rows, and this endpoint only ever returns the one
    addressed to the caller. There is no endpoint that returns the copy written
    to the other person, deliberately — it is written in the second person to
    them.
    """
    reader, about = _resolve_pair(user, other_email)
    if not comparison.can_read_pair(db, user, [reader, about]):
        raise HTTPException(
            status_code=403, detail=authz.SHARED_GUIDE_NOT_PUBLISHED_DENIED
        )

    versions = _current_versions(db, [reader, about])
    names = {email: _display_name(db, email) for email in (reader, about)}
    row = comparison.current_pair_guide(db, reader, about)
    can_generate = comparison.refuse_generation(db, [reader, about]) is None
    if row is None:
        return {
            "state": "none",
            "guide": None,
            "current_profile_versions": versions,
            "display_names": names,
            "can_generate": can_generate
            and all(version is not None for version in versions.values()),
            "advisory_notice": ADVISORY_NOTICE,
        }
    return {
        "state": row.status,
        "guide": _document_dict(
            row, names=names, current_versions=versions, include_body=True
        ),
        "current_profile_versions": versions,
        "display_names": names,
        "can_generate": can_generate
        and all(version is not None for version in versions.values()),
        "advisory_notice": ADVISORY_NOTICE,
    }


# ── The overlap grid ─────────────────────────────────────────────────────────


@router.get("/pair/{other_email}/heatmap")
def get_pair_heatmap(
    other_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Where the caller and one colleague line up, and where they pull apart.

    The STRICTER of the two pair gates — ``refuse_generation``, not
    ``can_read_pair`` — and the choice is deliberate.

    Reading a pair GUIDE is allowed without the reader having published anything,
    because ``authz.can_view_multi_subject_guide`` exempts the viewer's own
    publish state: consent is something you give others, not something you need
    in order to read about yourself. That is right for re-reading a document, and
    it is safe there because the document could only have been generated when
    both people had published.

    A grid has no such history. It is computed live from two stored profiles, so
    the loose gate would newly describe a colleague who published to a reader who
    published nothing — "asymmetric in exactly the direction that feels unfair to
    the person described", which is ``refuse_generation``'s own reasoning for
    declining the same shortcut. The refusal copy is identical either way
    (``SHARED_GUIDE_NOT_PUBLISHED_DENIED``), so the page says the same sentence.

    It deliberately does NOT require a generated pair guide to exist: the grid
    answers on a pair nobody has spent an LLM call on yet, which is exactly when
    somebody is deciding whether to ask for one.

    Bands only for the colleague, always. ``can_view_raw_profile`` is asked
    separately for each side, which on the ordinary path means the caller sees
    their own intensities and never the other person's — the asymmetry the two
    flags on ``heatmap.pair_rows`` exist for. A support admin supporting the pair
    passes it for both and sees both, which is the same access they already have
    through the support reads.

    Motivators are absent by construction (``heatmap.CROSS_PERSON_FRAMEWORKS``),
    and that is a measurement rule rather than a privacy one: ``emphasis`` is
    within-person, so two people's motivator numbers are not on one scale.
    """
    reader, about = _resolve_pair(user, other_email)
    refusal = comparison.refuse_generation(db, [reader, about])
    if refusal is not None:
        raise HTTPException(status_code=403, detail=refusal)

    profiles = {email: _current_profile(db, email) for email in (reader, about)}
    envelopes = {
        email: (
            heatmap.envelopes_of(profile)
            if profile is not None
            else {f: contracts.not_taken_envelope(f) for f in contracts.FRAMEWORKS}
        )
        for email, profile in profiles.items()
    }

    rows = heatmap.pair_rows(
        reader,
        envelopes[reader],
        about,
        envelopes[about],
        include_reader_scores=authz.can_view_raw_profile(user, reader),
        include_about_scores=authz.can_view_raw_profile(user, about),
    )
    logger.info(
        "[smart-teams] pair heatmap read for (%s, %s) by %s",
        reader,
        about,
        _fold(user.email),
    )
    return {
        "reader_email": reader,
        "about_email": about,
        "display_names": {email: _display_name(db, email) for email in (reader, about)},
        "rows": [row.as_dict() for row in rows],
        # Both dots for the pair map. A style key is what the subject's own
        # published guide already says about them, so this exposes no new fact —
        # it just puts the two of them on one picture.
        "markers": {
            email: heatmap.map_marker(envelopes[email]) for email in (reader, about)
        },
        "current_profile_versions": _current_versions(db, [reader, about]),
        # Both must be scored for any row to be comparable; said plainly so the
        # page can distinguish "no overlap found" from "we could not look".
        "both_scored": all(profile is not None for profile in profiles.values()),
        "advisory_notice": ADVISORY_NOTICE,
    }


# ── Generate ─────────────────────────────────────────────────────────────────


@router.post(
    "/pair/{other_email}/generate",
    status_code=202,
    dependencies=[Depends(reject_support_background_work)],
)
def generate_pair_guide(
    other_email: str,
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Generate (or regenerate) the pair guide written to the caller about a
    colleague.

    The requester is always one of the two — there is no parameter for the
    reader. See the module docstring for why that is the answer to "may one
    employee generate a pair guide for two other people".

    ``comparison.refuse_generation`` is the consent gate and it is STRICTER than
    the read gate: **both** people, the requester included, must have published
    their own personal guide first. Generating writes a colleague's profile
    content into a cached row that a third colleague may later read, and the only
    consent we hold for describing somebody company-wide is that they published
    their own guide. Its docstring carries the full argument, including why the
    looser "only the other person must have published" rule was declined.

    Regeneration is not a separate endpoint: the cache key holds both profile
    versions, so calling this after either person retakes lands on a NEW row and
    calling it twice on the same versions lands on the same one. That is also the
    staleness fix — the stale flag on the old row is what the UI shows a
    Regenerate button for.
    """
    require_automations_http()
    reader, about = _resolve_pair(user, other_email)

    refusal = comparison.refuse_generation(db, [reader, about])
    if refusal is not None:
        raise HTTPException(status_code=403, detail=refusal)

    profiles = {}
    for email in (reader, about):
        profile = _current_profile(db, email)
        if profile is None:
            # Reachable in principle: a published personal guide plus no current
            # profile row means somebody's profile was retired underneath it.
            raise HTTPException(
                status_code=409,
                detail=(
                    "One of you does not have a scored profile right now, so "
                    "there is nothing to write from."
                ),
            )
        profiles[email] = profile

    try:
        row = comparison.ensure_pair_row(db, profiles[reader], profiles[about])
    except comparison.ComparisonError as exc:
        raise _comparison_error(exc) from None

    document = row.document
    versions = {
        reader: int(profiles[reader].version),
        about: int(profiles[about].version),
    }
    names = {email: _display_name(db, email) for email in (reader, about)}

    if not row.created and document.status == "ready":
        # The cache hit. This row was built from the profile versions both people
        # still have, so there is nothing to generate.
        db.commit()
        return {
            "enqueued": False,
            "reason": "cached",
            "guide": _document_dict(
                document,
                names=names,
                current_versions=dict(versions),
                include_body=True,
            ),
            "advisory_notice": ADVISORY_NOTICE,
        }

    document.status = "pending"
    document.error = None
    document_id = document.id
    db.commit()

    from backend.jobs.launcher import submit_background_work

    args = ["--document-id", document_id, "--generate"]
    executor, execution_name = submit_background_work(
        background_tasks,
        module=COMPARISON_JOB_MODULE,
        args=args,
        fallback=lambda: _run_job_inline(args),
        label=f"smart-teams-comparison:{reader}|{about}",
    )
    logger.info(
        "[smart-teams] pair guide %s for %s|%s dispatched via %s (%s)",
        document_id,
        reader,
        about,
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
            document, names=names, current_versions=dict(versions), include_body=False
        ),
        "advisory_notice": ADVISORY_NOTICE,
    }


def _run_job_inline(args: list[str]) -> None:
    """Off-GCP fallback for the pair dispatch.

    ``submit_background_work`` reaches this only when ``APP_MODE`` is not GCP
    (local development) or when the Cloud Run Job launch itself failed — and in
    the second case it has already logged that loudly. Calls the job's own
    ``main`` so the local path and the deployed path run identical code rather
    than a second, thinner implementation of it that can drift.
    """
    try:
        from backend.jobs import smart_teams_comparison_job

        code = smart_teams_comparison_job.main(args)
    except Exception:  # noqa: BLE001 — a fallback must not take the request down
        logger.exception("[smart-teams] inline pair-guide job failed for args %s", args)
        return
    if code:
        logger.error(
            "[smart-teams] inline pair-guide job exited %s for args %s — the row "
            "carries the failure",
            code,
            args,
        )


# ── No approve, no unpublish, no export, no Save-to-Assets ───────────────────
#
# Not omissions. Each one is a decision, and the reasoning lives next to the
# absence rather than in a review thread:
#
# * **No approve / publish.** ``st_documents.published`` is one boolean and a
#   pair guide has two subjects, so the flag cannot record the consent that
#   matters. ``comparison.approve_pair`` raises and says so. The consent that
#   makes a pair guide readable is each person having published their OWN guide,
#   which each of them controls alone and can withdraw alone — and an unpublish
#   by either of them takes this document out of every third party's reach
#   without anybody having to remember it exists.
# * **No unpublish.** There is nothing to retract: ``published`` is never set,
#   and the pair guide is never in the RAG corpus (``comparison.NO_RAG_REASON``).
#   The retraction path is ``POST /api/smart-teams/guides/{id}/unpublish`` on
#   either person's personal guide, which closes this document to everybody
#   except the two of them.
# * **No .docx export.** ``guide.export_docx`` renders from the stored
#   ``content_md`` and ``comparison.render_markdown`` deliberately emits the same
#   five markup shapes, so adding one later is small. It is not here because a
#   downloaded file outlives consent — the person who unpublishes their personal
#   guide cannot recall a .docx a colleague already has — and that trade is worth
#   a deliberate decision rather than arriving as a convenience.
# * **No Save-to-Assets**, for the reason ST-5 and ST-6 both wrote down: an Asset
#   is governed by ``has_document_access``, whose rules 1 and 1b return True for
#   ``is_admin`` and ``sees_all_departments`` before any stamp is read. Those are
#   exactly the readers ``can_view_multi_subject_guide`` refuses, and an Asset row
#   is not something a later unpublish can retract.
