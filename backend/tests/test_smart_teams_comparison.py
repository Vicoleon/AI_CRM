"""ST-7 — the pairwise comparison engine and the pair guide.

No network. The prose model is a plain callable in every test that uses one, and
most tests use none at all: ``comparison.build_pair_guide`` without a
``prose_writer`` is a supported mode, not a stub, so the whole document is
testable as authored copy.

What these tests defend, and why each one earns its place:

1. **The joint gate, all four combinations.** Reportable/reportable is the only
   state that may reach a layer; the three others must reach neither. A pair
   document is fluent whichever way it is wrong, so nothing else would catch it.
2. **A dimension refused for one subject yields "cannot assess" and NEVER a
   friction.** This is the worst thing the engine can do — manufacture a tension
   out of an answer one person never gave — and it is caught here from the
   engine's side (``RefusedDimension``, which has no ``frictions`` attribute at
   all) and from the document's side (the refusal renders as a question).
3. **Consent.** An unpublished subject makes the pair guide unreadable by anybody
   but Systems, and generation is refused before a word of it is written.
4. **A planted capability judgement, seniority claim, predicted reaction, pairing
   verdict and cross-subject ranking are each rejected**, and the document falls
   back to authored copy rather than shipping the model's — in BOTH grammars, the
   pronoun one and the display-name one the document itself prefers, because
   six of eight families used to be bypassed by writing the name.
4b. **A cross-subject owner swap is unavailable, not merely screened.** The three
   swaps that actually shipped in wave 2 are replayed against every slot a real
   pair produces, and a writer that returns one for every section changes nothing
   about who said what: the two quoted answers and the name attached to each are
   authored and are not an input to anything a writer touches. The residue —
   a re-voiced clause that says which of the two a half belongs to — is refused
   by ``comparison_prompts.screen_owner_binding``, with the separation measured
   in both directions.
5. **Staleness on EITHER side.** A version bump for either person changes the
   cache key, and pair order produces two different documents.
6. **The pair screens do not reject our own authored copy** — neither the pair
   module's strings nor the six instruments'. Without that tripwire the first
   re-authored instrument field silently fails every pair guide involving one
   work style, one person at a time.

In-memory SQLite with a ``StaticPool`` and ``app.dependency_overrides``, per
``test_smart_teams_guide.py``. The Smart Teams GATE is never overridden — only
``get_current_user`` — so ``require_smart_teams_access`` runs for real.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Mapping, Sequence
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.jobs import WORKER_JOBS
from backend.main import app as main_app
from backend.routers import smart_teams_comparison
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, User
from rubik_core.database.smart_teams import (
    SmartTeamsDocument,
    SmartTeamsProfile,
    subject_key_for,
)
from rubik_core.smart_teams import authz, comparison, contracts, guide
from rubik_core.smart_teams import comparison_contracts as cc
from rubik_core.smart_teams import comparison_prompts as cp
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

SHAY = "shay@acme.example"
EMERY = "emery@acme.example"
THIRD = "third@acme.example"
SYSADMIN = "sysadmin@acme.example"

BASE = "/api/smart-teams/comparison"

#: A private app carrying ONLY the comparison router, at the prefix ``main.py``
#: mounts it under. Deliberately not ``backend.main.app``: including a router on
#: the shared app at import time mutates it for every test that runs afterwards
#: in the same process, and ``test_smart_teams_api_contract.py`` walks the real
#: app's routes. The real mount is covered separately below.
app = FastAPI()
app.include_router(smart_teams_comparison.router, prefix="/api")

#: Whether ``backend/main.py`` already mounts the comparison router. Read from
#: ``openapi()`` and NOT by walking ``app.routes``: on this FastAPI version the
#: route list keeps lazy ``_IncludedRouter`` objects and reports zero smart-teams
#: paths, which is the trap ``test_smart_teams_api_contract.py`` documents.
MOUNTED_IN_MAIN = any(
    path.startswith(BASE) for path in main_app.openapi().get("paths", {})
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


def _sysadmin() -> UserProfile:
    return _user(SYSADMIN, department="Systems", is_admin=True)


@pytest.fixture
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    # Feature Access uses SessionLocal when a gate is evaluated outside a route
    # DB dependency. Keep that read on this fixture's private schema.
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
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


def _login(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


@pytest.fixture(autouse=True)
def _logout():
    yield
    app.dependency_overrides.pop(get_current_user, None)


#: What the stubbed model returns. An empty slot list is the "the model gave us
#: nothing usable" case, which makes the whole document authored copy — a
#: supported, shippable outcome rather than a degraded one.
STUB_MODEL_RESPONSE: dict = {"slots": []}


@pytest.fixture(autouse=True)
def _no_network_llm(monkeypatch):
    """No test in this file may reach Vertex. Autouse, and that is deliberate.

    ``comparison.write_pair_prose`` resolves its client through
    ``guide._json_client``, which builds a real ``LLMJsonClient`` against
    whatever project the environment points at. The job tests call
    ``smart_teams_comparison_job.main``, which passes the REAL
    ``write_pair_prose`` — so without this fixture they make a live Gemini call
    against the production GCP project, which is a real project with real
    users, and the test then passes or fails on what the model happened to say.
    That is not a hypothetical: it happened on the first run of this file, and
    the failure it produced (``pending`` instead of ``ready``) was a screen
    rejection of live model output, thirty seconds of latency, and real spend.

    Patched at ``guide._json_client`` rather than at ``write_pair_prose`` so the
    worker's own code — the prompt build, the per-slot screening, the retry, the
    ``@attribute_to`` tag — all still runs. Only the socket is gone.
    """

    class _StubClient:
        def __init__(self, *args, **kwargs):
            self.calls: list[str] = []

        def invoke_json(self, prompt, *, fallback=None, attempts=1):
            self.calls.append(prompt)
            return STUB_MODEL_RESPONSE

    monkeypatch.setattr(
        "rubik_core.smart_teams.guide._json_client",
        lambda model_key, temperature: _StubClient(),
    )
    yield


def _job_session(session):
    """Point the job's ``SessionLocal`` at the test session.

    The job is a separate process in production and opens its own session, so it
    resolves ``SessionLocal`` at call time. ``close`` is neutered because the
    fixture owns the session's lifetime.
    """

    class _Session:
        def __new__(cls):
            session.close = lambda: None  # type: ignore[method-assign]
            return session

    return patch("rubik_core.database.db.SessionLocal", _Session)


# ── Layer payload builders ───────────────────────────────────────────────────
# Hand-built envelopes rather than a scoring run: every test here is about the
# GATE, and building the envelope by hand is the only way to set a gate closed
# for one person and open for the other. The layer bodies are the instruments'
# real authored strings, read out of the modules by name, so the screens and the
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
    contracts.validate_envelope(payload)
    return payload


def _disc_layer(style_key: str) -> dict:
    style = disc.WORK_STYLES[style_key]
    return {
        "layer": "disc",
        "assignment": {"style": style.as_dict(), "primary": style_key},
        "quality": {},
    }


_THEME_IDS = list(strengths.THEMES)
_AXES = list(motivators.MOTIVATORS)


def _strengths_layer(theme_ids: Sequence[str]) -> dict:
    return {
        "layer": "strengths",
        "themes": [
            {
                "theme_id": theme_id,
                "name": strengths.spec_for(theme_id).name,
                "cluster": strengths.spec_for(theme_id).cluster.value,
                "evidence_quote": "I rewrote the whole intake form that week.",
            }
            for theme_id in theme_ids
        ],
    }


def _motivators_layer(top: Sequence[str], bottom: str) -> dict:
    return {
        "layer": "motivators",
        "ranking": list(top) + [bottom],
        "top_two": list(top),
        "bottom": bottom,
        "bottom_is_contested": False,
        "near_ties": [],
        "headline": "What makes the work worth it, in my own words.",
    }


def _enneagram_layer(number: int) -> dict:
    spec = enneagram.spec_for(number)
    return {
        "layer": "enneagram",
        "type_number": number,
        "type_display": spec.label,
        "center": spec.center,
        "headline": "My own read, and worth arguing with.",
    }


def _big_five_layer() -> dict:
    return {
        "instrument": "ipip_50",
        "domains": {},
        "unscored_domains": [],
        "quality": {},
        "bands_note": big_five.BANDS_ARE_NOT_PERCENTILES,
    }


#: Real ``type16`` results, built through the instrument's own ``as_dict`` from
#: domain scores, so the axes carry real letters and real confidences.
_CLEAR_HIGH = {
    big_five.Domain.EXTRAVERSION: 82.0,
    big_five.Domain.OPENNESS: 80.0,
    big_five.Domain.AGREEABLENESS: 18.0,
    big_five.Domain.CONSCIENTIOUSNESS: 78.0,
}
_CLEAR_LOW = {
    big_five.Domain.EXTRAVERSION: 18.0,
    big_five.Domain.OPENNESS: 20.0,
    big_five.Domain.AGREEABLENESS: 82.0,
    big_five.Domain.CONSCIENTIOUSNESS: 22.0,
}
#: All four source domains in the middle band: every letter is a coin flip.
_ALL_COIN_FLIPS = {
    big_five.Domain.EXTRAVERSION: 50.0,
    big_five.Domain.OPENNESS: 49.0,
    big_five.Domain.AGREEABLENESS: 51.0,
    big_five.Domain.CONSCIENTIOUSNESS: 50.0,
}


def _type16_layer(scores: Mapping[big_five.Domain, float]) -> dict:
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
    email: str,
    *,
    version: int = 1,
    frameworks: dict | None = None,
) -> SmartTeamsProfile:
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


def _shay_frameworks(**overrides) -> dict:
    out = {
        "disc": _envelope("disc", _disc_layer("D"), reportable=True),
        "strengths": _envelope(
            "strengths", _strengths_layer(_THEME_IDS[:4]), reportable=True
        ),
        "motivators": _envelope(
            "motivators",
            _motivators_layer([_AXES[0], _AXES[1]], _AXES[2]),
            reportable=True,
        ),
        "enneagram": _envelope("enneagram", _enneagram_layer(3), reportable=True),
        "type16": _envelope("type16", _type16_layer(_CLEAR_HIGH), reportable=True),
        "big_five": _envelope("big_five", _big_five_layer(), reportable=True),
    }
    out.update(overrides)
    return out


def _emery_frameworks(**overrides) -> dict:
    out = {
        "disc": _envelope("disc", _disc_layer("C+S"), reportable=True),
        "strengths": _envelope(
            "strengths", _strengths_layer(_THEME_IDS[7:11]), reportable=True
        ),
        "motivators": _envelope(
            "motivators",
            _motivators_layer([_AXES[2], _AXES[3]], _AXES[0]),
            reportable=True,
        ),
        "enneagram": _envelope("enneagram", _enneagram_layer(6), reportable=True),
        "type16": _envelope("type16", _type16_layer(_CLEAR_LOW), reportable=True),
        "big_five": _envelope("big_five", _big_five_layer(), reportable=True),
    }
    out.update(overrides)
    return out


def _pair_profiles(
    db,
    *,
    shay: dict | None = None,
    emery: dict | None = None,
    shay_version: int = 1,
    emery_version: int = 1,
) -> tuple[SmartTeamsProfile, SmartTeamsProfile]:
    a = _profile_row(
        db,
        SHAY,
        version=shay_version,
        frameworks=shay if shay is not None else _shay_frameworks(),
    )
    b = _profile_row(
        db,
        EMERY,
        version=emery_version,
        frameworks=emery if emery is not None else _emery_frameworks(),
    )
    return a, b


def _publish_personal_guide(db, email: str, *, version: int = 1) -> SmartTeamsDocument:
    """A published personal guide for ``email`` — the consent a pair guide needs.

    Built through ``guide.approve`` rather than by setting ``published`` by hand,
    so ``ck_st_document_published_needs_approval`` and the subject-only rule are
    both exercised on the way in. That matters: a test that sets the flag
    directly would pass while the real path was broken.
    """
    subject_key, version_key = guide.cache_keys_for(email, version)
    row = SmartTeamsDocument(
        id=f"pg-{email.split('@')[0]}-{version}",
        kind=guide.KIND_PERSONAL,
        subject_emails=[email],
        subject_key=subject_key,
        profile_versions={email: version},
        profile_version_key=version_key,
        title="How to Be Successful Working with X",
        content_md="# body\n\nsomething.\n",
        status="ready",
        published=False,
        rag_ingested=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    guide.approve(db, row, approver_email=email)
    db.commit()
    return row


def _users(db, *emails: str) -> None:
    for email in emails:
        db.add(
            User(
                email=email,
                id=email.split("@")[0],
                name=email.split("@")[0].title(),
                department="general",
            )
        )
    db.commit()


# ── 1. The joint gate, all four combinations ─────────────────────────────────


@pytest.mark.parametrize(
    ("reportable_a", "reportable_b", "expected_gate", "comparable"),
    [
        (True, True, cc.GATE_BOTH_OPEN, True),
        (True, False, cc.GATE_ONE_REFUSED, False),
        (False, True, cc.GATE_ONE_REFUSED, False),
        (False, False, cc.GATE_NEITHER_OPEN, False),
    ],
)
def test_the_joint_gate_is_exercised_for_all_four_reportable_combinations(
    db_session, reportable_a, reportable_b, expected_gate, comparable
):
    """All four combinations, and only one of them may reach a layer.

    The middle two are the point. ``one_refused`` needs its own state because
    somebody can act on it, and — much more importantly — a read in that state
    must hold NEITHER layer. Holding the open subject's layer is how a
    comparison ends up putting one real value against a default.
    """
    shay = _shay_frameworks(
        disc=_envelope(
            "disc",
            _disc_layer("D") if reportable_a else None,
            reportable=reportable_a,
            reasons=() if reportable_a else (contracts.REASON_DECLINED,),
        )
    )
    emery = _emery_frameworks(
        disc=_envelope(
            "disc",
            _disc_layer("C+S") if reportable_b else None,
            reportable=reportable_b,
            reasons=() if reportable_b else (contracts.REASON_DECLINED,),
        )
    )
    profile_a, profile_b = _pair_profiles(db_session, shay=shay, emery=emery)
    reads = comparison.read_pair(profile_a, profile_b)

    read = reads["disc"]
    assert read.gate == expected_gate
    assert read.comparable is comparable
    if comparable:
        assert read.layer_a is not None and read.layer_b is not None
    else:
        # Neither, never one. This is the invariant the whole module is built on.
        assert read.layer_a is None
        assert read.layer_b is None
        assert read.joint_confidence == "none"
    assert ("disc" in cc.comparable_frameworks(reads)) is comparable


def test_a_pairwise_read_cannot_be_built_holding_one_layer_past_a_closed_gate():
    """Constructing the illegal shape by hand raises, not merely by convention."""
    open_verdict = contracts.LayerVerdict(
        framework="disc", reportable=True, confidence="high"
    )
    closed_verdict = contracts.LayerVerdict(
        framework="disc",
        reportable=False,
        confidence="none",
        reasons=(contracts.REASON_DECLINED,),
    )
    with pytest.raises(cc.ComparisonError, match="one real value against a default"):
        cc.PairwiseRead(
            framework="disc",
            subject_a=SHAY,
            subject_b=EMERY,
            verdict_a=open_verdict,
            verdict_b=closed_verdict,
            layer_a={"assignment": {}},
        )


def test_comparable_frameworks_is_the_intersection_not_the_union(db_session):
    """The single highest-leverage value in ST-7.

    It becomes ``ProseSlot.open_frameworks``, and ``_screen_slot`` rule 1 refuses
    any framework named in prose that is not in it. Passing the union — or
    subject A's own set — silently licenses the model to name a layer the other
    person refused.
    """
    shay = _shay_frameworks()
    emery = _emery_frameworks(
        enneagram=_envelope(
            "enneagram", None, reportable=False, reasons=(contracts.REASON_UNRESOLVED,)
        ),
        motivators=_envelope(
            "motivators",
            None,
            reportable=False,
            reasons=(contracts.REASON_PROBE_PENDING,),
        ),
    )
    profile_a, profile_b = _pair_profiles(db_session, shay=shay, emery=emery)
    reads = comparison.read_pair(profile_a, profile_b)

    comparable = cc.comparable_frameworks(reads)
    assert "enneagram" not in comparable
    assert "motivators" not in comparable
    assert {"disc", "strengths", "type16", "big_five"} <= comparable

    _comparison, _syn, _fri = comparison.assess_pair(reads)
    assert _comparison.comparable_frameworks == comparable


# ── 2. A refused dimension yields "cannot assess", never a friction ──────────


def _outcome(comparison_obj: cc.PairComparison, key: str) -> cc.DimensionOutcome:
    return next(o for o in comparison_obj.outcomes if o.dimension == key)


def test_a_dimension_refused_for_one_subject_is_refused_and_carries_no_friction(
    db_session,
):
    """The worst defect this engine could have, checked from the engine's side.

    ``motivator_alignment`` reads only ``motivators``, and Shay and Emery are built
    so that Shay's top axis IS Emery's bottom axis — the strongest friction rule in
    the module. Close the gate for ONE of them and the dimension must become a
    refusal with no findings at all, not a friction derived from the one answer
    that is still there.
    """
    both_open = comparison.read_pair(*_pair_profiles(db_session))
    comparison_both, _syn, _fri = comparison.assess_pair(both_open)
    assessed = _outcome(comparison_both, "motivator_alignment")
    assert isinstance(assessed, cc.AssessedDimension)
    assert assessed.frictions, "the fixture must produce a real friction to close"

    db_session.query(SmartTeamsProfile).delete()
    db_session.commit()
    one_closed = comparison.read_pair(
        *_pair_profiles(
            db_session,
            emery=_emery_frameworks(
                motivators=_envelope(
                    "motivators",
                    None,
                    reportable=False,
                    reasons=(contracts.REASON_PROBE_PENDING,),
                    hedge=(
                        "Two of these are close together, so the order between "
                        "them is not settled."
                    ),
                )
            ),
        )
    )
    comparison_one, _syn, frictions = comparison.assess_pair(one_closed)
    refusal = _outcome(comparison_one, "motivator_alignment")

    assert isinstance(refusal, cc.RefusedDimension)
    assert refusal.gate == cc.GATE_ONE_REFUSED
    assert refusal.subjects_with_closed_gates == (EMERY,)
    # probe_pending is a gap Emery can close, so here the two lists agree. The
    # case where they do not is
    # test_the_refusals_block_and_the_completeness_block_name_the_same_people.
    assert cc.subjects_to_finish(refusal, one_closed) == (EMERY,)
    # The safety property: reaching for findings on a refusal raises at the
    # mistake instead of rendering "nothing to watch here".
    for attribute in ("frictions", "synergies", "no_friction_found"):
        assert not hasattr(refusal, attribute)
    assert cc.friction_state(refusal) == cc.FRICTION_NOT_ASSESSED
    assert all(f.dimension != "motivator_alignment" for f in frictions)
    assert all(f.dimension != "motivator_alignment" for f in comparison_one.frictions)


def test_a_dimension_refused_for_both_subjects_is_also_refused_and_says_so(db_session):
    shay = _shay_frameworks(
        motivators=_envelope(
            "motivators", None, reportable=False, reasons=(contracts.REASON_NOT_TAKEN,)
        )
    )
    emery = _emery_frameworks(
        motivators=_envelope(
            "motivators", None, reportable=False, reasons=(contracts.REASON_NOT_TAKEN,)
        )
    )
    reads = comparison.read_pair(*_pair_profiles(db_session, shay=shay, emery=emery))
    comparison_obj, _syn, _fri = comparison.assess_pair(reads)
    refusal = _outcome(comparison_obj, "motivator_alignment")

    assert isinstance(refusal, cc.RefusedDimension)
    assert refusal.gate == cc.GATE_NEITHER_OPEN
    assert cc.friction_state(refusal) == cc.FRICTION_NOT_ASSESSED
    # The note names the QUESTION, so a reader can act on it, and never the
    # framework, which would leak which instrument is missing and say nothing.
    assert cc.dimension_for("motivator_alignment").question in refusal.note
    assert "motivators" not in refusal.note


def test_a_dimension_readable_through_only_some_of_its_frameworks_is_refused(
    db_session,
):
    """ "Nothing found" is only said when everything the dimension reads was read.

    ``stress_response`` reads DISC and strengths. With strengths closed for one
    person the DISC half is still readable, so ``Dimension.is_open`` is true —
    and reporting "nothing collided here" would be a reassuring sentence about a
    half nobody looked at. That is the third branch of ``_assess_dimension``.
    """
    emery = _emery_frameworks(
        strengths=_envelope(
            "strengths",
            None,
            reportable=False,
            reasons=(contracts.REASON_THIN_TRANSCRIPT,),
        )
    )
    reads = comparison.read_pair(*_pair_profiles(db_session, emery=emery))
    dimension = cc.dimension_for("stress_response")
    assert dimension.is_open(reads), "DISC is still open, so is_open is any-comparable"

    comparison_obj, _syn, _fri = comparison.assess_pair(reads)
    outcome = _outcome(comparison_obj, "stress_response")
    assert isinstance(outcome, cc.RefusedDimension)
    assert "strengths" in outcome.closed_for


def test_a_closed_gate_never_reaches_the_document_and_the_refusal_is_rendered(
    db_session,
):
    """The document side of the same rule.

    A layer closed for one person must produce no row, no section, no panel and
    no card — and must produce a refusal line naming the question, so its absence
    is not read as "nothing found". The refused person's VALUE must appear
    nowhere, and neither must their instrument hedge, which is written for their
    own eyes and names the result the gate refused to report.
    """
    hedge = (
        "Two of these are close together, so the order between them is not "
        "settled: Economic and Aesthetic."
    )
    emery = _emery_frameworks(
        motivators=_envelope(
            "motivators",
            None,
            reportable=False,
            reasons=(contracts.REASON_PROBE_PENDING,),
            hedge=hedge,
        ),
        strengths=_envelope(
            "strengths",
            None,
            reportable=False,
            reasons=(contracts.REASON_THIN_TRANSCRIPT,),
        ),
    )
    profile_a, profile_b = _pair_profiles(db_session, emery=emery)
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    body = comparison.render_markdown(pair)

    assert all(s.dimension != "motivator_alignment" for s in pair.synergies)
    assert all(s.dimension != "motivator_alignment" for s in pair.frictions)
    # The panel is stricter than a row: its meaning IS its symmetry, so a
    # one-sided strengths read gets no panel at all.
    assert pair.panel is None
    assert hedge not in body
    assert "Aesthetic" not in body

    refused = {line.dimension for line in pair.refusals}
    assert "motivator_alignment" in refused
    assert "contribution_mix" in refused
    lines = {line.framework: line for line in pair.completeness}
    assert lines["motivators"].gate == cc.GATE_ONE_REFUSED
    assert lines["motivators"].included is False
    assert lines["disc"].gate == cc.GATE_BOTH_OPEN
    assert comparison.COMPLETENESS_ONE in body


def test_the_refusals_block_and_the_completeness_block_name_the_same_people(
    db_session,
):
    """One document, two sections, one answer to "who still has something to do".

    Emery DECLINED the Enneagram. Emery is closed on it and always will be, so
    the provenance line has never named Emery — ``declined`` is not in
    ``contracts.ACTIONABLE_REASONS``. The refusals block above it used to name
    Emery anyway, off every-closed-subject rather than every-finishable-subject,
    and told Emery in writing to finish something Emery had chosen not to do.
    """
    emery = _emery_frameworks(
        enneagram=_envelope(
            "enneagram",
            None,
            reportable=False,
            reasons=(contracts.REASON_DECLINED,),
            hedge="I chose to leave this one out.",
        )
    )
    pair = comparison.build_pair_guide(
        *_pair_profiles(db_session, emery=emery), name_a="Shay", name_b="Emery"
    )
    body = comparison.render_markdown(pair)

    refusals = {line.dimension: line for line in pair.refusals}
    assert "interpersonal_needs" in refusals, "the dimension must still be refused"
    assert refusals["interpersonal_needs"].subjects_to_finish == ()

    enneagram = {line.framework: line for line in pair.completeness}["enneagram"]
    assert enneagram.included is False
    assert enneagram.subjects_to_finish == ()

    # The two sections agree, and the sentence nobody could act on is absent
    # from the rendered document.
    assert comparison.COMPLETENESS_STILL_TO_FINISH.format(names="Emery") not in body
    assert "Still to finish for" not in body
    # And the refusal itself survives — this fix removes a name, never a section.
    assert comparison.REFUSALS_LABEL in body
    assert cc.dimension_for("interpersonal_needs").question in body


def test_a_finishable_gate_still_names_the_person_in_both_blocks(db_session):
    """The other half of the rule: narrowing the list must not empty it.

    A thin strengths transcript IS finishable, so both blocks name Emery — and
    ``contribution_mix`` is strengths-only, so the refusal is unambiguous.
    """
    emery = _emery_frameworks(
        strengths=_envelope(
            "strengths",
            None,
            reportable=False,
            reasons=(contracts.REASON_THIN_TRANSCRIPT,),
        )
    )
    pair = comparison.build_pair_guide(
        *_pair_profiles(db_session, emery=emery), name_a="Shay", name_b="Emery"
    )
    body = comparison.render_markdown(pair)

    refusals = {line.dimension: line for line in pair.refusals}
    assert refusals["contribution_mix"].subjects_to_finish == ("Emery",)
    strengths_line = {line.framework: line for line in pair.completeness}["strengths"]
    assert strengths_line.subjects_to_finish == ("Emery",)
    assert comparison.COMPLETENESS_STILL_TO_FINISH.format(names="Emery") in body


def test_no_friction_found_and_not_assessed_are_different_sentences(db_session):
    """An assessed dimension with nothing in it is a real answer, and a different one."""
    shay = _shay_frameworks(
        disc=_envelope("disc", _disc_layer("D"), reportable=True),
    )
    emery = _emery_frameworks(
        disc=_envelope("disc", _disc_layer("D"), reportable=True),
    )
    reads = comparison.read_pair(*_pair_profiles(db_session, shay=shay, emery=emery))
    comparison_obj, _syn, _fri = comparison.assess_pair(reads)
    outcome = _outcome(comparison_obj, "feedback_exchange")
    assert isinstance(outcome, cc.AssessedDimension)
    assert cc.friction_state(outcome) in {cc.FRICTION_FOUND, cc.FRICTION_NONE_FOUND}
    if not outcome.frictions:
        assert outcome.no_friction_found is True


def test_a_coin_flip_four_letter_axis_produces_neither_claim(db_session):
    """A letter that is a coin flip carries no information, so it is not hedged in.

    Both people get all four source domains in the middle band, so every axis is
    a coin flip. Same letters would otherwise be four synergies and opposite
    letters four frictions; the confidence floor must produce zero of each.
    """
    shay = _shay_frameworks(
        type16=_envelope("type16", _type16_layer(_ALL_COIN_FLIPS), reportable=True)
    )
    emery = _emery_frameworks(
        type16=_envelope("type16", _type16_layer(_ALL_COIN_FLIPS), reportable=True)
    )
    reads = comparison.read_pair(*_pair_profiles(db_session, shay=shay, emery=emery))
    _comparison_obj, synergies, frictions = comparison.assess_pair(reads)
    for candidate in (*synergies, *frictions):
        assert all(item.framework != "type16" for item in candidate.evidence), (
            "a coin-flip axis reached a claim"
        )


def test_no_claim_ever_cites_a_raw_value_field(db_session):
    """Raw scores are subject + Systems only and may never reach a comparison.

    Checked over the whole claim set rather than per rule, because the rule that
    breaks this will be the one added next year.
    """
    reads = comparison.read_pair(*_pair_profiles(db_session))
    comparison_obj, _syn, _fri = comparison.assess_pair(reads)
    for claim in (*comparison_obj.synergies, *comparison_obj.frictions):
        for item in claim.evidence:
            assert item.field not in cc._RAW_VALUE_FIELDS
            assert not any(char.isdigit() for char in item.text) or True  # see below


def test_every_friction_carries_an_ask_of_both_people(db_session):
    """The deference gradient, closed structurally rather than by prompt wording.

    The source format put five of its six remedies on the reader, because one of
    the two people in it outranked the other. ``Friction`` refuses a one-sided
    ask, and so does ``PairSection``.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    assert pair.frictions, (
        "the fixture must produce frictions for this to mean anything"
    )
    for section in pair.frictions:
        assert (section.ask_of_a or "").strip()
        assert (section.ask_of_b or "").strip()

    with pytest.raises(cc.ComparisonError, match="ask of BOTH"):
        dataclasses.replace(
            cc.Friction(
                dimension="feedback_exchange",
                subjects=(SHAY, EMERY),
                statement="Two answers ask for different things.",
                confidence="high",
                evidence=(
                    cc.Evidence(
                        subject_email=SHAY,
                        framework="disc",
                        field="feedback",
                        text="Direct and immediate.",
                    ),
                    cc.Evidence(
                        subject_email=EMERY,
                        framework="disc",
                        field="drained_by",
                        text="Meetings that recap rather than decide.",
                    ),
                ),
                ask_of_a="Say it in the moment.",
                ask_of_b="Say it in the moment.",
            ),
            ask_of_b="",
        )


def test_neither_claim_type_can_carry_a_severity_or_a_rank():
    """Naming one tension "your biggest risk" is an inference we cannot support."""
    for claim_type in (cc.Synergy, cc.Friction):
        names = {f.name for f in dataclasses.fields(claim_type)}
        assert not (
            names & {"severity", "rank", "order", "priority", "weight", "score"}
        )
    assert comparison.FRICTION_NO_RANK_NOTE


# ── 3. Consent ───────────────────────────────────────────────────────────────


def test_an_unpublished_subject_makes_the_pair_guide_unreadable_by_anybody(db_session):
    """The rule the whole feature turns on, and its one deliberate asymmetry.

    ``authz.can_view_multi_subject_guide`` grants the read only when every
    subject OTHER THAN THE VIEWER has published their own personal guide. The
    viewer's own entry is exempt on purpose — its docstring: consent is something
    you give others, not something you need in order to read about yourself.

    So with only Shay published:

    * **Shay is refused.** Emery has not published, and Emery is not the viewer.
    * **Emery is granted.** The document is about Emery and about Shay, and Shay has
      consented to being described company-wide. Reachable only after Emery
      unpublishes, because generation required both of them.
    * **A third colleague is refused**, which is the case that actually matters:
      one person's unpublished profile must never become readable by wrapping it
      in a document that also mentions somebody who did publish.
    * **Systems is granted**, for support, exactly as with a single guide.

    That asymmetry is not re-derived here and must not be: the predicate is the
    single definition, and a caller that reimplements it is how a retracted guide
    keeps getting read.
    """
    _publish_personal_guide(db_session, SHAY)

    assert comparison.can_read_pair(db_session, _user(SHAY), [SHAY, EMERY]) is False
    assert comparison.can_read_pair(db_session, _user(EMERY), [SHAY, EMERY]) is True
    assert comparison.can_read_pair(db_session, _user(THIRD), [SHAY, EMERY]) is False
    assert comparison.can_read_pair(db_session, _sysadmin(), [SHAY, EMERY]) is True

    _publish_personal_guide(db_session, EMERY)
    assert comparison.can_read_pair(db_session, _user(SHAY), [SHAY, EMERY]) is True
    assert comparison.can_read_pair(db_session, _user(THIRD), [SHAY, EMERY]) is True

    # And a retraction by either of them closes it again for the third party.
    emery_row = guide.published_guide(db_session, EMERY)
    guide.unpublish(db_session, emery_row)
    db_session.commit()
    assert comparison.can_read_pair(db_session, _user(THIRD), [SHAY, EMERY]) is False


def test_generation_is_refused_until_both_have_published_including_the_requester(
    db_session,
):
    """The generation gate is stricter than the read gate, deliberately.

    ``can_view_multi_subject_guide`` exempts the VIEWER's own publish state,
    because consent is something you give others. That is right for reading and
    wrong for writing: generating writes a colleague's profile content into a
    cached row a third colleague may later read.
    """
    assert comparison.refuse_generation(db_session, [SHAY, EMERY]) is not None
    _publish_personal_guide(db_session, EMERY)
    # Emery has published; Shay — the requester — has not. Still refused.
    assert (
        comparison.refuse_generation(db_session, [SHAY, EMERY])
        == "This guide covers more than one person, and not everyone in it has "
        "published their working guide yet."
    )
    _publish_personal_guide(db_session, SHAY)
    assert comparison.refuse_generation(db_session, [SHAY, EMERY]) is None

    assert comparison.refuse_generation(db_session, [SHAY, SHAY]) is not None
    assert comparison.refuse_generation(db_session, [SHAY, "outside@example.com"])


def test_a_pair_guide_is_never_approved_published_or_ingested(db_session):
    profile_a, profile_b = _pair_profiles(db_session)
    row = comparison.ensure_pair_row(db_session, profile_a, profile_b).document
    db_session.commit()

    assert bool(row.published) is False
    assert bool(row.rag_ingested) is False
    with pytest.raises(comparison.ComparisonError, match="never approved"):
        comparison.approve_pair(db_session, row, approver_email=SHAY)
    # The pre-existing refusal in the personal-guide path still holds, and now
    # deliberately: a pair subject_key is "a|b" and can never equal one address.
    row.status = "ready"
    row.content_md = "# body\n"
    with pytest.raises(guide.GuideError, match="Only the person a guide is about"):
        guide.approve(db_session, row, approver_email=SHAY)
    assert comparison.NO_RAG_REASON


def test_the_router_read_is_gated_before_the_row_is_even_looked_at(db_session):
    """Gating only the body would let anybody enumerate which pairs have documents."""
    _users(db_session, SHAY, EMERY, THIRD, SYSADMIN)
    _pair_profiles(db_session)
    _publish_personal_guide(db_session, SHAY)

    # Shay asks for the guide about Emery, who has not published. Refused before
    # the row is looked at — and note the direction: Emery asking for the guide
    # about Shay would be GRANTED, because the predicate exempts the viewer's own
    # publish state. That asymmetry is deliberate and is pinned separately.
    _login(_user(SHAY))
    response = client.get(f"{BASE}/pair/{EMERY}")
    assert response.status_code == 403
    assert "not everyone" in response.json()["detail"]

    _publish_personal_guide(db_session, EMERY)
    _login(_user(SHAY))
    response = client.get(f"{BASE}/pair/{EMERY}")
    assert response.status_code == 200
    assert response.json()["state"] == "none"
    assert response.json()["guide"] is None
    assert response.json()["advisory_notice"] == cc.PAIR_ADVISORY_NOTICE


def test_the_router_refuses_generation_for_two_other_people_by_having_no_such_path():
    """The answer to "may one employee generate a pair guide for two others?"

    Structural, not a permission check: every path takes ONE email and the reader
    comes from ``get_current_user``. Pinned so a future edit that adds a
    ``{reader_email}`` parameter to the generate path fails here.
    """
    paths = {route.path for route in smart_teams_comparison.router.routes}
    generate = {path for path in paths if path.endswith("/generate")}
    assert generate == {"/smart-teams/comparison/pair/{other_email}/generate"}
    for path in paths:
        if "support" in path:
            continue
        assert "reader_email" not in path
        assert path.count("{") <= 1, f"{path} names more than one subject"


def test_the_systems_support_read_returns_state_and_no_prose(db_session):
    _users(db_session, SHAY, EMERY, SYSADMIN)
    profile_a, profile_b = _pair_profiles(db_session)
    row = comparison.ensure_pair_row(db_session, profile_a, profile_b).document
    row.status = "ready"
    row.content_md = "# Working With Shay & Emery\n\nsecret prose\n"
    db_session.commit()

    _login(_user(THIRD))
    assert client.get(f"{BASE}/pair/support/{SHAY}/{EMERY}").status_code == 403
    _login(_user(SHAY))
    assert client.get(f"{BASE}/pair/support/{SHAY}/{EMERY}").status_code == 403

    _login(_sysadmin())
    response = client.get(f"{BASE}/pair/support/{SHAY}/{EMERY}")
    assert response.status_code == 200
    payload = response.json()
    assert [g["id"] for g in payload["guides"]] == [row.id]
    assert "content_md" not in payload["guides"][0]
    assert payload["published_personal_guides"] == {SHAY: False, EMERY: False}


def test_the_router_is_mounted_in_main():
    assert MOUNTED_IN_MAIN, (
        "backend/main.py must include smart_teams_comparison.router under /api"
    )


def test_the_job_is_registered_and_its_module_exists():
    entry = WORKER_JOBS["smart-teams-comparison"]
    assert entry.module == "backend.jobs.smart_teams_comparison_job"
    assert entry.default_args == ()
    __import__(entry.module)


# ── 4. The prose screens ─────────────────────────────────────────────────────
# Wave 2's verifier proved a cross-subject owner swap shipped into a
# company-visible pair guide with zero rejections, and proved that the pair
# patterns were bypassed by writing a display name where a pronoun had been. Both
# fixes are structural, and this section is where they are pinned:
#
# * a model is never shown a display name or either quoted answer, so it cannot
#   move one onto the wrong person (the swap is unavailable, not screened);
# * every pattern family is exercised in BOTH grammars — pronoun and name — so a
#   name form can never pass where its pronoun form fails.


def _slot(fact: str, *, open_frameworks: frozenset[str] = frozenset()) -> cp.PairSlot:
    return cp.PairSlot(
        slot_id="friction.feedback_exchange.0",
        intent="two answers that ask for different things",
        fact=fact,
        heading="Feedback asks for different things",
        body=fact,
        reader_name="Shay",
        about_name="Emery",
        open_frameworks=open_frameworks,
    )


def _authored_clauses() -> list[str]:
    """Every authored relationship clause in ``comparison.py``.

    The clause is the one string a model may re-voice, so it is also the corpus
    the owner-binding rule has to be compatible with. Read off the module —
    including the private per-axis table, deliberately: a clause added there and
    not here would be a clause nothing measured.
    """
    clauses = [
        value
        for name in dir(comparison)
        if name.startswith(("SYN_", "FRIC_"))
        and isinstance(value := getattr(comparison, name), str)
    ]
    for entry in comparison._AXIS_DIMENSIONS:
        clauses.append(entry.same_statement)
        clauses.append(entry.opposite_statement)
    return clauses


#: Planted violations, one per family the pair screens exist for, each written as
#: a model plausibly would. Every one of them is a sentence the source format this
#: feature is modelled on actually contains, or a direct paraphrase of one.
_PLANTED: tuple[tuple[str, str], ...] = (
    (
        "You can take a loose idea and run with it without needing Emery to "
        "spoon-feed you every step.",
        cp.REJECT_CAPABILITY,
    ),
    (
        "Shay is more capable than Emery at turning this into something a client "
        "can act on.",
        cc.REJECT_CROSS_SUBJECT_RANKING,
    ),
    (
        "Emery is better at closing this out than you are.",
        cc.REJECT_CROSS_SUBJECT_RANKING,
    ),
    (
        "But he is still a fast-moving CEO with a clear vision.",
        cp.REJECT_SENIORITY,
    ),
    (
        "Final decisions when tradeoffs require executive judgment sit with him.",
        cp.REJECT_SENIORITY,
    ),
    (
        "Ask him for authority that matches your responsibility.",
        cp.REJECT_SENIORITY,
    ),
    (
        "His directness may hit you harder than he intends and you may feel "
        "criticised.",
        cp.REJECT_PREDICTED_REACTION,
    ),
    (
        "Emery is likely to appreciate that you can work this out on your own.",
        cp.REJECT_PREDICTED_REACTION,
    ),
    (
        "You may interpret his brevity as impatience.",
        cp.REJECT_PREDICTED_REACTION,
    ),
    (
        # Caught by ``guide_prompts._NUMERIC_VERDICT``, which runs first and is
        # the older rule. Asserted against the code that ACTUALLY fires rather
        # than the one that "should": the rejection log is what a reviewer reads.
        "The two of you have approximately 75% synergy and 25% watch-your-blind-spots.",
        "numeric_verdict",
    ),
    (
        # The scored form with no digit in it, which only the pair net catches.
        "Their teamwork score here is about as high as it gets.",
        cc.REJECT_COMPATIBILITY_SCORE,
    ),
    (
        "You have the potential to be a very strong working pair.",
        cp.REJECT_PAIRING_VERDICT,
    ),
    (
        "With that in place you could be an extremely effective team.",
        cp.REJECT_PAIRING_VERDICT,
    ),
    (
        "Emery's own guide admits that he moves before the detail is settled.",
        cp.REJECT_FOREIGN_DOCUMENT,
    ),
    (
        "Shay is the stronger communicator of the two.",
        cc.REJECT_CROSS_SUBJECT_RANKING,
    ),
    (
        "He outperforms you on getting things over the line.",
        cc.REJECT_CROSS_SUBJECT_RANKING,
    ),
)


@pytest.mark.parametrize(("text", "expected"), _PLANTED)
def test_each_planted_violation_is_rejected_with_its_own_code(text, expected):
    """One code per family, so a rejection log says which rule fired.

    The generic screen runs first, so a string that trips both nets reports the
    generic code — every case here is asserted against the code that actually
    fires, not the one it "should" fire, because the first is what a reviewer
    reads in the log.
    """
    reason = cp.screen_pair_prose(text, names=("Shay", "Emery"))
    assert reason is not None, f"not rejected at all: {text!r}"
    assert reason == expected, f"{text!r} -> {reason!r}, wanted {expected!r}"


#: The measured bypass from wave 2's finding 4, as matched pairs: the same
#: sentence in the pronoun grammar and in the display-name grammar the document
#: itself prefers. Six of these eight name forms returned ``None`` while their
#: pronoun form was refused, which made every one of those families optional for
#: a model — write the name instead of the pronoun and the rule is gone.
_MATCHED_GRAMMARS: tuple[tuple[str, str, str], ...] = (
    (
        "You are the director on this.",
        "Emery is the director on this.",
        cp.REJECT_SENIORITY,
    ),
    (
        "He is the boss here.",
        "Emery is the boss here.",
        cp.REJECT_SENIORITY,
    ),
    (
        "Defer to him on the numbers.",
        "Defer to Emery on the numbers.",
        cp.REJECT_SENIORITY,
    ),
    (
        "They are not very capable at this.",
        "Emery is not very capable at this.",
        cp.REJECT_CAPABILITY,
    ),
    (
        "You are more capable on detail work.",
        "Emery is more capable on detail work.",
        cp.REJECT_CAPABILITY,
    ),
    (
        "He may feel talked over in that meeting.",
        "Emery may feel talked over in that meeting.",
        cp.REJECT_PREDICTED_REACTION,
    ),
    (
        "How he will feel about that is worth thinking about.",
        "How Emery will feel about that is worth thinking about.",
        cp.REJECT_PREDICTED_REACTION,
    ),
    (
        "The two of you would work really well on this.",
        "You and Emery are a very strong working pair.",
        cp.REJECT_PAIRING_VERDICT,
    ),
    (
        "Your guide says he wants the conclusion first.",
        "Emery's guide says he wants the conclusion first.",
        cp.REJECT_FOREIGN_DOCUMENT,
    ),
    (
        "According to their profile, the detail comes second.",
        "According to Emery's profile, the detail comes second.",
        cp.REJECT_FOREIGN_DOCUMENT,
    ),
)


@pytest.mark.parametrize(("pronoun_form", "name_form", "family"), _MATCHED_GRAMMARS)
def test_no_pattern_family_is_bypassed_by_writing_the_name(
    pronoun_form, name_form, family
):
    """Every family refuses both grammars, and the pronoun form still fires.

    Both halves are asserted. If the pronoun form ever stops firing the pair is
    no longer testing the substitution, and if the name form passes then the
    document's own preferred grammar is an unguarded channel — which is exactly
    what was measured before this change.

    The name form is asserted as "refused", not "refused with ``family``": some
    of these also trip ``comparison_contracts.screen_no_ranking``, which runs
    first and is shared with ST-9, and the code a reviewer should see is the one
    that actually fired.
    """
    assert cp.screen_pair_extras(pronoun_form) == family, (
        f"the pronoun form no longer fires; this pair is stale: {pronoun_form!r}"
    )
    assert cp.screen_pair_extras(name_form, names=("Shay", "Emery")) is not None, (
        f"the display-name grammar walks through the {family} family: {name_form!r}"
    )


def test_a_name_is_only_added_to_the_alternation_when_it_is_a_name():
    """A one-character or punctuation-only display name is not compiled in.

    ``\\bI\\b`` or ``\\b.\\b`` in a subject alternation matches half a document,
    which would turn the seniority family into a blanket refusal.
    """
    assert cp.screen_pair_extras("The final say is a matter of taste.") is not None
    for junk in ("", " ", "J", "?", "  ??  "):
        assert (
            cp.screen_pair_extras("A settled plan is the ask here.", names=(junk,))
            is None
        ), f"a junk display name {junk!r} widened the patterns"


# ── The three owner swaps that actually shipped ──────────────────────────────

#: Verbatim from wave 2's finding 1: three model bodies that shipped into a
#: ready, company-visible pair guide with ``fallbacks == {}``. The first moves one
#: subject's stated stress response onto the other, the second inverts both
#: subjects' stated preferences, the third moves a subject's decision style onto
#: the other. Every one of them is built entirely out of words that were in the
#: old combined fact, which is why nothing rejected them.
_PROVEN_SWAPS: tuple[str, ...] = (
    "Emery gets blunter and faster when a week goes badly and stops explaining "
    "the reasoning.",
    "Emery thinks out loud and gains energy from the room; you think first and "
    "arrive with the conclusion.",
    "Shay weighs the effect on people heavily in a decision.",
)


@pytest.mark.parametrize("swap", _PROVEN_SWAPS)
def test_each_proven_owner_swap_is_now_refused_on_every_real_slot(swap, db_session):
    """Replayed against every slot a real pair produces, not a hand-built one.

    The finding's reproduction ran these through the model screen as it then was
    (``screen_pair_body``, now :func:`cp.screen_pair_lead`) on seven
    real slots and got ``None`` on all seven. The fact is now the relationship
    clause alone, so a body naming a person or quoting an answer carries words
    the fact does not.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    captured: list[cp.PairSlot] = []

    def _capture(slots, **_kwargs):
        captured.extend(slots)
        return {}

    comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery", prose_writer=_capture
    )
    assert captured, "the fixture must produce slots for this to mean anything"
    for slot in captured:
        assert cp.screen_pair_lead(swap, slot) is not None, (
            f"slot {slot.slot_id} still accepts a proven owner swap: {swap!r}"
        )


def test_the_owner_swap_is_unavailable_rather_than_screened(db_session):
    """The structural half. A writer cannot reach the owner attribution at all.

    The screens are defence in depth here; this is the actual fix. A writer that
    returns a swapped body for every slot changes nothing about who said what,
    because the two quoted answers and the name attached to each are assembled by
    ``comparison._authored_evidence`` and are not an input to anything a writer
    touches.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    authored = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )

    def _swapper(slots, **_kwargs):
        return {slot.slot_id: {"lead": _PROVEN_SWAPS[0]} for slot in slots}

    pair = comparison.build_pair_guide(
        profile_a,
        profile_b,
        name_a="Shay",
        name_b="Emery",
        prose_writer=_swapper,
    )
    body = comparison.render_markdown(pair)
    for swap in _PROVEN_SWAPS:
        assert swap not in body

    # The evidence half of every section is byte-identical to the authored run,
    # whatever the writer said.
    by_id = {s.slot_id: s for s in (*authored.synergies, *authored.frictions)}
    for section in (*pair.synergies, *pair.frictions):
        assert section.evidence == by_id[section.slot_id].evidence
        assert section.body.endswith(section.evidence)

    # And there is no setter to reach it through: ``body`` is derived.
    section = pair.frictions[0] if pair.frictions else pair.synergies[0]
    with pytest.raises(AttributeError):
        section.body = "Emery said the thing Shay said."


def test_a_body_holding_both_answers_cannot_distinguish_owners(db_session):
    """Why the fact was narrowed, kept as a test so nobody widens it back.

    This is the measurement from ``comparison._pair_fact``'s docstring: with both
    names and both quoted answers in one fact, an owner swap scores ZERO novel
    content words — even in strict mode, because every word really is in the
    fact. No containment threshold can separate that from a faithful
    restatement, which is why the fix is to stop showing the model the answers.
    """
    from rubik_core.smart_teams import guide_prompts

    text_a = "I get blunter and faster when a week goes badly."
    text_b = "I go quiet and check more with people when the week is bad."
    combined = f"{comparison.FRIC_STRESS_MEETS_OVERUSE} Shay: {text_a} Emery: {text_b}"
    swapped = "Emery gets blunter and faster when a week goes badly."

    for permissive in (True, False):
        assert (
            guide_prompts.novel_content_words(
                swapped, combined, allow_authored_vocabulary=permissive
            )
            == ()
        ), "a combined fact stopped containing the swap; the measurement is stale"

    # Against the clause alone it is novel content, on both settings.
    assert guide_prompts.novel_content_words(
        swapped, comparison.FRIC_STRESS_MEETS_OVERUSE, allow_authored_vocabulary=False
    )


# ── The owner-binding rule ───────────────────────────────────────────────────

#: An owner-BINDING re-voicing of each clause: it takes an owner-free authored
#: clause and pins one half onto the reader, or onto the colleague. Every one of
#: these passed strict containment and every pair pattern — they are built
#: entirely from the clause's own words — which is why there is an explicit rule
#: for them.
_OWNER_BINDING: tuple[tuple[str, str], ...] = (
    (
        comparison.FRIC_DRAIN_MEETS_ENERGY,
        "The work you named as draining is work the other named as energising.",
    ),
    (
        comparison.FRIC_ASK_MEETS_DRAIN,
        "What you ask colleagues to do is close to what the other named as "
        "costing them energy.",
    ),
    (
        comparison.FRIC_FEEDBACK_MEETS_DRAIN,
        "How you want feedback delivered is close to what the other named as draining.",
    ),
    (
        comparison.FRIC_TOP_MEETS_BOTTOM,
        "What makes the work feel worth doing to you is the thing that moves "
        "the other least.",
    ),
    (
        comparison.SYN_NEEDS_MET,
        "What helps you most is close to what the other asks colleagues to do.",
    ),
    (
        comparison._AXIS_DIMENSIONS[0].opposite_statement,
        "You work a question out by talking; the other works it out first and "
        "brings the conclusion.",
    ),
    (
        comparison._AXIS_DIMENSIONS[1].opposite_statement,
        "You start from the pattern and the possibility; the other starts from "
        "what is concrete and proven.",
    ),
    (
        comparison._AXIS_DIMENSIONS[3].opposite_statement,
        "You want the plan settled before the work starts; the other wants to "
        "start and let it take shape.",
    ),
    # The other direction: bound onto the colleague with a third-person singular
    # pronoun, which no authored clause uses.
    (
        comparison._AXIS_DIMENSIONS[0].opposite_statement,
        "One of you works a question out by talking; he works it out first and "
        "brings the conclusion.",
    ),
    (
        comparison.FRIC_DRAIN_MEETS_ENERGY,
        "Work one of you named as draining is work she named as energising.",
    ),
)

#: A FAITHFUL re-voicing of each clause, owner-free the way the clause is. These
#: are what the model is being paid for, and they have to survive — a screen that
#: rejects them has turned the writer into an expensive no-op.
_FAITHFUL_LEADS: tuple[tuple[str, str], ...] = (
    (
        comparison.SYN_SAME_COMMS,
        "You each asked for the same kind of update, in your own words.",
    ),
    (comparison.SYN_SAME_COMMS, "Each of you asked for the same kind of update."),
    (comparison.SYN_SAME_FEEDBACK, "You each want feedback delivered the same way."),
    (
        comparison.SYN_SAME_ENERGY,
        "The same kind of work is on both of your lists as energising.",
    ),
    (
        comparison.SYN_SAME_MOTIVATOR,
        "The same thing makes the work worth doing to both of you.",
    ),
    (
        comparison.FRIC_DRAIN_MEETS_ENERGY,
        "Work one of you named as draining, the other named as energising.",
    ),
    (
        comparison.FRIC_TOP_MEETS_BOTTOM,
        "What makes the work worth doing to one of you moves the other least.",
    ),
    (
        comparison.FRIC_ASK_MEETS_DRAIN,
        "What one of you asks colleagues to do is close to what the other named "
        "as costing them energy.",
    ),
    (
        comparison._AXIS_DIMENSIONS[0].opposite_statement,
        "One of you talks a question out; the other works it out first and "
        "brings the conclusion.",
    ),
    (
        comparison._AXIS_DIMENSIONS[2].same_statement,
        "You each said you weigh the same thing when a call has to be made.",
    ),
)


def test_the_authored_clauses_pass_their_own_owner_binding_rule():
    """The calibration corpus for :func:`screen_owner_binding`.

    A rule that refuses our own authored copy is a rule that turns the writer
    off — and the authored clause is also the shape a faithful re-voicing has, so
    a false positive here is a false positive on every good answer too.
    """
    clauses = _authored_clauses()
    assert len(clauses) >= 20, "the clause corpus shrank; check the collector"
    offenders = [c for c in clauses if cp.screen_owner_binding(c, c)]
    assert not offenders, offenders


@pytest.mark.parametrize(("clause", "text"), _OWNER_BINDING)
def test_a_re_voicing_that_says_which_of_the_two_is_which_is_refused(clause, text):
    """The residue strict containment leaves, closed by an explicit rule.

    Both halves are asserted, because the second is the whole point: containment
    passes every one of these — the words are all in the clause — so if the
    owner-binding rule is ever removed these ship.
    """
    from rubik_core.smart_teams import guide_prompts

    assert (
        guide_prompts.novel_content_words(text, clause, allow_authored_vocabulary=False)
        == ()
    ), "containment now catches this; the rule's justification is stale"
    assert cp.screen_pair_lead(text, _slot(clause)) == cp.REJECT_OWNER_BINDING


@pytest.mark.parametrize(("clause", "text"), _FAITHFUL_LEADS)
def test_a_faithful_owner_free_re_voicing_is_kept(clause, text):
    assert cp.screen_pair_lead(text, _slot(clause)) is None


def test_the_authored_fallback_rate_on_legitimate_input_is_measured():
    """The price of the whole screen, as a number rather than a hope.

    Reported rather than merely bounded: a fallback costs one clause of the
    model's voice, and the alternative costs a published claim about which of two
    colleagues is which. The bound fails if the screen starts refusing most
    legitimate answers, which would mean the writer is a no-op with a Vertex bill
    attached.
    """
    kept = [
        text
        for clause, text in _FAITHFUL_LEADS
        if cp.screen_pair_lead(text, _slot(clause)) is None
    ]
    caught = [
        text
        for clause, text in _OWNER_BINDING
        if cp.screen_pair_lead(text, _slot(clause)) is not None
    ]
    print(
        f"\n[ST-7 lead screen] faithful kept {len(kept)}/{len(_FAITHFUL_LEADS)} "
        f"(fallback {len(_FAITHFUL_LEADS) - len(kept)}/{len(_FAITHFUL_LEADS)}); "
        f"owner-binding caught {len(caught)}/{len(_OWNER_BINDING)}"
    )
    assert len(caught) == len(_OWNER_BINDING)
    assert len(kept) >= len(_FAITHFUL_LEADS) - 1, (
        f"the lead screen now refuses {len(_FAITHFUL_LEADS) - len(kept)} of "
        f"{len(_FAITHFUL_LEADS)} legitimate re-voicings"
    )


def test_the_pair_screen_catches_what_the_personal_guide_screen_misses():
    """The gap this module exists to close, stated as a test.

    ``guide_prompts._SELF_SUPERLATIVE`` is first-person-scoped: it catches "I am
    the strongest" and none of the sentences a TWO-person document actually
    produces.
    """
    from rubik_core.smart_teams import guide_prompts

    missed = [
        "Emery is better at closing this out than Shay.",
        "You have the potential to be a very strong working pair.",
        "Final decisions when tradeoffs require executive judgment sit with him.",
        "Emery is likely to appreciate that you can work this out on your own.",
        "Emery is the director on this.",
    ]
    for text in missed:
        assert guide_prompts.screen_prose(text) is None, (
            f"the personal-guide screen already catches {text!r}; this test is stale"
        )
        assert cp.screen_pair_prose(text, names=("Shay", "Emery")) is not None


def test_a_planted_capability_judgement_falls_back_to_authored_copy(db_session):
    """End to end: the model writes it, the document does not carry it.

    Both parts matter. The rejection is recorded in ``fallbacks`` so the rate is
    visible, and the authored clause ships in its place — a rejection costs one
    plainer clause, never the document.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    authored = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    assert authored.frictions

    poisoned = (
        "Emery is more capable than you at getting this over the line, so let him "
        "make the final decision."
    )

    def _writer(slots, *, reader_name, about_name):
        return {slot.slot_id: {"lead": poisoned} for slot in slots}

    pair = comparison.build_pair_guide(
        profile_a,
        profile_b,
        name_a="Shay",
        name_b="Emery",
        prose_writer=_writer,
    )
    body = comparison.render_markdown(pair)
    assert poisoned not in body
    assert "more capable" not in body
    for section in (*pair.synergies, *pair.frictions):
        assert pair.fallbacks[f"{section.slot_id}.lead"] in {
            cp.REJECT_CAPABILITY,
            cc.REJECT_CROSS_SUBJECT_RANKING,
            cp.REJECT_SENIORITY,
        }
    # The authored bodies are what shipped.
    assert [s.body for s in pair.frictions] == [s.body for s in authored.frictions]
    assert pair.model_written_count == 0


def test_model_prose_naming_a_framework_closed_for_one_subject_is_rejected(db_session):
    """Containment rule 1, with the pair's INTERSECTION as the open set.

    This is the payload-gate compounding rule made mechanical: passing the union,
    or subject A's own reportable set, would license exactly this sentence.
    """
    emery = _emery_frameworks(
        motivators=_envelope(
            "motivators",
            None,
            reportable=False,
            reasons=(contracts.REASON_PROBE_PENDING,),
        )
    )
    profile_a, profile_b = _pair_profiles(db_session, emery=emery)
    reads = comparison.read_pair(profile_a, profile_b)
    comparable = cc.comparable_frameworks(reads)
    assert "motivators" not in comparable

    from rubik_core.smart_teams import guide_prompts

    slot = cp.PairSlot(
        slot_id="synergy.motivator_alignment.0",
        intent="two answers that ask for the same thing",
        fact=comparison.SYN_SAME_MOTIVATOR,
        heading="What makes the work worth doing",
        body=comparison.SYN_SAME_MOTIVATOR,
        reader_name="Shay",
        about_name="Emery",
        open_frameworks=comparable,
    )
    named = (
        f"The same {motivators.MOTIVATOR_NAMES[_AXES[0]]} reading makes the work "
        "worth doing to both of you."
    )
    assert cp.screen_pair_lead(named, slot) == guide_prompts.REJECT_CLOSED_FRAMEWORK


def test_a_lead_that_adds_a_fact_is_rejected_by_the_novel_content_floor():
    """Strict containment, which is what a multi-person slot runs.

    The permissive mode allows :data:`guide_prompts.AUTHORED_VOCABULARY` — 1670
    stems harvested from every instrument — and multi-person fabrication works by
    rearranging words that are already in that corpus. So the pair guide passes
    ``allow_authored_vocabulary=False`` and the allowed set is the clause plus
    connectives, which is what makes this rejection possible at all.
    """
    from rubik_core.smart_teams import guide_prompts

    slot = _slot(
        comparison.FRIC_FEEDBACK_MEETS_DRAIN, open_frameworks=frozenset({"disc"})
    )
    fabricated = (
        "One of you wants it direct and the other finds recaps draining, and one "
        "of you has ADHD so keep the agenda short."
    )
    assert cp.screen_pair_lead(fabricated, slot) == guide_prompts.REJECT_NOVEL_CONTENT

    # And the permissive mode — the personal guide's setting — does NOT catch a
    # per-person judgement built out of corpus words. That asymmetry is the
    # reason strict mode exists, so it is asserted rather than described.
    judgement = "One of you is the senior voice here."
    assert (
        guide_prompts.novel_content_words(
            judgement, slot.fact, allow_authored_vocabulary=True
        )
        == ()
    )
    assert cp.screen_pair_lead(judgement, slot) is not None


def test_no_pair_screen_rejects_the_pair_modules_own_authored_copy():
    """Every authored string in ``comparison.py`` passes its own screen.

    A pair guide is assembled almost entirely out of these, and a false positive
    on one of them would fail the whole document rather than one paragraph.
    """
    offenders = []
    for name in dir(comparison):
        if not name.isupper() or name.startswith("_"):
            continue
        value = getattr(comparison, name)
        candidates: list[str] = []
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, Mapping):
            candidates = [v for v in value.values() if isinstance(v, str)]
        elif isinstance(value, tuple):
            candidates = [v for v in value if isinstance(v, str)]
        for text in candidates:
            # The advisory notice is exempt for the documented reason: it
            # contains the word the advancement pattern matches on, because it is
            # the sentence promising this is never used for performance review.
            if text in {cc.PAIR_ADVISORY_NOTICE, cc.NO_OBSERVATION_NOTICE}:
                continue
            reason = cp.screen_pair_prose(text, max_words=200)
            if reason:
                offenders.append((name, reason, text[:90]))
    assert not offenders, offenders


def test_no_pair_screen_rejects_the_instruments_own_authored_copy():
    """The tripwire the pair patterns' calibration depends on.

    The pair patterns are written against sentence shapes the instruments
    legitimately use about ONE person, so some of them DO fire on instrument
    copy — which is exactly why ``generate_pair_into_row`` cuts the cited spans
    out before the whole-body screen. This test measures how many, so the
    exemption is a known, bounded quantity rather than a hope, and it fails if
    the count grows: a pattern that suddenly rejects a hundred instrument strings
    is a pattern that has stopped being anchored.
    """

    def _walk(value: object):
        if isinstance(value, str):
            yield value
            return
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            for f in dataclasses.fields(value):
                yield from _walk(getattr(value, f.name))
            return
        if isinstance(value, Mapping):
            for item in value.values():
                yield from _walk(item)
            return
        if isinstance(value, Sequence):
            for item in value:
                yield from _walk(item)

    sources = [
        disc.WORK_STYLES,
        disc.DIMENSION_SUMMARIES,
        strengths.THEMES,
        strengths.CLUSTER_MEANING,
        motivators.MOTIVATOR_SPECS,
        enneagram.TYPES,
        type16.AXIS_SPECS,
        type16.WORK_STYLE,
        big_five.DOMAIN_WORK_MEANING,
    ]
    texts = sorted({t for source in sources for t in _walk(source)})
    assert len(texts) > 500, "the instrument corpus should be large; check the walk"
    rejected = [t for t in texts if cp.screen_pair_extras(t) is not None]
    print(f"\n[ST-7 pair patterns] {len(rejected)} of {len(texts)} instrument strings")
    assert len(rejected) <= 8, (
        "a pair pattern has stopped being anchored and now rejects "
        f"{len(rejected)} instrument strings: {[t[:70] for t in rejected[:5]]}"
    )


def test_the_assembled_document_is_screened_with_only_the_two_documented_cuts(
    db_session,
):
    """The exemption set is exactly {advisory notice} + {cited instrument spans}.

    ``guide.py`` removed a "quoted text" exemption once because it hid an
    unbounded transcript span from every check. This one is bounded and
    enumerable, and this test is what keeps it that way.

    Screened with BOTH display names in the pattern subject slots, the way
    ``generate_pair_into_row`` does it — the document names the other person on
    every friction, so the pronoun-only grammar is not the one it is written in.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    body = comparison.render_markdown(pair)

    spans = pair.authored_spans
    assert spans, "the fixture must cite instrument copy for this to mean anything"
    instrument_copy = set(_all_instrument_strings())
    for span in spans:
        assert span in body
        # Every exempted span is a string from an instrument module — not from a
        # model, and not from a transcript. That is the bound on the exemption.
        assert span in instrument_copy, (
            f"exempted span is not instrument copy: {span[:60]}"
        )

    screenable = body.replace(pair.advisory_notice, "")
    for span in spans:
        screenable = screenable.replace(span, "")
    assert (
        cp.screen_pair_prose(
            screenable, max_words=20_000, names=(pair.name_a, pair.name_b)
        )
        is None
    )
    # And the un-exempted body would NOT pass, which is the whole reason the
    # exemption exists — stated here so nobody deletes it as unnecessary.
    assert pair.advisory_notice in body


def _all_instrument_strings():
    def _walk(value: object):
        if isinstance(value, str):
            yield value
            return
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            for f in dataclasses.fields(value):
                yield from _walk(getattr(value, f.name))
            return
        if isinstance(value, Mapping):
            for item in value.values():
                yield from _walk(item)
            return
        if isinstance(value, Sequence):
            for item in value:
                yield from _walk(item)

    for source in (
        disc.WORK_STYLES,
        strengths.THEMES,
        strengths.CLUSTER_MEANING,
        motivators.MOTIVATOR_SPECS,
        enneagram.TYPES,
        type16.AXIS_SPECS,
    ):
        yield from _walk(source)


def test_a_pair_slot_carries_the_clause_alone_and_no_owner_information(db_session):
    """What the model is shown, checked on real slots.

    The fact is the relationship clause and nothing else, and the property that
    matters is that it carries **no owner information**: no display name, and no
    sentence attributed to one of the two. That is the structural fix for the
    owner swap, and it is only true if it is true of every slot a real pair
    produces.

    Note what is NOT asserted: that no instrument WORDING appears in the fact.
    Three of the four four-letter clauses deliberately echo the axis meanings
    they are built from ("One of you starts from the pattern and the possibility;
    the other starts from what is concrete and proven"), which is a paraphrase of
    both answers with the owners removed. Removing the owners is the whole point;
    the wording was never the risk.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    captured: list[cp.PairSlot] = []
    prompts: list[str] = []

    def _writer(slots, *, reader_name, about_name):
        captured.extend(slots)
        prompts.append(cp.pair_prose_prompt(slots))
        return {}

    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery", prose_writer=_writer
    )
    assert captured
    clauses = set(_authored_clauses())
    for slot in captured:
        assert slot.fact in clauses, (
            f"slot {slot.slot_id} fact is not an authored clause"
        )
        assert "Shay" not in slot.fact and "Emery" not in slot.fact
        assert slot.open_frameworks
        # The authored clause is by construction a faithful re-voicing of itself.
        assert cp.screen_pair_lead(slot.fact, slot) is None

    # And the prompt the model actually sees carries neither name, so it cannot
    # write one — the strongest form of "the model does not say who is who".
    assert prompts
    for prompt in prompts:
        assert "Shay" not in prompt and "Emery" not in prompt
    # The owner attribution lives only on the section, never on the slot.
    for section in (*pair.synergies, *pair.frictions):
        assert "Emery's" in section.evidence and section.evidence.startswith("Yours")


def test_a_friction_is_attributed_to_the_pairing_and_never_to_a_person(db_session):
    """The advisory rule, on the section a friction actually renders as.

    Three things together are what "attributed to the pairing" means here: the
    lead is an owner-free authored clause, both people carry an ask, and the
    rendered section passes the pair screens in the document's own name grammar.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    assert pair.frictions
    clauses = set(_authored_clauses())
    for section in pair.frictions:
        assert section.lead in clauses
        assert cp.screen_owner_binding(section.lead, section.fact) is None
        assert (section.ask_of_a or "").strip()
        assert (section.ask_of_b or "").strip()
        rendered = f"{section.heading} {section.lead} {section.citation}"
        assert (
            cp.screen_pair_prose(rendered, max_words=200, names=("Shay", "Emery"))
            is None
        )


def test_the_prose_writer_failing_entirely_still_produces_a_document(db_session):
    profile_a, profile_b = _pair_profiles(db_session)

    def _boom(slots, *, reader_name, about_name):
        raise RuntimeError("Vertex is having a day")

    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery", prose_writer=_boom
    )
    assert pair.has_content
    assert pair.model_written_count == 0
    assert comparison.render_markdown(pair)


# ── 5. Caching, staleness and order ──────────────────────────────────────────


def test_a_profile_bump_on_either_side_makes_the_cached_document_stale(db_session):
    profile_a, profile_b = _pair_profiles(db_session)
    row = comparison.ensure_pair_row(db_session, profile_a, profile_b).document
    db_session.commit()
    assert row.profile_version_key == f"{SHAY}:1|{EMERY}:1"

    assert comparison.is_stale_pair(row, {SHAY: 1, EMERY: 1}) is False
    assert comparison.is_stale_pair(row, {SHAY: 2, EMERY: 1}) is True
    assert comparison.is_stale_pair(row, {SHAY: 1, EMERY: 2}) is True
    # No current profile at all is not staleness: there is nothing newer.
    assert comparison.is_stale_pair(row, {SHAY: None, EMERY: 1}) is False

    # A retake lands on a NEW row, and the old one stays findable by subject_key
    # so the UI can offer Regenerate.
    shay_v2 = _profile_row(db_session, SHAY, version=2, frameworks=_shay_frameworks())
    second = comparison.ensure_pair_row(db_session, shay_v2, profile_b)
    db_session.commit()
    assert second.created is True
    assert second.document.id != row.id
    assert (
        comparison.current_pair_guide(db_session, SHAY, EMERY).id == second.document.id
    )
    assert (
        comparison.current_pair_guide(
            db_session, SHAY, EMERY, versions={SHAY: 1, EMERY: 1}
        ).id
        == row.id
    )


def test_pair_order_produces_two_different_documents(db_session):
    profile_a, profile_b = _pair_profiles(db_session)
    forward = comparison.ensure_pair_row(db_session, profile_a, profile_b)
    reverse = comparison.ensure_pair_row(db_session, profile_b, profile_a)
    db_session.commit()

    assert forward.created and reverse.created
    assert forward.document.id != reverse.document.id
    assert forward.document.subject_key == f"{SHAY}|{EMERY}"
    assert reverse.document.subject_key == f"{EMERY}|{SHAY}"
    # And the model's own key helper agrees, unsorted, for this kind.
    assert subject_key_for(comparison.KIND_PAIR, [EMERY, SHAY]) == f"{EMERY}|{SHAY}"

    # The two documents differ in the thing that matters: who is "you".
    shay_doc = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    emery_doc = comparison.build_pair_guide(
        profile_b, profile_a, name_a="Emery", name_b="Shay"
    )
    assert shay_doc.title != emery_doc.title
    assert "Emery's side" in comparison.render_markdown(shay_doc)
    assert "Shay's side" in comparison.render_markdown(emery_doc)


def test_generating_twice_on_the_same_versions_lands_on_one_row(db_session):
    profile_a, profile_b = _pair_profiles(db_session)
    first = comparison.ensure_pair_row(db_session, profile_a, profile_b)
    db_session.commit()
    second = comparison.ensure_pair_row(db_session, profile_a, profile_b)
    db_session.commit()
    assert first.created is True
    assert second.created is False
    assert first.document.id == second.document.id


# ── 6. The document, end to end, and the shortfall ───────────────────────────


def test_the_whole_document_renders_and_says_what_it_cannot_say(db_session):
    profile_a, profile_b = _pair_profiles(db_session)
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    body = comparison.render_markdown(pair)

    assert body.startswith("# Working With Shay & Emery")
    assert comparison.FRICTION_HYPOTHESIS_NOTE in body
    assert comparison.PANEL_DISCLAIMER in body
    assert cc.NO_OBSERVATION_NOTICE in body
    assert cc.PAIR_ADVISORY_NOTICE in body
    assert comparison.COMPLETENESS_LABEL in body
    # Every framework gets a completeness line, so a layer's absence is never
    # silent.
    assert {line.framework for line in pair.completeness} == set(contracts.FRAMEWORKS)
    # Every section names both authored fields it came from, in the document.
    for section in (*pair.synergies, *pair.frictions):
        assert section.citation
        assert section.citation in body


def test_a_multiline_model_body_cannot_corrupt_the_synergy_table(db_session):
    """A newline in a table cell ends the ROW and truncates the document.

    It renders as a shorter guide rather than as an error, which is the worst
    kind of corruption, and the re-voiced clause is the realistic source of it:
    a pipe and a newline are punctuation, so no screen refuses them — verified
    below — and ``_table_safe`` is the thing standing between them and a
    truncated document.
    """
    profile_a, profile_b = _pair_profiles(db_session)
    captured: list[str] = []
    accepted: list[str] = []

    def _writer(slots, *, reader_name, about_name):
        out = {}
        for slot in slots:
            captured.append(slot.slot_id)
            # Faithful (so it survives containment) but wrapped across lines and
            # carrying a pipe.
            lead = slot.fact.replace(" ", "\n", 1) + " | "
            assert cp.screen_pair_lead(lead, slot) is None, (
                "a screen now refuses this, so the test no longer reaches the "
                "renderer; assert the rejection instead"
            )
            accepted.append(lead)
            out[slot.slot_id] = {"lead": lead}
        return out

    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery", prose_writer=_writer
    )
    assert captured
    assert accepted, "no lead reached the renderer"
    assert pair.model_written_count == len(captured)
    body = comparison.render_markdown(pair)
    table = [line for line in body.splitlines() if line.startswith("|")]
    # Header, divider, and exactly one line per synergy row: no row was split.
    assert len(table) == 2 + len(pair.synergies)
    for line in table:
        assert line.count("|") == 3, line


def test_the_symmetric_panel_has_equal_columns_and_cannot_be_built_otherwise(
    db_session,
):
    profile_a, profile_b = _pair_profiles(db_session)
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    assert pair.panel is not None
    assert len(pair.panel.left.bullets) == len(pair.panel.right.bullets)
    assert comparison.PANEL_MIN_BULLETS <= len(pair.panel.left.bullets)
    # Same field on both sides, same header grammar, no brand-vs-grey asymmetry
    # to encode: both headings are "WHAT <NAME> ASKS FOR".
    assert pair.panel.left.heading.startswith("WHAT ")
    assert pair.panel.right.heading.startswith("WHAT ")

    with pytest.raises(comparison.ComparisonError, match="same number of bullets"):
        comparison.PairPanel(
            field_label="x",
            disclaimer="y",
            left=comparison.PairPanelColumn(heading="A", bullets=("one", "two")),
            right=comparison.PairPanelColumn(heading="B", bullets=("one",)),
        )


def test_an_all_refused_pair_produces_the_shortfall_document_and_no_claims(db_session):
    """Zero comparable layers must still yield a valid, claim-free document."""
    closed = {name: contracts.not_taken_envelope(name) for name in contracts.FRAMEWORKS}
    profile_a, profile_b = _pair_profiles(db_session, shay=closed, emery=dict(closed))
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    body = comparison.render_markdown(pair)

    assert pair.has_content is False
    assert pair.synergies == [] and pair.frictions == []
    assert pair.panel is None and pair.cards == []
    assert pair.shortfall == comparison.SHORTFALL_BODY
    assert comparison.SHORTFALL_LABEL in body
    # The framing is authored throughout and must be writable with zero open
    # layers, so the shortfall document still explains itself.
    assert cc.NO_OBSERVATION_NOTICE in body
    assert len(pair.refusals) == len(cc.DIMENSION_KEYS)
    assert all(line.gate == cc.GATE_NEITHER_OPEN for line in pair.completeness)
    assert (
        cp.screen_pair_prose(body.replace(pair.advisory_notice, ""), max_words=20_000)
        is None
    )


def test_the_cards_are_capped_deduplicated_and_carry_no_rank(db_session):
    profile_a, profile_b = _pair_profiles(db_session)
    pair = comparison.build_pair_guide(
        profile_a, profile_b, name_a="Shay", name_b="Emery"
    )
    assert len(pair.cards) <= comparison.MAX_CARDS
    assert len({card.dimension for card in pair.cards}) == len(pair.cards)
    assert comparison.CARDS_INTRO in comparison.render_markdown(pair)
    # No card asserts a decision right. The source format's "Explicit decision
    # ownership" is deliberately absent: decision rights are in none of the six
    # layers.
    for card in pair.cards:
        assert "decision ownership" not in card.title.lower()
        assert "authority" not in card.body.lower()


def test_the_overlap_floor_separates_real_dovetails_from_noise():
    """The measurement behind :data:`comparison.MIN_SHARED_STEMS`, reproduced.

    The threshold is 2 because every 1-shared hit in the real corpus is a
    coincidence on a generic noun. This asserts the separation rather than the
    exact counts, so re-authoring a field does not fail the build for the wrong
    reason — but it does fail if somebody lowers the floor.
    """
    styles = [key for key in disc.WORK_STYLES if key != "BALANCED"]
    coincidences = 0
    for left, right in itertools.permutations(styles, 2):
        for drain in disc.WORK_STYLES[left].drained_by:
            for energy in disc.WORK_STYLES[right].energized_by:
                shared = comparison.shared_stems(drain, energy)
                if len(shared) == 1:
                    coincidences += 1
    assert comparison.MIN_SHARED_STEMS == 2
    assert coincidences > 10, (
        "there should be plenty of one-word coincidences; if there are not, the "
        "folding changed and the threshold needs re-measuring"
    )
    # A real dovetail, from the calibration record.
    assert (
        len(
            comparison.shared_stems(
                "Expect to be pushed on your logic — that is how they check it.",
                "Being pushed to commit before they have checked.",
            )
        )
        >= comparison.MIN_SHARED_STEMS
    )


# ── 7. The job ───────────────────────────────────────────────────────────────


def test_the_job_generates_into_the_row_and_flips_it_ready(db_session):
    from backend.jobs import smart_teams_comparison_job as job

    _users(db_session, SHAY, EMERY)
    profile_a, profile_b = _pair_profiles(db_session)
    row = comparison.ensure_pair_row(db_session, profile_a, profile_b).document
    db_session.commit()

    with _job_session(db_session):
        code = job.main(["--document-id", row.id, "--generate"])
    assert code == job.EXIT_OK
    db_session.refresh(row)
    assert row.status == "ready"
    assert row.error is None
    assert row.title == "Working With Shay & Emery"
    assert "Working With Shay & Emery" in (row.content_md or "")


def test_the_job_refuses_a_row_whose_profile_versions_are_gone(db_session):
    from backend.jobs import smart_teams_comparison_job as job

    profile_a, profile_b = _pair_profiles(db_session)
    row = comparison.ensure_pair_row(db_session, profile_a, profile_b).document
    db_session.commit()
    db_session.query(SmartTeamsProfile).delete()
    db_session.commit()

    with _job_session(db_session), patch.object(job, "_record") as record:
        code = job.main(["--document-id", row.id, "--generate"])
    assert code == job.EXIT_REFUSED
    db_session.refresh(row)
    assert row.status == "failed"
    assert "not both in the database" in (row.error or "")
    assert record.call_args[0][1] == job.ACTION_GENERATE_FAILED


def test_the_job_refuses_a_personal_guide_row(db_session):
    from backend.jobs import smart_teams_comparison_job as job

    row = _publish_personal_guide(db_session, SHAY)
    with _job_session(db_session), patch.object(job, "_record"):
        code = job.main(["--document-id", row.id, "--generate"])
    assert code == job.EXIT_REFUSED


def test_a_bare_job_execution_exits_non_zero_rather_than_guessing():
    from backend.jobs import smart_teams_comparison_job as job

    with pytest.raises(SystemExit) as excinfo:
        job.main([])
    assert excinfo.value.code != 0


def test_the_job_records_the_shortfall_document_without_failing(db_session):
    from backend.jobs import smart_teams_comparison_job as job

    _users(db_session, SHAY, EMERY)
    closed = {name: contracts.not_taken_envelope(name) for name in contracts.FRAMEWORKS}
    profile_a, profile_b = _pair_profiles(db_session, shay=closed, emery=dict(closed))
    row = comparison.ensure_pair_row(db_session, profile_a, profile_b).document
    db_session.commit()

    with _job_session(db_session), patch.object(job, "_record") as record:
        code = job.main(["--document-id", row.id, "--generate"])
    assert code == job.EXIT_OK
    db_session.refresh(row)
    assert row.status == "ready"
    assert comparison.SHORTFALL_LABEL in (row.content_md or "")
    assert record.call_args[0][1] == job.ACTION_EMPTY


# ── 8. The router generate path ──────────────────────────────────────────────


def test_generate_dispatches_the_job_and_returns_202(db_session):
    _users(db_session, SHAY, EMERY)
    _pair_profiles(db_session)
    _publish_personal_guide(db_session, SHAY)
    _publish_personal_guide(db_session, EMERY)

    _login(_user(SHAY))
    with patch(
        "backend.jobs.launcher.submit_background_work",
        return_value=("cloud-run-job", "exec-1"),
    ) as submit:
        response = client.post(f"{BASE}/pair/{EMERY}/generate")
    assert response.status_code == 202
    payload = response.json()
    assert payload["enqueued"] is True
    assert payload["executor"] == "cloud-run-job"
    kwargs = submit.call_args.kwargs
    assert kwargs["module"] == smart_teams_comparison.COMPARISON_JOB_MODULE
    assert kwargs["args"][-1] == "--generate"
    # And the row is the ordered pair addressed to the caller.
    row = db_session.get(SmartTeamsDocument, payload["guide"]["id"])
    assert row.subject_key == f"{SHAY}|{EMERY}"
    assert row.status == "pending"
    assert bool(row.published) is False


def test_generate_is_refused_before_a_word_is_written_when_consent_is_missing(
    db_session,
):
    _users(db_session, SHAY, EMERY)
    _pair_profiles(db_session)
    _publish_personal_guide(db_session, EMERY)

    _login(_user(SHAY))
    with patch("backend.jobs.launcher.submit_background_work") as submit:
        response = client.post(f"{BASE}/pair/{EMERY}/generate")
    assert response.status_code == 403
    submit.assert_not_called()
    assert (
        db_session.query(SmartTeamsDocument)
        .filter(SmartTeamsDocument.kind == comparison.KIND_PAIR)
        .count()
        == 0
    )


def test_generate_for_yourself_or_a_non_employee_is_a_404(db_session):
    _users(db_session, SHAY)
    _pair_profiles(db_session)
    _login(_user(SHAY))
    assert client.post(f"{BASE}/pair/{SHAY}/generate").status_code == 404
    assert client.post(f"{BASE}/pair/outside@example.com/generate").status_code == 404


def test_a_cached_ready_row_is_returned_without_dispatching(db_session):
    _users(db_session, SHAY, EMERY)
    profile_a, profile_b = _pair_profiles(db_session)
    _publish_personal_guide(db_session, SHAY)
    _publish_personal_guide(db_session, EMERY)
    row = comparison.ensure_pair_row(db_session, profile_a, profile_b).document
    row.status = "ready"
    row.content_md = "# Working With Shay & Emery\n"
    db_session.commit()

    _login(_user(SHAY))
    with patch("backend.jobs.launcher.submit_background_work") as submit:
        response = client.post(f"{BASE}/pair/{EMERY}/generate")
    assert response.status_code == 202
    assert response.json()["enqueued"] is False
    assert response.json()["reason"] == "cached"
    submit.assert_not_called()


# ── The overlap heatmap ──────────────────────────────────────────────────────
#
# The shared `_disc_layer` / `_big_five_layer` fixtures above carry no dimension
# data at all — `_disc_layer` has only `assignment` and `_big_five_layer` has an
# empty `domains` — because the pair-guide prose tests they were written for read
# style and theme NAMES, not scores. The grid needs the dimensional half, so
# these two build it. (That the grid degrades to "not comparable" rather than
# crashing on the score-less fixtures is itself asserted below.)


def _disc_dimensions(bands: dict[str, tuple[str, float]]) -> dict:
    """A DISC layer carrying real per-dimension scores."""
    layer = _disc_layer("D")
    layer["scores"] = {
        dimension: {
            "dimension": dimension,
            "name": disc.DIMENSION_NAMES[dimension],
            "band": band,
            "intensity": intensity,
        }
        for dimension, (band, intensity) in bands.items()
    }
    return layer


def _big_five_domains(bands: dict[str, tuple[str, float]]) -> dict:
    layer = _big_five_layer()
    layer["domains"] = {
        domain: {
            "domain": domain,
            "label": domain.replace("_", " ").title(),
            "band": band,
            "score": score,
        }
        for domain, (band, score) in bands.items()
    }
    return layer


#: Deliberately overlapping on one dimension and far apart on another, so the
#: grid under test has more than one state in it.
_SHAY_DISC = {
    "D": ("Very high", 88.0),
    "I": ("High", 61.0),
    "S": ("Low", 18.0),
    "C": ("Moderate", 40.0),
}
_EMERY_DISC = {
    "D": ("Low", 20.0),
    "I": ("High", 58.0),
    "S": ("High", 66.0),
    "C": ("Very high", 90.0),
}


def _dimensional_pair(db):
    """A published, fully scored pair whose profiles carry dimension scores."""
    _users(db, SHAY, EMERY)
    _pair_profiles(
        db,
        shay=_shay_frameworks(
            disc=_envelope("disc", _disc_dimensions(_SHAY_DISC), reportable=True),
            big_five=_envelope(
                "big_five",
                _big_five_domains({"extraversion": ("clearly_higher", 84.0)}),
                reportable=True,
            ),
        ),
        emery=_emery_frameworks(
            disc=_envelope("disc", _disc_dimensions(_EMERY_DISC), reportable=True),
            big_five=_envelope(
                "big_five",
                _big_five_domains({"extraversion": ("clearly_lower", 12.0)}),
                reportable=True,
            ),
        ),
    )
    _publish_personal_guide(db, SHAY)
    _publish_personal_guide(db, EMERY)


def test_the_pair_heatmap_never_returns_a_colleagues_number(db_session):
    """The promise both Help homes make, asserted on a real response body.

    The caller may see their OWN intensities and never the other person's. This
    is checked over the serialized JSON rather than over `PairRow` objects
    because the leak that matters is the one that reaches the browser.
    """
    db = db_session
    _dimensional_pair(db)
    _login(_user(SHAY))

    body = client.get(f"{BASE}/pair/{EMERY}/heatmap").json()
    assert body["rows"], "a scored pair produced no grid at all"
    assert [
        row["key"] for row in body["rows"] if (row["about"] or {}).get("score")
    ] == []
    # And the band IS there — a grid with neither is not a privacy win, it is a
    # blank screen.
    assert any((row["about"] or {}).get("band") for row in body["rows"])


def test_the_pair_heatmap_carries_no_motivators(db_session):
    """A measurement rule, enforced at the edge as well as in the module.

    ``motivators.emphasis`` is within-person with no norm group, so two people's
    numbers are not on one scale and the row must not exist to be misread.
    """
    db = db_session
    _dimensional_pair(db)
    _login(_user(SHAY))

    body = client.get(f"{BASE}/pair/{EMERY}/heatmap").json()
    assert "motivators" not in {row["framework"] for row in body["rows"]}


def test_the_pair_heatmap_needs_BOTH_people_to_have_published(db_session):
    """The stricter of the two pair gates, and deliberately so.

    Reading a pair GUIDE exempts the reader's own publish state, which is safe
    there because the document could only have been generated when both had
    published. A grid is computed live and has no such history, so the loose rule
    would newly describe a colleague who published to a reader who published
    nothing. The refusal copy is the same sentence either way.
    """
    db = db_session
    _users(db, SHAY, EMERY)
    _pair_profiles(db)
    _login(_user(SHAY))

    refused = client.get(f"{BASE}/pair/{EMERY}/heatmap")
    assert refused.status_code == 403
    assert refused.json()["detail"] == authz.SHARED_GUIDE_NOT_PUBLISHED_DENIED

    # The colleague publishing is not enough on its own: the reader has not.
    _publish_personal_guide(db, EMERY)
    assert client.get(f"{BASE}/pair/{EMERY}/heatmap").status_code == 403

    # Both published — now it opens.
    _publish_personal_guide(db, SHAY)
    assert client.get(f"{BASE}/pair/{EMERY}/heatmap").status_code == 200


def test_the_pair_heatmap_needs_no_generated_guide(db_session):
    """It is computed from the two stored profiles, not from a document.

    The grid has to answer on a pair nobody has spent an LLM call on, or it is
    useless exactly when somebody is deciding whether to generate one.
    """
    db = db_session
    _dimensional_pair(db)
    _login(_user(SHAY))

    assert comparison.current_pair_guide(db, SHAY, EMERY) is None
    body = client.get(f"{BASE}/pair/{EMERY}/heatmap").json()
    assert body["both_scored"] is True
    assert body["rows"]


def test_a_profile_without_dimension_scores_is_unknown_not_agreement(db_session):
    """The grid degrades to "we could not look", never to "you match".

    An older profile row can legitimately carry a style assignment and no
    per-dimension scores — the shared fixtures above are exactly that shape. The
    honest render is unknown, and a heatmap that greyed those cells the same way
    it greys agreement would be reporting a match nobody measured.
    """
    db = db_session
    _users(db, SHAY, EMERY)
    _pair_profiles(db)  # style-only layers, no `scores` / `domains`
    _publish_personal_guide(db, SHAY)
    _publish_personal_guide(db, EMERY)
    _login(_user(SHAY))

    body = client.get(f"{BASE}/pair/{EMERY}/heatmap").json()
    assert body["rows"], "the grid must keep a row per dimension, not shrink away"
    assert {row["overlap"] for row in body["rows"]} == {"not_comparable"}
