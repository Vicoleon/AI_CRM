"""The Smart Teams frontend↔backend contract, checked mechanically.

WHY THIS FILE EXISTS
--------------------
ST-3 shipped a frontend that called seven endpoints the router does not have.
Every answer-save went to ``PATCH /api/smart-teams/interview/answers`` — a path
that exists only for ``GET`` — so somebody could have answered 155 items over
half an hour with every single save failing while the page looked fine. Both
sides typechecked. Both sides passed their own tests. Nothing in either suite
looked across the boundary, so nothing failed.

That is the class of bug this file closes, and it does it the only way that
survives the next agent: by reading ``frontend/src/api/smartTeams.ts`` OFF DISK,
pulling every request path and method out of it, and asserting each one exists on
the real FastAPI app. Reading a frontend file from a backend test is an
established move in this repo — ``test_full_app_access.py`` and
``test_smart_teams_access.py`` both read ``AuthContext.tsx`` the same way, for
the same reason: a one-sided edit shows up as a failure nowhere else.

The reverse direction (routes no client calls) is a WARNING, not a failure. The
Systems support read has no screen yet and should not break the build for it.

Two things beyond path parity are pinned here, because they are the same failure
wearing a different hat — a field name that only one side believes in:

* **The names the page switches on.** ``payload`` (not ``content``),
  ``payload.answer_kind`` in every branch, ``levels``/``skills`` (not
  ``catalog``), ``scoring.status`` (not ``state``), ``minutes_remaining`` (there
  is no ``seconds_remaining``), ``current_section_payload``, ``saved_keys``.
* **The notices are actually there.** The advisory-use promise and the privacy
  notice are server-authored strings the page renders verbatim; a page that
  renders an empty notice makes a promise nobody can read. So they are asserted
  non-empty on a REAL response, not merely typed.

Everything asserted here runs: the app is built, the routes are enumerated from
``app.openapi()`` (walking ``app.routes`` is NOT valid on this FastAPI version —
it keeps lazy ``_IncludedRouter`` objects and reports zero smart-teams routes),
and the live reads go through ``TestClient`` against in-memory SQLite.

The interview is ADVISORY throughout — collaboration and staffing only, never
performance review, promotion, or compensation.
"""

from __future__ import annotations

import re
import uuid
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base
from rubik_core.database.smart_teams import SmartTeamsInterview
from rubik_core.identifiers import SMART_TEAMS_ASSISTANT_SPEAKER
from rubik_core.smart_teams import interview as iv

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_FILE = REPO_ROOT / "frontend/src/api/smartTeams.ts"

BASE = "/api/smart-teams"
OWNER = "owner@acme.example"

#: Every call site in the client is written as ``client.<verb><Type>('<path>')``
#: or with a template literal. The optional generic is matched with
#: ``[^()]*`` rather than ``[^>]*`` so a nested type argument
#: (``Record<string, X>``) does not end the match early.
CALL_RE = re.compile(
    r"client\.(get|post|put|patch|delete|head|options)"
    r"(?:<[^()]*?>)?"
    r"\(\s*[`'\"]([^`'\"]+)[`'\"]"
)

#: Paths that legitimately have no caller in the client today. Listed so the
#: warning below is informative rather than noise — NOT enforced in either
#: direction.
#:
#: The ST-5 and ST-6 blocks below are here because those two waves shipped a
#: backend with no frontend at all — `frontend/src/api/smartTeams.ts` covers the
#: interview and nothing else. They are listed rather than tolerated so that the
#: day a guides or uploads screen lands, the reviewer's job is to DELETE lines
#: from this set, and a line that will not delete is a route the screen missed.
KNOWN_UNCALLED = {
    (f"{BASE}/interview/answers", "GET"),
    (f"{BASE}/support/interviews/{{subject_email}}", "GET"),
    # ── ST-5 official-report upload (backend/routers/smart_teams_uploads.py) ──
    (f"{BASE}/uploads", "GET"),
    (f"{BASE}/uploads", "POST"),
    (f"{BASE}/uploads/{{upload_id}}", "GET"),
    (f"{BASE}/uploads/{{upload_id}}/confirm", "POST"),
    (f"{BASE}/uploads/{{upload_id}}/reject", "POST"),
    (f"{BASE}/support/uploads/{{subject_email}}", "GET"),
    # ── ST-6 personal guide (backend/routers/smart_teams_guides.py) ───────────
    (f"{BASE}/guides/me", "GET"),
    (f"{BASE}/guides/{{subject_email}}", "GET"),
    (f"{BASE}/guides/support/{{subject_email}}", "GET"),
    (f"{BASE}/guides/generate", "POST"),
    (f"{BASE}/guides/{{document_id}}/approve", "POST"),
    (f"{BASE}/guides/{{document_id}}/unpublish", "POST"),
    (f"{BASE}/guides/{{document_id}}/docx", "GET"),
    (f"{BASE}/guides/{{document_id}}/save-to-assets", "POST"),
    # ── ST-8 skills (backend/routers/smart_teams_skills.py) ───────────────────
    # Same situation as ST-5 and ST-6: the backend landed without a screen.
    # `/people/{subject_email}` additionally has no caller BY DESIGN yet — it is
    # the read ST-9's Team Builder is built on, and ST-9 is the wave that gets a
    # UI. Delete lines from here as those screens land; a line that will not
    # delete is a route the screen missed.
    (f"{BASE}/skills", "GET"),
    (f"{BASE}/skills/taxonomy", "GET"),
    (f"{BASE}/skills/declare", "POST"),
    (f"{BASE}/skills/refresh", "POST"),
    (f"{BASE}/skills/people/{{subject_email}}", "GET"),
    (f"{BASE}/skills/support/{{subject_email}}", "GET"),
    (f"{BASE}/skills/{{skill_key}}/confirm", "POST"),
    (f"{BASE}/skills/{{skill_key}}/deny", "POST"),
    # ── ST-9 Team Builder (backend/routers/smart_teams_teams.py) ──────────────
    # No caller yet BY DESIGN: ST-9 shipped the matcher, the copilot-gated API and
    # the chat tool (`build_project_team` on the coordinator, which is how the
    # feature is reachable today). The Team Builder PAGE is the piece that will
    # delete these two lines.
    (f"{BASE}/teams", "POST"),
    (f"{BASE}/teams/skills", "GET"),
    # ── ST-7 pair guide (backend/routers/smart_teams_comparison.py) ───────────
    # No caller yet BY DESIGN: ST-7 shipped the comparison engine, the pair
    # document and this API. The pair-guide page is the piece that will delete
    # the first two lines. The support read is a Systems endpoint and, like its
    # ST-5/ST-6/ST-8 siblings, may never get a screen at all.
    (f"{BASE}/comparison/pair/{{other_email}}", "GET"),
    (f"{BASE}/comparison/pair/{{other_email}}/generate", "POST"),
    (f"{BASE}/comparison/pair/support/{{reader_email}}/{{about_email}}", "GET"),
}


def _placeholders(path: str) -> str:
    """Collapse both parameter syntaxes to ``{}`` so they compare positionally.

    ``/interview/sections/${key}`` (TS template literal) and
    ``/interview/sections/{section_key}`` (FastAPI) are the same route; the
    parameter's NAME is the server's business.
    """
    return re.sub(r"\$\{[^}]*\}|\{[^}]*\}", "{}", path)


def _strip_comments(source: str) -> str:
    """Drop comments before scanning, so prose about a path is never read as one.

    This file's whole job is to be literal about what the client calls. A comment
    naming ``PATCH /interview/answers`` (there is one — it documents the bug this
    test exists to prevent) must not be mistaken for a call site.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    kept = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _client_calls() -> list[tuple[str, str, int]]:
    """Every (METHOD, path, line number) the client asks for. Read from disk."""
    assert CLIENT_FILE.is_file(), f"the Smart Teams API client moved: {CLIENT_FILE}"
    source = CLIENT_FILE.read_text(encoding="utf-8")
    body = _strip_comments(source)
    lines = body.splitlines()
    calls: list[tuple[str, str, int]] = []
    for match in CALL_RE.finditer(body):
        line_no = body[: match.start()].count("\n") + 1
        calls.append((match.group(1).upper(), match.group(2), line_no))
    assert lines, "the client file read as empty"
    return calls


def _app_routes() -> dict[str, set[str]]:
    """Every mounted path → the methods it serves, from the built app.

    ``app.openapi()`` and not ``app.routes``: this FastAPI version keeps lazy
    ``_IncludedRouter`` objects in ``app.routes``, so walking it finds zero
    smart-teams routes and would make this whole file pass vacuously.
    """
    paths = app.openapi()["paths"]
    return {path: {method.upper() for method in ops} for path, ops in paths.items()}


# ── The guard: every path the client calls exists ─────────────────────────────


def test_the_client_only_calls_routes_that_exist():
    """The one that would have caught the whole ST-3 divergence."""
    routes = _app_routes()
    normalized: dict[str, set[str]] = {}
    for path, methods in routes.items():
        normalized.setdefault(_placeholders(path), set()).update(methods)

    failures: list[str] = []
    for method, path, line_no in _client_calls():
        key = _placeholders(path)
        if key not in normalized:
            failures.append(
                f"{CLIENT_FILE.name}:{line_no} calls {method} {path} — "
                f"there is NO route at {key} on the app"
            )
        elif method not in normalized[key]:
            failures.append(
                f"{CLIENT_FILE.name}:{line_no} calls {method} {path} — that path "
                f"exists but serves only {sorted(normalized[key])}"
            )
    assert not failures, (
        "the frontend calls endpoints the backend does not have:\n"
        + "\n".join(failures)
    )


def test_the_scan_actually_found_the_calls():
    """A regex that stops matching would make the test above pass vacuously.

    So the shape of the client is pinned too: a healthy number of calls, all of
    them under ``/api/smart-teams``, and specifically the two whose method got
    the previous cut into trouble — the answer SAVE (a PUT on the section, not a
    PATCH on a GET-only path) and the conversational TURN.
    """
    calls = _client_calls()
    assert len(calls) >= 8, calls
    assert all(path.startswith(f"{BASE}/") for _, path, _ in calls), calls

    pairs = {(method, _placeholders(path)) for method, path, _ in calls}
    assert ("PUT", f"{BASE}/interview/sections/{{}}") in pairs, sorted(pairs)
    assert ("POST", f"{BASE}/interview/sections/{{}}/turn") in pairs, sorted(pairs)
    assert ("POST", f"{BASE}/interview/complete") in pairs, sorted(pairs)
    assert ("POST", f"{BASE}/interview/retake") in pairs, sorted(pairs)
    assert ("GET", f"{BASE}/interview/status") in pairs, sorted(pairs)
    # The path the previous client wrote every answer to. It is GET-only, and a
    # write to it must never come back.
    assert ("PATCH", f"{BASE}/interview/answers") not in pairs, sorted(pairs)


def test_routes_with_no_caller_are_reported_not_failed():
    """The reverse direction, as information.

    A support endpoint with no screen is a legitimate state and must not break
    the build. This warns so the gap is visible in test output, and asserts only
    that the ones we already know about are the ones showing up.
    """
    called = {(method, _placeholders(path)) for method, path, _ in _client_calls()}
    uncalled = sorted(
        (path, method)
        for path, methods in _app_routes().items()
        if path.startswith(BASE)
        for method in methods
        if (method, _placeholders(path)) not in called
    )
    if uncalled:
        warnings.warn(
            "smart-teams routes with no frontend caller: "
            + ", ".join(f"{method} {path}" for path, method in uncalled),
            stacklevel=1,
        )
    unexpected = [pair for pair in uncalled if pair not in KNOWN_UNCALLED]
    assert not unexpected, (
        "these smart-teams routes have no caller and are not on the known list — "
        "either wire them up or add them to KNOWN_UNCALLED with a reason: "
        f"{unexpected}"
    )


# ── Live reads: the field names the page switches on ─────────────────────────


def _profile(email: str) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department="Compliance",
        roles=["user"],
        is_exec=False,
        managed_departments=[],
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
    )


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
    # The GATE is never overridden — only identity — so
    # ``require_smart_teams_access`` runs for real.
    app.dependency_overrides[get_current_user] = lambda: _profile(OWNER)
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


@pytest.fixture
def api() -> TestClient:
    return TestClient(app)


def test_both_notices_arrive_non_empty_on_a_real_response(db_session, api):
    """The page renders these verbatim, so "present" is not enough — non-empty is.

    An empty advisory notice is a promise about performance review, promotion and
    compensation that nobody can read.
    """
    for response in (
        api.get(f"{BASE}/interview"),
        api.post(f"{BASE}/interview/start"),
    ):
        assert response.status_code == 200, response.text
        body = response.json()
        for field in ("advisory_notice", "privacy_notice"):
            assert isinstance(body.get(field), str), (field, sorted(body))
            assert body[field].strip(), field
        # Not a paraphrase: the advisory sentence is the instrument's own.
        assert "performance" in body["advisory_notice"].lower()


def test_the_resume_state_carries_the_cursor_and_payload_names_the_page_reads(
    db_session, api
):
    body = api.post(f"{BASE}/interview/start").json()
    for field in (
        "resume_section_key",
        "section_cursor",
        "current_section",
        "current_section_payload",
        "sections",
        "progress",
        "section_answers",
    ):
        assert field in body, (field, sorted(body))
    # `current_section_content` was the old name on this side while the section
    # read called it `payload`. One shape, one name.
    assert "current_section_content" not in body
    progress = body["progress"]
    assert "minutes_remaining" in progress
    assert "seconds_remaining" not in progress, (
        "the client renders minutes_remaining; a seconds_remaining here would "
        "mean two names for one number"
    )


@pytest.mark.parametrize(
    "section_key,answer_kind",
    [
        (iv.SECTION_DISC_LIKERT, "likert"),
        (iv.SECTION_DISC_FORCED_CHOICE, "choice_ab"),
        (iv.SECTION_BIG_FIVE, "likert"),
        (iv.SECTION_STRENGTHS, "transcript"),
        (iv.SECTION_MOTIVATORS, "likert"),
        (iv.SECTION_MOTIVATORS_PROBE, "transcript"),
        (iv.SECTION_ENNEAGRAM, "likert"),
        (iv.SECTION_ENNEAGRAM_CONFIRMATION, "structured"),
        (iv.SECTION_SKILLS, "skill_level"),
    ],
)
def test_every_section_read_ships_payload_with_a_discriminator(
    db_session, api, section_key, answer_kind
):
    """`payload`, not `content`, and `answer_kind` in EVERY branch.

    The client has one switch. A branch that omits the discriminator falls
    through to whatever renderer is last, which is how a placeholder section
    renders as an empty page.
    """
    api.post(f"{BASE}/interview/start")
    response = api.get(f"{BASE}/interview/sections/{section_key}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "payload" in body, sorted(body)
    assert "content" not in body
    assert body["payload"]["answer_kind"] == answer_kind
    assert body["payload"]["answer_kind"] == body["section"]["answer_kind"]
    for field in ("progress", "answers", "is_completed", "first_unanswered_item"):
        assert field in body, (field, sorted(body))


def test_the_big_five_bank_sends_its_stem(db_session, api):
    """IPIP items start mid-sentence ("Am the life of the party.").

    The leading "I…" travels once, in `stem`. A client that ignores it renders 50
    statements that read as if a word went missing.
    """
    api.post(f"{BASE}/interview/start")
    payload = api.get(f"{BASE}/interview/sections/{iv.SECTION_BIG_FIVE}").json()[
        "payload"
    ]
    assert payload["stem"].strip()
    assert payload["items"], "an empty bank would render as a blank section"
    # No keying, ever: a respondent who can see which item scores which trait is
    # answering a different instrument.
    assert set(payload["items"][0]) == {"item_id", "prompt"}


def test_the_skills_section_sends_levels_and_skills(db_session, api):
    """`levels` + `skills` — decided, and NOT `catalog`, which the client invented."""
    api.post(f"{BASE}/interview/start")
    payload = api.get(f"{BASE}/interview/sections/{iv.SECTION_SKILLS}").json()[
        "payload"
    ]
    assert "catalog" not in payload
    assert isinstance(payload["levels"], list) and payload["levels"]
    assert isinstance(payload["skills"], list)  # empty until ST-8 seeds the taxonomy


def test_the_conversational_sections_carry_a_runtime_not_a_placeholder(db_session, api):
    """The strengths segment opens on a real question, attributed to Cubie."""
    api.post(f"{BASE}/interview/start")
    payload = api.get(f"{BASE}/interview/sections/{iv.SECTION_STRENGTHS}").json()[
        "payload"
    ]
    assert payload["available"] is True
    assert payload["turn_endpoint"].endswith(f"/{iv.SECTION_STRENGTHS}/turn")
    conversation = payload["conversation"]
    assert conversation["transcript"], "an empty transcript renders as a blank section"
    first = conversation["transcript"][0]
    assert first["speaker"] == SMART_TEAMS_ASSISTANT_SPEAKER, first
    assert first["text"].strip()
    assert conversation["awaiting_answer"] is True
    assert conversation["finished"] is False
    assert conversation["questions_total"] >= 1


def test_the_probe_is_unavailable_with_a_reason_rather_than_an_empty_question(
    db_session, api
):
    """A layer that cannot be computed has to LOOK uncomputed.

    The motivators probe is worded from the scored motivators bank. Before that
    bank is finished there is no question to ask, and the page must render the
    server's reason — not an empty prompt that implies one.
    """
    api.post(f"{BASE}/interview/start")
    payload = api.get(
        f"{BASE}/interview/sections/{iv.SECTION_MOTIVATORS_PROBE}"
    ).json()["payload"]
    assert payload["available"] is False
    assert payload["conversation"] is None
    assert payload["unavailable_reason"].strip()


def test_the_enneagram_confirmation_is_asked_not_asserted(db_session, api):
    """With no answers on the bank: a question, no candidates, two options.

    Nothing here may read as a finding — the options are the instrument's own
    strings, and the client sends the chosen one back verbatim because the server
    maps it to a type BY POSITION.
    """
    api.post(f"{BASE}/interview/start")
    payload = api.get(
        f"{BASE}/interview/sections/{iv.SECTION_ENNEAGRAM_CONFIRMATION}"
    ).json()["payload"]
    assert payload["available"] is True
    confirmation = payload["confirmation"]
    assert confirmation["required"] is True
    assert confirmation["candidates"] == []
    assert len(confirmation["options"]) == 2, confirmation["options"]
    assert confirmation["question"].strip()
    assert confirmation["answered_items"] == 0
    # The pick-from-the-nine branch needs all nine, with a label each.
    assert len(payload["types"]) == 9
    assert set(payload["types"][0]) == {"number", "label"}


def test_a_save_echoes_saved_keys_and_progress(db_session, api):
    """The client retires exactly what landed, so both names are load-bearing."""
    api.post(f"{BASE}/interview/start")
    section = iv.SECTION_BY_KEY[iv.SECTION_DISC_LIKERT]
    item = section.item_ids[0]
    response = api.put(
        f"{BASE}/interview/sections/{iv.SECTION_DISC_LIKERT}",
        json={"answers": {item: 3}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved_keys"] == [item]
    assert body["progress"]["steps_done"] >= 1
    for field in ("section_completed", "skipped", "caveats", "ready_to_complete"):
        assert field in body, (field, sorted(body))


def test_the_scoring_vocabulary_is_status_and_has_no_running_state(db_session, api):
    """`scoring.status`, not `scoring.state`.

    A client polling for `running` or branching on `failed` polls for ever: the
    row has no failure column, so a run that never came back is `delayed` by age
    and nothing else may be expected.
    """
    row = SmartTeamsInterview(
        id=uuid.uuid4().hex,
        user_email=OWNER,
        status="completed",
        sections_completed=[s.key for s in iv.SECTIONS],
        answers={},
    )
    db_session.add(row)
    db_session.commit()

    body = api.get(f"{BASE}/interview/status").json()
    assert "state" not in body["scoring"], body["scoring"]
    assert body["scoring"]["status"] in {"not_started", "pending", "delayed", "ready"}
    assert "job_module" in body["scoring"]
    # None of these are fields the page may expect.
    for absent in ("run_id", "error", "updated_at"):
        assert absent not in body["scoring"], absent

    # And the same vocabulary on the interview read, which is what the landing
    # page actually branches on.
    completed = api.get(f"{BASE}/interview").json()
    assert completed["status"] == "completed"
    assert completed["can_retake"] is True
    assert completed["scoring"]["status"] in {"pending", "delayed", "ready"}
