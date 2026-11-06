"""Smart Teams wave 2 — the seams BETWEEN ST-7 (pair guide) and ST-9 (Team Builder).

Two agents built the comparison engine and the Team Builder in parallel, each in
its own modules with its own router, and each covered by its own test file. This
file covers only what neither of them could: the joins, and the two places where
wave 2 differs from wave 1 in kind rather than in degree.

What it is defending, and why each one is here rather than assumed
-----------------------------------------------------------------

1. **Six routers are mounted, and they DO NOT all carry the same gate.** Wave 1
   could assert one rule for every Smart Teams router ("company-wide, always").
   Wave 2 cannot: ``plan.md`` Decision #4 puts Compare and Team Builder behind
   the full-app copilot gate, ST-9 implements exactly that, and ST-7 deliberately did
   NOT — it stayed company-wide and enforces consent per document instead. Both
   placements are therefore asserted SEPARATELY and positively, against real
   responses from a real narrowed-out employee. A test that asserted one rule for
   all six would have to be wrong about one of them, and the failure mode is a
   403 in production with a green suite.

   This file does not decide whether ST-7's deviation from the written plan is
   correct — that is Blair's call. It pins what the code actually does, so the
   deviation is visible rather than silent, and so that "fixing" it later is a
   deliberate edit to an assertion instead of an unnoticed behaviour change.

2. **No two Smart Teams routes collide, now across all SIX routers.** Checked on
   the routers' own route objects and not on ``app.openapi()``: OpenAPI is keyed
   by path, so a genuine duplicate registration is silently MERGED there and the
   second handler becomes unreachable without anything failing. Two new prefixes
   landed beside four existing ones and three of the six declare a
   one-segment catch-all.

3. **The one new job is registered, importable, runnable, and really
   dispatched.** ``launch_module_execution`` validates that the Cloud Run *Job*
   exists, not the module inside it, so a dispatch naming a module that is not
   there returns 200, the endpoint returns 202, and nothing ever happens. That
   bug has already shipped in this feature once.

4. **The compounding consent rule, end to end.** A pair guide has TWO subjects
   behind ONE ``published`` flag, so that flag cannot be the rule — whoever
   pressed Generate would be consenting for their colleague. The whole path is
   walked here: two interviews, two scores, two approved personal guides, a pair
   guide, a read, then ONE subject unpublishes and the read must close. This is
   the assertion the feature is one bad release away from losing.

5. **A profile version bump invalidates a cached pair guide.** ST-7 keys the
   cache on ``(kind, subject_key, profile_version_key)`` built from BOTH
   subjects' current versions. A retake by EITHER person must miss that cache.
   Asserted for the person who did not press any button, because the interesting
   direction is the one where the reader's own profile is unchanged.

6. **Team Builder answers, and refuses, through the real endpoint.** One ask
   that resolves and one that cannot be satisfied — the second being a
   first-class answer with named shortfalls rather than an error, which is what
   stops "nobody has confirmed X" from being rendered as "the company has nobody
   who can do X".

7. **One ranking screen, not two.** ST-9 reuses
   ``comparison_contracts.screen_no_ranking`` rather than carrying a second
   copy, and the advisory promise is IN both documents rather than near them.

No network. Every Vertex seam in this path — the strengths extractor, the
personal guide's prose writer, the pair guide's prose writer and the team
rationale writer — is stubbed, and ``_forbid_llm`` additionally makes the real
JSON client factories raise, so a code path that reaches for Vertex fails
loudly instead of being skipped over in a mock.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.jobs import WORKER_JOBS
from backend.main import app
from backend.routers import (
    smart_teams as st_router,
)
from backend.routers import (
    smart_teams_comparison as comparison_router,
)
from backend.routers import (
    smart_teams_guides as guides_router,
)
from backend.routers import (
    smart_teams_skills as skills_router,
)
from backend.routers import (
    smart_teams_teams as teams_router,
)
from backend.routers import (
    smart_teams_uploads as uploads_router,
)
from backend.tests._rosters import narrow_copilot
from rubik_core.auth.registry import UserProfile
from rubik_core.database import db as db_module
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, User
from rubik_core.database.smart_teams import SmartTeamsDocument, SmartTeamsProfile
from rubik_core.identifiers import SMART_TEAMS_ASSISTANT_SPEAKER
from rubik_core.smart_teams import (
    comparison,
    comparison_contracts,
    guide,
    scoring,
    skill_taxonomy,
    team_builder,
    team_contracts,
)
from rubik_core.smart_teams import interview as iv
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
)

READER = "reader@acme.example"
ABOUT = "about@acme.example"
OUTSIDER = "outsider@acme.example"
SYSADMIN = "sysadmin@acme.example"
LEAD = "lead@acme.example"

BASE = "/api/smart-teams"
GUIDES = f"{BASE}/guides"
COMPARE = f"{BASE}/comparison"
TEAMS = f"{BASE}/teams"
SKILLS = f"{BASE}/skills"

#: A department deliberately OUTSIDE the copilot. If the copilot roster ever
#: grows to include HR this constant is what has to change, not the assertions —
#: the point is to sign in as somebody the copilot excludes.
NARROWED_OUT_DEPARTMENT = "HR"

#: A department deliberately INSIDE it, for the two copilot-gated routes.
COPILOT_DEPARTMENT = "Sales"

#: Two curated skills, resolved through the real taxonomy by the phrases a person
#: would type. ``resolve`` matches names and aliases, NOT raw keys, so the
#: phrases and the keys are both pinned here — a test that asked for
#: ``"data_privacy"`` would get ``None`` and silently prove nothing.
SKILL_PHRASE = "data privacy"
SKILL_KEY = "data_privacy"
UNHELD_PHRASE = "Financial reporting"
UNHELD_KEY = "financial_reporting"


# ── Identity and database ────────────────────────────────────────────────────


def _profile(
    email: str,
    *,
    department: str = NARROWED_OUT_DEPARTMENT,
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


def _as(email: str, **kw) -> None:
    """Sign in as somebody. Only the IDENTITY is faked — the routers' own gates
    still run, which is the whole point of asserting the mounts here."""
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The real app, one in-memory database, and a cubie-worker that really runs.

    ``SessionLocal`` is pointed at the same engine rather than at the request
    session: the comparison job opens its OWN session (it runs in a separate
    process), and a fixture that handed it the request's session would hide a
    job that forgets to commit.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    monkeypatch.setattr(db_module, "SessionLocal", Session, raising=False)
    from rubik_core.config import AppMode, settings

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL, raising=False)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path), raising=False)

    app.dependency_overrides[get_db] = lambda: db
    _as(READER)
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


# ── The fake dispatcher: a worker that refuses an unregistered module ─────────


class Dispatcher:
    """Records what a router dispatched, and runs it for real on demand.

    Deliberately NOT executed inside the request: the reason the pair guide puts
    its Gemini call on cubie-worker is that Cloud Run throttles the service CPU
    once the response returns, so the realistic ordering is "respond, then the
    job runs". :meth:`drain` is that second step.

    :meth:`__call__` asserts the module is in ``WORKER_JOBS`` *before* recording
    it. That assertion is the one this file exists for: a router naming a module
    nobody registered is a 202 that does nothing.
    """

    def __init__(self) -> None:
        self.pending: list[tuple[str, list[str]]] = []
        self.seen: list[tuple[str, tuple[str, ...]]] = []
        self.exits: list[tuple[str, tuple[str, ...], int]] = []

    def __call__(self, background_tasks, *, module, args, fallback, label, **kw):
        registered = {job.module for job in WORKER_JOBS.values()}
        assert module in registered, (
            f"{label} dispatched {module!r}, which is not in WORKER_JOBS. A "
            "Cloud Run Jobs launch validates the JOB, not the module inside it, "
            "so this returns 202 and then silently does nothing."
        )
        self.pending.append((module, list(args)))
        self.seen.append((module, tuple(args)))
        return "job", f"exec-{len(self.seen)}"

    def drain(self, db) -> list[int]:
        """Run every queued job, in order, and report their exit codes."""
        codes: list[int] = []
        db.commit()
        while self.pending:
            module, args = self.pending.pop(0)
            code = importlib.import_module(module).main(list(args))
            self.exits.append((module, tuple(args), code))
            codes.append(code)
        db.expire_all()
        return codes

    def modules(self) -> list[str]:
        return [module for module, _ in self.seen]


@pytest.fixture
def worker(monkeypatch) -> Dispatcher:
    dispatcher = Dispatcher()
    monkeypatch.setattr(
        "backend.jobs.launcher.submit_background_work", dispatcher, raising=True
    )
    return dispatcher


@pytest.fixture(autouse=True)
def _forbid_rag_network(monkeypatch):
    """AUTOUSE. Records corpus writes instead of performing them.

    This fixture exists because its absence did real damage. ``approve`` ->
    ``worker.drain`` really runs the guide job, whose ``ingest_published_guide``
    defaulted to the live ``RagManager`` with no mode guard — so this file dialled
    Vertex and wrote synthetic guides for its own test addresses into the LIVE
    companywide corpus of our production GCP project. Thirteen real client
    constructions per run. The suite still reported green, because the job
    catches the exception.

    Two things now stop that. First, the product code refuses to ingest unless
    it is **running inside Cloud Run** — ``running_in_managed_runtime()``, which
    reads K_SERVICE / K_REVISION / CLOUD_RUN_JOB. The discriminator is
    deliberately the RUNTIME and not ``APP_MODE``: APP_MODE resolves to GCP on a
    developer workstation here, so an APP_MODE fence protects nothing, and local
    runs already resolve to the deployed dev database. Second, this fixture is
    autouse, so no test in this file can reach the network even if that fence is
    later loosened. Note that ``raising=True`` matters — if either symbol is
    renamed, this fails loudly rather than silently stopping to protect anything.
    """

    class Recorder:
        def __init__(self) -> None:
            self.ingested: list[str] = []
            self.deleted: list[dict] = []
            self.delete_result = True

        def manager(self):
            recorder = self

            class _Manager:
                def update_document(self, slug, path):
                    recorder.ingested.append(slug)
                    return {"gcs_uri": f"gs://fake/{path.name}"}

            return _Manager()

        def delete(self, *args, **kwargs):
            self.deleted.append({"args": args, "kwargs": kwargs})
            return self.delete_result

    # Declare that we are simulating the managed runtime. ``ingest_published_guide``
    # refuses outside Cloud Run because an unfenced default once wrote synthetic
    # guides into the LIVE companywide corpus, so a test exercising that path has
    # to say it is standing in for the worker. Safe only BECAUSE RagManager is
    # patched two lines below — the marker opens the fence, the patch is what
    # guarantees no network. Do not set one without the other.
    monkeypatch.setenv("CLOUD_RUN_JOB", "cubie-worker-test")

    recorder = Recorder()
    from rubik_core.dam import vectorstore
    from rubik_core.rag import manager as rag_manager

    monkeypatch.setattr(
        rag_manager, "RagManager", lambda *a, **k: recorder.manager(), raising=True
    )
    monkeypatch.setattr(
        vectorstore, "delete_document_vectors", recorder.delete, raising=True
    )
    return recorder


@pytest.fixture(autouse=True)
def _forbid_llm(monkeypatch):
    """No test in this file may reach Vertex. Every seam is stubbed explicitly.

    Patching the client FACTORIES rather than trusting that each stub is in the
    right place: a path that reaches for a real model raises here with a name,
    instead of quietly making a network call in CI. ``comparison`` deliberately
    has no factory of its own — it calls ``guide._json_client`` — so covering
    ``guide`` covers the pair guide too.
    """

    def _refuse(*_a, **_kw):
        raise AssertionError(
            "a Smart Teams integration test tried to build a real LLM client"
        )

    monkeypatch.setattr(scoring, "_json_client", _refuse, raising=True)
    monkeypatch.setattr(guide, "_json_client", _refuse, raising=True)


_STORY = (
    "I like untangling a messy audit file until the story is obvious to "
    "everyone else in the room, and then writing the two sentences that make "
    "it obvious. "
)
_QUOTE = "untangling a messy audit file until the story is obvious"


def _strengths_read() -> strengths.StrengthsRead:
    return strengths.StrengthsRead(
        themes=tuple(
            strengths.ThemeRead(
                theme_id=theme_id,
                confidence=strengths.Confidence.HIGH,
                evidence_quote=_QUOTE,
                why="Named this pattern unprompted, twice.",
            )
            for theme_id in list(strengths.THEMES)[:2]
        )
    )


@pytest.fixture(autouse=True)
def _stub_extractor(monkeypatch):
    """The scoring engine's one Vertex call."""
    monkeypatch.setattr(
        scoring, "extract_strengths", lambda _t, **_kw: _strengths_read(), raising=True
    )


@pytest.fixture(autouse=True)
def _stub_personal_prose(monkeypatch):
    """ST-6's prose writer. Returns ``{}`` so ``build_guide`` falls back to its
    authored wording — a supported mode, and the right stub for a wiring test."""
    monkeypatch.setattr(guide, "write_prose", lambda slots, **_kw: {}, raising=True)


@pytest.fixture(autouse=True)
def _stub_pair_prose(monkeypatch):
    """ST-7's prose writer, resolved by module global inside the job.

    Returns ``{}`` — every slot absent — so the document falls back to authored
    copy. The counter proves the writer was CALLED, which is the wiring fact
    this file is entitled to assert; what the model would have said is
    ST-7's own test's business.
    """
    calls: list[int] = []

    def _writer(slots, **_kw):
        calls.append(len(slots))
        return {}

    monkeypatch.setattr(comparison, "write_pair_prose", _writer, raising=True)
    return calls


@pytest.fixture(autouse=True)
def _stub_team_rationale(monkeypatch):
    """ST-9's prose writer. The router injects nothing, so the module global is
    the seam the real request path uses."""
    calls: list[int] = []

    def _writer(slots, **_kw):
        calls.append(len(slots))
        return {}

    monkeypatch.setattr(team_builder, "write_rationale", _writer, raising=True)
    return calls


# ── Real interview answers, targeted so at least one layer opens its gate ─────

_BIG_FIVE_TARGETS = {
    big_five.Domain.EXTRAVERSION: 5,
    big_five.Domain.AGREEABLENESS: 4,
    big_five.Domain.CONSCIENTIOUSNESS: 5,
    big_five.Domain.EMOTIONAL_STABILITY: 2,
    big_five.Domain.OPENNESS: 4,
}
_MOTIVATOR_WEIGHTS = (5, 3, 4, 2, 1, 2)
_MOTIVATOR_POSITION = {name: i for i, name in enumerate(motivators.MOTIVATORS)}


def _keyed(value: int, reverse: bool) -> int:
    return (disc.LIKERT_MIN + disc.LIKERT_MAX) - value if reverse else value


def _targets(dominant: str = "D") -> dict[str, object]:
    """One value per item id across all four banks. NOT a straight line.

    A page of 3s is refused by every instrument (deliberately), and a pair guide
    built from two refused profiles is the all-refusals document, which would
    make the lifecycle assertions below assert nothing about a guide with
    content in it.

    ``dominant`` picks which DISC dimension is answered high, so the two
    subjects can be given genuinely different styles. Two identical profiles
    would still exercise the plumbing but would leave every friction candidate
    empty, and "no frictions" is exactly the state a broken assessor also
    produces.
    """
    out: dict[str, object] = {}
    for item in disc.LIKERT_ITEMS:
        out[item.item_id] = _keyed(
            5 if item.dimension == dominant else 2, item.keying == "reverse"
        )
    for index, pair in enumerate(disc.FORCED_CHOICE_PAIRS):
        if pair.option_a.dimension == dominant:
            out[pair.item_id] = "a"
        elif pair.option_b.dimension == dominant:
            out[pair.item_id] = "b"
        else:
            # Index-based, not hash-based: PYTHONHASHSEED varies per run and a
            # flaky fixture is worse than no fixture.
            out[pair.item_id] = "a" if index % 2 == 0 else "b"
    for item in big_five.ITEMS:
        out[item.item_id] = _keyed(
            _BIG_FIVE_TARGETS[item.domain], item.is_reverse_keyed
        )
    for item in motivators.LIKERT_ITEMS:
        out[item.item_id] = _keyed(
            _MOTIVATOR_WEIGHTS[_MOTIVATOR_POSITION[item.motivator]],
            item.keying == "reverse",
        )
    for item in enneagram.LIKERT_ITEMS:
        out[item.item_id] = 5 if item.type_number == 3 else 2
    return out


def _transcript_body(section: iv.Section) -> dict:
    prompt_id = section.prompt_ids[0] if section.prompt_ids else None
    return {
        section.transcript_key: [
            {
                "speaker": SMART_TEAMS_ASSISTANT_SPEAKER,
                "text": "What kind of work leaves you with more energy than it took?",
                "prompt_id": prompt_id,
            },
            {"speaker": "interviewee", "text": _STORY * 6, "prompt_id": prompt_id},
        ]
    }


def _answer_the_whole_interview(dominant: str = "D") -> None:
    """Walk the catalog through the real endpoints, as a person would."""
    targets = _targets(dominant)
    started = client.post(f"{BASE}/interview/start")
    assert started.status_code == 200, started.text

    for section in iv.SECTIONS:
        if section.item_ids:
            body = {
                "answers": {item: targets[item] for item in section.item_ids},
                "complete": True,
            }
        elif section.answer_kind == "transcript":
            body = {"answers": _transcript_body(section), "complete": True}
        elif section.answer_kind == "structured":
            body = {
                "answers": {section.meta_key: {"declined": True}},
                "complete": True,
            }
        else:  # declared skills — declaring none is a legitimate answer
            body = {"answers": {}, "complete": True}
        resp = client.put(f"{BASE}/interview/sections/{section.key}", json=body)
        assert resp.status_code == 200, (section.key, resp.text)


def _seed_user(db, email: str, name: str) -> None:
    db.add(User(email=email, id=email.split("@")[0], name=name))
    db.commit()


def _current_version(db, email: str) -> int | None:
    row = (
        db.query(SmartTeamsProfile)
        .filter(SmartTeamsProfile.user_email == email)
        .filter(SmartTeamsProfile.is_current.is_(True))
        .one_or_none()
    )
    return int(row.version) if row is not None else None


def _publish_personal_guide(db, worker: Dispatcher, email: str, dominant: str) -> str:
    """Interview -> score -> generate -> approve, for one person.

    Every hop goes through the real endpoints and the real cubie-worker entry
    points, because the thing under test in this file is whether those hops
    connect. Returns the approved document id.
    """
    _as(email)
    _answer_the_whole_interview(dominant)
    done = client.post(f"{BASE}/interview/complete")
    assert done.status_code == 202, done.text
    assert worker.drain(db) == [0], "the scoring job failed"
    assert _current_version(db, email) == 1, "no profile version was written"

    queued = client.post(f"{GUIDES}/generate")
    assert queued.status_code == 202, queued.text
    document_id = queued.json()["guide"]["id"]
    assert worker.drain(db) == [0], "the guide job failed"

    approved = client.post(f"{GUIDES}/{document_id}/approve")
    assert approved.status_code == 200, approved.text
    # The corpus write, and the exit code is CHECKED. This drain used to ignore
    # it, which is why the live-network write this file was doing stayed
    # invisible: the guide job catches the ingest exception and still exits 0 in
    # the aggregate, so the only way an unfenced or broken ingest shows up is by
    # asserting the code here and asserting the recorder below actually got it.
    assert worker.drain(db) == [0], "the guide RAG-ingest job failed"
    db.expire_all()
    assert db.get(SmartTeamsDocument, document_id).published is True
    return document_id


client = TestClient(app)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The mount, and the two DIFFERENT gates
# ═════════════════════════════════════════════════════════════════════════════

ST_ROUTERS = {
    "smart_teams": st_router.router,
    "smart_teams_uploads": uploads_router.router,
    "smart_teams_guides": guides_router.router,
    "smart_teams_skills": skills_router.router,
    "smart_teams_comparison": comparison_router.router,
    "smart_teams_teams": teams_router.router,
}

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _mounted_table() -> dict[str, set[str]]:
    """Every mounted Smart Teams path -> its methods, from the BUILT app.

    ``app.openapi()`` and not ``app.routes``: this FastAPI version keeps lazy
    ``_IncludedRouter`` objects on ``app.routes``, so walking that finds zero
    smart-teams routes and every assertion here would pass vacuously.
    """
    return {
        path: {method.upper() for method in ops}
        for path, ops in app.openapi()["paths"].items()
        if path.startswith(BASE)
    }


def test_all_six_smart_teams_routers_are_mounted():
    """Every route each of the six routers declares is reachable on the real app.

    Built from the routers' own declarations rather than from a hardcoded list,
    so a new endpoint on any of them is covered the day it lands and a router
    that gets un-mounted fails here rather than 404ing in a browser.
    """
    table = _mounted_table()
    assert table, "no smart-teams routes are mounted at all"

    for name, router in ST_ROUTERS.items():
        declared = {
            (f"/api{route.path}", method)
            for route in router.routes
            for method in getattr(route, "methods", set()) & _HTTP_METHODS
        }
        assert declared, f"{name} declares no routes"
        missing = sorted(
            (path, method)
            for path, method in declared
            if method not in table.get(path, set())
        )
        assert not missing, (
            f"{name} is not fully mounted in backend/main.py: {missing}. Add "
            f'app.include_router({name}.router, prefix="/api") on `app` — NOT '
            "on the copilot-gated `core` router."
        )


def test_the_two_wave2_routers_carry_the_gates_their_docstrings_claim():
    """Declared on the ``APIRouter`` itself, so no endpoint can be added without.

    The two wave-2 routers deliberately DIFFER, and this asserts the difference
    rather than a single rule:

    * ST-7 comparison — ``require_smart_teams_access`` only. Company-wide.
    * ST-9 teams — the same gate PLUS ``require_full_app_access``, stacked and
      not swapped, so the company boundary is still refused first.

    A swap rather than a stack would let an off-domain identity through whenever
    the copilot check happened to pass, which is why the order and the count are
    both pinned.
    """
    from backend.auth import require_full_app_access, require_smart_teams_access

    def _gates(router) -> list[object]:
        return [dep.dependency for dep in router.dependencies]

    assert _gates(comparison_router.router) == [require_smart_teams_access], (
        "ST-7's router gate changed. If the copilot gate was ADDED, the comment "
        "in backend/main.py and the router docstring both explain why it was "
        "deliberately absent — update those too, and the assertions below."
    )
    assert _gates(teams_router.router) == [
        require_smart_teams_access,
        require_full_app_access,
    ], (
        "ST-9's router must stack the copilot gate ON TOP of the company gate, in "
        "that order (plan.md Decision #4)."
    )


def test_someone_narrowed_out_of_the_copilot_reaches_comparison_not_team_builder(
    wired, monkeypatch
):
    """The gate difference, proved against real responses by one HR employee.

    Not two assertions about configuration — two assertions about what an
    employee outside the copilot departments actually gets. ST-7 must answer them
    (the pair guide is half about the reader, so a copilot mount would tell a
    narrowed-out colleague that a document describing them exists and is closed to
    them). ST-9 must refuse them (Team Builder is a staffing tool used ON other
    people, and plan.md put it behind the copilot).
    """
    narrow_copilot(monkeypatch, COPILOT_DEPARTMENT)  # an admin narrowed it
    _seed_user(wired, READER, "Reader Person")
    _seed_user(wired, ABOUT, "About Person")
    _as(READER, department=NARROWED_OUT_DEPARTMENT)

    pair = client.get(f"{COMPARE}/pair/{ABOUT}")
    assert pair.status_code in (200, 403), pair.text
    assert pair.status_code != 404, (
        "ST-7's router is not mounted for a narrowed-out employee. A 403 here would "
        "be the consent gate (legitimate); a 404 is the mount."
    )

    team = client.post(TEAMS, json={"skills": [SKILL_PHRASE]})
    assert team.status_code == 403, (
        f"Team Builder answered a narrowed-out employee ({team.status_code}). "
        "plan.md Decision #4 puts it behind require_full_app_access."
    )


def test_a_copilot_user_reaches_team_builder(wired):
    """The other half of the same rule: the copilot gate must not refuse everybody.

    Without this, a gate that rejected all callers would pass the assertion
    above and Team Builder would be unreachable in production.
    """
    _as(LEAD, department=COPILOT_DEPARTMENT)
    resp = client.post(TEAMS, json={"skills": [SKILL_PHRASE]})
    assert resp.status_code == 200, resp.text
    assert "state" in resp.json()


def test_an_off_domain_identity_is_refused_on_both_new_routers(wired):
    """The company gate is on each APIRouter, so mounting cannot have dropped it.

    Asserted on ST-7 as well as ST-9 precisely BECAUSE ST-7 has no copilot gate:
    "company-wide" must still mean "this company", and the only thing between
    an off-domain identity and a pair guide is this dependency.
    """
    _as("attacker@gmail.com")
    assert client.get(f"{COMPARE}/pair/{ABOUT}").status_code == 403
    assert client.post(TEAMS, json={"skills": [SKILL_PHRASE]}).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# 2. Path collisions, across all six
# ═════════════════════════════════════════════════════════════════════════════


def test_no_two_smart_teams_routes_share_a_path_and_method():
    """Checked on the ROUTERS, not on ``app.openapi()``.

    OpenAPI is a dict keyed by path, so two routers registering the same
    path+method silently merge there: the collision disappears from the document
    and the second handler becomes dead code. Walking the route objects is the
    only way to see it.
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for name, router in ST_ROUTERS.items():
        for route in router.routes:
            for method in getattr(route, "methods", set()) & _HTTP_METHODS:
                seen.setdefault((f"/api{route.path}", method), []).append(name)
    collisions = {key: owners for key, owners in seen.items() if len(owners) > 1}
    assert not collisions, (
        f"duplicate path+method across Smart Teams routers: {collisions}"
    )


def test_the_comparison_catch_all_cannot_shadow_the_support_route(wired):
    """``GET /comparison/pair/{other_email}`` is a one-segment catch-all.

    ``/pair/support/{reader}/{about}`` has three segments so it cannot be eaten,
    but the failure mode if it ever were is silent — the colleague-read handler
    answering a support call and applying the WRONG gate — so it is asserted
    against real responses rather than against declaration order.
    """
    _seed_user(wired, READER, "Reader Person")
    _seed_user(wired, ABOUT, "About Person")

    _as(SYSADMIN, department="Systems", is_admin=True)
    support = client.get(f"{COMPARE}/pair/support/{READER}/{ABOUT}")
    assert support.status_code == 200, support.text
    body = support.json()
    assert body["reader_email"] == READER and body["about_email"] == ABOUT
    assert "guides" in body, (
        "the support route was answered by the colleague-read handler"
    )


def test_the_teams_skills_route_is_not_eaten_by_the_teams_post(wired):
    """``POST /teams`` and ``GET /teams/skills`` are different endpoints.

    A regression here looks like the vocabulary endpoint returning a team, which
    a client would render as an empty roster rather than as an error.
    """
    _as(LEAD, department=COPILOT_DEPARTMENT)
    resp = client.get(f"{TEAMS}/skills")
    assert resp.status_code == 200, resp.text
    assert "state" not in resp.json(), (
        "GET /teams/skills was answered by the team-building handler"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 3. Job registration — the "202 and does nothing" guard
# ═════════════════════════════════════════════════════════════════════════════


def test_the_comparison_job_is_registered_importable_and_runnable():
    """Four separate facts, each of which has failed in this feature before.

    A Cloud Run Jobs launch validates that the JOB exists, not that the module
    inside the image does — so a dispatch naming a module that is absent,
    unimportable, or missing ``main`` succeeds at the API level and the person
    watches a spinner forever.
    """
    module = comparison_router.COMPARISON_JOB_MODULE
    entries = {name: job for name, job in WORKER_JOBS.items() if job.module == module}
    assert entries, (
        f"the comparison router dispatches {module!r} and nothing in WORKER_JOBS "
        "names it. Register it in backend/jobs/__init__.py."
    )
    assert len(entries) == 1, f"{module!r} is registered under {sorted(entries)}"
    name, job = next(iter(entries.items()))

    imported = importlib.import_module(module)
    assert callable(getattr(imported, "main", None)), f"{module} has no main()"

    # The registered example is the command an operator will actually paste, so
    # it has to PARSE. ``<st_documents.id>``-style placeholders are substituted
    # rather than dropped — dropping one leaves a dangling flag and would make
    # this assertion test argparse instead of the registration.
    parser = imported._build_parser()
    example = [
        "placeholder" if arg.startswith("<") and arg.endswith(">") else arg
        for arg in job.example_args
    ]
    assert parser.parse_args(example) is not None

    # And the file the registration claims dispatches it really names it, so the
    # "run it by hand" note cannot drift away from the code.
    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / (job.dispatched_from or "")
    assert source.is_file(), f"{name}: dispatched_from {job.dispatched_from!r} is gone"
    assert module in source.read_text(encoding="utf-8"), (
        f"{name}: {job.dispatched_from} does not name {module!r}"
    )


def test_a_bare_comparison_job_run_refuses_instead_of_guessing():
    """No default_args, and a bare run must exit non-zero.

    A guessed default is not a harmless convenience on a document-scoped job:
    acting on a document nobody named is worse than doing nothing.
    """
    module = comparison_router.COMPARISON_JOB_MODULE
    job = next(j for j in WORKER_JOBS.values() if j.module == module)
    assert job.default_args == (), f"{module} has default_args={job.default_args!r}"
    with pytest.raises(SystemExit):
        importlib.import_module(module)._build_parser().parse_args([])


def test_team_builder_dispatches_no_job_at_all():
    """ST-9 is synchronous, and that is a decision worth pinning.

    Team Builder answers inside the request: the match is a pure read and the
    one Vertex call is a short rationale. If a job is ever added, this assertion
    is the reminder that it must be registered in ``WORKER_JOBS`` too — the
    failure mode otherwise is the 202-that-does-nothing this file guards
    everywhere else.
    """
    source = Path(teams_router.__file__).read_text(encoding="utf-8")
    assert "submit_background_work" not in source, (
        "the teams router now dispatches background work; register the module "
        "in backend/jobs/__init__.py and extend this file's job assertions."
    )


# ═════════════════════════════════════════════════════════════════════════════
# 4. The compounding consent rule, end to end
# ═════════════════════════════════════════════════════════════════════════════


def test_a_pair_guide_needs_both_consents_and_closes_when_one_is_withdrawn(
    wired, worker, _stub_pair_prose
):
    """The whole wave-2 consent path, through the endpoints and the real job.

    ST-3 interview -> ST-4 scoring job -> ST-6 generate + approve (x2) -> ST-7
    generate -> ST-7 job -> the reader reads it -> the OTHER subject unpublishes
    their own personal guide -> the reader is denied.

    The last hop is the one that matters and the one neither agent could test:
    a pair guide carries two subjects behind ONE ``published`` flag, so the
    document's own flag cannot be the rule. Withdrawing consent on a PERSONAL
    guide has to close a DIFFERENT document, and nothing in ST-7 writes that
    flag.
    """
    db = wired
    _seed_user(db, READER, "Reader Person")
    _seed_user(db, ABOUT, "About Person")
    _seed_user(db, OUTSIDER, "Outsider Person")

    # ── both people publish their own guide first ────────────────────────────
    _publish_personal_guide(db, worker, READER, dominant="D")
    about_guide = _publish_personal_guide(db, worker, ABOUT, dominant="S")

    # ── the pair guide cannot be generated before that, and is now allowed ───
    _as(READER)
    queued = client.post(f"{COMPARE}/pair/{ABOUT}/generate")
    assert queued.status_code == 202, queued.text
    document_id = queued.json()["guide"]["id"]
    assert worker.modules()[-1] == comparison_router.COMPARISON_JOB_MODULE
    assert worker.seen[-1][1] == ("--document-id", document_id, "--generate")

    assert worker.drain(db) == [0], "the pair guide job failed"
    row = db.get(SmartTeamsDocument, document_id)
    assert row.status == "ready", row.error
    assert (row.content_md or "").strip(), "a ready pair guide with no body"
    assert _stub_pair_prose, "the pair prose writer was never called"

    # A pair guide is never approved, never published, never RAG-ingested.
    assert row.published is False, "a pair guide must never be published"
    assert row.rag_ingested is False, (
        "a pair guide must never enter the companywide corpus — it is about two "
        "people and readable only by them"
    )

    # The advisory promise is IN the document, and neither ranking screen fires.
    assert comparison_contracts.PAIR_ADVISORY_NOTICE in row.content_md
    rest = row.content_md.replace(comparison_contracts.PAIR_ADVISORY_NOTICE, "")
    assert comparison_contracts.screen_no_ranking(rest) is None, (
        "the generated pair guide contains cross-subject ranking language"
    )
    assert strengths._ADVANCEMENT_PROSE.search(rest) is None, (
        "the generated pair guide contains advancement language"
    )

    # ── the reader can read it ───────────────────────────────────────────────
    visible = client.get(f"{COMPARE}/pair/{ABOUT}")
    assert visible.status_code == 200, visible.text
    assert visible.json()["guide"]["id"] == document_id
    assert visible.json()["guide"]["content_md"].strip()
    assert visible.json()["stale"] is not True if "stale" in visible.json() else True

    # ── an unrelated employee never sees this document ───────────────────────
    # There is no endpoint that returns a pair guide about two OTHER people, so
    # the outsider's read resolves to their OWN (outsider, about) pair — which
    # does not exist. The assertion that matters is that the id above never
    # appears in it.
    _as(OUTSIDER)
    outside = client.get(f"{COMPARE}/pair/{ABOUT}")
    assert outside.status_code == 200, outside.text
    assert outside.json()["state"] == "none"
    assert document_id not in outside.text, (
        "a pair guide about two other people leaked to a third employee"
    )

    # ── the OTHER subject withdraws consent ─────────────────────────────────
    _as(ABOUT)
    pulled = client.post(f"{GUIDES}/{about_guide}/unpublish")
    assert pulled.status_code == 200, pulled.text
    worker.drain(db)

    # ── and the reader is denied, though nothing touched the pair row ────────
    _as(READER)
    denied = client.get(f"{COMPARE}/pair/{ABOUT}")
    assert denied.status_code == 403, (
        f"the pair guide is still readable ({denied.status_code}) after its "
        "other subject unpublished. One person's withdrawn consent must close "
        "every document that speaks about them."
    )
    db.expire_all()
    assert db.get(SmartTeamsDocument, document_id) is not None, (
        "the row should survive — consent closed the READ, not the record"
    )

    # A Systems admin still reaches the support view: it returns state, not prose.
    _as(SYSADMIN, department="Systems", is_admin=True)
    support = client.get(f"{COMPARE}/pair/support/{READER}/{ABOUT}")
    assert support.status_code == 200, support.text
    assert any(g["id"] == document_id for g in support.json()["guides"])
    assert "content_md" not in support.text, (
        "the support read must not ship the body of a pair guide"
    )


def test_generation_is_refused_before_the_other_person_has_published(wired, worker):
    """The gate runs on the WRITE too, not only on the read.

    Otherwise the document exists, the job writes prose about somebody who never
    consented, and the only thing standing between that text and a reader is the
    read gate. Consent has to be checked before the words are written.
    """
    db = wired
    _seed_user(db, READER, "Reader Person")
    _seed_user(db, ABOUT, "About Person")

    _publish_personal_guide(db, worker, READER, dominant="D")
    # ABOUT interviews and scores but NEVER approves a guide.
    _as(ABOUT)
    _answer_the_whole_interview("S")
    client.post(f"{BASE}/interview/complete")
    assert worker.drain(db) == [0]

    _as(READER)
    refused = client.post(f"{COMPARE}/pair/{ABOUT}/generate")
    assert refused.status_code in (403, 409), (
        f"a pair guide was generated ({refused.status_code}) about somebody who "
        "has not published their own guide"
    )
    assert (
        db.query(SmartTeamsDocument)
        .filter(SmartTeamsDocument.kind == comparison.KIND_PAIR)
        .count()
        == 0
    ), "a pair row was written despite the refusal"


# ═════════════════════════════════════════════════════════════════════════════
# 5. A profile version bump invalidates the cached pair guide
# ═════════════════════════════════════════════════════════════════════════════


def test_a_retake_by_either_subject_makes_the_cached_pair_guide_stale(
    wired, worker, _stub_pair_prose
):
    """ST-7 keys its cache on BOTH subjects' current profile versions.

    Asserted for the subject who did NOT press Generate, because that is the
    direction a single-subject cache would get wrong: the reader's own profile is
    untouched, so a key built from the reader alone still hits, and the guide
    keeps citing what their colleague used to say about themselves.
    """
    db = wired
    _seed_user(db, READER, "Reader Person")
    _seed_user(db, ABOUT, "About Person")

    _publish_personal_guide(db, worker, READER, dominant="D")
    _publish_personal_guide(db, worker, ABOUT, dominant="S")

    _as(READER)
    document_id = client.post(f"{COMPARE}/pair/{ABOUT}/generate").json()["guide"]["id"]
    assert worker.drain(db) == [0]

    fresh = client.get(f"{COMPARE}/pair/{ABOUT}")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["guide"]["stale"] is False
    versions_before = {READER: 1, ABOUT: 1}
    assert (
        comparison.current_pair_guide(db, READER, ABOUT, versions=versions_before)
        is not None
    ), "the cache does not find the guide it just wrote"

    # ── the OTHER subject retakes ────────────────────────────────────────────
    _as(ABOUT)
    retake = client.post(f"{BASE}/interview/retake")
    assert retake.status_code == 201, retake.text
    assert retake.json()["created"] is True
    _answer_the_whole_interview("I")
    client.post(f"{BASE}/interview/complete")
    assert worker.drain(db) == [0]
    assert _current_version(db, ABOUT) == 2, "the retake did not bump the version"

    # ── the cached pair guide must miss ──────────────────────────────────────
    db.expire_all()
    assert (
        comparison.current_pair_guide(db, READER, ABOUT, versions={READER: 1, ABOUT: 2})
        is None
    ), (
        "a pair guide was served from cache after one subject's profile "
        "advanced. profile_version_key must cover BOTH subjects."
    )
    # The old row stays findable by subject_key, so the UI can offer Regenerate.
    assert comparison.current_pair_guide(db, READER, ABOUT) is not None

    # ── and the reader is TOLD, rather than silently served stale prose ──────
    # The read may legitimately close instead: a retake abandons the previously
    # approved personal guide, which withdraws consent. Either answer is honest;
    # silently serving the old document as current is not.
    _as(READER)
    after = client.get(f"{COMPARE}/pair/{ABOUT}")
    assert after.status_code in (200, 403), after.text
    if after.status_code == 200 and after.json().get("guide"):
        assert after.json()["guide"]["stale"] is True, (
            "the pair guide cites an old profile version and does not say so"
        )
        assert after.json()["guide"]["id"] == document_id


# ═════════════════════════════════════════════════════════════════════════════
# 6. Team Builder: an ask that resolves, and one that cannot be satisfied
# ═════════════════════════════════════════════════════════════════════════════


def _declare(db, email: str, keys: list[str]) -> None:
    """Declare curated skills as ``email``, through the real ST-8 endpoint.

    Declared skills are ``confirmed`` by definition, which is what makes them
    the only thing Team Builder is allowed to match on.
    """
    _as(email)
    resp = client.post(f"{SKILLS}/declare", json={"skills": dict.fromkeys(keys)})
    assert resp.status_code == 200, resp.text


def test_a_team_ask_that_can_be_satisfied_names_the_people_who_confirmed_it(
    wired, worker, _stub_team_rationale
):
    """The ST-8 -> ST-9 join: confirmed skills become a proposable group.

    Also the advisory assertions that matter most on this surface, because this
    is the one document in the feature that is ABOUT several colleagues and
    written FOR somebody else: no per-person number anywhere in the payload, and
    the no-ranking screen clean over everything rendered.
    """
    db = wired
    _seed_user(db, READER, "Reader Person")
    _seed_user(db, ABOUT, "About Person")
    skill_taxonomy.seed_taxonomy(db)
    db.commit()

    _publish_personal_guide(db, worker, READER, dominant="D")
    _declare(db, READER, [SKILL_KEY])

    _as(LEAD, department=COPILOT_DEPARTMENT)
    resp = client.post(
        TEAMS, json={"skills": [SKILL_PHRASE], "ask": "an agency exam in October"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["unrecognised"] == [], f"{SKILL_PHRASE!r} did not resolve"
    assert [a["skill"] for a in body["asked_for"]] == [SKILL_KEY]
    assert READER in [member["email"] for member in body["group"]], (
        "the person who confirmed the requested skill was not proposed"
    )
    covered = {item["skill"]: item for item in body["coverage"]}
    assert covered[SKILL_KEY]["covered"] is True
    assert READER in covered[SKILL_KEY]["covered_by"]
    assert body["state"] == team_contracts.STATE_COMPLETE, body["shortfalls"]

    # The advisory promise, and no ranking anywhere in what a person will read.
    assert body["advisory_notice"] == team_contracts.TEAM_ADVISORY_NOTICE
    rendered = "\n".join(
        [body["report"], *body["write_up"], body["style_balance"]["note"]]
    )
    rendered = rendered.replace(team_contracts.TEAM_ADVISORY_NOTICE, "")
    assert comparison_contracts.screen_no_ranking(rendered) is None, (
        "the team write-up contains ranking or compatibility-score language"
    )

    # No per-person number reached the wire. The contract forbids the FIELD at
    # import; this asserts the projection did not compute one either.
    for member in body["group"]:
        assert not any(
            key in member
            for key in ("score", "rating", "rank", "fit", "match", "percent")
        ), f"a per-person metric reached the payload: {sorted(member)}"


def test_a_team_ask_nobody_can_satisfy_is_an_answer_with_named_shortfalls(
    wired, worker, _stub_team_rationale
):
    """An unsatisfiable ask is a first-class answer, not an error and not silence.

    The failure this guards is the confidently-wrong one: an empty roster
    rendered as a finished team, or a requested skill quietly dropped so the
    answer looks complete against fewer skills than were asked for.
    """
    db = wired
    _seed_user(db, READER, "Reader Person")
    skill_taxonomy.seed_taxonomy(db)
    db.commit()

    _publish_personal_guide(db, worker, READER, dominant="D")
    _declare(db, READER, [SKILL_KEY])  # deliberately NOT the skill asked for

    _as(LEAD, department=COPILOT_DEPARTMENT)
    resp = client.post(TEAMS, json={"skills": [UNHELD_PHRASE]})
    assert resp.status_code == 200, (
        "an ask nobody can satisfy must be an ANSWER, not an error"
    )
    body = resp.json()

    assert [a["skill"] for a in body["asked_for"]] == [UNHELD_KEY], (
        "the requested skill was dropped rather than reported"
    )
    assert body["state"] != team_contracts.STATE_COMPLETE
    codes = {item["code"] for item in body["shortfalls"]}
    assert team_contracts.SHORTFALL_NO_ONE_WITH_SKILL in codes, (
        f"no shortfall names the uncovered skill; got {codes}"
    )
    covered = {item["skill"]: item for item in body["coverage"]}
    assert covered[UNHELD_KEY]["covered"] is False
    assert covered[UNHELD_KEY]["covered_by"] == []
    assert READER not in [member["email"] for member in body["group"]], (
        "somebody who never confirmed the requested skill was proposed for it"
    )


def test_a_phrase_outside_the_taxonomy_is_reported_not_coerced(
    wired, worker, _stub_team_rationale
):
    """An unrecognised skill must surface, never land on a neighbour.

    Coercing "industry stuff" onto the nearest curated key attributes work to
    people who have never touched it, and the answer would look complete.
    """
    db = wired
    _seed_user(db, READER, "Reader Person")
    skill_taxonomy.seed_taxonomy(db)
    db.commit()
    _publish_personal_guide(db, worker, READER, dominant="D")
    _declare(db, READER, [SKILL_KEY])

    _as(LEAD, department=COPILOT_DEPARTMENT)
    resp = client.post(TEAMS, json={"skills": ["interpretive dance"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "interpretive dance" in body["unrecognised"]
    assert body["asked_for"] == [], "an unresolved phrase became a curated skill"
    codes = {item["code"] for item in body["shortfalls"]}
    assert team_contracts.SHORTFALL_SKILL_NOT_IN_TAXONOMY in codes


# ═════════════════════════════════════════════════════════════════════════════
# 7. One vocabulary, one ranking screen
# ═════════════════════════════════════════════════════════════════════════════


#: Ranking phrasings the shared screen MISSED when this file was written,
#: kept as their own list because they are the ones a regression
#: would come back on. Each is a sentence the advisory rule forbids in a shape
#: that walked through the original patterns: a superlative scoped by a COUNT
#: ("of the three") rather than by a collective noun ("of the group", which was
#: always caught), a leadership designation, or a named weak member.
#:
#: They are now caught. This was a ``strict=True`` xfail asking for exactly that
#: fix; ``screen_no_ranking`` grew the count-scoped alternative, the leadership
#: patterns and ``weakest link``, the xfail XPASSed, and the cases are folded
#: into the delegation test above so BOTH screens are asserted on them rather
#: than only ST-7's.
_ONCE_UNSCREENED_RANKING = (
    "Dakota is the strongest of the three.",
    "Dakota is the most experienced of the three.",
    "Dakota is the obvious choice to lead.",
    "Dakota should lead the team.",
    "Dakota is the weakest link.",
)


def test_st9_reuses_st7s_ranking_screen_rather_than_carrying_a_second_copy():
    """The recorded wave-1 failure was two agents inventing two shapes.

    ``team_contracts.screen_team_prose`` must DELEGATE to
    ``comparison_contracts.screen_no_ranking``. A second regex copy would drift:
    a pattern added to one screen would silently stop covering the other
    surface, and the surface it stopped covering is the one written about
    several colleagues at once.
    """
    for ranked in (
        "Dakota is the strongest member of the group.",
        "Dakota is more capable than Emery.",
        "Dakota is the best person for this.",
        "Their compatibility score is 82%.",
        *_ONCE_UNSCREENED_RANKING,
    ):
        assert comparison_contracts.screen_no_ranking(ranked) is not None, ranked
        assert team_contracts.screen_team_prose(ranked) is not None, (
            f"team_contracts.screen_team_prose accepted {ranked!r}, which "
            "comparison_contracts.screen_no_ranking refuses — the two screens "
            "have drifted apart"
        )


def test_there_is_exactly_one_dimension_vocabulary():
    """ST-7 owns the pairwise dimension list; ST-9 must not have grown a rival.

    ST-9 legitimately does not USE dimensions — its group findings are strength
    clusters and work styles, which are properties of a set of people rather
    than of a pair. What it must not do is define a second list of the same
    thing under another name, which is how two features start disagreeing about
    what "decision style" means.
    """
    assert comparison_contracts.DIMENSION_KEYS, "ST-7 defines no dimensions"
    for name in dir(team_contracts):
        if "DIMENSION" in name.upper():
            raise AssertionError(
                f"team_contracts.{name} looks like a second dimension "
                "vocabulary. Import comparison_contracts.DIMENSIONS instead."
            )


def test_the_team_builder_never_imports_the_pair_generator():
    """ST-9 reads profile layers directly; it does not consume a PairComparison.

    Worth pinning because ``plan.md`` sequences ST-9 after ST-7 and a reader
    reasonably expects a dependency. There is none, deliberately: a team finding
    is about a group, and building it out of N-choose-2 pair comparisons would
    be N-choose-2 Gemini calls to answer one chat turn — and every one of those
    documents is written in the second person to a specific reader.

    If that ever changes, it must go through ``comparison_contracts``' types
    rather than reaching into ``comparison``'s internals, which is what this
    assertion is really protecting.
    """
    source = Path(team_builder.__file__).read_text(encoding="utf-8")
    assert "import comparison\n" not in source
    assert "from rubik_core.smart_teams.comparison import" not in source
    # The shared CONTRACT is fine, and is in fact required — see the screen test.
    assert "comparison_contracts" in Path(team_contracts.__file__).read_text(
        encoding="utf-8"
    ), "team_contracts must reuse comparison_contracts' no-ranking screen"


def test_both_new_llm_workers_bill_to_the_smart_teams_cost_bucket():
    """A new AI feature that lands in "Unattributed" is a reporting defect.

    Asserted on the WORKER — the function that actually calls Vertex — because
    the recorded failure was a decorator on an entry point losing its tag at a
    thread hop.
    """
    from backend.routers.admin import _FEATURE_LABELS

    assert comparison.COST_FEATURE in _FEATURE_LABELS, (
        "the pair guide's spend would land in the Unattributed row"
    )
    assert team_builder.COST_FEATURE in _FEATURE_LABELS

    # Read off the SOURCE rather than the live attribute: the autouse fixtures in
    # this file replace both writers with stubs, so an introspection check here
    # would be inspecting the stub and would pass however the real code is
    # decorated. The decorator must sit on the function that calls Vertex.
    for module, worker_name in (
        (comparison, "write_pair_prose"),
        (team_builder, "write_rationale"),
    ):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert f"@attribute_to(COST_FEATURE)\ndef {worker_name}(" in source, (
            f"{module.__name__}.{worker_name} is the function that calls Vertex "
            "and it must carry @attribute_to(COST_FEATURE) directly. A "
            "decorator on an entry point loses the tag at a thread hop — that "
            "is the recorded cause of the 75% Unattributed row."
        )


# ═════════════════════════════════════════════════════════════════════════════
# 8. Exactly one path to the live companywide corpus, and it is fenced
# ═════════════════════════════════════════════════════════════════════════════


def test_the_only_corpus_write_is_the_personal_guide_and_it_is_recorded_not_dialled(
    wired, worker, _forbid_rag_network
):
    """The runtime half of the fence: what actually reached a corpus this run.

    The static test below proves there is one call site. This one proves what
    that call site DID: exactly one write, to the companywide slug, and it went
    into the recorder rather than to Vertex. Both halves are needed — a second
    unfenced call site is one failure mode, and a fenced call site that silently
    stopped ingesting anything at all is the opposite one, and it would look like
    a pass everywhere else in this file.

    It also closes the gap that made the original pollution invisible. The guide
    job catches the ingest exception, so before this assertion existed the only
    evidence of a write was a log line nobody read.
    """
    db = wired
    _seed_user(db, READER, "Reader Person")
    _publish_personal_guide(db, worker, READER, dominant="D")

    assert _forbid_rag_network.ingested == [guide.GUIDE_RAG_SLUG], (
        "the personal-guide ingest did not land in the recorder exactly once: "
        f"{_forbid_rag_network.ingested!r}. Either the write went somewhere else "
        "(check the fixture's monkeypatch target) or it stopped happening."
    )

    # A pair guide is generated and never ingested — no second corpus write.
    _seed_user(db, ABOUT, "About Person")
    _publish_personal_guide(db, worker, ABOUT, dominant="S")
    before = list(_forbid_rag_network.ingested)
    _as(READER)
    queued = client.post(f"{COMPARE}/pair/{ABOUT}/generate")
    assert queued.status_code == 202, queued.text
    worker.drain(db)
    assert _forbid_rag_network.ingested == before, (
        "generating a pair guide wrote to a corpus. A pair guide is about two "
        "people and readable only by them; the companywide corpus is readable "
        f"by every employee in Chat. New writes: "
        f"{_forbid_rag_network.ingested[len(before) :]!r}"
    )


def test_only_one_smart_teams_call_site_can_build_a_rag_client_and_it_is_fenced():
    """The check that would have caught the corpus pollution this file caused.

    Instrumenting ``RagManager`` across the whole ``-k smart_teams`` suite counts
    ZERO constructions now (measured over 873 tests). That measurement
    cannot itself live in a test — counting it means wrapping the class before the
    first import, which is a pytest plugin rather than an assertion — so what is
    pinned here is the property that makes it true and that a new call site would
    break: **there is exactly one place in Smart Teams that can build a RAG
    client, and the managed-runtime fence comes before it.**

    A second call site is precisely how this went wrong the first time. The guide
    job's ``ingest_published_guide`` defaulted to the real client with no guard,
    a workstation run wrote synthetic guides for test addresses into the LIVE
    companywide corpus of the production project, and the suite stayed green
    the whole time because the job catches the exception. Static, so it costs
    nothing and cannot itself reach the network.
    """
    from backend.jobs import smart_teams_comparison_job

    package = Path(guide.__file__).parent
    jobs = Path(smart_teams_comparison_job.__file__).parent
    files = sorted(package.glob("*.py")) + sorted(jobs.glob("smart_teams_*.py"))
    assert len(files) > 10, "the file sweep found almost nothing; check the paths"

    call_sites: list[tuple[str, str]] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment explaining the fence is not a call site
            if "import RagManager" in stripped or "RagManager(" in stripped:
                call_sites.append((path.name, stripped))

    assert len(call_sites) == 1, (
        "Smart Teams must have exactly one path to the companywide corpus. "
        f"Found {len(call_sites)}: {call_sites!r}. Every additional one is an "
        "unfenced write to a corpus every employee can retrieve from Chat."
    )
    source = Path(guide.__file__).read_text(encoding="utf-8")
    assert call_sites[0][0] == Path(guide.__file__).name, call_sites
    guard = source.index("running_in_managed_runtime()")
    build = source.index("import RagManager")
    assert guard < build, (
        "guide.py builds a RagManager before checking "
        "running_in_managed_runtime() — the fence is downstream of the thing it "
        "is fencing"
    )
