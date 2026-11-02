"""Smart Teams wave 1 — the seams BETWEEN ST-4, ST-5, ST-6 and ST-8.

Three agents built the interview scorer, the official-report upload and the
personal guide in parallel, each in its own module with its own router, and none
of them touched ``backend/main.py`` or ``backend/jobs/__init__.py``. Every one of
those features is covered by its own test file. This file covers only what none
of them could: the joins.

What it is defending, and why each one is here rather than assumed
-----------------------------------------------------------------

1. **All four routers are mounted, and mounted COMPANY-WIDE.** A Smart Teams
   router on the copilot-gated ``core`` parent 403s most of the company out of their own
   guide, and the shape of that bug is a 403 in production and a green test
   suite — every other test in this feature drives a private ``FastAPI()`` or
   overrides the identity, so none of them can see the mount. This one asserts
   the real ``backend.main.app`` and signs in as an employee in a department
   that an admin has narrowed out of the copilot.

2. **No two Smart Teams routes collide.** Checked against the routers' own
   route objects and not against ``app.openapi()``: OpenAPI is keyed by path, so
   a genuine duplicate registration is silently MERGED there and the second
   handler becomes unreachable without anything failing.

3. **Every job a router dispatches exists, is registered, and its arguments
   parse.** ``launch_module_execution`` validates that the Cloud Run *Job*
   exists, not the module inside it, so a dispatch naming a module that is not
   there returns 200, the endpoint returns 202, and nothing ever happens. That
   exact bug has already shipped in this feature once. Here the fake dispatcher
   REFUSES a module that is not in ``WORKER_JOBS`` and then runs the real
   ``main(argv)`` — so a registration that drifts from a dispatch site fails
   here rather than in a person's spinner.

4. **The full lifecycle across three features**: interview → score → generate →
   approve → a colleague reads it → unpublish → the colleague is denied. Every
   hop goes through the HTTP endpoints and the real cubie-worker entry points.

5. **An upload merge is a version bump, and ST-6 has to notice.** ST-5's merge
   calls ``profile.write_profile_version``; ST-6 keys its cache on the version.
   This asserts the published guide comes out of company view, its corpus copy
   is purged with BOTH arguments the DAM routing asymmetry needs, the old row
   reads ``stale``, and a regenerate lands on a NEW row rather than overwriting
   approved wording.

6. **ST-8's read is confirmed-only.** The read ST-9's Team Builder will staff
   projects from must never return an unconfirmed inference or a denial —
   telling a project lead somebody has expertise they never claimed is the worst
   thing that half of the feature can do.

7. **There is one profile version writer, not two.** ST-4's interview path and
   ST-5's merge path both land on ``profile._insert_version`` under
   ``_with_version_retry``; two independent writers would race on
   ``uq_st_profile_one_current``.

No network. The two Vertex calls in this whole path — the strengths extractor
and the guide's prose writer — are stubbed, and the RAG manager and the vector
delete are stubbed too. ``_forbid_llm`` additionally makes the real JSON client
factories raise, so a code path that reaches for Vertex fails loudly instead of
being skipped over in a mock.
"""

from __future__ import annotations

import importlib

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
    smart_teams_guides as guides_router,
)
from backend.routers import (
    smart_teams_uploads as uploads_router,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.database import db as db_module
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, User
from rubik_core.database.smart_teams import (
    SKILL_LEVELS,
    SmartTeamsDocument,
    SmartTeamsInterview,
    SmartTeamsProfile,
    SmartTeamsSkill,
)
from rubik_core.identifiers import SMART_TEAMS_ASSISTANT_SPEAKER
from rubik_core.smart_teams import guide, scoring, skills
from rubik_core.smart_teams import interview as iv
from rubik_core.smart_teams import profile as st_profile
from rubik_core.smart_teams import uploads as up
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
)

OWNER = "owner@acme.example"
COLLEAGUE = "colleague@acme.example"
SYSADMIN = "sysadmin@acme.example"

BASE = "/api/smart-teams"
GUIDES = f"{BASE}/guides"

#: A department that is deliberately OUTSIDE the copilot. If the copilot
#: roster ever grows to include HR this constant is what has to change, not the
#: assertion — the point is to sign in as somebody the copilot excludes.
NARROWED_OUT_DEPARTMENT = "HR"

client = TestClient(app)


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
    """Sign in as somebody. Only the IDENTITY is faked — ``require_smart_teams_
    access`` still runs, which is the whole point of asserting the mount here."""
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The real app, one in-memory database, and a cubie-worker that really runs.

    ``SessionLocal`` is pointed at the same engine rather than at the request
    session: every job in this feature opens its OWN session (they run in a
    separate process), and a fixture that handed them the request's session
    would hide a job that forgets to commit.
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
    # Off-GCP storage for the uploaded report, per ST-5's own fixture.
    from rubik_core.config import AppMode, settings

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL, raising=False)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path), raising=False)

    app.dependency_overrides[get_db] = lambda: db
    _as(OWNER)
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


# ── The fake dispatcher: a worker that refuses an unregistered module ─────────


class Dispatcher:
    """Records what a router dispatched, and runs it for real on demand.

    Deliberately NOT executed inside the request: the whole reason these three
    features put their slow halves on cubie-worker is that Cloud Run throttles
    the service CPU once the response returns, so the realistic ordering is
    "respond, then the job runs". :meth:`drain` is that second step.

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
def _forbid_llm(monkeypatch):
    """No test in this file may reach Vertex. Every seam is stubbed explicitly.

    Patching the client FACTORIES rather than trusting that each stub is in the
    right place: a path that reaches for a real model raises here with a name,
    instead of quietly making a network call in CI.
    """

    def _refuse(*_a, **_kw):
        raise AssertionError(
            "a Smart Teams integration test tried to build a real LLM client"
        )

    monkeypatch.setattr(scoring, "_json_client", _refuse, raising=True)
    monkeypatch.setattr(guide, "_json_client", _refuse, raising=True)
    monkeypatch.setattr(up, "_json_client", _refuse, raising=True)


# ── Stubs for the two model calls and the two RAG calls ──────────────────────


_STORY = (
    "I like untangling a messy audit file until the story is obvious to "
    "everyone else in the room, and then writing the two sentences that make "
    "it obvious. "
)
_QUOTE = "untangling a messy audit file until the story is obvious"


def _strengths_read() -> strengths.StrengthsRead:
    """A reportable strengths read, built through the instrument's own types."""
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
    """The scoring engine's one Vertex call. Resolved by module global at call
    time (``extractor or extract_strengths``), so this is the injection point the
    cubie-worker entry point actually uses."""
    monkeypatch.setattr(
        scoring, "extract_strengths", lambda _t, **_kw: _strengths_read(), raising=True
    )


@pytest.fixture(autouse=True)
def _stub_prose(monkeypatch):
    """The guide's one Vertex call.

    Returns ``{}`` — every slot absent — which makes ``build_guide`` fall back to
    its authored wording. That is a supported mode and not a degraded one, and it
    is the right stub for an integration test: it keeps model text out of an
    assertion about wiring while still proving the writer is CALLED, which the
    counter below records.
    """
    calls: list[int] = []

    def _writer(slots, *, display_name, **_kw):
        calls.append(len(slots))
        return {}

    monkeypatch.setattr(guide, "write_prose", _writer, raising=True)
    return calls


@pytest.fixture
def rag(monkeypatch):
    """The corpus write and the corpus delete, recorded rather than performed."""

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


def _targets() -> dict[str, object]:
    """One value per item id across all four banks. NOT a straight line.

    A page of 3s is refused by every instrument (deliberately — see
    ``test_smart_teams_scoring``), and a guide built from a refused profile is
    the shortfall document, which would make the lifecycle test below assert
    nothing about a guide with content in it.
    """
    out: dict[str, object] = {}
    for item in disc.LIKERT_ITEMS:
        out[item.item_id] = _keyed(
            5 if item.dimension == "D" else 2, item.keying == "reverse"
        )
    for index, pair in enumerate(disc.FORCED_CHOICE_PAIRS):
        if pair.option_a.dimension == "D":
            out[pair.item_id] = "a"
        elif pair.option_b.dimension == "D":
            out[pair.item_id] = "b"
        else:
            # Alternate on the rest so the position-share check has more than one
            # column to look at. Index-based, not hash-based: PYTHONHASHSEED
            # varies per run and a flaky fixture is worse than no fixture.
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


TARGETS = _targets()


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


def _answer_the_whole_interview() -> None:
    """Walk the catalog through the real endpoints, as a person would."""
    started = client.post(f"{BASE}/interview/start")
    assert started.status_code == 200, started.text

    for section in iv.SECTIONS:
        if section.item_ids:
            body = {
                "answers": {item: TARGETS[item] for item in section.item_ids},
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


# ═════════════════════════════════════════════════════════════════════════════
# 1. The mount
# ═════════════════════════════════════════════════════════════════════════════

ST_ROUTERS = {
    "smart_teams": st_router.router,
    "smart_teams_uploads": uploads_router.router,
    "smart_teams_guides": guides_router.router,
}

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _mounted_table() -> dict[str, set[str]]:
    """Every mounted Smart Teams path -> its methods, from the BUILT app.

    ``app.openapi()`` and not ``app.routes``: this FastAPI version keeps lazy
    ``_IncludedRouter`` objects on ``app.routes``, so walking that finds zero
    smart-teams routes and every assertion here would pass vacuously. (That is
    also why ``test_smart_teams_guide.py``'s own mount check can only ever
    skip — it walks ``app.routes``.)
    """
    return {
        path: {method.upper() for method in ops}
        for path, ops in app.openapi()["paths"].items()
        if path.startswith(BASE)
    }


def test_all_four_smart_teams_routers_are_mounted():
    """Every route each router declares is reachable on the real app.

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


def test_every_smart_teams_router_is_reachable_outside_the_copilot_gate(wired):
    """The company-wide mount, proved by an HR employee touching all four.

    A 403 on any of these means the router was mounted under ``core`` or its gate
    was swapped for ``require_full_app_access`` — which locks most of the company
    out of an interview they are nudged weekly to finish, and out of consenting
    to a guide their colleagues can read.
    """
    _as(OWNER, department=NARROWED_OUT_DEPARTMENT)

    interview = client.get(f"{BASE}/interview")
    assert interview.status_code == 200, interview.text

    uploads = client.get(f"{BASE}/uploads")
    assert uploads.status_code == 200, uploads.text
    assert uploads.json()["uploads"] == []

    own_guide = client.get(f"{GUIDES}/me")
    assert own_guide.status_code == 200, own_guide.text
    assert own_guide.json()["state"] == "none", "no guide is a state, not a 404"


def test_an_off_domain_identity_is_refused_on_all_three_routers(wired):
    """The gate is on each APIRouter, so mounting cannot have dropped it."""
    _as("attacker@gmail.com")
    for method, path in (
        ("get", f"{BASE}/interview"),
        ("get", f"{BASE}/uploads"),
        ("get", f"{GUIDES}/me"),
    ):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, (path, resp.status_code, resp.text)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Path collisions
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


def test_the_guides_catch_all_cannot_shadow_a_literal_route(wired):
    """``GET /guides/{subject_email}`` is a one-segment catch-all.

    Two literals live beside it and both must win: ``/guides/me`` (declared
    first, so FastAPI matches it first) and ``/guides/support/{email}`` (two
    segments, so it cannot be eaten). The failure mode is silent — a colleague
    read handler answering the page's own "my guide" call — so it is asserted
    against real responses rather than against declaration order.
    """
    _seed_user(wired, OWNER, "Owner Person")
    _as(OWNER)

    own = client.get(f"{GUIDES}/me")
    assert own.status_code == 200
    assert set(own.json()) >= {"state", "can_generate"}, (
        "GET /guides/me was answered by the colleague-read handler"
    )

    _as(SYSADMIN, department="Systems", is_admin=True)
    support = client.get(f"{GUIDES}/support/{OWNER}")
    assert support.status_code == 200, support.text
    assert support.json()["subject_email"] == OWNER
    assert "guides" in support.json(), (
        "GET /guides/support/{email} was answered by the colleague-read handler"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 3. Job registration
# ═════════════════════════════════════════════════════════════════════════════

#: The module constant each router dispatches through, read off the router
#: itself so a rename cannot leave this list pointing at the old name.
DISPATCHED_MODULES = {
    "smart_teams": st_router.SCORING_JOB_MODULE,
    "smart_teams_uploads": uploads_router.PARSE_JOB_MODULE,
    "smart_teams_guides": guides_router.GUIDE_JOB_MODULE,
}


@pytest.mark.parametrize(("router_name", "module"), sorted(DISPATCHED_MODULES.items()))
def test_every_dispatched_module_is_registered_importable_and_runnable(
    router_name, module
):
    """The "202 and does nothing" guard, in four parts.

    A Cloud Run Jobs launch validates that the JOB exists, not that the module
    inside the image does — so a dispatch naming a module that is absent,
    unimportable, or missing ``main`` succeeds at the API level and the person
    watches a spinner forever.
    """
    entries = {name: job for name, job in WORKER_JOBS.items() if job.module == module}
    assert entries, (
        f"{router_name} dispatches {module!r} and nothing in WORKER_JOBS names "
        "it. Register it in backend/jobs/__init__.py."
    )
    assert len(entries) == 1, f"{module!r} is registered under {sorted(entries)}"
    name, job = next(iter(entries.items()))

    imported = importlib.import_module(module)
    assert callable(getattr(imported, "main", None)), f"{module} has no main()"

    # The registered example is the command an operator will actually paste, so
    # it has to PARSE. ``<st_documents.id>``-style placeholders stand in for a
    # real id and are substituted rather than dropped — dropping one leaves a
    # dangling flag and would make this assertion test argparse instead of the
    # registration.
    parser = imported._build_parser()
    example = [
        "placeholder" if arg.startswith("<") and arg.endswith(">") else arg
        for arg in job.example_args
    ]
    namespace = parser.parse_args(example)
    assert namespace is not None, f"{name}: example_args {job.example_args!r}"

    # And the file the registration claims dispatches it really names it, so the
    # "run it by hand" note cannot drift away from the code.
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / (job.dispatched_from or "")
    assert source.is_file(), f"{name}: dispatched_from {job.dispatched_from!r} is gone"
    assert module in source.read_text(encoding="utf-8"), (
        f"{name}: {job.dispatched_from} does not name {module!r}"
    )


def test_a_bare_smart_teams_job_run_refuses_instead_of_guessing():
    """No default_args on any of the three, and a bare run must exit non-zero.

    A guessed default here is not a harmless convenience: ``--purge`` chosen by
    default would retract a guide nobody asked to retract.
    """
    for module in sorted(set(DISPATCHED_MODULES.values())):
        job = next(j for j in WORKER_JOBS.values() if j.module == module)
        assert job.default_args == (), f"{module} has default_args={job.default_args!r}"
        with pytest.raises(SystemExit):
            importlib.import_module(module)._build_parser().parse_args([])


# ═════════════════════════════════════════════════════════════════════════════
# 4. The lifecycle: interview -> score -> guide -> approve -> read -> unpublish
# ═════════════════════════════════════════════════════════════════════════════


def test_interview_to_score_to_guide_to_approval_to_a_colleague_and_back(
    wired, worker, rag, _stub_prose
):
    """The whole wave-1 path, end to end, through the endpoints and the jobs.

    Every hop is a separate feature's code, and every hop is the one the three
    agents could not test together:

    ST-3 interview writes -> ST-4 scoring job -> ST-6 generate job -> ST-6
    approve (consent) -> ST-6 ingest job -> a third party's read -> ST-6
    unpublish -> ST-6 purge job -> the third party is denied again.
    """
    db = wired
    _seed_user(db, OWNER, "Owner Person")
    _seed_user(db, COLLEAGUE, "Colleague Person")

    # ── interview ───────────────────────────────────────────────────────────
    _as(OWNER)
    _answer_the_whole_interview()

    done = client.post(f"{BASE}/interview/complete")
    assert done.status_code == 202, done.text
    assert done.json()["enqueued"] is True
    assert worker.modules() == [st_router.SCORING_JOB_MODULE]

    # ── score, on the worker ────────────────────────────────────────────────
    assert worker.drain(db) == [0]
    version = _current_version(db, OWNER)
    assert version == 1, "the scoring job did not write a profile version"
    stored = (
        db.query(SmartTeamsProfile).filter(SmartTeamsProfile.user_email == OWNER).one()
    )
    assert stored.origin == "interview"
    assert stored.is_current is True

    # ── generate ────────────────────────────────────────────────────────────
    queued = client.post(f"{GUIDES}/generate")
    assert queued.status_code == 202, queued.text
    assert queued.json()["enqueued"] is True
    assert worker.modules()[-1] == guides_router.GUIDE_JOB_MODULE
    assert worker.seen[-1][1] == (
        "--document-id",
        queued.json()["guide"]["id"],
        "--generate",
    )

    assert worker.drain(db) == [0]
    document_id = queued.json()["guide"]["id"]
    row = db.get(SmartTeamsDocument, document_id)
    assert row.status == "ready", row.error
    assert (row.content_md or "").strip(), "a ready guide with no body"
    assert row.published is False and row.approved_at is None
    assert _stub_prose, "the prose writer was never called"

    # A guide is a document about a person that their colleagues read. The
    # advisory promise has to be IN it, not merely near it.
    assert disc.ADVISORY_NOTICE in row.content_md
    rest = row.content_md.replace(disc.ADVISORY_NOTICE, "")
    assert strengths._ADVANCEMENT_PROSE.search(rest) is None, (
        "the generated guide contains advancement language"
    )

    # ── a colleague cannot read an unapproved draft ──────────────────────────
    _as(COLLEAGUE)
    denied = client.get(f"{GUIDES}/{OWNER}")
    assert denied.status_code == 404, denied.text
    assert client.get(f"{GUIDES}/{document_id}/docx").status_code in (403, 404)

    # ── only the subject may approve, and approval is what publishes ─────────
    not_yours = client.post(f"{GUIDES}/{document_id}/approve")
    assert not_yours.status_code in (403, 404), not_yours.text
    db.expire_all()
    assert db.get(SmartTeamsDocument, document_id).published is False

    _as(OWNER)
    approved = client.post(f"{GUIDES}/{document_id}/approve")
    assert approved.status_code == 200, approved.text
    db.expire_all()
    row = db.get(SmartTeamsDocument, document_id)
    assert row.published is True
    assert row.approved_at is not None, "published without approved_at (CHECK)"
    assert row.approved_by == OWNER

    # Publication dispatches the corpus write; consent itself was synchronous.
    assert worker.modules()[-1] == guides_router.GUIDE_JOB_MODULE
    assert worker.seen[-1][1] == ("--document-id", document_id, "--ingest")
    assert row.rag_ingested is False, "rag_ingested must not be claimed before the job"
    assert worker.drain(db) == [0]
    db.expire_all()
    row = db.get(SmartTeamsDocument, document_id)
    assert row.rag_ingested is True
    assert rag.ingested == [guide.GUIDE_RAG_SLUG]

    # ── now the colleague can read it ────────────────────────────────────────
    _as(COLLEAGUE)
    visible = client.get(f"{GUIDES}/{OWNER}")
    assert visible.status_code == 200, visible.text
    assert visible.json()["guide"]["id"] == document_id
    assert visible.json()["guide"]["content_md"].strip()
    assert client.get(f"{GUIDES}/{document_id}/docx").status_code == 200

    # ── unpublish, and the read closes again ────────────────────────────────
    _as(OWNER)
    pulled = client.post(f"{GUIDES}/{document_id}/unpublish")
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["rag_purge"] == "pending", (
        'unpublish must never report "done" on the strength of having asked'
    )
    assert worker.seen[-1][1] == ("--document-id", document_id, "--purge")

    _as(COLLEAGUE)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 404
    assert client.get(f"{GUIDES}/{document_id}/docx").status_code in (403, 404)

    # ── the purge is attempted with BOTH arguments the DAM asymmetry needs ───
    assert worker.drain(db) == [0]
    assert rag.deleted, "the purge job never called delete_document_vectors"
    kwargs = rag.deleted[-1]["kwargs"]
    assert kwargs.get("source_path"), "purge without source_path hits the wrong corpus"
    assert kwargs.get("is_company_wide") is True
    db.expire_all()
    assert db.get(SmartTeamsDocument, document_id).rag_ingested is False


def test_a_failed_purge_leaves_the_row_saying_it_is_still_ingested(
    wired, worker, rag, _stub_prose
):
    """The one outcome in this feature that is a privacy defect, not a hiccup.

    Out of the app and still answerable in Chat. The row must keep saying
    ``rag_ingested = true`` — the honest value — and the job must exit non-zero
    so the execution is marked FAILED rather than looking like a retraction that
    happened.
    """
    db = wired
    _seed_user(db, OWNER, "Owner Person")
    _as(OWNER)
    _answer_the_whole_interview()
    client.post(f"{BASE}/interview/complete")
    worker.drain(db)
    document_id = client.post(f"{GUIDES}/generate").json()["guide"]["id"]
    worker.drain(db)
    client.post(f"{GUIDES}/{document_id}/approve")
    worker.drain(db)

    rag.delete_result = False
    client.post(f"{GUIDES}/{document_id}/unpublish")
    codes = worker.drain(db)
    assert codes and codes[-1] != 0, "a failed purge must fail the task"
    db.expire_all()
    row = db.get(SmartTeamsDocument, document_id)
    assert row.published is False, "the in-app read must close regardless"
    assert row.rag_ingested is True, (
        "rag_ingested was cleared without the removal being proved"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 5. ST-5 merge -> ST-6 staleness -> regeneration
# ═════════════════════════════════════════════════════════════════════════════

_VENDOR_SENTENCE = "Owner is a high-potential leader ready for promotion to Director."


def _vendor_payload() -> dict:
    """What the parse model returns, INCLUDING prose the contract must drop."""
    return {
        "framework": "disc",
        "scale": "zero_to_100",
        "disc": {"D": 78, "I": 44, "S": 31, "C": 66},
        # Off-contract keys. ``_coerce_proposal`` drops them; asserted below.
        "summary": _VENDOR_SENTENCE,
        "style_name": "Vendor Driver",
    }


@pytest.fixture
def stub_parse(monkeypatch):
    """The report reader and the parse model. Coercion is NOT stubbed.

    ``_coerce_proposal`` is the control that keeps vendor prose out of the
    profile, so the fake parser runs the real one over a payload that contains
    some — a stub that returned an already-clean proposal would test nothing.
    """
    monkeypatch.setattr(
        up,
        "extract_report_text",
        lambda _data: ("DISC PROFILE REPORT\nD 78 I 44 S 31 C 66", False),
        raising=True,
    )
    monkeypatch.setattr(
        up,
        "parse_report_text",
        lambda _text, **_kw: up._coerce_proposal(_vendor_payload()),
        raising=True,
    )


def test_an_upload_merge_bumps_the_version_and_ST6_notices(
    wired, worker, rag, stub_parse, _stub_prose
):
    """ST-5 writes a profile version; ST-6's cached guide must go stale.

    The join neither agent could test: ST-5 calls
    ``profile.write_profile_version``, ST-6 keys its cache on
    ``profile_version_key``, and the consequence for a person is that the guide
    their colleagues were reading has to come out of view until they approve a
    new one. Asserted here on the row, on the colleague's read, on the corpus,
    and on what the confirm response TELLS them.
    """
    db = wired
    _seed_user(db, OWNER, "Owner Person")
    _seed_user(db, COLLEAGUE, "Colleague Person")

    # A published guide off the interview profile, so there is something to lose.
    _as(OWNER)
    _answer_the_whole_interview()
    client.post(f"{BASE}/interview/complete")
    worker.drain(db)
    first_id = client.post(f"{GUIDES}/generate").json()["guide"]["id"]
    worker.drain(db)
    client.post(f"{GUIDES}/{first_id}/approve")
    worker.drain(db)
    assert _current_version(db, OWNER) == 1
    db.expire_all()
    assert db.get(SmartTeamsDocument, first_id).published is True

    _as(COLLEAGUE)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 200

    # ── upload -> parse (on the worker) -> proposal ──────────────────────────
    _as(OWNER)
    posted = client.post(
        f"{BASE}/uploads",
        files={"file": ("official-disc.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert posted.status_code == 202, posted.text
    upload_id = posted.json()["id"]
    assert worker.modules()[-1] == uploads_router.PARSE_JOB_MODULE
    assert worker.drain(db) == [0]

    proposal = client.get(f"{BASE}/uploads/{upload_id}")
    assert proposal.status_code == 200, proposal.text
    assert proposal.json()["framework"] == "disc"
    assert proposal.json()["review_status"] == "pending"

    # ── confirm -> merge -> a new version, and the guide consequence ─────────
    confirmed = client.post(
        f"{BASE}/uploads/{upload_id}/confirm", json={"framework": "disc"}
    )
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["merged"]["profile_version"] == 2
    assert payload["merged"]["provenance"]["disc"] == "official_upload"
    assert first_id in payload["guide"]["unpublished_document_ids"]
    assert payload["guide"]["message"], (
        "the person has to be TOLD their guide left company view"
    )
    assert payload["guide"]["regeneration"] == "stale"

    # The vendor's prose never reached any column.
    db.expire_all()
    merged = (
        db.query(SmartTeamsProfile)
        .filter(SmartTeamsProfile.user_email == OWNER)
        .filter(SmartTeamsProfile.version == 2)
        .one()
    )
    for framework in (
        "disc",
        "big_five",
        "type16",
        "strengths",
        "motivators",
        "enneagram",
    ):
        assert _VENDOR_SENTENCE not in str(getattr(merged, framework))
    assert merged.is_current is True
    retired = (
        db.query(SmartTeamsProfile)
        .filter(SmartTeamsProfile.user_email == OWNER)
        .filter(SmartTeamsProfile.version == 1)
        .one()
    )
    assert retired.is_current is None, "is_current must be NULL, never False"

    # ── ST-6 agrees: the old row is stale and out of view ───────────────────
    old = db.get(SmartTeamsDocument, first_id)
    assert old.published is False
    assert old.approved_at is not None, (
        "approved_at must survive an unpublish — it is the record consent existed"
    )
    assert guide.is_stale(old, _current_version(db, OWNER)) is True
    assert rag.deleted, "the superseded guide's corpus copy was never purged"

    _as(COLLEAGUE)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 404

    _as(OWNER)
    own = client.get(f"{GUIDES}/me")
    assert own.status_code == 200
    assert own.json()["guide"]["stale"] is True
    assert own.json()["current_profile_version"] == 2

    # ── regeneration lands on a NEW row, and re-approval is required ─────────
    again = client.post(f"{GUIDES}/generate")
    assert again.status_code == 202, again.text
    second_id = again.json()["guide"]["id"]
    assert second_id != first_id, (
        "regeneration overwrote the row whose wording was approved"
    )
    assert worker.drain(db) == [0]
    db.expire_all()
    fresh = db.get(SmartTeamsDocument, second_id)
    assert fresh.status == "ready", fresh.error
    assert fresh.published is False and fresh.approved_at is None

    _as(COLLEAGUE)
    assert client.get(f"{GUIDES}/{OWNER}").status_code == 404, (
        "a regenerated guide must not be company-visible before its subject "
        "approves it again"
    )


def test_rejecting_an_upload_changes_nothing_about_the_guide(
    wired, worker, rag, stub_parse, _stub_prose
):
    """The other half of the merge seam: a decision NOT to merge is inert.

    A published guide must survive a rejected upload untouched — same row, still
    published, still not stale — because nothing about the person's profile
    changed.
    """
    db = wired
    _seed_user(db, OWNER, "Owner Person")
    _as(OWNER)
    _answer_the_whole_interview()
    client.post(f"{BASE}/interview/complete")
    worker.drain(db)
    document_id = client.post(f"{GUIDES}/generate").json()["guide"]["id"]
    worker.drain(db)
    client.post(f"{GUIDES}/{document_id}/approve")
    worker.drain(db)

    upload_id = client.post(
        f"{BASE}/uploads",
        files={"file": ("official-disc.pdf", b"%PDF-1.4 fake", "application/pdf")},
    ).json()["id"]
    worker.drain(db)
    rejected = client.post(f"{BASE}/uploads/{upload_id}/reject")
    assert rejected.status_code == 200, rejected.text

    db.expire_all()
    assert _current_version(db, OWNER) == 1
    row = db.get(SmartTeamsDocument, document_id)
    assert row.published is True
    assert guide.is_stale(row, _current_version(db, OWNER)) is False


# ═════════════════════════════════════════════════════════════════════════════
# 6. ST-8's read, which ST-9 will staff projects from
# ═════════════════════════════════════════════════════════════════════════════


def _seed_taxonomy(db) -> None:
    for key, name in (
        ("data_privacy", "Data Privacy"),
        ("gaap", "GAAP"),
        ("process_reviews", "Process Reviews"),
        ("tax_planning", "Tax Planning"),
    ):
        db.add(SmartTeamsSkill(skill_key=key, name=name, cluster="Compliance"))
    db.commit()


def test_the_team_builder_read_returns_confirmed_skills_only(wired):
    """ST-9's read must never surface a question we asked as a fact.

    Four rows, one of each kind: declared (confirmed by definition), an
    inference the person confirmed, an inference they have not answered, and a
    denial tombstone. Only the first two may come back — and the denial is the
    trap, because it lives in the same ``confirmed = False`` state as a pending
    suggestion, which is exactly what makes ``confirmed.is_(True)`` the only
    correct predicate.
    """
    db = wired
    _seed_taxonomy(db)
    level = SKILL_LEVELS[0]

    skills.record_declared_skills(db, OWNER, {"data_privacy": level})

    def _suggest(key: str) -> None:
        skills.upsert_inference(
            db,
            skills.Suggestion(
                user_email=OWNER,
                skill_key=key,
                level=level,
                rationale="Answered three questions about this in a meeting.",
                items=({"kind": "meeting", "ref": f"mtg-{key}"},),
            ),
        )

    _suggest("gaap")
    skills.confirm(db, OWNER, "gaap")
    _suggest("process_reviews")  # left unanswered
    _suggest("tax_planning")
    skills.deny(db, OWNER, "tax_planning", denied_by=OWNER)

    confirmed = skills.confirmed_skills_for(db, [OWNER])[OWNER]
    keys = sorted(skill.skill_key for skill in confirmed)
    assert keys == ["data_privacy", "gaap"], (
        "confirmed_skills_for returned something nobody claimed or accepted"
    )

    # The subject's own view still sees all three states — that is what makes a
    # suggestion answerable and a denial visibly a decision rather than data loss.
    buckets = skills.bucketed_for(db, OWNER)
    assert sorted(r.skill_key for r, _ in buckets.confirmed) == ["data_privacy", "gaap"]
    assert [r.skill_key for r, _ in buckets.suggested] == ["process_reviews"]
    assert [r.skill_key for r, _ in buckets.denied] == ["tax_planning"]


def test_a_denied_skill_is_never_re_suggested_by_a_later_sweep(wired):
    """The tombstone, which is the only reason the read above stays honest.

    A delete would let the next sweep read the same meeting, find nothing on
    file, and ask again — and a suggestion that keeps coming back is a
    suggestion somebody eventually clicks by accident.
    """
    db = wired
    _seed_taxonomy(db)
    level = SKILL_LEVELS[0]
    suggestion = skills.Suggestion(
        user_email=OWNER,
        skill_key="gaap",
        level=level,
        rationale="Mentioned in two meetings.",
        items=({"kind": "meeting", "ref": "MTG-1"},),
    )
    skills.upsert_inference(db, suggestion)
    skills.deny(db, OWNER, "gaap", denied_by=OWNER)

    assert skills.upsert_inference(db, suggestion) == skills.SKIPPED_DENIED
    row = skills.subject_row_for(db, OWNER, "gaap")
    assert skills.is_denied(row) is True
    assert bool(row.confirmed) is False
    assert skills.confirmed_skills_for(db, [OWNER])[OWNER] == ()


def test_a_confirmed_skill_read_folds_email_case(wired):
    """``users.email`` is a case-sensitive PK stored verbatim from the Google
    token, so every Smart Teams read folds. A Team Builder that misses somebody
    because their address is capitalised differently staffs the wrong project."""
    db = wired
    _seed_taxonomy(db)
    skills.record_declared_skills(db, OWNER, {"gaap": SKILL_LEVELS[0]})
    folded = skills.confirmed_skills_for(db, ["Owner@ACME.example"])
    assert list(folded) == [OWNER]
    assert [skill.skill_key for skill in folded[OWNER]] == ["gaap"]


# ═════════════════════════════════════════════════════════════════════════════
# 7. One version writer, not two
# ═════════════════════════════════════════════════════════════════════════════


def test_both_version_writers_share_one_insert_path(wired, worker, stub_parse):
    """ST-4 and ST-5 must not be two independent version writers.

    They are two documented ENTRY POINTS — ``score_and_store_interview`` is
    idempotent on ``st_interviews.profile_id`` because a Cloud Run task retry
    must not produce two versions from one sitting, and
    ``write_profile_version`` deliberately is not because a merge legitimately
    appends every time it runs. Both must go through ``_insert_version`` under
    ``_with_version_retry``, which is where the ``is_current = NULL`` retirement
    and the version-collision rollback live. A second implementation of that
    would race on ``uq_st_profile_one_current`` — and the loser is a 500 in
    front of somebody confirming their own report.

    Asserted by counting: patching ``_insert_version`` and driving both paths,
    every version written has to pass through it.
    """
    db = wired
    _seed_user(db, OWNER, "Owner Person")

    calls: list[str] = []
    real_insert = st_profile._insert_version

    def _counting(session, report, *, origin, status, interview_id):
        calls.append(origin)
        return real_insert(
            session,
            report,
            origin=origin,
            status=status,
            interview_id=interview_id,
        )

    # Patched on the module the two entry points resolve it from.
    original = st_profile._insert_version
    st_profile._insert_version = _counting
    try:
        _as(OWNER)
        _answer_the_whole_interview()
        client.post(f"{BASE}/interview/complete")
        assert worker.drain(db) == [0]

        upload_id = client.post(
            f"{BASE}/uploads",
            files={"file": ("d.pdf", b"%PDF-1.4 fake", "application/pdf")},
        ).json()["id"]
        assert worker.drain(db) == [0]
        merged = client.post(f"{BASE}/uploads/{upload_id}/confirm")
        assert merged.status_code == 200, merged.text
    finally:
        st_profile._insert_version = original

    assert calls == ["interview", "official_upload"], calls
    db.expire_all()
    versions = sorted(
        (row.version, row.status, row.is_current)
        for row in db.query(SmartTeamsProfile)
        .filter(SmartTeamsProfile.user_email == OWNER)
        .all()
    )
    assert versions == [(1, "superseded", None), (2, "current", True)], versions


def test_the_scoring_entry_point_is_idempotent(wired, worker):
    """Half of the difference between the two entry points, stated as a test.

    Re-running the scoring job for one interview must not append a second
    version: Cloud Run retries a failed task, and "the LLM call succeeded but the
    commit timed out" must not leave somebody with two profile versions from one
    sitting. The other half — that a merge legitimately appends every time it
    runs — is the ``official_upload`` call counted in the test above.
    """
    db = wired
    _as(OWNER)
    _answer_the_whole_interview()
    # The ``worker`` fixture is what makes this test mean anything: without it
    # the off-GCP launcher runs the scoring fallback INLINE inside the request,
    # and the first call below would already be the second one.
    client.post(f"{BASE}/interview/complete")
    assert worker.modules() == [st_router.SCORING_JOB_MODULE]
    db.commit()

    interview_id = (
        db.query(SmartTeamsInterview)
        .filter(SmartTeamsInterview.user_email == OWNER)
        .one()
    ).id

    first = st_profile.score_and_store_interview(db, interview_id)
    second = st_profile.score_and_store_interview(db, interview_id)
    assert first.created is True
    assert second.created is False
    assert second.profile.id == first.profile.id
    assert _current_version(db, OWNER) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 8. Advisory-only, across every prose surface wave 1 added
# ═════════════════════════════════════════════════════════════════════════════


def test_no_wave1_response_body_carries_advancement_language(
    wired, worker, rag, stub_parse, _stub_prose
):
    """One screen over every response body this wave can return.

    ``strengths._ADVANCEMENT_PROSE`` is the pattern; the advisory notice is
    excised first because it is the sentence that PROMISES the profile is never
    used for "performance review, promotion, or compensation", so it contains
    the word the pattern matches on. Screening it rejected every guide once
    already.
    """
    db = wired
    _seed_user(db, OWNER, "Owner Person")
    _as(OWNER)
    _answer_the_whole_interview()

    bodies: list[tuple[str, str]] = []

    def _capture(label: str, resp) -> None:
        assert resp.status_code < 400, (label, resp.status_code, resp.text)
        bodies.append((label, resp.text))

    _capture("complete", client.post(f"{BASE}/interview/complete"))
    worker.drain(db)
    generated = client.post(f"{GUIDES}/generate")
    _capture("generate", generated)
    worker.drain(db)
    document_id = generated.json()["guide"]["id"]
    _capture("guides/me", client.get(f"{GUIDES}/me"))
    _capture("approve", client.post(f"{GUIDES}/{document_id}/approve"))
    worker.drain(db)
    _capture("colleague read", client.get(f"{GUIDES}/{OWNER}"))
    upload = client.post(
        f"{BASE}/uploads",
        files={"file": ("d.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    _capture("uploads", upload)
    worker.drain(db)
    _capture("upload read", client.get(f"{BASE}/uploads/{upload.json()['id']}"))
    _capture("unpublish", client.post(f"{GUIDES}/{document_id}/unpublish"))

    notice = disc.ADVISORY_NOTICE
    # The notice itself really would trip the pattern, so excising it is
    # load-bearing rather than defensive.
    assert strengths._ADVANCEMENT_PROSE.search(notice) is not None

    for label, text in bodies:
        # JSON-escaped bodies: the notice appears with escaped quotes, so both
        # forms are removed before screening.
        cleaned = text.replace(notice, "").replace(notice.replace('"', '\\"'), "")
        found = strengths._ADVANCEMENT_PROSE.search(cleaned)
        assert found is None, f"{label} response contains {found.group(0)!r}"


def test_a_derived_four_letter_code_never_renders_bare(wired, worker, _stub_prose):
    """A bare four-letter code reads as a psychometric verdict.

    ST-6 runs ``type16.find_unqualified_codes`` over the assembled body at
    generation time; this asserts the property on a real generated guide rather
    than on a unit fixture, because the failure only appears once the derived
    layer is open and something renders it.
    """
    from rubik_core.smart_teams.instruments import type16

    db = wired
    _seed_user(db, OWNER, "Owner Person")
    _as(OWNER)
    _answer_the_whole_interview()
    client.post(f"{BASE}/interview/complete")
    worker.drain(db)
    document_id = client.post(f"{GUIDES}/generate").json()["guide"]["id"]
    worker.drain(db)

    db.expire_all()
    body = db.get(SmartTeamsDocument, document_id).content_md or ""
    assert body.strip()
    assert type16.find_unqualified_codes(body) == (), (
        "a derived type rendered without its qualifier"
    )
