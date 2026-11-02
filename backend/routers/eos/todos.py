"""EOS todos — weekly action items.

Create accepts `owner_email` as an alternative to `owner_id`. An
optional `l10_meeting_id` ties the todo to a specific L10 session for
agenda carry-over. Each to-do also carries a notes thread (progress /
blockers), mirroring the Issue-notes endpoints.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func as sa_func

from backend.models.eos.traction import (
    TodoCreate,
    TodoNoteCreate,
    TodoNoteResponse,
    TodoResponse,
    TodoUpdate,
)
from backend.routers.eos._authz import (
    require_can_mutate,
    require_can_read,
    require_exec_scope_allowed,
    visible_todo_ids,
)
from backend.routers.eos._priority_sync import sync_todo_priority
from backend.routers.eos._shared import (
    CurrentUser,
    DBDep,
)
from rubik_core.database.eos import Person, Todo, TodoNote
from rubik_core.database.eos._base import TodoStatus

router = APIRouter(prefix="/todos", tags=["EOS: Todos"])


@router.get("/", response_model=list[TodoResponse])
def list_todos(
    db: DBDep,
    current_user: CurrentUser,
    owner_id: UUID | None = None,
    department_name: str | None = None,
    is_exec: bool | None = None,
):
    """List to-dos the caller may read.

    Team filters (both optional, mirroring /rocks and /issues):
    ``is_exec=true`` → the Exec/Leadership team's to-dos; ``department_name``
    → one department team (case-insensitive). RLAC visibility applies first,
    so a non-exec filtering by the Exec team simply gets nothing.
    """
    query = db.query(Todo).filter(Todo.is_deleted == False)  # noqa: E712
    # Row-level access: limit to todos the caller may read (None = unrestricted).
    visible_ids = visible_todo_ids(current_user, db)
    if visible_ids is not None:
        query = query.filter(Todo.id.in_(visible_ids))
    if owner_id:
        query = query.filter(Todo.owner_id == owner_id)
    if is_exec is not None:
        query = query.filter(Todo.is_exec == is_exec)
    if department_name:
        query = query.filter(
            sa_func.lower(Todo.department_name) == department_name.lower()
        )
    return query.all()


def _get_todo_or_404(db, todo_id: UUID, current_user) -> Todo:
    """Fetch a live to-do the caller may read, else 404.

    ``require_can_read`` raises 404 (not 403) so Exec-team to-dos are
    indistinguishable from non-existent ones to a non-exec holding the id.
    """
    todo = (
        db.query(Todo)
        .filter(Todo.id == todo_id, Todo.is_deleted == False)  # noqa: E712
        .first()
    )
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")
    require_can_read(current_user, todo, db)  # Exec-team rows stay exec-only
    return todo


# Declared BEFORE ``/{todo_id}`` — FastAPI matches in declaration order, and
# the dynamic route would otherwise swallow "note-counts" as a malformed UUID.
@router.get("/note-counts", response_model=dict[UUID, int])
def todo_note_counts(db: DBDep, current_user: CurrentUser):
    """``{todo_id: note_count}`` for every readable to-do that has notes.

    One batched call so a list of to-dos can show which rows carry notes
    without a fetch per row. To-dos with no notes are omitted (callers read a
    missing key as 0). Scoped by the same RLAC visibility as ``list_todos``,
    so Exec-team counts never leak to a non-exec.
    """
    query = (
        db.query(TodoNote.todo_id, sa_func.count(TodoNote.id))
        .join(Todo, Todo.id == TodoNote.todo_id)
        .filter(
            TodoNote.is_deleted == False,  # noqa: E712
            Todo.is_deleted == False,  # noqa: E712
        )
    )
    visible_ids = visible_todo_ids(current_user, db)
    if visible_ids is not None:
        query = query.filter(Todo.id.in_(visible_ids))
    return {todo_id: count for todo_id, count in query.group_by(TodoNote.todo_id)}


@router.get("/{todo_id}", response_model=TodoResponse)
def get_todo(todo_id: UUID, db: DBDep, current_user: CurrentUser):
    return _get_todo_or_404(db, todo_id, current_user)


@router.post("/", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
def create_todo(db: DBDep, current_user: CurrentUser, todo_in: TodoCreate):
    if todo_in.is_exec:
        require_exec_scope_allowed(current_user, True)
    # Resolve owner: prefer explicit ID, fall back to email lookup.
    if todo_in.owner_email:
        person = db.query(Person).filter(Person.email == todo_in.owner_email).first()
        if not person:
            raise HTTPException(status_code=400, detail="Owner email not found")
        todo_in.owner_id = person.id
    elif not todo_in.owner_id:
        raise HTTPException(status_code=400, detail="Owner ID or Email is required")

    todo_data = todo_in.model_dump(exclude={"owner_email"})
    # Avoid passing l10_meeting_id=None as an explicit kwarg if it's
    # falsy — keeps the upstream's exact insert shape.
    if not todo_data.get("l10_meeting_id"):
        todo_data.pop("l10_meeting_id", None)

    todo = Todo(**todo_data, created_by=current_user.email)
    db.add(todo)
    db.commit()
    db.refresh(todo)
    return todo


@router.patch("/{todo_id}", response_model=TodoResponse)
def update_todo(
    todo_id: UUID, db: DBDep, current_user: CurrentUser, todo_in: TodoUpdate
):
    todo = (
        db.query(Todo)
        .filter(Todo.id == todo_id, Todo.is_deleted == False)  # noqa: E712
        .first()
    )
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    require_can_mutate(current_user, todo, db)
    was_done = todo.status == TodoStatus.DONE
    for field, value in todo_in.model_dump(exclude_unset=True).items():
        setattr(todo, field, value)

    todo.updated_by = current_user.email  # Who flipped the to-do
    db.commit()
    db.refresh(todo)
    # Auto-clear (or reopen) the AI-Priorities card for this to-do — for the
    # owner AND any manager/exec whose team briefing surfaced it.
    sync_todo_priority(
        todo_id=todo.id,
        was_done=was_done,
        is_done=todo.status == TodoStatus.DONE,
        actor=current_user.email,
    )
    return todo


@router.delete("/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_todo(todo_id: UUID, db: DBDep, current_user: CurrentUser):
    todo = (
        db.query(Todo)
        .filter(Todo.id == todo_id, Todo.is_deleted == False)  # noqa: E712
        .first()
    )
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    require_can_mutate(current_user, todo, db)
    todo.is_deleted = True
    todo.updated_by = current_user.email  # Attribute the delete
    db.commit()
    return None


# ---- notes (progress / blockers on a to-do) ---------------------------------
# Same contract as the Issue-notes endpoints in ./issues.py: read-gated by the
# parent row, anyone who can see it may contribute, deletion follows the
# creator / admin / exec rule.


@router.get("/{todo_id}/notes", response_model=list[TodoNoteResponse])
def list_todo_notes(todo_id: UUID, db: DBDep, current_user: CurrentUser):
    """Notes on a to-do, oldest-first (the progress thread)."""
    _get_todo_or_404(db, todo_id, current_user)
    return (
        db.query(TodoNote)
        .filter(
            TodoNote.todo_id == todo_id,
            TodoNote.is_deleted == False,  # noqa: E712
        )
        .order_by(TodoNote.created_at.asc())
        .all()
    )


@router.post(
    "/{todo_id}/notes",
    response_model=TodoNoteResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_todo_note(
    todo_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    note_in: TodoNoteCreate,
):
    """Add a note to a to-do (anyone who can see the to-do can contribute)."""
    _get_todo_or_404(db, todo_id, current_user)
    content = (note_in.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must not be empty")
    note = TodoNote(
        todo_id=todo_id,
        content=content,
        created_by=current_user.email,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.delete(
    "/{todo_id}/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_todo_note(
    todo_id: UUID, note_id: UUID, db: DBDep, current_user: CurrentUser
):
    """Delete a note — author, or an admin / exec member."""
    _get_todo_or_404(db, todo_id, current_user)
    note = (
        db.query(TodoNote)
        .filter(
            TodoNote.id == note_id,
            TodoNote.todo_id == todo_id,
            TodoNote.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    require_can_mutate(current_user, note, db)  # creator / admin / exec
    note.is_deleted = True
    note.updated_by = current_user.email
    db.commit()
    return None
