"""The cubie-worker job that scores a completed Work Style Interview.

This suite exists because of a defect that was invisible from both sides. The
router named ``backend.jobs.smart_teams_score_job`` in ``SCORING_JOB_MODULE``
and the module did not exist. ``launch_module_execution`` POSTs
``containerOverrides.args`` to the ``cubie-worker`` job, and the Cloud Run Jobs
API validates that the JOB exists — not that the module inside it does. So the
launch returned 200, ``submit_background_work`` reported ``executor="job"``, the
endpoint returned 202, and the interview sat ``completed`` and unscored forever
with no error anywhere. Every test here is aimed at one of the things that made
that silent:

* ``test_the_module_the_router_names_is_importable_and_runnable`` — the module
  is resolved through the router's own constant, so renaming either side breaks
  this rather than production.
* the exit-code tests — a job that cannot do its work must fail the Cloud Run
  execution. Exiting 0 on a failure is what makes a broken feature look healthy
  in the executions list.
* the audit-row tests — the console is not a durable record. A missing profile
  has to be answerable from the database weeks later, which is what
  ``GET /api/admin/audit?action=SMART_TEAMS_SCORING_FAILED`` reads.
* ``test_a_retried_execution_does_not_write_a_second_profile_version`` —
  ``cubie-worker`` runs with ``--max-retries=1``, so the second run is a real
  scenario, not a hypothetical. ``profile.py`` claims idempotency; this executes
  it twice and checks the version list rather than trusting the claim.

No test here makes a network call: the one LLM worker in a scoring run
(``scoring.extract_strengths``) is substituted, and the answer blobs are built
from the item banks rather than from literal ids so an item-bank edit changes
the fixtures instead of quietly scoring less.
"""

from __future__ import annotations

import uuid

import pytest

from rubik_core.config import settings
from rubik_core.smart_teams import contracts
from rubik_core.smart_teams import interview as interview_catalog
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
)

SHAY = "shay@acme.example"
SAM = "sam@acme.example"

_BIG_FIVE_TARGETS = {
    big_five.Domain.EXTRAVERSION: 5,
    big_five.Domain.AGREEABLENESS: 4,
    big_five.Domain.CONSCIENTIOUSNESS: 5,
    big_five.Domain.EMOTIONAL_STABILITY: 2,
    big_five.Domain.OPENNESS: 4,
}
#: Distinct weights per Spranger axis, so the ranking settles and no probe fires.
_MOTIVATOR_WEIGHTS = (5, 3, 4, 2, 1, 2)
_MOTIVATOR_POSITION = {name: i for i, name in enumerate(motivators.MOTIVATORS)}
_STORY = (
    "I like untangling a messy audit file until the story is obvious to "
    "everyone else in the room, and then writing the two sentences that make "
    "it obvious. "
)
_QUOTE = "untangling a messy audit file until the story is obvious"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """A throwaway SQLite database bound to the module-level ``SessionLocal``.

    The job opens its own sessions (it is a separate process in production and
    is written that way), so re-pointing the shared engine is the only way to
    put it on a test database — which is also exactly what the repo-root
    conftest guards.

    Restored on the way out. The engine is process-global, and a ``tmp_path``
    URL left behind points at a directory pytest may have already cleaned up —
    which fails a LATER module and blames it for this one's mess.
    """
    import rubik_core.database.db as dbmod

    previous_url = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'st_score_job.db'}"
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import smart_teams  # noqa: F401 — registers tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        settings.DATABASE_URL = previous_url
        dbmod._engine = None


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Substitute the single Vertex call a scoring run makes.

    ``score_answers`` resolves ``extractor or extract_strengths`` from module
    globals at call time, so patching the name is enough and the job under test
    stays completely unmodified.
    """
    from rubik_core.smart_teams import scoring

    monkeypatch.setattr(scoring, "extract_strengths", _stub_extractor())


def _stub_extractor(read=None):
    def extractor(_transcript, **_kwargs):
        return read if read is not None else _two_themes()

    return extractor


def _two_themes() -> strengths.StrengthsRead:
    ids = list(strengths.THEMES)[:2]
    return strengths.StrengthsRead(
        themes=tuple(
            strengths.ThemeRead(
                theme_id=theme_id,
                confidence=strengths.Confidence.HIGH,
                evidence_quote=_QUOTE,
                why="Named this pattern unprompted, twice.",
            )
            for theme_id in ids
        )
    )


# ── Answer blob ──────────────────────────────────────────────────────────────


def _keyed(value: int, reverse: bool) -> int:
    return (disc.LIKERT_MIN + disc.LIKERT_MAX) - value if reverse else value


def _answers() -> dict:
    out: dict = {}
    for item in disc.LIKERT_ITEMS:
        out[item.item_id] = _keyed(
            5 if item.dimension == "D" else 2, item.keying == "reverse"
        )
    for pair in disc.FORCED_CHOICE_PAIRS:
        if pair.option_a.dimension == "D":
            out[pair.item_id] = "a"
        elif pair.option_b.dimension == "D":
            out[pair.item_id] = "b"
        else:
            out[pair.item_id] = "a" if pair.option_a.dimension == "I" else "b"
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
    out["transcript:strengths"] = contracts.transcript_to_json(
        [
            contracts.Turn(
                speaker=contracts.Speaker.ASSISTANT,
                text="What kind of work leaves you with more energy than it took?",
                prompt_id=strengths.ELICITATION_SCRIPT[0].prompt_id,
            ),
            contracts.Turn(
                speaker=contracts.Speaker.INTERVIEWEE,
                text=_STORY * 6,
                prompt_id=strengths.ELICITATION_SCRIPT[0].prompt_id,
            ),
        ]
    )
    out["meta:enneagram_confirmation"] = {
        "type_number": None,
        "wing": None,
        "declined": False,
    }
    return out


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed_interview(*, email: str = SHAY, completed=None) -> str:
    """A ``completed`` interview row, the state the job is dispatched against."""
    from rubik_core.database.smart_teams import SmartTeamsInterview

    interview_id = uuid.uuid4().hex
    db = _session()
    try:
        db.add(
            SmartTeamsInterview(
                id=interview_id,
                user_email=email,
                status="completed",
                sections_completed=list(
                    interview_catalog.SECTION_KEYS if completed is None else completed
                ),
                answers=_answers(),
            )
        )
        db.commit()
    finally:
        db.close()
    return interview_id


def _versions(email: str = SHAY):
    from rubik_core.smart_teams import profile

    db = _session()
    try:
        return [
            (row.version, row.status, row.is_current)
            for row in profile.profile_history(db, email)
        ]
    finally:
        db.close()


def _audit_rows(action: str | None = None):
    from rubik_core.database.models import AuditLog

    db = _session()
    try:
        query = db.query(AuditLog)
        if action:
            query = query.filter(AuditLog.action == action)
        return query.order_by(AuditLog.id).all()
    finally:
        db.close()


def _job():
    from backend.jobs import smart_teams_score_job

    return smart_teams_score_job


# ── The module the router names ───────────────────────────────────────────────


def test_the_module_the_router_names_is_importable_and_runnable():
    """The defect this file exists for: a dispatch target that is not there.

    Resolved through ``SCORING_JOB_MODULE`` rather than a literal, so renaming
    either side fails here instead of in production — where the launch still
    returns 200 and the person just never gets a profile.
    """
    import importlib

    from backend.routers.smart_teams import SCORING_JOB_MODULE

    module = importlib.import_module(SCORING_JOB_MODULE)
    assert callable(module.main)
    # The router passes exactly this flag; a parser that does not accept it is
    # the same silent failure one layer down.
    assert (
        module._build_parser().parse_args(["--interview-id", "abc"]).interview_id
        == "abc"
    )


def test_the_job_is_in_the_worker_registry_and_names_its_dispatcher():
    from backend.jobs import WORKER_JOBS, undispatched_worker_jobs
    from backend.routers.smart_teams import SCORING_JOB_MODULE

    job = WORKER_JOBS["smart-teams-score"]
    assert job.module == SCORING_JOB_MODULE
    assert job.dispatched_from == "backend/routers/smart_teams.py"
    # Something does dispatch it — that is the whole difference between this
    # job and the three that only ever run by hand.
    assert "smart-teams-score" not in undispatched_worker_jobs()


def test_the_registered_command_is_the_one_that_actually_works():
    """gcloud splits ``--args`` on commas; a stray space breaks the module path."""
    from backend.jobs import worker_job_args, worker_job_execute_command

    assert worker_job_args("smart-teams-score", "--interview-id", "abc") == [
        "-m",
        "backend.jobs.smart_teams_score_job",
        "--interview-id",
        "abc",
    ]
    command = worker_job_execute_command(
        "smart-teams-score",
        "--interview-id",
        "abc",
        workspace="test",
        project="test-project",
    )
    assert (
        '--args="-m,backend.jobs.smart_teams_score_job,--interview-id,abc"' in command
    )


# ── The happy path ───────────────────────────────────────────────────────────


def test_main_scores_a_seeded_interview_and_exits_zero():
    interview_id = _seed_interview()

    assert _job().main(["--interview-id", interview_id]) == 0

    assert _versions() == [(1, "current", True)]
    # And the interview is linked to it, which is what /interview/status reads
    # to stop reporting "pending".
    from rubik_core.database.smart_teams import SmartTeamsInterview

    db = _session()
    try:
        row = db.get(SmartTeamsInterview, interview_id)
        assert row.profile_id is not None
    finally:
        db.close()
    # A clean run leaves no failure trace to chase.
    assert _audit_rows() == []


def test_a_retried_execution_does_not_write_a_second_profile_version():
    """``cubie-worker`` has ``--max-retries=1``; the second run is real.

    Executes the job twice rather than asserting that ``profile.py`` promises
    idempotency. Two versions from one sitting would mean the person's live
    profile is a duplicate of itself and every guide citing v1 points at a
    superseded row.
    """
    interview_id = _seed_interview()

    first = _job().main(["--interview-id", interview_id])
    second = _job().main(["--interview-id", interview_id])

    assert (first, second) == (0, 0)
    assert _versions() == [(1, "current", True)]
    assert _audit_rows() == []


# ── Loud failure ─────────────────────────────────────────────────────────────


def test_a_missing_interview_id_exits_non_zero_with_a_clear_message(caplog):
    job = _job()
    with caplog.at_level("ERROR"):
        code = job.main(["--interview-id", "does-not-exist"])

    assert code == job.EXIT_REFUSED
    assert code != 0, "a scoring failure that exits 0 is invisible in Cloud Run"
    assert "does-not-exist" in caplog.text
    assert "no interview" in caplog.text


def test_a_missing_interview_leaves_a_durable_audit_trace():
    """The console is not a record. This row is what an admin can still read."""
    job = _job()
    job.main(["--interview-id", "does-not-exist"])

    rows = _audit_rows(job.ACTION_FAILED)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "failure"
    assert row.doc_id == "does-not-exist"
    # No interview row means no owner to name, so the actor is the job itself —
    # deliberately not an email address.
    assert row.actor_email == job.SYSTEM_ACTOR
    assert "@" not in row.actor_email
    assert row.details["error_type"] == "ProfileError"
    assert row.details["retryable"] is False


def test_an_unfinished_interview_is_refused_and_traced_against_its_owner():
    """Scoring half an interview would lock the rest of the answers out.

    The trace is keyed to the person, because "why has Sam still not got a
    profile?" is the question somebody actually asks, and ``actor_email`` is the
    only field ``GET /api/admin/audit`` can filter on.
    """
    job = _job()
    interview_id = _seed_interview(
        email=SAM, completed=[interview_catalog.SECTION_KEYS[0]]
    )

    assert job.main(["--interview-id", interview_id]) == job.EXIT_REFUSED
    assert _versions(SAM) == []

    rows = _audit_rows(job.ACTION_FAILED)
    assert len(rows) == 1
    assert rows[0].actor_email == SAM
    assert rows[0].doc_id == interview_id
    assert rows[0].details["job"] == job.SYSTEM_ACTOR


def test_an_unexpected_error_exits_non_zero_and_is_marked_retryable(monkeypatch):
    """Anything that is not a refusal may well succeed on the retry."""
    from rubik_core.smart_teams import scoring

    job = _job()
    interview_id = _seed_interview()

    def boom(_interview_id):
        raise RuntimeError("Cloud SQL connection reset")

    monkeypatch.setattr(scoring, "score_interview", boom)

    assert job.main(["--interview-id", interview_id]) == job.EXIT_ERROR
    rows = _audit_rows(job.ACTION_FAILED)
    assert len(rows) == 1
    assert rows[0].details["retryable"] is True
    assert "connection reset" in rows[0].details["error"]


def test_a_lost_layer_is_traced_even_though_a_profile_was_written(monkeypatch):
    """A version landed, so exit 0 — but a layer is silently empty on it.

    ``ScoringReport`` keeps "this layer errored" apart from "this layer honestly
    refused", and only the first is an operational problem. Without this row the
    thin profile is indistinguishable from a person whose answers genuinely did
    not support that layer.
    """
    from rubik_core.smart_teams import scoring

    job = _job()
    interview_id = _seed_interview()

    def boom(_transcript, **_kwargs):
        raise RuntimeError("deadline exceeded")

    monkeypatch.setattr(scoring, "extract_strengths", boom)

    assert job.main(["--interview-id", interview_id]) == 0
    assert _versions() == [(1, "current", True)]

    rows = _audit_rows(job.ACTION_DEGRADED)
    assert len(rows) == 1
    assert rows[0].actor_email == SHAY
    assert rows[0].details["failed_frameworks"] == ["strengths"]
    assert rows[0].details["layer_failures"]["strengths"]["stage"] == "llm"
    assert rows[0].details["empty"] is False
    # The five good layers still reached the row.
    assert "disc" in rows[0].details["reportable_frameworks"]


def test_a_profile_with_nothing_reportable_is_traced_as_degraded(monkeypatch):
    """A document about a person with nothing in it is worth someone looking at.

    Nothing here *errored* — every instrument refused a page of 3s, which is
    correct behaviour — so the job exits 0 and the profile is written. The row
    is the operator's notice that the person will open an empty profile.
    """
    from rubik_core.database.smart_teams import SmartTeamsInterview

    job = _job()
    interview_id = _seed_interview()

    # Straight-line every scale item: the all-midpoint sitting every instrument
    # refuses in its own vocabulary.
    db = _session()
    try:
        row = db.get(SmartTeamsInterview, interview_id)
        row.answers = {
            key: (3 if not isinstance(value, (str, list, dict)) else value)
            for key, value in _answers().items()
        }
        db.commit()
    finally:
        db.close()

    from rubik_core.smart_teams import scoring

    monkeypatch.setattr(
        scoring,
        "extract_strengths",
        _stub_extractor(strengths.StrengthsRead(themes=())),
    )

    assert job.main(["--interview-id", interview_id]) == 0

    rows = _audit_rows(job.ACTION_DEGRADED)
    assert len(rows) == 1
    assert rows[0].details["empty"] is True
    assert rows[0].details["reportable_frameworks"] == []
    # Nothing errored — the layers refused, which is not the same thing.
    assert rows[0].details["failed_frameworks"] == []


def test_a_failed_audit_write_does_not_change_the_exit_code(monkeypatch, caplog):
    """If the database is unreachable the log is the only record — say so.

    The trace must never become the failure: a job that crashes while recording
    why it failed reports a different problem than the one it had. The log
    assertion is what proves the substitution actually ran, rather than the exit
    code happening to match.
    """
    job = _job()

    def broken(*_args, **_kwargs):
        raise RuntimeError("audit_logs is unreachable")

    monkeypatch.setattr("backend.audit.record_audit", broken)

    with caplog.at_level("ERROR"):
        code = job.main(["--interview-id", "does-not-exist"])

    assert code == job.EXIT_REFUSED
    assert _audit_rows() == []
    assert "audit_logs is unreachable" in caplog.text
    assert "only record" in caplog.text
