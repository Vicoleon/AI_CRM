"""Notes on EOS To-Dos — the progress / blocker thread.

A to-do row carries only description + owner + due date, so "where does this
stand?" had nowhere to live. These cover the contract of the notes endpoints:
anyone who can READ the to-do may contribute a note; the thread reads
oldest-first; the batched ``/note-counts`` map lets a list advertise which rows
have notes without a fetch per row, and never counts a to-do the caller can't
see (the Exec-team boundary); and deletion follows the creator / admin / exec
rule.

No mocks — a temp SQLite DB per test, endpoint-level through TestClient.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_todo_notes.db"
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


def _profile(
    email: str,
    *,
    department: str = "Sales",
    roles: list[str] | None = None,
    is_exec: bool = False,
) -> UserProfile:
    now = "2026-07-28T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _seed() -> dict:
    """Two Sales to-dos (Sam's and Pat's) plus one Exec-team to-do."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person, Todo
    from rubik_core.database.models import Department

    db = SessionLocal()
    ids: dict = {}
    try:
        db.add(Department(name="Sales"))
        sam = Person(
            first_name="Sam",
            last_name="Sales",
            email="sam@x.example",
            created_by="seed",
        )
        pat = Person(
            first_name="Pat", last_name="Peer", email="pat@x.example", created_by="seed"
        )
        eve = Person(
            first_name="Eve", last_name="Exec", email="eve@x.example", created_by="seed"
        )
        db.add_all([sam, pat, eve])
        db.commit()

        sam_todo = Todo(
            description="Send the vendor contract",
            department_name="Sales",
            owner_id=sam.id,
            created_by="sam@x.example",
        )
        pat_todo = Todo(
            description="Update the pricing sheet",
            department_name="Sales",
            owner_id=pat.id,
            created_by="pat@x.example",
        )
        exec_todo = Todo(
            description="Board deck",
            is_exec=True,
            owner_id=eve.id,
            created_by="eve@x.example",
        )
        db.add_all([sam_todo, pat_todo, exec_todo])
        db.commit()
        for key, obj in (
            ("sam_todo", sam_todo),
            ("pat_todo", pat_todo),
            ("exec_todo", exec_todo),
        ):
            db.refresh(obj)
            ids[key] = obj.id
        return ids
    finally:
        db.close()


SAM = _profile("sam@x.example")
PAT = _profile("pat@x.example")
EVE = _profile("eve@x.example", department="Executive", is_exec=True)
ADMIN = _profile("admin@x.example", roles=["admin"])


# ---- the thread -------------------------------------------------------------


def test_notes_read_oldest_first_and_are_attributed():
    ids = _seed()
    _as(SAM)
    todo = ids["sam_todo"]
    assert client.get(f"/api/eos/todos/{todo}/notes").json() == []

    first = client.post(
        f"/api/eos/todos/{todo}/notes", json={"content": "Waiting on legal review."}
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/eos/todos/{todo}/notes", json={"content": "Legal signed off — sending."}
    )
    assert second.status_code == 201

    thread = client.get(f"/api/eos/todos/{todo}/notes").json()
    assert [n["content"] for n in thread] == [
        "Waiting on legal review.",
        "Legal signed off — sending.",
    ]
    assert {n["created_by"] for n in thread} == {"sam@x.example"}
    assert {n["todo_id"] for n in thread} == {str(todo)}


def test_a_teammate_can_note_someone_elses_todo():
    """Notes are a team surface: reading the to-do is the only requirement, so
    a manager or peer can record what they know without owning the row."""
    ids = _seed()
    _as(PAT)
    res = client.post(
        f"/api/eos/todos/{ids['sam_todo']}/notes", json={"content": "I have the file."}
    )
    assert res.status_code == 201
    assert res.json()["created_by"] == "pat@x.example"


def test_blank_note_is_rejected():
    ids = _seed()
    _as(SAM)
    todo = ids["sam_todo"]
    assert (
        client.post(f"/api/eos/todos/{todo}/notes", json={"content": "   "}).status_code
        == 422
    )
    assert client.get(f"/api/eos/todos/{todo}/notes").json() == []


def test_content_is_trimmed():
    ids = _seed()
    _as(SAM)
    res = client.post(
        f"/api/eos/todos/{ids['sam_todo']}/notes", json={"content": "  on track  "}
    )
    assert res.json()["content"] == "on track"


def test_notes_on_a_missing_todo_404():
    _seed()
    _as(SAM)
    ghost = uuid.uuid4()
    assert client.get(f"/api/eos/todos/{ghost}/notes").status_code == 404
    assert (
        client.post(f"/api/eos/todos/{ghost}/notes", json={"content": "x"}).status_code
        == 404
    )


# ---- batched counts ---------------------------------------------------------


def test_note_counts_only_lists_todos_that_have_notes():
    ids = _seed()
    _as(SAM)
    client.post(f"/api/eos/todos/{ids['sam_todo']}/notes", json={"content": "one"})
    client.post(f"/api/eos/todos/{ids['sam_todo']}/notes", json={"content": "two"})
    client.post(f"/api/eos/todos/{ids['pat_todo']}/notes", json={"content": "just one"})

    counts = client.get("/api/eos/todos/note-counts").json()
    assert counts == {str(ids["sam_todo"]): 2, str(ids["pat_todo"]): 1}


def test_note_counts_never_leaks_the_exec_team():
    """RLAC applies to the counts exactly as it does to the to-do list, so a
    non-exec can't infer activity on the confidential Exec surface."""
    ids = _seed()
    _as(EVE)
    client.post(f"/api/eos/todos/{ids['exec_todo']}/notes", json={"content": "draft 1"})

    _as(SAM)
    assert str(ids["exec_todo"]) not in client.get("/api/eos/todos/note-counts").json()

    _as(EVE)
    assert client.get("/api/eos/todos/note-counts").json() == {str(ids["exec_todo"]): 1}


def test_note_counts_route_is_not_swallowed_by_the_by_id_route():
    """``/note-counts`` must stay declared before ``/{todo_id}`` — otherwise
    FastAPI parses "note-counts" as a UUID and the call 422s."""
    _seed()
    _as(SAM)
    res = client.get("/api/eos/todos/note-counts")
    assert res.status_code == 200
    assert res.json() == {}


# ---- deletion ---------------------------------------------------------------


def test_author_can_delete_own_note():
    ids = _seed()
    _as(SAM)
    todo = ids["sam_todo"]
    note_id = client.post(
        f"/api/eos/todos/{todo}/notes", json={"content": "typo"}
    ).json()["id"]

    assert client.delete(f"/api/eos/todos/{todo}/notes/{note_id}").status_code == 204
    assert client.get(f"/api/eos/todos/{todo}/notes").json() == []
    # A soft-deleted note stops counting, so the row's chip disappears.
    assert client.get("/api/eos/todos/note-counts").json() == {}


def test_admin_can_delete_someone_elses_note():
    ids = _seed()
    _as(PAT)
    todo = ids["sam_todo"]
    note_id = client.post(
        f"/api/eos/todos/{todo}/notes", json={"content": "pat's note"}
    ).json()["id"]

    _as(ADMIN)
    assert client.delete(f"/api/eos/todos/{todo}/notes/{note_id}").status_code == 204


def test_a_peer_cannot_delete_someone_elses_note():
    ids = _seed()
    _as(SAM)
    todo = ids["sam_todo"]
    note_id = client.post(
        f"/api/eos/todos/{todo}/notes", json={"content": "sam's note"}
    ).json()["id"]

    _as(PAT)
    assert client.delete(f"/api/eos/todos/{todo}/notes/{note_id}").status_code == 403
    assert len(client.get(f"/api/eos/todos/{todo}/notes").json()) == 1


def test_deleting_a_note_through_the_wrong_todo_404s():
    ids = _seed()
    _as(SAM)
    note_id = client.post(
        f"/api/eos/todos/{ids['sam_todo']}/notes", json={"content": "mine"}
    ).json()["id"]

    assert (
        client.delete(f"/api/eos/todos/{ids['pat_todo']}/notes/{note_id}").status_code
        == 404
    )
