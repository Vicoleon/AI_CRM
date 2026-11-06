"""Meeting Skills — Templates, Skills and Routines built in Ask Cubie.

Plaud-style meeting intelligence, with Chat as the only surface: a person asks
Cubie for a meeting in a shape of their choosing, Cubie writes the shape (a
**Template**), and a whole repeatable job over meetings becomes a **Skill**.
A **Routine** runs a Skill on its own when a new transcript lands. A Routine
on a clock is an ordinary scheduled task whose instruction runs a Skill, so it
needs no table here.

* ``meeting_templates`` — a named outline: sections, their order and what goes
  in each. Nothing else.
* ``meeting_skills`` — the whole job: instructions, tone, depth, which
  meetings, an optional Template, and where the result goes.
* ``meeting_definition_versions`` — every saved shape of either, append-only,
  so an old output stays reproducible after an edit.
* ``meeting_skill_runs`` — one row per run: what was read, the model's
  intermediate answer, what verification dropped, what was delivered.
* ``meeting_routines`` / ``meeting_routine_fires`` — "when a transcript of mine
  is ready, run this Skill". A fire is unique per (routine, transcript), so a
  transcript re-announced by a retried sync produces one output.

House rules, as in ``deal_defers.py`` and ``loop_closure.py``:

* Emails are lower-folded and have NO foreign key to ``users``.
* Instants are naive UTC.
* Vocabularies are strings with a CHECK, never DB enums.
* New tables only (an ALTER locks a live table): nothing here ALTERs
  ``scheduled_tasks``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base

# Duplicated as literals in alembic/versions/0001_cubie_baseline.py
# (a migration is a record of the shape that shipped).
CREATED_VIA: tuple[str, ...] = ("chat", "upload", "output")
DEFINITION_KINDS: tuple[str, ...] = ("template", "skill")
DETAIL_LEVELS: tuple[str, ...] = ("brief", "standard", "exhaustive")
MEETING_SCOPES: tuple[str, ...] = ("one", "many")
DELIVER_AS: tuple[str, ...] = ("chat", "email_draft_to_me", "email_draft_to_attendees")
RUN_TRIGGERS: tuple[str, ...] = ("chat", "routine", "scheduled", "dry_run")
RUN_STATUSES: tuple[str, ...] = ("ok", "partial", "failed")
ROUTINE_TRIGGERS: tuple[str, ...] = ("transcript_ready",)
ROUTINE_STATUSES: tuple[str, ...] = ("active", "paused", "error_paused")
FIRE_STATUSES: tuple[str, ...] = ("queued", "running", "succeeded", "failed", "skipped")


def _uuid() -> str:
    return uuid.uuid4().hex


def _fold(value):
    return value.strip().lower() if isinstance(value, str) else value


class MeetingTemplate(Base):
    """A named outline for a meeting write-up. Private to its owner."""

    __tablename__ = "meeting_templates"

    id = Column(String, primary_key=True, default=_uuid)
    owner_email = Column(String, nullable=False)
    name = Column(String, nullable=False)
    #: ``name`` folded, so "BANT note" and "bant note" are one template.
    name_key = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    #: [{"key", "title", "instruction", "format", "required"}] — see
    #: ``rubik_core.meeting_skills.sections``.
    sections = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_via = Column(String, nullable=False, default="chat")
    source_session_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("owner_email", "name_key", name="uq_meeting_templates_name"),
        CheckConstraint(
            in_check("created_via", CREATED_VIA), name="ck_meeting_templates_via"
        ),
        CheckConstraint(
            lower_check("owner_email"), name="ck_meeting_templates_owner_lower"
        ),
    )

    @validates("owner_email")
    def _fold_owner(self, _key, value):
        return _fold(value)


class MeetingSkill(Base):
    """A saved, repeatable job over meetings. Private to its owner."""

    __tablename__ = "meeting_skills"

    id = Column(String, primary_key=True, default=_uuid)
    owner_email = Column(String, nullable=False)
    name = Column(String, nullable=False)
    #: What follows the "/" in the chat box: ``client-debrief``.
    slug = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    #: The playbook in plain words: what to look for, what to ignore.
    instructions = Column(Text, nullable=False)
    template_id = Column(
        String,
        ForeignKey("meeting_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    tone = Column(String, nullable=True)
    detail_level = Column(String, nullable=False, default="standard")
    #: "one" meeting per run, or "many" across ``window_days``.
    meetings = Column(String, nullable=False, default="one")
    window_days = Column(Integer, nullable=False, default=7)
    deliver_as = Column(String, nullable=False, default="chat")
    exclude_personal = Column(Boolean, nullable=False, default=True)
    version = Column(Integer, nullable=False, default=1)
    invocation_count = Column(Integer, nullable=False, default=0)
    last_invoked_at = Column(DateTime, nullable=True)
    created_via = Column(String, nullable=False, default="chat")
    source_session_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("owner_email", "slug", name="uq_meeting_skills_slug"),
        CheckConstraint(
            in_check("detail_level", DETAIL_LEVELS), name="ck_meeting_skills_detail"
        ),
        CheckConstraint(
            in_check("meetings", MEETING_SCOPES), name="ck_meeting_skills_meetings"
        ),
        CheckConstraint(
            in_check("deliver_as", DELIVER_AS), name="ck_meeting_skills_deliver_as"
        ),
        CheckConstraint(
            in_check("created_via", CREATED_VIA), name="ck_meeting_skills_via"
        ),
        CheckConstraint(
            lower_check("owner_email"), name="ck_meeting_skills_owner_lower"
        ),
    )

    @validates("owner_email")
    def _fold_owner(self, _key, value):
        return _fold(value)


class MeetingDefinitionVersion(Base):
    """One saved shape of a template or skill. Never updated, never deleted
    while its definition lives (the definition's delete takes its history)."""

    __tablename__ = "meeting_definition_versions"

    id = Column(String, primary_key=True, default=_uuid)
    kind = Column(String, nullable=False)
    definition_id = Column(String, nullable=False)
    version = Column(Integer, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "kind", "definition_id", "version", name="uq_meeting_definition_versions"
        ),
        CheckConstraint(
            in_check("kind", DEFINITION_KINDS), name="ck_meeting_definition_kind"
        ),
    )


class MeetingSkillRun(Base):
    """One run of a skill (or of an ad-hoc template) over one or more meetings.

    ``skill_version`` / ``template_version`` pin the shape that ran, and
    ``transcript_hashes`` the text it read, so an output can be explained after
    either has changed. ``intermediate`` is the model's cited answer BEFORE
    verification; ``verification`` lists what was dropped and why.
    """

    __tablename__ = "meeting_skill_runs"

    id = Column(String, primary_key=True, default=_uuid)
    owner_email = Column(String, nullable=False)
    skill_id = Column(String, nullable=True)
    skill_version = Column(Integer, nullable=True)
    template_id = Column(String, nullable=True)
    template_version = Column(Integer, nullable=True)
    #: The sections the output was rendered with (a saved template's, or the
    #: ones Cubie wrote for this request).
    sections = Column(JSON, nullable=True)
    doc_ids = Column(JSON, nullable=False, default=list)
    transcript_hashes = Column(JSON, nullable=True)
    trigger = Column(String, nullable=False, default="chat")
    routine_id = Column(String, nullable=True)
    plan = Column(JSON, nullable=True)
    intermediate = Column(JSON, nullable=True)
    verification = Column(JSON, nullable=True)
    output_md = Column(Text, nullable=True)
    citations = Column(JSON, nullable=True)
    #: Where it went besides chat: {"gmail_draft_url": ...}.
    output_ref = Column(JSON, nullable=True)
    status = Column(String, nullable=False, default="ok")
    error = Column(Text, nullable=True)
    model = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_meeting_skill_runs_owner_created", "owner_email", "created_at"),
        Index("ix_meeting_skill_runs_skill", "skill_id"),
        CheckConstraint(
            in_check("trigger", RUN_TRIGGERS), name="ck_meeting_runs_trigger"
        ),
        CheckConstraint(
            in_check("status", RUN_STATUSES), name="ck_meeting_runs_status"
        ),
        CheckConstraint(lower_check("owner_email"), name="ck_meeting_runs_owner_lower"),
    )

    @validates("owner_email")
    def _fold_owner(self, _key, value):
        return _fold(value)


class MeetingRoutine(Base):
    """ "When a transcript of mine is ready, run this skill." Private to its owner.

    A routine on a clock is a scheduled task, not a row here (see the module
    docstring); this table holds only the event-triggered kind.
    """

    __tablename__ = "meeting_routines"

    id = Column(String, primary_key=True, default=_uuid)
    owner_email = Column(String, nullable=False)
    name = Column(String, nullable=False)
    skill_id = Column(
        String,
        ForeignKey("meeting_skills.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_kind = Column(String, nullable=False, default="transcript_ready")
    #: {"title_contains": [...], "external_only": bool}
    filter = Column(JSON, nullable=False, default=dict)
    deliver_slack = Column(Boolean, nullable=False, default=True)
    status = Column(String, nullable=False, default="active")
    paused_reason = Column(String, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    fire_count = Column(Integer, nullable=False, default=0)
    last_fired_at = Column(DateTime, nullable=True)
    last_status = Column(String, nullable=True)
    #: Transcripts approved before this instant never fire it: a routine
    #: created today is not a backfill over every old meeting.
    active_since = Column(DateTime, nullable=False, default=datetime.utcnow)
    source_session_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_meeting_routines_owner_status", "owner_email", "status"),
        CheckConstraint(
            in_check("trigger_kind", ROUTINE_TRIGGERS),
            name="ck_meeting_routines_trigger",
        ),
        CheckConstraint(
            in_check("status", ROUTINE_STATUSES), name="ck_meeting_routines_status"
        ),
        CheckConstraint(
            lower_check("owner_email"), name="ck_meeting_routines_owner_lower"
        ),
    )

    @validates("owner_email")
    def _fold_owner(self, _key, value):
        return _fold(value)


class MeetingRoutineFire(Base):
    """One routine firing for one transcript. The unique pair is the dedupe."""

    __tablename__ = "meeting_routine_fires"

    id = Column(String, primary_key=True, default=_uuid)
    routine_id = Column(
        String,
        ForeignKey("meeting_routines.id", ondelete="CASCADE"),
        nullable=False,
    )
    doc_id = Column(String, nullable=False)
    owner_email = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued")
    skill_run_id = Column(String, nullable=True)
    chat_session_id = Column(String, nullable=True)
    delivery = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("routine_id", "doc_id", name="uq_meeting_routine_fires_doc"),
        Index("ix_meeting_routine_fires_status_created", "status", "created_at"),
        CheckConstraint(
            in_check("status", FIRE_STATUSES), name="ck_meeting_routine_fires_status"
        ),
        CheckConstraint(
            lower_check("owner_email"), name="ck_meeting_routine_fires_owner_lower"
        ),
    )

    @validates("owner_email")
    def _fold_owner(self, _key, value):
        return _fold(value)
