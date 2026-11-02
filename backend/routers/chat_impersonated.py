"""Ask Cubie while impersonating a person — HubSpot only, nothing saved.

    POST /api/chat/impersonated

The one write the person-impersonation lock lets through (see
``rubik_core/auth/impersonation.py``). It answers with
``rubik_core.agent_runtime.hubspot_only_chat``: two HubSpot tools, no
sub-agents, an in-memory session. Nothing is written to the person's chat
history — no ``ChatSession``, ``ChatMessage`` or ``InteractionLog`` row — so
follow-up context travels in ``history`` from the browser.

Refused unless an impersonation is actually in force for this request (the
header is present AND the server granted the swap). A plain user, or an admin
whose header was refused, gets a 403 rather than a HubSpot-only chat as
themselves.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.audit import record_audit
from backend.auth import get_current_user
from rubik_core.agent_runtime.hubspot_only_chat import run_hubspot_only_turn
from rubik_core.ai_providers.registry import IncompatibleModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.auth.impersonation import impersonation_active
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat/impersonated", tags=["chat-impersonated"])

TURN_TIMEOUT_SECONDS = 90


class Turn(BaseModel):
    role: str
    content: str = Field(max_length=20_000)


class ImpersonatedChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    history: list[Turn] = Field(default_factory=list, max_length=40)
    timezone: str = "UTC"


class ImpersonatedChatResponse(BaseModel):
    answer: str
    request_id: str
    #: Which tools ran — always a subset of the two HubSpot tools.
    tools_used: list[str]


@router.post("", response_model=ImpersonatedChatResponse)
async def chat_impersonated(
    body: ImpersonatedChatRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    impersonation = getattr(request.state, "impersonation", None)
    if not impersonation_active() or not impersonation:
        raise HTTPException(
            status_code=403,
            detail="This chat is only for an admin impersonating someone.",
        )
    request_id = uuid.uuid4().hex[:8]
    # Who asked, as whom — never the question itself (nothing is stored).
    record_audit(
        db,
        actor_email=impersonation["actor_email"],
        action="IMPERSONATE_CHAT",
        target=impersonation["target_email"],
        details={"request_id": request_id, "chars": len(body.query)},
    )
    history = [(t.role, t.content) for t in body.history if t.content]
    try:
        result = await asyncio.wait_for(
            run_hubspot_only_turn(
                user_email=user.email,
                user_name=user.name or user.email,
                query=body.query,
                history=history,
                timezone=body.timezone,
                request_id=request_id,
            ),
            timeout=TURN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504, detail="HubSpot took too long to answer. Try again."
        )
    except (IncompatibleModelRoute, ProviderRequestError):
        raise HTTPException(
            status_code=502,
            detail="The selected workspace AI model could not answer. Check its connection in Admin → Workspace.",
        ) from None
    try:
        from rubik_core.cost.usage import record_usage

        # Billed to the admin who asked, under its own AI Cost category.
        record_usage(
            operation="generate",
            model=result.model_id,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            thinking_tokens=result.thinking_tokens,
            feature="chat_impersonation",
            user_email=impersonation["actor_email"],
            request_id=request_id,
            metadata={
                "impersonating": impersonation["target_email"],
                "provider": result.provider,
                "billing_owner": result.billing_owner,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[%s] impersonation chat cost record failed", request_id)
    return ImpersonatedChatResponse(
        answer=result.answer
        or "HubSpot did not return anything for that. Try rephrasing the question.",
        request_id=request_id,
        tools_used=result.tool_calls,
    )
