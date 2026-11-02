"""ST-9 — the Team Builder: assemble a group for a piece of work.

The ask it answers: "get me a team who can do X with a mix of Y". This module is
the half that touches the database. It reads three inputs, builds a
:class:`team_contracts.TeamResult`, and writes the sentences that render it.

## The three inputs, and nothing else

1. **Confirmed skills.** ``skills.confirmed_skills_for`` — ST-8's evidence-free
   colleague DTO, filtered on ``confirmed.is_(True)`` and nothing else. A
   pending suggestion and a denial tombstone both live in ``confirmed = False``,
   so the one predicate excludes both, and an AI-inferred skill the person has
   never answered can never influence a match. That is not a convention here: it
   is the only read this module makes of the skills tables, and it returns DTOs
   with no evidence field, so a serializer cannot leak a transcript excerpt
   either.
2. **Published profile layers.** A person's working style and strength clusters
   are read only when they have PUBLISHED their personal guide *and* the layer's
   own gate is open (``guide.read_layers`` → ``contracts.layer_of``).
   Publication is the consent switch — ``authz.is_guide_published`` — and a
   draft nobody approved is not a company-visible fact about how somebody works.
3. **PTO / OOO.** The materialised ``ooo_periods`` table
   (``rubik_core.availability.store``'s table, read in one batched query here),
   never ``detect_pto``: one DWD calendar impersonation per candidate is
   unaffordable inside a chat turn.

**No workload, no utilisation, no capacity, no raw scores, no transcripts.**
Workload was deferred out of v1, and ``team_contracts`` refuses a field named
after any of it at import.

## Why nobody is dropped for not having done the interview

A person with confirmed skills and no published profile is proposable. They are
simply somebody we can say less about, and the answer says so —
``StyleBalance.profiles_missing_for`` counts them, the balance note explains what
is therefore unknown about the group's styles, and a per-person line states it in
words. Silently excluding them would make "has taken the Work Style Interview" a
selection criterion for internal project work, which is exactly what Smart Teams
is not allowed to become.

The one population that genuinely cannot be proposed is people who have told
Smart Teams nothing at all: no confirmed skill and no published guide. There is
no fact to match on, so :func:`proposable_people` does not scan for them, and
:data:`SCOPE_NOTICE` says out loud that the answer is drawn from people who have
confirmed skills or published a guide rather than from the whole company.

## How the group is chosen, and why the choice cannot rank anybody

Selection is driven by the **ask**, and among people it is driven by the
**alphabet**:

* Walk the requested skills in the order they were asked for, required ones
  first. For each one not yet covered, take the alphabetically first person who
  has confirmed it and is not out for the whole window.
* Stop when every requested skill is covered. Never pad to reach a requested
  size — a person added for no reason is a person the answer cannot explain, and
  the honest report of "you asked for five and four people cover this" is
  :data:`team_contracts.SHORTFALL_SIZE_NOT_MET`.

What is deliberately absent is any per-person quantity used to order people. The
obvious "improvement" here is greedy set cover — pick whoever covers the most
remaining skills — and it is exactly what this must not do: "covers 3 of the
remaining asks" is a per-person number driving a staffing decision, which is the
leaderboard the contract was written to make impossible. Ask order plus the
alphabet is person-blind, deterministic, and explainable to the person who was
not picked. :data:`SELECTION_NOTICE` states it in every answer, because a reader
who assumes the group is "the best five" will read a ranking that is not there.

## The sentences: nothing about a named person is written by a model

Every line in the write-up is authored first. The difference from wave 2 is
which of them a model is ever allowed to touch, and it is now a property of the
plumbing rather than of a screen.

:func:`rationale_slots` builds a slot per fact. :func:`model_eligible_slots`
keeps only the ones whose fact names NOBODY — the group's style balance, which
dimensions are readable, and the gaps in the ask — and those are the only slots
:func:`compose_rationale` puts in a prompt. A slot about what somebody has
confirmed, what their own guide says, whether they are around, or that they are
the only holder of a skill is rendered from the authored template and is not sent
anywhere. **A model cannot write a sentence about a colleague in a staffing
answer because it is never shown one.**

That replaces screening for this class of failure, and it had to. The wave-2
verifier got all seven of these into a rendered report through the screens:
"Alice Person is the senior voice in this group.", "Give the lead to Alice
Person.", "Carla Person is likely to slow the group down.", "Carla Person is a
risk on this project.", "Alice Person deserves the lead on this.", "Alice Person
is the anchor of this group.", "Carla Person is the natural owner of the audit
work." Every one of them scored ZERO novel content words, because the containment
floor allowed any stem from ``guide_prompts.AUTHORED_VOCABULARY`` — 1670 of them,
harvested across all six instruments — so a per-person judgement could be built
entirely out of words the corpus already held. Tightening the floor
(``allow_authored_vocabulary=False``) does reject all seven, but with a novel-word
budget of zero it also rejects most honest rephrasings of a short fact about a
named person: strict containment over per-person prose is authored fallback with
a Vertex call in front of it, so the call is not made.

The screens are all still there, still called on the group slots, and now called
in strict mode with ``allowed_names`` empty — a group sentence that names anybody
at all is refused. This is the surface where "get me the best team" arrives as a
literal instruction from a real user, so the net stays under the trapeze.

## What this module does NOT do

* **No document row, no RAG ingest.** A team answer is not an ``st_documents``
  row and is not written to any corpus. ``guide.GUIDE_RAG_SLUG`` is the
  companywide corpus with no ACL in front of retrieval, and a team write-up
  quotes several people's guides at once. Producing a saved, exportable *team
  guide* (plan.md's "one click generates the team guide") is therefore NOT built
  here, and nothing in this module enables it.
* **No background job.** The whole answer is a handful of indexed queries plus at
  most one Gemini call, and it has to come back inside the request or the chat
  turn that asked for it. There is nothing to persist and nothing to poll, so
  there is no ``WORKER_JOBS`` entry — instead ``@attribute_to`` sits on
  :func:`write_rationale`, the function that actually makes the call, so the AI
  Cost tag survives the ``asyncio.to_thread`` hop the chat tool makes.

## For a human to review

**Availability has no per-person "we looked" record, and this module does not
invent one.** ``ooo_periods`` holds periods, not scans: a person with no rows is
either tracked-and-free or never scanned, and nothing in the table tells them
apart. ``availability_states`` therefore reports ``unknown`` for anybody with no
rows at all, which is truthful and will read as noisy — most people have no PTO
booked. The fix is a per-user sync watermark on the availability side (a column
this module does not own); until then the alternative was to render "never
scanned" as "free", which is the failure ``AVAILABILITY_UNKNOWN`` exists to
prevent.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import attribute_to
from rubik_core.database.models import OOOPeriod, User
from rubik_core.database.smart_teams import (
    SmartTeamsDocument,
    SmartTeamsPersonSkill,
    SmartTeamsProfile,
)
from rubik_core.smart_teams import (
    authz,
    comparison_contracts,
    guide,
    heatmap,
    skills,
    team_contracts,
    team_prompts,
)
from rubik_core.smart_teams.guide_prompts import ProseSlot
from rubik_core.smart_teams.instruments import strengths

logger = logging.getLogger(__name__)

#: The AI Cost bucket. Same string as ``scoring.COST_FEATURE`` and
#: ``guide.COST_FEATURE`` — a module-level literal rather than an import of one
#: because ``test_smart_teams_conversation`` resolves every ``@attribute_to``
#: argument by AST, and an imported name is a label it cannot check. A second
#: SPELLING would open a second "Unattributed"-shaped hole; a second binding of
#: the same string does not, and the test below it pins them equal.
COST_FEATURE = "smart_teams"

__all__ = [
    "COST_FEATURE",
    "RATIONALE_MODEL_KEY",
    "RATIONALE_TEMPERATURE",
    "RATIONALE_ATTEMPTS",
    "MAX_REQUESTED_SKILLS",
    "MAX_POOL",
    "SCOPE_NOTICE",
    "SELECTION_NOTICE",
    "AVAILABILITY_NOTICE",
    "StyleRead",
    "RationaleLine",
    "TeamAnswer",
    "proposable_people",
    "published_layers",
    "availability_states",
    "build_team",
    "rationale_slots",
    "PERSON_SLOT_KINDS",
    "GROUP_SLOT_KINDS",
    "names_a_person",
    "model_eligible_slots",
    "write_rationale",
    "compose_rationale",
    "answer_for",
    "balance_sentences",
    "DimensionRead",
    "dimension_readiness",
    "render_report",
]


#: Model key, resolved to a concrete Gemini id by
#: ``priorities.agents.base._resolve_model_id`` → ``settings.VERTEX_LLM_MODEL_FLASH``.
#: A key, never an id: model ids are centralised in ``rubik_core/config.py``.
#:
#: Flash rather than the Pro the personal guide uses, and the difference is the
#: deadline: a guide is generated once per person on a background Job, while this
#: runs inside a chat turn against ``chat.REQUEST_TIMEOUT_SECONDS`` (90s) with a
#: user watching. The job here is restatement of authored sentences, which is
#: what Flash is for.
RATIONALE_MODEL_KEY = "gemini-flash"

#: Rewriting authored sentences, not composing. The same 0.2 every other Smart
#: Teams call settled on after the chat hallucination RCA.
RATIONALE_TEMPERATURE = 0.2

#: Total tries for the rationale call. A second failure falls back to authored
#: copy: a model that has ranked people twice is not one more attempt away from
#: not doing it.
RATIONALE_ATTEMPTS = 2

#: The most skills one request may name. Equal to
#: :data:`team_contracts.MAX_TEAM_SIZE` because one person per skill is the worst
#: case for the cover: asking for more skills than the biggest allowed group
#: could only be answered by a group too big to propose, so it is refused where
#: it is asked rather than half-answered here.
MAX_REQUESTED_SKILLS = team_contracts.MAX_TEAM_SIZE

#: How many people the pool query will look at. A guard, not a policy: the pool
#: is everyone who has confirmed a skill or published a guide, bounded by the
#: company — but a runaway seed script must not turn one chat turn into a
#: full-table scan rendered into a prompt.
MAX_POOL = 500


# ── Authored notices ─────────────────────────────────────────────────────────

#: What population the answer was drawn from. It names the honest reasons and
#: forbids the over-reading, and it deliberately does NOT report how many people were searched — the size of the
#: pool is other people's data, not this requester's.
SCOPE_NOTICE = (
    f"{PRODUCT_NAME} matched this against people who have confirmed a skill in Smart "
    "Teams or published a working guide. Somebody who has done neither will not "
    "appear here, and that is not a statement about what they can do — it means "
    f"{PRODUCT_NAME} has nothing on file from them. Never say the company has nobody who can do "
    "something; say nobody has confirmed it in Smart Teams."
)

#: Why the roster is the roster. Read this as the answer to "why isn't Alex on
#: it": because more people than the work needed had confirmed the same skill,
#: and the tie was broken by the alphabet.
SELECTION_NOTICE = (
    "This group was assembled by walking the skills asked for and taking, for "
    "each one, the first person alphabetically who has confirmed it and is "
    "around. When more than one person has confirmed the same skill, the choice "
    "between them is alphabetical and nothing else — it is not an assessment of "
    "who is better suited, and there is no runner-up list. Present it as one "
    "group that covers the ask, never as the best available group."
)

#: What "available" is and is not. ``AVAILABILITY_UNKNOWN`` exists precisely so
#: "we did not check" cannot render as "they are free", and this sentence is what
#: makes that visible to a reader.
AVAILABILITY_NOTICE = (
    "Time off comes from the daily calendar sync, so it covers scheduled PTO "
    "and nothing else — not meetings, not other projects, and not workload, "
    f"which {PRODUCT_NAME} deliberately does not consider. Where it says time off is "
    f"unknown for somebody, that means {PRODUCT_NAME} has no time-off record for them at "
    "all: it is not a statement that they are free."
)

# Shortfall details. Each one states the fact AND forbids the over-reading,
# because the model is what renders this.
_NO_ONE_DETAIL = (
    "Nobody has confirmed {name} in Smart Teams. That is not the same as the "
    "company having nobody who can do it — say only that nobody has confirmed it, and "
    "suggest asking the people who do this work to add it to their skills in "
    f"{PRODUCT_NAME}."
)

_EVERYONE_OUT_DETAIL = (
    "Everybody who has confirmed {name} is out of office for the whole of the "
    "window asked for. Say that the skill is missing from the group for that "
    "reason, and offer to run it again for different dates."
)

_NOT_IN_TAXONOMY_DETAIL = (
    f"{PRODUCT_NAME} does not track a skill called "
    "'{phrase}', so nothing was filtered on "
    "it. Say that plainly — do NOT quietly answer as though it had been "
    f"matched — and offer the closest names {PRODUCT_NAME} does track so they can pick "
    "one."
)

_NO_PROFILES_DETAIL = (
    "Nobody in scope has confirmed a skill or published a working guide, so "
    "there is nothing to assemble a group from. Say that the Work Style "
    f"Interview and the skills list in {PRODUCT_NAME} are what fill this in, and do not "
    "name anybody."
)

_SIZE_DETAIL = (
    "A group of {size} was asked for and only {found} of the people who cover "
    f"part of this ask can be proposed. {PRODUCT_NAME} does not pad a group with people it "
    "has no reason to include, so say the group is smaller than asked for and "
    "why."
)

_SIZE_OVER_NOTE = (
    "Covering everything asked for takes more people than the size asked for. "
    "The group below covers the whole ask; say that it is larger than requested "
    "rather than dropping somebody and leaving a skill uncovered."
)

_MORE_HOLDERS_NOTE = (
    "More people than this group needed have confirmed some of these skills. "
    "Say that the group is one combination that covers the ask, not a shortlist, "
    "and do not speculate about who else it could have been."
)


def _fold(email: object) -> str:
    return str(email or "").strip().lower()


def _display_name(row: User, email: str) -> str:
    """The name to print for somebody who HAS a ``users`` row.

    The local-part fallback is for a real row with a blank ``name`` column, not
    for a missing row — :func:`proposable_people` drops those before it gets
    here, and it used to be this fallback that rendered an offboarded address as
    a plausible colleague called "Exemployee".
    """
    name = (getattr(row, "name", None) or "").strip()
    if name:
        return name
    local = email.split("@", 1)[0]
    return local.replace(".", " ").title() or email


# ── Input 1: who can be proposed at all ──────────────────────────────────────


def proposable_people(db: Session) -> dict[str, str]:
    """``{email: display name}`` for everybody Team Builder can propose.

    Three things have to hold. They have told Smart Teams something about
    themselves — at least one CONFIRMED skill, or a PUBLISHED personal guide.
    ``authz.is_employee`` clears the address (on the Workspace domain, not the
    impersonation pseudo-identity). **And they still have a ``users`` row.**

    ## Why the ``users`` row is the employment check

    Offboarding deletes the ``users`` row — that is how a leaver drops off every
    roster — and it does not touch the Smart Teams tables. So a confirmed skill
    outlives the employee, and without this filter an ex-colleague kept being
    proposed onto live work with their name, their skills and an "availability:
    unknown" beside them. The row is what every other roster site in this codebase
    treats as the existence of an employee, and it is one indexed query on the
    session already in hand — the same one that was already resolving display names,
    so this costs nothing.

    Deliberately NOT ``priorities.batch.list_active_users``, which is the fullest
    derivation of "an active employee" here (a ``users`` row, then
    ``is_eligible_priorities_email``, then a never-signed-in filter, then an
    injected deactivated-account lookup). Three reasons, and each one is about
    that function rather than about this rule:

    * It opens its own ``SessionLocal``, so calling it here would read a
      different database from the one this function was handed — in a test, the
      deployed one.
    * Its later clauses ask "will a morning DM have a reader", which is not a
      question about employment. Dropping somebody from a staffing answer because
      they have never opened the web app would exclude a real colleague, and the
      answer's reader is the requester, not the candidate.
    * The deactivated-account signal (Slack ``deleted`` / Google ``suspended``)
      lives in ``backend.services.directory_status`` and needs a network call per
      run. A chat turn cannot afford it, and a detection outage must not empty a
      staffing answer. That signal is therefore the one part of "active" this
      function does NOT have: somebody suspended in Workspace whose ``users`` row
      has not yet been removed is still proposable here. Closing that needs
      offboarding to retire ``st_person_skills`` and unpublish ``st_documents``,
      which is a ticket rather than a query.
    """
    confirmed = {
        _fold(email)
        for (email,) in db.query(SmartTeamsPersonSkill.user_email)
        .filter(SmartTeamsPersonSkill.confirmed.is_(True))
        .distinct()
        .limit(MAX_POOL)
        .all()
    }
    published = {
        _fold(row.subject_key)
        for row in db.query(SmartTeamsDocument)
        .filter(SmartTeamsDocument.kind == guide.KIND_PERSONAL)
        .limit(MAX_POOL)
        .all()
        if authz.is_guide_published(row.published)
    }
    wanted = [
        email
        for email in sorted({e for e in confirmed | published if e})
        if authz.is_employee(email)
    ]
    if not wanted:
        return {}
    rows = {
        _fold(row.email): row
        for row in db.query(User)
        .filter(func.lower(User.email).in_(wanted), User.is_platform_support.is_(False))
        .all()
    }
    missing = [email for email in wanted if email not in rows]
    if missing:
        # Loud, because the durable fix is on the offboarding side: these people
        # still have live Smart Teams rows that nothing will ever clear.
        logger.info(
            "[smart-teams] team builder: %d address(es) have Smart Teams data "
            "but no users row and are not proposable (offboarded, or seeded "
            "without an account): %s",
            len(missing),
            ", ".join(missing),
        )
    return {
        email: _display_name(rows[email], email) for email in wanted if email in rows
    }


# ── Input 2: published profile layers ────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class StyleRead:
    """What one person's PUBLISHED profile says about how they work.

    ``work_style_key`` / ``work_style_name`` are ``None`` when the DISC gate is
    closed, and ``clusters`` is empty when the strengths gate is. ``published``
    is false when they have no published guide at all, in which case both are
    empty regardless of what the profile row holds — that is the consent switch,
    not a rendering preference.

    ``open_frameworks`` is this person's open gates, and it is what a prose slot
    about them is allowed to name. There is deliberately no numeric field of any
    kind on this class: it feeds a group-level balance and a descriptive line,
    and a confidence or a margin here would be one field away from a per-person
    number in a staffing answer.
    """

    email: str
    published: bool = False
    work_style_key: str | None = None
    #: Where they sit on the map, from the APPROVED version's layers — DISC and
    #: Big Five, never motivators (within-person) or enneagram (a type). ``None``
    #: when nothing placeable is open, which draws as "not placed".
    map_position: tuple[float, float] | None = None
    work_style_name: str | None = None
    work_style_identity: str | None = None
    clusters: frozenset[str] = frozenset()
    open_frameworks: frozenset[str] = frozenset()

    @property
    def has_any_layer(self) -> bool:
        return bool(self.work_style_key or self.clusters)


def _approved_version_on(document: SmartTeamsDocument, email: str) -> int | None:
    """The profile version a published personal guide was generated FROM.

    ``profile_versions`` is ``{email: version}``, written by
    ``guide.ensure_guide_row`` and folded by ``document_cache_keys``; it is read
    here rather than recomputed because it is the only field that says what the
    subject actually approved. ``None`` means the row cannot say — an
    old or hand-written row — and every caller treats that as "read nothing",
    which is the fail-closed side.
    """
    versions = document.profile_versions
    if not isinstance(versions, Mapping):
        return None
    for key, value in versions.items():
        if _fold(key) != email:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _approved_versions(db: Session, wanted: Sequence[str]) -> dict[str, int | None]:
    """``{email: the profile version whose guide they published}``.

    Only people with a published guide appear. The value is ``None`` when the
    published row does not record a version (see :func:`_approved_version_on`).

    **Consent is version-bound, and that is the whole point of this function.**
    The flag is still read through ``authz.is_guide_published`` — the consent
    switch, which also rejects the ``status`` column and any non-boolean truthy
    value — but a boolean alone is what let a retake leak. ``st_documents`` is
    append-only and a retake never clears the old row's ``published`` flag, so a
    subject-keyed boolean answers True from the v1 row while the CONTENT was
    being read off the v2 profile: the colleague's unapproved post-retake style
    served to every colleague until they got round to approving again.
    ``guide.published_guide``'s own docstring names that hazard, and the personal
    guide honours it; this is the derived surface catching up.

    The newest published row wins, mirroring ``guide.published_guide``'s
    ``created_at DESC``: after a retake AND a re-approval there can be two
    published rows, and the one they approved most recently is the one they mean.
    """
    rows = (
        db.query(SmartTeamsDocument)
        .filter(SmartTeamsDocument.kind == guide.KIND_PERSONAL)
        .filter(func.lower(SmartTeamsDocument.subject_key).in_(list(wanted)))
        .order_by(SmartTeamsDocument.created_at.asc())
        .all()
    )
    out: dict[str, int | None] = {}
    for row in rows:  # ascending, so the last write per subject is the newest
        if not authz.is_guide_published(row.published):
            continue
        email = _fold(row.subject_key)
        out[email] = _approved_version_on(row, email)
    return out


def published_layers(db: Session, emails: Iterable[str]) -> dict[str, StyleRead]:
    """Per-person style reads, from the version each person actually APPROVED.

    Three gates in series, and each is somebody else's rule rather than this
    module's: ``authz.is_guide_published`` answers "did they consent",
    :func:`_approved_versions` answers "consent to WHAT" — which profile version
    the published guide was generated from — and ``guide.read_layers``, which
    reads every payload through ``contracts.layer_of``, answers "may this layer
    be asserted at all". A closed gate yields ``None``/empty here and the group
    finding says what is therefore unknown. Nothing in this module reaches into a
    payload's ``layer`` dict without going through that call.

    The layers come off the APPROVED version's profile row, never the current
    one. ``st_profiles`` is append-only, so after a retake the approved row is
    still there to read: a colleague who has retaken but not re-approved
    contributes what they published, not what they have since said. When that row
    is gone or the version is unrecorded, they contribute
    ``StyleRead(published=True)`` with no layers — consent is on the record and
    there is nothing this module may read.
    """
    wanted = [e for e in dict.fromkeys(_fold(e) for e in emails or ()) if e]
    if not wanted:
        return {}

    approved = _approved_versions(db, wanted)
    versions = {version for version in approved.values() if version is not None}
    profiles: dict[tuple[str, int], SmartTeamsProfile] = {}
    if versions:
        # One query, bounded by the pool. Filtered on the SET of approved version
        # numbers rather than on ``is_current``: the numbers are small ints and
        # there are only a handful distinct across a whole pool, so this reads
        # barely more rows than the is_current query it replaced — and it can
        # actually reach a superseded version, which is the point.
        for row in (
            db.query(SmartTeamsProfile)
            .filter(func.lower(SmartTeamsProfile.user_email).in_(wanted))
            .filter(SmartTeamsProfile.version.in_(sorted(versions)))
            .order_by(SmartTeamsProfile.version.asc())
            .all()
        ):
            profiles[(_fold(row.user_email), int(row.version))] = row

    out: dict[str, StyleRead] = {}
    for email in wanted:
        if email not in approved:
            out[email] = StyleRead(email=email, published=False)
            continue
        version = approved[email]
        profile = profiles.get((email, version)) if version is not None else None
        if profile is None:
            # Consent is on the record and there is nothing we may read: the
            # approved version's row is gone, or the published row does not say
            # which version it was. Not the same thing as a closed gate, but it
            # renders identically and safely.
            out[email] = StyleRead(email=email, published=True)
            continue
        layers = guide.read_layers(profile)
        style = _style_dict(layers.get("disc"))
        out[email] = StyleRead(
            email=email,
            published=True,
            # Computed off the SAME approved-version row the style comes from,
            # so the dot and the label can never describe two different sittings.
            map_position=heatmap.map_point(heatmap.envelopes_of(profile)),
            work_style_key=_str_or_none(style, "key"),
            work_style_name=_str_or_none(style, "name"),
            work_style_identity=_str_or_none(style, "identity"),
            clusters=_clusters_of(layers.get("strengths")),
            open_frameworks=frozenset(
                framework for framework, read in layers.items() if read.reportable
            ),
        )
    return out


def _style_dict(read: guide.LayerRead | None) -> Mapping[str, object] | None:
    """The DISC style block past an open gate, or ``None``."""
    if read is None or not read.reportable or not read.layer:
        return None
    assignment = read.layer.get("assignment")
    if not isinstance(assignment, Mapping):
        return None
    style = assignment.get("style")
    return style if isinstance(style, Mapping) else None


def _str_or_none(source: Mapping[str, object] | None, key: str) -> str | None:
    if not source:
        return None
    value = source.get(key)
    return str(value) if isinstance(value, str) and value.strip() else None


def _clusters_of(read: guide.LayerRead | None) -> frozenset[str]:
    """The strengths clusters this person's themes cover, past an open gate."""
    if read is None or not read.reportable or not read.layer:
        return frozenset()
    covered = read.layer.get("clusters_covered")
    if not isinstance(covered, Sequence) or isinstance(covered, str):
        return frozenset()
    known = {cluster.value for cluster in strengths.Cluster}
    return frozenset(str(value) for value in covered if str(value) in known)


# ── Input 3: availability ────────────────────────────────────────────────────


def _window(request: team_contracts.TeamRequest, *, today: date) -> tuple[date, date]:
    """The dates availability is checked over.

    A request with no window is checked against today: "who is out right now" is
    the only honest answer to an ask that named no dates. A half-open window is
    closed onto itself rather than onto a guess about how long a project runs.
    """
    start = request.starts_on or today
    end = request.ends_on or request.starts_on or today
    if end < start:
        start, end = end, start
    return start, end


def availability_states(
    db: Session,
    emails: Iterable[str],
    *,
    start: date,
    end: date,
    beyond_horizon: bool = False,
) -> dict[str, tuple[str, date | None]]:
    """``{email: (availability state, back_on)}`` from the OOO table.

    One query for the whole pool rather than ``store.get_user_ooo_on`` per person
    per day: that helper is per-person and matches ``user_email`` exactly on a
    column written verbatim from the Google token, and a chat turn cannot afford
    ``len(pool) × len(window)`` round trips. The comparison here is
    ``func.lower``, so a mixed-case stored address still matches.

    The four states, and the honest reading of each:

    * ``out_for_window`` — periods cover every day of the window.
    * ``out_part_of_window`` — a period overlaps it.
    * ``available`` — this person HAS rows in the table, so the sweep has seen
      them, and none overlap.
    * ``unknown`` — this person has no rows at all, or the window runs past the
      horizon. See the review note in the module docstring: there is no
      per-person record of "scanned and found nothing", so "no rows" cannot be
      told apart from "never scanned" and must not render as free.

    ``beyond_horizon`` forces every non-overlapping read to ``unknown``: past
    ``team_contracts.AVAILABILITY_HORIZON_DAYS`` there is nothing in the table to
    be clear of.
    """
    wanted = [e for e in dict.fromkeys(_fold(e) for e in emails or ()) if e]
    if not wanted:
        return {}
    start_iso, end_iso = start.isoformat(), end.isoformat()
    rows = (
        db.query(OOOPeriod)
        .filter(func.lower(OOOPeriod.user_email).in_(wanted))
        .order_by(OOOPeriod.start_date.asc())
        .all()
    )
    tracked: set[str] = set()
    overlapping: dict[str, list[OOOPeriod]] = {}
    for row in rows:
        email = _fold(row.user_email)
        tracked.add(email)
        if str(row.start_date) <= end_iso and str(row.end_date) >= start_iso:
            overlapping.setdefault(email, []).append(row)

    out: dict[str, tuple[str, date | None]] = {}
    for email in wanted:
        periods = overlapping.get(email) or []
        if periods:
            state = (
                team_contracts.AVAILABILITY_OUT
                if _covers_whole_window(periods, start=start, end=end)
                else team_contracts.AVAILABILITY_PARTIAL
            )
            out[email] = (state, _parse_iso(str(periods[0].return_date)))
        elif beyond_horizon or email not in tracked:
            out[email] = (team_contracts.AVAILABILITY_UNKNOWN, None)
        else:
            out[email] = (team_contracts.AVAILABILITY_AVAILABLE, None)
    return out


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _covers_whole_window(
    periods: Sequence[OOOPeriod], *, start: date, end: date
) -> bool:
    """Whether the periods together cover every day from ``start`` to ``end``.

    Day by day rather than by interval arithmetic: the windows are weeks, the
    periods are few, and a merge-intervals bug that reports somebody available is
    the expensive kind of clever.
    """
    day = start
    while day <= end:
        iso = day.isoformat()
        if not any(str(p.start_date) <= iso <= str(p.end_date) for p in periods):
            return False
        day += timedelta(days=1)
    return True


# ── The answer ───────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class RationaleLine:
    """One rendered sentence, and where its wording came from.

    ``authored`` is our copy; ``text`` is what is shown. They differ only when a
    model line passed every screen. ``from_model`` is kept so a caller can report
    the degradation honestly rather than claiming a written-up answer when Vertex
    was down.

    ``from_model`` can only ever be True on a GROUP slot — one whose authored
    fact names nobody. :func:`compose_rationale` does not read a model's output
    for any other slot, so a line about a named colleague is authored by
    construction and ``text == authored`` for it always.
    """

    slot_id: str
    text: str
    authored: str
    from_model: bool = False


@dataclasses.dataclass(frozen=True)
class TeamAnswer:
    """A :class:`team_contracts.TeamResult` plus everything needed to render it.

    ``notes`` are authored sentences about the SHAPE of the answer — the group is
    bigger than the size asked for, more people hold a skill than the group
    needed. Those are facts about the request rather than gaps in it, so they are
    not :class:`team_contracts.Shortfall` entries (whose codes are a closed set
    that has to stay meaningful). ``display_names`` is carried so the renderer
    and the prose screen agree on exactly which names may appear.
    """

    result: team_contracts.TeamResult
    display_names: Mapping[str, str] = dataclasses.field(default_factory=dict)
    styles: Mapping[str, StyleRead] = dataclasses.field(default_factory=dict)
    notes: tuple[str, ...] = ()
    rationale: tuple[RationaleLine, ...] = ()
    window: tuple[date, date] | None = None

    @property
    def names(self) -> tuple[str, ...]:
        """The display names of the proposed group. The only names prose may use."""
        return tuple(
            self.display_names.get(email, email) for email in self.result.emails
        )


def build_team(
    db: Session,
    request: team_contracts.TeamRequest,
    *,
    today: date | None = None,
) -> TeamAnswer:
    """Match a parsed ask against the three inputs. No LLM, no writes.

    Pure read path, deterministic, safe to call twice. Every refusal it can
    produce is a :class:`team_contracts.Shortfall` on the result rather than an
    exception: an ask nobody can satisfy is a first-class answer, and raising
    would hand the caller an error to render as a failure.
    """
    today = today or date.today()
    start, end = _window(request, today=today)
    beyond = request.window_beyond_horizon(today=today)
    if beyond:
        # Never check past the horizon: unsynced weeks cannot clear anybody, so
        # the checked window stops there and every non-overlapping read below
        # becomes `unknown` rather than `available`.
        end = min(end, today + timedelta(days=team_contracts.AVAILABILITY_HORIZON_DAYS))

    pool = proposable_people(db)
    skills_by_email = skills.confirmed_skills_for(db, pool.keys()) if pool else {}
    styles = published_layers(db, pool.keys()) if pool else {}
    availability = (
        availability_states(
            db, pool.keys(), start=start, end=end, beyond_horizon=beyond
        )
        if pool
        else {}
    )

    asked = tuple(request.skills)
    ordered_asks = tuple(a for a in asked if a.required) + tuple(
        a for a in asked if not a.required
    )

    holders = {
        ask.skill_key: tuple(
            email
            for email in sorted(pool)
            if ask.skill_key in {s.skill_key for s in skills_by_email.get(email, ())}
        )
        for ask in asked
    }
    free_holders = {
        key: tuple(
            email
            for email in emails
            if availability.get(email, (team_contracts.AVAILABILITY_UNKNOWN, None))[0]
            != team_contracts.AVAILABILITY_OUT
        )
        for key, emails in holders.items()
    }

    # ── Selection: ask order, then the alphabet. Nothing else. ──
    selected: list[str] = []
    more_holders_than_needed = False
    for ask in ordered_asks:
        candidates = free_holders.get(ask.skill_key, ())
        if not candidates or any(email in selected for email in candidates):
            continue
        if len(selected) >= team_contracts.MAX_TEAM_SIZE:
            break
        selected.append(candidates[0])
        if len(candidates) > 1:
            more_holders_than_needed = True
    roster = sorted(set(selected))

    coverage = tuple(
        team_contracts.SkillCoverage(
            skill_key=ask.skill_key,
            phrase=ask.phrase,
            required=ask.required,
            covered_by=tuple(sorted(e for e in holders[ask.skill_key] if e in roster)),
        )
        for ask in asked
    )

    candidates_out = tuple(
        _candidate(
            email,
            display_name=pool.get(email, email),
            skills_confirmed=skills_by_email.get(email, ()),
            asked=asked,
            availability=availability.get(
                email, (team_contracts.AVAILABILITY_UNKNOWN, None)
            ),
            style=styles.get(email) or StyleRead(email=email),
        )
        for email in roster
    )

    shortfalls = list(_shortfalls(request, coverage, holders, free_holders, pool))
    notes: list[str] = []
    if roster and request.size is not None and len(roster) > request.size:
        notes.append(_SIZE_OVER_NOTE)
    if more_holders_than_needed:
        notes.append(_MORE_HOLDERS_NOTE)

    horizon_note: str | None = None
    if beyond:
        horizon_note = team_contracts.HORIZON_NOTE
        shortfalls.append(
            team_contracts.Shortfall(
                code=team_contracts.SHORTFALL_WINDOW_BEYOND_HORIZON,
                detail=team_contracts.HORIZON_NOTE,
            )
        )

    result = team_contracts.TeamResult(
        request=request,
        candidates=candidates_out,
        coverage=coverage,
        balance=_style_balance(request, roster, styles),
        shortfalls=tuple(shortfalls),
        horizon_note=horizon_note,
    )
    return TeamAnswer(
        result=result,
        display_names={email: pool.get(email, email) for email in roster},
        styles={email: styles.get(email) or StyleRead(email=email) for email in roster},
        notes=tuple(notes),
        window=(start, end),
    )


def _candidate(
    email: str,
    *,
    display_name: str,
    skills_confirmed: Sequence[skills.ConfirmedSkill],
    asked: Sequence[team_contracts.SkillAsk],
    availability: tuple[str, date | None],
    style: StyleRead,
) -> team_contracts.TeamCandidate:
    state, back_on = availability
    wanted = {ask.skill_key for ask in asked}
    return team_contracts.TeamCandidate(
        user_email=email,
        display_name=display_name,
        covers=tuple(
            team_contracts.CoveredSkill(
                skill_key=skill.skill_key, level=skill.level, origin=skill.origin
            )
            for skill in skills_confirmed
            if skill.skill_key in wanted
        ),
        availability=state,
        back_on=(back_on if state != team_contracts.AVAILABILITY_AVAILABLE else None),
        work_style_key=style.work_style_key,
        map_position=style.map_position,
        clusters=style.clusters,
    )


def _shortfalls(
    request: team_contracts.TeamRequest,
    coverage: Sequence[team_contracts.SkillCoverage],
    holders: Mapping[str, Sequence[str]],
    free_holders: Mapping[str, Sequence[str]],
    pool: Mapping[str, str],
) -> Iterable[team_contracts.Shortfall]:
    """Every honest reason this ask was not fully met.

    Ordered so the answer reads outward from the request: what Cubie did not
    understand, then what nobody has confirmed, then who is away, then whether
    there was anything to assemble from at all.
    """
    for unresolved in request.unresolved_skills:
        yield team_contracts.Shortfall(
            code=team_contracts.SHORTFALL_SKILL_NOT_IN_TAXONOMY,
            detail=_NOT_IN_TAXONOMY_DETAIL.format(phrase=unresolved.phrase),
            phrase=unresolved.phrase,
        )

    covered_keys = {item.skill_key for item in coverage if item.covered}
    for ask in request.skills:
        if not ask.required or ask.skill_key in covered_keys:
            continue
        if not holders.get(ask.skill_key):
            code, detail = (
                team_contracts.SHORTFALL_NO_ONE_WITH_SKILL,
                _NO_ONE_DETAIL.format(name=ask.name),
            )
        elif not free_holders.get(ask.skill_key):
            code, detail = (
                team_contracts.SHORTFALL_EVERYONE_OUT,
                _EVERYONE_OUT_DETAIL.format(name=ask.name),
            )
        else:  # pragma: no cover — the cover loop always takes a free holder
            code, detail = (
                team_contracts.SHORTFALL_NOT_ENOUGH_WITH_SKILL,
                _NO_ONE_DETAIL.format(name=ask.name),
            )
        yield team_contracts.Shortfall(
            code=code, detail=detail, skill_key=ask.skill_key, phrase=ask.phrase
        )

    if not pool:
        # Nothing on file from anybody. Distinct from "nobody has this skill",
        # and it forces an empty roster in ``TeamResult.__post_init__``.
        yield team_contracts.Shortfall(
            code=team_contracts.SHORTFALL_NO_PUBLISHED_PROFILES,
            detail=_NO_PROFILES_DETAIL,
        )
        return
    proposed = {email for item in coverage for email in item.covered_by}
    if request.size is not None and len(proposed) < request.size:
        yield team_contracts.Shortfall(
            code=team_contracts.SHORTFALL_SIZE_NOT_MET,
            detail=_SIZE_DETAIL.format(size=request.size, found=len(proposed)),
        )


# ── The group's styles ───────────────────────────────────────────────────────

_ALL_CLUSTERS: frozenset[str] = frozenset(
    cluster.value for cluster in strengths.Cluster
)

_ALL_ONE_STYLE_NOTE = (
    "Everybody in this group whose guide reports a working style reports the "
    "same one. That is a finding about the group and not about any of them: a "
    "group that works one way will agree quickly and will miss whatever that way "
    "misses. It is worth knowing before the work starts, and it is not a reason "
    "to take anybody off the group."
)

_NO_STYLES_NOTE = (
    "Nobody in this group has a published guide that reports a working style, so "
    f"{PRODUCT_NAME} has nothing to say about how the group's styles fit together. That is "
    "missing information, not a warning."
)

_SOME_STYLES_MISSING_NOTE = (
    "Some of this group have not published a working guide, so the style picture "
    "here is only about the ones who have. Do not present it as the whole "
    "group's."
)


def _style_balance(
    request: team_contracts.TeamRequest,
    roster: Sequence[str],
    styles: Mapping[str, StyleRead],
) -> team_contracts.StyleBalance:
    """What the GROUP's styles add up to. Never a per-person read.

    ``profiles_missing_for`` is the honesty valve: a group where two of four
    people have published nothing has less to say about its own balance, and the
    note says which half of the finding is missing rather than reporting a
    two-person balance as if it were the group's.
    """
    reads = [styles.get(email) or StyleRead(email=email) for email in roster]
    present = tuple(
        sorted(str(read.work_style_key) for read in reads if read.work_style_key)
    )
    covered = frozenset(cluster for read in reads for cluster in read.clusters)
    desired = frozenset(request.desired_clusters) or _ALL_CLUSTERS
    missing = desired - covered
    all_one = len(set(present)) == 1 and len(present) > 1
    return team_contracts.StyleBalance(
        clusters_covered=covered,
        clusters_missing=missing,
        styles_present=present,
        all_one_style=all_one,
        profiles_missing_for=sum(1 for read in reads if not read.has_any_layer),
        note=_balance_note(
            all_one=all_one,
            styles_present=present,
            missing=missing,
            missing_profiles=sum(1 for read in reads if not read.has_any_layer),
            roster_size=len(roster),
        ),
    )


def balance_sentences(
    balance: team_contracts.StyleBalance, *, roster_size: int
) -> list[str]:
    """The group's style picture as SEPARATE authored sentences.

    Separate rather than one paragraph because each one is a prose slot, and a
    slot's fact has to fit inside ``guide_prompts.MAX_BODY_WORDS`` for a model
    restatement of it to be acceptable at all. Joined, an all-one-style group
    with three cluster gaps ran past the cap, so the most valuable finding in the
    answer was the one guaranteed to fall back to authored copy.

    Cluster gaps read ``strengths.CLUSTER_MEANING`` verbatim — that module
    already says what a team short on each cluster costs, and one sentence about
    a Stewardship gap should be the same sentence everywhere it appears.
    """
    parts: list[str] = []
    if roster_size and not balance.styles_present:
        parts.append(_NO_STYLES_NOTE)
    elif balance.profiles_missing_for:
        parts.append(_SOME_STYLES_MISSING_NOTE)
    if balance.all_one_style:
        parts.append(_ALL_ONE_STYLE_NOTE)
    if balance.styles_present:
        for value in sorted(balance.clusters_missing):
            cluster = strengths.Cluster(value)
            parts.append(
                f"Nobody here covers {strengths.CLUSTER_LABELS[cluster]}. "
                f"{strengths.CLUSTER_MEANING[cluster]}"
            )
    return parts


def _balance_note(
    *,
    all_one: bool,
    styles_present: Sequence[str],
    missing: Iterable[str],
    missing_profiles: int,
    roster_size: int,
) -> str:
    """The whole style picture as one string, for ``StyleBalance.note``.

    The contract carries one ``note`` field, so the sentences are joined here —
    and :func:`balance_sentences` is what the prose slots use, one sentence each.
    """
    parts: list[str] = []
    if roster_size and not styles_present:
        parts.append(_NO_STYLES_NOTE)
    elif missing_profiles:
        parts.append(_SOME_STYLES_MISSING_NOTE)
    if all_one:
        parts.append(_ALL_ONE_STYLE_NOTE)
    if styles_present:
        for value in sorted(missing):
            cluster = strengths.Cluster(value)
            parts.append(
                f"Nobody here covers {strengths.CLUSTER_LABELS[cluster]}. "
                f"{strengths.CLUSTER_MEANING[cluster]}"
            )
    return " ".join(parts)


# ── Which style dimensions this group can be read on at all ──────────────────

_DIMENSIONS_THIN_NOTE = (
    f"{PRODUCT_NAME} can only read part of this group on "
    "{labels}. The layers those come "
    "from are not reportable for everybody here, so treat anything said about "
    "them as partial rather than as the whole group's picture."
)

_DIMENSIONS_CLOSED_NOTE = (
    f"{PRODUCT_NAME} has nothing to say about this group on "
    "{labels}: nobody here has a "
    "published, reportable layer behind them. That is missing information, not a "
    "finding."
)


@dataclasses.dataclass(frozen=True)
class DimensionRead:
    """How much of the group one comparison dimension can be read on.

    The vocabulary is ``comparison_contracts.DIMENSIONS`` — the same ten
    dimensions the pair guide is organised by — so "communication pace" means the
    same thing, and rests on the same instrument fields, in both documents.

    ``open_for`` / ``of_people`` are GROUP counts and there is deliberately no
    per-person mapping on this class. "Two of the four people here have a
    reportable layer behind decision style" is a statement about how complete the
    group's picture is; the same fact broken out by name is a list of who has not
    finished their interview, published to their colleagues.
    """

    key: str
    label: str
    open_for: int
    of_people: int

    @property
    def fully_open(self) -> bool:
        return self.of_people > 0 and self.open_for == self.of_people

    @property
    def closed(self) -> bool:
        return self.open_for == 0


def dimension_readiness(answer: TeamAnswer) -> tuple[DimensionRead, ...]:
    """Per dimension: how many of the group have a reportable layer behind it.

    A dimension is readable for one person when ANY of its source frameworks is
    reportable for them — the same "any, not all" rule
    ``comparison_contracts.Dimension.is_open`` applies to a pair, generalised to
    N people. A dimension nobody has open is not a finding of "nothing to worry
    about here"; it is a dimension with nothing to say, and
    :func:`dimension_notes` says which.
    """
    roster = answer.result.emails
    out: list[DimensionRead] = []
    for dimension in comparison_contracts.DIMENSIONS:
        frameworks = set(dimension.frameworks)
        open_for = sum(
            1
            for email in roster
            if frameworks
            & (answer.styles.get(email) or StyleRead(email=email)).open_frameworks
        )
        out.append(
            DimensionRead(
                key=dimension.key,
                label=dimension.label,
                open_for=open_for,
                of_people=len(roster),
            )
        )
    return tuple(out)


def dimension_notes(reads: Sequence[DimensionRead]) -> list[str]:
    """Authored sentences for the dimensions the group is thin or silent on."""
    thin = [read.label for read in reads if not read.closed and not read.fully_open]
    closed = [read.label for read in reads if read.closed]
    notes: list[str] = []
    if thin:
        notes.append(_DIMENSIONS_THIN_NOTE.format(labels=_and_list(thin)))
    if closed:
        notes.append(_DIMENSIONS_CLOSED_NOTE.format(labels=_and_list(closed)))
    return notes


def _and_list(values: Sequence[str]) -> str:
    lowered = [value[0].lower() + value[1:] for value in values]
    if len(lowered) == 1:
        return lowered[0]
    return ", ".join(lowered[:-1]) + " and " + lowered[-1]


# ── The sentences ────────────────────────────────────────────────────────────

SLOT_COVERS = "covers"
SLOT_STYLE = "style"
SLOT_SOLE = "sole"
SLOT_AVAILABILITY = "availability"
SLOT_NO_PROFILE = "no_profile"
SLOT_BALANCE = "balance"
SLOT_DIMENSIONS = "dimensions"
SLOT_GAP = "gap"

#: Slot kinds whose authored fact NAMES A COLLEAGUE. These are rendered from the
#: authored template and are never put in a prompt — see the module docstring for
#: the seven judgements that reached a rendered report when they were.
PERSON_SLOT_KINDS: frozenset[str] = frozenset(
    {SLOT_COVERS, SLOT_STYLE, SLOT_SOLE, SLOT_AVAILABILITY, SLOT_NO_PROFILE}
)

#: Slot kinds whose fact is about the GROUP, the ask, or what Cubie cannot read.
#: Listed rather than derived as "everything else" on purpose: a new slot kind
#: must be classified by whoever adds it, and the default for an unclassified one
#: is to be treated as if it named somebody. :func:`rationale_slots` is pinned
#: against the union of these two sets by
#: ``test_smart_teams_team_builder.py``, so a slot kind that belongs to neither
#: fails a test rather than quietly reaching a model.
GROUP_SLOT_KINDS: frozenset[str] = frozenset({SLOT_BALANCE, SLOT_DIMENSIONS, SLOT_GAP})


def names_a_person(slot_id: str) -> bool:
    """Whether a slot's authored fact is about a named colleague.

    Fails CLOSED: a slot id whose kind is in neither set is treated as naming
    somebody, so an unclassified slot is excluded from the prompt rather than
    included in it.
    """
    kind = str(slot_id or "").split(":", 1)[0]
    return kind not in GROUP_SLOT_KINDS


def model_eligible_slots(slots: Sequence[ProseSlot]) -> list[ProseSlot]:
    """The slots a model may be shown: the ones whose fact names nobody.

    This is the control that makes per-person model prose impossible in a
    staffing answer, and it is one filter at one call site
    (:func:`compose_rationale`) rather than a rule spread across the prompt, the
    screens and the renderer.
    """
    return [slot for slot in slots if not names_a_person(slot.slot_id)]


def rationale_slots(answer: TeamAnswer) -> list[ProseSlot]:
    """Every authored fact the write-up is built from — one line each.

    This is the WHOLE write-up, and every slot here renders whether or not a
    model is reachable. :func:`model_eligible_slots` is what decides which of
    them a model is allowed to reword (the ones naming nobody); the rest render
    from ``slot.body`` verbatim.

    Each slot's ``open_frameworks`` is the intersection of the open gates of the
    people that slot speaks about — the compounding rule
    ``comparison_contracts.comparable_frameworks`` states for two people,
    generalised to N. A slot about the whole group's styles may only name a
    framework every one of those people has open, so a group sentence can never
    name a layer one member refused. A slot about a skill, a calendar or a gap
    carries an EMPTY set, which makes every framework name a refusal — the strict
    default, and correct: none of those facts came from an instrument.
    """
    result = answer.result
    names = answer.display_names
    slots: list[ProseSlot] = []

    def _slot(slot_id: str, intent: str, fact: str, heading: str, *emails: str):
        opens = [
            (answer.styles.get(email) or StyleRead(email=email)).open_frameworks
            for email in emails
        ]
        open_frameworks: frozenset[str] = frozenset()
        if opens:
            open_frameworks = opens[0]
            for other in opens[1:]:
                open_frameworks = open_frameworks & other
        slots.append(
            ProseSlot(
                slot_id=slot_id,
                intent=intent,
                fact=fact,
                heading=heading,
                body=fact,
                open_frameworks=open_frameworks,
            )
        )

    for candidate in result.candidates:
        email = candidate.user_email
        name = names.get(email, candidate.display_name)
        if candidate.covers:
            listed = ", ".join(
                item.name + (f" ({item.level})" if item.level else "")
                for item in candidate.covers
            )
            _slot(
                f"{SLOT_COVERS}:{email}",
                "what this person has confirmed that the work needs — "
                "descriptive, never a judgement of how good they are at it",
                f"{name} has confirmed {listed}.",
                name,
            )
        style = answer.styles.get(email)
        if style is not None and style.work_style_name and style.work_style_identity:
            fact = (
                f"{name}'s published guide describes their working style as "
                f"{style.work_style_name}: {style.work_style_identity}"
            )
            _slot(
                f"{SLOT_STYLE}:{email}",
                "how this person's own published guide says they like to work, "
                "so the group knows what to expect",
                fact,
                name,
                email,
            )

    for item in result.coverage:
        if len(item.covered_by) != 1:
            continue
        email = item.covered_by[0]
        name = names.get(email, email)
        _slot(
            f"{SLOT_SOLE}:{item.skill_key}",
            "a single point of coverage — a risk to the work, not a compliment",
            f"{name} is the only person in this group who has confirmed "
            f"{item.name}. If they are pulled off, nobody else here covers that "
            "part of the work.",
            item.name,
        )

    for candidate in result.candidates:
        email = candidate.user_email
        name = names.get(email, candidate.display_name)
        if candidate.availability == team_contracts.AVAILABILITY_PARTIAL:
            back = (
                f" They are back on {candidate.back_on.isoformat()}."
                if candidate.back_on
                else ""
            )
            fact = f"{name} is out of office for part of this window.{back}"
        elif candidate.availability == team_contracts.AVAILABILITY_UNKNOWN:
            fact = (
                f"{PRODUCT_NAME} has no time-off record for {name}, so whether they are "
                "free for this window is unknown rather than clear."
            )
        else:
            continue
        _slot(
            f"{SLOT_AVAILABILITY}:{email}",
            "whether this person is around for the window asked for",
            fact,
            name,
        )

    for candidate in result.candidates:
        email = candidate.user_email
        style = answer.styles.get(email)
        if style is not None and style.has_any_layer:
            continue
        name = names.get(email, candidate.display_name)
        _slot(
            f"{SLOT_NO_PROFILE}:{email}",
            "what is NOT known about somebody in the group, said plainly rather "
            "than left as an absence",
            f"{name} has not published a working guide, so {PRODUCT_NAME} can say what "
            "they have confirmed they do and nothing about how they like to work.",
            name,
        )

    for index, sentence in enumerate(
        balance_sentences(result.balance, roster_size=len(result.candidates))
    ):
        _slot(
            f"{SLOT_BALANCE}:{index}",
            "what the GROUP's working styles add up to — never about a person",
            sentence,
            "Working styles",
            *result.emails,
        )

    for index, sentence in enumerate(dimension_notes(dimension_readiness(answer))):
        _slot(
            f"{SLOT_DIMENSIONS}:{index}",
            f"which parts of the group's style picture {PRODUCT_NAME} can read at all — "
            "an admission of missing information, never a finding about anybody",
            sentence,
            "What is not readable",
        )

    for shortfall in result.shortfalls:
        marker = shortfall.skill_key or shortfall.phrase or ""
        _slot(
            f"{SLOT_GAP}:{shortfall.code}:{marker}",
            "a gap in what was asked for, and what would fix it",
            shortfall.detail,
            "Not covered",
        )
    return slots


def _json_client(model_key: str, temperature: float):
    """The house JSON-over-Gemini client, imported lazily.

    Mirrors ``guide._json_client`` deliberately: a function so tests can stub it
    without patching the SDK, lazy so importing this module does not build a
    genai client.
    """
    from rubik_core.priorities.agents.base import LLMJsonClient

    return LLMJsonClient(model_key=model_key, temperature=temperature)


@attribute_to(COST_FEATURE)
def write_rationale(
    slots: Sequence[ProseSlot],
    *,
    ask: str = "",
    allowed_names: Sequence[str] = (),  # a group sentence names nobody
    model_key: str = RATIONALE_MODEL_KEY,
    temperature: float = RATIONALE_TEMPERATURE,
    attempts: int = RATIONALE_ATTEMPTS,
) -> dict[str, str]:
    """Restate the authored GROUP facts in plainer English. One LLM call.

    Returns ``{slot_id: line}`` containing only the strings that came back AND
    passed ``team_prompts.screen_rationale`` — the evaluative screen, the ranking
    screen, the unknown-name screen, and the guide's containment screens in
    STRICT mode against this slot's own fact. A slot the model skipped,
    mislabelled or wrote badly is simply absent and the caller keeps the authored
    wording, so a bad call costs the voice of an answer and never its content.

    Any slot that names a person is dropped before the prompt is built, even
    though :func:`compose_rationale` has already filtered them out. This is not
    belt-and-braces for its own sake: this function is public, ``@attribute_to``
    makes it look like the entry point, and a future caller that assembles slots
    itself must not be able to put a colleague's name in a prompt by forgetting
    a filter one file away.

    ``allowed_names`` defaults to EMPTY and callers should leave it that way: a
    group sentence names nobody, so any name in the returned line came from the
    model.

    ``@attribute_to`` sits HERE, on the function that makes the call, and not on
    :func:`answer_for` or on the chat tool: the AI Cost tag is a contextvar, the
    chat tool runs this on a worker thread, and a tag set at the entry point does
    not survive that hop. That is the 75%-"Unattributed" lesson.

    Never raises. A Team Builder answer must render while Vertex is down.
    """
    slots = model_eligible_slots(list(slots))
    if not slots:
        return {}
    prompt = team_prompts.rationale_prompt(slots, ask=ask)
    client = _json_client(model_key, temperature)
    by_id = {slot.slot_id: slot for slot in slots}

    for attempt in range(1, max(1, attempts) + 1):
        try:
            payload = client.invoke_json(prompt, fallback=None, attempts=1)
        except Exception as exc:  # noqa: BLE001 — the authored copy is the floor
            logger.warning(
                "[smart-teams] team rationale call failed (attempt %d): %s",
                attempt,
                exc,
            )
            continue

        accepted: dict[str, str] = {}
        rejected: list[tuple[str, str]] = []
        entries = payload.get("lines") if isinstance(payload, Mapping) else None
        for entry in entries or []:
            if not isinstance(entry, Mapping):
                continue
            slot_id = entry.get("id")
            if not isinstance(slot_id, str) or slot_id not in by_id:
                logger.info(
                    "[smart-teams] team rationale: dropping unknown slot id %r",
                    slot_id,
                )
                continue
            text = entry.get("body")
            reason = team_prompts.screen_rationale(
                text, by_id[slot_id], allowed_names=allowed_names
            )
            if reason:
                rejected.append((slot_id, reason))
                logger.warning(
                    "[smart-teams] team rationale: rejected slot %s (%s)",
                    slot_id,
                    reason,
                )
                continue
            accepted[slot_id] = str(text).strip()

        # A refused string means the model has demonstrated it will write what it
        # was told not to, so the sample is re-drawn rather than partly kept. On
        # the last attempt we keep whatever passed.
        if rejected and attempt < max(1, attempts):
            logger.info(
                "[smart-teams] team rationale: regenerating after %d rejected "
                "line(s): %s",
                len(rejected),
                ", ".join(f"{slot} ({reason})" for slot, reason in rejected),
            )
            continue
        return accepted
    return {}


RationaleWriter = Callable[..., Mapping[str, str]]


def compose_rationale(
    answer: TeamAnswer, *, rationale_writer: RationaleWriter | None = None
) -> tuple[RationaleLine, ...]:
    """Authored lines. A model's wording only where the fact names nobody.

    Two things happen here, and the first is the one that matters:

    1. **Only the GROUP slots are shown to the writer.** A slot whose fact names
       a colleague is rendered from :attr:`ProseSlot.body` — our authored
       template — and is not in the prompt, not offered to the writer, and not
       looked up in whatever the writer returned. So a per-person line cannot
       come from a model even if the writer returns one keyed to that slot id:
       there is no code path that reads it.
    2. The screen runs AGAIN over whatever came back for the group slots. That is
       not redundant: :func:`write_rationale` is one implementation of the
       callable, and a stub or a future caller that skipped the screen must not be
       able to put an unscreened sentence into a company-visible answer.
       ``allowed_names`` is empty here — a group sentence names nobody, so a name
       in one came from the model.

    ``rationale_writer`` is injectable for the same reason ``guide.build_guide``
    takes a ``prose_writer``: every test in this feature runs with the LLM
    stubbed, and a writer that returns ``{}`` must still produce a complete
    answer.
    """
    slots = rationale_slots(answer)
    if not slots:
        return ()
    eligible = model_eligible_slots(slots)
    written: Mapping[str, str] = {}
    if eligible:
        writer = rationale_writer or write_rationale
        try:
            written = (
                writer(
                    eligible,
                    ask=answer.result.request.raw_ask,
                    allowed_names=(),
                )
                or {}
            )
        except Exception:  # noqa: BLE001 — the authored copy is the floor
            logger.exception("[smart-teams] team rationale writer failed")
            written = {}

    lines: list[RationaleLine] = []
    for slot in slots:
        candidate: str | None = None
        if not names_a_person(slot.slot_id):
            candidate = written.get(slot.slot_id)
            if not isinstance(candidate, str) or not candidate.strip():
                candidate = None
            elif team_prompts.screen_rationale(candidate, slot, allowed_names=()):
                candidate = None
        lines.append(
            RationaleLine(
                slot_id=slot.slot_id,
                text=(candidate or slot.body).strip(),
                authored=slot.body,
                from_model=candidate is not None,
            )
        )
    return tuple(lines)


def answer_for(
    db: Session,
    request: team_contracts.TeamRequest,
    *,
    today: date | None = None,
    rationale_writer: RationaleWriter | None = None,
) -> TeamAnswer:
    """Match, then write it up. The one call both surfaces make."""
    answer = build_team(db, request, today=today)
    return dataclasses.replace(
        answer,
        rationale=compose_rationale(answer, rationale_writer=rationale_writer),
    )


# ── Rendering ────────────────────────────────────────────────────────────────

_STATE_LINES: Mapping[str, str] = {
    team_contracts.STATE_COMPLETE: "This group covers everything that was asked for.",
    team_contracts.STATE_PARTIAL: (
        "This is a PARTIAL answer. Report the gaps below as gaps — never present "
        "it as a finished team."
    ),
    team_contracts.STATE_EMPTY: (
        f"{PRODUCT_NAME} has no group to propose for this ask. Report the reasons below and "
        "do not name anybody."
    ),
}

#: The heading over the per-person lines. It used to read "Write-up (descriptive
#: only — none of it rates anybody)" above four model-written per-person verdicts,
#: which is the specific failure the honesty rule names: a heading asserting a
#: guarantee the code did not provide.
#:
#: What it may now claim, and why each clause is true: **Cubie's own wording** —
#: ``compose_rationale`` reads no model output for a slot that names a person, so
#: every line here is :attr:`ProseSlot.body`, an authored template filled in from
#: the typed ``TeamResult``. **Not an assessment** — the templates say what
#: somebody has confirmed, what their own published guide says, whether they are
#: around, and where the group has a single point of coverage; none of them
#: compares anybody or grades anybody. **The levels are their own** — "expert" /
#: "strong" / "working" is the word the person chose when they confirmed the
#: skill, not a mark Cubie gave them, and saying so is what stops the model
#: reading it back as a rating.
_WRITE_UP_HEADING = (
    f"Per-person lines, in {PRODUCT_NAME}'s own wording — a model wrote none of these. "
    "Each one says what somebody has confirmed about themselves, what their own "
    "published guide says about how they work, whether they are around, or that "
    "they are the only person here covering something. Any skill level in them "
    f"is the word that person chose when they confirmed it, not a mark {PRODUCT_NAME} gave "
    "them. None of this is an assessment of anybody, and the answer you write "
    "must not turn it into one:"
)

_AVAILABILITY_WORDS: Mapping[str, str] = {
    team_contracts.AVAILABILITY_AVAILABLE: "no time off booked in the window",
    team_contracts.AVAILABILITY_PARTIAL: "out for part of the window",
    team_contracts.AVAILABILITY_OUT: "out for the whole window",
    team_contracts.AVAILABILITY_UNKNOWN: "time off unknown",
}


def render_report(answer: TeamAnswer) -> str:
    """The markdown the API returns and the chat tool hands the model.

    Written as instructions as much as content: nothing here reaches a user
    directly — the model reads it and
    writes the answer — so a fact with no instruction beside it gets rendered
    however the model feels. Every notice that must survive to the user is a
    sentence in this string rather than a flag a frontend is trusted to render.
    """
    result = answer.result
    request = result.request
    out: list[str] = []

    # Echo the ask the way it was PHRASED, including the parts that did not
    # resolve: an echo of only the understood half reads as though the whole ask
    # was matched, which is the misreading the shortfall below exists to stop.
    phrases = [ask.phrase for ask in request.skills]
    phrases += [
        f"{skill.phrase} (not a skill {PRODUCT_NAME} tracks)"
        for skill in request.unresolved_skills
    ]
    out.append(f"Team Builder — asked for: {', '.join(phrases) or '(no skill named)'}")
    if answer.window:
        start, end = answer.window
        out.append(
            f"Window checked: {start.isoformat()} (no dates were given, so only "
            "today was checked)"
            if start == end
            else f"Window checked: {start.isoformat()} to {end.isoformat()}"
        )
    if request.size is not None:
        out.append(f"Size asked for: {request.size}")
    out.append(_STATE_LINES[result.presentation_state])
    out.append("")

    if result.candidates:
        out.append(
            f"The group ({len(result.candidates)}), listed alphabetically — the "
            "order means nothing:"
        )
        for candidate in result.candidates:
            covers = (
                ", ".join(
                    item.name + (f" ({item.level})" if item.level else "")
                    for item in candidate.covers
                )
                or "none of the skills asked for"
            )
            line = (
                f"- {candidate.display_name} — confirmed: {covers}; "
                f"{_AVAILABILITY_WORDS[candidate.availability]}"
            )
            if candidate.back_on:
                line += f", back {candidate.back_on.isoformat()}"
            out.append(line)
        out.append("")

    out.append("Coverage of the ask:")
    if result.coverage:
        for item in result.coverage:
            who = (
                ", ".join(
                    answer.display_names.get(email, email) for email in item.covered_by
                )
                or "NOBODY in this group"
            )
            tag = "required" if item.required else "nice to have"
            out.append(f"- {item.name} ({tag}): {who}")
    else:
        out.append(f"- (no skill {PRODUCT_NAME} tracks was named in the ask)")
    out.append("")

    # Each group sentence prints ONCE, in its own section, in whichever wording
    # survived the screens — so the write-up below is per-person lines only and
    # the reader is never shown the same finding twice in two voices.
    written = {line.slot_id: line.text for line in answer.rationale}
    style_sentences = balance_sentences(
        result.balance, roster_size=len(result.candidates)
    )
    if style_sentences:
        out.append(
            "About the group's working styles — a finding about the GROUP, never "
            "a judgement of anybody in it:"
        )
        for index, sentence in enumerate(style_sentences):
            out.append(f"- {written.get(f'{SLOT_BALANCE}:{index}', sentence)}")
        out.append("")

    thin = dimension_notes(dimension_readiness(answer))
    if thin:
        out.append(f"What {PRODUCT_NAME} cannot read about this group:")
        for index, note in enumerate(thin):
            out.append(f"- {written.get(f'{SLOT_DIMENSIONS}:{index}', note)}")
        out.append("")

    if result.shortfalls:
        out.append("What is missing, and what would fix it:")
        for shortfall in result.shortfalls:
            out.append(f"- {shortfall.detail}")
        out.append("")

    if answer.notes:
        out.append("About this answer:")
        for note in answer.notes:
            out.append(f"- {note}")
        out.append("")

    # Everything printed above already: the shortfall details, the group's style
    # sentences, and what is not readable. What is left is the per-person lines —
    # which, by ``compose_rationale``, are ALL of the authored ones. Selecting
    # them by "not a group slot" rather than by a prefix list keeps that true if
    # a new per-person slot kind is added.
    write_up = [line for line in answer.rationale if names_a_person(line.slot_id)]
    if write_up:
        out.append(_WRITE_UP_HEADING)
        for line in write_up:
            out.append(f"- {line.text}")
        out.append("")

    out.append(team_contracts.TEAM_ADVISORY_NOTICE)
    out.append(SELECTION_NOTICE)
    out.append(SCOPE_NOTICE)
    out.append(AVAILABILITY_NOTICE)
    return "\n".join(out).strip()
