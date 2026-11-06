from __future__ import annotations

import json as _json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from backend.services.gmail_service import EmailSummary, GmailService
from backend.services.user_context import build_user_context_block
from rubik_core.auth.registry import UserProfile
from rubik_core.cost.usage import attribute_to
from rubik_core.database.db import get_db
from rubik_core.database.models import AuditLog
from rubik_core.tools.gmail import GmailClient

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/email",
    tags=["Email"],
)


class DraftRequest(BaseModel):
    to: list[str]
    subject: str
    body_markdown: str
    cc: list[str] = []
    thread_id: str | None = None
    account_id: str | None = None


def _gmail_client(user_email: str, account_id: str | None) -> GmailClient:
    return (
        GmailClient(user_email=user_email, account_id=account_id)
        if account_id is not None
        else GmailClient(user_email=user_email)
    )


def _log_audit_action(db: Session, actor_email: str, action: str, details: dict):
    logger.info(f"AuditLog: {actor_email} -> {action}")
    try:
        log_entry = AuditLog(
            actor_email=actor_email, action=action, status="success", details=details
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to write AuditLog for {actor_email}: {e}")
        db.rollback()


@router.get("/search")
def search_threads(
    query: str = Query("", description="Gmail search query syntax"),
    max_results: int = Query(5, description="Maximum number of threads to return"),
    account_id: str | None = Query(
        None, description="Explicit connected Gmail account"
    ),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        client = _gmail_client(user.email, account_id)
        if not client.service:
            raise HTTPException(
                status_code=500, detail="Failed to initialize Gmail client."
            )

        threads = client.search_threads(query=query, max_results=max_results)
        _log_audit_action(
            db,
            user.email,
            "SEARCH_EMAIL_THREADS",
            {"account_id": account_id}
            if account_id is not None
            else {"query": query, "max_results": max_results},
        )
        return [{**thread, "account_id": account_id} for thread in threads]
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(status_code=403, detail="Gmail account is unavailable")
    except Exception as e:
        logger.warning(
            "Error searching Gmail threads for %s: %s", user.email, type(e).__name__
        )
        raise HTTPException(status_code=502, detail="Gmail request failed")


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: str,
    account_id: str | None = Query(
        None, description="Explicit connected Gmail account"
    ),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        client = _gmail_client(user.email, account_id)
        if not client.service:
            raise HTTPException(
                status_code=500, detail="Failed to initialize Gmail client."
            )

        thread = client.get_thread(thread_id=thread_id)
        _log_audit_action(
            db,
            user.email,
            "VIEW_EMAIL_THREAD",
            {"account_id": account_id}
            if account_id is not None
            else {"thread_id": thread_id},
        )
        return {**thread, "account_id": account_id}
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(status_code=403, detail="Gmail account is unavailable")
    except Exception as e:
        logger.warning(
            "Error fetching Gmail thread for %s: %s", user.email, type(e).__name__
        )
        raise HTTPException(status_code=502, detail="Gmail request failed")


@router.get("/inbox")
def email_inbox(
    account_id: str | None = Query(
        None, description="Explicit connected Gmail account"
    ),
    max_results: int = Query(25, ge=1, le=50),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Focused inbox for one selected mailbox; never merges personal mail."""
    try:
        service = GmailService(user_email=user.email, account_id=account_id)
        items = service.get_unread(max_results=max_results)
        _log_audit_action(
            db, user.email, "VIEW_EMAIL_INBOX", {"account_id": account_id}
        )
        return {"account_id": account_id, "items": items}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Gmail account is unavailable")
    except Exception as exc:
        logger.warning("Gmail inbox failed for %s: %s", user.email, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Gmail request failed")


class EmailSummaryResponse(BaseModel):
    since: str | None
    label: str | None
    count: int
    items: list[EmailSummary]


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    try:
        # Accept either "YYYY-MM-DD" or full ISO 8601 (with optional Z).
        return datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid `since` value: {since!r}. Use ISO 8601."
        )


async def _summarize_for_user(
    user: UserProfile,
    since: datetime | None,
    label: str | None,
    max_results: int,
    account_id: str | None = None,
) -> list[EmailSummary]:
    service, messages = await run_in_threadpool(
        _fetch_summary_messages, user.email, since, label, max_results, account_id
    )
    first_name = (user.name or user.email.split("@")[0]).split(" ")[0]
    return await service.summarize_messages(messages, user_first_name=first_name)


def _fetch_summary_messages(
    user_email: str,
    since: datetime | None,
    label: str | None,
    max_results: int,
    account_id: str | None,
) -> tuple[GmailService, list]:
    service = GmailService(user_email=user_email, account_id=account_id)
    if label:
        # Compose a label-scoped query rather than relying on `is:unread`.
        query = f"label:{label}"
        if since is not None:
            from backend.services.gmail_service import _to_gmail_after

            query = f"{query} after:{_to_gmail_after(since)}"
        messages = service.search(query=query, max_results=max_results)
    else:
        messages = service.get_unread(since=since, max_results=max_results)

    return service, messages


@router.get("/summary", response_model=EmailSummaryResponse)
async def email_summary(
    since: str | None = Query(
        None, description="ISO 8601 datetime; only summarize messages after this."
    ),
    label: str | None = Query(
        None, description="Optional Gmail label to scope (e.g. INBOX). Default: unread."
    ),
    max_results: int = Query(15, ge=1, le=50),
    account_id: str | None = Query(
        None, description="Explicit connected Gmail account"
    ),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Structured summary of the user's unread (or label-scoped) emails."""
    since_dt = _parse_since(since)
    try:
        items = await _summarize_for_user(
            user, since_dt, label, max_results, account_id
        )
        _log_audit_action(
            db,
            user.email,
            "EMAIL_SUMMARY",
            {"account_id": account_id}
            if account_id is not None
            else {
                "since": since_dt.isoformat() if since_dt else None,
                "label": label,
                "max_results": max_results,
            },
        )
        return EmailSummaryResponse(
            since=since_dt.isoformat() if since_dt else None,
            label=label,
            count=len(items),
            items=items,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Gmail account is unavailable")
    except Exception as exc:
        logger.warning(
            "Email summary failed for %s: %s", user.email, type(exc).__name__
        )
        raise HTTPException(status_code=502, detail="Gmail request failed")


@router.get("/digest", response_model=EmailSummaryResponse)
async def email_digest(
    max_results: int = Query(20, ge=1, le=50),
    account_id: str | None = Query(
        None, description="Explicit connected Gmail account"
    ),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """`While you were gone` digest — auto-scoped to since the user's last login.

    Falls back to the last 24 hours if no `last_login` is recorded yet.
    """
    since_dt: datetime | None = None
    if user.last_login:
        try:
            since_dt = datetime.fromisoformat(user.last_login.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            since_dt = None
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)

    try:
        items = await _summarize_for_user(
            user, since_dt, label=None, max_results=max_results, account_id=account_id
        )
        _log_audit_action(
            db,
            user.email,
            "EMAIL_DIGEST",
            {"account_id": account_id}
            if account_id is not None
            else {"since": since_dt.isoformat()},
        )
        return EmailSummaryResponse(
            since=since_dt.isoformat(),
            label=None,
            count=len(items),
            items=items,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Gmail account is unavailable")
    except Exception as exc:
        logger.warning("Email digest failed for %s: %s", user.email, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Gmail request failed")


@router.post("/drafts")
def create_draft(
    request: DraftRequest,
    http_request: Request,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if request.account_id is not None:
        http_request.state.private_gmail_activity = True
    try:
        client = _gmail_client(user.email, request.account_id)
        if not client.service:
            raise HTTPException(
                status_code=500, detail="Failed to initialize Gmail client."
            )

        result = client.create_draft(
            to=request.to,
            subject=request.subject,
            body_markdown=request.body_markdown,
            cc=request.cc,
            thread_id=request.thread_id,
        )
        _log_audit_action(
            db,
            user.email,
            "CREATE_EMAIL_DRAFT",
            {"account_id": request.account_id}
            if request.account_id is not None
            else {
                "to": request.to,
                "cc": request.cc,
                "subject": request.subject,
                "thread_id": request.thread_id,
            },
        )

        # UC-27 — best-effort auto-resolve the matching UC-25 FOLLOW_UP_DEBT
        # for this thread so the priorities pipeline stops re-firing
        # follow-up reminders once the user has actually composed a reply.
        # Drafts are a strong intent signal; if the user later abandons the
        # draft, the Gmail extractor's direction=they_sent_last detection
        # will resurface the debt on the next run.
        if request.account_id is None:
            _resolve_followup_debt(user.email, request.thread_id)

        return {**result, "account_id": request.account_id}
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(status_code=403, detail="Gmail account is unavailable")
    except Exception as e:
        logger.warning(
            "Error creating Gmail draft for %s: %s", user.email, type(e).__name__
        )
        raise HTTPException(status_code=502, detail="Gmail request failed")


def _resolve_followup_debt(user_email: str, thread_id: str | None) -> None:
    """UC-27 hook. Best-effort: never block draft creation on a memory write."""
    if not thread_id:
        return
    try:
        from rubik_core.priorities import memory as priority_memory
        from rubik_core.priorities.models import EntityKind, PriorityKind

        # Drafting the outbound touch resolves BOTH the legacy follow_up_debt
        # and the forward-looking pre_touchpoint_followup kind — the dreamer
        # may surface either for the same thread.
        for kind in (
            PriorityKind.FOLLOW_UP_DEBT,
            PriorityKind.PRE_TOUCHPOINT_FOLLOWUP,
        ):
            priority_memory.resolve(
                user_email=user_email,
                entity_kind=EntityKind.THREAD,
                entity_id=str(thread_id),
                priority_kind=kind,
                evidence={"kind": "gmail_draft_created", "thread_id": str(thread_id)},
                actor="system",
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "UC-27: failed to resolve follow-up debt for %s / thread %s",
            user_email,
            thread_id,
            exc_info=True,
        )


# ─── UC-7: Email Drafting Agent ─────────────────────────────────────────────

EmailType = Literal["cold_prospect", "follow_up", "re_engagement", "market_trigger"]

_EMAIL_TYPE_PROMPTS: dict[str, str] = {
    "cold_prospect": (
        "Draft a cold prospecting email. Lead with a specific insight about their "
        "business or industry, identify a likely gap, and propose a 15-minute call."
    ),
    "follow_up": (
        "Draft a follow-up email referencing the most recent meaningful interaction "
        "(meeting, prior email, deal stage). Move the conversation forward with a "
        "concrete next step. NEVER use the phrase 'just following up'."
    ),
    "re_engagement": (
        "Draft a re-engagement email for a lead that's gone cold. Lead with a "
        "fresh insight or recent industry development, not 'checking in'."
    ),
    "market_trigger": (
        "Draft an email triggered by a specific market or regulatory event. "
        "Lead with the event, articulate the operational gap it creates for the "
        "recipient, and bridge to the company's relevant offering."
    ),
}


class EmailDraftRequest(BaseModel):
    recipient_email: str
    recipient_name: str | None = None
    recipient_title: str | None = None
    company: str | None = None
    email_type: EmailType = "follow_up"
    trigger_context: str | None = None
    additional_context: str | None = None


class EmailDraftResponse(BaseModel):
    recipient: str
    email_type: str
    subject: str | None
    body: str | None
    gmail_draft_url: str | None
    raw_response: str


def _build_draft_user_message(req: EmailDraftRequest) -> str:
    """Compose the user message handed to the sales agent.

    The sales agent's system instruction is unchanged; this message just gives
    it the structured task + recipient details and instructs it to retrieve
    real context from the available tools before composing.
    """
    type_instruction = _EMAIL_TYPE_PROMPTS.get(
        req.email_type, _EMAIL_TYPE_PROMPTS["follow_up"]
    )
    recipient_label = req.recipient_name or req.recipient_email
    parts = [
        f"Task: {type_instruction}",
        "",
        f"Recipient: {recipient_label} <{req.recipient_email}>",
    ]
    if req.recipient_title:
        parts.append(f"Title: {req.recipient_title}")
    if req.company:
        parts.append(f"Company: {req.company}")
    if req.email_type == "market_trigger" and req.trigger_context:
        parts.append(f"Market trigger: {req.trigger_context}")
    elif req.trigger_context:
        parts.append(f"Trigger context: {req.trigger_context}")
    if req.additional_context:
        parts.append(f"Additional context from the user: {req.additional_context}")

    parts.extend(
        [
            "",
            "Before drafting, retrieve real context with the tools available to you:",
            "- search_hubspot_data: this recipient's company / deal / activity history.",
            "- search_past_meetings: any prior meeting/call transcript with this recipient "
            "or company. If this is a follow-up to a call, you MUST capture EVERY action "
            "item, to-do, next step, and commitment from that transcript (both yours and "
            "theirs) and reflect them in the email — do not drop or generalize any.",
            "- search_knowledge_base and search_products: the company's relevant offerings to ground the pitch.",
            "",
            "Then compose the email per your output format and save it as a Gmail draft.",
        ]
    )
    return "\n".join(parts)


_SUBJECT_RE = re.compile(r"(?im)^\s*Subject:\s*(.+?)\s*$")
_DRAFT_URL_RE = re.compile(r"https?://mail\.google\.com/[^\s)\]\"']+")


def _parse_draft_response(raw_text: str) -> tuple[str | None, str | None, str | None]:
    """Extract (subject, body, gmail_draft_url) from the agent's final text.

    The agent's system instruction defines an explicit output format. Parsing
    is permissive — if the LLM deviates, we return raw_text in the response
    and let the caller fall back.
    """
    subject_match = _SUBJECT_RE.search(raw_text)
    subject = subject_match.group(1).strip() if subject_match else None

    body: str | None = None
    if subject_match:
        body_start = subject_match.end()
        rest = raw_text[body_start:].lstrip("\n")
        # Trim at the Rationale section or the draft-saved confirmation.
        for stop in (
            "\nRationale",
            "\n## Rationale",
            "\n✅ Draft saved",
            "\nDraft saved",
        ):
            idx = rest.find(stop)
            if idx >= 0:
                rest = rest[:idx]
                break
        body = rest.strip() or None

    url_match = _DRAFT_URL_RE.search(raw_text)
    draft_url = url_match.group(0) if url_match else None
    return subject, body, draft_url


@router.post("/draft", response_model=EmailDraftResponse)
@attribute_to("sales_email_agent")
async def draft_sales_email(
    request: EmailDraftRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """UC-7: draft a sales email grounded in HubSpot, transcripts, and product context.

    Invokes the existing sales_email_agent (system prompt unchanged) with the
    UC-7 retrieval tool bundle. The agent picks which tools to call, drafts
    the email, and saves it as a Gmail draft. No email is ever sent.
    """
    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.genai import types

    from rubik_core.adk_agent.sales_email_agent import build_sales_email_agent
    from rubik_core.adk_agent.tools import AgentTools
    from rubik_core.ai_providers.adk_model import resolve_adk_route
    from rubik_core.ai_providers.registry import IncompatibleModelRoute
    from rubik_core.config import settings
    from rubik_core.cost.usage import extract_usage, record_usage

    _log_audit_action(
        db,
        user.email,
        "DRAFT_SALES_EMAIL",
        {
            "recipient": request.recipient_email,
            "email_type": request.email_type,
            "has_trigger": bool(request.trigger_context),
        },
    )

    tools_instance = AgentTools(user_email=user.email)
    retrieval_tools = [
        tools_instance.search_hubspot_data,
        tools_instance.search_past_meetings,
        tools_instance.search_knowledge_base,
        tools_instance.search_products,
    ]
    try:
        route = await run_in_threadpool(
            resolve_adk_route,
            "sales_email_agent",
            default_model=settings.VERTEX_LLM_MODEL_FLASH,
        )
        agent = build_sales_email_agent(
            tools_instance.gmail_create_draft,
            retrieval_tools=retrieval_tools,
            model_route=route,
        )
    except IncompatibleModelRoute:
        raise HTTPException(
            status_code=502,
            detail="Sales email model is unavailable. Check the workspace AI route.",
        ) from None
    except Exception:
        logger.warning("Sales email model could not be prepared")
        raise HTTPException(
            status_code=502,
            detail="Sales email model is unavailable. Check the workspace AI route.",
        ) from None

    session_service = InMemorySessionService()
    app_name = "email_drafting"
    user_id = user.email
    session_id = f"draft-{datetime.now(timezone.utc).timestamp()}"
    await session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )

    runner = Runner(
        app_name=app_name,
        agent=agent,
        session_service=session_service,
    )

    user_message = _build_draft_user_message(request)
    personalization = build_user_context_block(user, db)
    if personalization:
        user_message = f"{personalization}\n\n{user_message}"
    user_content = types.Content(
        role="user", parts=[types.Part.from_text(text=user_message)]
    )

    raw_chunks: list[str] = []
    usage = {
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "thinking": 0,
        "cached": 0,
    }
    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            if (
                not getattr(event, "partial", False)
                and getattr(event, "usage_metadata", None) is not None
            ):
                one = extract_usage(event)
                for key in usage:
                    usage[key] += one[key]
            if event.is_final_response() and event.content:
                raw_chunks.append(
                    "".join(p.text for p in event.content.parts if p.text)
                )
    except Exception:
        logger.warning("Sales email agent request failed")
        raise HTTPException(
            status_code=502,
            detail="Sales email model is unavailable. Check the workspace AI route.",
        ) from None
    finally:
        try:
            if any(usage.values()):
                record_usage(
                    operation="generate",
                    model=route.model,
                    prompt_tokens=usage["prompt"],
                    completion_tokens=usage["completion"],
                    total_tokens=usage["total"],
                    thinking_tokens=usage["thinking"],
                    cached_tokens=usage["cached"],
                    feature="sales_email_agent",
                    user_email=user.email,
                    metadata={
                        "provider": route.provider,
                        "billing_owner": route.billing_owner,
                    },
                )
        except Exception:
            logger.debug("Sales email usage recording failed")

    raw_response = "\n".join(c for c in raw_chunks if c).strip()
    if not raw_response:
        raise HTTPException(
            status_code=502,
            detail="Sales email agent returned no response.",
        )

    subject, body, draft_url = _parse_draft_response(raw_response)
    return EmailDraftResponse(
        recipient=request.recipient_email,
        email_type=request.email_type,
        subject=subject,
        body=body,
        gmail_draft_url=draft_url,
        raw_response=raw_response,
    )


# ─── UC-39: drafts/from-transcript endpoint ───────────────────────────────────
#
# Powers the "Generate follow-up email" smart action on AI Priorities cards
# whose entity_kind=transcript. Pulls the transcript text, asks the LLM to
# draft a follow-up to the participants the user owes a reply, creates a
# Gmail draft, and resolves the matching UC-25 COMMITMENT_DEBT so the
# priority doesn't refire tomorrow.


class TranscriptDraftRequest(BaseModel):
    transcript_id: str  # Drive file id of the Meet Recording transcript
    subject_hint: str | None = None
    focus_on: str | None = None


class TranscriptDraftResponse(BaseModel):
    draft_id: str | None = None
    gmail_draft_url: str | None = None
    to: list[str] = []
    subject: str = ""
    body_markdown: str = ""
    partial_errors: list[str] = []


def _external_transcript_emails(
    transcript_text: str, user_email: str, internal_domains: frozenset[str]
) -> list[str]:
    """Candidate draft recipients outside every verified workspace domain."""
    user_email_lower = user_email.lower()
    external_emails: list[str] = []
    for match in re.findall(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", transcript_text
    ):
        email = match.lower()
        if email == user_email_lower or email.rsplit("@", 1)[-1] in internal_domains:
            continue
        if email not in external_emails:
            external_emails.append(email)
    return external_emails[:5]


@router.post("/drafts/from-transcript", response_model=TranscriptDraftResponse)
@attribute_to("email")
async def create_draft_from_transcript(
    request: TranscriptDraftRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """LLM-draft a follow-up email from a Meet transcript + create a Gmail draft."""
    from rubik_core.config import settings
    from rubik_core.models import get_genai_client
    from rubik_core.tools.drive import GoogleDriveClient

    _log_audit_action(
        db,
        user.email,
        "CREATE_DRAFT_FROM_TRANSCRIPT",
        {
            "transcript_id": request.transcript_id,
            "focus_on": request.focus_on,
        },
    )

    errors: list[str] = []

    drive = GoogleDriveClient(user_email=user.email)
    transcript_text = ""
    try:
        transcript_text = drive.export_document_text(request.transcript_id) or ""
    except Exception as exc:
        logger.warning(f"transcript export failed for {request.transcript_id}: {exc}")
        errors.append(f"transcript_export_failed: {exc}")

    if not transcript_text:
        raise HTTPException(
            status_code=404,
            detail="Transcript text unavailable — check Drive permissions or file id",
        )

    transcript_text = transcript_text[:18000]
    from starlette.concurrency import run_in_threadpool

    from rubik_core.company.domain import workspace_domains

    internal_domains = await run_in_threadpool(workspace_domains)
    external_emails = _external_transcript_emails(
        transcript_text, user.email, internal_domains
    )

    focus_section = (
        f"\n## USER FOCUS\nThe user wants the email to lean into: **{request.focus_on}**\n"
        if request.focus_on
        else ""
    )
    subject_hint_section = (
        f"\n## SUBJECT HINT\n{request.subject_hint}\n" if request.subject_hint else ""
    )

    prompt = (
        "You are drafting a professional follow-up email for the user based on "
        "the meeting transcript below. It MUST completely capture, without "
        "dropping or generalizing any of them:\n"
        "- EVERY action item / to-do and who owns it (the user AND the other side)\n"
        "- specific commitments the user made\n"
        "- decisions reached, with any dates/timelines\n"
        "- open questions that still need an answer\n"
        "Prefer a short 'Next steps' bullet list so nothing is lost. Be concise "
        "and conversational (not corporate), but completeness of the action "
        "items takes priority over brevity — never trim a real to-do.\n\n"
        "## OUTPUT (raw JSON only, no markdown fences)\n"
        "{\n"
        '  "subject": "...",\n'
        '  "body_markdown": "..."\n'
        "}\n\n"
        f"{focus_section}{subject_hint_section}"
        f"## TRANSCRIPT\n{transcript_text}\n"
    )

    from rubik_core.inference_workloads import run_text_async

    model_id = settings.VERTEX_LLM_MODEL_PRO

    async def managed(route):
        response = await get_genai_client().aio.models.generate_content(
            model=route.model,
            contents=prompt,
        )
        return response.text or ""

    try:
        raw = await run_text_async(
            "transcript_email_draft",
            prompt,
            default_model=model_id,
            managed=managed,
            json_mode=True,
        )
    except Exception:
        logger.warning("transcript-draft model request failed")
        raise HTTPException(
            status_code=502,
            detail="AI draft failed. Check this workspace's model route and provider.",
        ) from None

    cleaned = (
        raw.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    try:
        data = _json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1:
            try:
                data = _json.loads(cleaned[start : end + 1])
            except Exception:
                data = {}
        else:
            data = {}

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="AI draft returned invalid JSON.")
    subject = (data.get("subject") or request.subject_hint or "Follow-up").strip()[:200]
    body_markdown = (data.get("body_markdown") or "").strip()
    if not body_markdown:
        raise HTTPException(status_code=502, detail="LLM produced an empty body")

    gmail_client = GmailClient(user_email=user.email)
    if not gmail_client.service:
        raise HTTPException(
            status_code=500, detail="Failed to initialize Gmail client."
        )

    try:
        draft_result = gmail_client.create_draft(
            to=external_emails or [user.email],
            subject=subject,
            body_markdown=body_markdown,
        )
    except Exception as exc:
        logger.exception(f"Gmail draft create failed for {user.email}")
        raise HTTPException(status_code=502, detail=str(exc))

    draft_id = draft_result.get("draft_id") if isinstance(draft_result, dict) else None
    draft_url = (
        draft_result.get("gmail_draft_url") if isinstance(draft_result, dict) else None
    )

    # UC-25 hook — resolve the COMMITMENT_DEBT on this transcript so the
    # priority doesn't refire tomorrow now that the user has acted on it.
    try:
        from rubik_core.priorities import memory as priority_memory
        from rubik_core.priorities.models import EntityKind, PriorityKind

        priority_memory.resolve(
            user_email=user.email,
            entity_kind=EntityKind.TRANSCRIPT,
            entity_id=str(request.transcript_id),
            priority_kind=PriorityKind.COMMITMENT_DEBT,
            evidence={
                "kind": "transcript_draft_created",
                "transcript_id": str(request.transcript_id),
                "draft_id": draft_id,
            },
            actor="system",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "UC-39: failed to resolve COMMITMENT_DEBT for %s / transcript %s",
            user.email,
            request.transcript_id,
            exc_info=True,
        )

    return TranscriptDraftResponse(
        draft_id=draft_id,
        gmail_draft_url=draft_url,
        to=external_emails,
        subject=subject,
        body_markdown=body_markdown,
        partial_errors=errors,
    )
