"""EOS issues — items raised in L10 for IDS (Identify/Discuss/Solve)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import func as sa_func

from backend.jobs.eos_ingest_hooks import enqueue_issue_ingest
from backend.jobs.launcher import reject_support_background_work
from backend.models.eos.issues import (
    IssueCreate,
    IssueNoteCreate,
    IssueNoteResponse,
    IssueResponse,
    IssueUpdate,
    derive_issue_title,
)
from backend.models.eos.traction import (
    TodoFromIssueCreate,
    TodoResponse,
    truncate_todo_text,
)
from backend.routers.eos._authz import (
    require_can_mutate,
    require_can_read,
    require_exec_scope_allowed,
    visible_issue_ids,
)
from backend.routers.eos._priority_sync import sync_issue_priority
from backend.routers.eos._shared import (
    CurrentUser,
    DBDep,
)
from rubik_core.database.eos import (
    Issue,
    IssueNote,
    IssueStatus,
    Person,
    Todo,
    TodoStatus,
)

router = APIRouter(prefix="/issues", tags=["EOS: Issues"])


@router.get("/", response_model=list[IssueResponse])
def list_issues(
    db: DBDep,
    current_user: CurrentUser,
    department_name: str | None = None,
    is_exec: bool | None = None,
):
    """List issues the caller may read.

    Team filters (both optional, mirroring /rocks): ``is_exec=true`` → the
    Exec/Leadership team's issues; ``department_name`` → one department team
    (case-insensitive — a department's identity is its slug). Access is
    enforced by the RLAC visibility filter FIRST, so filtering by the Exec
    team as a non-exec simply returns nothing.
    """
    query = db.query(Issue).filter(Issue.is_deleted == False)  # noqa: E712
    # Row-level access: limit to issues the caller may read (None = unrestricted).
    visible_ids = visible_issue_ids(current_user, db)
    if visible_ids is not None:
        query = query.filter(Issue.id.in_(visible_ids))
    if is_exec is not None:
        query = query.filter(Issue.is_exec == is_exec)
    if department_name:
        query = query.filter(
            sa_func.lower(Issue.department_name) == department_name.lower()
        )
    return query.all()


@router.get("/{issue_id}", response_model=IssueResponse)
def get_issue(issue_id: UUID, db: DBDep, current_user: CurrentUser):
    issue = (
        db.query(Issue)
        .filter(Issue.id == issue_id, Issue.is_deleted == False)  # noqa: E712
        .first()
    )
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    require_can_read(current_user, issue, db)  # Exec-team rows stay exec-only
    return issue


@router.post("/", response_model=IssueResponse, status_code=status.HTTP_201_CREATED)
def create_issue(
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    issue_in: IssueCreate,
):
    if issue_in.is_exec:
        require_exec_scope_allowed(current_user, True)
    reject_support_background_work()
    data = issue_in.model_dump()
    # Guarantee a grid headline even when the client omits one (rock→IDS,
    # legacy callers): fall back to a trimmed first line of the description.
    if not (data.get("title") or "").strip():
        data["title"] = derive_issue_title(data.get("description"))
    issue = Issue(**data, created_by=current_user.email)
    db.add(issue)
    db.commit()
    db.refresh(issue)
    enqueue_issue_ingest(background_tasks)
    return issue


@router.patch("/{issue_id}", response_model=IssueResponse)
def update_issue(
    issue_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    issue_in: IssueUpdate,
):
    issue = (
        db.query(Issue)
        .filter(Issue.id == issue_id, Issue.is_deleted == False)  # noqa: E712
        .first()
    )
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    require_can_mutate(current_user, issue, db)
    prev_status = issue.status
    updates = issue_in.model_dump(exclude_unset=True)
    new_status = updates.get("status", prev_status)
    if new_status == IssueStatus.SOLVED and prev_status != IssueStatus.SOLVED:
        reject_support_background_work()

    # Scrum-style dependency gate: an issue can't be solved while any to-do
    # spun off from it is still open — finish the children first. The client
    # mirrors this (disables the solve button), but the server is the
    # authority since a caller may not see every child in their RLAC scope.
    if new_status == IssueStatus.SOLVED and prev_status != IssueStatus.SOLVED:
        open_children = (
            db.query(Todo)
            .filter(
                Todo.source_issue_id == issue.id,
                Todo.is_deleted == False,  # noqa: E712
                Todo.status != TodoStatus.DONE,
            )
            .count()
        )
        if open_children:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This issue has {open_children} open to-do"
                    f"{'' if open_children == 1 else 's'}. Finish them "
                    "before marking the issue solved."
                ),
            )

    for field, value in updates.items():
        setattr(issue, field, value)

    # Never let an edit leave the grid headline blank (client cleared the
    # title, or a legacy row without one had only its description edited).
    if not (issue.title or "").strip():
        issue.title = derive_issue_title(issue.description)

    # Stamp / clear the solve timestamp on the open↔solved transition so the
    # dashboard's "% solved per week" trend has a reliable signal (updated_at
    # moves on every edit, so it can't stand in for "when it was solved").
    if new_status == IssueStatus.SOLVED and prev_status != IssueStatus.SOLVED:
        issue.solved_at = datetime.utcnow()
    elif new_status != IssueStatus.SOLVED and prev_status == IssueStatus.SOLVED:
        issue.solved_at = None

    issue.updated_by = current_user.email  # Who solved/edited the issue
    db.commit()
    db.refresh(issue)

    # Re-index on status change to `solved` — that's when an issue
    # becomes useful historical context for retrieval. Other patches
    # (description tweaks, priority changes) don't trigger a reindex —
    # they surface on the next solve/create's full-kind re-ingest.
    if issue.status == IssueStatus.SOLVED and prev_status != IssueStatus.SOLVED:
        enqueue_issue_ingest(background_tasks)

    # Auto-clear (or reopen) the AI-Priorities card for this issue — owner AND
    # any manager/exec whose team briefing surfaced it.
    sync_issue_priority(
        issue_id=issue.id,
        was_solved=prev_status == IssueStatus.SOLVED,
        is_solved=issue.status == IssueStatus.SOLVED,
        actor=current_user.email,
    )

    return issue


@router.delete("/{issue_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_issue(issue_id: UUID, db: DBDep, current_user: CurrentUser):
    issue = (
        db.query(Issue)
        .filter(Issue.id == issue_id, Issue.is_deleted == False)  # noqa: E712
        .first()
    )
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    require_can_mutate(current_user, issue, db)
    issue.is_deleted = True
    issue.updated_by = current_user.email  # Attribute the delete
    db.commit()
    return None


# -- Issue → To-Do (Scrum story → task) ------------------------------


@router.post(
    "/{issue_id}/todos",
    response_model=TodoResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_todo_from_issue(
    issue_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    payload: TodoFromIssueCreate,
):
    """Spin a To-Do off an Issue so an individual can follow up.

    The new to-do carries a ``source_issue_id`` back-pointer, inherits the
    issue's team scope (``department_name`` / ``is_exec``), and defaults its
    text to the issue's description. The parent issue then can't be marked
    solved until every to-do spun off it is done (see ``update_issue``).
    """
    issue = _get_issue_or_404(db, issue_id, current_user)
    require_can_mutate(current_user, issue, db)  # same right as editing the issue

    # Resolve the owner (Todo.owner_id is NOT NULL): explicit email → explicit
    # id → the issue's owner → the caller's own Person.
    owner_id = payload.owner_id
    if payload.owner_email:
        person = (
            db.query(Person)
            .filter(
                Person.email == payload.owner_email,
                Person.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if not person:
            raise HTTPException(status_code=400, detail="Owner email not found")
        owner_id = person.id
    if owner_id is None:
        owner_id = issue.owner_id
    if owner_id is None:
        caller = (
            db.query(Person)
            .filter(
                Person.email == current_user.email,
                Person.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        owner_id = caller.id if caller else None
    if owner_id is None:
        raise HTTPException(
            status_code=400,
            detail="Could not resolve a to-do owner — pass owner_email.",
        )

    # An issue body is routinely a paragraph; a to-do renders on a line. Cap it
    # the same way the issue's own title is derived, so the pair reads alike.
    description = truncate_todo_text(
        (payload.description or "").strip() or issue.description
    )

    todo = Todo(
        owner_id=owner_id,
        description=description,
        due_date=payload.due_date,
        department_name=issue.department_name,
        is_exec=issue.is_exec,
        source_issue_id=issue.id,
        status=TodoStatus.PENDING,
        created_by=current_user.email,
    )
    db.add(todo)
    db.commit()
    db.refresh(todo)
    return todo


# -- Issue notes — discussion / resolution thread ------------


def _get_issue_or_404(db, issue_id: UUID, current_user) -> Issue:
    """Fetch an issue the caller may READ, else 404. The read gate keeps a
    confidential Exec-team issue (and its notes / child to-dos) invisible to
    non-exec callers even when they hold the id."""
    issue = (
        db.query(Issue)
        .filter(Issue.id == issue_id, Issue.is_deleted == False)  # noqa: E712
        .first()
    )
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    require_can_read(current_user, issue, db)
    return issue


@router.get("/{issue_id}/notes", response_model=list[IssueNoteResponse])
def list_issue_notes(issue_id: UUID, db: DBDep, current_user: CurrentUser):
    """Notes on an issue, oldest-first (the discussion → resolution thread)."""
    _get_issue_or_404(db, issue_id, current_user)
    return (
        db.query(IssueNote)
        .filter(
            IssueNote.issue_id == issue_id,
            IssueNote.is_deleted == False,  # noqa: E712
        )
        .order_by(IssueNote.created_at.asc())
        .all()
    )


@router.post(
    "/{issue_id}/notes",
    response_model=IssueNoteResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_issue_note(
    issue_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    note_in: IssueNoteCreate,
):
    """Add a note to an issue (anyone who can see the issue can contribute)."""
    _get_issue_or_404(db, issue_id, current_user)
    content = (note_in.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must not be empty")
    note = IssueNote(
        issue_id=issue_id,
        content=content,
        created_by=current_user.email,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.delete(
    "/{issue_id}/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_issue_note(
    issue_id: UUID, note_id: UUID, db: DBDep, current_user: CurrentUser
):
    """Delete a note — author, or an admin / exec member."""
    note = (
        db.query(IssueNote)
        .filter(
            IssueNote.id == note_id,
            IssueNote.issue_id == issue_id,
            IssueNote.is_deleted == False,  # noqa: E712
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
