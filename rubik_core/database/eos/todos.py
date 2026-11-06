"""Weekly To-Dos.

A todo can be standalone, linked to a Rock, linked to an L10 meeting,
or all of the above. Both rock_id and l10_meeting_id are nullable so
the todo outlives its parent.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)

from rubik_core.database.models import Base

from ._base import AuditMixin, TodoStatus


class Todo(Base, AuditMixin):
    __tablename__ = "eos_todos"

    owner_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=False)
    rock_id = Column(sa.Uuid, ForeignKey("eos_rocks.id"), nullable=True)
    # Scrum-style back-pointer: set when this to-do was spun off from an
    # Issue ("turn an issue into a task"). Lets the parent Issue track its
    # child to-dos and block its own solve until they're all done.
    # ON DELETE SET NULL so the to-do outlives a hard-deleted issue.
    source_issue_id = Column(
        sa.Uuid,
        ForeignKey(
            "eos_issues.id", ondelete="SET NULL", name="fk_eos_todos_source_issue_id"
        ),
        nullable=True,
    )
    # Team-scoping. To-Dos belong to a team — a department
    # (``department_name``) or the Exec/Leadership team (``is_exec=True``).
    # Previously to-dos were only reachable via ``l10_meeting_id`` (a single
    # meeting instance), so pre-work added on the To-Do page never surfaced in
    # the team's L10. The team's L10 now shows the team's open to-dos.
    # A free-text team tag, deliberately NOT a key to ``departments.name``:
    # its writers (to-do create, promote-to-EOS, the Slack command, L10
    # convert) store the team as given, and promote-to-EOS falls back to the
    # caller's ``users.department``, which no ``departments`` row has to
    # match; removing a department must not clear the tag either. The live
    # schema never had a key here.
    department_name = Column(String, nullable=True)
    is_exec = Column(Boolean, default=False, nullable=False, server_default=sa.false())
    l10_meeting_id = Column(sa.Uuid, ForeignKey("eos_l10_meetings.id"), nullable=True)
    description = Column(Text, nullable=False)
    due_date = Column(DateTime, nullable=True)
    status = Column(
        Enum(
            TodoStatus,
            name="eos_todo_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=TodoStatus.PENDING,
        nullable=False,
    )


class TodoNote(Base, AuditMixin):
    """A progress / blocker note on a To-Do.

    A to-do row carries only description + owner + due date, so "where does
    this actually stand?" had nowhere to live: the answer was said out loud in
    the L10 and lost. Notes give it a home — one row per note, attributed via
    the inherited ``created_by`` + ``created_at``, so an overdue to-do reads as
    a short history ("waiting on vendor", "80% done") rather than one
    overwritable field. Deliberately the same shape as
    ``IssueNote`` (rubik_core/database/eos/issues.py) so both threads behave
    identically on the page and inside the L10.
    """

    __tablename__ = "eos_todo_notes"

    todo_id = Column(
        sa.Uuid,
        ForeignKey("eos_todos.id", ondelete="CASCADE"),
        nullable=False,
    )
    content = Column(Text, nullable=False)

    __table_args__ = (Index("ix_eos_todo_notes_todo_created", "todo_id", "created_at"),)
