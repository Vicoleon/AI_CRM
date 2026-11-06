"""Smart Teams skills — the store, and the one rule that governs every read.

``st_person_skills`` holds two very different things in one table, and this
module is where the difference is enforced so no caller has to remember it:

* a **declared** or **manager** row is a claim somebody made. ``confirmed`` is
  True from the moment it is written — the act of declaring a skill IS the
  confirmation.
* an **inferred** row is a QUESTION the nightly sweep is asking. It is written
  ``confirmed = False`` and it stays there until its subject says yes.

**``confirmed`` is the only gate, and ``confirmed.is_(True)`` is the only
correct read.** ``source != "inferred"`` is wrong the moment somebody confirms
an inference, and the index ``ix_st_person_skills_user_confirmed`` exists so
the right predicate is also the cheap one. This is the skills half of the same
contract ``contracts.reportable`` holds for the profile layers: an unconfirmed
inference is not a fact, and the Team Builder (ST-9) must never staff a project
off one. Telling a project lead that somebody has expertise nobody ever claimed
is the worst thing this feature can do.

Denial is a TOMBSTONE, not a delete
-----------------------------------
Re-suggesting a skill somebody already rejected is the single most annoying
behaviour this feature could have, and a delete guarantees it: the next sweep
reads the same meeting, finds nothing on file, and asks again.

So a denial keeps the ``(person, skill)`` row — the one
``uq_st_person_skill`` allows — with ``confirmed = False``, ``level = NULL``,
and a marker inside the ``evidence`` JSON (:data:`EVIDENCE_STATUS_DENIED`).
:func:`upsert_inference` refuses to touch a denied row, so the denial survives
every later sweep without needing a second table or a migration. The prior
evidence is kept underneath the marker so the person can still see what was
rejected and a support admin can audit the decision.

Declaring a skill you previously denied clears the tombstone — an explicit
statement about yourself always beats an earlier rejection of somebody else's
guess — and the rejection is retained under
:data:`EVIDENCE_PRIOR_DENIAL_KEY` rather than dropped.

Never overwrite what a person said
----------------------------------
:func:`upsert_inference` may only ever APPEND evidence to a row that is already
``confirmed`` or already ``source = "declared"``. It never rewrites ``level``,
never flips ``confirmed`` back to False, and never demotes ``source``. An
inference that agrees with a declaration is corroboration, not a correction.

Visibility, decided here and stated once
----------------------------------------
Skills are less sensitive than a profile, but "less" is not "not", so the rule
is explicit rather than implied:

* **A person's own skills** — every bucket, including unconfirmed suggestions,
  denials, and all evidence — are visible to the subject and to support admins
  (``authz.can_view_raw_profile``). Evidence quotes meeting transcripts and
  document titles, which are ACL'd content in their own right, so the evidence
  never leaves that audience.
* **Somebody else's CONFIRMED skills** are visible to any employee who can use
  Smart Teams, WITHOUT evidence. Rationale: a confirmed skill is a claim the
  person themselves made or accepted, knowing the Team Builder reads it — the
  confirmation IS the consent, the same way ``approved_at`` is for a guide. It
  is a directory entry ("ask me about GDPR"), not a psychometric read.
* **Unconfirmed suggestions and denials are never visible to anyone but the
  subject and support admins.** A suggestion is a question we asked, and a denial is
  an answer that is nobody else's business.

"WITHOUT evidence" is a TYPE, not a convention
----------------------------------------------
That third bullet used to be enforced by nothing. :func:`confirmed_skills_for`
— the colleague-visible / Team Builder read — returned raw
:class:`~rubik_core.database.smart_teams.SmartTeamsPersonSkill` rows carrying
``evidence`` and ``evidence_ref``, and the docstring above promised a caller
would strip them. It would not have: the first serializer anybody writes over
an ORM row is ``jsonable_encoder(row)`` or a ``from_attributes`` model, and both
emit the whole row. The evidence cites meetings and documents the VIEWER may
have no right to — :mod:`rubik_core.smart_teams.skill_inference` gathers it as
the SUBJECT, so nothing in it has passed ``meeting_participants`` or a document
ACL on the way to a colleague's screen.

So the safe shape is now the ONLY shape that read can produce:

* :func:`confirmed_skills_for` returns frozen :class:`ConfirmedSkill` DTOs of
  primitives — ``skill_key``, ``name``, ``cluster``, ``level``, ``origin``,
  ``confirmed_at``. There is no evidence, no ``evidence_ref``, no rationale and
  no denial state on the object, so no serializer downstream can emit one.
  ``public_dict`` accepts that DTO and nothing else.
* the ORM rows stay behind :func:`subject_rows_for`, :func:`subject_row_for` and
  :func:`bucketed_for`, whose names say who may call them: the subject, or a
  support admin, gated by ``authz.can_view_raw_profile`` at the router.

ADVISORY ONLY. A skill row says what somebody can be asked about on a project.
Nothing here may be read as competence, potential, promotion-readiness, or
fitness for a role, and nothing here may be joined to a review or comp surface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func

from rubik_core.database.smart_teams import (
    SKILL_LEVELS,
    SmartTeamsPersonSkill,
    SmartTeamsSkill,
)

logger = logging.getLogger(__name__)

#: ``st_person_skills.source`` values, re-exported so a caller never types one.
SOURCE_DECLARED = "declared"
SOURCE_INFERRED = "inferred"
SOURCE_MANAGER = "manager"

#: Answer-key prefix the interview's skills section writes
#: (``interview.PREFIX_SKILL``). Re-stated rather than imported so the worker
#: does not pull the interview catalog in; a test pins the two together.
ANSWER_KEY_PREFIX = "skill:"

# ── The evidence envelope ─────────────────────────────────────────────────────
# ``st_person_skills.evidence`` is a free-form JSON column, which means the
# shape has to be a contract somewhere or every reader invents its own. These
# keys are it.

#: ``"declared"`` | ``"inferred"`` | :data:`EVIDENCE_STATUS_DENIED`.
EVIDENCE_STATUS_KEY = "status"
#: The tombstone. A row carrying this is never re-suggested and never reported
#: as a skill, whatever else it says.
EVIDENCE_STATUS_DENIED = "denied"
#: The citation list: ``[{"kind", "ref", "title", "when", "excerpt", ...}]``.
EVIDENCE_ITEMS_KEY = "items"
#: What the model said it concluded, screened for advisory language before it
#: was ever stored.
EVIDENCE_RATIONALE_KEY = "rationale"
#: sha256 over the citation refs this row was last written from. An unchanged
#: source therefore costs one string comparison and no write.
EVIDENCE_SOURCE_HASH_KEY = "source_hash"
#: ISO timestamp + actor of a denial.
EVIDENCE_DENIED_AT_KEY = "denied_at"
EVIDENCE_DENIED_BY_KEY = "denied_by"
#: A denial that a later self-declaration overrode. Kept for audit.
EVIDENCE_PRIOR_DENIAL_KEY = "prior_denial"

#: Set when :func:`deny` created the row itself, with nothing having suggested or
#: declared the skill first. It marks the row as a pure tombstone.
#:
#: Needed because ``deny`` legitimately creates rows (a pre-emptive "I do not
#: have this" is as durable as a reactive one) and ``confirm`` only checks that a
#: row EXISTS. So deny-then-confirm composed into a confirmed, colleague-visible
#: skill on a key nothing had ever suggested, labelled ``inferred`` — while the
#: confirm docstring said that could not happen. The consequence is not
#: escalation, since a person may declare skills about themselves; it is that
#: ``origin`` is documented as what a project lead weighs ("they told us" versus
#: "Cubie suggested it and they agreed"), and that path published the wrong one
#: with no evidence behind it.
EVIDENCE_PRE_EMPTIVE_KEY = "pre_emptive_denial"

#: Refusal copy, next to the rule, so every surface refuses in the same words.
SUBJECT_ONLY_DENIED = (
    "Only you can confirm or change your own skills — a colleague can see what "
    "you have confirmed, and nothing else."
)
UNKNOWN_SKILL_DENIED = (
    "That skill is not in the curated skills list, so the Team Builder could "
    "never match on it. Ask an admin to add it and it becomes declarable for "
    "everyone at once."
)

#: How many citations one row keeps. A skill mentioned in forty meetings is not
#: forty times truer, and the confirm prompt shows the evidence — an unbounded
#: list would make that prompt unreadable and the JSON column large.
MAX_EVIDENCE_ITEMS = 6


class SkillError(Exception):
    """A skill write that must not be attempted — unknown key, bad level."""


def fold(email: object) -> str:
    """Lower-cased address, or ``""``. ``users.email`` is stored verbatim."""
    return str(email or "").strip().lower()


def _now(now: datetime | None) -> datetime:
    return now or datetime.utcnow()


# ── Reading a row ─────────────────────────────────────────────────────────────


def evidence_of(row: SmartTeamsPersonSkill | None) -> dict:
    payload = getattr(row, "evidence", None)
    return dict(payload) if isinstance(payload, Mapping) else {}


def is_denied(row: SmartTeamsPersonSkill | None) -> bool:
    """True iff this row is a denial tombstone.

    The one predicate every write path consults. Read from the evidence
    envelope rather than from ``source``/``confirmed``, because a denied row is
    indistinguishable from a merely-unconfirmed one in those two columns and
    conflating them is precisely the re-suggestion bug.
    """
    return evidence_of(row).get(EVIDENCE_STATUS_KEY) == EVIDENCE_STATUS_DENIED


def evidence_items(row: SmartTeamsPersonSkill | None) -> list[dict]:
    items = evidence_of(row).get(EVIDENCE_ITEMS_KEY)
    return [dict(item) for item in items if isinstance(item, Mapping)] if items else []


def source_hash(row: SmartTeamsPersonSkill | None) -> str | None:
    value = evidence_of(row).get(EVIDENCE_SOURCE_HASH_KEY)
    return value if isinstance(value, str) and value else None


def hash_evidence(items: Sequence[Mapping[str, object]]) -> str:
    """Stable digest of a citation set — what "the same source" means here.

    Only ``kind`` and ``ref`` go in: an excerpt that shifts by a character
    because a transcript was re-ingested is the same evidence, and re-writing
    the row (and re-billing an LLM call) for that is exactly the churn the
    watermark exists to avoid. Sorted, so the reader's ordering is irrelevant.
    """
    refs = sorted(
        f"{item.get('kind')}:{item.get('ref')}"
        for item in items
        if item.get("ref") is not None
    )
    return hashlib.sha256(json.dumps(refs, sort_keys=True).encode()).hexdigest()


# ── Reading a person ──────────────────────────────────────────────────────────


def subject_rows_for(db, user_email: str) -> list[SmartTeamsPersonSkill]:
    """Every ORM row for one person, including denials and all evidence.

    SUBJECT OR SYSTEMS ONLY. The rows carry ``evidence`` and ``evidence_ref``,
    which cite meetings and documents gathered as the SUBJECT — so a
    caller must have cleared ``authz.can_view_raw_profile`` before it gets here.
    The name says the audience because the previous name (``rows_for``) said
    nothing, and a colleague-facing caller reaching for the obvious-looking
    function is exactly how the evidence leaked.
    """
    email = fold(user_email)
    if not email:
        return []
    return (
        db.query(SmartTeamsPersonSkill)
        .filter(func.lower(SmartTeamsPersonSkill.user_email) == email)
        .order_by(SmartTeamsPersonSkill.skill_key.asc())
        .all()
    )


def subject_row_for(
    db, user_email: str, skill_key: str
) -> SmartTeamsPersonSkill | None:
    """One ORM row, by (person, skill). SUBJECT OR SYSTEMS ONLY, as above.

    Also the lookup every write path in this module uses, which is the second
    reason it is not a colleague-facing read: a caller holding this row can
    mutate somebody's claims.
    """
    email = fold(user_email)
    if not email or not skill_key:
        return None
    return (
        db.query(SmartTeamsPersonSkill)
        .filter(func.lower(SmartTeamsPersonSkill.user_email) == email)
        .filter(SmartTeamsPersonSkill.skill_key == skill_key)
        .one_or_none()
    )


#: The complete field set a colleague may see. An allowlist rather than a
#: denylist, and a module constant rather than a comment, so
#: ``test_smart_teams_skills.py`` can pin :class:`ConfirmedSkill` to it — a field
#: added to the DTO without a deliberate decision fails a test instead of
#: shipping to every employee.
COLLEAGUE_VISIBLE_FIELDS: frozenset[str] = frozenset(
    {"skill_key", "name", "cluster", "level", "origin", "confirmed_at"}
)


@dataclass(frozen=True)
class ConfirmedSkill:
    """One confirmed skill as a COLLEAGUE sees it. Primitives only, no evidence.

    The whole point of this class is what it does NOT have. There is no
    ``evidence``, no ``evidence_ref``, no rationale, no denial state and no
    reference to the ORM row, so a colleague-facing serializer —
    ``jsonable_encoder``, a Pydantic ``from_attributes`` model, ``{**vars(x)}``
    — cannot emit an ACL'd meeting title or a transcript excerpt even by
    accident. That was reachable while this read returned ORM rows.

    ``level`` and ``origin`` are here because a project lead reasonably weighs
    "they told us" against "Cubie suggested it and they agreed"; neither is
    evidence, and both are already what ``public_dict`` published.
    ``confirmed_at`` is an ISO string rather than a ``datetime`` so nothing
    lazy-loading rides along with it.
    """

    skill_key: str
    name: str
    cluster: str | None
    level: str | None
    origin: str
    confirmed_at: str | None


def _confirmed_skill(person_skill, taxonomy) -> ConfirmedSkill:
    """Project one (row, taxonomy) pair down to the colleague-visible fields."""
    return ConfirmedSkill(
        skill_key=person_skill.skill_key,
        name=getattr(taxonomy, "name", None) or person_skill.skill_key,
        cluster=getattr(taxonomy, "cluster", None),
        level=person_skill.level,
        origin=person_skill.source,
        confirmed_at=(
            person_skill.confirmed_at.isoformat() if person_skill.confirmed_at else None
        ),
    )


@dataclass(frozen=True, slots=True)
class SkillHolder:
    """One person who has confirmed one skill, as a COLLEAGUE sees them.

    The mirror of :class:`ConfirmedSkill` and constrained the same way, for the
    same reason: primitives only, no ``evidence``, no ``evidence_ref``, no
    rationale, no ORM row. A skill-to-people read is the direction a project lead
    actually asks in — "who can do contract review" rather than "what can Casey
    do" — and it fans out over the whole roster instead of one person, so a
    serializer accident here would leak an ACL'd meeting title about everybody at
    once rather than about one colleague.

    ``level`` and ``origin`` are here because they are already what the
    person-to-skills read publishes, and a lead reasonably weighs "they told us"
    against "Cubie suggested it and they agreed". Neither is evidence.
    """

    email: str
    display_name: str
    level: str | None
    origin: str
    confirmed_at: str | None

    def public_dict(self) -> dict[str, object]:
        return {
            "email": self.email,
            "display_name": self.display_name,
            "level": self.level,
            "origin": self.origin,
            "confirmed_at": self.confirmed_at,
        }


def claim_counts(db) -> dict[str, int]:
    """``{skill_key: rows}`` over ``st_person_skills``, all states.

    Counts confirmed claims, unanswered suggestions and denial tombstones
    together on purpose. The one caller is the curation screen, and the question
    it is answering is "can this row ever go away" — the RESTRICT foreign key
    does not care which state the referencing rows are in.
    """
    from sqlalchemy import func

    rows = (
        db.query(
            SmartTeamsPersonSkill.skill_key,
            func.count(SmartTeamsPersonSkill.id),
        )
        .group_by(SmartTeamsPersonSkill.skill_key)
        .all()
    )
    return {key: int(count) for key, count in rows}


def holders_of(
    db,
    skill_keys: Iterable[str],
    *,
    roster: Mapping[str, str] | None = None,
) -> dict[str, tuple[SkillHolder, ...]]:
    """Who has confirmed each of ``skill_keys``. The inverse of
    :func:`confirmed_skills_for`.

    ``confirmed.is_(True)`` and nothing else, exactly as the person-to-skills
    read does — so a denial tombstone (``confirmed = False``) is excluded by the
    same predicate, and an unconfirmed suggestion never appears. Cubie guessing
    that somebody knows something is not a reason to put their name in front of a
    project lead; only their own confirmation is.

    ``roster`` is the set of people who may be named, passed in rather than
    derived here. The caller supplies ``team_builder.proposable_people(db)``,
    which is the documented "is this a current employee" rule in this codebase —
    a ``users`` row plus :func:`authz.is_employee`. Passing it in rather than
    importing it avoids a cycle (``team_builder`` reads this module), and it
    keeps ONE definition of who is proposable instead of a second one that would
    eventually disagree. Omitting it returns every holder unfiltered, which is
    correct for a support caller and wrong for a colleague — so the route passes
    it.

    Every requested key gets an entry, so "nobody has confirmed this" stays
    distinguishable from "we did not ask about it". Holders are sorted by display
    name: any ordering by level or recency would read as a ranking of people,
    which this feature does not do.
    """
    wanted = [
        k for k in dict.fromkeys((k or "").strip() for k in skill_keys or []) if k
    ]
    if not wanted:
        return {}

    rows = (
        db.query(SmartTeamsPersonSkill)
        .filter(
            SmartTeamsPersonSkill.skill_key.in_(wanted),
            SmartTeamsPersonSkill.confirmed.is_(True),
        )
        .all()
    )

    out: dict[str, list[SkillHolder]] = {key: [] for key in wanted}
    for row in rows:
        email = fold(row.user_email)
        if roster is not None and email not in roster:
            # Skills outlive employment: st_person_skills is not touched when a
            # users row is removed, so without this an ex-colleague keeps being
            # offered for live work. team_builder logs the same condition loudly;
            # this read stays quiet because it is a lookup, not a proposal.
            continue
        display = (roster or {}).get(email) or email
        out.setdefault(row.skill_key, []).append(
            SkillHolder(
                email=email,
                display_name=display,
                level=row.level,
                origin=row.source,
                confirmed_at=(
                    row.confirmed_at.isoformat() if row.confirmed_at else None
                ),
            )
        )
    return {
        key: tuple(sorted(holders, key=lambda h: (h.display_name.lower(), h.email)))
        for key, holders in out.items()
    }


def confirmed_skills_for(
    db, emails: Iterable[str]
) -> dict[str, tuple[ConfirmedSkill, ...]]:
    """The colleague-visible / Team Builder read: confirmed skills, no evidence.

    Returns :class:`ConfirmedSkill` DTOs, never ORM rows — see "WITHOUT evidence
    is a TYPE" in the module docstring. This is the one read a person may run
    against somebody else, so the projection happens HERE rather than in a
    serializer a caller has to remember to use.

    ``confirmed.is_(True)`` and nothing else. A denial tombstone is
    ``confirmed = False`` so it is excluded by the same predicate, which is the
    whole reason the tombstone lives in that state rather than in a flag of its
    own.

    Every requested address gets a key, so "this person has confirmed nothing"
    and "we did not look them up" stay distinguishable to the caller.
    """
    wanted = [fold(e) for e in emails or []]
    wanted = [e for e in dict.fromkeys(wanted) if e]
    if not wanted:
        return {}
    rows = (
        db.query(SmartTeamsPersonSkill, SmartTeamsSkill)
        .join(
            SmartTeamsSkill,
            SmartTeamsSkill.skill_key == SmartTeamsPersonSkill.skill_key,
        )
        .filter(func.lower(SmartTeamsPersonSkill.user_email).in_(wanted))
        .filter(SmartTeamsPersonSkill.confirmed.is_(True))
        .order_by(SmartTeamsSkill.cluster.asc(), SmartTeamsSkill.name.asc())
        .all()
    )
    out: dict[str, list[ConfirmedSkill]] = {email: [] for email in wanted}
    for person_skill, taxonomy in rows:
        out.setdefault(fold(person_skill.user_email), []).append(
            _confirmed_skill(person_skill, taxonomy)
        )
    return {email: tuple(items) for email, items in out.items()}


@dataclass(frozen=True)
class PersonSkills:
    """One person's skills, bucketed the way both the API and the UI need."""

    user_email: str
    confirmed: tuple = ()
    suggested: tuple = ()
    denied: tuple = ()


def bucketed_for(db, user_email: str) -> PersonSkills:
    """Split one person's rows into confirmed / suggested / denied.

    SUBJECT OR SYSTEMS ONLY — every bucket holds ORM rows with their evidence,
    and two of the three buckets (suggested, denied) are things no colleague may
    see at all. :func:`confirmed_skills_for` is the read for anybody else.

    Three buckets rather than a flag on a flat list because they mean different
    things to the reader: confirmed is what the company can see, suggested is a
    question waiting on them, denied is a decision they already made (shown so
    they can change their mind, and so a denial never looks like data loss).
    """
    email = fold(user_email)
    rows = subject_rows_for(db, email)
    taxonomy = {
        row.skill_key: row
        for row in db.query(SmartTeamsSkill)
        .filter(SmartTeamsSkill.skill_key.in_([r.skill_key for r in rows] or [""]))
        .all()
    }
    confirmed, suggested, denied = [], [], []
    for row in rows:
        pair = (row, taxonomy.get(row.skill_key))
        if is_denied(row):
            denied.append(pair)
        elif bool(row.confirmed):
            confirmed.append(pair)
        else:
            suggested.append(pair)
    return PersonSkills(
        user_email=email,
        confirmed=tuple(confirmed),
        suggested=tuple(suggested),
        denied=tuple(denied),
    )


# ── Declaring ─────────────────────────────────────────────────────────────────


def declarations_from_answers(answers: Mapping[str, object] | None) -> dict[str, str]:
    """Pull ``{"skill:<key>": "<level>"}`` out of a saved interview section.

    The interview stores declared skills in ``st_interviews.answers`` under the
    ``skill:`` prefix (``interview.PREFIX_SKILL``); nothing materialised them
    into ``st_person_skills`` before ST-8, so this is the projection.

    A key present with a NULL value is a skill the person opened and then left
    blank — the interview allows that, and it is not a declaration. Dropped
    here rather than stored as a level-less claim.
    """
    out: dict[str, str] = {}
    for key, value in (answers or {}).items():
        if not isinstance(key, str) or not key.startswith(ANSWER_KEY_PREFIX):
            continue
        skill_key = key[len(ANSWER_KEY_PREFIX) :].strip().lower()
        if not skill_key or not isinstance(value, str):
            continue
        level = value.strip().lower()
        if level not in SKILL_LEVELS:
            continue
        out[skill_key] = level
    return out


@dataclass(frozen=True)
class DeclareResult:
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    #: Tombstones a self-declaration overrode, reported so the response can say
    #: "you had said no to this" rather than silently reversing it.
    undenied: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "created": list(self.created),
            "updated": list(self.updated),
            "unchanged": list(self.unchanged),
            "undenied": list(self.undenied),
        }


def record_declared_skills(
    db,
    user_email: str,
    declarations: Mapping[str, str | None],
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> DeclareResult:
    """Write a person's own declarations. ``confirmed = True``, by definition.

    Refuses (``SkillError``) any key outside the ACTIVE taxonomy. That is the
    same rule ``interview._validate_skill`` applies on the way in, and it is a
    hard refusal rather than a "pending taxonomy request" for a stated reason:
    there is no table to hold a request (adding one is a migration ST-8 does not
    own), and a row pointing at a skill the Team Builder cannot match on is a
    claim that silently does nothing. The refusal copy
    (:data:`UNKNOWN_SKILL_DENIED`) names the path forward instead.

    Never removes anything. A person retracting a skill uses :func:`deny`,
    which leaves a tombstone — so "I un-ticked it" and "I never ticked it" stay
    distinguishable, and a sweep cannot resurrect the retraction.
    """
    email = fold(user_email)
    if not email:
        raise SkillError("a declared skill needs a subject")
    stamp = _now(now)

    active = {
        key
        for (key,) in db.query(SmartTeamsSkill.skill_key)
        .filter(SmartTeamsSkill.is_active.is_(True))
        .all()
    }
    unknown = sorted(k for k in declarations if k not in active)
    if unknown:
        raise SkillError(f"{UNKNOWN_SKILL_DENIED} ({', '.join(unknown)})")

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    undenied: list[str] = []

    for skill_key, raw_level in declarations.items():
        level = (raw_level or "").strip().lower() or None
        if level is not None and level not in SKILL_LEVELS:
            raise SkillError(f"{skill_key}: {raw_level!r} is not a skill level")

        row = subject_row_for(db, email, skill_key)
        if row is None:
            db.add(
                SmartTeamsPersonSkill(
                    id=uuid.uuid4().hex,
                    user_email=email,
                    skill_key=skill_key,
                    level=level,
                    source=SOURCE_DECLARED,
                    confirmed=True,
                    confirmed_at=stamp,
                    evidence={EVIDENCE_STATUS_KEY: SOURCE_DECLARED},
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            created.append(skill_key)
            continue

        payload = evidence_of(row)
        was_denied = is_denied(row)
        if was_denied:
            # An explicit statement about yourself beats an earlier rejection
            # of somebody else's guess. The rejection is kept, not dropped.
            payload[EVIDENCE_PRIOR_DENIAL_KEY] = {
                EVIDENCE_DENIED_AT_KEY: payload.pop(EVIDENCE_DENIED_AT_KEY, None),
                EVIDENCE_DENIED_BY_KEY: payload.pop(EVIDENCE_DENIED_BY_KEY, None),
            }
            undenied.append(skill_key)
        payload[EVIDENCE_STATUS_KEY] = SOURCE_DECLARED

        changed = was_denied or row.level != level or row.source != SOURCE_DECLARED
        if not bool(row.confirmed):
            changed = True
        row.level = level
        # A declaration outranks an inference: the source becomes "declared"
        # and any evidence the sweep gathered stays attached as corroboration.
        row.source = SOURCE_DECLARED
        row.confirmed = True
        row.confirmed_at = row.confirmed_at or stamp
        row.evidence = payload
        if changed:
            row.updated_at = stamp
            updated.append(skill_key)
        else:
            unchanged.append(skill_key)

    if commit:
        db.commit()
    return DeclareResult(
        created=tuple(sorted(created)),
        updated=tuple(sorted(updated)),
        unchanged=tuple(sorted(unchanged)),
        undenied=tuple(sorted(undenied)),
    )


# ── Confirm / deny ────────────────────────────────────────────────────────────


def confirm(
    db, user_email: str, skill_key: str, *, now: datetime | None = None
) -> SmartTeamsPersonSkill:
    """The person says yes to a suggestion. One click, idempotent.

    ``source`` is deliberately left as ``"inferred"``: the row's history is that
    Cubie suggested it, and rewriting the provenance on confirmation would erase
    the only record of where it came from. ``confirmed`` is what every read
    switches on, which is why this is safe — and why reading "confirmed" as
    ``source != "inferred"`` would be wrong.
    """
    row = subject_row_for(db, user_email, skill_key)
    if row is None:
        raise SkillError(f"nothing to confirm: no {skill_key!r} row for this person")
    stamp = _now(now)
    payload = evidence_of(row)
    if payload.get(EVIDENCE_PRE_EMPTIVE_KEY) and not payload.get(EVIDENCE_ITEMS_KEY):
        # The row exists only because the person pre-emptively denied a skill
        # nothing had suggested. Confirming it would publish an "inferred" skill
        # with no inference and no evidence behind it. Declaring is the right
        # verb for "I do have this after all", and it records the honest origin.
        raise SkillError(
            f"{skill_key!r} was never suggested for this person — it exists only "
            "as a pre-emptive denial. Declare it instead, so its origin reads as "
            "declared rather than inferred."
        )
    if is_denied(row):
        # Changing your mind is allowed; the denial moves to the audit key so
        # the row does not read as both denied and confirmed.
        payload[EVIDENCE_PRIOR_DENIAL_KEY] = {
            EVIDENCE_DENIED_AT_KEY: payload.pop(EVIDENCE_DENIED_AT_KEY, None),
            EVIDENCE_DENIED_BY_KEY: payload.pop(EVIDENCE_DENIED_BY_KEY, None),
        }
    payload[EVIDENCE_STATUS_KEY] = row.source or SOURCE_INFERRED
    row.evidence = payload
    if not bool(row.confirmed):
        row.confirmed = True
        row.confirmed_at = stamp
        row.updated_at = stamp
    db.commit()
    return row


def deny(
    db,
    user_email: str,
    skill_key: str,
    *,
    now: datetime | None = None,
    denied_by: str | None = None,
) -> SmartTeamsPersonSkill:
    """The person says no. The refusal has to STICK across every later sweep.

    Writes the tombstone described in the module docstring: the ``(person,
    skill)`` row stays, ``confirmed`` goes False, ``level`` goes NULL, and the
    evidence envelope carries :data:`EVIDENCE_STATUS_DENIED` plus who denied it
    and when. :func:`upsert_inference` reads that marker first and returns
    without writing, so the next sweep reading the same meeting cannot ask
    again.

    Also the retraction path for a declared skill — one mechanism, so there is
    no second way to remove a skill that a sweep would not know about.

    Creates the row if the sweep has not written one yet (denying a skill you
    know you do not have is legitimate, and a pre-emptive tombstone is exactly
    as durable as a reactive one). The key must exist in the taxonomy — active
    or retired — because ``st_person_skills`` holds a RESTRICT foreign key.
    """
    email = fold(user_email)
    if not email:
        raise SkillError("a denial needs a subject")
    stamp = _now(now)
    exists = db.get(SmartTeamsSkill, skill_key) is not None
    if not exists:
        raise SkillError(UNKNOWN_SKILL_DENIED)

    row = subject_row_for(db, email, skill_key)
    pre_emptive = row is None
    if row is None:
        row = SmartTeamsPersonSkill(
            id=uuid.uuid4().hex,
            user_email=email,
            skill_key=skill_key,
            level=None,
            source=SOURCE_INFERRED,
            confirmed=False,
            evidence={},
            created_at=stamp,
            updated_at=stamp,
        )
        db.add(row)

    payload = evidence_of(row)
    if pre_emptive:
        # Nothing suggested this. Recorded so ``confirm`` cannot later treat the
        # tombstone as a suggestion the person agreed to.
        payload[EVIDENCE_PRE_EMPTIVE_KEY] = True
    payload[EVIDENCE_STATUS_KEY] = EVIDENCE_STATUS_DENIED
    payload[EVIDENCE_DENIED_AT_KEY] = stamp.isoformat()
    payload[EVIDENCE_DENIED_BY_KEY] = fold(denied_by) or email
    row.evidence = payload
    row.confirmed = False
    row.confirmed_at = None
    row.level = None
    row.updated_at = stamp
    db.commit()
    return row


# ── Writing an inference ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Suggestion:
    """One evidence-linked skill suggestion, ready to be written.

    ``items`` is never empty by construction — :meth:`__post_init__` refuses
    that. A suggestion with no citation is a confident sentence built on
    nothing, and it is the person's own name attached to it.
    """

    user_email: str
    skill_key: str
    level: str | None = None
    rationale: str = ""
    items: tuple[dict, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not fold(self.user_email):
            raise SkillError("a suggestion needs a subject")
        if not self.skill_key:
            raise SkillError("a suggestion needs a skill_key")
        if self.level is not None and self.level not in SKILL_LEVELS:
            raise SkillError(f"{self.level!r} is not a skill level")
        if not self.items:
            raise SkillError(
                f"{self.skill_key} for {self.user_email}: a suggestion with no "
                "evidence must never be written — the person has to be able to "
                "see what we read and why we concluded it"
            )
        for item in self.items:
            if not item.get("kind") or not item.get("ref"):
                raise SkillError(
                    f"{self.skill_key}: every evidence item needs a kind and a "
                    f"ref that identifies the source; got {item!r}"
                )

    @property
    def evidence_ref(self) -> str:
        """The single stable id the column holds — the first citation's ref."""
        return str(self.items[0].get("ref") or "")

    @property
    def digest(self) -> str:
        return hash_evidence(self.items)


#: What :func:`upsert_inference` did. A closed vocabulary because the sweep
#: report counts these and a free-text outcome would make that a string match.
WROTE_CREATED = "created"
WROTE_UPDATED = "updated"
WROTE_UNCHANGED = "unchanged"
WROTE_APPENDED = "evidence_appended"
SKIPPED_DENIED = "skipped_denied"

INFERENCE_OUTCOMES: tuple[str, ...] = (
    WROTE_CREATED,
    WROTE_UPDATED,
    WROTE_UNCHANGED,
    WROTE_APPENDED,
    SKIPPED_DENIED,
)


def _merge_items(
    existing: Sequence[Mapping], incoming: Sequence[Mapping]
) -> list[dict]:
    """Newest citations first, de-duplicated on ``(kind, ref)``, bounded."""
    merged: dict[tuple, dict] = {}
    for item in [*incoming, *existing]:
        key = (item.get("kind"), item.get("ref"))
        if key not in merged:
            merged[key] = dict(item)
    return list(merged.values())[:MAX_EVIDENCE_ITEMS]


def upsert_inference(
    db,
    suggestion: Suggestion,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> str:
    """Write one suggestion, honouring everything the person already said.

    The rules, in the order they are checked:

    1. **A denial wins.** A tombstoned row is left exactly as it is and
       :data:`SKIPPED_DENIED` is returned. This is what makes "denied" stick
       across sweeps.
    2. **A person's own words win.** On a row that is already ``confirmed`` or
       already ``source = "declared"``, only the evidence is appended: the
       level, the source and the confirmation are untouched. An inference that
       agrees with a declaration is corroboration, not a correction.
    3. **An unchanged source writes nothing.** If the stored
       :data:`EVIDENCE_SOURCE_HASH_KEY` matches this suggestion's digest, the
       row is left alone (:data:`WROTE_UNCHANGED`) rather than having its
       ``updated_at`` churned every night.
    4. Otherwise the pending suggestion is refreshed in place —
       ``confirmed = False``, because it is still a question.
    """
    stamp = _now(now)
    email = fold(suggestion.user_email)
    row = subject_row_for(db, email, suggestion.skill_key)

    if row is not None and is_denied(row):
        logger.debug(
            "[smart-teams][skills] %s denied %s previously — not re-suggesting",
            email,
            suggestion.skill_key,
        )
        return SKIPPED_DENIED

    if row is None:
        db.add(
            SmartTeamsPersonSkill(
                id=uuid.uuid4().hex,
                user_email=email,
                skill_key=suggestion.skill_key,
                level=suggestion.level,
                source=SOURCE_INFERRED,
                confirmed=False,
                confirmed_at=None,
                evidence_ref=suggestion.evidence_ref or None,
                evidence={
                    EVIDENCE_STATUS_KEY: SOURCE_INFERRED,
                    EVIDENCE_ITEMS_KEY: list(suggestion.items)[:MAX_EVIDENCE_ITEMS],
                    EVIDENCE_RATIONALE_KEY: suggestion.rationale,
                    EVIDENCE_SOURCE_HASH_KEY: suggestion.digest,
                },
                created_at=stamp,
                updated_at=stamp,
            )
        )
        if commit:
            db.commit()
        return WROTE_CREATED

    payload = evidence_of(row)
    if payload.get(EVIDENCE_SOURCE_HASH_KEY) == suggestion.digest:
        return WROTE_UNCHANGED

    merged = _merge_items(evidence_items(row), list(suggestion.items))
    payload[EVIDENCE_ITEMS_KEY] = merged
    payload[EVIDENCE_SOURCE_HASH_KEY] = suggestion.digest

    if bool(row.confirmed) or row.source == SOURCE_DECLARED:
        # Corroboration only. Nothing the person asserted is rewritten, and the
        # rationale is filed separately so a model sentence can never be read
        # as the reason a person declared something about themselves.
        corroboration = payload.setdefault("corroboration", [])
        if suggestion.rationale and suggestion.rationale not in corroboration:
            corroboration.append(suggestion.rationale)
        row.evidence = payload
        row.evidence_ref = row.evidence_ref or (suggestion.evidence_ref or None)
        row.updated_at = stamp
        if commit:
            db.commit()
        return WROTE_APPENDED

    payload[EVIDENCE_STATUS_KEY] = SOURCE_INFERRED
    payload[EVIDENCE_RATIONALE_KEY] = suggestion.rationale
    row.evidence = payload
    row.evidence_ref = suggestion.evidence_ref or row.evidence_ref
    row.level = suggestion.level or row.level
    row.source = SOURCE_INFERRED
    row.confirmed = False
    row.confirmed_at = None
    row.updated_at = stamp
    if commit:
        db.commit()
    return WROTE_UPDATED


# ── Serialization for the API ─────────────────────────────────────────────────


def public_dict(skill: ConfirmedSkill) -> dict:
    """A confirmed skill as a COLLEAGUE sees it. No evidence, ever.

    Takes a :class:`ConfirmedSkill` and NOT the ORM row, deliberately. When this
    accepted ``(person_skill, taxonomy)`` it was one keystroke away from
    ``{**public_dict(row, tax), **vars(row)}``, and every field it declined to
    publish was still sitting in the argument. Now the caller cannot reach the
    evidence, because the object it was handed never had any.
    """
    return {
        "skill_key": skill.skill_key,
        "name": skill.name,
        "cluster": skill.cluster,
        "level": skill.level,
        "confirmed": True,
        "confirmed_at": skill.confirmed_at,
        # Kept because "they told us" and "Cubie suggested it and they agreed"
        # are different things a project lead may reasonably weigh.
        "origin": skill.origin,
    }


def private_dict(person_skill, taxonomy) -> dict:
    """A skill as its SUBJECT (or a support admin) sees it — evidence included.

    Built from the ORM row directly rather than by spreading
    :func:`public_dict`, so the two serializers no longer share a code path:
    the colleague one takes a DTO with no evidence on it, and this one takes the
    row. Never reachable from a colleague-facing endpoint — the router gates it
    on ``authz.can_view_raw_profile``.
    """
    payload = evidence_of(person_skill)
    return {
        "skill_key": person_skill.skill_key,
        "name": getattr(taxonomy, "name", None) or person_skill.skill_key,
        "cluster": getattr(taxonomy, "cluster", None),
        "level": person_skill.level,
        "origin": person_skill.source,
        "confirmed": bool(person_skill.confirmed),
        "confirmed_at": (
            person_skill.confirmed_at.isoformat() if person_skill.confirmed_at else None
        ),
        "denied": is_denied(person_skill),
        "denied_at": payload.get(EVIDENCE_DENIED_AT_KEY),
        "rationale": payload.get(EVIDENCE_RATIONALE_KEY) or "",
        "evidence": evidence_items(person_skill),
        "evidence_ref": person_skill.evidence_ref,
    }
