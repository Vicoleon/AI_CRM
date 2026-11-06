"""The EOS identity join must fold case — on disk, on write, and on read.

``eos_people.email`` is the soft bridge to ``users.email`` and THE join for
every EOS identity lookup, but Google hands us a lower-cased address while the
column was historically written verbatim. A row stored as
``Opratt@acme.example`` therefore matched nothing: the caller resolved to
no ``Person``, ``_compute_scope`` returned empty ``owner_pids``, and they lost
every owner / direct-report / leadership grant — a department-only EOS surface
and, because the priorities EOS agent early-returns on empty ``owner_pids``, no
EOS content at all in AI Priorities. Silently: no error, the feed just goes
quiet.

Two sides have to hold, and each gets a test below:
  * READ  — ``person_by_email`` folds both sides (defence against a row
            written around the ORM).
  * WRITE — ``Person._normalize_email`` lower-cases on the way in.

(The rows already on disk were folded once by a backfill migration. Every
database now starts from the single baseline, empty, so that step retired
with the old chain.)

Legacy rows are seeded with raw SQL on purpose: going through the ORM would
hit the validator and silently fix the very thing under test.

No mocks — a temp SQLite DB per test, endpoint-level through TestClient.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

# The caller as Google authenticates them: always lower-case.
GOOGLE_EMAIL = "opratt@acme.example"
# The same human as their eos_people row was actually stored.
LEGACY_EMAIL = "Opratt@acme.example"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_person_email_case.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _profile(email: str = GOOGLE_EMAIL, *, department: str = "Sales") -> UserProfile:
    now = "2026-08-11T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name="M Pratt",
        roles=["user"],
        department=department,
        is_exec=False,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _insert_legacy_person(db, email: str) -> uuid.UUID:
    """Write an eos_people row with its casing preserved.

    Raw SQL by design — ``Person(email=...)`` would run the normalizing
    validator and store the fixed value, so an ORM insert cannot reproduce the
    bug this module exists to pin down.
    """
    pid = uuid.uuid4()
    db.execute(
        sa.text(
            """
            INSERT INTO eos_people
                (id, first_name, last_name, email, is_deleted,
                 created_at, updated_at, created_by)
            VALUES
                (:id, 'M', 'Pratt', :email, 0, :now, :now, 'seed')
            """
        ),
        {"id": str(pid), "email": email, "now": datetime(2026, 8, 11)},
    )
    db.commit()
    return pid


def _seed_legacy_person_with_content() -> uuid.UUID:
    """A legacy mixed-case Person owning one Rock / Issue / To-Do."""
    from rubik_core.database.eos import (
        Issue,
        Organization,
        Quarter,
        Rock,
        Todo,
        Visibility,
    )
    from rubik_core.database.models import Department

    db = _session()
    try:
        db.add(Department(name="Sales"))
        org = Organization(name="Acme", created_by="seed")
        db.add(org)
        db.commit()
        db.refresh(org)

        quarter = Quarter(
            organization_id=org.id,
            name="Q3-2026",
            start_date=datetime(2026, 7, 1),
            end_date=datetime(2026, 9, 30),
            is_current=True,
            created_by="seed",
        )
        db.add(quarter)
        db.commit()
        db.refresh(quarter)

        pid = _insert_legacy_person(db, LEGACY_EMAIL)

        db.add_all(
            [
                Rock(
                    description="legacy-owner rock",
                    owner_id=pid,
                    quarter_id=quarter.id,
                    visibility=Visibility.INDIVIDUAL,
                    created_by="seed",
                ),
                Issue(
                    description="legacy-owner issue",
                    owner_id=pid,
                    department_name="Sales",
                    created_by="seed",
                ),
                Todo(
                    description="legacy-owner todo",
                    owner_id=pid,
                    department_name="Sales",
                    created_by="seed",
                ),
            ]
        )
        db.commit()
        return pid
    finally:
        db.close()


# ---- READ: the ACL join folds case ----------------------------------------


def test_person_by_email_matches_a_legacy_mixed_case_row():
    """The regression itself: lower-case caller, mixed-case row on disk."""
    from backend.routers.eos._authz import person_by_email

    db = _session()
    try:
        pid = _insert_legacy_person(db, LEGACY_EMAIL)
        found = person_by_email(db, GOOGLE_EMAIL)
        assert found is not None, "lower-cased caller must match the stored row"
        assert found.id == pid
        # And the reverse direction — a mixed-case argument against a
        # lower-cased row, which is what the payload-sourced call sites pass.
        assert person_by_email(db, LEGACY_EMAIL).id == pid
    finally:
        db.close()


def test_compute_scope_gives_a_legacy_row_its_owner_grant_back():
    """Empty ``owner_pids`` is the failure mode everything downstream keys on."""
    from backend.routers.eos._authz import _compute_scope

    pid = _seed_legacy_person_with_content()
    db = _session()
    try:
        scope = _compute_scope(_profile(), db)
        assert pid in scope.owner_pids, (
            "a mixed-case eos_people row must still resolve to an owner grant; "
            "empty owner_pids is what silently emptied this user's EOS surface"
        )
    finally:
        db.close()


def test_a_legacy_row_still_sees_its_own_rock_issue_and_todo():
    """The user-visible outcome, end to end through the real endpoints."""
    _seed_legacy_person_with_content()
    _as(_profile())

    rocks = {r["description"] for r in client.get("/api/eos/rocks/").json()}
    issues = {r["description"] for r in client.get("/api/eos/issues/").json()}
    todos = {r["description"] for r in client.get("/api/eos/todos/").json()}

    assert "legacy-owner rock" in rocks
    assert "legacy-owner issue" in issues
    assert "legacy-owner todo" in todos


def test_a_legacy_row_gets_eos_content_in_ai_priorities():
    """The headline symptom: ``EosChannelAgent`` early-returns ``[]`` the moment
    ``_compute_scope`` yields empty ``owner_pids``, so a mixed-case row meant
    NO EOS content in the briefing at all — not a smaller feed, none."""
    from rubik_core.database.models import User
    from rubik_core.priorities.agents.eos_agent import EosChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig

    _seed_legacy_person_with_content()

    db = _session()
    try:
        db.add(
            User(
                id="opratt",
                email=GOOGLE_EMAIL,
                name="M Pratt",
                department="Sales",
                is_exec=False,
            )
        )
        db.commit()
    finally:
        db.close()

    records = EosChannelAgent(
        user_email=GOOGLE_EMAIL, config=PrioritiesConfig()
    ).fetch_signals()

    titles = " ".join(r.get("title", "") for r in records)
    assert records, "the EOS channel came back empty — the exact reported bug"
    assert "legacy-owner rock" in titles
    assert "legacy-owner issue" in titles
    assert "legacy-owner todo" in titles


def test_person_by_email_ignores_blank_and_whitespace():
    from backend.routers.eos._authz import person_by_email

    db = _session()
    try:
        _insert_legacy_person(db, LEGACY_EMAIL)
        assert person_by_email(db, None) is None
        assert person_by_email(db, "") is None
        assert person_by_email(db, "   ") is None
        # Surrounding whitespace is trimmed, not treated as a different address.
        assert person_by_email(db, f"  {GOOGLE_EMAIL} ") is not None
    finally:
        db.close()


def test_a_soft_deleted_row_still_does_not_match():
    """Case-folding must not quietly resurrect a retired Person."""
    from backend.routers.eos._authz import person_by_email

    db = _session()
    try:
        pid = _insert_legacy_person(db, LEGACY_EMAIL)
        db.execute(
            sa.text("UPDATE eos_people SET is_deleted = 1 WHERE id = :id"),
            {"id": str(pid)},
        )
        db.commit()
        assert person_by_email(db, GOOGLE_EMAIL) is None
    finally:
        db.close()


# ---- WRITE: the model normalizes on the way in ------------------------------


def test_person_email_is_lower_cased_on_write():
    from rubik_core.database.eos import Person

    db = _session()
    try:
        p = Person(
            first_name="M", last_name="Pratt", email=LEGACY_EMAIL, created_by="t"
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        assert p.email == GOOGLE_EMAIL

        # Reassignment normalizes too — not just the constructor.
        p.email = "  OTHER@ACME.example  "
        db.commit()
        db.refresh(p)
        assert p.email == "other@acme.example"
    finally:
        db.close()
