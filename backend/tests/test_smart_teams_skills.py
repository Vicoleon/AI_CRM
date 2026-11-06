"""ST-8 — skills: what a colleague may see, and what a denial is worth.

Eight things would fail silently if they regressed, and each has a test below.

1. **The colleague read carries no evidence.** This is finding 6 and the reason
   the read returns a DTO rather than an ORM row. The assertion is made on the
   SERIALIZED HTTP response, not on the object, because the leak was never in
   the intent — it was in what ``jsonable_encoder(row)`` would emit. The seeded
   evidence names a confidential meeting the viewer is not in, and the test
   greps the whole response body for its title and its excerpt, the same way
   ``test_smart_teams_uploads`` greps for the vendor sentence.

2. **An unconfirmed inference is not a fact.** A pending suggestion and a denial
   both live in ``confirmed = False``, so one predicate has to exclude both.
   The colleague read must show neither.

3. **A denial STICKS across sweeps.** Not asserted on the store's return value
   alone: the sweep is RUN TWICE over the same evidence with a denial in the
   middle, with a judge that says yes every time, and the second run must write
   nothing and report ``skipped_denied``.

4. **The sweep is idempotent over an unchanged source.** The second run over the
   same corpus makes no model call at all — the guarantee that keeps a nightly
   company-wide pass from being a nightly company-wide bill.

5. **Every inferred row carries evidence.** ``Suggestion`` refuses an empty
   citation list at construction, so a confident sentence about a person cannot
   be built on nothing.

6. **Nobody can touch anybody else's skills.** Confirm, deny and declare are all
   subject-from-principal. There is no request shape that names a subject, so
   the test asserts the absence structurally (no endpoint declares a
   subject-ish body field) as well as behaviourally (a colleague's confirm
   writes their OWN row, never the owner's).

7. **An off-taxonomy declaration is refused, not coerced.** 400 with the copy
   that names the path forward, and nothing written.

8. **The job exists, is registered, and is the module the router dispatches.**
   A launch of a module that is not in the image returns 200 and does nothing;
   this feature already shipped one 202-that-did-nothing.

The LLM is always stubbed — ``_YesJudge`` stands in for
``skill_inference.judge_candidates`` — and the readers are stubbed with a
``_StubReader`` so the tests exercise the real candidate assembly, the real
digest, and the real write path.

In-memory SQLite with a ``StaticPool`` and ``app.dependency_overrides``, per
``test_smart_teams_uploads.py``. The GATE is never overridden — only
``get_current_user`` — so ``require_smart_teams_access`` runs for real.
"""

from __future__ import annotations

import json
import logging
from dataclasses import FrozenInstanceError, fields

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
    SmartTeamsPersonSkill,
    SmartTeamsSkill,
)
from rubik_core.smart_teams import skill_inference as inference
from rubik_core.smart_teams import skill_taxonomy as taxonomy
from rubik_core.smart_teams import skills as store

OWNER = "owner@acme.example"
COLLEAGUE = "colleague@acme.example"
SYSADMIN = "sysadmin@acme.example"
CEO = "ceo@acme.example"

BASE = "/api/smart-teams/skills"

client = TestClient(app)

# The evidence a colleague must never see. A real shape: the ref is a transcript
# doc_id, the title is the meeting's filename, the excerpt is a line out of the
# transcript body. None of it passed ``meeting_participants`` on the way in —
# ``skill_inference`` gathered it as the SUBJECT.
SECRET_REF = "meet/2026-01-02/confidential-layoff-planning"
SECRET_TITLE = "CONFIDENTIAL: Q1 layoff planning"
SECRET_EXCERPT = "we will cut the audit team by four people"
SECRET_RATIONALE = "Ran the data privacy review in the layoff planning meeting."


# ── Identities ────────────────────────────────────────────────────────────────


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
        roles=["admin"] if is_admin else ["user"],
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
    )


def _as(email: str, **kw) -> None:
    """Sign in as somebody. The GATE still runs — only identity is faked."""
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    db = Session()
    app.dependency_overrides[get_db] = lambda: db
    _as(OWNER, department="Compliance")
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


@pytest.fixture
def seeded(db_session):
    """A real taxonomy, seeded through the real seeder."""
    taxonomy.seed_taxonomy(db_session)
    return db_session


# ── Seeding a confirmed inference whose evidence is not the viewer's ──────────


def _suggest(
    db,
    email: str,
    skill_key: str,
    *,
    ref: str = SECRET_REF,
    title: str = SECRET_TITLE,
    excerpt: str = SECRET_EXCERPT,
) -> str:
    return store.upsert_inference(
        db,
        store.Suggestion(
            user_email=email,
            skill_key=skill_key,
            level="working",
            rationale=SECRET_RATIONALE,
            items=(
                {"kind": "meeting", "ref": ref, "title": title, "excerpt": excerpt},
            ),
        ),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. Finding 6 — the colleague-visible read carries NO evidence
# ═════════════════════════════════════════════════════════════════════════════


def test_the_colleague_read_leaks_no_evidence_in_its_serialized_response(seeded):
    """Finding 6, asserted on the wire and not on the object.

    The regression this guards is not "somebody printed the evidence on
    purpose". It is that the read used to hand back ORM rows, so the FIRST
    serializer written over it — ``jsonable_encoder(row)``, a ``from_attributes``
    model, ``{**row.__dict__}`` — would have emitted an ACL'd meeting title and
    a transcript excerpt to every employee. So: grep the response body.
    """
    db = seeded
    _suggest(db, OWNER, "data_privacy")
    store.confirm(db, OWNER, "data_privacy")

    _as(COLLEAGUE, department="Sales")
    response = client.get(f"{BASE}/people/{OWNER}")
    assert response.status_code == 200, response.text

    body = json.dumps(response.json())
    for secret in (SECRET_TITLE, SECRET_EXCERPT, SECRET_REF, SECRET_RATIONALE):
        assert secret not in body, (
            f"the colleague-visible skills read leaked {secret!r}. The evidence "
            "cites meetings and documents the VIEWER may have no right "
            "to — it is gathered as the subject, not as the reader."
        )
    assert "evidence" not in body
    # And the claim itself did come through, so this is not passing vacuously.
    assert [s["skill_key"] for s in response.json()["skills"]] == ["data_privacy"]
    assert response.json()["skills"][0]["level"] == "working"


def test_the_colleague_dto_has_no_field_that_could_carry_evidence(seeded):
    """The type is the control, so the type is what is pinned.

    An allowlist, not a denylist: a field added to ``ConfirmedSkill`` without a
    deliberate decision fails here rather than shipping to every employee.
    """
    assert {f.name for f in fields(store.ConfirmedSkill)} == set(
        store.COLLEAGUE_VISIBLE_FIELDS
    )
    for banned in ("evidence", "evidence_ref", "rationale", "denied", "user_email"):
        assert banned not in store.COLLEAGUE_VISIBLE_FIELDS

    db = seeded
    _suggest(db, OWNER, "data_privacy")
    store.confirm(db, OWNER, "data_privacy")
    dto = store.confirmed_skills_for(db, [OWNER])[OWNER][0]
    assert isinstance(dto, store.ConfirmedSkill)
    assert not hasattr(dto, "evidence")
    assert not hasattr(dto, "evidence_ref")
    # Frozen, so a caller cannot staple the evidence back on.
    with pytest.raises(FrozenInstanceError):
        dto.level = "expert"  # type: ignore[misc]


def test_the_subject_does_see_their_own_evidence(seeded):
    """The other half of the rule: withholding it from the subject would make a
    suggestion unanswerable. They are the person the meeting was about."""
    db = seeded
    _suggest(db, OWNER, "data_privacy")

    _as(OWNER, department="Compliance")
    body = client.get(BASE).json()
    assert [s["skill_key"] for s in body["suggested"]] == ["data_privacy"]
    evidence = body["suggested"][0]["evidence"]
    assert evidence and evidence[0]["title"] == SECRET_TITLE
    assert body["suggested"][0]["rationale"] == SECRET_RATIONALE


# ═════════════════════════════════════════════════════════════════════════════
# 2. An unconfirmed inference is not a fact
# ═════════════════════════════════════════════════════════════════════════════


def test_an_unconfirmed_inference_is_excluded_from_the_confirmed_read(seeded):
    """Four rows, one of each kind, read three ways.

    The denial is the trap: it lives in the same ``confirmed = False`` state as
    a pending suggestion, which is exactly why ``confirmed.is_(True)`` is the
    only correct predicate and ``source != "inferred"`` is not.
    """
    db = seeded
    store.record_declared_skills(db, OWNER, {"data_privacy": "strong"})
    _suggest(db, OWNER, "financial_reporting")
    store.confirm(db, OWNER, "financial_reporting")
    _suggest(db, OWNER, "quality_assurance")  # left unanswered
    _suggest(db, OWNER, "audit_readiness")
    store.deny(db, OWNER, "audit_readiness", denied_by=OWNER)

    _as(COLLEAGUE, department="Sales")
    keys = [
        s["skill_key"] for s in client.get(f"{BASE}/people/{OWNER}").json()["skills"]
    ]
    assert sorted(keys) == ["data_privacy", "financial_reporting"], (
        "the colleague read surfaced a question we asked as a fact"
    )

    # The subject still sees all four states, split by what they mean to them.
    _as(OWNER, department="Compliance")
    own = client.get(BASE).json()
    assert [s["skill_key"] for s in own["declared"]] == ["data_privacy"]
    assert [s["skill_key"] for s in own["accepted_suggestions"]] == [
        "financial_reporting"
    ]
    assert [s["skill_key"] for s in own["suggested"]] == ["quality_assurance"]
    assert [s["skill_key"] for s in own["denied"]] == ["audit_readiness"]


def test_the_suggestion_notice_names_the_company_from_its_profile(seeded):
    """Spec §4.1: "nothing outside {company} was looked at" names the
    workspace's company once its profile is saved, and says "your company"
    until then — never "None", a blank or a raw token."""
    from rubik_core.company.profile import CompanyProfile, save_company_profile

    _as(OWNER, department="Compliance")
    notice = client.get(BASE).json()["suggestion_notice"]
    assert "Nothing outside your company was looked at." in notice
    assert "None" not in notice and "{" not in notice

    save_company_profile(seeded, CompanyProfile(name="Acme Corp"))
    notice = client.get(BASE).json()["suggestion_notice"]
    assert "Nothing outside Acme Corp was looked at." in notice


def test_a_person_with_no_skills_is_an_ordinary_complete_answer(seeded):
    """Empty is not a gap. Nothing in the payload is shaped like a to-do."""
    _as(OWNER, department="Compliance")
    own = client.get(BASE).json()
    assert own["declared"] == own["suggested"] == own["denied"] == []
    assert own["accepted_suggestions"] == []
    for forbidden in ("missing", "incomplete", "todo", "pending_action", "nag"):
        assert forbidden not in json.dumps(own).lower()

    _as(COLLEAGUE, department="Sales")
    assert client.get(f"{BASE}/people/{OWNER}").json()["skills"] == []


# ═════════════════════════════════════════════════════════════════════════════
# 3 + 4. The sweep: a denial sticks, and an unchanged source costs nothing
# ═════════════════════════════════════════════════════════════════════════════


class _StubReader:
    """One reader returning a fixed evidence set. Real assembly runs on top."""

    name = "meetings"

    def __init__(self, items):
        self._items = tuple(items)
        self.calls = 0

    def collect(self, user_email, *, since):
        self.calls += 1
        return inference.ReaderResult(
            name=self.name, items=self._items, examined=len(self._items)
        )


class _YesJudge:
    """Says yes to every candidate offered, and counts how often it was asked.

    A judge that always agrees is the adversarial case for the denial rule: if
    anything reaches the model after a denial, this stub will say yes to it.
    """

    def __init__(self):
        self.calls = 0
        self.offered: list[tuple[str, ...]] = []

    def __call__(self, candidates, *, names, **kwargs):
        self.calls += 1
        self.offered.append(tuple(c.skill_key for c in candidates))
        return [
            inference.Judgement(
                skill_key=c.skill_key, holds=True, level="working", why="Did the work."
            )
            for c in candidates
        ]


def _evidence_for(skill_key: str, phrase: str, *, refs: tuple[str, ...]):
    """Two citations for one skill — ``MIN_ITEMS_PER_CANDIDATE`` is 2."""
    return [
        inference.EvidenceItem(
            kind=inference.KIND_MEETING,
            ref=ref,
            title=SECRET_TITLE,
            phrase=phrase,
            excerpt=f"... {phrase} ...",
            when="2026-08-01T00:00:00",
            basis=inference.MEETING_BASIS_CAVEAT,
        )
        for ref in refs
    ]


@pytest.fixture
def sweep(seeded):
    """A sweep wired to one stub reader, one stub judge, and this session."""
    db = seeded
    judge = _YesJudge()
    reader = _StubReader(
        _evidence_for("gaap", "gaap", refs=("mtg-1", "mtg-2"))
        + _evidence_for("data privacy", "data privacy", refs=("mtg-3", "mtg-4"))
    )

    def run():
        return inference.run_sweep(
            emails=[OWNER],
            judge=judge,
            readers=[reader],
            session_factory=lambda: db,
            seed=False,
        )

    return run, judge, reader, db


def test_a_denied_skill_is_not_re_suggested_by_a_second_sweep(sweep):
    """The headline guarantee, proven by RUNNING THE SWEEP TWICE.

    Asserting ``upsert_inference(...) == SKIPPED_DENIED`` would only prove the
    store's rule. What has to hold is the whole pipeline: after a denial, a
    second sweep over the SAME evidence with a judge that says yes to everything
    must not put that suggestion back in front of the person. Re-suggesting
    something somebody explicitly rejected is the fastest way to get a feature
    switched off.
    """
    run, judge, _reader, db = sweep

    first = run()
    assert first.suggested == 2
    assert sorted(judge.offered[0]) == ["data_privacy", "financial_reporting"]

    store.deny(db, OWNER, "financial_reporting", denied_by=OWNER)
    row = store.subject_row_for(db, OWNER, "financial_reporting")
    assert store.is_denied(row) is True
    assert bool(row.confirmed) is False
    assert row.level is None

    second = run()

    # Non-vacuity: the second pass really did run the model. The denial CHANGED
    # the candidate corpus, so the idempotency skip does not apply — which is
    # what makes this the adversarial case rather than an accident of caching.
    assert judge.calls == 2, "the second sweep never reached the judge at all"
    assert second.people[0].skipped_unchanged is False
    assert judge.offered[-1] == ("data_privacy",)

    # The denied skill was never even OFFERED to the model on the second pass:
    # `build_candidates` drops it, so it costs no tokens either.
    assert "financial_reporting" not in judge.offered[-1], (
        "a denied skill reached the model again — build_candidates did not "
        "exclude the tombstone"
    )
    # And the write path refuses independently, which is the actual guarantee.
    assert (
        store.upsert_inference(
            db,
            store.Suggestion(
                user_email=OWNER,
                skill_key="financial_reporting",
                level="expert",
                rationale="Surely this time.",
                items=({"kind": "meeting", "ref": "mtg-1"},),
            ),
        )
        == store.SKIPPED_DENIED
    )
    # The row is still exactly a tombstone, and still invisible to colleagues.
    row = store.subject_row_for(db, OWNER, "financial_reporting")
    assert store.is_denied(row) is True
    assert bool(row.confirmed) is False
    assert row.level is None
    assert [s.skill_key for s in store.confirmed_skills_for(db, [OWNER])[OWNER]] == []
    assert second.suggested == 0


def test_a_denied_skill_stays_denied_through_the_endpoint_too(seeded):
    """The same rule from the outside: deny, then press refresh, then read."""
    db = seeded
    _suggest(db, OWNER, "financial_reporting")

    _as(OWNER, department="Compliance")
    assert client.post(f"{BASE}/financial_reporting/deny").status_code == 200
    own = client.get(BASE).json()
    assert [s["skill_key"] for s in own["denied"]] == ["financial_reporting"]
    assert own["suggested"] == []
    assert own["denied"][0]["denied_at"]

    # Denying twice is a no-op, not an error.
    assert client.post(f"{BASE}/financial_reporting/deny").status_code == 200

    # A colleague sees nothing at all — a denial is an answer that is nobody
    # else's business.
    _as(COLLEAGUE, department="Sales")
    assert client.get(f"{BASE}/people/{OWNER}").json()["skills"] == []


def test_the_sweep_is_idempotent_over_the_same_source(sweep):
    """An unchanged evidence corpus costs no model call and writes nothing.

    This is what makes a nightly company-wide pass affordable: one Vertex call
    per person whose evidence CHANGED, not per person.
    """
    run, judge, _reader, db = sweep

    first = run()
    assert judge.calls == 1
    assert first.suggested == 2
    updated_at = {
        row.skill_key: row.updated_at for row in store.subject_rows_for(db, OWNER)
    }

    second = run()
    assert judge.calls == 1, (
        "the second sweep called the model again over a byte-identical corpus"
    )
    assert second.people[0].skipped_unchanged is True
    assert second.suggested == 0
    assert {
        row.skill_key: row.updated_at for row in store.subject_rows_for(db, OWNER)
    } == updated_at, "an unchanged sweep churned updated_at"


def test_every_inferred_row_carries_evidence(sweep):
    """A suggestion with no citation is a confident sentence about a person
    built on nothing, and it is their name attached to it."""
    run, _judge, _reader, db = sweep
    run()

    rows = store.subject_rows_for(db, OWNER)
    assert rows
    for row in rows:
        if row.source != store.SOURCE_INFERRED:
            continue
        items = store.evidence_items(row)
        assert items, f"{row.skill_key} was suggested with no evidence"
        assert row.evidence_ref, f"{row.skill_key} has no evidence_ref"
        for item in items:
            assert item.get("kind") and item.get("ref")

    # Refused at construction, so it cannot be written in the first place.
    with pytest.raises(store.SkillError) as excinfo:
        store.Suggestion(user_email=OWNER, skill_key="financial_reporting", items=())
    assert "evidence" in str(excinfo.value)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Only ever your own skills
# ═════════════════════════════════════════════════════════════════════════════


def test_another_person_cannot_confirm_deny_or_declare_my_skills(seeded):
    """There is no request shape that names a subject, so a colleague acting on
    "my" skill can only ever write their own row — and here they have no
    suggestion to confirm, so they get a 404 and the owner's row is untouched.
    """
    db = seeded
    _suggest(db, OWNER, "data_privacy")

    _as(COLLEAGUE, department="Sales")
    assert client.post(f"{BASE}/data_privacy/confirm").status_code == 404

    # Nothing was written for the colleague, and the owner's row is unchanged.
    assert store.subject_rows_for(db, COLLEAGUE) == []
    owner_row = store.subject_row_for(db, OWNER, "data_privacy")
    assert bool(owner_row.confirmed) is False

    # A colleague's deny writes a tombstone on THEIR OWN row, not the owner's.
    assert client.post(f"{BASE}/data_privacy/deny").status_code == 200
    assert store.is_denied(store.subject_row_for(db, COLLEAGUE, "data_privacy"))
    assert not store.is_denied(store.subject_row_for(db, OWNER, "data_privacy"))

    # And a colleague's declaration lands on their own row too.
    assert (
        client.post(
            f"{BASE}/declare", json={"skills": {"financial_reporting": "working"}}
        ).status_code
        == 200
    )
    assert store.subject_row_for(db, COLLEAGUE, "financial_reporting") is not None
    assert store.subject_row_for(db, OWNER, "financial_reporting") is None


def test_no_write_endpoint_accepts_a_subject_from_the_request(seeded):
    """Structural, not behavioural: an accepted ``user_email`` would be a plain
    impersonation primitive over this feature, so no schema may have one.

    Checked against the OpenAPI document rather than by reading the source, so
    a new endpoint on this router is covered the day it lands.
    """
    spec = app.openapi()
    subjectish = {
        "user_email",
        "email",
        "subject",
        "subject_email",
        "for_user",
        "on_behalf_of",
    }
    for path, ops in spec["paths"].items():
        if not path.startswith(BASE):
            continue
        for method, op in ops.items():
            if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            blob = json.dumps(op.get("requestBody") or {})
            blob += json.dumps(
                [
                    p
                    for p in op.get("parameters", [])
                    if p.get("in") in {"query", "header"}
                ]
            )
            for name in subjectish:
                assert f'"{name}"' not in blob, (
                    f"{method.upper()} {path} accepts {name!r} from the request. "
                    "Every Smart Teams write takes its subject from the "
                    "authenticated principal — see require_smart_teams_access."
                )


def test_the_support_read_is_for_support_admins_only(seeded, monkeypatch):
    """``authz.can_view_raw_profile`` decides, and it grants exactly two
    viewers. An exec is refused: seniority must not buy somebody's evidence.
    Support is handed to the Systems team's admins here."""
    narrow_smart_teams_support(monkeypatch, "Systems")
    db = seeded
    _suggest(db, OWNER, "data_privacy")
    path = f"{BASE}/support/{OWNER}"

    _as(SYSADMIN, department="Systems", is_admin=True)
    body = client.get(path).json()
    assert [s["skill_key"] for s in body["suggested"]] == ["data_privacy"]
    assert body["suggested"][0]["evidence"][0]["title"] == SECRET_TITLE
    assert body["sweep_job_module"]

    _as(CEO, department="Executive", is_exec=True, managed=["*"])
    assert client.get(path).status_code == 403

    _as(COLLEAGUE, department="Sales")
    assert client.get(path).status_code == 403

    # An admin outside the support set is refused too.
    _as(COLLEAGUE, department="Sales", is_admin=True)
    assert client.get(path).status_code == 403

    # The subject themselves is not refused — it is their data.
    _as(OWNER, department="Compliance")
    assert client.get(path).status_code == 200


def test_the_support_read_is_attributable_in_the_logs(seeded, caplog):
    """Reading somebody's private skills leaves a record of WHO read them.

    The guide and pair support reads have logged since they shipped; this one
    and the interview/uploads reads did not, which was survivable only while no
    screen called them. Now that /smart-teams/support exists, a look is a
    routine action, and a routine look at private data that leaves no trace is
    how "who saw my answers?" becomes unanswerable.
    """
    db = seeded
    _suggest(db, OWNER, "data_privacy")
    _as(SYSADMIN, department="Systems", is_admin=True)

    with caplog.at_level(logging.INFO):
        assert client.get(f"{BASE}/support/{OWNER}").status_code == 200

    assert OWNER in caplog.text
    assert SYSADMIN in caplog.text, "the reader is not named in the log line"


def test_the_support_read_cannot_write(seeded):
    """A confirmation is a consent, and support is not consent. There is no
    endpoint a Systems admin can use to confirm on somebody's behalf."""
    spec = app.openapi()
    writable = {
        (path, method.upper())
        for path, ops in spec["paths"].items()
        if path.startswith(f"{BASE}/support")
        for method in ops
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert writable == set(), f"the support surface is not read-only: {writable}"


# ═════════════════════════════════════════════════════════════════════════════
# 6. The taxonomy is a closed list
# ═════════════════════════════════════════════════════════════════════════════


def test_an_off_taxonomy_declaration_is_refused_not_coerced(seeded):
    """400, with copy that names the path forward, and nothing written.

    The two alternatives are both worse: storing free text gives somebody a
    claim the Team Builder can never match on, and coercing onto the nearest
    curated key attributes work they never touched.
    """
    db = seeded
    _as(OWNER, department="Compliance")
    response = client.post(
        f"{BASE}/declare",
        json={
            "skills": {"data_privacy": "strong", "underwater_basket_weaving": "expert"}
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not in the curated skills list" in detail
    assert "Ask an admin" in detail
    assert "underwater_basket_weaving" in detail

    # The whole batch is validated before anything is written, so the VALID key
    # in the same request was not written either.
    assert store.subject_rows_for(db, OWNER) == []

    # A level that is not a level is refused the same way.
    assert (
        client.post(
            f"{BASE}/declare", json={"skills": {"data_privacy": "world class"}}
        ).status_code
        == 400
    )
    assert store.subject_rows_for(db, OWNER) == []


def test_declaring_a_previously_denied_skill_lifts_the_tombstone_and_says_so(seeded):
    """An explicit statement about yourself beats an earlier rejection of
    somebody else's guess — but the reversal is reported, not silent."""
    db = seeded
    store.deny(db, OWNER, "data_privacy", denied_by=OWNER)

    _as(OWNER, department="Compliance")
    body = client.post(
        f"{BASE}/declare", json={"skills": {"data_privacy": "strong"}}
    ).json()
    assert body["undenied"] == ["data_privacy"]
    assert [s["skill_key"] for s in body["declared"]] == ["data_privacy"]
    assert body["denied"] == []

    # The rejection is retained for audit rather than dropped.
    payload = store.evidence_of(store.subject_row_for(db, OWNER, "data_privacy"))
    assert store.EVIDENCE_PRIOR_DENIAL_KEY in payload


def test_the_taxonomy_search_matches_aliases_and_reports_whether_it_is_seeded(
    db_session,
):
    """Alias matching is the whole point of storing aliases: somebody typing
    "ccpa" has to find "Data privacy (GDPR / CCPA)". And an empty list has to
    be distinguishable from a table nobody has seeded yet."""
    _as(OWNER, department="Compliance")

    unseeded = client.get(f"{BASE}/taxonomy").json()
    assert unseeded["skills"] == []
    assert unseeded["seeded"] is False

    taxonomy.seed_taxonomy(db_session)
    seeded_body = client.get(f"{BASE}/taxonomy", params={"q": "ccpa"}).json()
    assert seeded_body["seeded"] is True
    assert [s["skill_key"] for s in seeded_body["skills"]] == ["data_privacy"]

    by_cluster = client.get(f"{BASE}/taxonomy", params={"cluster": "Technology"}).json()
    assert by_cluster["skills"]
    assert {s["cluster"] for s in by_cluster["skills"]} == {"Technology"}


def test_the_short_alias_bound_is_enforced_for_every_scanned_phrase():
    """Every phrase under ``MIN_SCAN_ALIAS_CHARS`` must be absent from the scan
    pattern. A two- or three-letter acronym collides with ordinary words, and a
    false positive here is a suggestion about a real person built on a
    coincidence."""
    short = [
        p for p in taxonomy.alias_index() if len(p) < taxonomy.MIN_SCAN_ALIAS_CHARS
    ]
    for phrase in short:
        assert not taxonomy.match_phrases(f"we discussed {phrase} at length"), (
            f"{phrase!r} is short enough to collide with ordinary prose but is "
            "still scanned"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 7. Wiring: the router is mounted and the job it dispatches exists
# ═════════════════════════════════════════════════════════════════════════════


def _mounted() -> dict[str, set[str]]:
    """Paths from ``app.openapi()``, not ``app.routes``.

    This FastAPI version keeps lazy ``_IncludedRouter`` objects on
    ``app.routes``, so walking that finds zero smart-teams routes and every
    assertion here would pass vacuously.
    """
    return {
        path: {method.upper() for method in ops}
        for path, ops in app.openapi()["paths"].items()
        if path.startswith(BASE)
    }


def test_the_skills_router_is_mounted_and_every_route_is_reachable():
    from backend.routers import smart_teams_skills

    table = _mounted()
    assert table, (
        "no /api/smart-teams/skills routes are mounted. Add "
        'app.include_router(smart_teams_skills.router, prefix="/api") on `app` '
        "— NOT on the copilot-gated `core` router."
    )
    declared = {
        (f"/api{route.path}", method)
        for route in smart_teams_skills.router.routes
        for method in getattr(route, "methods", set())
        & {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    assert declared
    missing = sorted(
        (path, method)
        for path, method in declared
        if method not in table.get(path, set())
    )
    assert not missing, f"smart_teams_skills is not fully mounted: {missing}"


def test_the_skills_routes_collide_with_no_other_smart_teams_route():
    """The router sits one segment deeper than the interview and uploads
    routers so its ``/{skill_key}/…`` writes cannot shadow anything on
    ``/api/smart-teams`` itself."""
    from backend.routers import smart_teams as interview_router
    from backend.routers import smart_teams_guides as guides_router
    from backend.routers import smart_teams_skills as skills_router
    from backend.routers import smart_teams_uploads as uploads_router

    seen: dict[tuple[str, str], str] = {}
    for name, router in (
        ("smart_teams", interview_router.router),
        ("smart_teams_uploads", uploads_router.router),
        ("smart_teams_guides", guides_router.router),
        ("smart_teams_skills", skills_router.router),
    ):
        for route in router.routes:
            for method in getattr(route, "methods", set()):
                key = (route.path, method)
                owner = seen.get(key)
                assert owner is None, (
                    f"{name} and {owner} both declare {method} {route.path}"
                )
                seen[key] = name


def test_the_router_gate_is_declared_on_the_router_itself():
    """So no endpoint can be added without it."""
    from backend.auth import require_smart_teams_access
    from backend.routers import smart_teams_skills

    dependencies = [
        dep.dependency for dep in (smart_teams_skills.router.dependencies or [])
    ]
    assert require_smart_teams_access in dependencies

    # And it really refuses: an off-domain caller (reachable only through local
    # mock auth) gets 403 rather than a payload.
    _as("vendor@outside.example")
    try:
        assert client.get(BASE).status_code == 403
        assert client.get(f"{BASE}/people/{OWNER}").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_the_sweep_job_is_registered_and_the_dispatched_module_exists():
    """A launch of a module that is not in the image returns 200 and does
    nothing — this feature already shipped one 202-that-did-nothing."""
    import importlib

    from backend.jobs import WORKER_JOBS, worker_job_execute_command
    from backend.routers.smart_teams_skills import SWEEP_JOB_MODULE

    entry = WORKER_JOBS["smart-teams-skill-sweep"]
    assert entry.module == SWEEP_JOB_MODULE
    assert importlib.import_module(entry.module) is not None
    assert entry.dispatched_from == "backend/routers/smart_teams_skills.py"
    assert entry.wants_cadence
    assert "backend.jobs.smart_teams_skill_sweep_job" in worker_job_execute_command(
        "smart-teams-skill-sweep", workspace="test", project="test-project"
    )


def test_the_sweep_job_argv_parses_and_the_bare_run_is_safe():
    """A bare run is legitimate here (watermark + digest + run lock make it
    cheap), unlike the three id-taking Smart Teams jobs."""
    from backend.jobs.smart_teams_skill_sweep_job import _build_parser

    bare = _build_parser().parse_args([])
    assert bare.emails is None and bare.since is None and bare.limit is None
    assert bare.no_seed is False

    args = _build_parser().parse_args(
        ["--emails", f"{OWNER}, {COLLEAGUE}", "--since", "2026-01-01", "--limit", "5"]
    )
    assert args.emails == [OWNER, COLLEAGUE]
    assert args.since.year == 2026
    assert args.limit == 5

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--since", "not-a-date"])


def test_the_refresh_endpoint_dispatches_the_sweep_for_the_caller_alone(
    seeded, monkeypatch
):
    """One person, taken from the principal. Nobody can start a sweep of
    somebody else from here."""
    calls: list[dict] = []

    def _fake_submit(background_tasks, *, module, args, fallback, label, **kw):
        calls.append({"module": module, "args": args, "label": label})
        return "job", "executions/fake"

    monkeypatch.setattr(
        "backend.jobs.launcher.submit_background_work", _fake_submit, raising=True
    )

    _as(OWNER, department="Compliance")
    response = client.post(f"{BASE}/refresh")
    assert response.status_code == 202, response.text
    assert calls == [
        {
            "module": "backend.jobs.smart_teams_skill_sweep_job",
            "args": ["--emails", OWNER],
            "label": f"smart-teams-skill-sweep:{OWNER}",
        }
    ]
    assert response.json()["user_email"] == OWNER


# ═════════════════════════════════════════════════════════════════════════════
# 8. Boundaries this feature must never cross
# ═════════════════════════════════════════════════════════════════════════════


def test_nothing_in_the_skills_stack_reaches_outside_the_company():
    """INTERNAL SIGNALS ONLY, checked by reading the source of all five files.

    ``rubik_core/people/`` (UC-47) does web research and deliberately skips
    anybody on the company's own domain. Pointing that at an employee would produce a
    dossier from pages the subject cannot correct, filed under the same heading
    as a claim they made themselves. The separation is structural, so a grep is
    a real test of it.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [
        "rubik_core/smart_teams/skills.py",
        "rubik_core/smart_teams/skill_taxonomy.py",
        "rubik_core/smart_teams/skill_inference.py",
        "backend/routers/smart_teams_skills.py",
        "backend/jobs/smart_teams_skill_sweep_job.py",
    ]
    banned = (
        "rubik_core.people",
        "import requests",
        "import httpx",
        "urllib.request",
        "BeautifulSoup",
        "serpapi",
        "google_search",
    )
    for relative in files:
        source = (root / relative).read_text()
        # Strip docstrings/comments cheaply: only import-ish lines matter here,
        # and the prose in these modules NAMES the boundary on purpose.
        code = "\n".join(
            line
            for line in source.splitlines()
            if not line.lstrip().startswith(("#", "*", '"', "'"))
        )
        for needle in banned:
            assert needle not in code, f"{relative} reaches outward via {needle!r}"


def test_the_cost_feature_tag_sits_on_the_worker_and_matches_the_admin_label():
    """``@attribute_to`` is a contextvar and does not survive a hand-off to a
    worker thread, so it belongs on the function that makes the Vertex call —
    not on ``run_sweep``. A missing or misspelled tag lands this feature's spend
    in the AI Cost tab's "Unattributed" row."""
    from backend.routers.admin import _FEATURE_LABELS

    assert inference.COST_FEATURE in _FEATURE_LABELS
    assert getattr(inference.judge_candidates, "__wrapped__", None) is not None, (
        "judge_candidates is not decorated — the one Vertex call in this "
        "feature would be billed to nobody"
    )
    # The model comes from a KEY resolved against config.py, never a literal id.
    assert inference.JUDGE_MODEL_KEY == "gemini-flash"
    source = inference.__file__
    import pathlib

    assert "gemini-2" not in pathlib.Path(source).read_text().replace(
        "gemini-flash", ""
    ), "a concrete Gemini model id is written at a call site"


def test_no_skills_prose_frames_a_person_as_promotable():
    """ADVISORY ONLY, applied to the copy this router actually emits.

    Same screen ``strengths._check_taxonomy`` runs over guide prose, run here
    because these strings render into the picker and into staffing output that
    colleagues read.

    A DISCLAIMER is exempt, and the exemption is narrow and checked rather than
    asserted. ``_ADVANCEMENT_PROSE`` matches the bare substring ``promot``, so a
    sentence whose whole job is to say the feature is NOT used for promotion
    fails it — the shipped canonical disclaimer ``disc.ADVISORY_NOTICE`` fails it
    too, which is why ``guide.py`` already carries an exemption for that exact
    string. Screening authored disclaimers with a screen calibrated for
    assertions is the same false-positive shape that permanently fails a guide
    when somebody mentions a promotional campaign, so what is asserted here is
    the property that actually matters: every advancement term in a disclaimer
    sits inside a negation.
    """
    import re

    from backend.routers import smart_teams_skills as router_module
    from rubik_core.smart_teams.instruments.disc import ADVISORY_NOTICE as CANONICAL
    from rubik_core.smart_teams.instruments.strengths import _ADVANCEMENT_PROSE

    # Copy that DESCRIBES skills gets the raw screen, unmodified.
    for name, prose in (
        ("suggestion_notice", router_module.suggestion_notice("your company")),
        ("DENIAL_NOTICE", router_module.DENIAL_NOTICE),
    ):
        hit = _ADVANCEMENT_PROSE.search(prose)
        assert hit is None, f"{name} contains advancement language: {hit}"

    for name in ("SUBJECT_ONLY_DENIED", "UNKNOWN_SKILL_DENIED"):
        prose = getattr(store, name)
        assert _ADVANCEMENT_PROSE.search(prose) is None

    # Disclaimers: every hit must be negated. Checked against the shipped
    # canonical disclaimer too, so this is a real property of the class and not
    # a hole cut for one string.
    negation = re.compile(r"\b(?:not|never|nothing|no)\b", re.IGNORECASE)
    for label, prose in (
        ("skills ADVISORY_NOTICE", router_module.ADVISORY_NOTICE),
        ("disc.ADVISORY_NOTICE", CANONICAL),
    ):
        hits = list(_ADVANCEMENT_PROSE.finditer(prose))
        assert hits, f"{label} is no longer a disclaimer — screen it directly"
        for hit in hits:
            clause = prose[max(0, hit.start() - 80) : hit.start()]
            assert negation.search(clause), (
                f"{label} says {hit.group(0)!r} without denying it: ...{clause}"
            )


def test_the_interview_answer_prefix_and_key_shape_stay_pinned_together():
    """``skills.ANSWER_KEY_PREFIX`` and ``skill_taxonomy._KEY_PATTERN`` are both
    re-stated rather than imported, so the worker does not pull the interview
    catalog. That is only safe while a test pins them to the originals."""
    from rubik_core.smart_teams import interview

    assert store.ANSWER_KEY_PREFIX == interview.PREFIX_SKILL
    assert taxonomy._KEY_PATTERN.pattern == interview._SKILL_KEY_PATTERN.pattern
    assert all(taxonomy._KEY_PATTERN.match(key) for key in taxonomy.SKILLS)


def test_declarations_from_a_saved_interview_project_onto_rows(seeded):
    """The interview stores declared skills under a ``skill:`` prefix and
    nothing materialised them into ``st_person_skills`` before ST-8."""
    db = seeded
    answers = {
        "skill:data_privacy": SKILL_LEVELS[1],
        "skill:financial_reporting": None,  # opened and left blank — not a claim
        "skill:not_a_real_skill": SKILL_LEVELS[0],
        "disc:1": "4",  # a different section entirely
    }
    declarations = store.declarations_from_answers(answers)
    assert declarations == {
        "data_privacy": SKILL_LEVELS[1],
        "not_a_real_skill": SKILL_LEVELS[0],
    }

    # And the unknown key is still refused on the way in, so a stale answer
    # cannot smuggle an off-taxonomy skill past the picker.
    with pytest.raises(store.SkillError):
        store.record_declared_skills(db, OWNER, declarations)
    assert store.subject_rows_for(db, OWNER) == []


def test_deny_then_confirm_cannot_manufacture_an_inferred_skill(seeded):
    """A pre-emptive tombstone is not a suggestion the person agreed to.

    ``deny`` legitimately creates its own row — saying "I do not have this"
    before any sweep suggests it is as durable as saying it after. ``confirm``
    only checked that a row EXISTED, so the two composed: deny a key nothing had
    suggested, then confirm it, and a colleague-visible skill appeared labelled
    ``inferred`` with no evidence behind it. The confirm docstring said that was
    impossible, which is the part that made it worth fixing rather than noting.

    ``origin`` is documented as the thing a project lead weighs — "they told us"
    versus "Cubie suggested it and they agreed" — so publishing the wrong one is
    the actual harm here, not privilege escalation. Declaring is the honest verb.
    """
    key = next(iter(taxonomy.SKILLS))

    denied = store.deny(seeded, OWNER, key)
    assert denied.confirmed is False
    assert store.is_denied(denied)

    with pytest.raises(store.SkillError, match="never suggested"):
        store.confirm(seeded, OWNER, key)

    # Still denied, and still invisible to a colleague.
    assert store.confirmed_skills_for(seeded, [OWNER])[OWNER] == ()

    # The honest path still works, and records the origin truthfully: a
    # declaration is confirmed on arrival and reads as declared, not inferred.
    store.record_declared_skills(seeded, OWNER, {key: "working"})
    row = store.subject_row_for(seeded, OWNER, key)
    assert row is not None
    assert row.confirmed is True
    assert row.source == store.SOURCE_DECLARED


def test_confirming_a_real_suggestion_after_denying_it_still_works(seeded):
    """Changing your mind about a genuine inference must remain possible.

    The guard above must not catch this: something really did suggest this skill,
    so the row carries evidence, and confirming it is the person agreeing with a
    real inference rather than inventing one.
    """
    key = next(iter(taxonomy.SKILLS))
    _suggest(seeded, OWNER, key)
    store.deny(seeded, OWNER, key)

    reconfirmed = store.confirm(seeded, OWNER, key)
    assert reconfirmed.confirmed is True
    assert reconfirmed.source == store.SOURCE_INFERRED


# ═════════════════════════════════════════════════════════════════════════════
# 8. The three process-improvement skills, and the alias that is a normal word
# ═════════════════════════════════════════════════════════════════════════════


def test_the_three_process_improvement_skills_are_distinct_and_resolvable():
    """Lean, Six Sigma and Kaizen are separate rows on purpose.

    They answer different staffing questions — flow, measured variation, running
    the improvement event — so collapsing them would make "someone who can
    measure whether the fix worked" unanswerable.
    """
    for key, expected_alias in (
        ("lean_process", "value stream mapping"),
        ("six_sigma", "dmaic"),
        ("kaizen", "gemba walk"),
    ):
        spec = taxonomy.SKILLS[key]
        assert spec.cluster == "Operations"
        assert taxonomy.resolve(expected_alias) == key
        assert taxonomy.resolve(spec.name) == key


def test_lean_is_findable_by_name_but_never_matched_in_prose():
    """The exact bug the length bound could not catch.

    ``MIN_SCAN_ALIAS_CHARS`` is a proxy for distinctiveness, and "lean" is where
    it breaks: four characters clears the bound, and it is also an everyday verb.
    A scan hit would put Lean process improvement in front of somebody as an
    AI-inferred skill on the strength of "I lean toward shipping early".

    Both halves matter. Excluded from the SCAN, kept in the INDEX — the picker's
    search box is an explicit lookup, where somebody typing "lean" means the
    methodology.
    """
    assert taxonomy.resolve("lean") == "lean_process"
    assert "lean" in taxonomy.alias_index()

    for innocent in (
        "I lean toward shipping early",
        "we leaned on the vendor for a date",
        "the report is lean on detail",
    ):
        assert not [
            h for h in taxonomy.match_phrases(innocent) if h.skill_key == "lean_process"
        ], f"{innocent!r} produced a Lean hit"

    # The unambiguous phrasings still land.
    assert [
        h.skill_key for h in taxonomy.match_phrases("we did value stream mapping")
    ] == ["lean_process"]


def test_every_scan_unsafe_alias_is_a_real_alias_and_is_excluded():
    """Guard the guard, in both directions.

    A phrase in the set that is not an alias is dead config that reads as
    protection. An alias in the set that still scans means the exclusion is not
    wired to the pattern.
    """
    index = taxonomy.alias_index()
    for phrase in taxonomy.SCAN_UNSAFE_ALIASES:
        assert phrase in index, f"{phrase!r} is not an alias — dead config"
        assert not taxonomy.match_phrases(f"we discussed {phrase} at length"), (
            f"{phrase!r} is listed scan-unsafe but is still matched"
        )


def test_no_belt_is_shipped_as_a_skill_or_alias():
    """A belt is a LEVEL, and this module ships domains only.

    Six Sigma's belts are the obvious thing to reach for when naming that skill,
    and they are exactly what the advisory guardrail forbids: a skill row says
    "you may ask me about this", never a rank.
    """
    for spec in taxonomy.SKILLS.values():
        blob = " ".join([spec.name, *spec.aliases]).lower()
        for rank in ("black belt", "green belt", "yellow belt", "master black"):
            assert rank not in blob, (
                f"{spec.skill_key} ships {rank!r}, which is a level"
            )


# ═════════════════════════════════════════════════════════════════════════════
# 9. Skills → PEOPLE (GET /holders), and admin-added skills being first-class
# ═════════════════════════════════════════════════════════════════════════════

HOLDER_A = "ana@acme.example"
HOLDER_B = "ben@acme.example"


def _roster(db, *emails: str) -> None:
    """``users`` rows, which is what ``proposable_people`` treats as employment."""
    from rubik_core.database.models import User

    for email in emails:
        db.add(
            User(
                email=email,
                id=email.split("@")[0],
                name=email.split("@")[0].title() + " Person",
                department="Systems",
            )
        )
    db.commit()


@pytest.fixture
def holders_world(seeded):
    """Two colleagues with confirmed skills, plus one admin-added skill row."""
    from rubik_core.database.smart_teams import SmartTeamsSkill

    db = seeded
    _roster(db, HOLDER_A, HOLDER_B)
    db.add(
        SmartTeamsSkill(
            skill_key="vendor_scorecards",
            name="Vendor scorecards",
            cluster="Operations",
            aliases=["vendor scorecard"],
            is_active=True,
        )
    )
    db.commit()
    store.record_declared_skills(db, HOLDER_A, {"six_sigma": "working"})
    store.record_declared_skills(db, HOLDER_B, {"six_sigma": "working"})
    store.record_declared_skills(db, HOLDER_A, {"kaizen": "working"})
    store.record_declared_skills(db, HOLDER_A, {"vendor_scorecards": "working"})
    _as(OWNER, department="Systems")
    return db


def test_holders_answers_the_question_a_project_lead_actually_asks(holders_world):
    """Skills → people, which is the inverse of the person read.

    ``/people/{email}`` only helps once you already suspect somebody; staffing
    starts from the work.
    """
    r = client.get(f"{BASE}/holders", params={"skills": "six sigma"})
    assert r.status_code == 200, r.text
    body = r.json()
    matched = body["matched"]
    assert len(matched) == 1
    assert matched[0]["skill_key"] == "six_sigma"
    assert matched[0]["name"] == "Six Sigma process improvement"
    names = [h["display_name"] for h in matched[0]["holders"]]
    assert names == sorted(names), "holders must be in name order, not ranked"
    assert {h["email"] for h in matched[0]["holders"]} == {HOLDER_A, HOLDER_B}


def test_holders_accepts_an_alias_a_key_and_reports_what_it_cannot_resolve(
    holders_world,
):
    """ "Nobody has this" and "we do not track this" are different answers."""
    r = client.get(
        f"{BASE}/holders", params={"skills": "dmaic,six_sigma,unicorn wrangling"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unknown"] == ["unicorn wrangling"]
    assert {m["skill_key"] for m in body["matched"]} == {"six_sigma"}


def test_holders_never_publishes_an_unconfirmed_suggestion(holders_world):
    """An AI guess is a question Cubie asked, not grounds to name somebody."""
    db = holders_world
    # The module's own helper, so this suggestion carries the same secret
    # evidence every other test in this file uses — which the next test asserts
    # never reaches the response.
    _suggest(db, HOLDER_B, "kaizen")
    db.commit()
    r = client.get(f"{BASE}/holders", params={"skills": "kaizen"})
    assert r.status_code == 200, r.text
    holders = r.json()["matched"][0]["holders"]
    assert {h["email"] for h in holders} == {HOLDER_A}, (
        "an unconfirmed suggestion put a name in front of a lead"
    )


def test_holders_publishes_no_evidence_for_anybody(holders_world):
    """The fan-out endpoint: one serializer slip leaks about the whole roster."""
    db = holders_world
    # Evidence has to EXIST for its absence to prove anything. A confirmed
    # inference is the dangerous case: it is published as a holder, and the
    # meeting title behind it never passed meeting_participants on the way in.
    _suggest(db, HOLDER_B, "kaizen")
    store.confirm(db, HOLDER_B, "kaizen")
    db.commit()
    assert any(
        h["email"] == HOLDER_B
        for h in client.get(f"{BASE}/holders", params={"skills": "kaizen"}).json()[
            "matched"
        ][0]["holders"]
    ), "precondition: the confirmed inference must be published as a holder"

    r = client.get(f"{BASE}/holders", params={"skills": "six sigma,kaizen"})
    blob = r.text.lower()
    for leak in ("evidence", "rationale", "excerpt", SECRET_TITLE.lower()):
        assert leak not in blob, f"{leak!r} reached a colleague-facing response"


def test_holders_finds_a_skill_an_admin_added_that_ships_in_no_release(holders_world):
    """The admin surface is pointless if the read paths cannot see its rows.

    ``seed_taxonomy`` never touches a row this module does not ship, which is
    what makes an admin row durable — and it has to be findable too.
    """
    r = client.get(f"{BASE}/holders", params={"skills": "vendor scorecard"})
    assert r.status_code == 200, r.text
    matched = r.json()["matched"]
    assert matched[0]["skill_key"] == "vendor_scorecards"
    assert matched[0]["name"] == "Vendor scorecards"
    assert [h["email"] for h in matched[0]["holders"]] == [HOLDER_A]


def test_holders_bounds_the_question_and_refuses_an_empty_one(holders_world):
    """Each term is a roster fan-out; a lead asking for eight wants Team Builder."""
    r = client.get(f"{BASE}/holders", params={"skills": ""})
    assert r.status_code == 400

    too_many = ",".join(f"skill{n}" for n in range(taxonomy_max() + 1))
    r = client.get(f"{BASE}/holders", params={"skills": too_many})
    assert r.status_code == 400
    assert str(taxonomy_max()) in r.json()["detail"]


def taxonomy_max() -> int:
    from backend.routers.smart_teams_skills import MAX_HOLDER_TERMS

    return MAX_HOLDER_TERMS


def test_an_offboarded_colleague_stops_being_named(holders_world):
    """Skills outlive employment: st_person_skills is not cleared on offboard."""
    from rubik_core.database.models import User

    db = holders_world
    db.query(User).filter(User.email == HOLDER_B).delete()
    db.commit()
    r = client.get(f"{BASE}/holders", params={"skills": "six sigma"})
    holders = r.json()["matched"][0]["holders"]
    assert {h["email"] for h in holders} == {HOLDER_A}


# ── the vocabulary the sweep infers against ──────────────────────────────────


def test_the_sweep_vocabulary_folds_in_admin_rows_but_curated_wording_wins(seeded):
    """An admin skill must be INFERRABLE, not merely declarable.

    Declaration validation already read the table, so an admin skill was
    declarable the moment it existed — but ``match_phrases`` read the module
    constant, so the AI half of the feature silently excluded exactly the skills
    somebody had curated on purpose.
    """
    from rubik_core.database.smart_teams import SmartTeamsSkill

    db = seeded
    db.add(
        SmartTeamsSkill(
            skill_key="vendor_scorecards",
            name="Vendor scorecards",
            cluster="Operations",
            aliases=["vendor scorecard"],
            is_active=True,
        )
    )
    # A stale row for a CURATED skill must not change how that skill matches.
    row = (
        db.query(SmartTeamsSkill).filter(SmartTeamsSkill.skill_key == "six_sigma").one()
    )
    row.name = "Something an old release called it"
    db.commit()

    vocab = taxonomy.vocabulary_with_db_skills(db)
    assert "vendor_scorecards" in vocab.specs
    assert [
        h.skill_key for h in vocab.match_phrases("the vendor scorecard is due")
    ] == ["vendor_scorecards"]
    assert vocab.specs["six_sigma"].name == taxonomy.SKILLS["six_sigma"].name

    # Curated matching is unchanged by the extension.
    assert [h.skill_key for h in vocab.match_phrases("financial reporting")] == [
        "financial_reporting"
    ]


def test_a_retired_admin_skill_leaves_the_sweep_vocabulary(seeded):
    """Retirement is ``is_active = False``, and it has to actually take effect."""
    from rubik_core.database.smart_teams import SmartTeamsSkill

    db = seeded
    db.add(
        SmartTeamsSkill(
            skill_key="vendor_scorecards",
            name="Vendor scorecards",
            cluster="Operations",
            aliases=["vendor scorecard"],
            is_active=False,
        )
    )
    db.commit()
    vocab = taxonomy.vocabulary_with_db_skills(db)
    assert "vendor_scorecards" not in vocab.specs


def test_an_unusable_admin_row_is_dropped_rather_than_killing_the_sweep(seeded):
    """One bad row must not lose a nightly run that would otherwise succeed."""
    from rubik_core.database.smart_teams import SmartTeamsSkill

    db = seeded
    db.add(
        SmartTeamsSkill(
            skill_key="ok_row",
            name="Fine",
            cluster="Nonexistent cluster",
            aliases=[],
            is_active=True,
        )
    )
    db.commit()
    vocab = taxonomy.vocabulary_with_db_skills(db)
    assert "ok_row" not in vocab.specs
    assert len(vocab.specs) == len(taxonomy.SKILLS)


# ═════════════════════════════════════════════════════════════════════════════
# 10. Curation: adding a skill without a deploy
# ═════════════════════════════════════════════════════════════════════════════

ADMIN_BASE = f"{BASE}/admin/taxonomy"
NEW = {
    "skill_key": "vendor_scorecards",
    "name": "Vendor scorecards",
    "cluster": "Operations",
    "aliases": ["vendor scorecard", "vendor review"],
}


def _as_curator() -> None:
    """A support admin (every admin, by default) — the only principal that may
    curate."""
    _as(SYSADMIN, department="Systems", is_admin=True)


@pytest.mark.parametrize(
    "label,kwargs",
    [
        ("a plain employee", {"department": "Systems"}),
        (
            "an admin outside the support set",
            {"department": "Compliance", "is_admin": True},
        ),
        ("an exec", {"department": "Compliance", "is_exec": True}),
        (
            "a team manager",
            {"department": "Compliance", "managed": ["Systems"]},
        ),
    ],
)
def test_only_a_support_admin_may_curate(seeded, label, kwargs, monkeypatch):
    """The vocabulary is company-wide and its rows cannot be deleted.

    Execs and Team Managers are refused for the reason ``is_support_admin``
    gives: seniority over people is not authority over the vocabulary those
    people get described in. Support is handed to the Systems team's admins
    here, so an admin outside it is refused as well.
    """
    narrow_smart_teams_support(monkeypatch, "Systems")
    _as(COLLEAGUE, **kwargs)
    for method, url, body in (
        ("get", ADMIN_BASE, None),
        ("post", ADMIN_BASE, NEW),
        ("patch", f"{ADMIN_BASE}/whatever", {"is_active": False}),
    ):
        r = (
            getattr(client, method)(url, json=body)
            if body
            else getattr(client, method)(url)
        )
        assert r.status_code == 403, f"{label} reached {method.upper()} {url}"


def test_a_curated_skill_is_created_searchable_and_inferrable(seeded):
    """The whole point: usable immediately, without a release."""
    db = seeded
    _as_curator()
    r = client.post(ADMIN_BASE, json=NEW)
    assert r.status_code == 201, r.text
    assert r.json()["skill"]["managed_in_code"] is False

    # Its own NAME resolves, not just the aliases it was given — phrases_for
    # adds the implicit ones, exactly as the seeder does for a curated row.
    vocab = taxonomy.vocabulary_with_db_skills(db)
    assert vocab.resolve("Vendor scorecards") == "vendor_scorecards"
    assert vocab.resolve("vendor review") == "vendor_scorecards"
    # And the sweep can now infer it from prose.
    assert [
        h.skill_key for h in vocab.match_phrases("the vendor scorecard slipped")
    ] == ["vendor_scorecards"]


def test_a_curated_skill_survives_the_next_seed(seeded):
    """``seed_taxonomy`` never touches a row the module does not ship."""
    db = seeded
    _as_curator()
    assert client.post(ADMIN_BASE, json=NEW).status_code == 201
    taxonomy.seed_taxonomy(db)
    row = db.get(SmartTeamsSkill, "vendor_scorecards")
    assert row is not None and row.is_active
    assert row.name == "Vendor scorecards"


def test_creation_reports_every_problem_at_once(seeded):
    """An admin filling in a form should not find four mistakes in four tries."""
    _as_curator()
    r = client.post(
        ADMIN_BASE,
        json={
            "skill_key": "Not A Slug",
            "name": "Ready for promotion",
            "cluster": "Nonexistent",
            "aliases": ["gaap"],
        },
    )
    assert r.status_code == 400, r.text
    problems = r.json()["detail"]["problems"]
    assert len(problems) >= 3, problems
    blob = " ".join(problems).lower()
    assert "slug" in blob
    assert "cluster" in blob
    assert "gaap" in blob, "an alias that already means something else was allowed"


def test_an_alias_that_already_means_something_else_is_refused(seeded):
    """A phrase resolves to one key; the loser would be iteration order."""
    _as_curator()
    r = client.post(
        ADMIN_BASE,
        json={**NEW, "aliases": ["value stream mapping"]},
    )
    assert r.status_code == 400
    assert "lean_process" in " ".join(r.json()["detail"]["problems"])


def test_the_advisory_screen_runs_on_a_curated_name(seeded):
    """A skill says "ask me about this", never that somebody is good at it."""
    _as_curator()
    # A promotion judgement, caught by the shared advancement screen.
    r = client.post(ADMIN_BASE, json={**NEW, "name": "QC review for promotion"})
    assert r.status_code == 400
    assert "advisory" in " ".join(r.json()["detail"]["problems"]).lower()

    # A LEVEL, caught by the taxonomy's own screen. Separate rule, separate
    # message: st_person_skills.level already carries how well somebody knows
    # something, on a scale ("familiar".."expert") a name like this contradicts.
    for name in (
        "Senior-level code review",
        "Expert code review",
        "Code review black belt",
    ):
        r = client.post(ADMIN_BASE, json={**NEW, "name": name})
        assert r.status_code == 400, f"{name!r} was accepted"
        assert "level" in " ".join(r.json()["detail"]["problems"]).lower()

    # ...and a real job title that merely looks like one is NOT refused.
    r = client.post(
        ADMIN_BASE,
        json={
            **NEW,
            "skill_key": "master_scheduling",
            "name": "Master scheduler oversight",
        },
    )
    assert r.status_code == 201, r.text


def test_a_shipped_skill_cannot_be_redefined_or_edited(seeded):
    """The seeder would overwrite it, so a refusal beats a silent revert."""
    _as_curator()
    r = client.post(
        ADMIN_BASE,
        json={**NEW, "skill_key": "six_sigma", "aliases": []},
    )
    assert r.status_code == 400
    assert "ships in the app" in " ".join(r.json()["detail"]["problems"])

    r = client.patch(f"{ADMIN_BASE}/six_sigma", json={"name": "Renamed"})
    assert r.status_code == 409
    assert "skill_taxonomy.py" in r.json()["detail"]


def test_a_duplicate_key_says_whether_to_reactivate(seeded):
    """A retired row is the thing to reactivate, not to duplicate."""
    _as_curator()
    assert client.post(ADMIN_BASE, json=NEW).status_code == 201
    r = client.post(ADMIN_BASE, json=NEW)
    assert r.status_code == 409
    assert "reactivate" not in r.json()["detail"]

    assert (
        client.patch(f"{ADMIN_BASE}/vendor_scorecards", json={"is_active": False})
    ).status_code == 200
    r = client.post(ADMIN_BASE, json=NEW)
    assert r.status_code == 409
    assert "reactivate" in r.json()["detail"]


def test_retiring_hides_a_skill_from_the_sweep_but_keeps_the_claims(seeded):
    """Retirement is the only removal — a claimed row cannot be deleted."""
    db = seeded
    _roster(db, HOLDER_A)
    _as_curator()
    assert client.post(ADMIN_BASE, json=NEW).status_code == 201
    store.record_declared_skills(db, HOLDER_A, {"vendor_scorecards": "working"})
    db.commit()

    r = client.patch(f"{ADMIN_BASE}/vendor_scorecards", json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["skill"]["is_active"] is False
    assert "vendor_scorecards" not in taxonomy.vocabulary_with_db_skills(db).specs
    # The claim is still on file.
    assert (
        db.query(SmartTeamsPersonSkill)
        .filter(SmartTeamsPersonSkill.skill_key == "vendor_scorecards")
        .count()
        == 1
    )


def test_the_curation_list_shows_retired_rows_and_who_owns_the_wording(seeded):
    """An admin who cannot see a retired row cannot understand the 409."""
    db = seeded
    _roster(db, HOLDER_A)
    _as_curator()
    client.post(ADMIN_BASE, json=NEW)
    store.record_declared_skills(db, HOLDER_A, {"vendor_scorecards": "working"})
    client.patch(f"{ADMIN_BASE}/vendor_scorecards", json={"is_active": False})

    body = client.get(ADMIN_BASE).json()
    by_key = {s["skill_key"]: s for s in body["skills"]}
    assert by_key["vendor_scorecards"]["is_active"] is False
    assert by_key["vendor_scorecards"]["managed_in_code"] is False
    assert by_key["vendor_scorecards"]["claims"] == 1
    assert by_key["six_sigma"]["managed_in_code"] is True
    assert body["clusters"] == list(taxonomy.CLUSTERS)


# ═════════════════════════════════════════════════════════════════════════════
# 11. --limit must not starve the tail of the roster
# ═════════════════════════════════════════════════════════════════════════════


def test_a_capped_run_walks_down_the_roster_instead_of_redoing_the_prefix(seeded):
    """The bug: ``people[:limit]`` sliced the SORTED roster before the
    unchanged-check, so a capped nightly run swept the alphabetically first N
    for ever and never reached anybody else — silently, and while the flag's own
    help text promised the opposite.

    Proven by running twice. The cap counts people PROCESSED, and somebody whose
    evidence has not changed costs a read and no Vertex call, so the second run
    must spend its budget on people the first run never got to.
    """
    db = seeded
    everyone = [f"p{n}@acme.example" for n in range(1, 7)]
    _roster(db, *everyone)
    judge = _YesJudge()
    reader = _StubReader(_evidence_for("gaap", "gaap", refs=("mtg-1", "mtg-2")))

    def run():
        return inference.run_sweep(
            judge=judge,
            readers=[reader],
            session_factory=lambda: db,
            seed=False,
            limit=2,
        )

    first = run()
    seen_first = [p.user_email for p in first.people if not p.skipped_unchanged]
    assert len(seen_first) == 2, seen_first
    assert first.limit_stopped_early is True

    second = run()
    seen_second = [p.user_email for p in second.people if not p.skipped_unchanged]
    assert len(seen_second) == 2, seen_second
    assert not set(seen_first) & set(seen_second), (
        "the capped run redid the same people — the roster tail is starving: "
        f"{seen_first} then {seen_second}"
    )

    third = run()
    seen_third = [p.user_email for p in third.people if not p.skipped_unchanged]
    assert not set(seen_third) & (set(seen_first) | set(seen_second))
    # Six people, two per run: after three runs everybody has been reached.
    assert set(seen_first) | set(seen_second) | set(seen_third) == set(everyone)


def test_an_uncapped_run_is_not_marked_as_stopped_early(seeded):
    """A partial pass must be distinguishable from a complete one in the log."""
    db = seeded
    _roster(db, "solo@acme.example")
    report = inference.run_sweep(
        judge=_YesJudge(),
        readers=[_StubReader(_evidence_for("gaap", "gaap", refs=("mtg-1", "mtg-2")))],
        session_factory=lambda: db,
        seed=False,
    )
    assert report.limit_stopped_early is False
