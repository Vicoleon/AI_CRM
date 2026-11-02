"""Smart Teams — the skills a person can be asked about (ST-8).

Its own router rather than more endpoints on ``backend/routers/smart_teams.py``,
for the reason that file gives for existing at all: that one is the Work Style
Interview, a thing you sit down and finish once. This one is a small, ongoing
surface — tick a skill, answer a suggestion, look somebody up before you staff a
project — and the two have different shapes and different lifetimes.

Mounted on ``app`` beside the interview, uploads and guide routers, NOT on the
pilot-gated ``core`` parent, and carrying ``require_smart_teams_access`` on the
``APIRouter`` itself so no endpoint can be added without it. Same reasoning as
its siblings: a skills directory is only worth having if the whole company is in
it, and a copilot mount would leave most of the company readable but unable to declare
anything about themselves.

**Only you can change your own skills.** Every write below takes its subject
from ``user.email`` — the authenticated principal — and no endpoint reads an
email out of a body, a header or a query string. That is not a convention here:
it is why :data:`~rubik_core.smart_teams.skills.SUBJECT_ONLY_DENIED` exists as
copy next to the rule. A confirm is a statement about yourself, and an endpoint
that accepted ``{"user_email": ...}`` would be a primitive for making that
statement on a colleague's behalf. The two endpoints that name somebody else in
the PATH are both reads, and each hands that value to an ``authz`` predicate
rather than trusting it:

* the colleague read (``GET /people/{subject_email}``) returns only what that
  person has CONFIRMED, through the evidence-free
  :class:`~rubik_core.smart_teams.skills.ConfirmedSkill` DTO; and
* the support read (``GET /support/{subject_email}``) goes through
  ``authz.can_view_raw_profile`` — the person themselves plus a support admin,
  and nobody else, not a manager and not an exec.

**Evidence is not colleague-visible, and that is enforced by the type.** A
suggestion's citations are meeting transcripts and document names gathered as
the SUBJECT, so none of it has passed ``meeting_participants`` or a document ACL
on the way to a third party's screen. ``confirmed_skills_for``
therefore returns DTOs with no evidence field at all rather than ORM rows a
serializer is trusted to strip — see the "WITHOUT evidence is a TYPE" section of
``rubik_core/smart_teams/skills.py``.

**Internal signals only, and nothing here can change that.** Every suggestion
this router shows or re-requests comes from
``rubik_core/smart_teams/skill_inference.py``, which reads the company's own systems
only: meetings the person was in and documents they uploaded. No endpoint below can widen that, because none of them names a
source — ``POST /refresh`` passes one email and nothing else. The boundary is
structural rather than a policy line: ``rubik_core/people/`` (UC-47) does web
research and deliberately SKIPS anybody on the company's own domain, and pointing that
kind of pipeline at an employee would produce a dossier sourced from pages the
subject cannot correct, filed under the same heading as a claim they made
themselves. Nothing in this module, this router's job, or the three modules
behind it imports that package or makes an outbound request.

**The sweep runs on cubie-worker, never here.** It reads transcripts and makes
one Vertex call per person; Cloud Run throttles
this process's CPU to near-zero the moment the response returns, so doing it in
a ``BackgroundTask`` would starve it silently. ``POST /refresh`` commits nothing
and dispatches :data:`SWEEP_JOB_MODULE` for the caller alone.

ADVISORY ONLY. A skill row says what somebody may be asked about on a project.
Nothing here is a measure of competence, potential, promotion-readiness or
fitness for a role, and nothing here may be joined to a review or comp surface.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import require_smart_teams_access
from backend.jobs.launcher import reject_support_background_work
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.profile import company_name_for_copy
from rubik_core.database.db import get_db
from rubik_core.database.smart_teams import SKILL_LEVELS, SmartTeamsSkill
from rubik_core.smart_teams import skill_taxonomy as taxonomy
from rubik_core.smart_teams import skills as store
from rubik_core.smart_teams import team_builder
from rubik_core.smart_teams.authz import (
    RAW_PROFILE_DENIED,
    can_curate_skill_taxonomy,
    can_view_raw_profile,
    is_employee,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/smart-teams/skills",
    tags=["smart-teams"],
    dependencies=[Depends(require_smart_teams_access)],
)

#: The cubie-worker module that runs the inference sweep. A constant so the
#: dispatch call and the report of what was dispatched cannot drift, and
#: registered in ``backend/jobs/__init__.py``'s ``WORKER_JOBS`` so the command to
#: run it by hand is not only in a docstring. A launch of a module that is not in
#: the image returns 200 and does nothing, which is how a 202-that-does-nothing
#: shipped once before.
SWEEP_JOB_MODULE = "backend.jobs.smart_teams_skill_sweep_job"

#: Said on every read of this section. Skills are the part of Smart Teams most
#: likely to be mistaken for an assessment, so the disclaimer travels with the
#: payload rather than living in a UI string somebody can forget to render.
ADVISORY_NOTICE = (
    "Skills say what you can be asked about on a project. They are not a "
    "rating, a review, or a measure of how good you are at your job, and "
    "nothing here feeds performance review, promotion, or pay."
)


def suggestion_notice(company: str) -> str:
    """Shown with a pending suggestion, because "where did this come from?" is
    the first thing anybody asks and the answer bounds what the suggestion
    means. ``company`` is the workspace's company name, or "your company"
    (:func:`rubik_core.company.company_name_for_copy`)."""
    return (
        f"{PRODUCT_NAME} suggested these from your own internal activity — "
        f"meetings you were in and documents you uploaded. Nothing outside "
        f"{company} was looked at. Each one shows what it was based on, and "
        "nobody else can see a suggestion until you confirm it."
    )


#: Shown with the denied bucket, so a denial reads as a decision that stuck
#: rather than as data waiting to come back.
DENIAL_NOTICE = (
    f"You said no to these, so {PRODUCT_NAME} will not suggest them again. They are "
    "listed only so you can change your mind — no colleague can see them."
)


class DeclareRequest(BaseModel):
    """Skills the caller is claiming about THEMSELVES.

    ``skills`` maps a curated ``skill_key`` to a level (or ``null`` for "I do
    this, at no stated level"). There is no subject field and there never may be
    one: the subject is the authenticated caller. A level outside
    ``SKILL_LEVELS`` and a key outside the ACTIVE taxonomy are both refusals
    rather than coercions — see :func:`declare_own_skills`.
    """

    skills: dict[str, str | None] = Field(default_factory=dict)


#: How many skills one ``/holders`` call may ask about. Each one is a fan-out
#: over the roster, and a lead who genuinely needs eight distinct competences is
#: describing a team rather than looking somebody up — that is the Team Builder's
#: question, and it answers it better.
MAX_HOLDER_TERMS = 6


def _fold(email: str | None) -> str:
    """Fold an address for comparison. ``users.email`` is stored verbatim from
    the Google token, so every Smart Teams surface folds — see
    ``database/smart_teams.py``'s "Email case" note."""
    return (email or "").strip().lower()


def _taxonomy_dict(row) -> dict:
    return {
        "skill_key": row.skill_key,
        "name": row.name,
        "cluster": row.cluster,
        "aliases": list(row.aliases or []),
    }


def _own_payload(db, email: str) -> dict:
    """The caller's own skills, in the four buckets a person actually thinks in.

    ``declared`` and ``accepted_suggestions`` are both confirmed and both
    colleague-visible, and they are split because they mean different things to
    the person looking at them: one they typed, one Cubie guessed and they agreed
    to. ``store.bucketed_for`` returns them in one bucket because
    ``confirmed.is_(True)`` is the only correct read predicate; the split here is
    presentation over ``origin``, not a second notion of confirmed.

    Every row is serialized with ``private_dict``, so the evidence IS included —
    this endpoint only ever runs for the subject themselves.
    """
    buckets = store.bucketed_for(db, email)
    confirmed = [store.private_dict(row, tax) for row, tax in buckets.confirmed]
    return {
        "user_email": email,
        "declared": [s for s in confirmed if s["origin"] != store.SOURCE_INFERRED],
        "accepted_suggestions": [
            s for s in confirmed if s["origin"] == store.SOURCE_INFERRED
        ],
        "suggested": [store.private_dict(row, tax) for row, tax in buckets.suggested],
        "denied": [store.private_dict(row, tax) for row, tax in buckets.denied],
        "levels": list(SKILL_LEVELS),
        "advisory_notice": ADVISORY_NOTICE,
        "suggestion_notice": suggestion_notice(company_name_for_copy(db)),
        "denial_notice": DENIAL_NOTICE,
    }


# ── Literal paths first ───────────────────────────────────────────────────────
# Declared before ``/{skill_key}/…`` so a future GET on a single skill cannot
# shadow one of these. FastAPI matches in declaration order.


@router.get("")
def list_own_skills(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller's own skills — declared, accepted, pending, denied.

    Evidence included, because the audience is the subject. Four empty buckets
    is an ordinary, complete answer for somebody who has declared nothing and
    whom no sweep has reached yet: nothing in the payload is shaped like a
    to-do.
    """
    return _own_payload(db, _fold(user.email))


@router.get("/taxonomy")
def search_taxonomy(
    q: str | None = Query(default=None, description="Free text: name or alias."),
    cluster: str | None = Query(default=None, description="Restrict to one cluster."),
    limit: int = Query(default=200, ge=1, le=500),
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The curated vocabulary, for the picker's search box.

    Matched against name AND stored aliases, so "gdpr" finds "Data privacy
    (GDPR / CCPA)" — that is the whole point of keeping aliases.

    ``seeded`` is reported rather than assumed. The taxonomy table is filled by
    ``skill_taxonomy.seed_taxonomy``, which runs at the top of every sweep, and
    until the first seed lands this endpoint honestly returns an empty list. A
    UI showing "no skills match" and a UI showing "the list has not been built
    yet" are different screens, and the caller cannot tell them apart from
    ``[]`` alone.
    """
    rows = taxonomy.search(db, query=q, cluster=cluster, limit=limit)
    return {
        "skills": [_taxonomy_dict(row) for row in rows],
        "clusters": list(taxonomy.clusters_present()),
        "levels": list(SKILL_LEVELS),
        "seeded": bool(taxonomy.keys_in_db(db, active_only=False)),
        "advisory_notice": ADVISORY_NOTICE,
    }


@router.post("/declare")
def declare_own_skills(
    body: DeclareRequest,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller states skills about themselves. ``confirmed`` by definition.

    400 for a key outside the ACTIVE taxonomy, with
    :data:`~rubik_core.smart_teams.skills.UNKNOWN_SKILL_DENIED` verbatim. That
    copy names the path forward (ask an admin to add it, and it becomes
    declarable for everyone at once) because the alternative shapes are both
    worse: storing a free-text skill gives somebody a claim the Team Builder can
    never match on, and coercing it onto the nearest curated key attributes work
    they never touched. ``record_declared_skills`` validates the whole batch
    before writing any of it, so a request with one bad key changes nothing.

    Declaring a skill you previously DENIED lifts that tombstone — an explicit
    statement about yourself beats an earlier rejection of somebody else's guess
    — and the response says so in ``undenied`` rather than reversing it
    silently.
    """
    if not body.skills:
        raise HTTPException(status_code=400, detail="No skills were sent.")
    try:
        result = store.record_declared_skills(db, user.email, body.skills)
    except store.SkillError as exc:
        # Every SkillError from this path is a sentence written for the person
        # (an unknown key, a level that is not a level), so it is safe and
        # useful to return verbatim.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {**result.to_dict(), **_own_payload(db, _fold(user.email))}


@router.post(
    "/refresh", status_code=202, dependencies=[Depends(reject_support_background_work)]
)
def refresh_own_suggestions(
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Ask for a fresh look at the caller's OWN internal activity.

    202, and scoped to one person: ``--emails <caller>``. Nobody can start a
    sweep of somebody else from here, and a company-wide pass is the scheduled
    job's business, not an endpoint's.

    Costs nothing when there is nothing new. The sweep skips a person whose
    evidence corpus is byte-identical to the last run, so pressing this twice
    makes one Vertex call at most — and if the nightly execution is running, the
    run lock makes this a no-op that says so in its own execution log rather
    than a second bill for the same answer.

    Suggestions the caller already denied cannot come back: ``build_candidates``
    drops them before the model sees them and ``upsert_inference`` refuses the
    write, so this button is safe to press after saying no to something.
    """
    from backend.jobs.launcher import submit_background_work

    email = _fold(user.email)
    executor, execution_name = submit_background_work(
        background_tasks,
        module=SWEEP_JOB_MODULE,
        args=["--emails", email],
        fallback=lambda: _sweep_inline(email),
        label=f"smart-teams-skill-sweep:{email}",
    )
    logger.info(
        "[smart-teams][skills] self-refresh for %s dispatched via %s (%s)",
        email,
        executor,
        execution_name,
    )
    return {
        "status": "queued",
        "user_email": email,
        "job_module": SWEEP_JOB_MODULE,
        "executor": executor,
        "execution_name": execution_name,
        "suggestion_notice": suggestion_notice(company_name_for_copy(db)),
        "advisory_notice": ADVISORY_NOTICE,
    }


def _sweep_inline(email: str) -> None:
    """Off-GCP fallback for the sweep dispatch. Never runs on the deployed app.

    ``submit_background_work`` reaches this only when ``APP_MODE`` is not GCP
    (local development) or when the Cloud Run Job launch itself failed, and in
    the second case it has already logged that loudly. ``seed=False``: seeding
    the taxonomy is the scheduled job's opening move and does not belong in a
    request-scoped fallback that exists to make local development work.
    """
    from rubik_core.smart_teams.skill_inference import run_sweep

    try:
        run_sweep(emails=[email], seed=False)
    except Exception:  # noqa: BLE001 — a fallback must not take the request down
        logger.exception("[smart-teams][skills] inline sweep failed for %s", email)


@router.get("/holders")
def find_people_by_skill(
    skills: str = Query(
        ...,
        description=(
            "Comma-separated skill keys or everyday names. Names are resolved "
            "through the taxonomy's aliases, so 'ccpa' finds data_privacy."
        ),
    ),
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Skills to PEOPLE — "find me whoever confirmed these" — the inverse of
    ``/people/{subject_email}``.

    This is the direction a project lead actually asks in. The existing read
    answers "what has Sam confirmed", which only helps once you already suspect
    Sam; staffing starts from the work, not from a name you have to guess.

    Safe on the same two narrowings as the person read, and it matters more here
    because this one fans out over the roster instead of one colleague:

    * ``confirmed.is_(True)`` — only what somebody said about themselves. An
      unconfirmed suggestion is a question Cubie asked, and a denial sits in the
      same ``confirmed = False`` state, so one predicate excludes both. Cubie
      guessing that a person knows something is never a reason to put their name
      in front of a lead.
    * :class:`~rubik_core.smart_teams.skills.SkillHolder` DTOs of primitives, so
      the evidence — ACL'd meeting titles, transcript excerpts, document names
      gathered as the SUBJECT — is not on the object this endpoint serializes.

    Bounded by ``proposable_people``, the codebase's documented "current
    employee" rule, because ``st_person_skills`` is not cleared when somebody is
    offboarded: without it an ex-colleague keeps surfacing for live work.

    Holders come back sorted by name. Not by level, not by recency, not by how
    many of the asked-for skills they hold — any of those would render as a
    ranking of people, and this feature does not rank people. A lead reads the
    list and decides.

    Unresolved terms are REPORTED rather than dropped, so "nobody has this" and
    "we do not track this" stay different answers.
    """
    requested = [term.strip() for term in (skills or "").split(",") if term.strip()]
    if not requested:
        raise HTTPException(status_code=400, detail="Name at least one skill.")
    if len(requested) > MAX_HOLDER_TERMS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Ask about at most {MAX_HOLDER_TERMS} skills at once — a longer "
                "list reads the whole roster for each one."
            ),
        )

    # One vocabulary for the whole call, and one that includes admin-added
    # skills: an admin who curates a skill has to be able to search for it.
    vocab = taxonomy.vocabulary_with_db_skills(db)
    resolved, unknown = taxonomy.resolve_terms(vocab, requested)
    roster = team_builder.proposable_people(db)
    holders = store.holders_of(db, list(resolved.values()), roster=roster)

    return {
        "asked": requested,
        "matched": [
            {
                "term": term,
                "skill_key": key,
                "name": (spec.name if (spec := vocab.specs.get(key)) else key),
                "holders": [h.public_dict() for h in holders.get(key, ())],
            }
            for term, key in resolved.items()
        ],
        "unknown": unknown,
        "advisory_notice": ADVISORY_NOTICE,
        "scope_notice": (
            "These are people who confirmed the skill about themselves. "
            "Suggestions nobody has answered are not shown, what any suggestion "
            "was based on is private to them, and this is not a ranking — it is "
            "everyone who said yes, in name order."
        ),
    }


@router.get("/people/{subject_email}")
def read_colleague_skills(
    subject_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """One colleague's CONFIRMED skills, for staffing. No evidence, ever.

    This is the read ST-9's Team Builder is built on, and the only Smart Teams
    read one employee may run against another without a further gate. What makes
    that safe is the pair of narrowings underneath it:

    * ``confirmed.is_(True)`` — a pending suggestion is a question Cubie asked,
      not a fact, and a denial lives in the same ``confirmed = False`` state, so
      one predicate excludes both; and
    * the return type. ``confirmed_skills_for`` yields
      :class:`~rubik_core.smart_teams.skills.ConfirmedSkill` DTOs of primitives,
      so the evidence — ACL'd meeting titles and transcript excerpts collected as
      the SUBJECT — is not on the object this endpoint serializes.

    404 for a subject who is not a company employee. A confirmed skill needs no
    per-subject permission check beyond that: the confirmation IS the consent,
    the same way ``approved_at`` is for a guide.
    """
    subject = _fold(subject_email)
    if not is_employee(subject):
        raise HTTPException(status_code=404, detail="No such person.")
    confirmed = store.confirmed_skills_for(db, [subject]).get(subject, ())
    return {
        "subject_email": subject,
        "skills": [store.public_dict(skill) for skill in confirmed],
        "advisory_notice": ADVISORY_NOTICE,
        # Said out loud so a reader does not mistake a short list for a complete
        # picture of somebody, and does not go looking for the evidence.
        "scope_notice": (
            "These are the skills this person has confirmed about themselves. "
            "Suggestions they have not answered are not shown, and what any "
            "suggestion was based on is private to them."
        ),
    }


@router.get("/support/{subject_email}")
def support_read_skills(
    subject_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """One person's full skills, for a support admin debugging with them.

    The path value names the SUBJECT and is never trusted as the caller:
    ``authz.can_view_raw_profile`` decides, and it grants exactly two viewers —
    the person themselves and a support admin. An exec is refused, a team
    manager is refused, and an admin outside the support set is refused. The refusal
    copy comes from that module too, so every surface says the same thing.

    Read-only on purpose. A support admin can see why a suggestion was made and
    re-run the sweep; they cannot confirm, deny or declare on somebody's behalf.
    A confirmation is a statement about yourself, and support is not consent.
    """
    subject = _fold(subject_email)
    if not can_view_raw_profile(user, subject):
        raise HTTPException(status_code=403, detail=RAW_PROFILE_DENIED)
    # Audited: the evidence on an inferred skill is meeting titles and
    # document names gathered AS the subject, so this read hands over
    # citations that never passed the reader's own ACLs.
    logger.info(
        "[smart-teams][skills] support read of %s's skills by %s",
        subject,
        _fold(user.email),
    )
    return {**_own_payload(db, subject), "sweep_job_module": SWEEP_JOB_MODULE}


# ── Per-skill writes ──────────────────────────────────────────────────────────
# Always about the CALLER's own row. ``skill_key`` in the path selects which of
# their skills, never whose.


@router.post("/{skill_key}/confirm")
def confirm_own_skill(
    skill_key: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller says yes to a suggestion about themselves. Idempotent.

    404 when they have no row for that skill — a confirm is an answer to a
    question Cubie asked them, so there is nothing to answer. Note what this
    does NOT do: it never creates a row, because a confirm that could create one
    would be a declaration wearing the wrong verb (and would let somebody
    "confirm" a skill nothing ever suggested). ``POST /declare`` is that path.

    ``source`` stays ``"inferred"`` afterwards, deliberately — the row's history
    is that Cubie suggested it and the person agreed, and rewriting the
    provenance on confirmation would erase the only record of where it came
    from. ``confirmed`` is what every read switches on.
    """
    try:
        row = store.confirm(db, user.email, skill_key)
    except store.SkillError as exc:
        logger.info(
            "[smart-teams][skills] confirm refused for %s/%s: %s",
            _fold(user.email),
            skill_key,
            exc,
        )
        raise HTTPException(
            status_code=404, detail="You have no suggestion for that skill."
        ) from None
    return {
        "skill": store.private_dict(row, db.get(SmartTeamsSkill, row.skill_key)),
        **_own_payload(db, _fold(user.email)),
    }


@router.post("/{skill_key}/deny")
def deny_own_skill(
    skill_key: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller says no. The refusal STICKS across every later sweep.

    A denial is a tombstone, not a delete: the ``(person, skill)`` row stays
    with ``confirmed = False``, ``level`` NULL and a marker in its evidence
    envelope, and both ``build_candidates`` and ``upsert_inference`` read that
    marker. A delete would let the next sweep read the same meeting, find
    nothing on file, and ask again — and a suggestion that keeps coming back is
    the fastest way to get this feature switched off.

    Also the retraction path for a skill the caller DECLARED, so there is one
    mechanism rather than a second "remove" a sweep would not know about.

    ``denied_by`` is the authenticated caller, never a body field. 400 for a key
    that is not in the taxonomy at all (active or retired) — ``st_person_skills``
    holds a RESTRICT foreign key, so there is nothing to tombstone against.
    Denying twice is a no-op, not an error.
    """
    try:
        row = store.deny(db, user.email, skill_key, denied_by=user.email)
    except store.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "skill": store.private_dict(row, db.get(SmartTeamsSkill, row.skill_key)),
        "denial_notice": DENIAL_NOTICE,
        **_own_payload(db, _fold(user.email)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Curation: adding a skill the shipped list does not have
# ─────────────────────────────────────────────────────────────────────────────
#
# The seed list is authored in ``skill_taxonomy.py`` and released with the app,
# which is right for a vocabulary the whole company shares — and too slow for the
# case that actually keeps coming up: somebody needs one more skill now, and it
# does not justify a deploy.
#
# ``seed_taxonomy`` already made this safe: it never touches a row the module does
# not ship, so an admin row survives every subsequent seed. What was missing was a
# way to create one, and — the part that would have made the feature half-real —
# any way for the INFERENCE side to see it. See ``vocabulary_with_db_skills``.
#
# Retirement is ``is_active = False`` and there is deliberately no delete:
# ``st_person_skills`` holds a RESTRICT foreign key, so deleting a skill people
# have claimed would either fail or erase their history.

CURATION_DENIED = (
    "Curating the skill list is limited to the admins who support Smart Teams. "
    "It is a company-wide "
    "vocabulary, and a skill somebody has claimed can be retired but never "
    "deleted."
)


class NewSkill(BaseModel):
    skill_key: str = Field(..., max_length=64)
    name: str = Field(..., max_length=120)
    cluster: str = Field(..., max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=24)


class SkillEdit(BaseModel):
    """Every field optional: this is a PATCH, and absent means unchanged."""

    name: str | None = Field(default=None, max_length=120)
    cluster: str | None = Field(default=None, max_length=80)
    aliases: list[str] | None = Field(default=None, max_length=24)
    is_active: bool | None = None


def _require_curator(user: UserProfile) -> None:
    if not can_curate_skill_taxonomy(user):
        raise HTTPException(status_code=403, detail=CURATION_DENIED)


@router.get("/admin/taxonomy")
def list_taxonomy_for_curation(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Every skill, ACTIVE OR RETIRED, with what an admin needs to decide.

    Retired rows are included on purpose: the alternative is an admin trying to
    add a skill that already exists in a retired state, getting a duplicate-key
    refusal, and having no way to see why.

    ``in_use`` counts confirmed AND unconfirmed claims, because both block the
    only thing an admin might want after retiring: the row cannot go away.
    """
    _require_curator(user)
    rows = db.query(SmartTeamsSkill).order_by(SmartTeamsSkill.skill_key).all()
    counts = store.claim_counts(db)
    out = []
    for row in rows:
        entry = {
            **_taxonomy_dict(row),
            "is_active": bool(row.is_active),
            "managed_in_code": row.skill_key in taxonomy.SKILLS,
            "claims": counts.get(row.skill_key, 0),
        }
        out.append(entry)
    return {
        "clusters": list(taxonomy.CLUSTERS),
        "skills": out,
        "curation_notice": (
            "Skills added here survive every release — the seeder never touches "
            "a row the app does not ship. They can be retired but never deleted, "
            "because somebody may have claimed them."
        ),
        "advisory_notice": ADVISORY_NOTICE,
    }


@router.post("/admin/taxonomy", status_code=201)
def create_curated_skill(
    body: NewSkill,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Add one skill to the company vocabulary.

    Validated against every rule the shipped list obeys — slug shape, a known
    cluster, no alias that already means something else, and the advisory screen
    on the name and every alias. All problems come back at once rather than the
    first one, so filling in the form is not four round trips.

    A key that already exists is a 409 whether it is active or retired: the
    retired row is the thing to reactivate, and creating a second row for the
    same competence is how the vocabulary stops being closed.
    """
    _require_curator(user)
    vocab = taxonomy.vocabulary_with_db_skills(db)
    spec, problems = taxonomy.validate_new_skill(
        vocab,
        skill_key=body.skill_key,
        name=body.name,
        cluster=body.cluster,
        aliases=body.aliases,
    )
    if spec is None:
        raise HTTPException(status_code=400, detail={"problems": problems})

    existing = db.get(SmartTeamsSkill, spec.skill_key)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{spec.skill_key!r} already exists"
                + ("" if existing.is_active else " (retired — reactivate it instead)")
                + "."
            ),
        )

    row = SmartTeamsSkill(
        skill_key=spec.skill_key,
        name=spec.name,
        cluster=spec.cluster,
        aliases=list(taxonomy.phrases_for(spec)),
        is_active=True,
    )
    db.add(row)
    db.commit()
    logger.info(
        "[smart-teams][skills] %s added curated skill %r", user.email, spec.skill_key
    )
    return {
        "skill": {
            **_taxonomy_dict(row),
            "is_active": True,
            "managed_in_code": False,
            "claims": 0,
        },
        "notice": (
            "Added. It is searchable now, and the next nightly sweep will start "
            "suggesting it from meetings and documents."
        ),
    }


@router.patch("/admin/taxonomy/{skill_key}")
def edit_curated_skill(
    skill_key: str,
    body: SkillEdit,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Rename, re-cluster, re-alias or retire ONE admin-added skill.

    A skill that ships in the app is refused with 409 rather than edited. The
    seeder overwrites ``name``, ``cluster`` and ``aliases`` from the module on
    every run, so an edit here would appear to work and then silently revert —
    worse than a refusal that says where to make the change.

    ``is_active = False`` is retirement, and it is the only kind of removal:
    ``st_person_skills`` holds a RESTRICT foreign key, so a claimed skill cannot
    be deleted. Retiring hides it from the picker and the sweep; the claims stay.
    """
    _require_curator(user)
    row = db.get(SmartTeamsSkill, (skill_key or "").strip().lower())
    if row is None:
        raise HTTPException(status_code=404, detail=f"No skill {skill_key!r}.")
    if row.skill_key in taxonomy.SKILLS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{row.skill_key!r} ships with the app, so the seeder would "
                "overwrite any change made here. Edit skill_taxonomy.py and "
                "release it instead."
            ),
        )

    wants_content = (
        body.name is not None or body.cluster is not None or body.aliases is not None
    )
    if wants_content:
        vocab = taxonomy.vocabulary_with_db_skills(db)
        spec, problems = taxonomy.validate_new_skill(
            vocab,
            skill_key=row.skill_key,
            name=body.name if body.name is not None else row.name,
            cluster=body.cluster if body.cluster is not None else row.cluster,
            aliases=(
                body.aliases if body.aliases is not None else list(row.aliases or [])
            ),
        )
        if spec is None:
            raise HTTPException(status_code=400, detail={"problems": problems})
        row.name = spec.name
        row.cluster = spec.cluster
        row.aliases = list(taxonomy.phrases_for(spec))

    if body.is_active is not None:
        row.is_active = bool(body.is_active)

    db.commit()
    logger.info(
        "[smart-teams][skills] %s edited curated skill %r (active=%s)",
        user.email,
        row.skill_key,
        row.is_active,
    )
    return {
        "skill": {
            **_taxonomy_dict(row),
            "is_active": bool(row.is_active),
            "managed_in_code": False,
            "claims": store.claim_counts(db).get(row.skill_key, 0),
        }
    }
