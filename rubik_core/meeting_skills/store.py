"""The only writer of the Meeting Skills tables.

Every function takes the OWNER's email and touches only that person's rows:
templates and skills are private, and nothing here can read or change someone
else's. Saving under an existing name makes a new version (the old shape is
kept in ``meeting_definition_versions``) rather than a second copy.

Functions return plain dicts, never ORM rows, so a caller cannot hold a row
past its session.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.meeting_skills import (
    DELIVER_AS,
    DETAIL_LEVELS,
    MEETING_SCOPES,
    MeetingDefinitionVersion,
    MeetingSkill,
    MeetingSkillRun,
    MeetingTemplate,
)
from rubik_core.meeting_skills.sections import Section, sections_to_json


class StoreError(ValueError):
    """A save or lookup the person needs to hear about. The message is for them."""


def _session():
    from rubik_core.database import db as _db

    return _db.SessionLocal()


def _owner(email: str) -> str:
    return (email or "").strip().lower()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:40].strip("-")


def _key(name: str) -> str:
    return " ".join((name or "").lower().split())


def template_view(row: MeetingTemplate) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "sections": list(row.sections or []),
        "version": row.version,
        "updated_at": row.updated_at,
    }


def skill_view(
    row: MeetingSkill, template: MeetingTemplate | None = None
) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description or "",
        "instructions": row.instructions,
        "template_id": row.template_id,
        "template_name": template.name if template else "",
        "template_version": template.version if template else None,
        "sections": list(template.sections or []) if template else [],
        "tone": row.tone or "",
        "detail_level": row.detail_level,
        "meetings": row.meetings,
        "window_days": row.window_days,
        "deliver_as": row.deliver_as,
        "exclude_personal": bool(row.exclude_personal),
        "version": row.version,
        "invocation_count": row.invocation_count,
    }


def _record_version(
    db, kind: str, definition_id: str, version: int, payload: dict
) -> None:
    db.add(
        MeetingDefinitionVersion(
            kind=kind, definition_id=definition_id, version=version, payload=payload
        )
    )


# ── templates ───────────────────────────────────────────────────────────────


def _find_template(db, owner: str, name_or_id: str) -> MeetingTemplate | None:
    ref = (name_or_id or "").strip().lstrip("/")
    if not ref:
        return None
    q = db.query(MeetingTemplate).filter(MeetingTemplate.owner_email == owner)
    found = (
        q.filter(MeetingTemplate.id == ref).first()
        or q.filter(MeetingTemplate.name_key == _key(ref)).first()
    )
    if found is None:
        # The "/" menu writes a template as its slug ("/bant-note").
        slug = slugify(ref)
        found = (
            next((t for t in q.all() if slugify(t.name) == slug), None)
            if slug
            else None
        )
    return found


def save_template(
    owner_email: str,
    name: str,
    sections: list[Section],
    *,
    description: str = "",
    created_via: str = "chat",
    source_session_id: str | None = None,
) -> tuple[dict, bool]:
    """Create the template, or a new version of the one with this name.

    Returns ``(template, created)``. Raises :class:`StoreError`.
    """
    owner = _owner(owner_email)
    name = " ".join((name or "").split())[:80]
    if not name:
        raise StoreError("The template needs a name.")
    stored = sections_to_json(sections)
    with _session() as db:
        row = _find_template(db, owner, name)
        created = row is None
        if created:
            count = (
                db.query(MeetingTemplate)
                .filter(MeetingTemplate.owner_email == owner)
                .count()
            )
            if count >= settings.MEETING_SKILLS_MAX_TEMPLATES_PER_USER:
                raise StoreError(
                    f"You already have {count} templates, the most {PRODUCT_NAME} keeps per "
                    "person. Delete one you no longer use first."
                )
            row = MeetingTemplate(
                owner_email=owner,
                name=name,
                name_key=_key(name),
                description=(description or "").strip() or None,
                sections=stored,
                version=1,
                created_via=created_via
                if created_via in ("chat", "upload", "output")
                else "chat",
                source_session_id=source_session_id,
            )
            db.add(row)
            db.flush()
        else:
            row.sections = stored
            row.version = (row.version or 1) + 1
            if (description or "").strip():
                row.description = description.strip()
            row.updated_at = datetime.utcnow()
        _record_version(
            db,
            "template",
            row.id,
            row.version,
            {"name": row.name, "description": row.description, "sections": stored},
        )
        db.commit()
        db.refresh(row)
        return template_view(row), created


def get_template(owner_email: str, name_or_id: str) -> dict | None:
    with _session() as db:
        row = _find_template(db, _owner(owner_email), name_or_id)
        return template_view(row) if row else None


def list_templates(owner_email: str) -> list[dict]:
    with _session() as db:
        rows = (
            db.query(MeetingTemplate)
            .filter(MeetingTemplate.owner_email == _owner(owner_email))
            .order_by(MeetingTemplate.name_key)
            .all()
        )
        return [template_view(r) for r in rows]


def delete_template(owner_email: str, name_or_id: str) -> dict:
    """Delete it and its history. Skills that used it keep running with the
    default outline. Raises :class:`StoreError` when there is no such template."""
    owner = _owner(owner_email)
    with _session() as db:
        row = _find_template(db, owner, name_or_id)
        if row is None:
            raise StoreError(f"You have no template called '{name_or_id}'.")
        view = template_view(row)
        used_by = [
            s.name
            for s in db.query(MeetingSkill)
            .filter(
                MeetingSkill.owner_email == owner, MeetingSkill.template_id == row.id
            )
            .all()
        ]
        db.query(MeetingDefinitionVersion).filter(
            MeetingDefinitionVersion.kind == "template",
            MeetingDefinitionVersion.definition_id == row.id,
        ).delete(synchronize_session=False)
        db.delete(row)
        db.commit()
        view["used_by"] = used_by
        return view


# ── skills ──────────────────────────────────────────────────────────────────


def _find_skill(db, owner: str, ref: str) -> MeetingSkill | None:
    ref = (ref or "").strip().lstrip("/")
    if not ref:
        return None
    q = db.query(MeetingSkill).filter(MeetingSkill.owner_email == owner)
    return (
        q.filter(MeetingSkill.id == ref).first()
        or q.filter(MeetingSkill.slug == slugify(ref)).first()
    )


def save_skill(
    owner_email: str,
    name: str,
    instructions: str,
    *,
    template_id: str | None = None,
    description: str = "",
    tone: str = "",
    detail_level: str = "standard",
    meetings: str = "one",
    window_days: int = 7,
    deliver_as: str = "chat",
    exclude_personal: bool = True,
    created_via: str = "chat",
    source_session_id: str | None = None,
) -> tuple[dict, bool]:
    """Create the skill, or a new version of the one with this name.

    Returns ``(skill, created)``. Raises :class:`StoreError`.
    """
    owner = _owner(owner_email)
    name = " ".join((name or "").split())[:80]
    slug = slugify(name)
    if not slug:
        raise StoreError("The skill needs a name made of letters or numbers.")
    instructions = (instructions or "").strip()
    if not instructions:
        raise StoreError(
            f"The skill needs instructions: what {PRODUCT_NAME} should do each time."
        )
    if len(instructions) > settings.MEETING_SKILLS_MAX_INSTRUCTION_CHARS:
        raise StoreError(
            "The instructions are too long; keep them under "
            f"{settings.MEETING_SKILLS_MAX_INSTRUCTION_CHARS} characters."
        )
    if detail_level not in DETAIL_LEVELS:
        raise StoreError(f"Detail must be one of: {', '.join(DETAIL_LEVELS)}.")
    if meetings not in MEETING_SCOPES:
        raise StoreError("Meetings must be 'one' (a meeting at a time) or 'many'.")
    if deliver_as not in DELIVER_AS:
        raise StoreError(f"Delivery must be one of: {', '.join(DELIVER_AS)}.")
    window_days = max(1, min(int(window_days or 7), 31))
    with _session() as db:
        template = None
        if template_id:
            template = (
                db.query(MeetingTemplate)
                .filter(
                    MeetingTemplate.owner_email == owner,
                    MeetingTemplate.id == template_id,
                )
                .first()
            )
            if template is None:
                raise StoreError("That template is not one of yours.")
        row = _find_skill(db, owner, slug)
        created = row is None
        if created:
            count = (
                db.query(MeetingSkill).filter(MeetingSkill.owner_email == owner).count()
            )
            if count >= settings.MEETING_SKILLS_MAX_SKILLS_PER_USER:
                raise StoreError(
                    f"You already have {count} skills, the most {PRODUCT_NAME} keeps per "
                    "person. Delete one you no longer use first."
                )
            row = MeetingSkill(
                owner_email=owner, slug=slug, version=1, invocation_count=0
            )
            db.add(row)
        else:
            row.version = (row.version or 1) + 1
        row.name = name
        row.description = (description or "").strip() or None
        row.instructions = instructions
        row.template_id = template.id if template else None
        row.tone = (tone or "").strip()[:200] or None
        row.detail_level = detail_level
        row.meetings = meetings
        row.window_days = window_days
        row.deliver_as = deliver_as
        row.exclude_personal = bool(exclude_personal)
        row.created_via = (
            created_via if created_via in ("chat", "upload", "output") else "chat"
        )
        row.source_session_id = row.source_session_id or source_session_id
        row.updated_at = datetime.utcnow()
        db.flush()
        view = skill_view(row, template)
        _record_version(
            db,
            "skill",
            row.id,
            row.version,
            {k: v for k, v in view.items() if k not in ("id", "invocation_count")},
        )
        db.commit()
        return view, created


def get_skill(owner_email: str, ref: str) -> dict | None:
    owner = _owner(owner_email)
    with _session() as db:
        row = _find_skill(db, owner, ref)
        if row is None:
            return None
        template = (
            db.query(MeetingTemplate)
            .filter(MeetingTemplate.id == row.template_id)
            .first()
            if row.template_id
            else None
        )
        return skill_view(row, template)


def list_skills(owner_email: str) -> list[dict]:
    owner = _owner(owner_email)
    with _session() as db:
        rows = (
            db.query(MeetingSkill)
            .filter(MeetingSkill.owner_email == owner)
            .order_by(MeetingSkill.slug)
            .all()
        )
        templates = {
            t.id: t
            for t in db.query(MeetingTemplate)
            .filter(MeetingTemplate.owner_email == owner)
            .all()
        }
        return [skill_view(r, templates.get(r.template_id)) for r in rows]


def delete_skill(owner_email: str, ref: str) -> dict:
    """Delete it, its history and any routine that runs it. Raises
    :class:`StoreError` when there is no such skill."""
    owner = _owner(owner_email)
    with _session() as db:
        row = _find_skill(db, owner, ref)
        if row is None:
            raise StoreError(f"You have no skill called '{ref}'.")
        view = skill_view(row)
        from rubik_core.database.meeting_skills import MeetingRoutine

        routines = (
            db.query(MeetingRoutine)
            .filter(
                MeetingRoutine.owner_email == owner, MeetingRoutine.skill_id == row.id
            )
            .all()
        )
        view["routines_removed"] = [r.name for r in routines]
        for routine in routines:
            db.delete(routine)
        db.query(MeetingDefinitionVersion).filter(
            MeetingDefinitionVersion.kind == "skill",
            MeetingDefinitionVersion.definition_id == row.id,
        ).delete(synchronize_session=False)
        db.delete(row)
        db.commit()
        return view


def record_invocation(skill_id: str) -> None:
    with _session() as db:
        row = db.query(MeetingSkill).filter(MeetingSkill.id == skill_id).first()
        if row is not None:
            row.invocation_count = (row.invocation_count or 0) + 1
            row.last_invoked_at = datetime.utcnow()
            db.commit()


def menu(owner_email: str) -> dict[str, list[dict]]:
    """What the "/" menu in the chat box lists: skills (most used in the last
    30 days first) and templates."""
    owner = _owner(owner_email)
    recent = datetime.utcnow() - timedelta(days=30)
    with _session() as db:
        skills = db.query(MeetingSkill).filter(MeetingSkill.owner_email == owner).all()
        templates = (
            db.query(MeetingTemplate)
            .filter(MeetingTemplate.owner_email == owner)
            .order_by(MeetingTemplate.name_key)
            .all()
        )

        def rank(s: MeetingSkill):
            used_lately = bool(s.last_invoked_at and s.last_invoked_at >= recent)
            return (not used_lately, -(s.invocation_count or 0), s.slug)

        return {
            "skills": [
                {
                    "slug": s.slug,
                    "name": s.name,
                    "description": s.description or s.instructions[:120],
                }
                for s in sorted(skills, key=rank)
            ],
            "templates": [
                {
                    "slug": slugify(t.name),
                    "name": t.name,
                    "description": t.description
                    or ", ".join(sec.get("title", "") for sec in (t.sections or []))[
                        :120
                    ],
                }
                for t in templates
            ],
        }


# ── runs ────────────────────────────────────────────────────────────────────


def record_run(**fields: Any) -> str:
    """Persist one run. Returns its id. Never raises: losing the log line must
    not cost the person their answer."""
    import logging

    try:
        fields["owner_email"] = _owner(fields.get("owner_email", ""))
        with _session() as db:
            row = MeetingSkillRun(**fields)
            db.add(row)
            db.commit()
            return row.id
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("[meeting-skills] run log failed")
        return ""
