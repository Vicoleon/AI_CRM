"""ST-6 — the personal working guide: builder, lifecycle, router, job.

No network. The prose model is a plain callable in every test that uses one, and
most tests use none at all: ``guide.build_guide`` without a ``prose_writer`` is a
supported mode, not a stub, so the whole document is testable as authored copy.

What these tests are actually defending, and each of them has already been the
shape of a real bug in this feature:

1. **A refused layer cannot produce a sentence.** The all-refused profile must
   still yield a valid document, and that document must contain no claim — no
   guideline, no tie-in, no growth edge — while naming every layer and why. The
   failure mode this catches is the worst thing the feature can do: a confident
   sentence assembled out of a closed gate.
2. **A partially-refused profile hedges in exactly one place.** Refused layers
   appear in the completeness section carrying their own hedge, and nowhere else.
   A hedge in a citation box reads as an assessment being coy.
3. **Advancement language in a model response is rejected**, and the guide falls
   back to the authored sentence rather than shipping the model's.
4. **Publishing without approval is impossible.** Both because ``guide.approve``
   is the only writer of ``published`` and sets ``approved_at`` in the same
   assignment, and because the DB CHECK refuses the row if anything ever tries.
5. **Unpublish revokes a third party's read**, and the purge is attempted with
   BOTH arguments the DAM routing asymmetry needs.
6. **A profile version bump makes the cached guide stale**, and regenerating
   lands on a new row rather than overwriting the published one.
7. **A third party cannot read an unapproved draft** — through the JSON read or
   through the .docx download, which is the endpoint that usually forgets.

In-memory SQLite with a ``StaticPool`` and ``app.dependency_overrides``, per
``test_smart_teams_router.py``. The Smart Teams GATE is never overridden — only
``get_current_user`` — so ``require_smart_teams_access`` runs for real.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app as main_app
from backend.routers import smart_teams_guides
from backend.tests._rosters import narrow_smart_teams_support
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, User
from rubik_core.database.smart_teams import SmartTeamsDocument, SmartTeamsProfile
from rubik_core.smart_teams import contracts, guide, guide_prompts
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

OWNER = "owner@acme.example"
OTHER = "hr@acme.example"
SYSADMIN = "sysadmin@acme.example"

BASE = "/api/smart-teams/guides"


#: A private app carrying ONLY the guides router, at the prefix ``main.py`` mounts
#: it under.
#:
#: Deliberately not ``backend.main.app``. Including a router on the shared app at
#: import time mutates it for every test that runs afterwards in the same
#: process, and it does not stay theoretical:
#: ``test_smart_teams_api_contract.py`` walks ``app.routes`` looking for routes
#: with no frontend caller, so mounting here made that file fail with a list of
#: THIS file's endpoints. A private app is the isolation, and the real mount is
#: covered separately by :func:`test_the_router_is_mounted_in_main`.
#:
#: The Smart Teams gate still runs for real: ``require_smart_teams_access`` is
#: declared on the ``APIRouter`` itself, not on the mount, which is the whole
#: point of putting it there.
app = FastAPI()
app.include_router(smart_teams_guides.router, prefix="/api")

#: Whether ``backend/main.py`` already includes the guides router. Read once, at
#: import, before any test can touch either app.
MOUNTED_IN_MAIN = any(
    getattr(route, "path", "").startswith(BASE) for route in main_app.routes
)

client = TestClient(app)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _user(
    email: str,
    *,
    department: str = "general",
    is_admin: bool = False,
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["admin"] if is_admin else ["user"],
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
    )


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    session = Session()

    def _override():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()


@pytest.fixture
def as_owner(db_session):
    app.dependency_overrides[get_current_user] = lambda: _user(OWNER)
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _login(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _job_session(session):
    """Point the job's ``SessionLocal`` at the test session.

    The job is a separate process in production and opens its own session, so it
    resolves ``SessionLocal`` at call time rather than through the FastAPI
    dependency the router tests override. ``close`` is neutered because the
    fixture owns the session's lifetime — a job closing it would take every
    later assertion in the test with it.
    """

    class _Session:
        def __new__(cls):
            session.close = lambda: None  # type: ignore[method-assign]
            return session

    return patch("rubik_core.database.db.SessionLocal", _Session)


# ── Layer payload builders ───────────────────────────────────────────────────
# Hand-built envelopes rather than a scoring run: the point of every test here
# is the GATE, and building it by hand is the only way to set a gate closed for
# one layer and open for another. The layer bodies are the instruments' real
# authored strings, taken from the modules by name, so the screen and the
# renderers see the prose they will see in production.


def _envelope(
    framework: str,
    layer: dict | None,
    *,
    reportable: bool,
    confidence: str = "high",
    reasons: tuple[str, ...] = (),
    hedge: str | None = None,
) -> dict:
    payload = {
        contracts.KEY_SCHEMA_VERSION: contracts.PAYLOAD_SCHEMA_VERSION,
        contracts.KEY_FRAMEWORK: framework,
        contracts.KEY_REPORTABLE: reportable,
        contracts.KEY_CONFIDENCE: confidence if reportable else "none",
        contracts.KEY_REASONS: list(reasons),
        contracts.KEY_HEDGE: hedge,
        contracts.KEY_INSTRUMENT_VERSION: None,
        contracts.KEY_SCORED_AT: "2026-08-20T00:00:00",
        contracts.KEY_LAYER: layer if reportable else None,
    }
    # Prove the fixture is on-contract in the fixture, so a test failure is
    # never "the test built a payload guide.py would have refused anyway".
    contracts.validate_envelope(payload)
    return payload


def _disc_layer(style_key: str = "D") -> dict:
    style = disc.WORK_STYLES[style_key]
    return {
        "layer": "disc",
        "assignment": {"style": style.as_dict(), "primary": style_key},
        "quality": {},
    }


def _strengths_layer() -> dict:
    theme_id = next(iter(strengths.THEMES))
    spec = strengths.spec_for(theme_id)
    return {
        "layer": "strengths",
        "themes": [
            {
                "theme_id": theme_id,
                "name": spec.name,
                "evidence_quote": "I rewrote the whole intake form the week I got there.",
            }
        ],
    }


def _enneagram_layer(number: int = 3) -> dict:
    spec = enneagram.spec_for(number)
    return {
        "layer": "enneagram",
        "type_number": number,
        "type_display": spec.label,
        "headline": f"{spec.label} — my own read, and worth arguing with.",
    }


def _motivators_layer() -> dict:
    axes = list(motivators.MOTIVATORS)[:3]
    return {
        "layer": "motivators",
        "top_two": [axes[0], axes[1]],
        "bottom": axes[2],
        "bottom_is_contested": False,
        "near_ties": [],
        "headline": "What makes the work worth it, in my own words.",
    }


def _type16_partial_coin_flip_layer() -> dict:
    """A REAL ``type16`` result with two axes in the middle band.

    Built through ``type16.Type16Result.as_dict`` from real domain scores rather
    than hand-written, because the defect being guarded is what the instrument's
    own renderer interpolates: 40 is the Agreeableness score and 48 is
    ``round(47.5)`` on Conscientiousness, and both used to be printed into the
    published body.
    """
    scores = {
        big_five.Domain.EXTRAVERSION: 72.0,
        big_five.Domain.OPENNESS: 68.0,
        big_five.Domain.AGREEABLENESS: 40.0,
        big_five.Domain.CONSCIENTIOUSNESS: 47.5,
    }
    calls = []
    for axis, spec in type16.AXIS_SPECS.items():
        score = scores[spec.source_domain]
        band = big_five.band_for(score)
        calls.append(
            type16.LetterCall(
                axis=axis,
                letter=spec.high_letter if score > type16.MIDPOINT else spec.low_letter,
                other_letter=(
                    spec.low_letter if score > type16.MIDPOINT else spec.high_letter
                ),
                source_domain=spec.source_domain,
                source_score=score,
                source_band=band,
                confidence=type16.CONFIDENCE_BY_BAND[band],
            )
        )
    return type16.Type16Result(
        calls=tuple(calls), unresolved_axes=(), suppressed_reason=None
    ).as_dict()


def _profile_row(
    db,
    email: str = OWNER,
    *,
    version: int = 1,
    frameworks: dict | None = None,
) -> SmartTeamsProfile:
    """A scored profile version. ``frameworks`` maps name -> envelope; anything
    omitted is written as ``not_taken`` rather than NULL, which is what the
    scoring engine does and what ``read_layers`` expects."""
    columns = {
        name: contracts.not_taken_envelope(name) for name in contracts.FRAMEWORKS
    }
    columns.update(frameworks or {})
    row = SmartTeamsProfile(
        id=f"prof-{email.split('@')[0]}-{version}",
        user_email=email,
        version=version,
        status="current",
        is_current=True,
        scored_at=datetime.utcnow(),
        **columns,
    )
    # is_current is True or NULL and never False (uq_st_profile_one_current), so
    # an older version is retired by setting NULL.
    for older in (
        db.query(SmartTeamsProfile)
        .filter(SmartTeamsProfile.user_email == email)
        .filter(SmartTeamsProfile.is_current.is_(True))
        .all()
    ):
        older.is_current = None
    db.add(row)
    db.commit()
    return row


def _full_profile(db, email: str = OWNER, *, version: int = 1) -> SmartTeamsProfile:
    """Four open gates — enough for well past the guideline floor."""
    return _profile_row(
        db,
        email,
        version=version,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
            "enneagram": _envelope("enneagram", _enneagram_layer(), reportable=True),
            "motivators": _envelope("motivators", _motivators_layer(), reportable=True),
        },
    )


# ── The calibration corpus ───────────────────────────────────────────────────
# The measurement behind ``guide_prompts.MAX_NOVEL_CONTENT_WORDS``, carried here
# rather than in a scratch file so the threshold cannot drift away from its own
# evidence. Facts are read out of the instrument modules, so a re-authored field
# breaks the corpus instead of silently changing what was measured.
#
# Each entry is ``(fact, model_body, model_heading)``.


def _disc_field(style_key: str, field: str, index: int | None = None) -> str:
    """One authored DISC field, exactly as ``guide._disc_candidates`` reads it."""
    value = disc.WORK_STYLES[style_key].as_dict()[field]
    if index is not None:
        return value[index]
    if isinstance(value, list):
        return " ".join(value[:2])
    return value


_THEME_IDS = list(strengths.THEMES)

#: Faithful paraphrases: first-person restatements that add nothing. Written the
#: way the prompt asks for them — an "I" statement blended with something the
#: reader can do — and deliberately including the awkward cases, where the
#: paraphrase reaches for a synonym rather than reusing the fact's own words.
_PARAPHRASES: tuple[tuple[str, str, str], ...] = (
    (
        _disc_field("D", "communication", 0),
        "Open with what you need from me and by when. I would rather have the "
        "request first and the background second, and I will ask for the context "
        "if I want it.",
        "Lead With the Ask and the Deadline",
    ),
    (
        _disc_field("D", "communication", 1),
        "Keep the update to what has changed and what you need from me. I do not "
        "need the whole history again.",
        "Keep Me in the Loop Briefly",
    ),
    (
        _disc_field("D", "communication", 2),
        "If you disagree, say it out loud. Hinting reads to me as having nothing "
        "to say, so I would rather hear the objection plainly.",
        "Say the Disagreement Out Loud",
    ),
    (
        _disc_field("D", "feedback"),
        "Give it to me directly and straight away, with the business consequence "
        "attached. I will argue about it — that is me engaging, not rejecting it. "
        "Land the point once and let me come back to it.",
        "Give Me Feedback Directly",
    ),
    (
        _disc_field("D", "energized_by"),
        "Point me at a problem with a clear finish line and the authority to go "
        "and get it. Hand me the thing nobody else wants to touch.",
        "Point Me at a Clear Finish Line",
    ),
    (
        _disc_field("D", "drained_by"),
        "Meetings that recap rather than decide cost me the most, and so does "
        "waiting on an approval nobody will own.",
        "What Costs Me the Most Energy",
    ),
    (
        _disc_field("I", "communication", 0),
        "Talk it through live with me. A call settles in ten minutes what email "
        "takes three days to do.",
        "Call Me Instead of Writing",
    ),
    (
        _disc_field("I", "communication", 1),
        "Expect half-formed ideas from me. I am drafting out loud, not committing "
        "to any of it yet.",
        "Expect Me to Draft Out Loud",
    ),
    (
        _disc_field("I", "communication", 2),
        "Follow the conversation with two lines in writing, so the decision "
        "survives the meeting it was made in.",
        "Write the Decision Down After",
    ),
    (
        _disc_field("I", "feedback"),
        "In person and in private, warmth first, and specific about the behaviour "
        "rather than about me. Write down what we agreed at the end, or the good "
        "feeling is the part I remember.",
        "Feedback in Private, Warmth First",
    ),
    (
        _disc_field("I", "energized_by"),
        "New rooms, new people, and work that needs explaining to them. Visible "
        "momentum and a team that is talking to each other.",
        "Point Me at New Rooms and People",
    ),
    (
        _disc_field("I", "drained_by"),
        "Long solo stretches with nobody to react to drain me, and so does being "
        "told to put it in a template when I wanted to discuss it.",
        "What Drains Me Fastest",
    ),
    (
        _disc_field("S", "communication", 0),
        "Give me notice. A change announced on Monday for Thursday lands; one "
        "announced Thursday for Thursday does not.",
        "Give Me Notice of a Change",
    ),
    (
        _disc_field("S", "communication", 1),
        "Ask me directly for my view. I will not fight the room for airtime.",
        "Ask Me Directly for My View",
    ),
    (
        _disc_field("S", "communication", 2),
        "Tell me what stays the same, not only what is changing.",
        "Tell Me What Stays the Same",
    ),
    (
        _disc_field("S", "feedback"),
        "Privately and calmly, with the relationship stated up front and time to "
        "think afterwards. Come back in a week — the first conversation is when I "
        "hear it and the second is when I respond.",
        "Give Me Time to Think Afterwards",
    ),
    (
        _disc_field("S", "energized_by"),
        "Repeat work done well, with a group I trust. Being the person others "
        "come to when something has to hold.",
        "Point Me at Work That Has to Hold",
    ),
    (
        _disc_field("S", "drained_by"),
        "Priorities reorganised mid-week with no explanation drain me, and so "
        "does open conflict left unresolved in the room.",
        "What Costs Me the Most",
    ),
    (
        _disc_field("C", "communication", 0),
        "Send me the detail in writing before you need an answer, and I will come "
        "back with the gaps rather than improvise in the room.",
        "Send the Detail Before the Meeting",
    ),
    (
        _disc_field("C", "feedback"),
        "Be specific and bring the evidence with you. A general impression is "
        "something I cannot act on.",
        "Bring the Specifics With You",
    ),
    (
        strengths.spec_for(_THEME_IDS[0]).ask_for,
        "Give me the batch rather than the single item — what is actually going "
        "on across all of these — and let me come back to you rather than "
        "pattern-match live in the meeting. I need the data in front of me to do "
        "this well.",
        "Give Me the Batch, Not the Item",
    ),
    (
        strengths.spec_for(_THEME_IDS[1]).ask_for,
        "Hand me the failures nobody can explain, and say out loud how deep you "
        "want me to go and by when. Patch it today and root-cause it Thursday is "
        "something I can work with; just fix it is not.",
        "Hand Me the Failure Nobody Can Explain",
    ),
    (
        strengths.spec_for(_THEME_IDS[2]).ask_for,
        "Bring me the messy ask before you have decomposed it, and give me "
        "permission to change it. What should I actually be asking here is the "
        "question I most want handed to me.",
        "Bring Me the Messy Ask First",
    ),
    (
        strengths.spec_for(_THEME_IDS[3]).ask_for,
        "Give me the claims that will be challenged — the client-facing number, "
        "the figure in front of a regulator, the one an auditor will ask about. "
        "And tell me explicitly when a rough answer is genuinely enough, because "
        "I will not assume it.",
        "Give Me the Claims That Get Challenged",
    ),
    (
        strengths.spec_for(_THEME_IDS[4]).ask_for,
        "Hand me the long version and ask me what it comes down to. I would "
        "rather cut it down for you than have you read all of it.",
        "Ask Me What It Comes Down To",
    ),
    (
        motivators.spec_for(motivators.MOTIVATORS[0]).high_read,
        "Lead with the payoff and the cost, then the method. Put the number in "
        "the first two sentences of any ask — a request that arrives without one "
        "reads to me as unfinished thinking. I will happily cut scope to protect "
        "the return, so tell me which parts of a plan are load-bearing.",
        "Lead With the Payoff and the Cost",
    ),
    (
        motivators.spec_for(motivators.MOTIVATORS[0]).low_read,
        "A business case is not what convinces me, and pushing one harder reads "
        "as pressure. Give me the reason the work matters on its own terms and "
        "let the economics be a constraint rather than the argument.",
        "A Business Case Is Not What Convinces Me",
    ),
    (
        motivators.spec_for(motivators.MOTIVATORS[1]).high_read,
        "Send me the source, not the summary — I will go and find it anyway. "
        "Expect why questions that are curiosity rather than challenge, and give "
        "me the problem before the answer.",
        "Send Me the Source, Not the Summary",
    ),
    (
        motivators.spec_for(motivators.MOTIVATORS[2]).high_read,
        "Name who benefits and you have my attention; describe an outcome purely "
        "as a metric and you have lost it. I will absorb other people's problems "
        "until my own list slips, so protecting my focus is a favour rather than "
        "a restriction.",
        "Name Who Benefits and You Have Me",
    ),
    (
        motivators.spec_for(motivators.MOTIVATORS[2]).low_read,
        "I am not cold; helping simply is not the fuel. I will do the supportive "
        "thing when it is on the list and not spontaneously, so put it on the "
        "list rather than hoping.",
        "Put the Supportive Thing on the List",
    ),
    (
        enneagram.spec_for(1).needs_from_colleagues,
        "Say explicitly which parts of a deliverable need to be excellent and "
        "which genuinely only need to be done — I cannot tell from the outside "
        "and I will default to excellent on all of it. Acknowledge the fix, not "
        "only the ship.",
        "Tell Me What Only Needs to Be Done",
    ),
    (
        enneagram.spec_for(8).needs_from_colleagues,
        "Be direct with me; a hedged delivery reads as evasion. Disagree to my "
        "face and hold your ground — I respect that far more than agreement. Tell "
        "me plainly when my force flattened a room.",
        "Be Direct and Hold Your Ground",
    ),
)

#: Fabrications. The first four are the strings the wave-1 verifier actually got
#: into a published guide; the rest are the same shape — a medical condition, a
#: family circumstance, a qualification, a claimed skill, a grievance.
_FABRICATIONS: tuple[tuple[str, str, str], ...] = (
    (
        _disc_field("D", "communication", 0),
        "I burned out at my last job after my divorce, so lead with the ask and "
        "the deadline.",
        "Lead With the Ask",
    ),
    (
        _disc_field("D", "communication", 1),
        "I have ADHD so keep agendas short, and keep updates to what changed.",
        "Keep Agendas Short",
    ),
    (
        strengths.spec_for(_THEME_IDS[0]).ask_for,
        "Ask me about Data Privacy and GAAP audits, which are my top two strengths.",
        "Ask Me About Data Privacy",
    ),
    (
        _disc_field("D", "feedback"),
        "I am an Enneagram Type 8 and a natural Systems Thinker, so lead with the "
        "ask. My INTJ wiring (derived from Big Five) means I decide fast.",
        "Lead With the Ask",
    ),
    (
        _disc_field("I", "communication", 0),
        "Talk it through live — I am dyslexic and long email threads are a real "
        "struggle for me.",
        "Call Me Instead",
    ),
    (
        _disc_field("S", "communication", 0),
        "Give me notice. I am a single parent and my childcare pickup is at four, "
        "so late meetings do not work.",
        "Give Me Notice",
    ),
    (
        _disc_field("C", "feedback"),
        "Be specific. I am in recovery and I do not drink, so please keep team "
        "socials away from bars.",
        "Be Specific With Me",
    ),
    (
        motivators.spec_for(motivators.MOTIVATORS[0]).high_read,
        "Lead with the payoff. I took a pay cut to join and I am still bitter "
        "about my last review, so money talk is loaded for me.",
        "Lead With the Payoff",
    ),
    (
        enneagram.spec_for(8).needs_from_colleagues,
        "Be direct with me. My therapist says I intimidate people and I am "
        "working on it in counselling.",
        "Be Direct With Me",
    ),
    (
        strengths.spec_for(_THEME_IDS[1]).ask_for,
        "Hand me the failures nobody can explain. I ran the Basel III "
        "remediation at my previous bank and I speak fluent Mandarin.",
        "Hand Me the Failures",
    ),
    (
        _disc_field("I", "feedback"),
        "In person and in private. I cried in a one-to-one once and I would "
        "rather that did not happen again.",
        "Feedback in Private",
    ),
    (
        enneagram.spec_for(9).needs_from_colleagues,
        "Ask for my actual position and wait. I am immunocompromised so I stay "
        "remote on Mondays and Fridays.",
        "Ask and Wait",
    ),
    (
        motivators.spec_for(motivators.MOTIVATORS[2]).high_read,
        "Name who benefits. I volunteer at a hospice on weekends and that is "
        "where my head really is.",
        "Name Who Benefits",
    ),
    (
        _disc_field("D", "energized_by"),
        "Point me at a problem with a clear finish line. I am studying for the "
        "CFA and I plan to move into treasury next year.",
        "Point Me at a Problem",
    ),
)


def _seed_user(db, email: str, name: str) -> None:
    db.add(User(email=email, id=email.split("@")[0], name=name))
    db.commit()


def _ready_guide(
    db, profile: SmartTeamsProfile, *, name: str = "Owner"
) -> SmartTeamsDocument:
    row = guide.ensure_guide_row(db, profile).document
    guide.generate_into_row(db, row, profile, display_name=name)
    db.commit()
    return row


# ── 1. The all-refused profile ───────────────────────────────────────────────


def test_all_refused_profile_yields_an_honest_document_with_no_claims(db_session):
    """Every gate closed: a real document, and not one sentence about the person.

    The document must exist (an empty file is not an honest outcome either), must
    say plainly that there was not enough to write from, and must name the
    interview sections that would change it. And it must contain none of the
    devices that assert something: no guidelines, no tie-in citation, no growth
    edge, no final thought.
    """
    profile = _profile_row(db_session)  # every framework not_taken
    built = guide.build_guide(profile, display_name="Owner")

    assert built.guidelines == []
    assert built.growth_edge is None
    assert built.working_preferences == []
    assert built.derived_layer is None
    assert built.final_thought is None, (
        "house copy about what I will bring is filler under a guide with no "
        "content — it reads as a claim"
    )
    assert built.shortfall == guide.SHORTFALL_BODY
    assert built.has_content is False
    assert built.framing == guide.FRAMING_NO_LAYERS

    body = guide.render_markdown(built)
    assert guide.SHORTFALL_LABEL.upper() in body
    assert built.advisory_notice in body
    # Every layer is accounted for, with a reason, and the sections that fix it.
    assert len(built.completeness) == len(contracts.FRAMEWORKS)
    assert all(not line.included for line in built.completeness)
    assert all(line.note.strip() for line in built.completeness), (
        "a blank note next to a layer name reads as something withheld"
    )
    assert any(line.sections_to_finish for line in built.completeness)
    # No citation device anywhere: those are the only places an instrument may
    # be named, and naming one here would be naming a layer that refused.
    assert "tie-in" not in body.lower()
    # Caps, because that is the form render_markdown emits — asserting the
    # lower-case constant here would pass no matter what the renderer did.
    assert guide.GROWTH_EDGE_LABEL.upper() not in body


def test_an_all_refused_guide_names_no_framework_in_its_framing(db_session):
    """The framing paragraph is a slot template, and a closed gate drops its slot.

    With no slots at all it is fixed house copy that names nothing — the failure
    mode being guarded is handing the model the whole profile and asking for a
    paragraph, which is how a refused layer gets papered over.
    """
    profile = _profile_row(db_session)
    framing = guide.framing(guide.read_layers(profile), has_guidelines=False)
    assert framing == guide.FRAMING_NO_LAYERS
    # "Work Style Interview" is the name of the questionnaire and is allowed to
    # appear; it is not a citation. Everything else that names a layer is not.
    haystack = framing.lower().replace("work style interview", "")
    for label in guide.FRAMEWORK_LABELS.values():
        assert label.lower() not in haystack


# ── 2. Partial refusal ───────────────────────────────────────────────────────


def test_partially_refused_profile_renders_content_and_hedges_separately(db_session):
    """Open gates produce content; closed ones produce a reason, in ONE place.

    DISC + strengths + enneagram + motivators open, Big Five and the derived
    four-letter layer refused with their own hedges. Those hedges must appear
    NOWHERE — not in the completeness section either — and the completeness
    section must carry the publication-safe prose for the reason code instead.
    """
    bf_hedge = "Not enough of the scale bank was answered to band these."
    t16_hedge = "Two of the four axes landed too close to call."
    profile = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
            "enneagram": _envelope("enneagram", _enneagram_layer(), reportable=True),
            "motivators": _envelope("motivators", _motivators_layer(), reportable=True),
            "big_five": _envelope(
                "big_five",
                None,
                reportable=False,
                reasons=(contracts.REASON_INSUFFICIENT_ANSWERS,),
                hedge=bf_hedge,
            ),
            "type16": _envelope(
                "type16",
                None,
                reportable=False,
                reasons=(contracts.REASON_COIN_FLIP,),
                hedge=t16_hedge,
            ),
        },
    )
    built = guide.build_guide(profile, display_name="Owner")

    assert len(built.guidelines) >= guide.MIN_GUIDELINES
    assert built.shortfall is None
    # The refused descriptive sections are omitted whole, not hedged in place.
    assert built.working_preferences == []
    assert built.derived_layer is None

    # Every guideline carries provenance, and only from an OPEN gate.
    open_gates = {"disc", "strengths", "enneagram", "motivators"}
    assert all(g.source for g in built.guidelines)
    assert {f for f, _ in built.sources} <= open_gates

    body = guide.render_markdown(built)
    # A refused layer's own hedge appears NOWHERE. It is authored for the
    # subject's private view, where naming a near-tie helps; in a company-visible
    # document it is a finding from a layer the engine refused to report. What
    # appears instead is the publication-safe prose for the reason code, in the
    # completeness section — which now sits in the end matter, BELOW the final
    # thought and the sign-off, so the body matches the source format.
    assert bf_hedge not in body
    assert t16_hedge not in body
    completeness_at = body.index(guide.COMPLETENESS_LABEL.upper())
    bf_note = guide.PUBLICATION_REASON_PROSE[contracts.REASON_INSUFFICIENT_ANSWERS]
    t16_note = guide.PUBLICATION_REASON_PROSE[contracts.REASON_COIN_FLIP]
    assert body.count(bf_note) == 1
    assert body.count(t16_note) == 1
    assert body.index(bf_note) > completeness_at
    assert body.index(t16_note) > completeness_at

    lines = {line.framework: line for line in built.completeness}
    assert lines["big_five"].included is False
    assert lines["big_five"].note == bf_note
    assert lines["type16"].note == t16_note
    assert lines["disc"].included is True
    # A coin flip is not something the person can go and fix by answering more,
    # so no section is suggested for it; insufficient answers is.
    assert lines["big_five"].sections_to_finish
    assert lines["type16"].sections_to_finish == ()


def test_a_refused_layer_drops_its_framing_slot(db_session):
    """The framing is a slot template and a closed gate removes a slot.

    DISC refused, strengths open: the paragraph must still read as a paragraph
    and must not contain a work-style claim. This is the mechanism that replaces
    "hand the model the whole profile and ask for an opener", which is how a
    closed gate gets papered over.
    """
    profile = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope(
                "disc",
                None,
                reportable=False,
                reasons=(contracts.REASON_RETAKE_RECOMMENDED,),
                hedge="The answers here looked rushed.",
            ),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
        },
    )
    framing = guide.framing(guide.read_layers(profile), has_guidelines=True)
    assert "work style" not in framing.lower()
    assert disc.WORK_STYLES["D"].name not in framing
    # The open layer's slot is still there, so the paragraph is not a stub.
    assert "the work I am most useful in is" in framing
    assert framing.startswith(guide.FRAMING_OPENER)
    assert framing.endswith(guide.FRAMING_CLOSER)
    # And the refused layer's hedge is not smuggled into the opener.
    assert "rushed" not in framing


def test_tie_in_wording_leans_when_confidence_is_not_high(db_session):
    """Confidence gates the WORDING of a citation, not just its presence.

    At ``moderate`` the style name must lean rather than land, and the style's
    ``identity`` sentence — the most quotable line in the document — must be
    left out entirely.
    """
    layers = guide.read_layers(
        _profile_row(
            db_session,
            frameworks={
                "disc": _envelope(
                    "disc", _disc_layer(), reportable=True, confidence="moderate"
                )
            },
        )
    )
    callout = guide._tie_in(layers["disc"])
    assert callout is not None
    assert "leans toward" in callout.body
    identity = disc.WORK_STYLES["D"].identity
    assert identity not in callout.body


def test_growth_edge_is_omitted_at_low_confidence(db_session):
    """The strictest rule in the document: no growth edge from a thin layer.

    An open gate is not enough — it needs ``high`` or ``moderate``. And when
    nothing qualifies the box is omitted rather than hedged, because a growth
    edge composed from thin evidence is the single most damaging sentence this
    feature could publish.
    """
    low = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True, confidence="low"),
            "strengths": _envelope(
                "strengths", _strengths_layer(), reportable=True, confidence="low"
            ),
            "enneagram": _envelope(
                "enneagram", _enneagram_layer(), reportable=True, confidence="low"
            ),
            "motivators": _envelope(
                "motivators", _motivators_layer(), reportable=True, confidence="low"
            ),
        },
    )
    assert guide.growth_edge(guide.read_layers(low)) is None
    built = guide.build_guide(low, display_name="Owner")
    assert built.guidelines, "low confidence still produces guidelines"
    assert built.growth_edge is None
    assert guide.GROWTH_EDGE_LABEL.upper() not in guide.render_markdown(built)

    high = _profile_row(db_session, version=2)
    high.disc = _envelope("disc", _disc_layer(), reportable=True, confidence="high")
    db_session.commit()
    assert guide.growth_edge(guide.read_layers(high)) is not None


def test_no_guideline_can_exist_without_a_source(db_session):
    """``Guideline.source`` is the mechanism that keeps the count honest.

    Ten guidelines means ten authored fields on reportable layers. A source
    naming a framework that does not exist is refused at construction.
    """
    with pytest.raises(ValueError):
        guide.Guideline(
            slot_id="invented",
            source=("vibes", "hunch"),
            heading="Trust Me",
            body="No layer said this.",
            fact="",
            intent="",
        )


def test_the_guideline_count_is_dynamic_and_never_padded(db_session):
    """The heading says how many there are, and nothing pads to reach the cap."""
    built = guide.build_guide(_full_profile(db_session), display_name="Owner")
    count = len(built.guidelines)
    assert guide.MIN_GUIDELINES <= count <= guide.MAX_GUIDELINES
    body = guide.render_markdown(built)
    heading = guide.CONTENTS_TEMPLATE.format(count=count, noun="things").upper()
    assert f"## {heading}" in body
    # ...and the page-1 index under it names every guideline exactly once.
    for index, guideline in enumerate(built.guidelines, start=1):
        assert f"- **{index:02d}**  {guideline.heading}" in body


def test_below_the_floor_becomes_the_shortfall_document(db_session):
    """Two qualified guidelines is not a guide. It is the shortfall document.

    An enneagram layer alone yields one candidate and a motivators layer two, so
    this profile is under ``MIN_GUIDELINES`` — and the builder must drop them
    rather than publish a three-line document about somebody.
    """
    profile = _profile_row(
        db_session,
        frameworks={
            "enneagram": _envelope("enneagram", _enneagram_layer(), reportable=True),
        },
    )
    candidates = guide.candidate_guidelines(guide.read_layers(profile))
    assert 0 < len(candidates) < guide.MIN_GUIDELINES
    built = guide.build_guide(profile, display_name="Owner")
    assert built.guidelines == []
    assert built.shortfall == guide.SHORTFALL_BODY


# ── 3. Screening the model ───────────────────────────────────────────────────


def test_advancement_language_from_the_model_is_rejected(db_session):
    """A model rewrite that reaches for advancement language is refused.

    The guide keeps the authored sentence. This is the assembly-time belt — the
    screen also runs inside ``write_prose`` — and it is what makes a stub writer
    (a test, ST-7, a future caller) unable to route around the rule.
    """
    profile = _full_profile(db_session)
    authored = {
        g.slot_id: (g.heading, g.body)
        for g in guide.candidate_guidelines(guide.read_layers(profile))
    }
    target = next(iter(authored))
    bad = "This all shows he has potential for a bigger role."
    assert guide_prompts.screen_prose(bad) == guide_prompts.REJECT_ADVANCEMENT

    def writer(slots, *, display_name):
        return {
            slot.slot_id: {
                "heading": "Ready for Promotion"
                if slot.slot_id == target
                else "Talk to Me Plainly",
                "body": bad if slot.slot_id == target else slot.body,
            }
            for slot in slots
        }

    built = guide.build_guide(profile, display_name="Owner", prose_writer=writer)
    rejected = next(g for g in built.guidelines if g.slot_id == target)
    assert rejected.model_written is False
    assert rejected.heading == authored[target][0]
    assert rejected.body == authored[target][1]
    assert bad not in guide.render_markdown(built)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("As an INTJ I prefer written detail.", guide_prompts.REJECT_UNQUALIFIED_CODE),
        (
            "My Gallup themes point at execution.",
            guide_prompts.REJECT_FOREIGN_VOCABULARY,
        ),
        (
            "I am the Achiever, so give me targets.",
            guide_prompts.REJECT_FOREIGN_VOCABULARY,
        ),
        (
            "My Power motivator wants authority.",
            guide_prompts.REJECT_FOREIGN_VOCABULARY,
        ),
        ("We are about 75% compatible on pace.", guide_prompts.REJECT_NUMERIC_VERDICT),
        ("I score high on structure.", guide_prompts.REJECT_NUMERIC_VERDICT),
        ("My manager thinks I move too fast.", guide_prompts.REJECT_THIRD_PARTY),
        (
            "The marketing team never decides anything.",
            guide_prompts.REJECT_THIRD_PARTY,
        ),
        ("", guide_prompts.REJECT_EMPTY),
        (None, guide_prompts.REJECT_EMPTY),
    ],
)
def test_the_screen_refuses_the_whole_forbidden_vocabulary(text, code):
    """Every rule the format spec's section 5 asks for, as a control.

    A bare four-letter code, another vendor's instrument or type nickname, a
    number offered as a verdict, and a claim about somebody who is not the
    subject — the last of which is what stops a guide characterising a whole
    department. The prompt forbids all of these too; only this is a control.
    """
    assert guide_prompts.screen_prose(text) == code


def test_the_screen_permits_our_own_vocabulary():
    """A false positive costs a plainer sentence, so the net must not be so wide
    that it rejects faithful rewrites of our own authored copy."""
    for spec in (disc.WORK_STYLES[k] for k in disc.WORK_STYLES):
        assert guide_prompts.screen_prose(spec.feedback, max_words=200) is None
    assert (
        guide_prompts.screen_prose(
            "Tell me what you need and when you need it, and I will tell you "
            "plainly whether I can."
        )
        is None
    )
    # "we", "you" and "each other" are the whole point of the document.
    assert (
        guide_prompts.screen_prose("If we disagree, say so and we will sort it out.")
        is None
    )


def test_write_prose_regenerates_once_then_falls_back(db_session):
    """A screen rejection buys one regeneration, then the authored copy wins.

    Heading and body are judged separately, so the clean heading survives and the
    rejected body is simply absent from the entry — which is the whole point of
    accepting them independently: a bad body does not cost a good heading and
    vice versa.
    """
    slots = [
        guide_prompts.ProseSlot(
            slot_id="disc.feedback",
            intent="how to give feedback",
            fact="Say it plainly.",
            heading="How to Give Me Feedback",
            body="Say it plainly.",
        )
    ]
    calls: list[str] = []

    class _Client:
        def invoke_json(self, prompt, *, fallback=None, attempts=1):
            calls.append(prompt)
            return {
                "slots": [
                    {
                        "id": "disc.feedback",
                        "heading": "How to Give Me Feedback",
                        "body": "I am ready for a promotion when you are.",
                    }
                ]
            }

    with patch.object(guide, "_json_client", return_value=_Client()):
        written = guide.write_prose(slots, display_name="Owner")
    assert written == {"disc.feedback": {"heading": "How to Give Me Feedback"}}, (
        "the advancement body must be dropped and the clean heading kept"
    )
    assert len(calls) == guide.PROSE_ATTEMPTS, (
        "a rejected sample must be re-drawn once; two failures fall back rather "
        "than trying a third time"
    )


def test_write_prose_keeps_a_clean_sample(db_session):
    slots = [
        guide_prompts.ProseSlot(
            slot_id="disc.feedback",
            intent="how to give feedback",
            fact="Say it plainly.",
            heading="How to Give Me Feedback",
            body="Say it plainly.",
        )
    ]

    class _Client:
        def invoke_json(self, prompt, *, fallback=None, attempts=1):
            return {
                "slots": [
                    {
                        "id": "disc.feedback",
                        "heading": "Say It Straight to Me",
                        "body": "Tell me the thing itself. I would rather hear it now than work it out later.",
                    }
                ]
            }

    with patch.object(guide, "_json_client", return_value=_Client()):
        written = guide.write_prose(slots, display_name="Owner")
    assert written["disc.feedback"]["heading"] == "Say It Straight to Me"


def test_the_model_never_sees_the_transcript(db_session):
    """The prompt is built from authored slot facts only.

    ``ProseSlot`` carries no transcript field, and the prompt builder takes
    nothing else — so there is no path by which the most private thing this
    feature holds reaches a model call.
    """
    profile = _full_profile(db_session)
    slots = [
        guide_prompts.ProseSlot(
            slot_id=g.slot_id,
            intent=g.intent,
            fact=g.fact,
            heading=g.heading,
            body=g.body,
        )
        for g in guide.candidate_guidelines(guide.read_layers(profile))
    ]
    prompt = guide_prompts.prose_prompt(slots, display_name="Owner")
    quote = _strengths_layer()["themes"][0]["evidence_quote"]
    assert quote not in prompt, (
        "the subject's own words are lifted verbatim into the document and must "
        "never be shown to the model"
    )


# ── 4. Publication is consent ────────────────────────────────────────────────


def test_published_requires_approved_at_at_the_database_level(db_session):
    """The CHECK is real. Belt for any future writer of ``published``."""
    from sqlalchemy.exc import IntegrityError

    subject_key, version_key = guide.cache_keys_for(OWNER, 1)
    db_session.add(
        SmartTeamsDocument(
            id="forced",
            kind=guide.KIND_PERSONAL,
            subject_emails=[OWNER],
            subject_key=subject_key,
            profile_versions={OWNER: 1},
            profile_version_key=version_key,
            status="ready",
            content_md="# body\n",
            published=True,  # with no approved_at
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_approve_sets_consent_and_publication_together(db_session):
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    assert row.published is False
    assert row.approved_at is None

    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    assert row.published is True
    assert row.approved_at is not None
    assert row.approved_by == OWNER


def test_nobody_but_the_subject_can_approve(db_session):
    """Not an admin, not an exec. The library refuses before the router does."""
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    with pytest.raises(guide.GuideError):
        guide.approve(db_session, row, approver_email=SYSADMIN)
    assert row.published is False
    assert row.approved_at is None


def test_a_pending_or_failed_guide_cannot_be_approved(db_session):
    """Approving an unfinished row would publish an empty document."""
    profile = _full_profile(db_session)
    row = guide.ensure_guide_row(db_session, profile).document
    db_session.commit()
    assert row.status == "pending"
    with pytest.raises(guide.GuideError):
        guide.approve(db_session, row, approver_email=OWNER)


def test_regenerating_over_a_published_guide_is_refused(db_session):
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    with pytest.raises(guide.GuideError):
        guide.generate_into_row(db_session, row, profile, display_name="Owner")


def test_approving_a_new_version_retires_the_old_publication(db_session):
    """Approving v2 withdraws consent for the v1 wording."""
    v1 = _full_profile(db_session, version=1)
    first = _ready_guide(db_session, v1)
    guide.approve(db_session, first, approver_email=OWNER)
    db_session.commit()

    v2 = _full_profile(db_session, version=2)
    second = _ready_guide(db_session, v2)
    superseded = guide.approve(db_session, second, approver_email=OWNER)
    db_session.commit()

    assert [row.id for row in superseded] == [first.id]
    assert first.published is False
    assert first.unpublished_at is not None
    assert second.published is True
    # approved_at is left alone on the retired row: it is the record that consent
    # was once given, and clearing it would make the audit trail lie.
    assert first.approved_at is not None


# ── 5. Unpublish and the purge ───────────────────────────────────────────────


def test_unpublish_revokes_a_third_party_read(db_session):
    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()

    _login(_user(OTHER))
    try:
        ok = client.get(f"{BASE}/{OWNER}")
        assert ok.status_code == 200
        assert ok.json()["guide"]["published"] is True

        _login(_user(OWNER))
        assert client.post(f"{BASE}/{row.id}/unpublish").status_code == 200

        _login(_user(OTHER))
        gone = client.get(f"{BASE}/{OWNER}")
        assert gone.status_code == 404
        docx = client.get(f"{BASE}/{row.id}/docx")
        assert docx.status_code == 403, (
            "the download is the endpoint that usually forgets the gate"
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_the_purge_passes_both_arguments_the_dam_routing_needs(db_session):
    """``source_path`` AND ``is_company_wide``.

    ``delete_document_vectors`` skips its first branch entirely without both
    (``if slug and source_path and ...``) and only the default-corpus fallback
    runs — which is precisely where an unpublished guide keeps answering in Chat.
    """
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    row.rag_ingested = True
    row.gcs_uri = "gs://bucket/workspace/rags/companywide/smart_teams_guide__owner.md"
    db_session.commit()

    seen: dict = {}

    def _purge(doc_id, **kwargs):
        seen.update({"doc_id": doc_id, **kwargs})
        return True

    guide.unpublish(db_session, row)
    assert guide.purge_guide_vectors(db_session, row, purge=_purge) is True
    db_session.commit()

    assert (
        seen["source_path"]
        == "gs://bucket/workspace/rags/companywide/smart_teams_guide__owner.md"
    )
    assert seen["is_company_wide"] is True
    assert row.rag_ingested is False
    assert row.gcs_uri is None


def test_a_failed_purge_leaves_the_row_saying_it_is_still_ingested(db_session):
    """A False return is a failure, and the row must not claim a retraction.

    The guide is out of the app and still reachable by Chat. Reporting otherwise
    is the privacy defect; the row telling the truth is what makes it findable.
    """
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    row.rag_ingested = True
    row.gcs_uri = "gs://bucket/x.md"
    db_session.commit()
    guide.unpublish(db_session, row)

    assert (
        guide.purge_guide_vectors(db_session, row, purge=lambda *a, **k: False) is False
    )
    assert row.rag_ingested is True
    assert row.gcs_uri == "gs://bucket/x.md"


def test_purging_a_published_guide_is_refused(db_session):
    """Unpublish first, or the app hides a document Chat can still quote."""
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    row.rag_ingested = True
    row.gcs_uri = "gs://bucket/x.md"
    db_session.commit()
    with pytest.raises(guide.GuideError):
        guide.purge_guide_vectors(db_session, row, purge=lambda *a, **k: True)


def test_an_ingested_guide_with_no_gcs_uri_reports_failure(db_session):
    """Nothing to match on, so say so rather than report a purge that could not
    have happened."""
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    row.rag_ingested = True
    row.gcs_uri = None
    db_session.commit()
    called: list = []
    assert (
        guide.purge_guide_vectors(
            db_session, row, purge=lambda *a, **k: called.append(1) or True
        )
        is False
    )
    assert called == []
    assert row.rag_ingested is True


# ── 6. Ingest: approved guides only ──────────────────────────────────────────


def test_only_an_approved_published_guide_may_be_ingested(db_session):
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)

    class _Manager:
        def update_document(self, slug, path):  # pragma: no cover - must not run
            raise AssertionError("a draft must never reach the corpus")

    with pytest.raises(guide.GuideError):
        guide.ingest_published_guide(
            db_session, row, display_name="Owner", manager_factory=_Manager
        )
    assert row.rag_ingested is False


def test_ingest_writes_the_guide_body_under_a_version_free_filename(db_session):
    """The basename is keyed on the subject, NOT the version.

    ``update_document`` matches the ragFile to replace by its target GCS URI, so
    a stable name makes a regenerate a replacement. Versioning it would leave
    the pre-retake text alive in retrieval.
    """
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()

    captured: dict = {}

    class _Manager:
        def update_document(self, slug, path):
            captured["slug"] = slug
            captured["name"] = path.name
            captured["text"] = path.read_text(encoding="utf-8")
            return {"gcs_uri": f"gs://b/workspace/rags/{slug}/{path.name}"}

    guide.ingest_published_guide(
        db_session, row, display_name="Owner", manager_factory=_Manager
    )
    db_session.commit()

    assert captured["slug"] == guide.GUIDE_RAG_SLUG == "companywide"
    assert captured["name"] == guide.rag_filename(OWNER)
    assert "1" not in captured["name"], "a versioned basename accumulates ragFiles"
    # The chunk says what it is and who it is about, and carries the advisory
    # line — a chunk that reads as anonymous advice about "them" is worse than
    # no chunk.
    assert OWNER in captured["text"]
    assert "Advisory only" in captured["text"]
    assert row.content_md in captured["text"]
    assert row.rag_ingested is True
    assert row.gcs_uri.endswith(guide.rag_filename(OWNER))


def test_nothing_but_the_guide_body_reaches_the_corpus(db_session):
    """Never a score, never a transcript.

    The ingested text is the header plus ``content_md``, and ``content_md`` is
    rendered only from authored layer prose — so a raw numeric score present on
    the profile row cannot appear in it.
    """
    profile = _full_profile(db_session)
    profile.disc = _envelope(
        "disc",
        {**_disc_layer(), "scores": {"D": {"raw": 41, "intensity": "high"}}},
        reportable=True,
    )
    db_session.commit()
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()

    text = guide.rag_document_text(row, display_name="Owner")
    header, separator, body = text.partition("\n\n---\n\n")
    assert separator, "the ingested chunk must carry a self-describing header"
    # The numeric check runs on the BODY, not the whole chunk: the header stamps
    # ``approved_at``, and a timestamp whose clock happens to contain the same
    # digits as a raw score made this assertion pass or fail by time of day. It
    # failed in a full-suite run and passed alone, which is the least useful
    # possible failure.
    assert "41" not in body
    assert "intensity" not in body
    assert "scores" not in body
    # The header is fixed provenance and carries no layer data of its own.
    assert "intensity" not in header
    assert row.content_md in body


# ── 7. Staleness ─────────────────────────────────────────────────────────────


def test_a_profile_version_bump_makes_the_cached_guide_stale(db_session):
    profile = _full_profile(db_session, version=1)
    row = _ready_guide(db_session, profile)
    assert guide.is_stale(row, 1) is False
    assert guide.is_stale(row, 2) is True
    # No current profile at all is not stale — there is nothing newer to be
    # stale against.
    assert guide.is_stale(row, None) is False


def test_regenerating_after_a_retake_lands_on_a_new_row(db_session):
    """The cache key is the staleness mechanism, and it protects the published
    wording: v2 is a new row, so v1 stays published until the person approves
    the new one."""
    v1 = _full_profile(db_session, version=1)
    first = _ready_guide(db_session, v1)
    guide.approve(db_session, first, approver_email=OWNER)
    db_session.commit()

    v2 = _full_profile(db_session, version=2)
    second = guide.ensure_guide_row(db_session, v2)
    db_session.commit()
    assert second.created is True
    assert second.document.id != first.id
    assert first.published is True, "v1 stays published until v2 is approved"
    assert guide.published_guide(db_session, OWNER).id == first.id


def test_ensure_guide_row_is_idempotent_for_one_version(db_session):
    """A double-clicked Generate lands one row (``uq_st_document_cache``)."""
    profile = _full_profile(db_session)
    a = guide.ensure_guide_row(db_session, profile)
    db_session.commit()
    b = guide.ensure_guide_row(db_session, profile)
    db_session.commit()
    assert a.created is True
    assert b.created is False
    assert a.document.id == b.document.id


def test_cache_keys_fold_case(db_session):
    """``users.email`` is stored verbatim from the Google token, so a mixed-case
    subject must produce the same key or the cache misses forever."""
    assert guide.cache_keys_for("Owner@ACME.example", 1) == guide.cache_keys_for(
        OWNER, 1
    )


# ── 8. The router ────────────────────────────────────────────────────────────


def test_a_third_party_cannot_read_an_unapproved_draft(db_session):
    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")

    _login(_user(OTHER))
    try:
        assert client.get(f"{BASE}/{OWNER}").status_code == 404
        assert client.get(f"{BASE}/{row.id}/docx").status_code == 403
        # And they cannot approve it or take it down for the subject.
        assert client.post(f"{BASE}/{row.id}/approve").status_code == 403
        assert client.post(f"{BASE}/{row.id}/unpublish").status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_an_admin_outside_the_support_set_cannot_read_a_draft(db_session, monkeypatch):
    """``is_support_admin`` needs BOTH the admin role and the support row.

    With support handed to the Systems team's admins, a Marketing admin gets
    nothing, and an exec gets nothing either: seniority must not buy somebody
    else's unapproved draft.
    """
    narrow_smart_teams_support(monkeypatch, "Systems")
    profile = _full_profile(db_session)
    _ready_guide(db_session, profile)

    for viewer in (
        _user("boss@acme.example", is_admin=True, department="marketing"),
        _user("ceo@acme.example", is_exec=True, managed=["*"]),
    ):
        _login(viewer)
        try:
            assert client.get(f"{BASE}/{OWNER}").status_code == 404
            assert client.get(f"{BASE}/support/{OWNER}").status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)


def test_a_systems_admin_gets_the_support_read(db_session):
    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    _ready_guide(db_session, profile, name="Owner Person")

    _login(_user(SYSADMIN, is_admin=True, department="Systems"))
    try:
        res = client.get(f"{BASE}/support/{OWNER}")
        assert res.status_code == 200
        payload = res.json()
        assert payload["subject_email"] == OWNER
        assert len(payload["guides"]) == 1
        assert payload["guides"][0]["status"] == "ready"
        # Support state, not prose.
        assert "content_md" not in payload["guides"][0]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_own_guide_read_is_a_state_not_a_404(db_session, as_owner):
    res = client.get(f"{BASE}/me")
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "none"
    assert body["guide"] is None
    assert body["can_generate"] is False
    assert body["advisory_notice"]

    profile = _full_profile(db_session)
    res = client.get(f"{BASE}/me")
    assert res.json()["can_generate"] is True
    assert res.json()["current_profile_version"] == int(profile.version)


def test_generate_refuses_without_a_scored_profile(db_session, as_owner):
    res = client.post(f"{BASE}/generate")
    assert res.status_code == 409
    assert "Work Style Interview" in res.json()["detail"]


def test_generate_dispatches_to_the_worker_and_returns_202(db_session, as_owner):
    """Generation never runs in the web process — Cloud Run throttles the CPU
    after the response, so the Gemini call would be starved."""
    _full_profile(db_session)
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", "exec/1")
    ) as dispatch:
        res = client.post(f"{BASE}/generate")
    assert res.status_code == 202
    body = res.json()
    assert body["enqueued"] is True
    assert body["executor"] == "job"
    assert body["guide"]["status"] == "pending"
    assert "content_md" not in body["guide"]

    kwargs = dispatch.call_args.kwargs
    from backend.routers import smart_teams_guides as mod

    assert kwargs["module"] == mod.GUIDE_JOB_MODULE
    assert kwargs["args"][0] == "--document-id"
    assert kwargs["args"][2] == "--generate"


def test_a_second_generate_on_a_ready_row_is_a_cache_hit(db_session, as_owner):
    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    _ready_guide(db_session, profile, name="Owner Person")
    with patch("backend.jobs.launcher.submit_background_work") as dispatch:
        res = client.post(f"{BASE}/generate")
    assert res.status_code == 202
    assert res.json()["enqueued"] is False
    assert res.json()["reason"] == "cached"
    assert res.json()["guide"]["content_md"]
    dispatch.assert_not_called()


def test_generate_retries_a_failed_row_without_creating_a_second(db_session, as_owner):
    """A job that died leaves ``failed``; Generate must retry the SAME row.

    A second row would give one person two guides for one profile version, which
    ``uq_st_document_cache`` exists to prevent — and the UI would have to guess
    which one is theirs.
    """
    profile = _full_profile(db_session)
    row = guide.ensure_guide_row(db_session, profile).document
    guide.fail_row(db_session, row, "vertex was down")
    db_session.commit()
    row_id = row.id

    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", "exec/9")
    ) as dispatch:
        res = client.post(f"{BASE}/generate")
    assert res.status_code == 202
    assert res.json()["enqueued"] is True
    assert res.json()["guide"]["id"] == row_id
    assert res.json()["guide"]["status"] == "pending"
    assert res.json()["guide"]["error"] is None
    dispatch.assert_called_once()
    assert (
        db_session.query(SmartTeamsDocument)
        .filter(SmartTeamsDocument.kind == guide.KIND_PERSONAL)
        .count()
        == 1
    )


def test_generate_refuses_over_a_published_row(db_session, as_owner):
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    res = client.post(f"{BASE}/generate")
    assert res.status_code == 409
    assert "Unpublish" in res.json()["detail"]


def test_approve_publishes_and_dispatches_the_corpus_write(db_session, as_owner):
    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", "exec/2")
    ) as dispatch:
        res = client.post(f"{BASE}/{row.id}/approve")
    assert res.status_code == 200
    body = res.json()
    assert body["guide"]["published"] is True
    assert body["guide"]["approved_at"]
    # Consent is recorded synchronously; the corpus copy is not, and the response
    # says so rather than claiming it is done.
    assert body["rag_ingested"] is False
    assert [d["action"] for d in body["dispatched"]] == ["ingest"]
    assert dispatch.call_args.kwargs["args"][2] == "--ingest"


def test_unpublish_reports_the_corpus_state_honestly(db_session, as_owner):
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    row.rag_ingested = True
    row.gcs_uri = "gs://b/x.md"
    db_session.commit()

    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", "exec/3")
    ) as dispatch:
        res = client.post(f"{BASE}/{row.id}/unpublish")
    assert res.status_code == 200
    body = res.json()
    assert body["guide"]["published"] is False
    assert body["rag_purge"] == "pending", (
        "never 'done' on the strength of having asked — until the job succeeds "
        "the guide is out of the app and still answerable in Chat"
    )
    assert body["guide"]["rag_ingested"] is True
    assert dispatch.call_args.kwargs["args"][2] == "--purge"


def test_unpublish_is_idempotent(db_session, as_owner):
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", "e")
    ):
        assert client.post(f"{BASE}/{row.id}/unpublish").status_code == 200
    with patch("backend.jobs.launcher.submit_background_work") as dispatch:
        again = client.post(f"{BASE}/{row.id}/unpublish")
    assert again.status_code == 200
    assert again.json()["guide"]["published"] is False
    dispatch.assert_not_called()


def test_the_docx_export_reopens_as_a_valid_word_file(db_session, as_owner):
    from io import BytesIO

    from docx import Document

    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")

    res = client.get(f"{BASE}/{row.id}/docx")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert "how-to-work-with-owner-person.docx" in res.headers["content-disposition"]

    doc = Document(BytesIO(res.content))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert any("Owner Person" in t for t in texts)
    # The tinted callout is a single-cell table, and every tie-in lives in one —
    # so the markdown check has to walk the CELLS too. It did not, and that is
    # exactly how the literal ``**`` around a tie-in label survived into the
    # export unseen: nothing in a callout ever appears in ``doc.paragraphs``.
    assert doc.tables, "the tie-in citations are missing from the export"
    cells = [
        cell.text
        for table in doc.tables
        for row in table.rows
        for cell in row.cells
        if cell.text.strip()
    ]
    assert cells
    for text in texts + cells:
        assert "**" not in text, f"markdown survived the export: {text!r}"
    # The label is rendered as a run, not as asterisks, and it names a framework
    # — those boxes are the only place in the document that may.
    assert any("TIE-IN" in text.upper() for text in cells)


def test_the_export_renders_the_brand_hanging_numeral(db_session, as_owner):
    """The one branded device the markdown round-trip could have lost.

    ``render_markdown`` writes a guideline as ``### {n}. {heading}``, and the
    format spec puts the numeral in the brand colour at a display size with the
    heading in neutral ink beside it. Rendering the whole line as one bold run
    would look fine and be wrong, so the split is pinned: numeral run brand, heading run
    not, and the literal "1." gone from the text.
    """
    from io import BytesIO

    from docx import Document

    from rubik_core.deep_prep.docx import BRAND

    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")

    doc = Document(BytesIO(client.get(f"{BASE}/{row.id}/docx").content))
    numbered = [
        p
        for p in doc.paragraphs
        if len(p.runs) >= 2 and p.runs[0].text.strip().isdigit()
    ]
    assert len(numbered) == len(guide.build_guide(profile, display_name="X").guidelines)
    first = numbered[0]
    assert first.runs[0].text.strip() == "1"
    assert first.runs[0].font.color.rgb == BRAND
    assert first.runs[1].font.color.rgb != BRAND
    assert first.runs[1].text.strip()
    assert "1." not in first.text, "the markdown numbering must not survive as text"


def test_exporting_a_guide_with_no_body_is_refused(db_session, as_owner):
    profile = _full_profile(db_session)
    row = guide.ensure_guide_row(db_session, profile).document
    db_session.commit()
    assert client.get(f"{BASE}/{row.id}/docx").status_code == 409


def test_there_is_no_save_to_assets_route(db_session, as_owner):
    """A guide is never an Asset, so the subject's own POST 404s too.

    There was a ``POST /{document_id}/save-to-assets`` and it was a hole: the
    Asset row it registered let ``GET /api/assets/{doc_id}/download`` serve an
    UNPUBLISHED draft's bytes to the readers ``can_view_guide`` refuses. The
    reasoning for removing it rather than re-stamping it is at the site in
    ``backend/routers/smart_teams_guides.py``; the access consequences are pinned
    in ``backend/tests/test_smart_teams_guide_access.py``. This assertion is here
    so a reader of this file does not go looking for the endpoint.
    """
    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")
    assert client.post(f"{BASE}/{row.id}/save-to-assets").status_code == 404
    # The export is the only way out, and it is gated.
    assert client.get(f"{BASE}/{row.id}/docx").status_code == 200


def test_the_guide_router_is_reachable_outside_the_copilot_gate(db_session):
    """The mount is company-wide. A guide is only worth writing if everyone has
    one, and the gate on the router is the audience — this fails if anyone moves
    it onto the copilot-gated parent."""
    _login(_user("hr.person@acme.example", department="Human Resources"))
    try:
        assert client.get(f"{BASE}/me").status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── 9. The worker job ────────────────────────────────────────────────────────


def test_the_job_generates_into_a_pending_row(db_session):
    from backend.jobs import smart_teams_guide_job as job

    profile = _full_profile(db_session)
    _seed_user(db_session, OWNER, "Owner Person")
    row = guide.ensure_guide_row(db_session, profile).document
    db_session.commit()
    row_id = row.id

    with (
        _job_session(db_session),
        patch.object(job, "_record") as record,
        patch.object(guide, "write_prose", return_value={}),
    ):
        code = job.main(["--document-id", row_id, "--generate"])
    assert code == job.EXIT_OK
    record.assert_not_called()
    db_session.expire_all()
    stored = db_session.get(SmartTeamsDocument, row_id)
    assert stored.status == "ready"
    assert stored.content_md
    assert stored.error is None


def test_the_job_marks_a_row_failed_rather_than_leaving_it_pending(db_session):
    """A row stuck on ``pending`` is a person watching a spinner with nothing
    anywhere saying why."""
    from backend.jobs import smart_teams_guide_job as job

    profile = _full_profile(db_session)
    row = guide.ensure_guide_row(db_session, profile).document
    db_session.commit()
    row_id = row.id

    with (
        _job_session(db_session),
        patch.object(job, "_record") as record,
        patch.object(
            guide,
            "generate_into_row",
            side_effect=RuntimeError("vertex is having a day"),
        ),
    ):
        code = job.main(["--document-id", row_id, "--generate"])
    assert code == job.EXIT_ERROR
    assert record.call_args.args[1] == job.ACTION_GENERATE_FAILED
    db_session.expire_all()
    stored = db_session.get(SmartTeamsDocument, row_id)
    assert stored.status == "failed"
    assert "vertex is having a day" in stored.error


def test_the_job_records_an_unknown_document_and_does_not_retry_forever(db_session):
    from backend.jobs import smart_teams_guide_job as job

    with _job_session(db_session), patch.object(job, "_record") as record:
        assert job.main(["--document-id", "nope", "--purge"]) == job.EXIT_REFUSED
    assert record.call_args.args[1] == job.ACTION_PURGE_FAILED
    assert record.call_args.args[2]["retryable"] is False


def test_a_failed_purge_exits_non_zero_and_says_it_is_still_in_chat(db_session):
    """The one outcome in this job that is a privacy defect."""
    from backend.jobs import smart_teams_guide_job as job

    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    guide.approve(db_session, row, approver_email=OWNER)
    row.rag_ingested = True
    row.gcs_uri = "gs://b/x.md"
    db_session.commit()
    guide.unpublish(db_session, row)
    db_session.commit()
    row_id = row.id

    with (
        _job_session(db_session),
        patch.object(job, "_record") as record,
        patch("rubik_core.dam.vectorstore.delete_document_vectors", return_value=False),
    ):
        code = job.main(["--document-id", row_id, "--purge"])
    assert code == job.EXIT_ERROR
    details = record.call_args.args[2]
    assert record.call_args.args[1] == job.ACTION_PURGE_FAILED
    assert details["still_answerable_in_chat"] is True
    db_session.expire_all()
    assert db_session.get(SmartTeamsDocument, row_id).rag_ingested is True


def test_the_job_requires_exactly_one_action(db_session):
    from backend.jobs import smart_teams_guide_job as job

    with pytest.raises(SystemExit):
        job.main(["--document-id", "x"])
    with pytest.raises(SystemExit):
        job.main(["--document-id", "x", "--ingest", "--purge"])


def test_the_job_is_registered_for_the_worker(db_session):
    """A job nobody can spell is a job nobody runs.

    ``WORKER_JOBS`` is documentation with a test attached and not a dispatcher —
    Cloud Run invokes the module directly, so a missing entry cannot break a
    running job. It can only leave the guide job off the deploy checklist and out
    of ``worker_job_execute_command``, which is what an operator reaches for.

    Adding the entry is one edit to ``backend/jobs/__init__.py``, a file ST-6 does
    not own this phase, so this SKIPS rather than fails while it is outstanding —
    and asserts the entry is right the moment it lands.
    """
    from backend.jobs import WORKER_JOBS
    from backend.routers import smart_teams_guides as mod

    entry = WORKER_JOBS.get("smart-teams-guide")
    if entry is None:
        pytest.skip(
            "backend/jobs/__init__.py:WORKER_JOBS has no 'smart-teams-guide' "
            "entry yet — add one with module='backend.jobs."
            "smart_teams_guide_job' and dispatched_from='backend/routers/"
            "smart_teams_guides.py'"
        )
    assert entry.module == mod.GUIDE_JOB_MODULE
    assert entry.dispatched_from == "backend/routers/smart_teams_guides.py"
    assert entry.default_args == (), (
        "there is no meaningful bare run: --document-id and one action are "
        "required, so a bare execution must exit non-zero with a usage message"
    )


def test_the_router_is_mounted_in_main(db_session):
    """The guides router must be included in ``backend/main.py`` on ``app``.

    On ``app`` and NOT on the copilot-gated ``core`` parent: a guide is only worth
    writing if the whole company has one, and fencing it behind the copilot
    would lock out most of the company. Registering it is one line in a file
    ST-6 does not own this phase, so this SKIPS while it is outstanding — every
    other router test here runs against the private app above and is not waiting
    on it.
    """
    if not MOUNTED_IN_MAIN:
        pytest.skip(
            "backend/main.py does not include smart_teams_guides yet — add "
            'app.include_router(smart_teams_guides.router, prefix="/api") '
            "next to the smart_teams and smart_teams_uploads mounts"
        )
    assert any(getattr(route, "path", "").startswith(BASE) for route in main_app.routes)


# ── 10. Advisory-only, end to end ────────────────────────────────────────────


def test_no_rendered_guide_contains_advancement_language(db_session):
    """The assembled body is screened once more on the way into the row.

    Everything except the advisory notice, which is excluded deliberately: the
    notice promises the profile is "never used for performance review, promotion,
    or compensation decisions", so it contains the very word the advancement
    pattern matches on. Screening it rejected every guide ever generated — the
    disclaimer enforcing the advisory boundary tripped the check enforcing the
    advisory boundary. This test pins both halves of that: the notice is present
    in the stored body, and nothing else in the body trips the pattern.
    """
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    assert disc.ADVISORY_NOTICE in row.content_md
    rest = row.content_md.replace(disc.ADVISORY_NOTICE, "")
    assert guide_prompts.screen_prose(rest, max_words=10_000) is None
    assert strengths._ADVANCEMENT_PROSE.search(rest) is None
    # ...and the pattern really would have fired on the notice, so the exclusion
    # is load-bearing rather than defensive.
    assert strengths._ADVANCEMENT_PROSE.search(disc.ADVISORY_NOTICE) is not None


def test_a_screen_failure_on_the_assembled_body_fails_the_row(db_session):
    """Rather than storing it. A guide is a document about a real person."""
    profile = _full_profile(db_session)
    row = guide.ensure_guide_row(db_session, profile).document
    with patch.object(
        guide, "render_markdown", return_value="She has real potential for promotion."
    ):
        with pytest.raises(guide.GuideError):
            guide.generate_into_row(db_session, row, profile, display_name="Owner")
    assert row.status == "failed"
    assert "prose screen" in row.error


def test_every_rendered_guide_carries_the_advisory_notice(db_session):
    for profile in (_full_profile(db_session), _profile_row(db_session, version=2)):
        built = guide.build_guide(profile, display_name="Owner")
        assert disc.ADVISORY_NOTICE in guide.render_markdown(built)


def test_stale_and_current_versions_are_reported_to_the_client(db_session, as_owner):
    profile = _full_profile(db_session, version=1)
    _seed_user(db_session, OWNER, "Owner Person")
    _ready_guide(db_session, profile, name="Owner Person")
    assert client.get(f"{BASE}/me").json()["guide"]["stale"] is False

    _full_profile(db_session, version=2)
    body = client.get(f"{BASE}/me").json()["guide"]
    # /me returns the newest row for the subject, which after a retake is still
    # the v1 guide until Generate is pressed — so it must report itself stale.
    assert body["profile_version"] == 1
    assert body["current_profile_version"] == 2
    assert body["stale"] is True


def test_an_off_domain_subject_is_never_readable(db_session):
    """Fail-closed: an external address in a path is a 404 for everybody.

    ``meeting_participants`` carries external attendee addresses and the
    company's HubSpot portal can host a client's pipeline, so off-domain values
    reach real callers.
    """
    _login(_user(SYSADMIN, is_admin=True, department="Systems"))
    try:
        assert client.get(f"{BASE}/vendor@outside.example").status_code == 404
        assert client.get(f"{BASE}/support/vendor@outside.example").status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_document_dict_omits_the_body_when_asked(db_session):
    """The flag exists so a future caller cannot accidentally render prose it was
    not supposed to have."""
    from backend.routers.smart_teams_guides import _document_dict

    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile)
    assert "content_md" not in _document_dict(
        row, display_name="Owner", current_version=1, include_body=False
    )
    assert _document_dict(
        row, display_name="Owner", current_version=1, include_body=True
    )["content_md"]


# ── 11. Containment: the model rewrites facts, it never supplies them ────────
#
# The section that exists because this feature published, about a real person, a
# medical condition, a marital event, two skills they never claimed, and an
# Enneagram type asserted from a CLOSED gate — all of it model-written, all of it
# through the generic screen, into content_md, the .docx export and the
# companywide RAG corpus.

#: The four strings the wave-1 verifier actually got into a published guide.
SHIPPED_FABRICATIONS: tuple[str, ...] = (
    "I am an Enneagram Type 8 and a natural Systems Thinker, so lead with the "
    "ask. My INTJ wiring (derived from Big Five) means I decide fast.",
    "I burned out at my last job after my divorce",
    "I have ADHD so keep agendas short",
    "Ask me about Data Privacy and GAAP audits, which are my top two strengths.",
)


def _fabricating_writer(bodies: Sequence[str]):
    """A ``prose_writer`` that returns ``bodies`` round-robin over the slots."""

    def writer(slots, *, display_name):
        return {
            slot.slot_id: {
                "heading": "Talk to Me Plainly",
                "body": bodies[index % len(bodies)],
            }
            for index, slot in enumerate(slots)
        }

    return writer


def test_no_shipped_fabrication_can_reach_a_guide(db_session):
    """The four strings that actually shipped, against the profile they shipped on.

    DISC and strengths open; big_five, type16, motivators and enneagram refused.
    Every fabricated body must fall back to authored copy, be absent from
    ``content_md``, and be recorded in ``fallbacks`` so the rate is visible.
    """
    profile = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
            "motivators": _envelope(
                "motivators",
                None,
                reportable=False,
                reasons=(contracts.REASON_PROBE_PENDING,),
            ),
            "enneagram": _envelope(
                "enneagram",
                None,
                reportable=False,
                reasons=(contracts.REASON_UNRESOLVED,),
            ),
        },
    )
    authored = {
        g.slot_id: (g.heading, g.body)
        for g in guide.candidate_guidelines(guide.read_layers(profile))
    }
    built = guide.build_guide(
        profile,
        display_name="Owner",
        prose_writer=_fabricating_writer(SHIPPED_FABRICATIONS),
    )
    body = guide.render_markdown(built)

    for fabrication in SHIPPED_FABRICATIONS:
        assert fabrication not in body
    for guideline in built.guidelines:
        assert guideline.body == authored[guideline.slot_id][1]
        assert f"{guideline.slot_id}.body" in built.fallbacks
    # ...and the whole document still survives storage, approval and ingest.
    row = guide.ensure_guide_row(db_session, profile).document
    guide.generate_into_row(db_session, row, profile, display_name="Owner")
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    rag = guide.rag_document_text(row, display_name="Owner")
    for fabrication in SHIPPED_FABRICATIONS:
        assert fabrication not in row.content_md
        assert fabrication not in rag


def test_a_closed_gate_framework_cannot_be_named(db_session):
    """Naming a refused layer is refused, whatever else the sentence does.

    The worst half of the shipped defect: an Enneagram type and a four-letter code
    asserted about somebody whose enneagram and type16 gates were CLOSED, in the
    same document whose completeness section said those layers had no result.
    """
    profile = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
        },
    )
    layers = guide.read_layers(profile)
    slot = guide_prompts.ProseSlot(
        slot_id="disc.feedback",
        intent="how to give feedback",
        fact=disc.WORK_STYLES["D"].as_dict()["feedback"],
        heading="How to Give Me Feedback",
        body="x",
        open_frameworks=frozenset(
            name for name, read in layers.items() if read.reportable
        ),
    )
    for text in (
        "I read as The Line-Holder, so say it plainly.",
        "My Guiding Framework is what makes the work worth it.",
        "Structure and follow-through is where I sit, so say it plainly.",
        "My four letters (derived from Big Five) explain it.",
    ):
        assert guide_prompts.screen_slot_body(text, slot) == (
            guide_prompts.REJECT_CLOSED_FRAMEWORK
        ), text
    # A term from an OPEN framework is still refused when this slot's fact does
    # not contain it — an open gate is permission to cite that layer where it was
    # cited, not licence to name it anywhere.
    assert (
        guide_prompts.screen_slot_body(
            "Ask me about Pattern Reader work and say it plainly.", slot
        )
        == guide_prompts.REJECT_UNSOURCED_VOCABULARY
    )


def test_a_digit_the_fact_does_not_contain_is_refused(db_session):
    """ "Type 8", a percentage, a made-up count. No authored fact carries a digit."""
    slot = guide_prompts.ProseSlot(
        slot_id="disc.feedback",
        intent="how to give feedback",
        fact="Say it plainly, and say it once.",
        heading="How to Give Me Feedback",
        body="Say it plainly.",
        open_frameworks=frozenset(contracts.FRAMEWORKS),
    )
    for text in (
        "Say it plainly, and give me 2 days to think about it.",
        "Say it plainly — I am about 70 percent decided already.",
    ):
        assert guide_prompts.screen_slot_body(text, slot) == (
            guide_prompts.REJECT_UNSOURCED_NUMBER
        ), text
    assert not any(
        char.isdigit() for spec in strengths.THEMES.values() for char in spec.ask_for
    ), "if an authored fact ever carries a digit, this rule needs re-reading"


def test_legitimate_rephrasing_is_not_rejected(db_session):
    """A faithful paraphrase of every authored fact in a real guide survives.

    The other half of the trade. A net that refuses model prose wholesale would
    satisfy every containment test above and quietly turn every guide back into
    its authored fallback, which breaks the standing rule that a feature has to
    genuinely use AI.
    """
    profile = _full_profile(db_session)
    paraphrase = {fact: (body, heading) for fact, body, heading in _PARAPHRASES}
    covered = [
        g
        for g in guide.candidate_guidelines(guide.read_layers(profile))
        if g.fact in paraphrase
    ]
    assert covered, "the paraphrase corpus has drifted from the authored facts"

    def writer(slots, *, display_name):
        out = {}
        for slot in slots:
            if slot.fact in paraphrase:
                body, heading = paraphrase[slot.fact]
                out[slot.slot_id] = {"heading": heading, "body": body}
        return out

    built = guide.build_guide(profile, display_name="Owner", prose_writer=writer)
    assert built.fallbacks == {}, (
        "a faithful paraphrase of our own authored copy must not fall back"
    )
    assert built.model_written_count == len(covered)
    body = guide.render_markdown(built)
    for guideline in built.guidelines:
        if guideline.slot_id in {g.slot_id for g in covered}:
            assert guideline.body in body


def test_the_novel_content_floor_separates_paraphrase_from_fabrication():
    """The measurement ``MAX_NOVEL_CONTENT_WORDS`` was chosen from.

    Pinned so the separation cannot regress silently — a widened
    ``AUTHORED_VOCABULARY`` or a looser stemmer would move both distributions and
    nothing else in the suite would notice.
    """
    faithful = [
        len(guide_prompts.novel_content_words(body, fact))
        for fact, body, _heading in _PARAPHRASES
    ]
    fabricated = [
        len(guide_prompts.novel_content_words(body, fact))
        for fact, body, _heading in _FABRICATIONS
    ]
    assert len(faithful) == 32 and len(fabricated) == 14

    threshold = guide_prompts.MAX_NOVEL_CONTENT_WORDS
    assert threshold == 0
    caught = sum(1 for n in fabricated if n > threshold)
    fell_back = sum(1 for n in faithful if n > threshold)
    assert caught == len(fabricated), (
        f"only {caught}/{len(fabricated)} fabrications are caught at threshold "
        f"{threshold}; counts were {sorted(fabricated)}"
    )
    assert fell_back <= 1, (
        f"{fell_back}/{len(faithful)} faithful paraphrases now fall back; the "
        f"measurement in guide_prompts._CALIBRATION recorded 1. Counts: "
        f"{sorted(faithful)}"
    )
    # And the reason the threshold is 0 rather than 1: at 1 these survive.
    assert sum(1 for n in fabricated if n == 1) == 3


def test_the_subjects_own_words_do_not_fail_their_guide(db_session):
    """Finding: an ordinary thing a person said permanently failed generation.

    "promot" is a bare substring of the advancement pattern and the department
    names are in the third-party pattern, so anyone who mentioned a promotion, a
    promotional campaign, or a named team in their strengths conversation could
    never have a guide — and there was no recovery, because the quote comes from
    the scored payload and no endpoint can edit ``content_md``.

    The first fix exempted every verbatim span from the screen, and this test
    asserted the quote appeared in the body. That exemption turned out to remove
    the only check standing over quoted text — ten strings were then proven to
    publish a bare four-letter type, an Enneagram type from a closed gate, a raw
    domain score and a named colleague into a company-visible document.

    So what is asserted here has changed, and it is the narrower promise: a quote
    the screen refuses costs its CITATION BOX, not the guide. The document still
    generates, the guideline still renders, and the words simply are not quoted.
    The quote surviving in the body is no longer the guarantee; the guide
    surviving is. ``test_smart_teams_quote_screen.py`` pins both halves of the
    screen's calibration.
    """
    for index, quote in enumerate(
        (
            "I built the promotion tracker for the marketing launch.",
            "I rebuilt the intake form and the compliance team hated it.",
        ),
        start=1,
    ):
        # The screen really does refuse these, which is the whole point: the
        # question is what that costs the person.
        assert guide_prompts.screen_prose(quote) is not None

        layer = _strengths_layer()
        layer["themes"][0]["evidence_quote"] = quote
        profile = _profile_row(
            db_session,
            version=index,
            frameworks={
                "disc": _envelope("disc", _disc_layer(), reportable=True),
                "strengths": _envelope("strengths", layer, reportable=True),
                "enneagram": _envelope(
                    "enneagram", _enneagram_layer(), reportable=True
                ),
                "motivators": _envelope(
                    "motivators", _motivators_layer(), reportable=True
                ),
            },
        )
        row = guide.ensure_guide_row(db_session, profile).document
        built = guide.generate_into_row(db_session, row, profile, display_name="Owner")
        db_session.commit()
        # The guide generates. That is the finding this test exists for.
        assert row.status == "ready", row.error
        # And the refused quote is simply not in it — dropped at the point it
        # would have entered, rather than exempted from the screen or allowed to
        # fail the document.
        assert quote not in row.content_md
        assert quote not in built.verbatim_spans
        # The guideline the citation box sat under still renders, so the person
        # loses a quote and not a section.
        assert built.guidelines


def test_a_model_written_span_still_fails_the_assembled_screen(db_session):
    """The exemption is for the subject's words only, not for prose in general.

    Guarding the obvious way to over-read the fix above: the assembled-body screen
    is still fatal for everything except the advisory notice and the subject's own
    verbatim quote.
    """
    profile = _full_profile(db_session)
    row = guide.ensure_guide_row(db_session, profile).document
    with patch.object(
        guide, "render_markdown", return_value="She has real potential for promotion."
    ):
        with pytest.raises(guide.GuideError):
            guide.generate_into_row(db_session, row, profile, display_name="Owner")
    assert row.status == "failed"
    assert "prose screen" in row.error


# ── 12. Raw scores are the subject's and Systems' alone ─────────────────────


def test_raw_domain_scores_never_reach_the_body_export_or_corpus(db_session):
    """The derived-layer paragraph used to publish the subject's Big Five scores.

    ``type16.Type16Result.describe()`` interpolates ``call.source_score`` for every
    middle-band axis, and the guide rendered it verbatim: "T/F sat in the middle
    band (40 on people-weighting); J/P sat in the middle band (48 on structure and
    follow-through)". Published, exported, and ingested into the companywide
    corpus, so every employee could retrieve a colleague's raw domain scores
    through Chat.
    """
    layer = _type16_partial_coin_flip_layer()
    # The instrument's own subject-facing description really does carry them, so
    # this test is about the renderer the guide chooses and not about a shape that
    # no longer exists.
    assert "40 on people-weighting" in layer["description"]
    assert "48 on structure and follow-through" in layer["description"]

    profile = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
            "enneagram": _envelope("enneagram", _enneagram_layer(), reportable=True),
            "motivators": _envelope("motivators", _motivators_layer(), reportable=True),
            "type16": _envelope("type16", layer, reportable=True),
        },
    )
    row = _ready_guide(db_session, profile, name="Owner Person")
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    rag = guide.rag_document_text(row, display_name="Owner Person")
    data, _filename = guide.export_docx(row, display_name="Owner Person")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        docx_xml = archive.read("word/document.xml").decode("utf-8")

    for surface in (row.content_md, rag, docx_xml):
        assert "40 on people-weighting" not in surface
        assert "48 on structure and follow-through" not in surface
        # No bare number attached to any domain label, whatever the wording.
        for label in big_five.DOMAIN_LABELS.values():
            assert not re.search(
                rf"\d{{1,3}}\s+on\s+(?:the\s+)?{re.escape(label)}",
                surface,
                re.IGNORECASE,
            )
    # The section is still rendered — the fix is a safer paragraph, not a
    # dropped one — and it names the open axes without the scores.
    built = guide.build_guide(profile, display_name="Owner Person")
    assert built.derived_layer
    assert "T/F (people-weighting)" in built.derived_layer
    assert "J/P (structure and follow-through)" in built.derived_layer
    # ...and it speaks in the first person, like the rest of the document.
    assert "It is in your Big Five scores" not in built.derived_layer
    assert "One of the five things I answered for" in built.derived_layer


def test_the_screen_refuses_a_raw_score_attached_to_a_domain_label():
    """So it cannot come back through another instrument's prose."""
    assert (
        guide_prompts.screen_prose(
            "T/F sat in the middle band (40 on people-weighting).", max_words=100
        )
        == guide_prompts.REJECT_RAW_DOMAIN_SCORE
    )
    assert (
        guide_prompts.screen_prose(
            "My structure and follow-through came out at 48.", max_words=100
        )
        == guide_prompts.REJECT_RAW_DOMAIN_SCORE
    )
    # The band descriptions our own Big Five section renders carry no numbers,
    # so the rule costs nothing there.
    for domain in big_five.DOMAIN_LABELS:
        for band in big_five.BAND_LABELS:
            assert (
                guide_prompts.screen_prose(
                    big_five.describe_domain(domain, band), max_words=200
                )
                is None
            )


# ── 13. A refused layer's completeness line names no result ─────────────────


def test_a_refused_layers_completeness_line_names_no_result(db_session):
    """An instrument hedge can BE a finding, so none of them is rendered here.

    ``motivators`` refused for ``probe_pending`` hedges by naming its top two and
    its bottom; ``enneagram`` refused as ``unresolved`` hedges by naming both
    candidate types. Both used to be printed into a company-visible, RAG-ingested
    document, under a line that says the layer is not included.
    """
    motivator_hedge = (
        "Driven most by Guiding Framework and Human Impact (their answers put "
        "these two close together); Practical Return moves them least."
    )
    enneagram_hedge = (
        "Your answers land on The Supporter and The Peace-Keeper about equally, "
        "so this is not settled."
    )
    profile = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
            "motivators": _envelope(
                "motivators",
                None,
                reportable=False,
                reasons=(contracts.REASON_PROBE_PENDING,),
                hedge=motivator_hedge,
            ),
            "enneagram": _envelope(
                "enneagram",
                None,
                reportable=False,
                reasons=(contracts.REASON_UNRESOLVED,),
                hedge=enneagram_hedge,
            ),
        },
    )
    row = _ready_guide(db_session, profile, name="Owner Person")
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    rag = guide.rag_document_text(row, display_name="Owner Person")

    for surface in (row.content_md, rag):
        assert motivator_hedge not in surface
        assert enneagram_hedge not in surface
        # ...and not just the whole sentence: none of the results it named.
        for named in (
            "Guiding Framework",
            "Human Impact",
            "Practical Return",
            "The Supporter",
            "The Peace-Keeper",
        ):
            assert named not in surface, f"{named!r} came from a refused layer"

    lines = {
        line.framework: line for line in guide.completeness(guide.read_layers(profile))
    }
    assert (
        lines["motivators"].note
        == (guide.PUBLICATION_REASON_PROSE[contracts.REASON_PROBE_PENDING])
    )
    assert (
        lines["enneagram"].note
        == (guide.PUBLICATION_REASON_PROSE[contracts.REASON_UNRESOLVED])
    )


def test_every_publication_reason_names_nothing_and_speaks_as_i(db_session):
    """Every line in the mapping, not only the two that shipped a leak.

    Two properties. It may not name a framework term — that is what makes it safe
    to print for a layer the engine refused. And it may speak as "I" or about the
    section, but never as "you" or "they": the instrument hedges gave this section
    away by addressing the subject in the second person ("Your answers land on…")
    or describing them in the third ("their answers put these two close
    together"), inside a document written in the first person.
    """
    for code, note in guide.PUBLICATION_REASON_PROSE.items():
        assert note.strip()
        for pronoun in ("your", "yours", "you", "their", "them", "they"):
            assert not re.search(rf"\b{pronoun}\b", note, re.IGNORECASE), (
                f"{code}: {pronoun!r} — this guide is written as 'I'"
            )
        assert guide_prompts.screen_prose(note, max_words=100) is None, code
        for framework, terms in guide_prompts.FRAMEWORK_TERMS.items():
            for term in terms:
                assert term.lower() not in note.lower(), (
                    f"{code} names {term!r} from {framework}"
                )


def test_a_reportable_layers_own_note_is_still_carried(db_session):
    """The change is scoped to REFUSED layers.

    A reportable layer's hedge qualifies a finding this guide is already
    publishing — the Enneagram always carries one, inviting the person to correct
    it — so it can only soften what is on the page, never add what is not.
    """
    note = "Worth arguing with: I recognise myself in the next one along too."
    profile = _profile_row(
        db_session,
        frameworks={
            "disc": _envelope("disc", _disc_layer(), reportable=True),
            "strengths": _envelope("strengths", _strengths_layer(), reportable=True),
            "enneagram": _envelope(
                "enneagram", _enneagram_layer(), reportable=True, hedge=note
            ),
            "motivators": _envelope("motivators", _motivators_layer(), reportable=True),
        },
    )
    lines = {
        line.framework: line for line in guide.completeness(guide.read_layers(profile))
    }
    assert lines["enneagram"].included is True
    assert lines["enneagram"].note == f"Included. {note}"


# ═════════════════════════════════════════════════════════════════════════════
# Format parity with the two source guides Emery circulated
# ═════════════════════════════════════════════════════════════════════════════
#
# Those PDFs ARE the format spec (docs/smart_teams/plan.md). What we shipped
# first diverged from them in ways a reader notices immediately: no page-1
# contents index, lower-case structural headings, no running footer, no
# sign-off. These pin the shape so it cannot drift back.


def _rendered_pair(db_session):
    """A full guide, as markdown and as the Word export's document.xml."""
    profile = _full_profile(db_session)
    built = guide.build_guide(profile, display_name="Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    data, _filename = guide.export_docx(row, display_name="Owner Person")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        footer_xml = "".join(
            archive.read(n).decode("utf-8")
            for n in archive.namelist()
            if n.startswith("word/footer")
        )
    return built, guide.render_markdown(built), document_xml, footer_xml, data


def test_the_contents_index_lists_every_guideline_once(db_session):
    """Page 1 of the source is mostly this index; we shipped without one."""
    built, body, _xml, _footer, _data = _rendered_pair(db_session)
    heading = guide.CONTENTS_TEMPLATE.format(
        count=len(built.guidelines), noun="things"
    ).upper()
    assert f"## {heading}" in body
    for index, guideline in enumerate(built.guidelines, start=1):
        entry = f"- **{index:02d}**  {guideline.heading}"
        assert body.count(entry) == 1, f"{entry!r} should appear exactly once"
        # ...and the section itself still follows, so the index is an index
        # rather than a replacement for the content.
        assert f"### {index}. {guideline.heading}" in body


def test_the_word_export_sets_the_index_as_a_two_column_grid(db_session):
    """A bulleted list is not the source's layout; a 2-column table is.

    Filled DOWN each column — 01-05 left, 06-10 right in the source — so a
    reader scanning the left column gets the first half of the guide.
    """
    from docx import Document

    built, _body, _xml, _footer, data = _rendered_pair(db_session)
    doc = Document(io.BytesIO(data))
    grids = [t for t in doc.tables if len(t.columns) == 2]
    assert len(grids) == 1, "expected exactly one two-column contents grid"

    count = len(built.guidelines)
    rows = (count + 1) // 2
    assert len(grids[0].rows) == rows

    cells = [
        grids[0].rows[position % rows].cells[position // rows].text.strip()
        for position in range(count)
    ]
    for index, (guideline, text) in enumerate(zip(built.guidelines, cells), start=1):
        assert text.startswith(f"{index:02d}"), f"column fill order wrong at {index}"
        assert guideline.heading in text


def test_the_running_footer_names_the_subject_and_numbers_the_pages(db_session):
    """The source runs "WORKING WITH SHAY VAUGHN   1 / 5" on every page.

    The confidential line the house helper writes is kept as well — it is a real
    obligation on an internal document about a person, not decoration.
    """
    _built, _body, _xml, footer_xml, _data = _rendered_pair(db_session)
    assert "WORKING WITH OWNER PERSON" in footer_xml
    # A live PAGE field, not a frozen number: the guide's length varies with how
    # much of the interview somebody finished.
    assert "PAGE" in footer_xml
    assert "Confidential" in footer_xml


def test_the_guide_is_signed_off_with_a_first_name(db_session):
    """The source closes with a sign-off; we ended on a legal notice."""
    _built, body, _xml, _footer, _data = _rendered_pair(db_session)
    assert "— Owner" in body
    final_at = body.index(guide.FINAL_THOUGHT_LABEL.upper())
    assert body.index("— Owner") > final_at, "the sign-off follows the final thought"


def test_structural_headings_are_capitalised_and_guideline_headings_are_not(
    db_session,
):
    """The source's own convention: structure in caps, content in title case."""
    built, body, _xml, _footer, _data = _rendered_pair(db_session)
    for label in (
        guide.FINAL_THOUGHT_LABEL,
        guide.COMPLETENESS_LABEL,
    ):
        assert f"## {label.upper()}" in body
        assert f"## {label}" not in body, f"{label!r} rendered in mixed case"
    # Guideline headings keep their own casing — caps everywhere would shout.
    for index, guideline in enumerate(built.guidelines, start=1):
        assert f"### {index}. {guideline.heading}" in body


def test_the_advisory_notice_and_completeness_survive_below_the_signature(
    db_session,
):
    """Matching the source must not mean deleting these.

    Neither source guide has a completeness table or an advisory notice, so
    "make it exactly the same" taken literally would drop both. The completeness
    table is how a reader learns a layer was WITHHELD rather than that the person
    lacks the trait, and the advisory notice is the guardrail keeping a
    company-visible document about a person out of performance territory. They
    move below the sign-off; they do not leave.
    """
    built, body, _xml, _footer, _data = _rendered_pair(db_session)
    signature_at = body.index("— Owner")
    assert body.index(guide.COMPLETENESS_LABEL.upper()) > signature_at
    assert built.advisory_notice in body
    assert body.index(built.advisory_notice) > signature_at


# ═════════════════════════════════════════════════════════════════════════════
# The PDF export — the copy meant to be sent rather than edited
# ═════════════════════════════════════════════════════════════════════════════


def test_the_pdf_and_the_word_export_say_the_same_thing(db_session):
    """Two renderers of one document must not disagree.

    Both read the stored ``content_md`` through the same
    ``guide.parse_guide_blocks``, so this compares the text each one actually
    emitted rather than trusting that they were written to match.
    """
    import pdfplumber
    from docx import Document

    profile = _full_profile(db_session)
    built = guide.build_guide(profile, display_name="Owner Person")
    row = _ready_guide(db_session, profile, name="Owner Person")
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()

    pdf_bytes, pdf_name = guide.export_pdf(row, display_name="Owner Person")
    docx_bytes, docx_name = guide.export_docx(row, display_name="Owner Person")
    assert pdf_name.endswith(".pdf") and docx_name.endswith(".docx")
    assert pdf_name[:-4] == docx_name[:-5], "the two exports disagree on the stem"
    assert pdf_bytes.startswith(b"%PDF-"), "not a PDF"

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as opened:
        pdf_text = " ".join((p.extract_text() or "") for p in opened.pages)
    doc = Document(io.BytesIO(docx_bytes))
    docx_text = " ".join(
        [p.text for p in doc.paragraphs]
        + [c.text for t in doc.tables for r in t.rows for c in r.cells]
    )

    def flat(text: str) -> str:
        # The PDF maps em dashes to hyphens for the latin-1 core font, and both
        # wrap lines differently, so compare on collapsed whitespace and ASCII
        # dashes rather than on exact strings.
        return re.sub(r"\s+", " ", text.replace("—", "-")).strip()

    flat_pdf, flat_docx = flat(pdf_text), flat(docx_text)
    for guideline in built.guidelines:
        heading = flat(guideline.heading)
        assert heading in flat_pdf, f"{heading!r} missing from the PDF"
        assert heading in flat_docx, f"{heading!r} missing from the Word export"
    assert flat(guide.FINAL_THOUGHT_LABEL.upper()) in flat_pdf
    assert flat(built.advisory_notice) in flat_pdf


def test_the_pdf_carries_the_running_footer_on_every_page(db_session):
    """ "WORKING WITH {NAME}  n / N", the way the source does it."""
    import pdfplumber

    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile, name="Owner Person")
    guide.approve(db_session, row, approver_email=OWNER)
    db_session.commit()
    data, _name = guide.export_pdf(row, display_name="Owner Person")

    with pdfplumber.open(io.BytesIO(data)) as opened:
        total = len(opened.pages)
        for number, page in enumerate(opened.pages, start=1):
            footer = page.crop((0, page.height - 70, page.width, page.height))
            text = re.sub(r"\s+", " ", footer.extract_text() or "")
            assert "WORKING WITH OWNER PERSON" in text, f"page {number} has no footer"
            # The total is fpdf2's "{nb}" alias; if it failed to resolve the
            # literal braces would still be sitting there.
            assert f"{number} / {total}" in text, f"page {number} numbering wrong"
            assert "{nb}" not in text


def test_no_guide_character_is_lost_to_the_latin_1_core_font(db_session):
    """The reason a PDF was deferred, checked instead of assumed.

    ``export_docx``'s docstring deferred a PDF because the house renderer is
    latin-1 only and "a name outside latin-1 would render as ?". Measured: no
    roster name is lossy, and the only lossy character in a guide is the em
    dash, which ``deep_prep.pdf._PUNCT`` already maps to a hyphen — the same
    hyphen the source PDFs use ("Give Me Ownership - Not a Script"). This fails
    the day that stops being true, which is the point.
    """
    from rubik_core.smart_teams.guide_pdf import unsupported_characters

    profile = _full_profile(db_session)
    built = guide.build_guide(profile, display_name="Owner Person")
    body = guide.render_markdown(built)
    assert unsupported_characters(body) == set(), (
        "a guide now contains characters the latin-1 core font cannot render; "
        "bundle a Unicode TTF rather than shipping a '?' into somebody's guide"
    )
    # And an accented name — the case the docstring worried about — survives.
    assert unsupported_characters("Blair Ellison") == set()


def test_a_guide_with_no_body_refuses_both_exports(db_session):
    """Same refusal, so neither format is the one that produces an empty file."""
    profile = _full_profile(db_session)
    row = _ready_guide(db_session, profile, name="Owner Person")
    row.content_md = ""
    db_session.commit()
    for export in (guide.export_pdf, guide.export_docx):
        with pytest.raises(guide.GuideError):
            export(row, display_name="Owner Person")
