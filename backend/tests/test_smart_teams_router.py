"""ST-3 — the Work Style Interview endpoints.

Five things fail silently if they regress, and each has a test below that would
catch it:

1. **The mount is company-wide.**
   ``test_smart_teams_is_reachable_outside_the_copilot_gate`` calls the
   interview as an HR user, a department an admin may narrow out of the
   copilot. If anybody moves this router onto the copilot-gated ``core`` parent
   in ``backend/main.py``, the interview 403s for most of the company while the
   weekly nudge keeps asking them to finish it, and
   nothing else in the suite notices. ``test_smart_teams_access.py`` pins the
   predicate; this pins the mount.

2. **A person can only ever touch their own interview.** The subject comes from
   the authenticated principal, so there is no id in any own-data path to walk —
   the test proves it by having a second user call the same URLs and get their
   OWN empty state back rather than the first user's answers, and by having a
   non-Systems employee (an exec included) refused on the support read.

3. **Saving is repeatable.** A flaky network retries a PUT; two identical saves
   must leave one row with one copy of each answer and one entry in
   ``sections_completed``, not a duplicated bank.

4. **Completion enqueues exactly one job.** Scoring runs on cubie-worker because
   Cloud Run throttles the service's CPU after the response — so the test
   asserts the dispatch happened through ``submit_background_work`` AND that a
   retried completion dispatches nothing.

5. **Bad answers are refused loudly.** An unknown item id and an out-of-range
   value are 4xx, because the alternative (dropping what we do not recognise) is
   an interview that scores as if the person answered less than they did.

In-memory SQLite with a ``StaticPool`` and ``app.dependency_overrides``, per
``test_crm_sync_endpoints.py``. The GATE is never overridden — only
``get_current_user`` is — so ``require_smart_teams_access`` runs for real
against each fake profile, which is the point of the first test.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.tests._rosters import narrow_smart_teams_support
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base
from rubik_core.database.smart_teams import (
    SKILL_LEVELS,
    SmartTeamsInterview,
    SmartTeamsProfile,
    SmartTeamsSkill,
)
from rubik_core.identifiers import SMART_TEAMS_ASSISTANT_SPEAKER
from rubik_core.smart_teams import interview as iv
from rubik_core.smart_teams.instruments import disc

OWNER = "owner@acme.example"
OTHER = "hr@acme.example"
SYSADMIN = "sysadmin@acme.example"
CEO = "ceo@acme.example"

BASE = "/api/smart-teams"

client = TestClient(app)


def _profile(
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
        roles=["admin"] if is_admin else ["user"],  # is_admin is derived from roles
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at="2026-08-19T00:00:00",
        updated_at="2026-08-19T00:00:00",
    )


def _as(email: str, **kw) -> None:
    """Sign in as somebody. The GATE still runs — only identity is faked."""
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()

    app.dependency_overrides[get_db] = lambda: db
    _as(OWNER, department="Compliance")
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


# ── Fixture helpers ───────────────────────────────────────────────────────────

DISC_LIKERT = iv.SECTION_BY_KEY[iv.SECTION_DISC_LIKERT]
BIG_FIVE = iv.SECTION_BY_KEY[iv.SECTION_BIG_FIVE]
STRENGTHS = iv.SECTION_BY_KEY[iv.SECTION_STRENGTHS]
SKILLS = iv.SECTION_BY_KEY[iv.SECTION_SKILLS]


def _full_bank(section: iv.Section) -> dict:
    """Every item in a scale bank answered, in range."""
    if section.answer_kind == "choice_ab":
        return {item_id: "a" for item_id in section.item_ids}
    return {item_id: 3 for item_id in section.item_ids}


def _transcript(section: iv.Section) -> dict:
    return {
        section.transcript_key: [
            {
                "speaker": SMART_TEAMS_ASSISTANT_SPEAKER,
                "text": "Tell me about work you would repeat.",
            },
            {
                "speaker": "interviewee",
                "text": "Rebuilding the exam checklist so reviewers stopped guessing.",
                "prompt_id": section.prompt_ids[0] if section.prompt_ids else None,
            },
        ]
    }


def _answer_everything(email: str = OWNER) -> None:
    """Walk the whole catalog through the real endpoints, as a person would."""
    resp = client.post(f"{BASE}/interview/start")
    assert resp.status_code == 200, resp.text
    for section in iv.SECTIONS:
        if section.item_ids:
            body = {"answers": _full_bank(section), "complete": True}
        elif section.answer_kind == "transcript":
            body = {"answers": _transcript(section), "complete": True}
        elif section.answer_kind == "structured":
            body = {
                "answers": {section.meta_key: {"declined": True}},
                "complete": True,
            }
        else:  # declared skills — declaring none is a legitimate answer
            body = {"answers": {}, "complete": True}
        resp = client.put(f"{BASE}/interview/sections/{section.key}", json=body)
        assert resp.status_code == 200, (section.key, resp.text)


# ── 1. the mount is company-wide ──────────────────────────────────────────────


def test_smart_teams_is_reachable_outside_the_copilot_gate(db_session):
    """HR is the load-bearing department: outside the copilot, inside the company.

    A 403 here means the router was mounted under the copilot-gated ``core``
    parent, or the gate was swapped for ``require_full_app_access``. Either way
    most of the company loses the interview.
    """
    _as(OTHER, department="HR")
    resp = client.get(f"{BASE}/interview")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "not_started"

    started = client.post(f"{BASE}/interview/start")
    assert started.status_code == 200
    assert started.json()["created"] is True


def test_an_off_domain_identity_is_refused_by_the_gate(db_session):
    """The gate asserts the Workspace domain itself — local mock auth may not have."""
    _as("attacker@gmail.com")
    for method, path in (
        ("get", f"{BASE}/interview"),
        ("post", f"{BASE}/interview/start"),
        ("get", f"{BASE}/interview/status"),
    ):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, (path, resp.text)


def test_an_unauthenticated_call_is_refused(db_session):
    """No session, no interview. ``get_current_user`` is what raises here.

    Overridden to raise the real 401 the dependency raises, because the gate
    below it can only ever see a resolved user.
    """
    from fastapi import HTTPException

    def _no_session():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_current_user] = _no_session
    assert client.get(f"{BASE}/interview").status_code == 401
    assert client.post(f"{BASE}/interview/start").status_code == 401
    assert client.post(f"{BASE}/interview/complete").status_code == 401


# ── 2. start / resume ─────────────────────────────────────────────────────────


def test_start_is_idempotent_and_resumes_mid_interview(db_session):
    """Two starts, one interview — and the second lands where the first left off."""
    first = client.post(f"{BASE}/interview/start").json()
    assert first["created"] is True
    assert first["current_section"]["key"] == iv.SECTIONS[0].key
    assert first["progress"]["percent"] == 0

    # Answer half of the opening DISC bank, then come back.
    half = dict(list(_full_bank(DISC_LIKERT).items())[:12])
    saved = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}", json={"answers": half}
    ).json()
    assert saved["section_completed"] is False

    second = client.post(f"{BASE}/interview/start").json()
    assert second["created"] is False
    assert second["interview_id"] == first["interview_id"]
    # Mid-bank resume: still on the same section, pointed at the next unanswered
    # item, and the bar has moved because a half-answered bank is partial credit.
    assert second["current_section"]["key"] == DISC_LIKERT.key
    assert second["first_unanswered_item"] == DISC_LIKERT.item_ids[12]
    assert second["progress"]["percent"] > 0
    assert len(second["section_answers"]) == 12

    assert db_session.query(SmartTeamsInterview).count() == 1


def test_a_completed_section_moves_the_cursor_to_the_next_one(db_session):
    client.post(f"{BASE}/interview/start")
    state = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT)},
    ).json()
    assert state["section_completed"] is True
    assert state["sections_completed"] == [DISC_LIKERT.key]
    assert state["current_section"]["key"] == iv.SECTIONS[1].key
    assert state["ready_to_complete"] is False


def test_the_current_section_carries_the_items_to_render(db_session):
    """Item prose comes from the instrument, and keying never leaves it."""
    state = client.post(f"{BASE}/interview/start").json()
    content = state["current_section_payload"]
    assert content["answer_kind"] == DISC_LIKERT.answer_kind
    assert len(content["items"]) == len(DISC_LIKERT.item_ids)
    first = content["items"][0]
    assert first["item_id"] == DISC_LIKERT.item_ids[0]
    assert first["prompt"]
    # A respondent who can see which dimension an item scores is answering a
    # different instrument.
    assert "dimension" not in first and "keyed" not in first
    assert len(content["scale"]) == len(disc.LIKERT_ANCHORS)


def test_the_two_disc_banks_do_not_leak_into_each_other(db_session):
    """``disc_fc_`` shares a prefix with ``disc_``; routing is set membership."""
    client.post(f"{BASE}/interview/start")
    client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT)},
    )
    forced = iv.SECTION_BY_KEY[iv.SECTION_DISC_FORCED_CHOICE]
    state = client.put(
        f"{BASE}/interview/sections/{forced.key}",
        json={"answers": _full_bank(forced)},
    ).json()
    assert set(state["saved_keys"]) == set(forced.item_ids)

    likert = client.get(f"{BASE}/interview/sections/{DISC_LIKERT.key}").json()
    assert set(likert["answers"]) == set(DISC_LIKERT.item_ids)
    fc = client.get(f"{BASE}/interview/sections/{forced.key}").json()
    assert set(fc["answers"]) == set(forced.item_ids)
    # The prefix overlap is the whole point: neither read spilled into the other.
    assert not set(likert["answers"]) & set(fc["answers"])


# ── 3. save: validated, and safe to repeat ────────────────────────────────────


def test_an_unknown_item_id_is_refused(db_session):
    client.post(f"{BASE}/interview/start")
    resp = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": {"disc_not_a_real_item": 3}},
    )
    assert resp.status_code == 400, resp.text
    assert "isn't an answer" in resp.json()["detail"]
    # Nothing was written.
    row = db_session.query(SmartTeamsInterview).one()
    assert row.answers == {}


def test_an_item_from_another_section_is_refused(db_session):
    """A real id, the wrong section. This is how a client's off-by-one bank
    would otherwise be scored as if the person answered it."""
    client.post(f"{BASE}/interview/start")
    resp = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": {BIG_FIVE.item_ids[0]: 3}},
    )
    assert resp.status_code == 400
    assert BIG_FIVE.item_ids[0] in resp.json()["detail"]


def test_an_out_of_range_value_is_refused(db_session):
    client.post(f"{BASE}/interview/start")
    for bad in (9, 0, True, "3"):
        resp = client.put(
            f"{BASE}/interview/sections/{DISC_LIKERT.key}",
            json={"answers": {DISC_LIKERT.item_ids[0]: bad}},
        )
        assert resp.status_code == 422, (bad, resp.text)
    assert db_session.query(SmartTeamsInterview).one().answers == {}


def test_an_unknown_section_is_a_404(db_session):
    client.post(f"{BASE}/interview/start")
    assert client.get(f"{BASE}/interview/sections/nope").status_code == 404
    assert (
        client.put(f"{BASE}/interview/sections/nope", json={"answers": {}}).status_code
        == 404
    )


def test_saving_the_same_section_twice_is_a_no_op(db_session):
    """The flaky-network case: the second PUT must not duplicate anything."""
    client.post(f"{BASE}/interview/start")
    body = {"answers": _full_bank(DISC_LIKERT), "complete": True}
    first = client.put(f"{BASE}/interview/sections/{DISC_LIKERT.key}", json=body).json()
    second = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}", json=body
    ).json()

    assert (
        first["sections_completed"] == second["sections_completed"] == [DISC_LIKERT.key]
    )
    row = db_session.query(SmartTeamsInterview).one()
    assert len(row.answers) == len(DISC_LIKERT.item_ids)
    assert row.sections_completed == [DISC_LIKERT.key]
    assert db_session.query(SmartTeamsInterview).count() == 1


def test_a_transcript_save_replaces_rather_than_appends(db_session):
    """One key holds the WHOLE conversation, so a resend cannot double the turns."""
    client.post(f"{BASE}/interview/start")
    body = {"answers": _transcript(STRENGTHS), "complete": True}
    client.put(f"{BASE}/interview/sections/{STRENGTHS.key}", json=body)
    client.put(f"{BASE}/interview/sections/{STRENGTHS.key}", json=body)
    row = db_session.query(SmartTeamsInterview).one()
    assert len(row.answers[STRENGTHS.transcript_key]) == 2


def test_a_half_answered_bank_cannot_be_marked_complete(db_session):
    """``complete`` is a request, not an instruction — the row decides."""
    client.post(f"{BASE}/interview/start")
    half = dict(list(_full_bank(DISC_LIKERT).items())[:5])
    state = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": half, "complete": True},
    ).json()
    assert state["section_completed"] is False
    assert state["sections_completed"] == []
    assert state["current_section"]["key"] == DISC_LIKERT.key


def test_only_an_optional_section_may_be_skipped(db_session):
    client.post(f"{BASE}/interview/start")
    resp = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": {}, "skip": True},
    )
    assert resp.status_code == 400
    assert "can't be skipped" in resp.json()["detail"]

    probe = iv.SECTION_BY_KEY[iv.SECTION_MOTIVATORS_PROBE]
    state = client.put(
        f"{BASE}/interview/sections/{probe.key}", json={"answers": {}, "skip": True}
    ).json()
    assert state["skipped"] is True
    assert probe.key in state["sections_completed"]


def test_a_long_conversational_answer_is_clipped_and_the_clip_is_disclosed(db_session):
    """Clipped in the handler, never bounded by pydantic: a 422 body would echo
    what the person just typed about themselves straight back out."""
    from backend.routers import smart_teams as router_mod

    client.post(f"{BASE}/interview/start")
    long_text = "x" * (router_mod.MAX_TURN_CHARS + 500)
    state = client.put(
        f"{BASE}/interview/sections/{STRENGTHS.key}",
        json={
            "answers": {
                STRENGTHS.transcript_key: [
                    {"speaker": "interviewee", "text": long_text, "prompt_id": None}
                ]
            }
        },
    ).json()
    assert state["caveats"]
    row = db_session.query(SmartTeamsInterview).one()
    stored = row.answers[STRENGTHS.transcript_key][0]["text"]
    assert len(stored) == router_mod.MAX_TURN_CHARS


def test_a_declared_skill_outside_the_taxonomy_is_refused(db_session):
    """Enforced as soon as there IS a taxonomy — ST-8 seeds it."""
    db_session.add(
        SmartTeamsSkill(skill_key="data_privacy", name="Data privacy", is_active=True)
    )
    db_session.commit()

    client.post(f"{BASE}/interview/start")
    good = client.put(
        f"{BASE}/interview/sections/{SKILLS.key}",
        json={"answers": {"skill:data_privacy": "strong"}},
    )
    assert good.status_code == 200, good.text

    bad = client.put(
        f"{BASE}/interview/sections/{SKILLS.key}",
        json={"answers": {"skill:telepathy": "expert"}},
    )
    assert bad.status_code == 422
    assert "taxonomy" in bad.json()["detail"]


def test_saving_without_an_open_interview_is_a_409(db_session):
    resp = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT)},
    )
    assert resp.status_code == 409
    assert "Start one" in resp.json()["detail"]


# ── 4. own-interview-only ─────────────────────────────────────────────────────


def test_another_employee_never_sees_or_touches_this_interview(db_session):
    """There is no id to walk: every own-data endpoint reads the principal.

    So the second employee does not get a 403 on somebody else's row — they get
    their OWN empty state, which is the stronger property. What must never
    happen is the first person's answers coming back.
    """
    client.post(f"{BASE}/interview/start")
    client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT), "complete": True},
    )
    owner_id = db_session.query(SmartTeamsInterview).one().id

    _as(OTHER, department="HR")
    assert client.get(f"{BASE}/interview").json()["status"] == "not_started"
    assert client.get(f"{BASE}/interview/answers").json()["status"] == "not_started"
    section = client.get(f"{BASE}/interview/sections/{DISC_LIKERT.key}").json()
    assert section["answers"] == {}
    assert section["interview_id"] is None

    # Their own start creates a SECOND row, and the owner's is untouched.
    other_id = client.post(f"{BASE}/interview/start").json()["interview_id"]
    assert other_id != owner_id
    owner_row = db_session.query(SmartTeamsInterview).filter_by(id=owner_id).one()
    assert len(owner_row.answers) == len(DISC_LIKERT.item_ids)

    # And abandoning as the other person does not abandon the owner's.
    assert client.post(f"{BASE}/interview/abandon").json()["interview_id"] == other_id
    db_session.refresh(owner_row)
    assert owner_row.status == "in_progress"


def test_the_support_read_is_for_support_admins_only(db_session, monkeypatch):
    """Raw answers are the person's, plus the support admins. Not their
    manager's, not an exec's — ``is_exec`` bypasses gates all over the app and
    must not buy this. Support is handed to the Systems team's admins here, so
    an admin outside it is refused too."""
    narrow_smart_teams_support(monkeypatch, "Systems")
    client.post(f"{BASE}/interview/start")
    client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT), "complete": True},
    )

    _as(SYSADMIN, department="Systems", is_admin=True)
    allowed = client.get(f"{BASE}/support/interviews/{OWNER}")
    assert allowed.status_code == 200, allowed.text
    assert len(allowed.json()["answers"]) == len(DISC_LIKERT.item_ids)
    assert allowed.json()["subject_email"] == OWNER

    for email, kw in (
        (OTHER, {"department": "HR"}),
        (CEO, {"department": "Operations", "is_exec": True, "managed": ["*"]}),
        # An admin outside the support set is still refused.
        ("admin@acme.example", {"department": "Marketing", "is_admin": True}),
    ):
        _as(email, **kw)
        denied = client.get(f"{BASE}/support/interviews/{OWNER}")
        assert denied.status_code == 403, (email, denied.text)
        assert "private" in denied.json()["detail"]


def test_the_support_read_is_attributable_in_the_logs(db_session, caplog):
    """Reading somebody's 155 private answers leaves a record of WHO read them.

    This is the most sensitive of the five support reads and was the one that
    did not log, which was survivable only while nothing called it. The
    /smart-teams/support screen makes it a routine action, and a routine look at
    private data with no trace is how "who saw my answers?" stops having an
    answer.
    """
    client.post(f"{BASE}/interview/start")
    _as(SYSADMIN, department="Systems", is_admin=True)

    with caplog.at_level(logging.INFO):
        assert client.get(f"{BASE}/support/interviews/{OWNER}").status_code == 200

    assert OWNER in caplog.text
    assert SYSADMIN in caplog.text, "the reader is not named in the log line"


def test_the_support_read_folds_the_subject_address(db_session):
    """``users.email`` is stored verbatim from the Google token, so a support
    lookup typed in any casing has to resolve to the same person."""
    client.post(f"{BASE}/interview/start")
    _as(SYSADMIN, department="Systems", is_admin=True)
    resp = client.get(f"{BASE}/support/interviews/OWNER@ACME.example")
    assert resp.status_code == 200
    assert resp.json()["subject_email"] == OWNER


# ── 5. completion dispatches exactly one job ──────────────────────────────────


def test_completing_an_unfinished_interview_is_a_409(db_session):
    client.post(f"{BASE}/interview/start")
    client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT), "complete": True},
    )
    with patch("backend.jobs.launcher.submit_background_work") as submit:
        resp = client.post(f"{BASE}/interview/complete")
    assert resp.status_code == 409
    assert submit.call_count == 0
    assert db_session.query(SmartTeamsInterview).one().status == "in_progress"


def test_completion_enqueues_exactly_one_scoring_job(db_session):
    """The row is committed BEFORE the dispatch, and the flipped status is what
    makes a retried completion dispatch nothing."""
    from backend.routers import smart_teams as router_mod

    _answer_everything()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", "exec-1")
    ) as submit:
        first = client.post(f"{BASE}/interview/complete")
        second = client.post(f"{BASE}/interview/complete")

    assert first.status_code == 202, first.text
    assert first.json()["enqueued"] is True
    assert first.json()["scoring"]["status"] == "pending"
    assert first.json()["scoring"]["executor"] == "job"

    assert submit.call_count == 1
    kwargs = submit.call_args.kwargs
    assert kwargs["module"] == router_mod.SCORING_JOB_MODULE
    row = db_session.query(SmartTeamsInterview).one()
    assert kwargs["args"] == ["--interview-id", row.id]
    assert callable(kwargs["fallback"])

    # The retry is a plain read of the completed state.
    assert second.status_code == 202
    assert second.json()["enqueued"] is False
    assert row.status == "completed"
    assert row.completed_at is not None
    assert row.section_cursor is None


def test_the_optional_probe_is_recorded_as_skipped_at_completion(db_session):
    """Whether the motivators probe applies is a function of the SCORED result,
    which the request path does not have — so an untouched optional section is
    recorded as skipped instead of holding the person at a question nothing has
    decided to ask."""
    client.post(f"{BASE}/interview/start")
    probe = iv.SECTION_BY_KEY[iv.SECTION_MOTIVATORS_PROBE]
    for section in iv.SECTIONS:
        if section.key == probe.key:
            continue
        if section.item_ids:
            body = {"answers": _full_bank(section), "complete": True}
        elif section.answer_kind == "transcript":
            body = {"answers": _transcript(section), "complete": True}
        elif section.answer_kind == "structured":
            body = {"answers": {section.meta_key: {"declined": True}}, "complete": True}
        else:
            body = {"answers": {}, "complete": True}
        assert (
            client.put(
                f"{BASE}/interview/sections/{section.key}", json=body
            ).status_code
            == 200
        )

    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", None)
    ):
        resp = client.post(f"{BASE}/interview/complete")
    assert resp.status_code == 202, resp.text
    assert resp.json()["auto_skipped_sections"] == [probe.key]


# ── 6. status polling ─────────────────────────────────────────────────────────


def test_status_reports_pending_then_ready(db_session):
    _answer_everything()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", None)
    ):
        client.post(f"{BASE}/interview/complete")

    pending = client.get(f"{BASE}/interview/status").json()
    assert pending["status"] == "completed"
    assert pending["scoring"]["status"] == "pending"
    assert pending["profile"] is None

    # ST-4's job writes the profile version; the poll then reports it.
    row = db_session.query(SmartTeamsInterview).one()
    profile = SmartTeamsProfile(
        id="prof-1",
        user_email=OWNER,
        version=1,
        status="current",
        is_current=True,
        origin="interview",
        scored_at=datetime.utcnow(),
    )
    db_session.add(profile)
    row.profile_id = profile.id
    db_session.commit()

    ready = client.get(f"{BASE}/interview/status").json()
    assert ready["scoring"]["status"] == "ready"
    assert ready["profile"]["version"] == 1
    assert ready["profile"]["status"] == "current"


def test_status_reports_delayed_rather_than_claiming_a_failure(db_session):
    """There is no failure column on ``st_interviews``, so nothing is written —
    the age of the completion is reported instead."""
    from backend.routers import smart_teams as router_mod

    row = SmartTeamsInterview(
        id="iv-stale",
        user_email=OWNER,
        status="completed",
        sections_completed=[s.key for s in iv.SECTIONS],
        answers={},
        completed_at=datetime.utcnow()
        - timedelta(seconds=router_mod.SCORING_STALE_SECONDS + 60),
    )
    db_session.add(row)
    db_session.commit()

    payload = client.get(f"{BASE}/interview/status").json()
    assert payload["scoring"]["status"] == "delayed"
    assert "an admin" in payload["scoring"]["message"]
    db_session.refresh(row)
    assert row.status == "completed"  # untouched


def test_status_before_starting_is_a_200(db_session):
    payload = client.get(f"{BASE}/interview/status").json()
    assert payload["status"] == "not_started"
    assert payload["scoring"]["status"] == "not_started"


# ── 7. abandon ────────────────────────────────────────────────────────────────


def test_abandon_lets_a_person_start_over_and_keeps_the_old_row(db_session):
    first = client.post(f"{BASE}/interview/start").json()["interview_id"]
    client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT), "complete": True},
    )
    assert client.post(f"{BASE}/interview/abandon").json() == {
        "status": "abandoned",
        "interview_id": first,
    }
    # Repeating it is a no-op, not a 404.
    assert client.post(f"{BASE}/interview/abandon").json() == {"status": "none"}

    second = client.post(f"{BASE}/interview/start").json()
    assert second["created"] is True
    assert second["interview_id"] != first
    assert second["sections_completed"] == []
    # The abandoned row survives: the answers are the person's, and the
    # completion sweep needs to know they started.
    old = db_session.query(SmartTeamsInterview).filter_by(id=first).one()
    assert old.status == "abandoned"
    assert len(old.answers) == len(DISC_LIKERT.item_ids)


def test_a_completed_interview_is_not_reopened_by_start(db_session):
    """A retake is a new row, not a reopened one — reopening would edit the
    answers a scored profile cites as its provenance."""
    _answer_everything()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", None)
    ):
        client.post(f"{BASE}/interview/complete")

    state = client.post(f"{BASE}/interview/start").json()
    assert state["created"] is False
    assert state["status"] == "completed"
    # ...and start SAYS where the retake lives instead of doing one silently.
    assert state["can_retake"] is True
    assert db_session.query(SmartTeamsInterview).count() == 1


def test_a_raced_duplicate_open_interview_is_collapsed(db_session):
    """No DB uniqueness backs "one open interview per person" —
    ``ix_st_interviews_user_status`` is an index — so two requests that both
    miss on the read can both insert. The row holding the answers wins."""
    kept = SmartTeamsInterview(
        id="iv-kept",
        user_email=OWNER,
        status="in_progress",
        section_cursor=DISC_LIKERT.key,
        sections_completed=[],
        answers={DISC_LIKERT.item_ids[0]: 4},
        started_at=datetime.utcnow() - timedelta(minutes=5),
        last_activity_at=datetime.utcnow() - timedelta(minutes=5),
    )
    empty = SmartTeamsInterview(
        id="iv-empty",
        user_email=OWNER,
        status="in_progress",
        section_cursor=DISC_LIKERT.key,
        sections_completed=[],
        answers={},
    )
    db_session.add_all([kept, empty])
    db_session.commit()

    state = client.get(f"{BASE}/interview").json()
    assert state["interview_id"] == "iv-kept"
    db_session.refresh(empty)
    assert empty.status == "abandoned"


# ── 8. the row the ORM writes stays legal ─────────────────────────────────────


def test_the_stored_email_is_folded_whatever_the_token_said(db_session):
    """``ck_st_interview_email_lower`` is a CHECK constraint; a mixed-case write
    is an IntegrityError, and the person silently has no interview."""
    _as("Owner@ACME.example", department="Compliance")
    client.post(f"{BASE}/interview/start")
    row = db_session.query(SmartTeamsInterview).one()
    assert row.user_email == OWNER

    # And the folded read finds it again.
    assert client.get(f"{BASE}/interview").json()["interview_id"] == row.id


# ── 9. the notices ────────────────────────────────────────────────────────────
# The advisory-only promise and the privacy promise were built nowhere and
# rendered as empty strings, so the page looked finished while the sentence
# "never used for performance review or promotion" was simply absent from the
# product. A notice that can silently go missing is worth pinning.


def test_every_interview_entry_point_carries_both_notices(db_session):
    """Not started, in progress, and completed — all three open the page."""
    from backend.routers import smart_teams as router_mod

    def _check(payload, where):
        assert payload["advisory_notice"], where
        assert payload["privacy_notice"], where
        # Verbatim from the instrument. A paraphrase here would be a different
        # promise from the one every other Smart Teams surface makes.
        assert payload["advisory_notice"] == disc.ADVISORY_NOTICE, where
        assert "performance review" in payload["advisory_notice"], where
        assert payload["privacy_notice"] == router_mod.PRIVACY_NOTICE, where

    _check(client.get(f"{BASE}/interview").json(), "not_started")
    _check(client.post(f"{BASE}/interview/start").json(), "start")
    _check(client.get(f"{BASE}/interview").json(), "in_progress")

    _answer_everything()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", None)
    ):
        client.post(f"{BASE}/interview/complete")
    _check(client.get(f"{BASE}/interview").json(), "completed")
    _check(client.post(f"{BASE}/interview/retake").json(), "retake")


def test_the_resume_state_names_both_the_stored_cursor_and_the_resolved_one(db_session):
    """``section_cursor`` is what is on the row; ``resume_section_key`` is what
    ``resume_target`` makes of it. They differ the moment a save completes the
    section the cursor was on, and shipping only the raw value made the client
    re-implement the resolution."""
    state = client.post(f"{BASE}/interview/start").json()
    assert state["section_cursor"] == DISC_LIKERT.key
    assert state["resume_section_key"] == DISC_LIKERT.key

    saved = client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT), "complete": True},
    ).json()
    assert saved["resume_section_key"] == iv.SECTIONS[1].key


# ── 10. the section read the page actually consumes ───────────────────────────


def test_the_section_read_is_keyed_payload_with_progress_beside_it(db_session):
    """``payload`` (not ``content``), and ``progress`` alongside it — the client
    switches on ``payload.answer_kind`` and draws its header from ``progress``."""
    client.post(f"{BASE}/interview/start")
    body = client.get(f"{BASE}/interview/sections/{DISC_LIKERT.key}").json()
    assert "content" not in body
    assert body["payload"]["answer_kind"] == "likert"
    assert body["progress"]["sections_total"] == len(iv.SECTIONS)
    assert body["progress"]["percent"] == 0
    assert body["section"]["key"] == DISC_LIKERT.key


def test_every_section_payload_stamps_its_answer_kind(db_session):
    """One discriminator, present on all nine — including the three that used to
    return a placeholder with no renderable question in it."""
    client.post(f"{BASE}/interview/start")
    # The probe's questions are worded from the SCORED motivators bank, so give
    # it one to score before asking what it renders.
    client.put(
        f"{BASE}/interview/sections/{iv.SECTION_MOTIVATORS}",
        json={"answers": _full_bank(iv.SECTION_BY_KEY[iv.SECTION_MOTIVATORS])},
    )
    for section in iv.SECTIONS:
        body = client.get(f"{BASE}/interview/sections/{section.key}").json()
        payload = body["payload"]
        assert payload["answer_kind"] == section.answer_kind, section.key
        if section.answer_kind == "likert":
            assert payload["items"] and payload["scale"], section.key
        elif section.answer_kind == "choice_ab":
            assert payload["items"] and payload["instruction"], section.key
        elif section.answer_kind == "transcript":
            assert payload["available"] is True, section.key
            assert payload["conversation"]["pending_question"], section.key
            assert payload["prompts"], section.key
        elif section.answer_kind == "structured":
            assert payload["available"] is True, section.key
            assert payload["confirmation"]["question"], section.key
            assert payload["confirmation"]["options"], section.key
        else:
            # Declared skills: the catalog shape the manifest publishes.
            assert payload["levels"] == list(SKILL_LEVELS), section.key
            assert payload["skills"] == [], section.key


def test_the_skills_payload_lists_the_active_taxonomy(db_session):
    db_session.add_all(
        [
            SmartTeamsSkill(
                skill_key="data_privacy",
                name="Data privacy",
                cluster="compliance",
                is_active=True,
            ),
            SmartTeamsSkill(
                skill_key="retired", name="Retired", cluster="x", is_active=False
            ),
        ]
    )
    db_session.commit()
    client.post(f"{BASE}/interview/start")
    payload = client.get(f"{BASE}/interview/sections/{SKILLS.key}").json()["payload"]
    assert [s["skill_key"] for s in payload["skills"]] == ["data_privacy"]
    assert payload["skills"][0]["cluster"] == "compliance"


def test_the_motivators_probe_says_why_it_is_not_ready_yet(db_session):
    """Its questions are CHOSEN by ``motivators.probe_form`` from the scored
    result. Before the bank is done there is no honest wording, so the payload
    says so instead of rendering an empty question."""
    client.post(f"{BASE}/interview/start")
    payload = client.get(
        f"{BASE}/interview/sections/{iv.SECTION_MOTIVATORS_PROBE}"
    ).json()["payload"]
    assert payload["available"] is False
    assert payload["conversation"] is None
    assert "Work values" in payload["unavailable_reason"]

    turn = client.post(
        f"{BASE}/interview/sections/{iv.SECTION_MOTIVATORS_PROBE}/turn",
        json={"answer": "anything"},
    )
    assert turn.status_code == 409, turn.text
    assert "Work values" in turn.json()["detail"]


# ── 11. the conversational runtime, actually reachable ────────────────────────


def _advance_plan():
    """A planner that always moves on. No network, no probes."""
    from rubik_core.smart_teams import conversation as cv

    return patch.object(cv, "plan_turn", return_value=cv.TurnPlan(advance=True))


def test_a_strengths_turn_records_the_answer_and_asks_the_next_question(db_session):
    from rubik_core.smart_teams import conversation as cv

    client.post(f"{BASE}/interview/start")
    opening = client.get(f"{BASE}/interview/sections/{STRENGTHS.key}").json()["payload"]
    first_question = opening["conversation"]["pending_question"]
    assert first_question == cv.strengths_script().questions[0].text
    assert opening["conversation"]["questions_total"] == len(
        cv.strengths_script().questions
    )

    with _advance_plan():
        turn = client.post(
            f"{BASE}/interview/sections/{STRENGTHS.key}/turn",
            json={
                "answer": "Rebuilding the exam checklist so reviewers stopped guessing."
            },
        )
    assert turn.status_code == 200, turn.text
    body = turn.json()
    assert body["accepted"] is True
    convo = body["payload"]["conversation"]
    assert convo["question_index"] == 1
    assert convo["pending_question"] != first_question
    assert body["section_completed"] is False

    # Speaker attribution survives the round trip. ``validate_extraction``
    # checks every evidence quote against the interviewee's OWN turns, so a
    # transcript that lost this would let a guide print Cubie's question back at
    # somebody as their words.
    stored = (
        db_session.query(SmartTeamsInterview).one().answers[STRENGTHS.transcript_key]
    )
    assert [t["speaker"] for t in stored] == [
        SMART_TEAMS_ASSISTANT_SPEAKER,
        "interviewee",
        SMART_TEAMS_ASSISTANT_SPEAKER,
    ]
    assert stored[1]["text"].startswith("Rebuilding the exam checklist")
    assert stored[0]["prompt_id"] == cv.strengths_script().questions[0].question_id


def test_the_runtime_completes_the_strengths_section_when_the_script_runs_out(
    db_session,
):
    """The section completes when the RUNTIME says the script is exhausted — the
    client cannot know that, because whether a follow-up was offered is decided
    inside the request."""
    client.post(f"{BASE}/interview/start")
    with _advance_plan():
        # One turn per prompt, plus a couple spare: the point is that the
        # RUNTIME decides when the script is exhausted, so the loop must
        # outrun the script rather than match its length.
        from rubik_core.smart_teams import conversation as _cv

        for n in range(len(_cv.strengths_script().questions) + 3):
            body = client.post(
                f"{BASE}/interview/sections/{STRENGTHS.key}/turn",
                json={"answer": f"Story number {n}, with enough words to count."},
            ).json()
    assert body["payload"]["conversation"]["finished"] is True
    assert body["payload"]["conversation"]["closing_line"]
    assert body["section_completed"] is True
    assert STRENGTHS.key in body["sections_completed"]
    row = db_session.query(SmartTeamsInterview).one()
    assert row.sections_completed == [STRENGTHS.key]
    assert row.section_cursor == iv.SECTIONS[0].key  # cursor moved to what is left

    # One turn too many is reported, not refused: the conversation comes back
    # finished with nothing recorded.
    with _advance_plan():
        extra = client.post(
            f"{BASE}/interview/sections/{STRENGTHS.key}/turn", json={"answer": "more"}
        )
    assert extra.status_code == 200
    assert extra.json()["accepted"] is False
    db_session.refresh(row)
    # Two turns per prompt — Cubie asks, the person answers — so the transcript
    # length is derived from the script rather than pinned, which is what makes
    # a script-length change a one-line change instead of a test edit.
    assert len(row.answers[STRENGTHS.transcript_key]) == 2 * len(
        _cv.strengths_script().questions
    )


def test_a_probing_planner_asks_a_follow_up_before_moving_on(db_session):
    """The one thing the planner is allowed to choose: an authored follow-up."""
    from rubik_core.smart_teams import conversation as cv

    script = cv.strengths_script()
    probe = script.questions[0].probes[0]
    client.post(f"{BASE}/interview/start")
    with patch.object(
        cv,
        "plan_turn",
        return_value=cv.TurnPlan(advance=False, probe=probe, probe_index=0),
    ):
        body = client.post(
            f"{BASE}/interview/sections/{STRENGTHS.key}/turn",
            json={"answer": "It was fine."},
        ).json()
    convo = body["payload"]["conversation"]
    assert convo["question_index"] == 0  # still on the same authored question
    assert convo["pending_question"] == probe
    assert convo["probes_offered"] == 1


def test_a_long_turn_answer_is_clipped_and_disclosed(db_session):
    from backend.routers import smart_teams as router_mod

    client.post(f"{BASE}/interview/start")
    with _advance_plan():
        body = client.post(
            f"{BASE}/interview/sections/{STRENGTHS.key}/turn",
            json={"answer": "y" * (router_mod.MAX_TURN_CHARS + 200)},
        ).json()
    assert body["caveats"]
    stored = (
        db_session.query(SmartTeamsInterview).one().answers[STRENGTHS.transcript_key]
    )
    assert len(stored[1]["text"]) == router_mod.MAX_TURN_CHARS


def test_a_turn_on_a_scale_bank_is_refused(db_session):
    client.post(f"{BASE}/interview/start")
    resp = client.post(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}/turn", json={"answer": "hi"}
    )
    assert resp.status_code == 400
    assert "isn't a conversation" in resp.json()["detail"]


def test_a_turn_without_an_open_interview_is_a_409(db_session):
    resp = client.post(
        f"{BASE}/interview/sections/{STRENGTHS.key}/turn", json={"answer": "hi"}
    )
    assert resp.status_code == 409


def test_the_motivators_probe_runs_once_its_bank_is_scored(db_session):
    """The wording comes from ``probe_form`` on the SCORED result, computed here
    on the request. This is the section that used to render nothing."""
    client.post(f"{BASE}/interview/start")
    motivators_section = iv.SECTION_BY_KEY[iv.SECTION_MOTIVATORS]
    client.put(
        f"{BASE}/interview/sections/{motivators_section.key}",
        json={"answers": _full_bank(motivators_section), "complete": True},
    )
    payload = client.get(
        f"{BASE}/interview/sections/{iv.SECTION_MOTIVATORS_PROBE}"
    ).json()["payload"]
    assert payload["available"] is True
    assert payload["conversation"]["pending_question"]
    with _advance_plan():
        body = client.post(
            f"{BASE}/interview/sections/{iv.SECTION_MOTIVATORS_PROBE}/turn",
            json={"answer": "Autonomy, mostly."},
        ).json()
    assert body["accepted"] is True
    stored = (
        db_session.query(SmartTeamsInterview)
        .one()
        .answers[f"transcript:{iv.SECTION_MOTIVATORS_PROBE}"]
    )
    assert [t["speaker"] for t in stored] == [
        SMART_TEAMS_ASSISTANT_SPEAKER,
        "interviewee",
        SMART_TEAMS_ASSISTANT_SPEAKER,
    ]


# ── 12. the Enneagram confirmation, scored on the fly ─────────────────────────


def test_the_confirmation_step_is_built_from_the_bank_on_the_request(db_session):
    """The chicken-and-egg: the confirmation reads back an inferred type, the
    inference needs the bank scored, and real scoring runs after the interview.
    Resolution — score the bank here, on the request. No model call is involved."""
    from rubik_core.smart_teams.instruments import enneagram

    section = iv.SECTION_BY_KEY[iv.SECTION_ENNEAGRAM]
    client.post(f"{BASE}/interview/start")
    client.put(
        f"{BASE}/interview/sections/{section.key}",
        json={"answers": _full_bank(section), "complete": True},
    )
    payload = client.get(
        f"{BASE}/interview/sections/{iv.SECTION_ENNEAGRAM_CONFIRMATION}"
    ).json()["payload"]
    confirmation = payload["confirmation"]
    assert payload["available"] is True
    assert confirmation["required"] is True
    assert confirmation["answered_items"] == len(section.item_ids)
    assert enneagram.DECLINE_OPTION in confirmation["options"]
    assert confirmation["footnote"]
    assert len(payload["types"]) == 9


def test_an_empty_enneagram_bank_still_offers_the_choice_and_claims_nothing(db_session):
    """No answers is not an error and must never read as a finding: the
    instrument's own "not enough answers to suggest a type" sentence is the
    question, and the person still gets a say."""
    from rubik_core.smart_teams.instruments import enneagram

    client.post(f"{BASE}/interview/start")
    confirmation = client.get(
        f"{BASE}/interview/sections/{iv.SECTION_ENNEAGRAM_CONFIRMATION}"
    ).json()["payload"]["confirmation"]
    assert confirmation["required"] is True
    assert confirmation["answered_items"] == 0
    assert confirmation["candidates"] == []
    # PICK_FROM_ALL_OPTION, not OTHER_OPTION: with no candidates shown there is
    # nothing for "Neither" to refer to, so the escape hatch is worded for a
    # person who was never offered a pair.
    assert list(confirmation["options"]) == [
        enneagram.PICK_FROM_ALL_OPTION,
        enneagram.DECLINE_OPTION,
    ]
    assert "Not enough answers" in confirmation["question"]


def test_declining_the_enneagram_completes_the_section(db_session):
    from rubik_core.smart_teams.instruments import enneagram

    client.post(f"{BASE}/interview/start")
    resp = client.post(
        f"{BASE}/interview/sections/{iv.SECTION_ENNEAGRAM_CONFIRMATION}/turn",
        json={"option": enneagram.DECLINE_OPTION},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["section_completed"] is True
    row = db_session.query(SmartTeamsInterview).one()
    assert row.answers[f"meta:{iv.SECTION_ENNEAGRAM_CONFIRMATION}"] == {
        "type_number": None,
        "wing": None,
        "declined": True,
    }


def test_picking_a_type_by_name_is_stored_as_that_type(db_session):
    from rubik_core.smart_teams.instruments import enneagram

    client.post(f"{BASE}/interview/start")
    resp = client.post(
        f"{BASE}/interview/sections/{iv.SECTION_ENNEAGRAM_CONFIRMATION}/turn",
        json={
            "option": enneagram.PICK_FROM_ALL_OPTION,
            "type_number": 5,
            "wing": 4,
        },
    )
    assert resp.status_code == 200, resp.text
    row = db_session.query(SmartTeamsInterview).one()
    assert row.answers[f"meta:{iv.SECTION_ENNEAGRAM_CONFIRMATION}"] == {
        "type_number": 5,
        "wing": 4,
        "declined": False,
    }


def test_an_option_that_was_not_offered_is_refused(db_session):
    """The option maps back to a type BY POSITION, so an option the presentation
    did not offer would write down a type nobody picked."""
    client.post(f"{BASE}/interview/start")
    resp = client.post(
        f"{BASE}/interview/sections/{iv.SECTION_ENNEAGRAM_CONFIRMATION}/turn",
        json={"option": "The first one is more like me"},
    )
    assert resp.status_code == 400, resp.text
    assert "was not offered" in resp.json()["detail"]
    assert db_session.query(SmartTeamsInterview).one().answers == {}

    nothing = client.post(
        f"{BASE}/interview/sections/{iv.SECTION_ENNEAGRAM_CONFIRMATION}/turn", json={}
    )
    assert nothing.status_code == 400
    assert "Pick one" in nothing.json()["detail"]


# ── 13. retake: the version-2 machinery, reachable at last ────────────────────


def _score_now(db, interview_id: str):
    """Run ST-4's writer against the test session, with the LLM layer stubbed.

    The real dispatch goes to cubie-worker and opens its own session, which an
    in-memory SQLite test does not share — so the job is simulated by calling
    the same entry point the job calls.
    """
    from rubik_core.smart_teams import profile as st_profile
    from rubik_core.smart_teams.instruments import strengths

    return st_profile.score_and_store_interview(
        db,
        interview_id,
        extractor=lambda _t, **_k: strengths.StrengthsRead(themes=()),
    )


def test_retake_writes_a_second_interview_and_a_second_profile_version(db_session):
    """Without this path every employee had exactly one interview forever, and
    ``write_profile_version``'s ``version = max + 1`` / ``is_current = NULL``
    retirement / ``uq_st_profile_one_current`` were all unreachable."""
    _answer_everything()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", None)
    ):
        client.post(f"{BASE}/interview/complete")
    first_id = db_session.query(SmartTeamsInterview).one().id
    _score_now(db_session, first_id)

    retake = client.post(f"{BASE}/interview/retake")
    assert retake.status_code == 201, retake.text
    body = retake.json()
    assert body["created"] is True
    assert body["previous_interview_id"] == first_id
    assert body["interview_id"] != first_id
    assert body["sections_completed"] == []
    assert body["status"] == "in_progress"

    # The old row is untouched: the profile it produced cites those answers.
    old = db_session.query(SmartTeamsInterview).filter_by(id=first_id).one()
    assert old.status == "completed"
    assert old.profile_id

    _answer_everything()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", None)
    ):
        assert client.post(f"{BASE}/interview/complete").status_code == 202
    second_id = body["interview_id"]
    _score_now(db_session, second_id)

    assert db_session.query(SmartTeamsInterview).count() == 2
    versions = [
        (p.version, p.status, p.is_current)
        for p in db_session.query(SmartTeamsProfile)
        .order_by(SmartTeamsProfile.version)
        .all()
    ]
    assert versions == [(1, "superseded", None), (2, "current", True)]

    ready = client.get(f"{BASE}/interview/status").json()
    assert ready["scoring"]["status"] == "ready"
    assert ready["profile"]["version"] == 2


def test_retake_with_nothing_finished_is_a_409(db_session):
    resp = client.post(f"{BASE}/interview/retake")
    assert resp.status_code == 409
    assert "Start one" in resp.json()["detail"]
    assert db_session.query(SmartTeamsInterview).count() == 0

    client.post(f"{BASE}/interview/abandon")
    assert client.post(f"{BASE}/interview/retake").status_code == 409


def test_retake_during_an_interview_resumes_instead_of_restarting(db_session):
    """ "Retake" must never be the request that discards work in progress —
    that is ``/interview/abandon``."""
    started = client.post(f"{BASE}/interview/start").json()
    client.put(
        f"{BASE}/interview/sections/{DISC_LIKERT.key}",
        json={"answers": _full_bank(DISC_LIKERT), "complete": True},
    )
    resumed = client.post(f"{BASE}/interview/retake")
    assert resumed.status_code == 201
    assert resumed.json()["created"] is False
    assert resumed.json()["interview_id"] == started["interview_id"]
    assert resumed.json()["sections_completed"] == [DISC_LIKERT.key]
    assert db_session.query(SmartTeamsInterview).count() == 1


def test_a_retake_belongs_to_its_own_owner(db_session):
    """The subject is the principal here too: nobody can retake for anybody."""
    _answer_everything()
    with patch(
        "backend.jobs.launcher.submit_background_work", return_value=("job", None)
    ):
        client.post(f"{BASE}/interview/complete")

    _as(OTHER, department="HR")
    assert client.post(f"{BASE}/interview/retake").status_code == 409
    assert db_session.query(SmartTeamsInterview).count() == 1


# ── 14. the new surfaces keep every protection the old ones had ───────────────


def test_the_turn_and_retake_endpoints_sit_behind_the_same_gate(db_session):
    """A new endpoint on this router inherits ``require_smart_teams_access``
    because the dependency is declared on the APIRouter itself. Pinned, because
    the two paths added for the conversational runtime are the first writes on
    this router that are not a plain section save."""
    _as("attacker@gmail.com")
    for path, body in (
        (f"{BASE}/interview/retake", None),
        (f"{BASE}/interview/sections/{STRENGTHS.key}/turn", {"answer": "hi"}),
    ):
        resp = client.post(path, json=body)
        assert resp.status_code == 403, (path, resp.text)


def test_a_turn_can_only_ever_land_on_the_callers_own_transcript(db_session):
    """There is no subject in the body or the path — the principal is the only
    input — so a second employee posting turns writes their own row or none."""
    from rubik_core.smart_teams import conversation as cv

    client.post(f"{BASE}/interview/start")
    with patch.object(cv, "plan_turn", return_value=cv.TurnPlan(advance=True)):
        client.post(
            f"{BASE}/interview/sections/{STRENGTHS.key}/turn",
            json={"answer": "The checklist rebuild, start to finish."},
        )
    owner_row = db_session.query(SmartTeamsInterview).one()
    owner_turns = len(owner_row.answers[STRENGTHS.transcript_key])

    _as(OTHER, department="HR")
    # No interview of their own yet: refused, and nothing of the owner's moves.
    assert (
        client.post(
            f"{BASE}/interview/sections/{STRENGTHS.key}/turn", json={"answer": "mine"}
        ).status_code
        == 409
    )
    client.post(f"{BASE}/interview/start")
    with patch.object(cv, "plan_turn", return_value=cv.TurnPlan(advance=True)):
        client.post(
            f"{BASE}/interview/sections/{STRENGTHS.key}/turn",
            json={"answer": "A different story entirely."},
        )

    db_session.refresh(owner_row)
    assert len(owner_row.answers[STRENGTHS.transcript_key]) == owner_turns
    other_row = (
        db_session.query(SmartTeamsInterview)
        .filter(SmartTeamsInterview.user_email == OTHER)
        .one()
    )
    assert "different story" in other_row.answers[STRENGTHS.transcript_key][1]["text"]
    assert (
        "different story" not in owner_row.answers[STRENGTHS.transcript_key][1]["text"]
    )
