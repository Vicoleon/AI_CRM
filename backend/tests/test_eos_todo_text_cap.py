"""A To-Do is one line — the 120-character cap and where it applies.

A To-Do has exactly ONE text field (``description``, a Text column), and every
surface renders it on a line or two. Nothing enforced that, so the paths that
mint a to-do out of prose — an Issue body, a Slack ``/todo`` ramble, a
meeting-prep idea — filed whole paragraphs, and the list clipped them with CSS
``truncate``. A UAT screenshot caught one mid-word: "Add to project
tracker; need quantifiable and non-quantifiable /Blair mentioned that GCP can
track how qu…", with no detail view to recover the rest from.

120 matches ``derive_issue_title``'s limit and the frontend's ``TODO_MAX_LEN``,
so Issues and To-Dos agree on what "one line" means. Longer input belongs in an
Issue, which has a body and a detail modal.

Covers the pure helper, the schema-level cap that every API write inherits, and
the issue→to-do path (which defaults to the entire issue description).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.eos.traction import TODO_TEXT_LIMIT, truncate_todo_text
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_todo_text_cap.db"
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


def _as_admin() -> None:
    now = "2026-08-19T00:00:00"
    app.dependency_overrides[get_current_user] = lambda: UserProfile(
        id="admin",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="Systems",
        is_exec=True,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


# -- the helper -------------------------------------------------------------


def test_short_text_is_untouched():
    assert truncate_todo_text("Pilot presentation for Globex") == (
        "Pilot presentation for Globex"
    )


def test_long_text_is_ellipsised_at_the_limit():
    out = truncate_todo_text("x" * 400)
    assert len(out) == TODO_TEXT_LIMIT
    assert out.endswith("…")


def test_only_the_first_line_survives():
    """Mirrors derive_issue_title: a pasted paragraph becomes its headline."""
    assert truncate_todo_text("Add project tracker\n\nWhy: Blair said GCP can…") == (
        "Add project tracker"
    )


def test_empty_and_none_stay_empty():
    assert truncate_todo_text("") == ""
    assert truncate_todo_text(None) == ""
    assert truncate_todo_text("   ") == ""


def test_a_boundary_length_string_is_not_ellipsised():
    exact = "y" * TODO_TEXT_LIMIT
    assert truncate_todo_text(exact) == exact


# -- the API ---------------------------------------------------------------


def _seed_owner() -> str:
    """A Person matching the acting user, so create paths can resolve an owner."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person

    db = SessionLocal()
    try:
        owner = Person(
            first_name="Ada",
            last_name="Admin",
            email="admin@example.com",
            created_by="admin@example.com",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)
        return str(owner.id)
    finally:
        db.close()


def test_create_todo_caps_the_description():
    _as_admin()
    owner_id = _seed_owner()
    long_text = (
        "Add to project tracker; need quantifiable and non-quantifiable "
        "/Blair mentioned that GCP can track how quickly tasks are occurring, "
        "how often, and they'd like a dashboard for that"
    )
    r = client.post(
        "/api/eos/todos/", json={"description": long_text, "owner_id": owner_id}
    )
    assert r.status_code == 201, r.text
    desc = r.json()["description"]
    assert len(desc) <= TODO_TEXT_LIMIT
    assert desc.endswith("…")
    assert desc.startswith("Add to project tracker")


def test_patching_a_description_caps_it_too():
    _as_admin()
    owner_id = _seed_owner()
    created = client.post(
        "/api/eos/todos/", json={"description": "short", "owner_id": owner_id}
    ).json()
    r = client.patch(f"/api/eos/todos/{created['id']}", json={"description": "z" * 300})
    assert r.status_code == 200, r.text
    assert len(r.json()["description"]) == TODO_TEXT_LIMIT


def test_issue_to_todo_caps_the_inherited_issue_body():
    """The riskiest path: it defaults the to-do text to the WHOLE issue body."""
    _as_admin()
    _seed_owner()
    body = (
        "The scorecard cells past the third column never colour green or red, "
        "which the exec team reads as the goal logic being broken. Root cause is "
        "the week axis, not the colouring."
    )
    issue = client.post("/api/eos/issues/", json={"description": body}).json()

    r = client.post(f"/api/eos/issues/{issue['id']}/todos", json={})
    assert r.status_code == 201, r.text
    todo = r.json()
    assert len(todo["description"]) <= TODO_TEXT_LIMIT
    assert todo["description"].endswith("…")
    # The link back to the issue is what carries the full context.
    assert todo["source_issue_id"] == issue["id"]
