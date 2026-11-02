"""Exec-team confidentiality on the AI surfaces (Chat + AI Priorities + RAG).

THE rule: nothing tagged Exec team may reach a user who
is NOT on the Exec team via AI Priorities or Chat. Being a **Cubie admin**
(``roles=["admin"]``) is an app-super-user role — it does NOT grant access to
Exec-team-confidential content. Exec-team membership is ``UserProfile.is_exec``
/ ``User.is_exec`` only.

Regression for the leak where a non-exec admin's AI-Priorities briefing surfaced
an Exec-team Rock ("Your EOS Rock … is off-track"). Root causes:
  * ``eos_agent`` relied on ``visible_*_ids`` ceilings that return ``None`` for
    admins (``_authz._is_unrestricted``) → fence disabled for admins.
  * ``alignment_context`` (chat system prompt) surfaced company Rocks + off-track
    Scorecard metrics with no exec filter at all — to every user.
  * ``rags_for_user_email`` handed admins every corpus, including the exec-only
    VTO / eos_exec RAGs.

No mocks for the DB — a temp SQLite DB per test.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    from rubik_core.config import settings

    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'exec_fence.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


def _seed_eos_with_exec_rows(owner_email: str) -> None:
    """One current-quarter org with an Exec-tagged Rock/Todo/Metric (owned by
    ``owner_email``) and a plain non-exec Rock/Metric. Also seeds a ``User``
    row so ``_user_team_scope`` can resolve the caller."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import (
        Organization,
        Person,
        Quarter,
        Rock,
        ScorecardMetric,
        Todo,
        Visibility,
    )

    db = SessionLocal()
    try:
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
        person = Person(
            first_name="Owner", last_name="One", email=owner_email, created_by="seed"
        )
        db.add(person)
        db.commit()
        db.refresh(quarter)
        db.refresh(person)

        db.add_all(
            [
                Rock(
                    description="EXEC ROCK auditor readiness",
                    is_exec=True,
                    visibility=Visibility.COMPANY,
                    owner_id=person.id,
                    quarter_id=quarter.id,
                    created_by="seed",
                ),
                Rock(
                    description="PLAIN company rock",
                    is_exec=False,
                    visibility=Visibility.COMPANY,
                    owner_id=person.id,
                    quarter_id=quarter.id,
                    created_by="seed",
                ),
                ScorecardMetric(
                    name="EXEC METRIC",
                    is_exec=True,
                    on_track=False,
                    owner_id=person.id,
                    created_by="seed",
                ),
                ScorecardMetric(
                    name="PLAIN METRIC",
                    is_exec=False,
                    on_track=False,
                    owner_id=person.id,
                    created_by="seed",
                ),
                Todo(
                    description="EXEC TODO",
                    is_exec=True,
                    owner_id=person.id,
                    created_by="seed",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()


def _seed_user(email: str, *, is_exec: bool, department: str = "Systems") -> None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import User

    db = SessionLocal()
    try:
        db.add(
            User(
                id=email.split("@")[0],
                email=email,
                name=email.split("@")[0].title(),
                department=department,
                is_exec=is_exec,
            )
        )
        db.commit()
    finally:
        db.close()


# ── Chat: alignment_context ────────────────────────────────────────────────


def test_alignment_hides_exec_content_from_non_exec_admin():
    """A non-exec admin who even *owns* the Exec rock gets none of the Exec
    content in their chat alignment block."""
    from rubik_core.agents import alignment_context as ac
    from rubik_core.database.db import SessionLocal

    _seed_eos_with_exec_rows("admin@x.example")
    _seed_user("admin@x.example", is_exec=False)

    db = SessionLocal()
    try:
        payload = ac.build_alignment_payload("admin@x.example", db)
    finally:
        db.close()

    assert payload is not None
    company = [r.description for r in payload.company_rocks]
    user_rocks = [r.description for r in payload.user_rocks]
    metrics = [m.name for m in payload.off_track_metrics]
    todos = [t.description for t in payload.user_todos_this_week]

    assert "EXEC ROCK auditor readiness" not in company
    assert "EXEC ROCK auditor readiness" not in user_rocks
    assert "PLAIN company rock" in company  # non-exec content still flows
    assert "EXEC METRIC" not in metrics
    assert "PLAIN METRIC" in metrics
    assert "EXEC TODO" not in todos


def test_alignment_shows_exec_content_to_exec_member():
    from rubik_core.agents import alignment_context as ac
    from rubik_core.database.db import SessionLocal

    _seed_eos_with_exec_rows("owner@x.example")
    _seed_user("exec@x.example", is_exec=True, department="Exec")

    db = SessionLocal()
    try:
        payload = ac.build_alignment_payload("exec@x.example", db)
    finally:
        db.close()

    assert payload is not None
    assert "EXEC ROCK auditor readiness" in [
        r.description for r in payload.company_rocks
    ]
    assert "EXEC METRIC" in [m.name for m in payload.off_track_metrics]


# ── AI Priorities: EosChannelAgent scoping ──────────────────────────────────


def test_eos_agent_scoped_query_fences_exec_for_non_exec():
    """The extractor's ``_scoped`` helper appends ``is_exec == False`` for a
    non-exec caller, so an Exec Rock never enters the candidate set even when
    the visibility ceiling is open (the admin case) and the caller owns it."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Rock
    from rubik_core.priorities.agents.eos_agent import EosChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig

    _seed_eos_with_exec_rows("admin@x.example")
    _seed_user("admin@x.example", is_exec=False)

    agent = EosChannelAgent(user_email="admin@x.example", config=PrioritiesConfig())
    records = agent.fetch_signals()

    # No emitted record points at an Exec-tagged Rock.
    db = SessionLocal()
    try:
        exec_rock_ids = {
            str(r.id) for r in db.query(Rock).filter(Rock.is_exec.is_(True)).all()
        }
    finally:
        db.close()
    leaked = [
        r
        for r in records
        if r.get("entity_kind") is not None and str(r.get("entity_id")) in exec_rock_ids
    ]
    assert not leaked, f"exec rock leaked into non-exec briefing: {leaked}"
    assert "EXEC ROCK" not in " ".join(r.get("title", "") for r in records)


# ── Chat RAG: rags_for_user_email ───────────────────────────────────────────


@pytest.fixture
def _fake_registry(monkeypatch):
    from rubik_core.rag import access as acc

    monkeypatch.setattr(
        acc,
        "current_registry",
        lambda: {"systems": 1, "companywide": 1, "vto": 1, "eos_exec": 1},
    )

    def _install_user(user):
        import rubik_core.auth.registry as reg

        monkeypatch.setattr(reg.UserRegistry, "get_user", lambda self, email: user)

    return _install_user


def _user(**kw):
    base = dict(
        is_admin=False,
        is_exec=False,
        sees_all_departments=False,
        department="Systems",
        managed_departments=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_rags_non_exec_admin_excludes_exec_only(_fake_registry):
    from rubik_core.rag.access import rags_for_user_email

    _fake_registry(_user(is_admin=True, is_exec=False))
    slugs = rags_for_user_email("admin@x.example")
    assert "vto" not in slugs and "eos_exec" not in slugs
    assert "systems" in slugs and "companywide" in slugs


def test_rags_exec_admin_gets_everything(_fake_registry):
    from rubik_core.rag.access import rags_for_user_email

    _fake_registry(_user(is_admin=True, is_exec=True))
    slugs = rags_for_user_email("execadmin@x.example")
    assert "vto" in slugs and "eos_exec" in slugs


def test_rags_scoped_exec_gets_exec_only(_fake_registry):
    from rubik_core.rag.access import rags_for_user_email

    _fake_registry(_user(is_exec=True, managed_departments=["Systems"]))
    slugs = rags_for_user_email("exec@x.example")
    assert "vto" in slugs and "eos_exec" in slugs


def test_rags_plain_user_excludes_exec_only(_fake_registry):
    from rubik_core.rag.access import rags_for_user_email

    _fake_registry(_user())
    slugs = rags_for_user_email("user@x.example")
    assert "vto" not in slugs and "eos_exec" not in slugs
    assert "companywide" in slugs
