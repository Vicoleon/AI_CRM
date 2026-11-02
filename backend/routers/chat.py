from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Literal

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from backend.models.chat import (
    ChatFolderResponse,
    ChatSessionDetailResponse,
    ChatSessionResponse,
)
from rubik_core.ai_providers.registry import IncompatibleModelRoute, ModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.database.db import get_db
from rubik_core.database.models import (
    ChatFolder,
    ChatMessage,
    ChatSession,
    InteractionLog,
)
from rubik_core.identifiers import ADK_COORDINATOR_NAME

logger = logging.getLogger(__name__)
_SELECTED_MODEL_ERROR = (
    "The selected workspace AI model could not answer. "
    "Check its connection in Admin → Workspace."
)

# ── Tunable budgets ───────────────────────────────────────────────
MAX_HISTORY_MESSAGES = 8  # last N messages sent to router / chain
MAX_HISTORY_CHARS = 16000  # hard cap on total chars of history content
REQUEST_TIMEOUT_SECONDS = 90  # overall wall-clock timeout for a chat request


def _model_cost_info(orchestrator) -> tuple[str, dict[str, str] | None]:
    route = getattr(orchestrator, "model_route", None)
    if isinstance(route, ModelRoute):
        return route.model, {
            "provider": route.provider,
            "billing_owner": route.billing_owner,
        }
    model = getattr(orchestrator, "model_id", None)
    return (
        model if isinstance(model, str) else settings.VERTEX_LLM_MODEL_FLASH,
        None,
    )


def _record_chat_usage(
    orchestrator,
    usage_by_agent: dict[str, dict[str, int]] | None,
    totals: dict[str, int],
    *,
    user_email: str,
    request_id: str,
) -> None:
    """Bill each ADK agent to the route that generated its tokens."""
    from rubik_core.cost.usage import record_usage

    if not usage_by_agent:
        model, metadata = _model_cost_info(orchestrator)
        record_usage(
            operation="generate",
            model=model,
            prompt_tokens=totals["prompt"],
            completion_tokens=totals["completion"],
            total_tokens=totals["total"],
            thinking_tokens=totals["thinking"],
            cached_tokens=totals["cached"],
            feature="chat",
            user_email=user_email,
            request_id=request_id,
            metadata=metadata,
        )
        return
    for author, usage in usage_by_agent.items():
        route = (
            getattr(orchestrator, "sales_model_route", None)
            if author == "sales_email_agent"
            else getattr(orchestrator, "model_route", None)
        )
        feature = "sales_email_agent" if author == "sales_email_agent" else "chat"
        if author not in {ADK_COORDINATOR_NAME, "sales_email_agent"}:
            logger.warning("Unrecognized ADK usage author %s", author)
        if isinstance(route, ModelRoute):
            model = route.model
            metadata = {
                "provider": route.provider,
                "billing_owner": route.billing_owner,
            }
        else:
            model, metadata = _model_cost_info(orchestrator)
        record_usage(
            operation="generate",
            model=model,
            prompt_tokens=usage["prompt"],
            completion_tokens=usage["completion"],
            total_tokens=usage["total"],
            thinking_tokens=usage["thinking"],
            cached_tokens=usage["cached"],
            feature=feature,
            user_email=user_email,
            request_id=request_id,
            metadata=metadata,
        )


def _run_with_loop(fn, *args, **kwargs):
    """Run a blocking function in the current thread, ensuring it has an event loop.

    Vertex AI's gRPC SDK requires an asyncio event loop in its executing thread.
    Usually `asyncio.to_thread` runs in a thread without a loop. We create one here
    if missing, but deliberately DO NOT close it. This prevents breaking Vertex AI's
    cached gRPC channels across multiple calls on the same reused thread pool worker.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return fn(*args, **kwargs)


def _expire_stale_hitl_pointer(db_session: ChatSession | None, query: str) -> None:
    """Clear a stale HITL confirmation pointer before running a turn.

    A pending calendar/draft action parks ``last_adk_invocation_id`` and is only
    meant to be resumed by the immediately-following short affirmation. This
    drops the parked pointer when (a) the current message isn't such an
    affirmation, or (b) it was parked longer ago than the TTL — so an unrelated
    later "ok" can't silently resume a stale action (the "Migration Plan" RCA).
    ``updated_at`` approximates when the pointer was parked (it's bumped at the
    end of every turn, including the one that parked it).
    """
    if db_session is None or not getattr(db_session, "last_adk_invocation_id", None):
        return

    # Import here to reuse the orchestrator's word-boundary affirmation test
    # without pulling the ADK import graph into module load.
    from rubik_core.orchestration.adk_orchestrator import _is_confirmation_reply

    if not _is_confirmation_reply(query):
        db_session.last_adk_invocation_id = None
        return

    ttl = settings.CHAT_HITL_PENDING_TTL_SECONDS
    parked_at = getattr(db_session, "updated_at", None)
    if ttl and parked_at is not None:
        age = (datetime.utcnow() - parked_at).total_seconds()
        if age > ttl:
            logger.info(
                "Expiring stale HITL pointer (age %.0fs > %ss TTL) for session %s",
                age,
                ttl,
                getattr(db_session, "id", "?"),
            )
            db_session.last_adk_invocation_id = None


def _trim_history(
    history: list[tuple],
    max_messages: int = MAX_HISTORY_MESSAGES,
    max_chars: int = MAX_HISTORY_CHARS,
) -> list[tuple]:
    """Keep the most recent messages and cap total character count.

    Oldest messages are dropped first when the char budget is exceeded.
    """
    recent = history[-max_messages:]
    # Trim from the front until within budget
    total = sum(len(c) for _, c in recent)
    while recent and total > max_chars:
        _, dropped = recent.pop(0)
        total -= len(dropped)
    return recent


def _build_orchestrator(
    user: UserProfile,
    db: Session,
    timezone: str = "UTC",
    *,
    source_session_id: str | None = None,
):
    """Factory: return the ADK orchestrator for one live chat turn.

    The assembly itself lives in ``backend/services/chat_orchestrator.py``
    because the scheduled-task runner builds the SAME agent with the same
    context blocks from a Cloud Run Job, and a Job importing this router to
    reach a private helper is how the two paths drift apart. This wrapper stays
    so the router's call sites (and the tests pinning them) read unchanged.

    ``source_session_id`` is stamped on any scheduled task created this turn,
    so a schedule can be traced back to the conversation that asked for it.
    """
    from backend.services.chat_orchestrator import build_chat_orchestrator

    return build_chat_orchestrator(
        user,
        db,
        timezone,
        source_session_id=source_session_id,
    )


router = APIRouter(prefix="/chat", tags=["chat"])


class HistoryMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    citations: list[dict[str, Any]] | None = None
    hubspot_data: str | None = None
    generated_sql: str | None = None
    debug: dict[str, Any] | None = None


class SessionResponse(BaseModel):
    id: str
    title: str
    updated_at: str


class SessionDetailResponse(BaseModel):
    id: str
    title: str
    messages: list[HistoryMessage]


class ChatScope(BaseModel):
    """V2 scope context — when the user opens chat from a specific record page
    (deal, meeting, asset, etc.), the UI sends this so the orchestrator can
    eventually pre-filter retrieval. V2_1: accepted and logged only.
    """

    kind: Literal["workspace", "department", "deal", "meeting", "asset", "contact"]
    id: str | None = None  # HubSpot deal_id, doc_id, event_id, etc.
    label: str | None = None  # Human-readable label, e.g. "Acme Corp"


class ChatRequest(BaseModel):
    query: str
    top_k: int = 5
    session_id: str | None = None
    history: list[
        HistoryMessage
    ] = []  # Kept for backwards compatibility, but DB takes precedence
    timezone: str = "UTC"
    # V2 fields — accepted and logged only at V2_1; defaults preserve V1 behavior.
    mode: Literal["ask", "draft", "investigate"] = "ask"
    scope: ChatScope | None = None
    # Files the person attached to this message: ids from POST /api/chat/files.
    # Each must be the caller's own (a 404 otherwise), and the query may then
    # be empty.
    attachment_ids: list[str] = Field(default_factory=list)


# ── Files attached in Ask Cubie (rubik_core/chat_files) ──────────────────────

# What the model is asked when a message is only files, with no words.
_FILES_ONLY_QUERY = (
    "(The person sent the attached file(s) without a message. Say briefly what "
    "each one is and what is in it, then ask what they would like to do with it.)"
)


def _history_text(m: ChatMessage) -> str:
    """A stored message as history text, naming any files it carried."""
    content = m.content or ""
    files = (m.metadata_json or {}).get("attachments") if m.role == "user" else None
    if files:
        names = ", ".join(str(f.get("filename") or "file") for f in files)
        content = f"{content} [attached: {names}]".strip()
    return content


def _load_attachments(request: ChatRequest, user: UserProfile, db: Session) -> list:
    """The caller's own files named on this message, checked before anything
    is saved: a 403 off the roster, a 404 for any id that is not theirs."""
    ids = [i for i in request.attachment_ids if i]
    if not ids:
        return []
    from rubik_core.chat_files import service as chat_files

    if not chat_files.enabled_for(user):
        raise HTTPException(
            status_code=403,
            detail="Attaching files is not turned on for you. Ask your admin.",
        )
    if len(set(ids)) > settings.CHAT_FILE_MAX_PER_MESSAGE:
        raise HTTPException(
            status_code=422,
            detail=f"Attach up to {settings.CHAT_FILE_MAX_PER_MESSAGE} files per message.",
        )
    try:
        return chat_files.owned(db, user.email, ids)
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail="One of the attached files couldn't be found. Remove it and attach it again.",
        ) from None


def _attachment_meta(uploads: list) -> dict | None:
    """What the user message stores, so the chips come back on reload."""
    if not uploads:
        return None
    return {
        "attachments": [
            {
                "id": u.id,
                "filename": u.filename,
                "kind": u.kind,
                "size_bytes": u.size_bytes,
            }
            for u in uploads
        ]
    }


# What admin-readable copies of an answer hold instead of an answer that drew on
# the person's private files (interaction_logs.llm_output here; the feedback
# snapshot is blanked in backend/routers/feedback.py on the same flag).
PRIVATE_ANSWER_PLACEHOLDER = (
    "[Not recorded: this answer drew on files private to the person who asked.]"
)


def _used_private_files(uploads: list, tool_calls) -> bool:
    """Did this turn read the person's private files (attached or searched)?"""
    return bool(uploads) or "search_my_files" in (tool_calls or [])


def _effective_query(request: ChatRequest, uploads: list) -> str:
    return request.query.strip() or (_FILES_ONLY_QUERY if uploads else request.query)


def _session_title(request: ChatRequest, uploads: list) -> str:
    text = request.query.strip() or (uploads[0].filename if uploads else "")
    return text[:40] + "..." if len(text) > 40 else text


async def _file_parts(
    db: Session, user: UserProfile, uploads: list, session_id: str
) -> list:
    """The message parts this turn adds: the attached files, then one line
    naming the person's other private files so the agent knows to search them."""
    from rubik_core.chat_files import service as chat_files

    enabled = chat_files.enabled_for(user)
    if not uploads and not enabled:
        return []
    parts: list = []
    if uploads:
        chat_files.bind_to_session(db, uploads, session_id)
        parts.extend(await run_in_threadpool(chat_files.turn_parts, uploads))
    if enabled:
        try:
            note = chat_files.library_note(
                db, user.email, exclude_ids=[u.id for u in uploads]
            )
        except Exception:  # noqa: BLE001 — a hint, never worth failing a turn
            logger.warning("could not list private files for the turn", exc_info=True)
            note = None
        if note is not None:
            parts.append(note)
    return parts


class Citation(BaseModel):
    source_path: str
    page: int | None
    chunk_index: int
    snippet: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    hubspot_data: str | None = None
    generated_sql: str | None = None
    debug: dict[str, Any] | None = None
    session_id: str
    request_id: str | None = None
    # Stable DB id of the persisted assistant message — lets the UI attach
    # WGLL feedback to this exact message. None on error/timeout (nothing saved).
    message_id: str | None = None
    # Set when the turn paused for the user's approval: the card the Chat page
    # draws, rendered by the server from the paused call's own arguments.
    confirmation: dict[str, Any] | None = None


@router.get("/sessions", response_model=list[ChatSessionResponse])
async def list_sessions(
    user: UserProfile = Depends(get_current_user), db: Session = Depends(get_db)
):
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.user_email == user.email)
        .order_by(desc(ChatSession.updated_at))
        .all()
    )
    return sessions


@router.get("/sessions/{session_id}", response_model=ChatSessionDetailResponse)
async def get_session(
    session_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_email == user.email)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_email == user.email)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    db.delete(session)
    db.commit()
    return {"message": "Session deleted"}


# ── Chat folders (UAT-03) ─────────────────────────────────────────
class FolderCreate(BaseModel):
    name: str


class FolderUpdate(BaseModel):
    name: str


class SessionFolderUpdate(BaseModel):
    # null moves the chat back to "Ungrouped".
    folder_id: str | None = None


def _clean_folder_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Folder name is required")
    return name[:100]


@router.get("/folders", response_model=list[ChatFolderResponse])
async def list_folders(
    user: UserProfile = Depends(get_current_user), db: Session = Depends(get_db)
):
    return (
        db.query(ChatFolder)
        .filter(ChatFolder.user_email == user.email)
        .order_by(ChatFolder.name)
        .all()
    )


@router.post("/folders", response_model=ChatFolderResponse)
async def create_folder(
    body: FolderCreate,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    folder = ChatFolder(
        id=str(uuid.uuid4()),
        user_email=user.email,
        name=_clean_folder_name(body.name),
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder


@router.patch("/folders/{folder_id}", response_model=ChatFolderResponse)
async def rename_folder(
    folder_id: str,
    body: FolderUpdate,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    folder = (
        db.query(ChatFolder)
        .filter(ChatFolder.id == folder_id, ChatFolder.user_email == user.email)
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    folder.name = _clean_folder_name(body.name)
    db.commit()
    db.refresh(folder)
    return folder


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    folder = (
        db.query(ChatFolder)
        .filter(ChatFolder.id == folder_id, ChatFolder.user_email == user.email)
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    # Ungroup the folder's chats instead of deleting them.
    db.query(ChatSession).filter(
        ChatSession.folder_id == folder_id, ChatSession.user_email == user.email
    ).update({ChatSession.folder_id: None}, synchronize_session=False)
    db.delete(folder)
    db.commit()
    return {"message": "Folder deleted"}


@router.patch("/sessions/{session_id}", response_model=ChatSessionResponse)
async def update_session_folder(
    session_id: str,
    body: SessionFolderUpdate,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_email == user.email)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if body.folder_id is not None:
        folder = (
            db.query(ChatFolder)
            .filter(
                ChatFolder.id == body.folder_id,
                ChatFolder.user_email == user.email,
            )
            .first()
        )
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")
    session.folder_id = body.folder_id
    db.commit()
    db.refresh(session)
    return session


@router.post("/", response_model=ChatResponse)
@attribute_to("chat")
async def chat(
    request: ChatRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    request_id = str(uuid.uuid4())[:8]
    request_start = time.time()
    backend_name = settings.ORCHESTRATOR_BACKEND.value
    logger.info(
        "[%s] chat request from %s — backend=%s",
        request_id,
        user.email,
        backend_name,
    )
    # V2 plumbing: accept and log mode + scope; behavior unchanged at V2_1.
    logger.info(
        "[%s] chat request mode=%s scope_kind=%s scope_id=%s",
        request_id,
        request.mode,
        request.scope.kind if request.scope else None,
        request.scope.id if request.scope else None,
    )

    # Before anything is saved: a file that isn't theirs refuses the message.
    uploads = _load_attachments(request, user, db)

    try:
        # Implement Department-based RBAC
        filters = {"status": "approved"}

        # Session Management — identical for both backends
        db_session = None
        history = []
        if request.session_id:
            db_session = (
                db.query(ChatSession)
                .filter(
                    ChatSession.id == request.session_id,
                    ChatSession.user_email == user.email,
                )
                .first()
            )

        if db_session:
            # Only load the most recent messages — not the entire session
            recent_msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == db_session.id)
                .order_by(ChatMessage.created_at.desc())
                .limit(MAX_HISTORY_MESSAGES)
                .all()
            )
            # Reverse so they're in chronological order
            recent_msgs.reverse()
            for m in recent_msgs:
                history.append((m.role, _history_text(m)))
        else:
            session_id = str(uuid.uuid4())
            title = _session_title(request, uploads)
            db_session = ChatSession(id=session_id, user_email=user.email, title=title)
            db.add(db_session)
            db.commit()
            db.refresh(db_session)
            history = [(m.role, m.content) for m in request.history]

        # Trim history by char budget
        history = _trim_history(history)
        logger.info(
            "[%s] history: %d messages, %d chars",
            request_id,
            len(history),
            sum(len(c) for _, c in history),
        )

        # Add the current user query to DB
        user_msg = ChatMessage(
            id=str(uuid.uuid4()),
            session_id=db_session.id,
            role="user",
            content=request.query,
            metadata_json=_attachment_meta(uploads),
        )
        db.add(user_msg)
        db.commit()

        # Drop any stale HITL confirmation pointer before this turn so an
        # unrelated message can't resume a long-parked calendar/draft action.
        _expire_stale_hitl_pointer(db_session, request.query)

        # Build the correct orchestrator via factory
        orchestrator = _build_orchestrator(
            user=user,
            db=db,
            timezone=request.timezone,
            source_session_id=db_session.id,
        )

        # Execute orchestration with overall timeout.
        # V2 plumbing: pass `mode` and `scope` through; the orchestrator
        # logs them but does not act on them yet (V2_1 = plumbing only).
        scope_payload = request.scope.model_dump() if request.scope else None
        extra_parts = await _file_parts(db, user, uploads, db_session.id)
        orch_result = await asyncio.wait_for(
            orchestrator.run(
                request_id=request_id,
                query=_effective_query(request, uploads),
                history=history,
                top_k=request.top_k,
                filters=filters,
                chat_session_id=db_session.id,
                db_session=db_session,
                mode=request.mode,
                scope=scope_payload,
                extra_parts=extra_parts,
            ),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        total_duration_ms = int((time.time() - request_start) * 1000)

        # Prepare response
        debug_info = orch_result.trace.model_dump(mode="json")
        # `trace.total_duration_ms` is the orchestrator's own wall-clock (LLM +
        # tools). Preserve it as exec_ms *before* we overwrite total_duration_ms
        # with the full-endpoint time (which also covers session/history/DB work).
        debug_info["exec_ms"] = debug_info.get("total_duration_ms", 0)
        debug_info["total_duration_ms"] = total_duration_ms
        debug_info["backend"] = backend_name
        # Flatten the fields the debug UI (DebugStatsPanel) reads at the top level.
        # intent is otherwise nested under trace.route, and the token counts live
        # on the OrchestrationResult, not on the trace.
        debug_info["intent"] = debug_info.get("route", {}).get("intent")
        debug_info["prompt_tokens"] = orch_result.prompt_tokens
        debug_info["completion_tokens"] = orch_result.completion_tokens
        debug_info["total_tokens"] = orch_result.total_tokens
        logger.info(
            "[%s] completed in %dms via %s", request_id, total_duration_ms, backend_name
        )

        # Add the assistant response to DB
        assistant_meta = {
            "citations": orch_result.citations,
            "hubspot_data": orch_result.hubspot_data,
            "generated_sql": orch_result.generated_sql,
            "debug": debug_info,
        }
        if orch_result.confirmation:
            # Saved so a reloaded conversation still shows what was approved.
            assistant_meta["confirmation"] = orch_result.confirmation
        private_turn = _used_private_files(
            uploads, (orch_result.trace.route.metadata or {}).get("tool_calls")
        )
        if private_turn:
            # Read by feedback.py: the rating's text snapshot is not kept.
            assistant_meta["private_files"] = True

        assistant_msg = ChatMessage(
            id=str(uuid.uuid4()),
            session_id=db_session.id,
            role="assistant",
            content=orch_result.answer,
            metadata_json=assistant_meta,
        )
        db.add(assistant_msg)

        # Audit / Interaction Logging
        interaction_log = InteractionLog(
            id=str(uuid.uuid4()),
            session_id=db_session.id,
            user_email=user.email,
            user_input=request.query,
            system_prompt=f"Intent: {orch_result.trace.route.intent} | Backend: {backend_name} | ReqID: {request_id}",
            # Admins can read interaction_logs: an answer built from someone's
            # private files is not copied there.
            llm_output=PRIVATE_ANSWER_PLACEHOLDER
            if private_turn
            else orch_result.answer,
            prompt_tokens=orch_result.prompt_tokens,
            completion_tokens=orch_result.completion_tokens,
            total_tokens=orch_result.total_tokens,
            duration_ms=total_duration_ms,
            successful=orch_result.trace.steps[-1].status == "success"
            if orch_result.trace.steps
            else True,
            mode=request.mode,
            scope=scope_payload,
        )
        db.add(interaction_log)

        # AI-Cost ledger: the chat coordinator runs through the ADK Runner, not
        # the shared genai client, so the cost wrapper can't see its tokens —
        # record them here (feature="chat") from the usage the orchestrator
        # already extracted. Sub-tool calls (web search, HubSpot, Slack) go
        # through the shared client and are captured separately under "chat"
        # when the request sets the feature context.
        try:
            _record_chat_usage(
                orchestrator,
                orch_result.usage_by_agent,
                {
                    "prompt": orch_result.prompt_tokens,
                    "completion": orch_result.completion_tokens,
                    "thinking": orch_result.thinking_tokens,
                    "cached": orch_result.cached_tokens,
                    "total": orch_result.total_tokens,
                },
                user_email=user.email,
                request_id=request_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[%s] chat cost record failed", request_id, exc_info=True)

        db_session.updated_at = datetime.utcnow()
        db.commit()

        return ChatResponse(
            answer=orch_result.answer,
            citations=[
                Citation(
                    source_path=c["source_path"],
                    page=c.get("page"),
                    chunk_index=c.get("chunk_index", 0),
                    snippet=c.get("snippet", ""),
                )
                for c in orch_result.citations
            ],
            hubspot_data=orch_result.hubspot_data,
            generated_sql=orch_result.generated_sql,
            debug=debug_info,
            session_id=db_session.id,
            request_id=request_id,
            message_id=assistant_msg.id,
            confirmation=orch_result.confirmation,
        )

    except asyncio.TimeoutError:
        total_duration_ms = int((time.time() - request_start) * 1000)
        logger.error("[%s] TIMEOUT after %dms", request_id, total_duration_ms)

        session_id_to_return = request.session_id
        try:
            if "db_session" in locals() and db_session and db_session.id:
                session_id_to_return = db_session.id
        except NameError:
            pass

        try:
            error_log = InteractionLog(
                id=str(uuid.uuid4()),
                session_id=session_id_to_return,
                user_email=user.email,
                user_input=request.query,
                duration_ms=total_duration_ms,
                successful=False,
                error_message=f"Timeout after {REQUEST_TIMEOUT_SECONDS}s (request_id={request_id})",
            )
            db.add(error_log)
            db.commit()
        except Exception:
            pass

        return ChatResponse(
            answer=f"The request timed out after {REQUEST_TIMEOUT_SECONDS} seconds. Please try again.",
            citations=[],
            session_id=session_id_to_return or str(uuid.uuid4()),
            request_id=request_id,
        )

    except (IncompatibleModelRoute, ProviderRequestError):
        return ChatResponse(
            answer=_SELECTED_MODEL_ERROR,
            citations=[],
            session_id=(
                db_session.id
                if "db_session" in locals() and db_session is not None
                else request.session_id or str(uuid.uuid4())
            ),
            request_id=request_id,
        )
    except HTTPException:
        raise
    except requests.exceptions.SSLError as e:
        logger.warning(
            "[%s] Authentication socket error (%s)", request_id, type(e).__name__
        )

        try:
            error_log = InteractionLog(
                id=str(uuid.uuid4()),
                session_id=request.session_id,
                user_email=user.email,
                user_input=request.query,
                successful=False,
                error_message="authentication_socket_error",
            )
            db.add(error_log)
            db.commit()
        except Exception:
            pass

        return ChatResponse(
            answer="I experienced an internal network issue. Please try again.",
            citations=[],
            session_id=request.session_id or str(uuid.uuid4()),
            request_id=request_id,
        )
    except Exception as e:
        logger.warning("[%s] Chat endpoint error (%s)", request_id, type(e).__name__)

        # Classify error for debug
        error_category = _classify_error(e)
        user_message = _user_safe_error_message(error_category, e)

        try:
            error_log = InteractionLog(
                id=str(uuid.uuid4()),
                session_id=request.session_id,
                user_email=user.email,
                user_input=request.query,
                successful=False,
                error_message=error_category,
            )
            db.add(error_log)
            db.commit()
        except Exception:
            pass

        session_id_to_return = request.session_id
        try:
            if "db_session" in locals() and db_session and db_session.id:
                session_id_to_return = db_session.id
        except NameError:
            pass

        return ChatResponse(
            answer=user_message,
            citations=[],
            session_id=session_id_to_return or str(uuid.uuid4()),
            request_id=request_id,
        )


def _sse(payload: dict[str, Any]) -> str:
    """Serialize one Server-Sent-Events frame."""
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/stream")
@attribute_to("chat")
async def chat_stream(
    request: ChatRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Streaming (SSE) variant of the chat endpoint — additive and flag-gated.

    Emits `data: {json}` frames: many ``{"type":"token","text":...}`` chunks as
    the model generates, then a terminal ``{"type":"done", ...}`` carrying the
    canonical answer + citations + session/message ids (or ``{"type":"error"}``).
    The non-streaming ``POST /chat/`` remains the default; the frontend opts in
    per-browser, so this can ship dormant. Toggle off via CHAT_STREAMING_ENABLED.
    """
    if not settings.CHAT_STREAMING_ENABLED:
        raise HTTPException(status_code=404, detail="Streaming is disabled")

    request_id = str(uuid.uuid4())[:8]
    request_start = time.time()
    # Before anything is saved: a file that isn't theirs refuses the message.
    uploads = _load_attachments(request, user, db)

    # ── Session + history setup (mirrors chat()) ──
    db_session = None
    history: list[tuple] = []
    if request.session_id:
        db_session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == request.session_id,
                ChatSession.user_email == user.email,
            )
            .first()
        )
    if db_session:
        recent_msgs = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == db_session.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(MAX_HISTORY_MESSAGES)
            .all()
        )
        recent_msgs.reverse()
        history = [(m.role, _history_text(m)) for m in recent_msgs]
    else:
        new_id = str(uuid.uuid4())
        title = _session_title(request, uploads)
        db_session = ChatSession(id=new_id, user_email=user.email, title=title)
        db.add(db_session)
        db.commit()
        db.refresh(db_session)
        history = [(m.role, m.content) for m in request.history]

    history = _trim_history(history)

    # Persist the user turn up-front (same as chat()).
    db.add(
        ChatMessage(
            id=str(uuid.uuid4()),
            session_id=db_session.id,
            role="user",
            content=request.query,
            metadata_json=_attachment_meta(uploads),
        )
    )
    db.commit()

    # Drop any stale HITL confirmation pointer before this turn (parity with the
    # non-streaming path) so an unrelated message can't resume a parked action.
    _expire_stale_hitl_pointer(db_session, request.query)

    session_db_id = db_session.id
    # Build the orchestrator now, while the request-scoped `db` is still open —
    # the generator below only touches a fresh SessionLocal, never `db`.
    try:
        orchestrator = _build_orchestrator(
            user=user,
            db=db,
            timezone=request.timezone,
            source_session_id=db_session.id,
        )
    except (IncompatibleModelRoute, ProviderRequestError):
        raise HTTPException(status_code=502, detail=_SELECTED_MODEL_ERROR) from None
    scope_payload = request.scope.model_dump() if request.scope else None
    # Read now, while the request-scoped `db` is open (see above).
    extra_parts = await _file_parts(db, user, uploads, db_session.id)

    async def event_stream():
        done = None
        try:
            async for ev in orchestrator.run_streaming(
                request_id=request_id,
                query=_effective_query(request, uploads),
                history=history,
                top_k=request.top_k,
                filters={"status": "approved"},
                chat_session_id=session_db_id,
                db_session=db_session,
                mode=request.mode,
                scope=scope_payload,
                extra_parts=extra_parts,
            ):
                if ev["type"] == "token":
                    yield _sse(ev)
                elif ev["type"] == "error":
                    if ev.get("total_tokens") or ev.get("usage_by_agent"):
                        try:
                            _record_chat_usage(
                                orchestrator,
                                ev.get("usage_by_agent"),
                                {
                                    "prompt": ev.get("prompt_tokens", 0),
                                    "completion": ev.get("completion_tokens", 0),
                                    "thinking": ev.get("thinking_tokens", 0),
                                    "cached": ev.get("cached_tokens", 0),
                                    "total": ev.get("total_tokens", 0),
                                },
                                user_email=user.email,
                                request_id=request_id,
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "[%s] chat cost record failed",
                                request_id,
                                exc_info=True,
                            )
                    yield _sse(
                        {
                            "type": "error",
                            "message": ev.get("message", "An error occurred."),
                            "request_id": request_id,
                        }
                    )
                    return
                elif ev["type"] == "done":
                    done = ev
        except Exception as exc:
            logger.warning(
                "[%s] chat stream failed (%s)", request_id, type(exc).__name__
            )
            yield _sse(
                {
                    "type": "error",
                    "message": "An internal error occurred. Please try again.",
                    "request_id": request_id,
                }
            )
            return

        if done is None:
            yield _sse(
                {
                    "type": "error",
                    "message": "No response was generated. Please try again.",
                    "request_id": request_id,
                }
            )
            return

        total_ms = int((time.time() - request_start) * 1000)
        message_id = str(uuid.uuid4())
        private_turn = _used_private_files(uploads, done.get("tool_calls"))
        debug_info = {
            "backend": settings.ORCHESTRATOR_BACKEND.value,
            "total_duration_ms": total_ms,
            "route": {"metadata": {"tool_calls": done.get("tool_calls", [])}},
            "streamed": True,
        }
        # Persist the assistant turn. FastAPI keeps the request-scoped session
        # open until a streaming response finishes draining, so `db` is valid
        # here — and using it (not a raw SessionLocal) honors the get_db override
        # in tests, so streamed turns never write to the real DB under pytest.
        try:
            db.add(
                ChatMessage(
                    id=message_id,
                    session_id=session_db_id,
                    role="assistant",
                    content=done.get("answer", ""),
                    metadata_json={
                        "citations": done.get("citations", []),
                        "hubspot_data": done.get("hubspot_data"),
                        "generated_sql": done.get("generated_sql"),
                        "debug": debug_info,
                        **({"private_files": True} if private_turn else {}),
                        **(
                            {"confirmation": done["confirmation"]}
                            if done.get("confirmation")
                            else {}
                        ),
                    },
                )
            )
            db.add(
                InteractionLog(
                    id=str(uuid.uuid4()),
                    session_id=session_db_id,
                    user_email=user.email,
                    user_input=request.query,
                    llm_output=PRIVATE_ANSWER_PLACEHOLDER
                    if private_turn
                    else done.get("answer", ""),
                    prompt_tokens=done.get("prompt_tokens", 0),
                    completion_tokens=done.get("completion_tokens", 0),
                    total_tokens=done.get("total_tokens", 0),
                    duration_ms=total_ms,
                    successful=True,
                    mode=request.mode,
                    scope=scope_payload,
                )
            )
            sess = db.query(ChatSession).filter(ChatSession.id == session_db_id).first()
            if sess:
                sess.updated_at = datetime.utcnow()
            db.commit()
            # AI-Cost ledger: record the streamed coordinator turn (ADK path,
            # invisible to the shared-client cost wrapper). See the non-stream
            # site above for rationale.
            try:
                _record_chat_usage(
                    orchestrator,
                    done.get("usage_by_agent"),
                    {
                        "prompt": done.get("prompt_tokens", 0),
                        "completion": done.get("completion_tokens", 0),
                        "thinking": done.get("thinking_tokens", 0),
                        "cached": done.get("cached_tokens", 0),
                        "total": done.get("total_tokens", 0),
                    },
                    user_email=user.email,
                    request_id=request_id,
                )
            except Exception:  # noqa: BLE001
                logger.debug("[%s] chat cost record failed", request_id, exc_info=True)
        except Exception as exc:
            logger.warning(
                "[%s] failed to persist streamed turn (%s)",
                request_id,
                type(exc).__name__,
            )

        yield _sse(
            {
                "type": "done",
                "answer": done.get("answer", ""),
                "citations": done.get("citations", []),
                "hubspot_data": done.get("hubspot_data"),
                "generated_sql": done.get("generated_sql"),
                "debug": debug_info,
                "session_id": session_db_id,
                "message_id": message_id,
                "request_id": request_id,
                "confirmation": done.get("confirmation"),
            }
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering (nginx / Cloud Run) so tokens flush live.
            "X-Accel-Buffering": "no",
        },
    )


# ── Error Classification (WS7) ────────────────────────────────────


def _classify_error(e: Exception) -> str:
    """Classify an exception into a debug-friendly category."""
    error_str = str(e).lower()
    error_type = type(e).__name__

    if "policy" in error_str or "denied" in error_str:
        return "policy_denied"
    if "timeout" in error_str or "deadline" in error_str:
        return "timeout"
    if "429" in error_str or "resource exhausted" in error_str or "quota" in error_str:
        return "downstream_api_error"
    if "validation" in error_str or "pydantic" in error_type.lower():
        return "agent_validation_error"
    if "tool" in error_str and ("error" in error_str or "fail" in error_str):
        return "tool_error"
    if "google" in error_str or "grpc" in error_str or "ssl" in error_str:
        return "downstream_api_error"
    return "agent_runtime_error"


def _user_safe_error_message(category: str, e: Exception) -> str:
    """Return a user-friendly error message based on the category."""
    messages = {
        "policy_denied": "This action is not permitted by the current policy. Please contact your administrator if you believe this is an error.",
        "timeout": "The request took too long to process. Please try a simpler question or try again later.",
        "downstream_api_error": "I'm experiencing connectivity issues with an external service. Please try again in a moment.",
        "agent_validation_error": "I had trouble processing that request. Could you try rephrasing your question?",
        "tool_error": "One of my tools encountered an issue. I'll try to help in a different way — could you rephrase your question?",
        "agent_runtime_error": "I encountered an unexpected issue while processing your request. Please try again.",
    }
    return messages.get(category, "An internal error occurred. Please try again.")
