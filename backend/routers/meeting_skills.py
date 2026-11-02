"""The "/" menu in the Ask Cubie chat box: the person's skills and templates.

Read-only. Everything else about Meeting Skills — creating, running, changing,
deleting — happens in the conversation itself (``rubik_core/tools/
meeting_skills_tools.py``), which is the feature's only surface. This route
exists so the chat box can list what "/" can run without a model call.

Scoped to the signed-in person, and gated on the ``meeting_skills`` feature:
404 when the kill switch is off (there is no such surface here), 403 when the
person is not on the Feature Access roster yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.features import feature_allows

router = APIRouter(prefix="/meeting-skills", tags=["meeting-skills"])


def require_meeting_skills(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    if not settings.MEETING_SKILLS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows("meeting_skills", user):
        raise HTTPException(
            status_code=403,
            detail="Meeting skills are not turned on for you. Ask your admin.",
        )
    return user


class MenuEntry(BaseModel):
    slug: str
    name: str
    description: str = ""


class MenuOut(BaseModel):
    skills: list[MenuEntry]
    templates: list[MenuEntry]


@router.get("/menu", response_model=MenuOut)
def slash_menu(user: UserProfile = Depends(require_meeting_skills)) -> MenuOut:
    """Skills (recently and most used first) and templates, for the "/" menu."""
    from rubik_core.meeting_skills import store

    data = store.menu(user.email)
    return MenuOut(
        skills=[MenuEntry(**s) for s in data["skills"]],
        templates=[MenuEntry(**t) for t in data["templates"]],
    )
