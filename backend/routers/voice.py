"""Voice input — ``POST /api/voice/transcribe``.

Gate, per the repo convention: ``VOICE_INPUT_ENABLED`` off answers 404 (there is
no such surface in this environment), a person outside the ``voice_input``
roster answers 403. Mounted on ``core`` in ``backend/main.py``, so the copilot
pilot gate applies first.

The clip is read into memory, re-encoded from the samples the checks below
measured, sent to Vertex once and dropped. Nothing is written to the
database, to storage or to the logs here; the words go back to the browser,
which in Ask Cubie sends them as the person's chat message (kept like any
other message).

Before Vertex is called, the server measures the clip itself
(``rubik_core/voice/signal.py``): a recording in which nobody spoke is answered
``no_speech`` without a model call, because Gemini turns silence into a
plausible question rather than saying it heard nothing.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.voice.signal import (
    Clip,
    UnreadableAudio,
    heard_speech,
    read_wav,
    to_wav,
)
from rubik_core.voice.transcribe import (
    SURFACE_LABELS,
    TranscriptionFailed,
    normalize_mime_type,
    transcribe_clip,
)

logger = logging.getLogger(__name__)

FEATURE_KEY = "voice_input"


def require_voice_input(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Gate for the whole router — 404 when switched off, 403 when not on it."""
    if not settings.VOICE_INPUT_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows(FEATURE_KEY, user):
        raise HTTPException(
            status_code=403,
            detail="Voice input is not turned on for you. Ask your admin.",
        )
    return user


router = APIRouter(
    prefix="/voice", tags=["voice"], dependencies=[Depends(require_voice_input)]
)


class TranscribeOut(BaseModel):
    text: str
    no_speech: bool
    seconds: float | None = None


def people_names(db: Session) -> list[str]:
    """Everyone's name as they spell it, so dictated names come back right.

    Best-effort: a database hiccup costs spelling hints, never the transcript.
    """
    from rubik_core.database.models import User

    try:
        return [
            name
            for (name,) in db.query(User.name)
            .filter(User.is_platform_support.is_(False))
            .all()
            if name
        ]
    except Exception:  # noqa: BLE001
        logger.warning("[voice] could not load names for spelling hints", exc_info=True)
        return []


async def read_upload(audio: UploadFile, *, max_seconds: int, too_long: str) -> Clip:
    """The uploaded recording, checked: a WAV Cubie made, readable, not too long.

    Shared with voice commands (``backend/routers/voice_commands.py``), which
    take shorter recordings through the same checks.
    """
    if normalize_mime_type(audio.content_type) is None:
        raise HTTPException(
            status_code=415,
            detail=f"Voice input expects the recording {PRODUCT_NAME} makes in your browser. Reload the page and try again.",
        )
    limit = settings.VOICE_MAX_UPLOAD_BYTES
    data = await audio.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=too_long)
    try:
        clip = read_wav(data)
    except UnreadableAudio:
        raise HTTPException(
            status_code=422, detail="That recording couldn't be read. Please try again."
        ) from None
    # A little slack over the browser's own stop, which lands a frame or two late.
    if clip.seconds > max_seconds + 2:
        raise HTTPException(status_code=413, detail=too_long)
    return clip


@router.post("/transcribe", response_model=TranscribeOut)
async def transcribe(
    audio: UploadFile = File(...),
    surface: str = Form("chat"),
    user: UserProfile = Depends(require_voice_input),
    db: Session = Depends(get_db),
) -> TranscribeOut:
    clip = await read_upload(
        audio,
        max_seconds=settings.VOICE_MAX_SECONDS,
        too_long=f"That recording is too long. Keep it under {settings.VOICE_MAX_SECONDS // 60} minutes.",
    )
    if not heard_speech(clip):
        # No model call, so no cost and no invented sentence.
        return TranscribeOut(text="", no_speech=True, seconds=round(clip.seconds, 1))

    people = await run_in_threadpool(people_names, db)
    try:
        result = await transcribe_clip(
            # The samples that were measured, re-encoded; never the raw upload.
            to_wav(clip),
            "audio/wav",
            people=people,
            surface=surface if surface in SURFACE_LABELS else "chat",
            user_email=user.email,
            clip_seconds=clip.seconds,
        )
    except TranscriptionFailed:
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} couldn't turn that recording into text. Please try again.",
        ) from None
    return TranscribeOut(
        text=result.text, no_speech=result.no_speech, seconds=round(clip.seconds, 1)
    )
