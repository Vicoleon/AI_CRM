"""The facts of one finished L10 session, as a grounded prompt block.

Everything an L10 recap is allowed to say comes from here: closing notes, the
per-person survey, the Segue check-ins, the issues SOLVED inside the meeting's
window and the to-dos CREATED in it — all scoped to the meeting's own TEAM via
``team_scope_filter``, exactly as the in-meeting items payload is, so a
department L10's recap can never pull in the Exec team's (or another
department's) rows.

Two consumers share it, which is why it lives here rather than inside the
router that grew it:

* ``GET /api/eos/l10/sessions/{id}/recap`` — the on-screen narrative recap.
* ``backend/services/l10_summary_email.py`` — the post-meeting email, which
  pairs these same facts with the meeting's RECORDING.

They must never disagree about what a meeting contained, and a second copy of
this assembly is how they would. Read-only: nothing here writes or commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.models.eos.issues import derive_issue_title
from backend.routers.eos._shared import team_scope_filter
from rubik_core.database.eos import (
    Issue,
    IssueNote,
    L10Attendance,
    Person,
    Todo,
)


@dataclass
class SessionDigest:
    """The session's own record, assembled for a prompt."""

    meeting_name: str
    # Newline-joined, section-headed facts. This is the ONLY thing a model
    # summarizing the session's tool entries is allowed to read.
    data_block: str
    # False when the team recorded nothing at all (no notes, no ratings, no
    # check-ins, no solved issues, no new to-dos). Callers use it to skip the
    # LLM entirely rather than paying for padded filler.
    has_material: bool
    # People named in the session, email → display name. The summary email uses
    # it to match attendees against the transcript.
    people_by_email: dict[str, str] = field(default_factory=dict)
    solved_issue_count: int = 0
    todo_created_count: int = 0


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "unknown"


def build_session_digest(db, session) -> SessionDigest:
    """Assemble :class:`SessionDigest` for one ``eos_l10_sessions`` row."""
    meeting = session.meeting
    meeting_name = meeting.name if meeting is not None else "L10 Meeting"

    # -- Per-person survey + Segue check-ins ---------------------------
    # Same rows / person-name resolution as ``get_session_survey``.
    attendance_rows = (
        db.query(L10Attendance)
        .filter(
            L10Attendance.session_id == session.id,
            L10Attendance.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    person_ids = [a.person_id for a in attendance_rows]
    people = (
        {p.id: p for p in db.query(Person).filter(Person.id.in_(person_ids)).all()}
        if person_ids
        else {}
    )

    def _person_name(pid) -> str:
        p = people.get(pid)
        if p is None:
            return "Unknown"
        name = f"{p.first_name} {p.last_name}".strip()
        return name or p.email or "Unknown"

    survey_lines: list[str] = []
    checkin_lines: list[str] = []
    for a in attendance_rows:
        name = _person_name(a.person_id)
        if a.rating is not None or (a.rating_comment or "").strip():
            rating_txt = f"{a.rating}/10" if a.rating is not None else "no rating"
            comment = (a.rating_comment or "").strip()
            survey_lines.append(
                f"- {name}: {rating_txt}" + (f' — "{comment}"' if comment else "")
            )
        personal = (a.checkin_personal_content or "").strip()
        professional = (a.checkin_professional_content or "").strip()
        if personal or professional:
            parts = []
            if personal:
                parts.append(f"personal: {personal}")
            if professional:
                parts.append(f"professional: {professional}")
            checkin_lines.append(f"- {name}: " + "; ".join(parts))

    # -- Issues SOLVED / To-Dos CREATED during the meeting window ------
    # Team-scoped exactly like get_meeting_items (team_scope_filter): a
    # department L10 sees only its own team's rows, the Exec L10 only Exec
    # rows — no cross-team leak. Only run when the window is fully bounded.
    solved_issues: list[Issue] = []
    todos_created: list[Todo] = []
    if meeting is not None and session.actual_start and session.actual_end:
        solved_issues = (
            db.query(Issue)
            .filter(
                team_scope_filter(Issue, meeting),
                Issue.solved_at.isnot(None),
                Issue.solved_at >= session.actual_start,
                Issue.solved_at <= session.actual_end,
                Issue.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        todos_created = (
            db.query(Todo)
            .filter(
                team_scope_filter(Todo, meeting),
                Todo.created_at >= session.actual_start,
                Todo.created_at <= session.actual_end,
                Todo.is_deleted == False,  # noqa: E712
            )
            .all()
        )

    # Latest resolution note per solved issue (best-effort context).
    resolution_by_issue: dict = {}
    if solved_issues:
        note_rows = (
            db.query(IssueNote)
            .filter(
                IssueNote.issue_id.in_([i.id for i in solved_issues]),
                IssueNote.is_deleted == False,  # noqa: E712
            )
            .order_by(IssueNote.issue_id, IssueNote.created_at.desc())
            .all()
        )
        for n in note_rows:
            # First row per issue in DESC order is the latest note.
            resolution_by_issue.setdefault(n.issue_id, (n.content or "").strip())

    # Resolve any to-do owners not already in the attendance people map.
    extra_owner_ids = [
        t.owner_id for t in todos_created if t.owner_id and t.owner_id not in people
    ]
    if extra_owner_ids:
        for p in db.query(Person).filter(Person.id.in_(extra_owner_ids)).all():
            people[p.id] = p  # visible to _person_name (closes over `people`)

    # -- Assemble the grounded data block ------------------------------
    notes = (session.notes or "").strip()
    rating = session.average_rating

    sections: list[str] = [
        f"Meeting: {meeting_name}",
        f"When: {_fmt_dt(session.actual_start)} to {_fmt_dt(session.actual_end)}",
        (
            "Overall meeting rating (average of attendee scores, 1-10): "
            f"{rating if rating is not None else 'not recorded'}"
        ),
        "Closing notes:\n" + (notes or "(none)"),
        "Per-person survey (rating out of 10 + optional comment):\n"
        + ("\n".join(survey_lines) if survey_lines else "(no survey responses)"),
        "Check-ins / Segue good news (personal & professional):\n"
        + ("\n".join(checkin_lines) if checkin_lines else "(none recorded)"),
    ]

    if solved_issues:
        issue_lines: list[str] = []
        for i in solved_issues:
            title = (i.title or derive_issue_title(i.description) or "").strip()
            desc = (i.description or "").strip()
            line = f"- {title}" if title else "- (untitled issue)"
            if desc and desc != title:
                line += f": {desc}"
            resolution = resolution_by_issue.get(i.id)
            if resolution:
                line += f" (resolution: {resolution})"
            issue_lines.append(line)
        sections.append("Issues solved during this meeting:\n" + "\n".join(issue_lines))
    else:
        sections.append("Issues solved during this meeting:\n(none)")

    if todos_created:
        todo_lines: list[str] = []
        for t in todos_created:
            desc = (t.description or "").strip() or "(no description)"
            owner = _person_name(t.owner_id) if t.owner_id else None
            todo_lines.append(f"- {desc}" + (f" (owner: {owner})" if owner else ""))
        sections.append("To-Dos created during this meeting:\n" + "\n".join(todo_lines))
    else:
        sections.append("To-Dos created during this meeting:\n(none)")

    people_by_email = {
        (p.email or "").lower(): f"{p.first_name or ''} {p.last_name or ''}".strip()
        for p in people.values()
        if (p.email or "").strip()
    }

    return SessionDigest(
        meeting_name=meeting_name,
        data_block="\n\n".join(sections),
        has_material=bool(
            notes or survey_lines or checkin_lines or solved_issues or todos_created
        ),
        people_by_email=people_by_email,
        solved_issue_count=len(solved_issues),
        todo_created_count=len(todos_created),
    )
