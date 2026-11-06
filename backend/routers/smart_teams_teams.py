"""Smart Teams — the Team Builder API (ST-9).

Two endpoints: ask for a group, and read the skill vocabulary you may ask with.
Both are synchronous — the whole answer is a handful of indexed queries plus at
most one Gemini call, there is nothing to persist, and a page that has to poll
for a staffing suggestion is a page nobody uses.

## The gate is BOTH gates, and plan.md is why

plan.md Decision #4 reads: *"Interview-only gate. Every company Workspace user
can take the interview and manage/view guides. **Compare + Team Builder stay
behind the existing full-app gate.**"* Its Access section says the same thing in
implementation terms, and ``backend/main.py``'s Smart Teams block already
predicted this router: *"Comparison and Team Builder (ST-7/ST-9) are pilot work
and will stack ``require_full_app_access`` on top of this gate on their own
router — NOT by moving this one."*

So this router declares BOTH dependencies, in that order:

* ``require_smart_teams_access`` — the company boundary. Off-domain identities
  and the department-impersonation pseudo-user are refused here, and refused
  first, because they are refused everywhere in this feature.
* ``require_full_app_access`` — the copilot roster. Team Builder reads several
  colleagues' published guides and their confirmed skills in one answer, which
  is a different kind of read from "manage my own profile", and it is the read
  the product scoped to the copilot.

Stacking rather than swapping matters: swapping would let an off-domain caller
who happens to be an admin through the Smart Teams boundary, and moving the mount
under ``core`` would take the interview with it.

## What this router does not decide

* **Who may be proposed** is ``rubik_core.smart_teams.team_builder``, and
  underneath it ``authz.is_guide_published`` and ``contracts.layer_of``. Nothing
  here reads a profile payload, compares a ``published`` flag, or filters a
  roster.
* **What may be said** is ``team_prompts``' screens. Nothing here writes a
  sentence about a person.
* **Who the caller is** is ``get_current_user``, only. There is deliberately no
  ``requested_by`` / ``on_behalf_of`` field on the request body: this feature
  holds people's private interview answers, so an accepted address would be a
  plain impersonation primitive over it, and a Team Builder run is also an
  audit-relevant read of other people's data.

## Advisory only

A team suggestion is about coverage, availability and the group's mix of working
styles. It is never a ranking of colleagues, never an assessment of anybody's
ability, and nothing on this surface may be wired to performance review,
promotion, or compensation. Every response carries
``team_contracts.TEAM_ADVISORY_NOTICE`` for that reason, and the notice rides the
payload rather than the frontend so it cannot silently go missing.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import require_full_app_access, require_smart_teams_access
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.smart_teams import (
    heatmap,
    skill_taxonomy,
    team_builder,
    team_contracts,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/smart-teams/teams",
    tags=["smart-teams"],
    dependencies=[
        Depends(require_smart_teams_access),
        Depends(require_full_app_access),
    ],
)


class TeamAskBody(BaseModel):
    """One Team Builder ask.

    ``skills`` are free-text phrases and they are resolved EXACTLY, through
    ``skill_taxonomy.resolve``. A phrase that does not resolve is reported back
    as unrecognised — never coerced onto a neighbouring skill, which is how
    somebody ends up staffed onto work they have never touched.

    There is no ``requested_by``. See the module docstring.
    """

    skills: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    size: int | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    style_mix: str = team_contracts.STYLE_MIX_UNSPECIFIED
    clusters: list[str] = Field(default_factory=list)
    ask: str = ""


def _request_from(body: TeamAskBody, *, requester: str) -> team_contracts.TeamRequest:
    """Parse the body into a :class:`team_contracts.TeamRequest`, or 400.

    Every rejection here is a ``TeamError`` from the contract — an out-of-range
    size, a window that ends before it starts, an unknown strengths cluster — and
    it is reported as a 400 with the contract's own sentence rather than a
    generic one. The contract wrote those messages for a reader.
    """
    phrases = [p for p in (body.skills or []) if str(p).strip()]
    optional = [p for p in (body.nice_to_have or []) if str(p).strip()]
    if not phrases and not optional:
        raise HTTPException(
            status_code=400,
            detail=(
                "Name at least one skill the work needs. Team Builder matches "
                "against the skills people have confirmed in Smart Teams; it "
                "cannot assemble a group from a project description alone."
            ),
        )
    if len(phrases) + len(optional) > team_builder.MAX_REQUESTED_SKILLS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"That names more than {team_builder.MAX_REQUESTED_SKILLS} "
                "skills. Covering each one takes at least one person, so this "
                "would only ever be answerable by a group too large to propose — "
                "ask for the skills that actually gate the work."
            ),
        )
    try:
        return team_contracts.TeamRequest.from_phrases(
            requested_by=requester,
            required_phrases=phrases,
            optional_phrases=optional,
            size=body.size,
            starts_on=body.starts_on,
            ends_on=body.ends_on,
            style_mix=(body.style_mix or team_contracts.STYLE_MIX_UNSPECIFIED),
            desired_clusters=[c for c in (body.clusters or []) if str(c).strip()],
            raw_ask=body.ask or "",
        )
    except team_contracts.TeamError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _answer_dict(answer: team_builder.TeamAnswer) -> dict:
    """The whole answer as JSON. No per-person number anywhere in it.

    Built field by field rather than by ``dataclasses.asdict`` on purpose: the
    payload is the one place a per-person judgement could be introduced by
    accident, and an explicit projection makes adding one a visible act. Every
    per-person value below is an identity, a set, or a state.
    """
    result = answer.result
    return {
        "state": result.presentation_state,
        "asked_for": [
            {
                "skill": ask.skill_key,
                "name": ask.name,
                "phrase": ask.phrase,
                "required": ask.required,
            }
            for ask in result.request.skills
        ],
        "unrecognised": [skill.phrase for skill in result.request.unresolved_skills],
        "window": (
            {
                "start": answer.window[0].isoformat(),
                "end": answer.window[1].isoformat(),
            }
            if answer.window
            else None
        ),
        "group": [
            {
                "email": candidate.user_email,
                "name": candidate.display_name,
                "confirmed": [
                    {
                        "skill": item.skill_key,
                        "name": item.name,
                        "level": item.level,
                        "origin": item.origin,
                    }
                    for item in candidate.covers
                ],
                "availability": candidate.availability,
                "back_on": (
                    candidate.back_on.isoformat() if candidate.back_on else None
                ),
                "work_style": candidate.work_style_key,
                # Where that style sits on the team map. Derived from the style
                # key the candidate already carries — no new data about anybody,
                # just the position their own published style implies. `None`
                # when their DISC layer is closed, which the map draws as
                # "not placed" rather than dropping them from the picture.
                "style_marker": heatmap.marker_from(
                    candidate.work_style_key,
                    candidate.map_position,
                    sorted(candidate.clusters),
                ),
                "clusters": sorted(candidate.clusters),
            }
            for candidate in result.candidates
        ],
        "coverage": [
            {
                "skill": item.skill_key,
                "name": item.name,
                "required": item.required,
                "covered_by": list(item.covered_by),
                "covered": item.covered,
            }
            for item in result.coverage
        ],
        "style_balance": {
            "clusters_covered": sorted(result.balance.clusters_covered),
            "clusters_missing": sorted(result.balance.clusters_missing),
            "cluster_notes": dict(result.balance.cluster_notes),
            "styles_present": list(result.balance.styles_present),
            "all_one_style": result.balance.all_one_style,
            "profiles_missing_for": result.balance.profiles_missing_for,
            "note": result.balance.note,
        },
        # The pair guide's ten dimensions, as GROUP counts: how much of the
        # group each one can be read on at all. No per-person mapping — that
        # would publish who has not finished their interview.
        "dimension_readiness": [
            {
                "key": read.key,
                "label": read.label,
                "open_for": read.open_for,
                "of_people": read.of_people,
            }
            for read in team_builder.dimension_readiness(answer)
        ],
        "unreadable_notes": team_builder.dimension_notes(
            team_builder.dimension_readiness(answer)
        ),
        "shortfalls": [
            {
                "code": item.code,
                "detail": item.detail,
                "skill": item.skill_key,
                "phrase": item.phrase,
            }
            for item in result.shortfalls
        ],
        "notes": list(answer.notes),
        "horizon_note": result.horizon_note,
        "write_up": [line.text for line in answer.rationale],
        "report": team_builder.render_report(answer),
        "advisory_notice": team_contracts.TEAM_ADVISORY_NOTICE,
        "selection_notice": team_builder.SELECTION_NOTICE,
        "scope_notice": team_builder.SCOPE_NOTICE,
        "availability_notice": team_builder.AVAILABILITY_NOTICE,
    }


@router.post("")
def build_team(
    body: TeamAskBody,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Propose a group for a piece of work.

    Reads confirmed skills, published profile layers, and the OOO table — and
    nothing else. No workload, no utilisation, no raw scores.

    Always 200 when the ask itself parses. An ask nobody can satisfy is an answer
    with ``state: "empty"`` and a ``shortfalls`` list saying why, not an error: a
    404 here would render as "something went wrong" when the truthful answer is
    "nobody has confirmed that skill in Smart Teams".
    """
    request = _request_from(body, requester=(user.email or "").strip().lower())
    answer = team_builder.answer_for(db, request)
    logger.info(
        "[smart-teams] team builder: %s asked for %d skill(s), state=%s, "
        "group=%d, shortfalls=%s",
        request.requested_by,
        len(request.skills),
        answer.result.presentation_state,
        len(answer.result.candidates),
        [item.code for item in answer.result.shortfalls],
    )
    return _answer_dict(answer)


@router.get("/skills")
def team_skill_vocabulary(
    query: str = "",
    cluster: str = "",
    limit: int = 100,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The skill names a Team Builder ask may use, for the page's picker.

    The same curated vocabulary ST-8's directory searches, served here so the
    Team Builder page does not have to guess at phrases that will resolve. It
    returns SKILLS, never people: which colleague holds one is the other
    endpoint's answer, and this one is not a directory of who is good at what.
    """
    rows = skill_taxonomy.search(
        db, query=query or None, cluster=cluster or None, limit=max(1, min(limit, 200))
    )
    return {
        "skills": [
            {"skill": row.skill_key, "name": row.name, "cluster": row.cluster}
            for row in rows
        ],
        "clusters": sorted(
            {row.cluster for row in skill_taxonomy.active_specs(db) if row.cluster}
        ),
        "max_skills_per_ask": team_builder.MAX_REQUESTED_SKILLS,
        "max_team_size": team_contracts.MAX_TEAM_SIZE,
        "style_mix_values": list(team_contracts.STYLE_MIX_VALUES),
        "advisory_notice": team_contracts.TEAM_ADVISORY_NOTICE,
    }
