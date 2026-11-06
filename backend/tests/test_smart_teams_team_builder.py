"""ST-9 — the Team Builder: what it matches on, and what it refuses to say.

Nine things would fail silently if they regressed, and each has a test below.

1. **An unconfirmed inference is not a skill.** The Team Builder predicate is
   ``confirmed.is_(True)`` and nothing else, so a suggestion the person has never
   answered — and a denial, which lives in the same state — must not put them in
   a group. Asserted by seeding a suggestion, matching, and getting an empty
   answer, then confirming the SAME row and getting the person.

2. **Somebody with no published profile is proposed, and the answer says what is
   therefore unknown.** Dropping them would make the Work Style Interview a
   selection criterion for internal project work. So: they appear, they are
   counted in ``profiles_missing_for``, and a line says Cubie can speak to what
   they confirmed and not to how they work.

3. **An all-one-style group is a GROUP finding.** This is the actual product
   value ("this group is five Momentum and no Stewardship" is a sentence a
   manager can act on), and the test pins that the finding exists, is about the
   group, and names nobody.

4. **PTO removes somebody from the window.** Out for the whole window is out; out
   for part of it stays with a return date. When everybody who has confirmed a
   required skill is out, the answer is ``everyone_out`` and NOT a group missing
   a skill silently.

5. **An unsatisfiable ask is a first-class answer.** Nobody has confirmed the
   skill → an empty roster, a shortfall naming the skill, no invented member, and
   copy that refuses to say the company has nobody who can do it.

6. **A planted comparative or evaluative sentence about an individual is
   rejected.** Both at the screen (``team_prompts.screen_rationale``) and end to
   end through ``compose_rationale`` with a writer that returns the violation —
   which must fall back to the authored line rather than dropping the content.

7. **The chat tool without a principal fails closed.** An unresolvable identity,
   an off-domain identity and a narrowed-out identity each get an explicit refusal
   that tells the model NOT to report an empty result.

8. **No raw score and no transcript quote reaches any output.** The seeded
   profile carries a real-shaped DISC score block and a strengths evidence quote;
   the report and the serialized API response are grepped for both.

9. **The gate is the copilot gate STACKED on the company gate**, per plan.md
   Decision #4, and the tool is actually registered on the coordinator — this
   feature has already shipped one 202-that-did-nothing, and a built-but-
   unregistered chat tool is the same failure.

The LLM is always stubbed: ``build_team`` makes no model call at all, and every
``compose_rationale`` here is given an explicit ``rationale_writer``. In-memory
SQLite with a ``StaticPool`` and ``app.dependency_overrides``, per
``test_smart_teams_skills.py``. The GATES are never overridden — only
``get_current_user`` — so both dependencies run for real.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.tests._rosters import narrow_copilot
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, OOOPeriod, User
from rubik_core.database.smart_teams import SmartTeamsProfile
from rubik_core.smart_teams import (
    authz,
    comparison_contracts,
    contracts,
    guide,
    team_builder,
    team_contracts,
    team_prompts,
)
from rubik_core.smart_teams import skill_taxonomy as taxonomy
from rubik_core.smart_teams import skills as store
from rubik_core.smart_teams.guide_prompts import ProseSlot, novel_content_words
from rubik_core.smart_teams.instruments import disc, strengths

client = TestClient(app)

BASE = "/api/smart-teams/teams"

# The copilot roster is what gates this router; Sales is on it and HR is not.
COPILOT_DEPARTMENT = "Sales"
NARROWED_OUT_DEPARTMENT = "Human Resources"

LEAD = "alice@acme.example"  # the requester, and alphabetically first
BOB = "bob@acme.example"
CARLA = "carla@acme.example"
DREW = "drew@acme.example"

# Raw data that must never reach a colleague-facing output.
SECRET_INTENSITY = 87.4
SECRET_QUOTE = "I rewrote the whole pricing matrix over a weekend"
SECRET_MEETING = "CONFIDENTIAL: Q1 layoff planning"
TODAY = date(2026, 8, 20)


# ── identities ───────────────────────────────────────────────────────────────


def _profile(
    email: str,
    *,
    department: str = COPILOT_DEPARTMENT,
    is_admin: bool = False,
    is_exec: bool = False,
) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["admin"] if is_admin else ["user"],
        is_exec=is_exec,
        managed_departments=[],
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
    )


def _as(email: str, **kw) -> None:
    """Sign in as somebody. Both GATES still run — only identity is faked."""
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(engine, monkeypatch):
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    db = Session()
    app.dependency_overrides[get_db] = lambda: db
    _as(LEAD)
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()


@pytest.fixture
def seeded(db_session):
    """A real taxonomy and four real users. No skills, no profiles yet."""
    taxonomy.seed_taxonomy(db_session)
    for email in (LEAD, BOB, CARLA, DREW):
        db_session.add(
            User(
                email=email,
                id=email.split("@")[0],
                name=email.split("@")[0].title() + " Person",
                department="Compliance",
                last_login=datetime(2026, 8, 1),
            )
        )
    db_session.commit()
    return db_session


# ── seeding helpers ──────────────────────────────────────────────────────────


def _declare(db, email: str, **skills: str) -> None:
    """Skills the person declared themselves. ``confirmed = True`` by definition."""
    store.record_declared_skills(db, email, dict(skills))


def _suggest(db, email: str, skill_key: str) -> None:
    """An AI inference the person has NOT answered. ``confirmed = False``."""
    store.upsert_inference(
        db,
        store.Suggestion(
            user_email=email,
            skill_key=skill_key,
            level="working",
            rationale="Ran this in a meeting.",
            items=({"kind": "meeting", "ref": "m/1", "title": SECRET_MEETING},),
        ),
    )


def _envelope(framework: str, layer: dict | None, *, reportable: bool = True) -> dict:
    """One framework payload, built from the contract's own key constants."""
    return {
        contracts.KEY_SCHEMA_VERSION: contracts.PAYLOAD_SCHEMA_VERSION,
        contracts.KEY_FRAMEWORK: framework,
        contracts.KEY_REPORTABLE: reportable,
        contracts.KEY_CONFIDENCE: "high" if reportable else "none",
        contracts.KEY_REASONS: [] if reportable else [contracts.REASON_NOT_TAKEN],
        contracts.KEY_HEDGE: None,
        contracts.KEY_INSTRUMENT_VERSION: "test",
        contracts.KEY_SCORED_AT: "2026-08-01T00:00:00",
        contracts.KEY_LAYER: layer,
    }


def _disc_layer(style_key: str) -> dict:
    """A DISC layer shaped like the real one, INCLUDING a raw score block."""
    style = disc.WORK_STYLES[style_key]
    return {
        "layer": "disc",
        # The raw numbers a colleague must never see. Real shape: this is what
        # ``DimensionScore.as_dict()`` puts in the stored payload.
        "scores": {
            "D": {
                "dimension": "D",
                "intensity": SECRET_INTENSITY,
                "band": "high",
                "likert_percent": 91.2,
            }
        },
        "assignment": {
            "style": style.as_dict(),
            "primary": style.primary,
            "secondary": style.secondary,
            "ranking": ["D", "I", "S", "C"],
            "confidence": "high",
        },
        "quality": {"flags": []},
        "advisory_notice": disc.ADVISORY_NOTICE,
    }


def _strengths_layer(cluster: strengths.Cluster) -> dict:
    """A strengths layer with a verbatim interview quote in it."""
    theme = next(spec for spec in strengths.THEMES.values() if spec.cluster == cluster)
    return {
        "layer": "strengths",
        "taxonomy_version": strengths.TAXONOMY_VERSION,
        "thin_transcript": False,
        "themes": [
            {
                "theme_id": theme.theme_id,
                "name": theme.name,
                "cluster": cluster.value,
                "confidence": "high",
                "evidence_quote": SECRET_QUOTE,
                "why": "They described doing exactly this.",
            }
        ],
        "clusters_covered": [cluster.value],
        "note": None,
    }


def _publish_profile(
    db,
    email: str,
    *,
    style_key: str | None = "D",
    cluster: strengths.Cluster | None = strengths.Cluster.MOMENTUM,
    publish: bool = True,
    version: int = 1,
) -> SmartTeamsProfile:
    """A scored current profile and (by default) a PUBLISHED personal guide.

    Publication goes through ``guide.approve`` — the only writer of
    ``published = True`` — so the test exercises the real consent path and the
    CHECK constraint that backs it.

    ``version`` above 1 is a RETAKE: every earlier row for this person is
    superseded first, because ``is_current`` is ``True``-or-``NULL`` and
    ``uq_st_profile_one_current`` is a real constraint. Leave ``publish=False``
    to get the state a retake actually produces — a new current profile whose
    guide is a draft nobody has approved.
    """
    if version > 1:
        for row in (
            db.query(SmartTeamsProfile)
            .filter(SmartTeamsProfile.user_email == email)
            .all()
        ):
            row.is_current = None
            row.status = "superseded"
        db.flush()
    profile = SmartTeamsProfile(
        id=f"prof-{email}-v{version}",
        user_email=email,
        version=version,
        status="current",
        is_current=True,
        disc=_envelope(
            "disc",
            _disc_layer(style_key) if style_key else None,
            reportable=style_key is not None,
        ),
        big_five=_envelope("big_five", None, reportable=False),
        type16=_envelope("type16", None, reportable=False),
        strengths=_envelope(
            "strengths",
            _strengths_layer(cluster) if cluster else None,
            reportable=cluster is not None,
        ),
        motivators=_envelope("motivators", None, reportable=False),
        enneagram=_envelope("enneagram", None, reportable=False),
        provenance={f: "interview" for f in contracts.FRAMEWORKS},
        scored_at=datetime(2026, 8, 1),
    )
    db.add(profile)
    db.flush()

    row = guide.ensure_guide_row(db, profile)
    row.document.status = "ready"
    row.document.content_md = "# How to work with them\n\nSomething authored."
    db.flush()
    if publish:
        guide.approve(db, row.document, approver_email=email)
    db.commit()
    return profile


def _out(db, email: str, start: date, end: date) -> None:
    db.add(
        OOOPeriod(
            user_email=email,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            return_date=(end + timedelta(days=1)).isoformat(),
            synced_at=datetime(2026, 8, 19),
        )
    )
    db.commit()


def _ask(
    *,
    requester: str = LEAD,
    required: tuple[str, ...] = ("data privacy",),
    optional: tuple[str, ...] = (),
    size: int | None = None,
    starts_on: date | None = None,
    ends_on: date | None = None,
    clusters: tuple[str, ...] = (),
    raw_ask: str = "get me a team for the agency exam",
) -> team_contracts.TeamRequest:
    return team_contracts.TeamRequest.from_phrases(
        requested_by=requester,
        required_phrases=list(required),
        optional_phrases=list(optional),
        size=size,
        starts_on=starts_on,
        ends_on=ends_on,
        desired_clusters=list(clusters),
        raw_ask=raw_ask,
    )


def _no_model(slots, **_kw) -> dict:
    """The stub every test runs with unless it is testing the writer."""
    return {}


# ═════════════════════════════════════════════════════════════════════════════
# 1. An unconfirmed inference is not a skill
# ═════════════════════════════════════════════════════════════════════════════


def test_an_unconfirmed_inference_never_puts_somebody_in_a_group(seeded):
    """The predicate is ``confirmed.is_(True)`` and nothing else.

    The same row is matched twice — once as a pending suggestion, once after the
    person confirmed it — so the test cannot pass by the person being absent for
    some other reason.
    """
    db = seeded
    _suggest(db, BOB, "data_privacy")
    db.commit()

    answer = team_builder.build_team(db, _ask(), today=TODAY)
    assert answer.result.emails == (), (
        "an AI-inferred skill the person has never answered put them in a group. "
        "Team Builder reads confirmed.is_(True) only — a suggestion and a denial "
        "are both confirmed=False"
    )
    assert answer.result.presentation_state == team_contracts.STATE_EMPTY
    codes = {s.code for s in answer.result.shortfalls}
    assert team_contracts.SHORTFALL_NO_ONE_WITH_SKILL in codes, codes
    # Both are true here and both are said: nobody has confirmed this skill, AND
    # there is nothing on file from anybody. They are different facts with
    # different fixes, which is why they are separate codes.
    assert team_contracts.SHORTFALL_NO_PUBLISHED_PROFILES in codes, codes

    store.confirm(db, BOB, "data_privacy")
    db.commit()
    after = team_builder.build_team(db, _ask(), today=TODAY)
    assert after.result.emails == (BOB,)


def test_a_denied_suggestion_stays_out(seeded):
    """A denial tombstone is ``confirmed = False``, so one predicate excludes both."""
    db = seeded
    _suggest(db, BOB, "data_privacy")
    store.deny(db, BOB, "data_privacy")
    db.commit()
    assert team_builder.build_team(db, _ask(), today=TODAY).result.emails == ()


# ═════════════════════════════════════════════════════════════════════════════
# 2. Somebody with no published profile is proposed, and it is said out loud
# ═════════════════════════════════════════════════════════════════════════════


def test_a_person_with_no_published_profile_is_proposed_and_the_gap_is_stated(seeded):
    """Not dropped, and not quietly rendered as if their style were known.

    Dropping them would make "has taken the Work Style Interview" a selection
    criterion for internal project work. So the assertions are all three halves:
    they are in the group, the balance counts them as missing, and a sentence
    says what is therefore unknown.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    db.commit()  # no profile row, no guide

    answer = team_builder.answer_for(
        db, _ask(), today=TODAY, rationale_writer=_no_model
    )
    assert answer.result.emails == (BOB,)
    assert answer.result.balance.profiles_missing_for == 1
    assert answer.result.balance.styles_present == ()

    lines = [line.text for line in answer.rationale]
    assert any("has not published a working guide" in line for line in lines), lines
    report = team_builder.render_report(answer)
    assert "has not published a working guide" in report


def test_an_unpublished_guide_is_not_read(seeded):
    """A draft nobody approved is not a company-visible fact about how they work.

    Same profile row, same open gates; only ``published`` differs. The style must
    not appear, because publication is the consent switch.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _publish_profile(db, BOB, publish=False)

    answer = team_builder.build_team(db, _ask(), today=TODAY)
    assert answer.result.emails == (BOB,)
    candidate = answer.result.candidates[0]
    assert candidate.work_style_key is None, (
        "an unpublished draft's DISC style reached a colleague-facing answer"
    )
    assert candidate.clusters == frozenset()
    assert answer.result.balance.profiles_missing_for == 1


def test_a_closed_gate_is_not_read_even_when_the_guide_is_published(seeded):
    """Consent is not the only gate: the layer's own gate still applies."""
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _publish_profile(db, BOB, style_key=None, cluster=strengths.Cluster.MOMENTUM)

    answer = team_builder.build_team(db, _ask(), today=TODAY)
    candidate = answer.result.candidates[0]
    assert candidate.work_style_key is None
    assert candidate.clusters == frozenset({strengths.Cluster.MOMENTUM.value})


# ═════════════════════════════════════════════════════════════════════════════
# 3. An all-one-style group is a group finding
# ═════════════════════════════════════════════════════════════════════════════


def test_an_all_one_style_group_is_surfaced_as_a_group_finding(seeded):
    """The product value, and it must be about the GROUP and name nobody."""
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")
    _publish_profile(db, BOB, style_key="D")
    _publish_profile(db, CARLA, style_key="D")

    answer = team_builder.answer_for(
        db,
        _ask(required=("data privacy", "gaap")),
        today=TODAY,
        rationale_writer=_no_model,
    )
    balance = answer.result.balance
    assert set(answer.result.emails) == {BOB, CARLA}
    assert balance.all_one_style is True
    assert balance.styles_present == ("D", "D")

    note = balance.note
    assert "same one" in note, note
    for name in (BOB, CARLA, "Bob", "Carla"):
        assert name not in note, (
            f"the all-one-style finding named {name}. It is a fact about the "
            "group; the same fact attached to a person is a label stuck to a "
            "colleague"
        )
    # Every authored sentence must survive our own screens, and must be short
    # enough that a model restatement of it CAN survive them — otherwise the most
    # valuable finding in the answer is the one guaranteed to fall back.
    sentences = team_builder.balance_sentences(balance, roster_size=2)
    assert sentences and " ".join(sentences) == note
    for sentence in sentences:
        slot = ProseSlot(
            slot_id="x",
            intent="i",
            fact=sentence,
            heading="h",
            body=sentence,
            open_frameworks=frozenset(contracts.FRAMEWORKS),
        )
        assert team_prompts.screen_rationale(sentence, slot) is None, sentence


def test_two_different_styles_is_not_reported_as_all_one_style(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")
    _publish_profile(db, BOB, style_key="D")
    _publish_profile(db, CARLA, style_key="S")

    answer = team_builder.build_team(
        db, _ask(required=("data privacy", "gaap")), today=TODAY
    )
    assert answer.result.balance.all_one_style is False
    assert answer.result.balance.styles_present == ("D", "S")


def test_a_missing_cluster_is_reported_with_the_authored_meaning(seeded):
    """``strengths.CLUSTER_MEANING`` verbatim — one sentence, one home."""
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _publish_profile(db, BOB, cluster=strengths.Cluster.MOMENTUM)

    answer = team_builder.build_team(db, _ask(), today=TODAY)
    balance = answer.result.balance
    assert strengths.Cluster.STEWARDSHIP.value in balance.clusters_missing
    assert strengths.CLUSTER_MEANING[strengths.Cluster.STEWARDSHIP] in balance.note


# ═════════════════════════════════════════════════════════════════════════════
# 4. PTO
# ═════════════════════════════════════════════════════════════════════════════


def test_somebody_out_for_the_whole_window_is_not_proposed(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, data_privacy="working")
    _out(db, BOB, date(2026, 9, 1), date(2026, 9, 30))

    answer = team_builder.build_team(
        db,
        _ask(starts_on=date(2026, 9, 7), ends_on=date(2026, 9, 11)),
        today=TODAY,
    )
    assert answer.result.emails == (CARLA,), (
        "somebody out for the whole requested window was still proposed"
    )


def test_somebody_out_for_part_of_the_window_stays_with_a_return_date(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _out(db, BOB, date(2026, 9, 9), date(2026, 9, 10))

    answer = team_builder.answer_for(
        db,
        _ask(starts_on=date(2026, 9, 7), ends_on=date(2026, 9, 11)),
        today=TODAY,
        rationale_writer=_no_model,
    )
    candidate = answer.result.candidates[0]
    assert candidate.availability == team_contracts.AVAILABILITY_PARTIAL
    assert candidate.back_on == date(2026, 9, 11)
    assert "out of office for part of this window" in team_builder.render_report(answer)


def test_everybody_who_has_the_skill_being_out_is_its_own_answer(seeded):
    """Not a group missing a skill silently, and not "nobody has it"."""
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _out(db, BOB, date(2026, 9, 1), date(2026, 9, 30))

    answer = team_builder.build_team(
        db,
        _ask(starts_on=date(2026, 9, 7), ends_on=date(2026, 9, 11)),
        today=TODAY,
    )
    assert answer.result.emails == ()
    codes = {s.code for s in answer.result.shortfalls}
    assert team_contracts.SHORTFALL_EVERYONE_OUT in codes, codes
    assert team_contracts.SHORTFALL_NO_ONE_WITH_SKILL not in codes, (
        "'everybody is away' was reported as 'nobody has confirmed this'"
    )


def test_no_time_off_record_is_unknown_and_never_available(seeded):
    """ "We did not check" and "they are free" must not render the same."""
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")
    _out(db, CARLA, date(2026, 12, 1), date(2026, 12, 2))  # a row, not overlapping

    answer = team_builder.build_team(
        db, _ask(required=("data privacy", "gaap")), today=TODAY
    )
    states = {c.user_email: c.availability for c in answer.result.candidates}
    assert states[BOB] == team_contracts.AVAILABILITY_UNKNOWN
    assert states[CARLA] == team_contracts.AVAILABILITY_AVAILABLE


def test_a_window_past_the_horizon_is_unknown_and_says_so(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _out(db, BOB, date(2026, 8, 25), date(2026, 8, 26))  # inside the horizon

    answer = team_builder.build_team(
        db,
        _ask(starts_on=TODAY, ends_on=TODAY + timedelta(days=120)),
        today=TODAY,
    )
    assert answer.result.horizon_note == team_contracts.HORIZON_NOTE
    assert team_contracts.SHORTFALL_WINDOW_BEYOND_HORIZON in {
        s.code for s in answer.result.shortfalls
    }


# ═════════════════════════════════════════════════════════════════════════════
# 5. Unsatisfiable asks
# ═════════════════════════════════════════════════════════════════════════════


def test_nobody_confirming_the_skill_is_an_honest_empty_answer(seeded):
    """No invented member, and the copy refuses the over-reading."""
    db = seeded
    _declare(db, BOB, financial_reporting="strong")  # a different skill

    answer = team_builder.answer_for(
        db, _ask(required=("data privacy",)), today=TODAY, rationale_writer=_no_model
    )
    assert answer.result.candidates == ()
    assert answer.result.presentation_state == team_contracts.STATE_EMPTY
    (shortfall,) = [
        s
        for s in answer.result.shortfalls
        if s.code == team_contracts.SHORTFALL_NO_ONE_WITH_SKILL
    ]
    assert shortfall.skill_key == "data_privacy"
    assert "not the same as the company having nobody" in shortfall.detail

    report = team_builder.render_report(answer)
    for email in (BOB, CARLA, DREW):
        assert email not in report, "an empty answer named somebody"
    assert "Bob" not in report


def test_nobody_in_scope_at_all_is_reported_as_nothing_on_file(seeded):
    """Distinct from "nobody has this skill": the interview has not been taken."""
    db = seeded  # users exist, but no confirmed skills and no published guides
    answer = team_builder.build_team(db, _ask(), today=TODAY)
    codes = {s.code for s in answer.result.shortfalls}
    assert team_contracts.SHORTFALL_NO_PUBLISHED_PROFILES in codes
    assert answer.result.candidates == (), (
        "a no-profiles answer must have an empty roster — the contract enforces "
        "it, and this pins that we honour it"
    )


def test_an_unrecognised_skill_phrase_is_reported_never_dropped(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")

    answer = team_builder.build_team(
        db, _ask(required=("data privacy", "reg z audit")), today=TODAY
    )
    reported = [
        s
        for s in answer.result.shortfalls
        if s.code == team_contracts.SHORTFALL_SKILL_NOT_IN_TAXONOMY
    ]
    assert [s.phrase for s in reported] == ["reg z audit"]
    assert "does not track a skill called 'reg z audit'" in reported[0].detail
    assert answer.result.presentation_state == team_contracts.STATE_PARTIAL


def test_a_group_smaller_than_the_size_asked_for_is_not_padded(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")  # not asked for
    _publish_profile(db, CARLA)

    answer = team_builder.build_team(db, _ask(size=3), today=TODAY)
    assert answer.result.emails == (BOB,), "the group was padded to reach the size"
    (shortfall,) = [
        s
        for s in answer.result.shortfalls
        if s.code == team_contracts.SHORTFALL_SIZE_NOT_MET
    ]
    assert "does not pad a group" in shortfall.detail
    assert answer.result.presentation_state == team_contracts.STATE_PARTIAL


# ═════════════════════════════════════════════════════════════════════════════
# 6. The prose screens
# ═════════════════════════════════════════════════════════════════════════════

_FACT = "Bob Person has confirmed Data privacy (GDPR / CCPA) (strong)."


def _slot(fact: str = _FACT) -> ProseSlot:
    return ProseSlot(
        slot_id="covers:bob@acme.example",
        intent="what this person has confirmed",
        fact=fact,
        heading="Bob Person",
        body=fact,
        open_frameworks=frozenset(),
    )


@pytest.mark.parametrize(
    "planted,expected",
    [
        # Cross-person ranking — the pair guide's screen, shared.
        (
            "Bob Person is stronger on data privacy than Carla Person.",
            team_prompts.REJECT_RANKING,
        ),
        ("Bob Person outperforms the others on Reg B.", team_prompts.REJECT_RANKING),
        (
            "Bob Person is the strongest of the two on this.",
            team_prompts.REJECT_RANKING,
        ),
        # Per-person evaluation with nobody to compare to — ST-9's own gap.
        (
            "Bob Person is the ideal fit for this exam.",
            team_prompts.REJECT_EVALUATIVE,
        ),
        (
            "Bob Person is well suited to running the Reg B review.",
            team_prompts.REJECT_EVALUATIVE,
        ),
        (
            "Bob Person is the most experienced person on data privacy.",
            team_prompts.REJECT_EVALUATIVE,
        ),
        ("Bob Person should lead this piece of work.", team_prompts.REJECT_EVALUATIVE),
        ("Bob Person is a strong candidate for it.", team_prompts.REJECT_EVALUATIVE),
        ("Bob Person would struggle with the volume.", team_prompts.REJECT_EVALUATIVE),
        ("Bob Person is the weakest link here.", team_prompts.REJECT_EVALUATIVE),
        # A compatibility score.
        (
            "Bob Person is a 90% fit for this work.",
            team_prompts.REJECT_RANKING,
        ),
    ],
)
def test_a_planted_comparative_or_evaluative_sentence_is_refused(planted, expected):
    """Every one of these names a real colleague in a document their manager reads."""
    assert team_prompts.screen_rationale(planted, _slot()) == expected, planted


def test_a_faithful_restatement_of_the_fact_survives_the_screens():
    """The screens must not refuse the sentence they exist to allow."""
    assert (
        team_prompts.screen_rationale(
            "Bob Person has confirmed data privacy (GDPR / CCPA).", _slot()
        )
        is None
    )


def test_a_name_that_is_not_in_the_group_is_refused():
    """Naming somebody who is not in the roster is the worst failure available."""
    reason = team_prompts.screen_rationale(
        "Bob Person and Marcus have both confirmed data privacy.",
        _slot(),
        allowed_names=("Bob Person",),
    )
    assert reason == team_prompts.REJECT_UNKNOWN_PERSON
    assert team_prompts.unknown_people_named(
        "Bob Person and Grant will cover it.", _FACT, ("Bob Person",)
    ) == ("Grant",)


def test_a_ranking_line_from_the_model_falls_back_to_the_authored_wording(seeded):
    """End to end: the violation is dropped and the CONTENT still ships.

    The failure this pins is not "the bad line was printed" — it is a screen that
    drops the line and leaves the answer with a hole in it.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")

    def _ranking_writer(slots, **_kw):
        return {
            slot.slot_id: "Bob Person is a better fit for this than Carla Person."
            for slot in slots
        }

    answer = team_builder.answer_for(
        db,
        _ask(required=("data privacy", "gaap")),
        today=TODAY,
        rationale_writer=_ranking_writer,
    )
    assert answer.rationale, "no lines at all"
    for line in answer.rationale:
        assert line.from_model is False
        assert line.text == line.authored
        assert "better fit" not in line.text
    assert "better fit" not in team_builder.render_report(answer)


def test_a_clean_model_line_on_a_group_slot_is_used(seeded):
    """The other half: a faithful restatement is kept, or the LLM is pointless.

    A GROUP slot, because those are the only ones a model is shown. The gap
    slot's fact is the "nobody has confirmed this" copy, which names a skill and
    no person.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    restated = (
        "Nobody has confirmed Financial reporting in Smart Teams. That is not the "
        "same as the company having nobody who can do it."
    )

    def _writer(slots, **_kw):
        return {
            slot.slot_id: restated
            for slot in slots
            if slot.slot_id.startswith(f"{team_builder.SLOT_GAP}:")
        }

    answer = team_builder.answer_for(
        db,
        _ask(required=("data privacy", "gaap")),
        today=TODAY,
        rationale_writer=_writer,
    )
    used = [line for line in answer.rationale if line.from_model]
    assert [line.text for line in used] == [restated], (
        "a faithful restatement of a group fact was rejected — strict "
        "containment has been tightened past the point where the model can "
        "contribute anything at all"
    )


def test_a_per_person_line_is_authored_even_when_the_writer_returns_a_clean_one(
    seeded,
):
    """The structural half of the wave-2 fix.

    The writer returns a sentence that would PASS every screen, keyed to a
    per-person slot. It must still not be used: ``compose_rationale`` never reads
    a model's output for a slot whose fact names somebody, so the question of
    whether the sentence was acceptable never arises.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    clean = "Bob Person has confirmed data privacy (GDPR / CCPA)."

    def _writer(slots, **_kw):
        # Key it to every slot id we can think of, including the ones this
        # writer was never offered.
        answer = team_builder.build_team(db, _ask(), today=TODAY)
        return {slot.slot_id: clean for slot in team_builder.rationale_slots(answer)}

    answer = team_builder.answer_for(db, _ask(), today=TODAY, rationale_writer=_writer)
    per_person = [
        line for line in answer.rationale if team_builder.names_a_person(line.slot_id)
    ]
    assert per_person, "no per-person lines at all — the fixture stopped working"
    for line in per_person:
        assert line.from_model is False, line.slot_id
        assert line.text == line.authored, line.slot_id
    assert clean not in team_builder.render_report(answer)


def test_the_writer_is_only_ever_offered_slots_that_name_nobody(seeded):
    """The filter is at the call site, so pin it AT the call site.

    Recorded from inside the writer rather than inferred from the output: a
    per-person fact that reaches a prompt has already left the building, whatever
    the screens do with what comes back.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")
    _publish_profile(db, BOB, style_key="D")
    _out(db, CARLA, date(2026, 8, 21), date(2026, 8, 22))
    offered: list = []

    def _recording_writer(slots, **_kw):
        offered.extend(slots)
        return {}

    answer = team_builder.answer_for(
        db,
        _ask(required=("data privacy", "gaap"), ends_on=TODAY + timedelta(days=7)),
        today=TODAY,
        rationale_writer=_recording_writer,
    )
    assert offered, "the writer was never called — the test proves nothing"
    kinds = {slot.slot_id.split(":", 1)[0] for slot in offered}
    assert kinds <= team_builder.GROUP_SLOT_KINDS, kinds

    # And the prompt those slots build carries no colleague's name.
    prompt = team_prompts.rationale_prompt(offered, ask=answer.result.request.raw_ask)
    for name in ("Bob", "Carla", BOB, CARLA):
        assert name not in prompt, (
            f"{name} reached the rationale prompt. The whole point of "
            "model_eligible_slots is that a model is never shown a fact about a "
            "colleague"
        )


def test_every_slot_kind_is_classified_as_naming_a_person_or_not(seeded):
    """An unclassified slot kind must fail here, not silently reach a model.

    ``names_a_person`` fails closed, so a new kind would be excluded from the
    prompt — safe, but it would also silently stop being model-eligible with no
    signal. This is the signal.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")
    _publish_profile(db, BOB, style_key="D")
    _out(db, CARLA, date(2026, 8, 21), date(2026, 8, 22))
    answer = team_builder.answer_for(
        db,
        _ask(
            required=("data privacy", "gaap"),
            optional=("not a real skill at all",),
            size=4,
            ends_on=TODAY + timedelta(days=7),
        ),
        today=TODAY,
        rationale_writer=_no_model,
    )
    slots = team_builder.rationale_slots(answer)
    kinds = {slot.slot_id.split(":", 1)[0] for slot in slots}
    known = team_builder.PERSON_SLOT_KINDS | team_builder.GROUP_SLOT_KINDS
    assert kinds - known == set(), (
        f"unclassified slot kind(s) {kinds - known}: add them to "
        "PERSON_SLOT_KINDS or GROUP_SLOT_KINDS in team_builder"
    )
    assert team_builder.PERSON_SLOT_KINDS & team_builder.GROUP_SLOT_KINDS == set()
    # And the two sets really do split the slots we just built.
    assert kinds & team_builder.PERSON_SLOT_KINDS
    assert kinds & team_builder.GROUP_SLOT_KINDS


def test_a_writer_that_explodes_still_produces_a_complete_answer(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")

    def _boom(slots, **_kw):
        raise RuntimeError("Vertex is down")

    answer = team_builder.answer_for(db, _ask(), today=TODAY, rationale_writer=_boom)
    assert answer.rationale
    assert all(line.from_model is False for line in answer.rationale)
    assert "has confirmed" in team_builder.render_report(answer)


# ═════════════════════════════════════════════════════════════════════════════
# 7. The chat tool fails closed without a principal
# ═════════════════════════════════════════════════════════════════════════════


def _tool(email: str):
    from rubik_core.tools.smart_teams_team_tools import TeamBuilderChatTools

    return TeamBuilderChatTools(user_email=email)


def test_the_chat_tool_refuses_when_the_identity_resolves_to_nobody(monkeypatch):
    """``adk_agent.agent`` builds a container with a placeholder ON the domain.

    ``example@acme.example`` passes ``authz.is_employee`` — it is on the
    Workspace domain — so the domain check alone would let a nonexistent person
    read colleagues' skills. The registry lookup is what refuses it, and the
    refusal must say nothing was searched.
    """
    monkeypatch.setattr(
        "rubik_core.auth.registry.UserRegistry.get_user",
        lambda self, email: None,
    )
    assert authz.is_employee("example@acme.example") is True, (
        "the premise of this test changed"
    )
    result = _tool("example@acme.example")._build_project_team(
        "data privacy", "", 0, "", "", "", ""
    )
    assert result["status"] == "error"
    assert "nothing was searched" in result["error_message"]
    assert "signed in" in result["error_message"]


def test_the_chat_tool_refuses_an_off_domain_identity(monkeypatch):
    def _boom(self, email):  # pragma: no cover — must not be reached
        raise AssertionError("the registry was queried for an off-domain identity")

    monkeypatch.setattr("rubik_core.auth.registry.UserRegistry.get_user", _boom)
    result = _tool("attacker@gmail.com")._build_project_team(
        "data privacy", "", 0, "", "", "", ""
    )
    assert result["status"] == "error"
    assert "the company's Workspace accounts" in result["error_message"]


def test_the_chat_tool_refuses_the_impersonation_pseudo_identity(monkeypatch):
    monkeypatch.setattr(
        "rubik_core.auth.registry.UserRegistry.get_user",
        lambda self, email: _profile(email),
    )
    result = _tool("impersonated_user@systems.local")._build_project_team(
        "data privacy", "", 0, "", "", "", ""
    )
    assert result["status"] == "error"


def test_the_chat_tool_refuses_somebody_narrowed_out_of_the_copilot(monkeypatch):
    """plan.md Decision #4: Team Builder rides on the copilot. A chat tool has
    no dependency to apply that, so it applies the predicate itself."""
    narrow_copilot(monkeypatch, COPILOT_DEPARTMENT)
    monkeypatch.setattr(
        "rubik_core.auth.registry.UserRegistry.get_user",
        lambda self, email: _profile(email, department=NARROWED_OUT_DEPARTMENT),
    )
    result = _tool(BOB)._build_project_team("data privacy", "", 0, "", "", "", "")
    assert result["status"] == "error"
    assert "copilot" in result["error_message"]
    assert "not say the search found nobody" in result["error_message"].replace(
        "Do NOT", "not"
    )


def test_the_chat_tool_takes_no_identity_parameter():
    """The model must not be able to say who it is acting as."""
    import inspect

    from rubik_core.tools.smart_teams_team_tools import TeamBuilderChatTools

    params = set(
        inspect.signature(TeamBuilderChatTools.build_project_team).parameters
    ) - {"self"}
    forbidden = {
        name
        for name in params
        if "email" in name or "user" in name or "behalf" in name or "as_" in name
    }
    assert not forbidden, (
        f"{forbidden} is in the tool signature, so it is in the JSON schema the "
        "model sees. Identity is a constructor field, never a tool parameter"
    )


def test_the_chat_tool_answers_for_a_real_copilot_user(monkeypatch, seeded, engine):
    """The happy path, with its own session factory so nothing touches a real DB."""
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _publish_profile(db, BOB)

    monkeypatch.setattr(
        "rubik_core.auth.registry.UserRegistry.get_user",
        lambda self, email: _profile(email),
    )
    monkeypatch.setattr(
        "rubik_core.database.db.SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    monkeypatch.setattr(team_builder, "write_rationale", _no_model)

    result = _tool(LEAD)._build_project_team(
        "data privacy, reg z audit", "", 0, "", "", "", "who can do the exam"
    )
    assert result["status"] == "success", result
    report = result["report"]
    assert "Bob Person" in report
    assert "does not track a skill called 'reg z audit'" in report
    assert team_contracts.NO_RANKING_NOTICE in report
    assert team_builder.SELECTION_NOTICE in report


def test_the_chat_tool_refuses_an_unreadable_date(monkeypatch):
    monkeypatch.setattr(
        "rubik_core.auth.registry.UserRegistry.get_user",
        lambda self, email: _profile(email),
    )
    result = _tool(LEAD)._build_project_team(
        "data privacy", "", 0, "next september", "", "", ""
    )
    assert result["status"] == "error"
    assert "YYYY-MM-DD" in result["error_message"]


def test_the_chat_tool_refuses_an_ask_with_no_skill(monkeypatch):
    monkeypatch.setattr(
        "rubik_core.auth.registry.UserRegistry.get_user",
        lambda self, email: _profile(email),
    )
    result = _tool(LEAD)._build_project_team("", "", 3, "", "", "", "give me a team")
    assert result["status"] == "error"
    assert "do not guess skills" in result["error_message"]


def test_the_chat_tool_is_registered_on_the_coordinator_and_not_confirmation_wrapped():
    """Built-but-unregistered is a known failure mode in this codebase.

    Also asserts it is registered BARE: the tool is read-only, and wrapping a
    read in ADK's confirmation pause trains users to click through prompts.
    """
    source = Path("rubik_core/agent_runtime/coordinator.py").read_text()
    assert "TeamBuilderChatTools" in source, "the container is never constructed"
    assert "teams_instance.build_project_team" in source, (
        "build_project_team is not in the coordinator's tool list, so chat can "
        "never call it"
    )
    assert not re.search(
        r"FunctionTool\(\s*teams_instance\.build_project_team", source
    ), "a read-only tool must not be wrapped in a confirmation pause"


def test_the_tool_is_not_registered_on_the_unprincipled_root_agent():
    """``adk_agent/agent.py`` runs as a placeholder address at module scope."""
    source = Path("rubik_core/adk_agent/agent.py").read_text()
    assert "TeamBuilderChatTools" not in source
    assert "build_project_team" not in source


# ═════════════════════════════════════════════════════════════════════════════
# 8. No raw score, no transcript quote, no per-person number
# ═════════════════════════════════════════════════════════════════════════════


def test_no_raw_score_or_interview_quote_reaches_the_report(seeded):
    """Raw scores and transcripts are subject + Systems only. Grep the output."""
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _suggest(db, CARLA, "financial_reporting")  # seeds the confidential meeting title
    _publish_profile(db, BOB)

    answer = team_builder.answer_for(
        db, _ask(), today=TODAY, rationale_writer=_no_model
    )
    report = team_builder.render_report(answer)
    for secret in (str(SECRET_INTENSITY), SECRET_QUOTE, SECRET_MEETING, "91.2"):
        assert secret not in report, f"the team report leaked {secret!r}"


def test_no_raw_score_reaches_the_api_response(seeded):
    """Asserted on the SERIALIZED response, not on the object.

    The leak was never the intent — it is what a serializer emits. So: grep the
    whole response body, and grep its KEYS for anything score-shaped.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _publish_profile(db, BOB)
    _as(LEAD)

    response = client.post(BASE, json={"skills": ["data privacy"]})
    assert response.status_code == 200, response.text
    body = json.dumps(response.json())
    for secret in (str(SECRET_INTENSITY), SECRET_QUOTE, SECRET_MEETING):
        assert secret not in body, f"the Team Builder API leaked {secret!r}"

    forbidden = re.compile(
        r"score|rank|rating|percentile|utilization|utilisation|workload|capacity"
        r"|suitability|\bfit\b",
        re.IGNORECASE,
    )
    for person in response.json()["group"]:
        offending = [key for key in person if forbidden.search(key)]
        assert not offending, (
            f"{offending} on a per-person payload entry. Team Builder is "
            "advisory: no per-person judgement, ranking or workload number"
        )


def test_a_candidate_carries_no_numeric_field_at_all(seeded):
    """The structural half of the same guarantee, on the dataclass."""
    import dataclasses

    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _publish_profile(db, BOB)

    candidate = team_builder.build_team(db, _ask(), today=TODAY).result.candidates[0]
    for field in dataclasses.fields(candidate):
        value = getattr(candidate, field.name)
        assert not isinstance(value, (int, float)) or isinstance(value, bool), (
            f"TeamCandidate.{field.name} holds a number ({value!r}). A number "
            "per person sorts people"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 9. The gate, the mount, and order that means nothing
# ═════════════════════════════════════════════════════════════════════════════


def test_the_router_is_copilot_gated_on_top_of_the_company_gate(seeded, monkeypatch):
    """plan.md Decision #4, and ``main.py``'s own note about this router."""
    narrow_copilot(monkeypatch, COPILOT_DEPARTMENT)
    db = seeded
    _declare(db, BOB, data_privacy="strong")

    _as(LEAD, department=NARROWED_OUT_DEPARTMENT)
    denied = client.post(BASE, json={"skills": ["data privacy"]})
    assert denied.status_code == 403, denied.text
    assert "not turned on for you" in denied.json()["detail"].lower()

    _as(LEAD, department=COPILOT_DEPARTMENT)
    allowed = client.post(BASE, json={"skills": ["data privacy"]})
    assert allowed.status_code == 200, allowed.text


def test_an_off_domain_identity_is_refused_before_the_copilot_gate(seeded):
    """Stacked, not swapped: an off-domain admin is still off-domain."""
    _as("attacker@gmail.com", is_admin=True)
    response = client.post(BASE, json={"skills": ["data privacy"]})
    assert response.status_code == 403
    assert "Workspace" in response.json()["detail"]


def test_the_endpoint_refuses_an_ask_with_no_skill(seeded):
    _as(LEAD)
    response = client.post(BASE, json={"skills": []})
    assert response.status_code == 400
    assert "at least one skill" in response.json()["detail"]


def test_the_roster_is_alphabetical_and_says_the_order_means_nothing(seeded):
    """Ordering is the last place a ranking can hide once the numbers are gone."""
    db = seeded
    _declare(db, DREW, data_privacy="expert")
    _declare(db, BOB, financial_reporting="familiar")

    answer = team_builder.answer_for(
        db,
        _ask(required=("data privacy", "gaap")),
        today=TODAY,
        rationale_writer=_no_model,
    )
    assert answer.result.emails == (BOB, DREW), (
        "the roster is not sorted by email — the contract raises on that, and "
        "an order chosen for any other reason is a ranking"
    )
    assert "the order means nothing" in team_builder.render_report(answer)


def test_the_alphabet_breaks_a_tie_and_the_answer_admits_it(seeded):
    """Two people, one skill, one needed. The answer must not read as a choice."""
    db = seeded
    _declare(db, BOB, data_privacy="familiar")
    _declare(db, CARLA, data_privacy="expert")

    answer = team_builder.build_team(db, _ask(), today=TODAY)
    assert answer.result.emails == (BOB,), (
        "the tie was broken by something other than the alphabet — 'expert' "
        "beating 'familiar' is a per-person ranking"
    )
    assert any("one combination that covers the ask" in note for note in answer.notes)


def test_the_skill_vocabulary_endpoint_returns_skills_and_not_people(seeded):
    _as(LEAD)
    response = client.get(f"{BASE}/skills", params={"query": "privacy"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["skills"], payload
    assert all(set(row) == {"skill", "name", "cluster"} for row in payload["skills"])
    assert "@acme.example" not in json.dumps(payload)


# ═════════════════════════════════════════════════════════════════════════════
# 10. A style dimension that is refused for some of the group has less to say
# ═════════════════════════════════════════════════════════════════════════════


def test_a_dimension_open_for_only_part_of_the_group_is_carried_honestly(seeded):
    """The pair guide's ten dimensions, generalised to N — and named honestly.

    Two people: one with a published, reportable DISC layer and one with nothing.
    A dimension DISC feeds is readable for half the group and must be reported as
    partial; a dimension nothing feeds is reported as having nothing to say. What
    neither may do is name who is missing, or read as "nothing to worry about
    here".
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    _declare(db, CARLA, financial_reporting="working")
    _publish_profile(db, BOB, style_key="D", cluster=None)

    answer = team_builder.answer_for(
        db,
        _ask(required=("data privacy", "gaap")),
        today=TODAY,
        rationale_writer=_no_model,
    )
    reads = {r.key: r for r in team_builder.dimension_readiness(answer)}
    assert set(reads) == set(comparison_contracts.DIMENSION_KEYS), (
        "the group's style vocabulary drifted from the pair guide's"
    )
    # feedback_exchange reads from DISC alone: open for Bob, closed for Carla.
    assert (
        reads["feedback_exchange"].open_for,
        reads["feedback_exchange"].of_people,
    ) == (1, 2)
    # motivator_alignment reads from motivators alone, which nobody has open.
    assert reads["motivator_alignment"].closed is True

    notes = team_builder.dimension_notes(tuple(reads.values()))
    assert len(notes) == 2, notes
    joined = " ".join(notes)
    for name in (BOB, CARLA, "Bob", "Carla"):
        assert name not in joined, (
            f"the dimension-coverage note named {name}. How complete the group's "
            "picture is is a group fact; per person it is a list of who has not "
            "finished their interview"
        )
    assert "nothing to say" in joined
    assert "missing information, not a finding" in joined
    report = team_builder.render_report(answer)
    for note in notes:
        assert note in report, "the report dropped a dimension-coverage note"


def test_a_group_nobody_has_published_for_is_silent_on_every_dimension(seeded):
    db = seeded
    _declare(db, BOB, data_privacy="strong")

    answer = team_builder.answer_for(
        db, _ask(), today=TODAY, rationale_writer=_no_model
    )
    reads = team_builder.dimension_readiness(answer)
    assert all(read.closed for read in reads)
    notes = team_builder.dimension_notes(reads)
    assert len(notes) == 1
    assert "nothing to say" in notes[0]


# ═════════════════════════════════════════════════════════════════════════════
# 11. The seven judgements the wave-2 verifier got into a rendered report
# ═════════════════════════════════════════════════════════════════════════════
#
# Each of these was measured passing every screen and printing under a heading
# that said none of it rated anybody. They are pinned twice over, because the
# fix is two things and either one alone would rot:
#
#   * the SCREEN refuses them (this is what any future model span has to clear);
#   * the PLUMBING never shows a model a fact about a person, so a screen miss
#     has nothing to miss.
#
# The screen test is the canary. The plumbing test is the guarantee.


# Each judgement is paired with the SLOT FACT it was measured against, which is
# the authored line about the person the judgement names — the covers slot for
# Alice and Carla, the sole-holder slot for the GAAP one. Pairing matters: a
# judgement about Carla scored against a fact naming only Alice fails containment
# on the word "Carla" for free, which would make the containment assertion below
# pass for the wrong reason and hide the real hole.
_ALICE_COVERS = "Alice Person has confirmed Data privacy (GDPR / CCPA) (expert)."
_CARLA_COVERS = "Carla Person has confirmed Financial reporting (strong)."
_CARLA_SOLE = (
    "Carla Person is the only person in this group who has confirmed GAAP "
    "reporting. If they are pulled off, nobody else here covers that part of "
    "the work."
)

MEASURED_JUDGEMENTS: tuple[tuple[str, str], ...] = (
    ("Alice Person is the senior voice in this group.", _ALICE_COVERS),
    ("Give the lead to Alice Person.", _ALICE_COVERS),
    ("Carla Person is likely to slow the group down.", _CARLA_COVERS),
    ("Carla Person is a risk on this project.", _CARLA_COVERS),
    ("Alice Person deserves the lead on this.", _ALICE_COVERS),
    ("Alice Person is the anchor of this group.", _ALICE_COVERS),
    ("Carla Person is the natural owner of the GAAP work.", _CARLA_SOLE),
)

MEASURED_JUDGEMENT_STRINGS = tuple(planted for planted, _ in MEASURED_JUDGEMENTS)


@pytest.mark.parametrize(("planted", "fact"), MEASURED_JUDGEMENTS)
def test_every_measured_judgement_is_refused_by_the_screen(planted, fact):
    """All seven, against the exact slot shape they were measured on.

    ``allowed_names`` carries both names, which is the PERMISSIVE setting — the
    live caller passes nothing — so a pass here would be a pass everywhere.
    """
    slot = ProseSlot(
        slot_id="covers:alice@acme.example",
        intent="what this person has confirmed",
        fact=fact,
        heading="Alice Person",
        body=fact,
        open_frameworks=frozenset(),
    )
    reason = team_prompts.screen_rationale(
        planted, slot, allowed_names=("Alice Person", "Carla Person")
    )
    assert reason is not None, (
        f"{planted!r} passed every screen. This is one of the seven strings the "
        "wave-2 verifier rendered into a real report"
    )


@pytest.mark.parametrize(("planted", "fact"), MEASURED_JUDGEMENTS)
def test_the_containment_floor_alone_would_still_refuse_it(planted, fact):
    """The regex is not load-bearing on its own: strict containment also refuses.

    This is the finding underneath the finding. In the lax mode the personal
    guide uses, every one of these scored ZERO novel content words against the
    fact about the person it names — they are built entirely out of stems that
    appear somewhere in the six instruments — so containment was doing nothing
    and the phrase list was the whole defence. Narrowing the allowed set to the
    fact plus connectives is what makes "contained" mean contained.
    """
    lax = novel_content_words(planted, fact)
    strict = novel_content_words(planted, fact, allow_authored_vocabulary=False)
    assert len(lax) == 0, (
        f"{planted!r} used to score zero novel words in lax mode against its own "
        f"slot fact and now scores {lax}; if that has changed, the measurement "
        "this fix was argued from has changed"
    )
    assert len(strict) > 0, f"strict containment found nothing novel in {planted!r}"


def test_no_measured_judgement_can_reach_a_rendered_report(seeded):
    """End to end, all seven at once, keyed to every slot in the answer.

    The writer is hostile: it returns the judgement for EVERY slot id, including
    the per-person ones it was never offered. What must come out is a complete
    report in which every per-person line is the authored one and none of the
    seven strings appears anywhere.
    """
    db = seeded
    _declare(db, "alice@acme.example", data_privacy="expert")
    _declare(db, CARLA, financial_reporting="strong")
    _publish_profile(db, "alice@acme.example", style_key="D")

    for planted in MEASURED_JUDGEMENT_STRINGS:

        def _hostile(slots, _planted=planted, **_kw):
            probe = team_builder.build_team(
                db, _ask(required=("data privacy", "gaap")), today=TODAY
            )
            return {
                slot.slot_id: _planted for slot in team_builder.rationale_slots(probe)
            }

        answer = team_builder.answer_for(
            db,
            _ask(required=("data privacy", "gaap")),
            today=TODAY,
            rationale_writer=_hostile,
        )
        report = team_builder.render_report(answer)
        assert planted not in report, planted
        assert answer.rationale, "the answer lost its content defending itself"
        for line in answer.rationale:
            assert line.text == line.authored, line.slot_id
            assert line.from_model is False, line.slot_id
        # The content is still there: this is a real answer, not a blanked one.
        assert "has confirmed" in report


def test_the_write_up_heading_claims_only_what_is_true(seeded):
    """The heading that used to sit above four per-person verdicts.

    It said "descriptive only — none of it rates anybody" while the lines under
    it were model-written. It now says who wrote them, which is the claim the
    code actually backs.
    """
    db = seeded
    _declare(db, BOB, data_privacy="strong")
    answer = team_builder.answer_for(
        db, _ask(), today=TODAY, rationale_writer=_no_model
    )
    report = team_builder.render_report(answer)
    assert "descriptive only — none of it rates anybody" not in report, (
        "the old heading is back. It asserts a guarantee the screens never gave"
    )
    heading = team_builder._WRITE_UP_HEADING
    assert heading in report
    assert "a model wrote none of these" in heading
    # And it is true of what is printed under it.
    body = report.split(heading, 1)[1]
    per_person = [
        line for line in answer.rationale if team_builder.names_a_person(line.slot_id)
    ]
    assert per_person
    for line in per_person:
        assert line.text in body
        assert line.from_model is False


# ═════════════════════════════════════════════════════════════════════════════
# 12. The ranking attack, pinned inert
# ═════════════════════════════════════════════════════════════════════════════

RANKING_ASKS = (
    "get me a team for the agency exam",
    "get me the best team",
    "who should lead",
    "rank these five by ability",
    "who is the strongest data privacy person we have",
)


def test_a_ranking_flavoured_ask_produces_a_byte_identical_report(seeded):
    """The instruction is not a lever. Selection is ask-order then the alphabet.

    Every one of these phrasings has to produce the SAME bytes, because nothing
    in the pipeline reads the requester's phrasing except the prompt — and with
    the model stubbed out there is nothing left for it to influence. A future
    change that let the raw ask reorder the roster, drop somebody, or add a
    sentence would show up here as a diff.
    """
    db = seeded
    _declare(db, "alice@acme.example", data_privacy="expert")
    _declare(db, BOB, data_privacy="working")
    _declare(db, CARLA, financial_reporting="strong")

    reports = {}
    for raw in RANKING_ASKS:
        answer = team_builder.answer_for(
            db,
            _ask(required=("data privacy", "gaap"), raw_ask=raw),
            today=TODAY,
            rationale_writer=_no_model,
        )
        reports[raw] = team_builder.render_report(answer)

    baseline = reports[RANKING_ASKS[0]]
    for raw, report in reports.items():
        assert report == baseline, (
            f"the phrasing {raw!r} changed the answer. The ask selects SKILLS; "
            "it must never select or order people"
        )
    # And the answer says so out loud, so a reader who asked for "the best" is
    # told what they actually got.
    assert team_builder.SELECTION_NOTICE in baseline
    assert "never as the best available group" in baseline


def test_an_unsatisfiable_ranking_ask_still_answers_honestly(seeded):
    """ "Rank these by ability" over an ask nobody covers is still an honest no.

    The failure mode is a refusal so defensive it stops answering: the request
    has a real gap, and the report has to name the gap rather than lecture about
    ranking.
    """
    db = seeded
    _declare(db, BOB, financial_reporting="working")
    answer = team_builder.answer_for(
        db,
        _ask(required=("data privacy",), raw_ask="rank these five by ability"),
        today=TODAY,
        rationale_writer=_no_model,
    )
    report = team_builder.render_report(answer)
    assert answer.result.emails == ()
    codes = {s.code for s in answer.result.shortfalls}
    assert team_contracts.SHORTFALL_NO_ONE_WITH_SKILL in codes, codes
    assert "Nobody has confirmed" in report
    assert "not the same as the company having nobody who can do it" in report
    assert "Bob" not in report, "an empty answer named somebody anyway"


# ═════════════════════════════════════════════════════════════════════════════
# 13. The shared no-ranking screen (finding 8)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "planted",
    [
        # Count-scoped superlatives: the collective-noun forms were caught, the
        # numeric ones were not, and a team document says the number.
        "the strongest of the three",
        "the most experienced of the three",
        "the weakest of the five",
        "the best of the 4",
        "the most senior among the three",
        # Leadership assignment. team_prompts caught these and the pair guide,
        # whose only screen over an assembled body is this one, did not.
        "the obvious choice to lead",
        "should lead the team",
        "Emery is the obvious choice to lead this.",
        "You should lead the plan and Emery should follow.",
        "Dakota needs to own the GAAP piece.",
        # The role as a NOUN, which needs no verb to be an assignment.
        "The team lead on this is Dakota.",
        "Dakota as team leader, with Emery on the reporting.",
        "Make Dakota the team lead.",
        # And the phrase team_contracts names as its own failure mode.
        "the weakest link",
    ],
)
def test_the_shared_screen_refuses_what_it_used_to_miss(planted):
    """One definition, so ST-7 and ST-9 inherit the same refusals.

    Asserted through BOTH entry points: a fix applied to only one of them is the
    asymmetry that made the pair guide the softer target for the same sentence
    about the same two people.
    """
    assert comparison_contracts.screen_no_ranking(planted) is not None, planted
    assert team_contracts.screen_team_prose(planted) is not None, planted


@pytest.mark.parametrize(
    "innocent",
    [
        "The best time to raise it is before the deadline.",
        "The strongest case for starting with the audit is the exam date.",
        "There are three of them and the work needs two.",
        "Cubie has no time-off record for them.",
        "Nobody here covers Stewardship.",
    ],
)
def test_the_shared_screen_still_allows_ordinary_advice(innocent):
    """A blunt screen is fine; a screen that refuses the copy it protects is not."""
    assert comparison_contracts.screen_no_ranking(innocent) is None, innocent


def test_no_authored_slot_fact_trips_the_shared_screen(seeded):
    """Our own copy has to survive our own screen, or every answer degrades.

    Built over a real answer carrying every slot kind: per-person coverage, a
    style, a sole holder, partial availability, a missing profile, the group's
    balance, an unreadable dimension, and two gaps.
    """
    db = seeded
    _declare(db, "alice@acme.example", data_privacy="expert")
    _declare(db, CARLA, financial_reporting="strong")
    _publish_profile(db, "alice@acme.example", style_key="D")
    _out(db, CARLA, date(2026, 8, 21), date(2026, 8, 22))
    answer = team_builder.answer_for(
        db,
        _ask(
            required=("data privacy", "gaap", "post-close qc"),
            optional=("not a real skill at all",),
            size=5,
            ends_on=TODAY + timedelta(days=7),
        ),
        today=TODAY,
        rationale_writer=_no_model,
    )
    slots = team_builder.rationale_slots(answer)
    assert len(slots) >= 8, f"only {len(slots)} slots — the fixture went thin"
    for slot in slots:
        assert comparison_contracts.screen_no_ranking(slot.fact) is None, slot.slot_id


# ═════════════════════════════════════════════════════════════════════════════
# 14. An offboarded employee is not proposable (finding 9)
# ═════════════════════════════════════════════════════════════════════════════

EX = "exemployee@acme.example"


def test_somebody_with_no_users_row_is_never_proposed(seeded):
    """Offboarding deletes the ``users`` row and leaves the skills rows alone.

    Without this filter the ex-employee kept being staffed onto live work, with
    their name reconstructed from the local part of their address and their
    availability rendered as "unknown". The whole ask was answered on them.
    """
    db = seeded
    _declare(db, EX, data_privacy="strong")  # confirmed, and no users row
    db.commit()

    pool = team_builder.proposable_people(db)
    assert EX not in pool, (
        "somebody with Smart Teams data and no users row is proposable. That is "
        "an offboarded employee still appearing in staffing answers"
    )

    answer = team_builder.answer_for(
        db, _ask(), today=TODAY, rationale_writer=_no_model
    )
    report = team_builder.render_report(answer)
    assert answer.result.emails == ()
    assert EX not in report
    assert "Exemployee" not in report, (
        "the display-name fallback rebuilt a plausible colleague out of the "
        "local part of an address with no account behind it"
    )
    # The honest empty answer, not a silent one.
    assert team_contracts.SHORTFALL_NO_ONE_WITH_SKILL in {
        s.code for s in answer.result.shortfalls
    }
    # Their skills row is untouched: we stopped proposing them, we did not
    # delete their history.
    assert store.confirmed_skills_for(db, [EX]).get(EX), (
        "the skills row was removed. Dropping somebody from the pool must not "
        "rewrite what they told Smart Teams"
    )


def test_a_published_guide_with_no_users_row_is_also_not_proposable(seeded):
    """The other half of the pool predicate. A published guide is not an account."""
    db = seeded
    _publish_profile(db, EX, style_key="I")
    db.commit()
    assert EX not in team_builder.proposable_people(db)


def test_the_offboarded_person_disappears_from_the_api_too(seeded):
    """Over HTTP, because the report and the serialized group are two renderers."""
    db = seeded
    _declare(db, EX, data_privacy="strong")
    _declare(db, BOB, data_privacy="working")
    db.commit()
    _as(LEAD, department=COPILOT_DEPARTMENT)
    response = client.post(BASE, json={"skills": ["data privacy"]})
    assert response.status_code == 200, response.text
    body = json.dumps(response.json())
    assert EX not in body
    assert "Exemployee" not in body
    assert BOB in body, "the filter took the live employee out too"


def test_a_users_row_with_a_blank_name_still_renders(seeded):
    """The local-part fallback is for a real account, and it must still work."""
    db = seeded
    db.add(
        User(
            email="nameless@acme.example",
            id="nameless",
            name="",
            department="Compliance",
            last_login=datetime(2026, 8, 1),
        )
    )
    _declare(db, "nameless@acme.example", data_privacy="strong")
    db.commit()
    assert team_builder.proposable_people(db)["nameless@acme.example"] == "Nameless"


# ═════════════════════════════════════════════════════════════════════════════
# 15. Consent is bound to the version that was approved (finding 5)
# ═════════════════════════════════════════════════════════════════════════════


def test_a_retake_nobody_approved_is_not_served(seeded):
    """The consent failure ``guide.published_guide`` exists to prevent.

    CARLA publishes v1 (style "I"), then retakes: v2 becomes current with a
    different style and its guide row is a draft CARLA has not approved. Team
    Builder must keep serving what CARLA published — never the newer, unapproved
    profile — because publication is consent to a VERSION, not a permanent flag
    on a person.
    """
    db = seeded
    _declare(db, CARLA, data_privacy="strong")
    v1_style = disc.WORK_STYLES["I"]
    v2_style = disc.WORK_STYLES["S"]
    _publish_profile(db, CARLA, style_key="I")
    _publish_profile(db, CARLA, style_key="S", publish=False, version=2)
    db.commit()

    # The state the fixture is claiming to be in.
    current = (
        db.query(SmartTeamsProfile)
        .filter(SmartTeamsProfile.user_email == CARLA)
        .filter(SmartTeamsProfile.is_current.is_(True))
        .one()
    )
    assert current.version == 2

    read = team_builder.published_layers(db, [CARLA])[CARLA]
    assert read.published is True, "the v1 consent was thrown away as well"
    assert read.work_style_key == "I", (
        f"served work style {read.work_style_key!r}. That is the post-retake "
        "profile nobody approved"
    )

    answer = team_builder.answer_for(
        db, _ask(), today=TODAY, rationale_writer=_no_model
    )
    report = team_builder.render_report(answer)
    assert v2_style.name not in report, (
        f"the unapproved v2 style {v2_style.name!r} reached the report"
    )
    assert v1_style.name in report

    _as(LEAD, department=COPILOT_DEPARTMENT)
    response = client.post(BASE, json={"skills": ["data privacy"]})
    assert response.status_code == 200, response.text
    body = json.dumps(response.json())
    assert '"work_style_key": "S"' not in body
    assert v2_style.name not in body


def test_approving_the_retake_switches_what_is_served(seeded):
    """The other half: once CARLA approves v2, v2 is what colleagues read.

    Without this, "read the approved version" would be indistinguishable from
    "pin the first version forever", which is a different bug with the same
    symptom.
    """
    db = seeded
    _declare(db, CARLA, data_privacy="strong")
    _publish_profile(db, CARLA, style_key="I")
    _publish_profile(db, CARLA, style_key="S", publish=False, version=2)
    db.commit()
    assert team_builder.published_layers(db, [CARLA])[CARLA].work_style_key == "I"

    v2 = guide.current_guide(db, CARLA, version=2)
    assert v2 is not None and not v2.published
    guide.approve(db, v2, approver_email=CARLA)
    db.commit()

    read = team_builder.published_layers(db, [CARLA])[CARLA]
    assert read.work_style_key == "S", (
        "approving the retake did not change what is served — consent is now "
        "pinned to the first version rather than bound to the approved one"
    )


def test_a_retake_before_anything_was_ever_published_reads_nothing(seeded):
    """No consent at all is still no consent, at any version."""
    db = seeded
    _declare(db, CARLA, data_privacy="strong")
    _publish_profile(db, CARLA, style_key="I", publish=False)
    _publish_profile(db, CARLA, style_key="S", publish=False, version=2)
    db.commit()
    read = team_builder.published_layers(db, [CARLA])[CARLA]
    assert read.published is False
    assert read.work_style_key is None
    assert read.clusters == frozenset()


def test_a_published_row_with_no_recorded_version_reads_nothing(seeded):
    """Fail closed on a row that cannot say what it was generated from.

    ``profile_versions`` is written by ``guide.ensure_guide_row``, so an empty
    one means a hand-edited or pre-contract row. Consent is on the record and
    there is nothing this module may safely read off it.
    """
    db = seeded
    _declare(db, CARLA, data_privacy="strong")
    _publish_profile(db, CARLA, style_key="I")
    document = guide.published_guide(db, CARLA)
    assert document is not None
    document.profile_versions = {}
    db.commit()

    read = team_builder.published_layers(db, [CARLA])[CARLA]
    assert read.published is True
    assert read.work_style_key is None, (
        "layers were read off a row that does not record which version was approved"
    )


def test_the_group_balance_counts_a_stale_subject_as_having_nothing_open(seeded):
    """A retake nobody approved must not be reported as a style Cubie can read.

    ``profiles_missing_for`` is the honesty valve. A subject whose only approved
    version has been superseded and whose approved profile row is unreadable has
    to be counted in it, or the group's balance is reported as more complete than
    it is.
    """
    db = seeded
    _declare(db, CARLA, data_privacy="strong")
    _publish_profile(db, CARLA, style_key="I")
    document = guide.published_guide(db, CARLA)
    document.profile_versions = {}
    db.commit()

    answer = team_builder.answer_for(
        db, _ask(), today=TODAY, rationale_writer=_no_model
    )
    assert answer.result.emails == (CARLA,)
    assert answer.result.balance.profiles_missing_for == 1
    assert answer.result.balance.styles_present == ()
    report = team_builder.render_report(answer)
    assert "nothing to say about how the group's styles fit together" in report


#: The rationale prompt's exact bytes when it was rebuilt from a concatenation
#: into one f-string. A deliberate wording change updates both numbers; an
#: accidental one (a lost newline, a doubled space) fails here first.
RATIONALE_SYSTEM_SHA256 = (
    "ca22d1caccefaa4bd66185b8d88975a02ab5d701c2791f57b03096abef5ae0a2"
)
RATIONALE_SYSTEM_CHARS = 2768


def test_the_rationale_prompt_is_byte_for_byte_pinned():
    prompt = team_prompts.RATIONALE_SYSTEM
    assert len(prompt) == RATIONALE_SYSTEM_CHARS
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        RATIONALE_SYSTEM_SHA256
    )
    assert prompt.startswith(f"You are {PRODUCT_NAME}, writing up a suggested")
    assert f"at most {team_prompts.MAX_LINE_WORDS} words" in prompt
