"""UC-11: per-user writing-style preferences.

`GET /api/preferences`  → current user's prefs (defaults if none saved).
`PUT /api/preferences`  → upsert.
`POST /api/preferences/preview` → render a sample email using the saved prefs
so the user can see their voice in action before relying on it everywhere.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.services.user_context import build_user_context_block, get_user_preferences
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import UserPreference, UserVoiceShortcut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/preferences", tags=["preferences"])


Tone = Literal["formal", "conversational", "direct"]
Verbosity = Literal["concise", "balanced", "detailed"]
Modifier = Literal["alt", "ctrl", "meta", "shift"]


class VoiceChord(BaseModel):
    """A keyboard shortcut: modifiers alone (``code`` null, e.g. Option+Command)
    or modifiers with one key (``code`` is ``KeyboardEvent.code``, e.g. Space).
    At least one modifier, so a shortcut can never fire while someone types."""

    mods: list[Modifier] = Field(min_length=1, max_length=4)
    code: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9]+$")


class VoiceShortcut(BaseModel):
    """The voice command shortcut, per kind of keyboard. Left out = default."""

    mac: VoiceChord | Literal["off"] | None = None
    pc: VoiceChord | Literal["off"] | None = None


class PreferencesPayload(BaseModel):
    # ── Email voice (UC-11) ──────────────────────────────────────────────
    writing_style: str | None = Field(
        default=None,
        description="Free-form description of the user's preferred writing style.",
    )
    tone: Tone | None = None
    preferred_greeting: str | None = None
    email_signature: str | None = None
    additional_instructions: str | None = None
    # ── General tab ──────────────────────────────────────────────────────
    timezone: str | None = Field(
        default=None, description="IANA timezone name, e.g. 'America/New_York'."
    )
    working_hours_start: str | None = Field(
        default=None, description="Local start of working hours, 'HH:MM'."
    )
    working_hours_end: str | None = Field(
        default=None, description="Local end of working hours, 'HH:MM'."
    )
    muted_notification_kinds: list[str] | None = Field(
        default=None,
        description="Notification.kind values to hide from the in-app bell.",
    )
    # ── AI Personalization tab ───────────────────────────────────────────
    role_title: str | None = None
    about_me: str | None = Field(
        default=None,
        description="What the user is responsible for and focuses on; injected "
        "into Chat, AI Priorities, and meeting prep.",
    )
    chat_verbosity: Verbosity | None = None
    response_language: str | None = Field(
        default=None, description="'auto' or a BCP-47 code (e.g. 'es')."
    )
    priorities_focus: str | None = None
    priorities_max: int | None = Field(default=None, ge=1, le=10)
    priorities_sources: list[str] | None = None
    priorities_muted_topics: list[str] | None = None
    # ── Voice commands ───────────────────────────────────────────────────
    voice_shortcut: VoiceShortcut | None = None


class PreferencesResponse(PreferencesPayload):
    user_email: str
    updated_at: datetime | None = None


# Columns the API exposes (and the merge-update writes). Field names match the
# UserPreference column names 1:1 so a payload value maps by setattr.
_PREF_FIELDS: tuple[str, ...] = (
    "writing_style",
    "tone",
    "preferred_greeting",
    "email_signature",
    "additional_instructions",
    "timezone",
    "working_hours_start",
    "working_hours_end",
    "muted_notification_kinds",
    "role_title",
    "about_me",
    "chat_verbosity",
    "response_language",
    "priorities_focus",
    "priorities_max",
    "priorities_sources",
    "priorities_muted_topics",
)


def _stored_shortcut(raw) -> VoiceShortcut | None:
    """A stored shortcut that no longer reads (hand-edited, an older shape) is
    the default again, never a broken Settings page."""
    if not raw:
        return None
    try:
        return VoiceShortcut.model_validate(raw)
    except ValidationError:
        logger.warning("preferences: ignoring an unreadable voice_shortcut")
        return None


def _read_shortcut(db: Session, user_email: str) -> VoiceShortcut | None:
    """The saved shortcut, or None for the default. Lives in its own table
    (``user_voice_shortcuts``); if that cannot be read, Settings still loads."""
    try:
        row = db.get(UserVoiceShortcut, user_email.lower())
    except SQLAlchemyError:
        db.rollback()
        logger.warning("preferences: voice shortcuts could not be read")
        return None
    return _stored_shortcut(row.shortcut if row else None)


def _write_shortcut(
    db: Session, user_email: str, shortcut: VoiceShortcut | None
) -> None:
    """Save (or clear, for the default) the person's shortcut, and commit."""
    key = user_email.lower()
    value = shortcut.model_dump(exclude_none=True) if shortcut else {}
    try:
        row = db.get(UserVoiceShortcut, key)
        if not value:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(
                UserVoiceShortcut(
                    user_email=key, shortcut=value, updated_at=datetime.utcnow()
                )
            )
        else:
            row.shortcut = value
            row.updated_at = datetime.utcnow()
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("preferences: voice shortcut could not be saved", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Your other settings were saved, but the voice shortcut couldn't be. Try again in a moment.",
        ) from None


def _to_response(
    user_email: str, prefs: UserPreference | None, shortcut: VoiceShortcut | None
) -> PreferencesResponse:
    if prefs is None:
        return PreferencesResponse(user_email=user_email, voice_shortcut=shortcut)
    values = {f: getattr(prefs, f) for f in _PREF_FIELDS}
    return PreferencesResponse(
        user_email=user_email,
        updated_at=prefs.updated_at,
        voice_shortcut=shortcut,
        **values,  # type: ignore[arg-type]
    )


@router.get("", response_model=PreferencesResponse)
async def get_preferences(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    prefs = get_user_preferences(db, user.email)
    return _to_response(user.email, prefs, _read_shortcut(db, user.email))


@router.put("", response_model=PreferencesResponse)
async def put_preferences(
    body: PreferencesPayload,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    prefs = get_user_preferences(db, user.email)
    if prefs is None:
        prefs = UserPreference(user_email=user.email)
        db.add(prefs)

    # Partial merge: only overwrite columns the caller actually sent. Each
    # Settings tab (Email / General / AI Personalization) PUTs just its own
    # fields, so they never clobber each other. An explicit ``null`` still
    # counts as "sent" and clears the field; an omitted field is left as-is.
    sent = body.model_dump(exclude_unset=True)
    for field, value in sent.items():
        if field in _PREF_FIELDS:
            setattr(prefs, field, value)
    db.commit()
    db.refresh(prefs)
    # The voice shortcut is kept in its own table (see UserVoiceShortcut), and
    # saved after the rest so a failure there never loses the other settings.
    if "voice_shortcut" in body.model_fields_set:
        _write_shortcut(db, user.email, body.voice_shortcut)
    return _to_response(user.email, prefs, _read_shortcut(db, user.email))


class PreviewResponse(BaseModel):
    subject: str | None
    body: str | None
    raw_response: str


@router.post("/preview", response_model=PreviewResponse)
async def preview_style(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a sample email using the user's saved prefs.

    Uses a synthetic recipient so the user can see how their preferences
    shape the agent's output. The generated draft is NOT saved to Gmail
    (the underlying agent only saves real recipients via `/email/draft`).
    """
    block = build_user_context_block(user, db)
    if not block:
        raise HTTPException(
            status_code=400,
            detail="Save your preferences first — there's nothing to preview yet.",
        )

    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.genai import types

    from rubik_core.adk_agent.sales_email_agent import build_sales_email_agent
    from rubik_core.adk_agent.tools import AgentTools
    from rubik_core.ai_providers.adk_model import resolve_adk_route
    from rubik_core.ai_providers.registry import IncompatibleModelRoute
    from rubik_core.ai_providers.runtime import ProviderRequestError
    from rubik_core.config import settings
    from rubik_core.cost.usage import extract_usage, record_usage

    tools_instance = AgentTools(user_email=user.email)
    try:
        route = resolve_adk_route(
            "sales_email_agent", default_model=settings.VERTEX_LLM_MODEL_FLASH
        )
        agent = build_sales_email_agent(
            tools_instance.gmail_create_draft, model_route=route
        )
    except (IncompatibleModelRoute, ProviderRequestError):
        raise HTTPException(
            status_code=502,
            detail="The selected workspace AI model is unavailable. Check its connection in Admin → Workspace.",
        ) from None

    session_service = InMemorySessionService()
    app_name = "preferences_preview"
    user_id = user.email
    session_id = f"preview-{datetime.utcnow().timestamp()}"
    await session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )

    runner = Runner(app_name=app_name, agent=agent, session_service=session_service)

    user_message = (
        f"{block}\n\n"
        "Task: write a SAMPLE follow-up email to a fictional prospect named Alex Rivera, "
        "VP of Operations at Globex, after a discovery call last week where they "
        "raised concerns about their current vendor's turnaround time. This is a preview to validate "
        "the user's writing-style preferences. Do NOT call gmail_create_draft — only output "
        "the email in the Subject/body format defined by your output instructions."
    )
    user_content = types.Content(
        role="user", parts=[types.Part.from_text(text=user_message)]
    )

    raw_chunks: list[str] = []
    prompt_tokens = completion_tokens = thinking_tokens = cached_tokens = 0
    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            if (
                not getattr(event, "partial", False)
                and getattr(event, "usage_metadata", None) is not None
            ):
                usage = extract_usage(event)
                prompt_tokens += usage["prompt"]
                completion_tokens += usage["completion"]
                thinking_tokens += usage["thinking"]
                cached_tokens += usage["cached"]
            if event.is_final_response() and event.content:
                raw_chunks.append(
                    "".join(p.text for p in event.content.parts if p.text)
                )
    except Exception as exc:
        if route.provider == "vertex_managed":
            logger.exception("Preview generation failed for %s", user.email)
        else:
            logger.warning(
                "Selected preview provider failed for %s (%s)",
                user.email,
                type(exc).__name__,
            )
        raise HTTPException(
            status_code=502,
            detail="The selected workspace AI model could not generate a preview. Check its connection in Admin → Workspace.",
        ) from None

    try:
        record_usage(
            operation="generate",
            model=route.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            thinking_tokens=thinking_tokens,
            cached_tokens=cached_tokens,
            feature="sales_email_agent",
            user_email=user.email,
            metadata={
                "provider": route.provider,
                "billing_owner": route.billing_owner,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("Preview AI cost record failed", exc_info=True)

    raw_response = "\n".join(c for c in raw_chunks if c).strip()
    if not raw_response:
        raise HTTPException(
            status_code=502, detail="Preview agent returned no response."
        )

    # Reuse the email router's parser so subject/body extraction stays consistent.
    from backend.routers.email import _parse_draft_response

    subject, body, _ = _parse_draft_response(raw_response)
    return PreviewResponse(subject=subject, body=body, raw_response=raw_response)
