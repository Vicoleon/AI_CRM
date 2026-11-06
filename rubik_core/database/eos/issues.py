"""Issues list — items raised in L10 sessions for IDS (Identify/Discuss/Solve)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Index, String, Text

from rubik_core.database.models import Base

from ._base import AuditMixin, IssuePriority, IssueStatus


class Issue(Base, AuditMixin):
    __tablename__ = "eos_issues"

    owner_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=True)
    department_name = Column(
        String, ForeignKey("departments.name", ondelete="SET NULL"), nullable=True
    )
    # Team-scoping. An issue belongs to a TEAM — either a department
    # (``department_name``) or the cross-department Exec/Leadership team
    # (``is_exec=True``, ``department_name`` NULL). The L10 for a given team
    # surfaces that team's open issues, so pre-work added on the Issues page
    # shows up in the right meeting. ``department_name`` is FK-constrained so
    # it can't hold a sentinel; the Exec team is marked with this flag.
    is_exec = Column(Boolean, default=False, nullable=False, server_default=sa.false())
    l10_meeting_id = Column(sa.Uuid, ForeignKey("eos_l10_meetings.id"), nullable=True)
    # Short human-facing headline shown on the IDS grid; the full detail lives in
    # ``description``. Nullable so pre-existing rows and programmatic creators
    # (e.g. rock→IDS) don't break — the router derives a title from the
    # description when one isn't supplied, so it's effectively always populated.
    title = Column(String, nullable=True)
    description = Column(Text, nullable=False)
    priority = Column(
        Enum(
            IssuePriority,
            name="eos_issue_priority",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=IssuePriority.LOW,
        nullable=False,
    )
    status = Column(
        Enum(
            IssueStatus,
            name="eos_issue_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=IssueStatus.OPEN,
        nullable=False,
    )
    # Timestamp of the open→solved transition (stamped by the Issues
    # router; cleared on re-open). Distinct from `updated_at`, which moves
    # on any edit — `solved_at` is the reliable signal for the dashboard's
    # "% issues solved per week" trend. Nullable: NULL while open/archived.
    solved_at = Column(DateTime, nullable=True)
    # When an issue is spawned from the "Add to IDS" button on a
    # rock's detail page, we keep a soft back-pointer so the IDS list can
    # show "this came from rock X" and so analytics can answer "which
    # rocks generate the most issues?". Nullable because most issues are
    # still raised standalone in the L10.
    source_rock_id = Column(
        sa.Uuid,
        ForeignKey(
            "eos_rocks.id", ondelete="SET NULL", name="fk_eos_issues_source_rock_id"
        ),
        nullable=True,
    )


class IssueNote(Base, AuditMixin):
    """A discussion/resolution note on an Issue.

    During IDS the team adds notes as they identify/discuss, and a final
    note (the resolution) before solving. A threaded model (many notes per
    issue, each attributed via the inherited ``created_by`` + ``created_at``)
    captures that history rather than one overwritable field.
    """

    __tablename__ = "eos_issue_notes"

    issue_id = Column(
        sa.Uuid,
        ForeignKey("eos_issues.id", ondelete="CASCADE"),
        nullable=False,
    )
    content = Column(Text, nullable=False)

    __table_args__ = (
        Index("ix_eos_issue_notes_issue_created", "issue_id", "created_at"),
    )
