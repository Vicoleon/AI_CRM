"""
ADK-based Orchestrator for the chat assistant — Phase 2 Full Parity.

Handles:
- Structured citation extraction from knowledge_search / search_knowledge_base tool outputs
- HubSpot data and generated_sql capture from search_hubspot_data tool outputs
- Rich debug payloads with backend, route, tool_calls, warnings
- Error normalization: classifies ADK errors into a safe taxonomy
- HITL (Human-in-the-loop) confirmation flow for armed write tools, shown to the
  user as a card rendered from the paused call's own arguments
- DB session is canonical; ADK session is transient execution context only
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.apps import App
from google.adk.runners import RunConfig, Runner
from google.genai import errors as genai_errors
from google.genai import types

from rubik_core.agent_runtime.coordinator import HeadlessRun, build_coordinator
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.cost.usage import extract_usage
from rubik_core.identifiers import ADK_APP_NAME, ADK_COORDINATOR_NAME
from rubik_core.orchestration.confirmation_render import (
    RenderContext,
    confirmation_markdown,
    confirmation_payload,
    render_confirmation,
)
from rubik_core.orchestration.models import (
    ExecutionTrace,
    OrchestrationResult,
    RouteDecision,
    RouteIntent,
    ToolExecutionResult,
)
from rubik_core.orchestration.session_service import get_session_service
from rubik_core.sales.flavour import is_sales_flavoured

logger = logging.getLogger(__name__)


# ── Error Taxonomy ────────────────────────────────────────────────


class ADKErrorCategory:
    POLICY_DENIED = "policy_denied"
    TOOL_ERROR = "tool_error"
    AGENT_VALIDATION_ERROR = "agent_validation_error"
    AGENT_RUNTIME_ERROR = "agent_runtime_error"
    DOWNSTREAM_API_ERROR = "downstream_api_error"
    TIMEOUT = "timeout"
    AGENT_LOOP_LIMIT = "agent_loop_limit"
    EMPTY_AGENT_RESULT = "empty_agent_result"


def _classify_adk_error(e: Exception) -> str:
    """Classify an ADK exception into a debug-friendly category."""
    error_str = str(e).lower()
    if "policy" in error_str or "denied" in error_str or "not permitted" in error_str:
        return ADKErrorCategory.POLICY_DENIED
    # ADK's LlmCallsLimitExceededError: "Max number of llm calls limit of `N`
    # exceeded" — the runaway-loop backstop tripped (CHAT_MAX_LLM_CALLS).
    if "llm calls limit" in error_str:
        return ADKErrorCategory.AGENT_LOOP_LIMIT
    # A 5xx from Vertex (e.g. "504 Gateway Timeout ... DEADLINE_EXCEEDED") is the
    # model service failing, not the question being too big. Checked before the
    # timeout branch, whose "deadline" match used to tell the user to ask a
    # simpler question (a "deals slipping" chat).
    if isinstance(e, genai_errors.ServerError):
        return ADKErrorCategory.DOWNSTREAM_API_ERROR
    if "timeout" in error_str or "deadline" in error_str:
        return ADKErrorCategory.TIMEOUT
    if "429" in error_str or "resource exhausted" in error_str:
        return ADKErrorCategory.DOWNSTREAM_API_ERROR
    if "validation" in error_str or "invalid" in error_str:
        return ADKErrorCategory.AGENT_VALIDATION_ERROR
    if "google" in error_str or "grpc" in error_str or "ssl" in error_str:
        return ADKErrorCategory.DOWNSTREAM_API_ERROR
    return ADKErrorCategory.AGENT_RUNTIME_ERROR


def _user_safe_adk_message(category: str) -> str:
    """Return a user-safe message for the given error category."""
    msgs = {
        ADKErrorCategory.POLICY_DENIED: "This action is not allowed by the current policy.",
        ADKErrorCategory.TOOL_ERROR: "One of my tools encountered an issue. Could you try rephrasing?",
        ADKErrorCategory.AGENT_VALIDATION_ERROR: "I had trouble processing that. Could you try rephrasing?",
        ADKErrorCategory.AGENT_RUNTIME_ERROR: "I encountered an unexpected issue. Please try again.",
        ADKErrorCategory.DOWNSTREAM_API_ERROR: "I'm having trouble reaching the AI service right now. Please ask again in a minute.",
        ADKErrorCategory.TIMEOUT: "The request took too long. Please try a simpler question.",
        ADKErrorCategory.AGENT_LOOP_LIMIT: "That question sent me in circles. Could you break it into smaller, more specific steps?",
        ADKErrorCategory.EMPTY_AGENT_RESULT: "I wasn't able to generate a response. Could you try again?",
    }
    return msgs.get(category, "An unexpected error occurred. Please try again.")


# ── HITL confirmation-reply detection ─────────────────────────────
#
# A parked calendar/draft action is resumed only when the user's NEXT message
# is a short, standalone affirmation. The old check was a substring scan
# (`"ok" in query`), which false-fired on ordinary words — "ok" ⊂ "book"/"look",
# "yes" ⊂ "yesterday" — and, with no TTL on the parked pointer, let an unrelated
# later message silently resume a stale action (the "Migration Plan" RCA). We
# match on word boundaries over a fixed affirmation set and only for short
# messages, so "book it tomorrow" or "what did I do yesterday" never resume.
_AFFIRMATION_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|confirm|confirmed|approve|approved|"
    r"proceed|go\s+ahead|do\s+it|sounds\s+good|please\s+do|correct)\b",
    re.IGNORECASE,
)
_MAX_AFFIRMATION_CHARS = 40  # a genuine confirmation is terse ("yes", "go ahead")

# Appended alongside the confirmed FunctionResponse so the resumed turn stays on
# rails: execute the one confirmed action, report it, and stop. Without this the
# model treated the resume as a fresh turn and volunteered new searches/topics
# (the unprompted post-booking "Migration Plan" retrieval in the RCA).
_HITL_RESUME_NOTE = (
    "[System note: The user has confirmed the pending action shown to them above. "
    "Execute ONLY that confirmed action, then report its outcome to the user in "
    "one or two sentences and STOP. Do not call any other tool, start a new "
    "search, or introduce a new topic in this turn.]"
)

# Said instead of a card when a paused call cannot be found among the turn's
# events. The user must never approve an action Cubie cannot show them, so
# nothing is parked and "yes" next turn resumes nothing.
_UNREADABLE_CONFIRMATION = (
    "I stopped before an action that needs your OK, but I couldn't read what it "
    "was, so I haven't queued it. Please ask again."
)


def _record_calls(event: Any, calls: dict[str, tuple[str, dict]]) -> None:
    """Remember every function call in ``event`` by id.

    ADK keys ``requested_tool_confirmations`` by the ORIGINAL call's id and
    puts that call (name and arguments) in an earlier event, so this is how a
    confirmation finds what it is confirming. ``adk_request_confirmation``
    carries a copy of the original call as well (ADK 1.30,
    ``functions.generate_request_confirmation_event``); it fills the gap if the
    original event was never seen.
    """
    for fc in event.get_function_calls() or []:
        name = getattr(fc, "name", None) or ""
        args = dict(getattr(fc, "args", None) or {})
        if name == "adk_request_confirmation":
            original = args.get("originalFunctionCall") or {}
            original_id = original.get("id")
            if original_id and original_id not in calls:
                calls[original_id] = (
                    original.get("name") or "",
                    dict(original.get("args") or {}),
                )
            continue
        fc_id = getattr(fc, "id", None)
        if fc_id:
            calls[fc_id] = (name, args)


def _confirmation_responses(session: Any, invocation_id: str) -> list:
    """FunctionResponses approving every action the parked turn showed.

    The card lists each action that paused in that turn and "yes" answers the
    card, so every one of them is confirmed, not only the first. ADK resumes
    them all from one user message (``request_confirmation.py`` reads every
    confirmation response in it).
    """
    for event in reversed(getattr(session, "events", None) or []):
        if event.invocation_id != invocation_id:
            continue
        ids = [
            fc.id
            for fc in event.get_function_calls()
            if fc.name == "adk_request_confirmation"
        ]
        if ids:
            return [
                types.Part(
                    function_response=types.FunctionResponse(
                        name="adk_request_confirmation",
                        id=fc_id,
                        response={"confirmed": True},
                    )
                )
                for fc_id in ids
            ]
    return []


def _today_in(tz_name: str | None) -> str:
    """Today's date in ``tz_name``, for the "[System Note: Today is …]" line.

    This used to be UTC while the coordinator's own system context carried the
    user's LOCAL date, so between 00:00 UTC and local midnight the model was
    handed two different "today"s and could resolve "yesterday" to either. A
    scheduled evening run in a time zone behind UTC is squarely inside that
    window.
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(timezone.utc)
    try:
        if tz_name:
            now = now.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 — an unknown zone falls back to UTC
        logger.warning("adk_orchestrator: unknown timezone %r — dating in UTC", tz_name)
    return now.strftime("%B %d, %Y (%A)")


def _is_confirmation_reply(text: str) -> bool:
    """True if ``text`` is a short, standalone affirmation of a pending HITL
    action (word-boundary + length gated). Replaces the substring scan that let
    ``"ok"``/``"yes"`` inside unrelated words resume a parked calendar/draft
    tool call."""
    t = (text or "").strip()
    if not t or len(t) > _MAX_AFFIRMATION_CHARS:
        return False
    return bool(_AFFIRMATION_RE.search(t))


class _TurnUsage:
    """Token usage for one coordinator turn, summed over its model calls.

    ADK puts the usage of ONE model call on that call's response event, so a
    turn where the coordinator calls tools and then answers carries several.
    Both exits used to read only the last event, which priced the final call
    alone.

    Partial events are skipped. In SSE streaming, ADK (1.30,
    ``utils/streaming_utils.py``) stamps every chunk's partial event with that
    chunk's ``usage_metadata``, then closes the call with one non-partial event
    carrying the call's final count. Counting the chunks too would bill one
    call many times over.
    """

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0
        self.thinking = 0
        self.cached = 0
        self.total = 0
        self.model_calls = 0
        self.by_agent: dict[str, dict[str, int]] = {}

    def add(self, event: Any) -> None:
        if getattr(event, "partial", False):
            return
        if getattr(event, "usage_metadata", None) is None:
            return
        u = extract_usage(event)
        self.prompt += u["prompt"]
        self.completion += u["completion"]
        self.thinking += u["thinking"]
        self.cached += u["cached"]
        self.total += u["total"]
        self.model_calls += 1
        author = getattr(event, "author", None) or ADK_COORDINATOR_NAME
        bucket = self.by_agent.setdefault(
            author,
            {"prompt": 0, "completion": 0, "thinking": 0, "cached": 0, "total": 0},
        )
        for key in bucket:
            bucket[key] += u[key]


class ADKOrchestrator:
    """Orchestrates chat queries using Google ADK with full parity."""

    def __init__(
        self,
        user: UserProfile,
        timezone: str = "UTC",
        alignment_context: str = "",
        wgll_context: str = "",
        user_context: str = "",
        scheduled_tasks_tools=None,
        todo_tools=None,
        meeting_skills_tools=None,
        headless: HeadlessRun | None = None,
    ):
        self.user = user
        self.timezone = timezone
        # Marks an unattended run (a scheduled task). Kept on the instance so
        # `run` can widen the tool budget and skip the HITL bookkeeping that
        # only makes sense when someone can answer "yes".
        self.headless = headless
        # Admin-configured chat model (Admin → Chat, stored in
        # `chat_model_config`). load_chat_config never raises and falls back to
        # settings.VERTEX_LLM_MODEL_FLASH; the extra guard here means even an
        # import error can't break a chat turn — build_coordinator defaults too.
        try:
            from rubik_core.agent_runtime.chat_config import load_chat_config

            chat_model_id = load_chat_config().model_id
        except Exception:  # noqa: BLE001
            chat_model_id = None
        # Build a per-user agent with the new multi-agent coordinator.
        # `alignment_context` is the EOS alignment block; pass "" to skip
        # injection (e.g. when CUBIE_DISABLE_ALIGNMENT is set or there's
        # no EOS data in the DB yet). `wgll_context` is the WGLL feedback
        # block (recent user ratings on chat answers); "" = no-op.
        # `user_context` is the per-user personalization block (role/focus +
        # answer verbosity + reply language); "" = no-op.
        # The model actually in force this turn. Exposed because the AI-Cost
        # ledger has to record what RAN: the chat router historically recorded
        # the PRO id while the coordinator ran FLASH, which mispriced every
        # chat row. New callers read this instead of guessing.
        from rubik_core.ai_providers.adk_model import resolve_adk_route

        self.model_route = resolve_adk_route(
            "chat_adk",
            default_model=chat_model_id or settings.VERTEX_LLM_MODEL_FLASH,
        )
        self.sales_model_route = (
            resolve_adk_route(
                "sales_email_agent",
                default_model=settings.VERTEX_LLM_MODEL_FLASH,
            )
            if headless is None
            and is_sales_flavoured(
                getattr(user, "department", ""),
                getattr(user, "roles", []),
            )
            else None
        )
        self.model_id = self.model_route.model
        self.agent = build_coordinator(
            user_email=user.email,
            user_timezone=timezone,
            user_roles=getattr(user, "roles", []),
            user_department=getattr(user, "department", ""),
            alignment_context=alignment_context,
            wgll_context=wgll_context,
            user_context=user_context,
            model_id=chat_model_id,
            model_route=self.model_route,
            sales_model_route=self.sales_model_route,
            scheduled_tasks_tools=scheduled_tasks_tools,
            todo_tools=todo_tools,
            meeting_skills_tools=meeting_skills_tools,
            headless=headless,
        )
        # Item 5: wrap the agent in an App with Vertex context caching so the
        # static system prompt + tool declarations aren't re-prefilled every call.
        # Guarded — any App/cache-config failure falls back to the plain agent
        # runner so a caching issue can never break a chat turn.
        self.app: App | None = None
        if (
            settings.CHAT_CONTEXT_CACHE_ENABLED
            and self.model_route.provider == "vertex_managed"
        ):
            try:
                self.app = App(
                    name=ADK_APP_NAME,
                    root_agent=self.agent,
                    context_cache_config=ContextCacheConfig(
                        min_tokens=settings.CHAT_CONTEXT_CACHE_MIN_TOKENS,
                        ttl_seconds=settings.CHAT_CONTEXT_CACHE_TTL_SECONDS,
                        cache_intervals=settings.CHAT_CONTEXT_CACHE_INTERVALS,
                    ),
                )
            except Exception:  # noqa: BLE001 — caching must never break a turn
                logger.exception(
                    "Context-cache App build failed; using plain (uncached) runner"
                )
                self.app = None

    def _build_runner(self) -> Runner:
        """Build the ADK Runner for one turn.

        When context caching is enabled (item 5), wrap the agent in the cached
        App so the static prompt prefix is reused; otherwise fall back to the
        plain agent runner. Both derive app_name=ADK_APP_NAME.
        """
        session_service = get_session_service()
        if self.app is not None:
            return Runner(app=self.app, session_service=session_service)
        return Runner(
            agent=self.agent,
            app_name=ADK_APP_NAME,
            session_service=session_service,
        )

    def _max_llm_calls(self) -> int:
        """ADK call ceiling for one turn.

        A scheduled run gets a wider budget than a live chat turn: nobody is
        watching a spinner, and a standing request ("summarize yesterday's
        meetings and flag what needs a reply") legitimately fans out over more
        tools than a typed question.
        """
        if self.headless is not None:
            return settings.SCHEDULED_TASKS_MAX_LLM_CALLS
        return settings.CHAT_MAX_LLM_CALLS

    async def run(
        self,
        request_id: str,
        query: str,
        history: list[tuple],
        top_k: int = 5,
        filters: dict | None = None,
        chat_session_id: str | None = None,
        db_session: Any | None = None,
        mode: str = "ask",
        scope: dict | None = None,
        extra_parts: list | None = None,
    ) -> OrchestrationResult:
        start_time = time.time()
        logger.info(
            "[%s] ADK orchestration started for %s", request_id, self.user.email
        )
        # V2 plumbing: accept and log mode + scope here too. The
        # orchestrator does NOT alter retrieval, routing, or prompt
        # behavior on these yet — that lands in later subtasks.
        logger.info(
            "[%s] ADK orchestrator mode=%s scope_kind=%s scope_id=%s",
            request_id,
            mode,
            scope.get("kind") if scope else None,
            scope.get("id") if scope else None,
        )

        # Build ADK runner
        runner = self._build_runner()

        # 1. Get or create ADK session (transient execution context, NOT source of truth)
        adk_session_id = chat_session_id or "default_session"
        session_service = get_session_service()
        session = await session_service.get_session(
            app_name=ADK_APP_NAME,
            user_id=self.user.email,
            session_id=adk_session_id,
        )
        if not session:
            session = await session_service.create_session(
                app_name=ADK_APP_NAME,
                user_id=self.user.email,
                session_id=adk_session_id,
            )

        # 2. Check for HITL resumption
        invocation_id = None
        if db_session and hasattr(db_session, "last_adk_invocation_id"):
            invocation_id = db_session.last_adk_invocation_id

        # 3. Construct the user message with history context + current date context
        today_str = _today_in(self.timezone)
        history_context = ""
        if history:
            history_context = "\n".join(
                [
                    f"{'User' if role == 'user' else 'Assistant'}: {content}"
                    for role, content in history[-6:]
                ]
            )

        query_with_context = f"[System Note: Today is {today_str}. Please use this to resolve relative dates like 'today' or 'last meeting'.]\n\n"
        if history_context:
            query_with_context += (
                f"[Previous conversation context]\n{history_context}\n\n"
            )
        query_with_context += f"[Current user query]\n{query}"

        user_parts = [types.Part.from_text(text=query_with_context)]
        # Files the person attached (and the note naming their private files),
        # built by the chat router. After the text, which must stay at index 0:
        # the HITL resume check below reads user_parts[0].text.
        user_parts.extend(extra_parts or [])

        # Check if we should inject a confirmation response
        if invocation_id and _is_confirmation_reply(query):
            approvals = _confirmation_responses(session, invocation_id)
            if approvals:
                user_parts.extend(approvals)
                # Keep the resumed turn on rails: do the confirmed action,
                # report it, stop — no new tools/topics (see _HITL_RESUME_NOTE).
                user_parts.append(types.Part.from_text(text=_HITL_RESUME_NOTE))
                logger.info(
                    "[%s] Injecting HITL confirmation for %d action(s)",
                    request_id,
                    len(approvals),
                )

        user_content = types.Content(role="user", parts=user_parts)

        # Execute the agent
        full_responses = []
        tool_calls_log = []
        tool_results_raw = {}  # tool_name -> raw output dict for citation/data extraction
        trace_steps = []
        requested_confirmations = []
        calls_by_id: dict[str, tuple[str, dict]] = {}
        confirmation = None
        warnings = []
        event = None
        turn_usage = _TurnUsage()

        # Structured data extracted from tools
        extracted_citations = []
        extracted_hubspot_data = None
        extracted_generated_sql = None

        try:
            async for event in runner.run_async(
                user_id=self.user.email,
                session_id=session.id,
                new_message=user_content,
                invocation_id=invocation_id
                if user_parts[0].text.lower() in ["yes", "confirm", "approve"]
                else None,
                # Runaway-loop backstop: ADK defaults to 500 LLM calls/turn, so a
                # mis-looping coordinator would grind until the 90s wall-clock.
                # Cap it (CHAT_MAX_LLM_CALLS) so a loop raises fast and is
                # classified as AGENT_LOOP_LIMIT instead of a mystery timeout.
                run_config=RunConfig(max_llm_calls=self._max_llm_calls()),
            ):
                turn_usage.add(event)
                _record_calls(event, calls_by_id)

                # Detect confirmation requests
                if event.actions.requested_tool_confirmations:
                    for (
                        fc_id,
                        confirmation,
                    ) in event.actions.requested_tool_confirmations.items():
                        requested_confirmations.append(
                            {
                                "fc_id": fc_id,
                                "hint": confirmation.hint,
                                "payload": confirmation.payload,
                            }
                        )
                        logger.info(
                            "[%s] Tool confirmation requested: %s",
                            request_id,
                            confirmation.hint,
                        )

                # Collect the final response text
                if event.is_final_response():
                    if event.content and event.content.parts:
                        current_resp_parts = []
                        for part in event.content.parts:
                            if part.text:
                                current_resp_parts.append(part.text)
                        if current_resp_parts:
                            full_responses.append("\n".join(current_resp_parts))
                else:
                    # Log function calls
                    for fc in event.get_function_calls():
                        tool_name = fc.name if hasattr(fc, "name") else str(fc)
                        args_str = str(fc.args)[:200] if hasattr(fc, "args") else ""
                        logger.info(
                            "[%s] ADK tool call: %s args=%s",
                            request_id,
                            tool_name,
                            args_str,
                        )
                        tool_calls_log.append(tool_name)

                    # Log function responses and extract structured data
                    for fr in event.get_function_responses():
                        tool_name = fr.name if hasattr(fr, "name") else "?"
                        tool_output = fr.response if hasattr(fr, "response") else {}

                        # Store raw tool output for data extraction
                        tool_results_raw[tool_name] = tool_output

                        # Determine status from tool response
                        status = "success"
                        error_msg = None
                        if isinstance(tool_output, dict):
                            if tool_output.get("status") == "error":
                                status = "error"
                                error_msg = tool_output.get(
                                    "error_message", "Tool returned an error"
                                )
                                warnings.append(f"Tool {tool_name} failed: {error_msg}")

                        trace_steps.append(
                            ToolExecutionResult(
                                tool_name=tool_name,
                                status=status,
                                output=str(tool_output)[:200]
                                if tool_output
                                else "Tool completed",
                                error=error_msg,
                                latency_ms=0,
                            )
                        )

                        # ── Citation extraction from knowledge tools (WS5) ──
                        self._extract_structured_data(
                            tool_name,
                            tool_output,
                            extracted_citations,
                            lambda d: self._set_hubspot(d),
                            lambda d: self._set_sql(d),
                        )

            # 4. Handle HITL prompts. What the user approves is the card the
            # server renders from the paused call's arguments, so any text the
            # model wrote this turn is set aside, and the pause is parked even
            # when there was some (it used to be dropped then).
            if requested_confirmations:
                answer, confirmation = self._pending_confirmation(
                    requested_confirmations, calls_by_id
                )
                if db_session:
                    db_session.last_adk_invocation_id = (
                        event.invocation_id if confirmation else None
                    )
            else:
                answer = full_responses[-1] if full_responses else ""
                if db_session:
                    db_session.last_adk_invocation_id = None

                if not answer:
                    warnings.append("empty_agent_result")
                    answer = "I wasn't able to generate a response. Could you try rephrasing your question?"

        except Exception as e:
            if self.model_route.provider != "vertex_managed":
                # The selected provider's exception can contain a request URL,
                # credential or prompt. Never copy it into Chat history, trace
                # or application logs.
                logger.warning(
                    "[%s] selected workspace AI provider failed (%s)",
                    request_id,
                    type(e).__name__,
                )
                error_category = ADKErrorCategory.DOWNSTREAM_API_ERROR
                answer = (
                    f"The selected {self.model_route.provider} model could not "
                    "answer. Check its connection in Admin → Workspace."
                )
                diagnostic = "selected_workspace_ai_provider_failed"
            else:
                logger.warning(
                    "[%s] ADK execution failed (%s)", request_id, type(e).__name__
                )
                error_category = _classify_adk_error(e)
                answer = _user_safe_adk_message(error_category)
                diagnostic = f"{error_category}: {type(e).__name__}"
            warnings.append(diagnostic)
            trace_steps.append(
                ToolExecutionResult(
                    tool_name="ADK_EXECUTION",
                    status="error",
                    output=None,
                    error=diagnostic,
                    latency_ms=int((time.time() - start_time) * 1000),
                )
            )

        total_duration_ms = int((time.time() - start_time) * 1000)

        # Infer the route from tool calls for tracing
        inferred_intent = self._infer_intent(tool_calls_log)

        # Extract hubspot_data and generated_sql from tool results
        hubspot_data = extracted_hubspot_data or self._extract_hubspot_from_tools(
            tool_results_raw
        )
        generated_sql = extracted_generated_sql or self._extract_sql_from_tools(
            tool_results_raw
        )

        route_decision = RouteDecision(
            intent=inferred_intent,
            explanation=f"ADK auto-routed via tool calls: {', '.join(tool_calls_log) or 'direct response'}",
            confidence=1.0,
            metadata={
                "tool_calls": tool_calls_log,
                "warnings": warnings,
                "backend": "adk",
            },
        )

        trace = ExecutionTrace(
            request_id=request_id,
            route=route_decision,
            steps=trace_steps,
            total_duration_ms=total_duration_ms,
        )

        logger.info(
            "[%s] ADK completed in %dms, tools=%s, citations=%d, answer=%d chars",
            request_id,
            total_duration_ms,
            tool_calls_log or ["none"],
            len(extracted_citations),
            len(answer),
        )

        # Item 5 observability: nonzero cached tokens = the context cache hit.
        if turn_usage.cached:
            logger.info(
                "[%s] context cache HIT — %d cached prompt tokens (of %d over %d "
                "model calls)",
                request_id,
                turn_usage.cached,
                turn_usage.prompt,
                turn_usage.model_calls,
            )

        return OrchestrationResult(
            answer=answer,
            citations=extracted_citations,
            hubspot_data=hubspot_data,
            generated_sql=generated_sql,
            trace=trace,
            prompt_tokens=turn_usage.prompt,
            completion_tokens=turn_usage.completion,
            thinking_tokens=turn_usage.thinking,
            cached_tokens=turn_usage.cached,
            total_tokens=turn_usage.total,
            usage_by_agent=turn_usage.by_agent,
            confirmation=confirmation,
        )

    def _pending_confirmation(
        self, requested: list[dict], calls_by_id: dict[str, tuple[str, dict]]
    ) -> tuple[str, dict | None]:
        """The answer and the card for a turn that paused for approval.

        Returns ``(message, None)`` when a paused call is missing from the
        turn's events: nothing is shown, so nothing may be approved.
        """
        ctx = RenderContext(user_email=self.user.email, timezone=self.timezone)
        views = []
        for req in requested:
            call = calls_by_id.get(req["fc_id"])
            if call is None:
                logger.warning(
                    "HITL: no function call found for confirmation %s", req["fc_id"]
                )
                return _UNREADABLE_CONFIRMATION, None
            views.append(render_confirmation(call[0], call[1], ctx))
        return confirmation_markdown(views), confirmation_payload(views)

    async def run_streaming(
        self,
        request_id: str,
        query: str,
        history: list[tuple],
        top_k: int = 5,
        filters: dict | None = None,
        chat_session_id: str | None = None,
        db_session: Any | None = None,
        mode: str = "ask",
        scope: dict | None = None,
        extra_parts: list | None = None,
    ) -> AsyncIterator[dict]:
        """Streaming counterpart to :meth:`run`.

        Async-yields plain dict events for the SSE endpoint to serialize:
          - ``{"type": "token", "text": <delta>}`` — incremental answer text
          - ``{"type": "error", "category", "message"}`` — a classified failure
          - ``{"type": "done", "answer", "awaiting_confirmation",
            "confirmation", "citations", "hubspot_data", "generated_sql",
            "tool_calls", "prompt_tokens", "completion_tokens",
            "thinking_tokens", "cached_tokens", "total_tokens", "duration_ms"}``
            — final metadata for persistence; token counts are summed over
            every model call in the turn (see ``_TurnUsage``)

        Mirrors run()'s tool/citation extraction AND its HITL confirmation flow:
        a confirmation turn parks ``last_adk_invocation_id`` on ``db_session``
        and ends with the server-rendered card as ``answer`` (replacing any text
        streamed before the pause) plus ``confirmation`` for the Chat page to
        draw; the next "yes" turn resumes the paused tool via an injected
        FunctionResponse. Gated behind CHAT_STREAMING_ENABLED at the endpoint.
        """
        start_time = time.time()
        runner = self._build_runner()
        adk_session_id = chat_session_id or "default_session"
        session_service = get_session_service()
        session = await session_service.get_session(
            app_name=ADK_APP_NAME, user_id=self.user.email, session_id=adk_session_id
        )
        if not session:
            session = await session_service.create_session(
                app_name=ADK_APP_NAME,
                user_id=self.user.email,
                session_id=adk_session_id,
            )

        today_str = _today_in(self.timezone)
        history_context = ""
        if history:
            history_context = "\n".join(
                f"{'User' if role == 'user' else 'Assistant'}: {content}"
                for role, content in history[-6:]
            )
        query_with_context = (
            f"[System Note: Today is {today_str}. Please use this to resolve "
            "relative dates like 'today' or 'last meeting'.]\n\n"
        )
        if history_context:
            query_with_context += (
                f"[Previous conversation context]\n{history_context}\n\n"
            )
        query_with_context += f"[Current user query]\n{query}"

        user_parts = [types.Part.from_text(text=query_with_context)]
        # Attached files after the text, as in run().
        user_parts.extend(extra_parts or [])

        # HITL resumption (parity with run()): if a prior streamed turn parked a
        # pending calendar/draft confirmation on this session and the user is now
        # confirming, inject the confirmation FunctionResponse so ADK resumes the
        # paused tool call instead of starting over.
        invocation_id = None
        if db_session is not None and hasattr(db_session, "last_adk_invocation_id"):
            invocation_id = db_session.last_adk_invocation_id
        if invocation_id and _is_confirmation_reply(query):
            approvals = _confirmation_responses(session, invocation_id)
            if approvals:
                user_parts.extend(approvals)
                # Keep the resumed turn on rails: do the confirmed action,
                # report it, stop — no new tools/topics (see _HITL_RESUME_NOTE).
                user_parts.append(types.Part.from_text(text=_HITL_RESUME_NOTE))
                logger.info(
                    "[%s] Injecting HITL confirmation for %d action(s) (stream)",
                    request_id,
                    len(approvals),
                )

        user_content = types.Content(role="user", parts=user_parts)

        streamed_text: list[str] = []
        final_text: list[str] = []
        extracted_citations: list = []
        tool_calls_log: list[str] = []
        tool_results_raw: dict = {}
        requested_confirmations: list = []
        calls_by_id: dict[str, tuple[str, dict]] = {}
        event = None
        turn_usage = _TurnUsage()

        try:
            async for event in runner.run_async(
                user_id=self.user.email,
                session_id=session.id,
                new_message=user_content,
                run_config=RunConfig(
                    streaming_mode=StreamingMode.SSE,
                    max_llm_calls=self._max_llm_calls(),
                ),
            ):
                # Skips the partial chunks itself (see _TurnUsage).
                turn_usage.add(event)

                # Incremental model text — the whole point of streaming.
                if getattr(event, "partial", False):
                    if event.content and event.content.parts:
                        delta = "".join(p.text for p in event.content.parts if p.text)
                        if delta:
                            streamed_text.append(delta)
                            yield {"type": "token", "text": delta}
                    continue

                _record_calls(event, calls_by_id)

                # HITL: the coordinator asked to confirm an armed write tool.
                if event.actions.requested_tool_confirmations:
                    for (
                        fc_id,
                        confirmation,
                    ) in event.actions.requested_tool_confirmations.items():
                        requested_confirmations.append(
                            {
                                "fc_id": fc_id,
                                "hint": confirmation.hint,
                                "payload": confirmation.payload,
                            }
                        )
                        logger.info(
                            "[%s] Tool confirmation requested (stream): %s",
                            request_id,
                            confirmation.hint,
                        )

                if event.is_final_response():
                    if event.content and event.content.parts:
                        txt = "\n".join(p.text for p in event.content.parts if p.text)
                        if txt:
                            final_text.append(txt)
                else:
                    for fc in event.get_function_calls():
                        tool_calls_log.append(
                            fc.name if hasattr(fc, "name") else str(fc)
                        )
                    for fr in event.get_function_responses():
                        tool_name = fr.name if hasattr(fr, "name") else "?"
                        tool_output = fr.response if hasattr(fr, "response") else {}
                        tool_results_raw[tool_name] = tool_output
                        self._extract_structured_data(
                            tool_name,
                            tool_output,
                            extracted_citations,
                            self._set_hubspot,
                            self._set_sql,
                        )
        except Exception as e:  # noqa: BLE001 — a stream error must reach the client
            if self.model_route.provider != "vertex_managed":
                logger.warning(
                    "[%s] selected workspace AI provider stream failed (%s)",
                    request_id,
                    type(e).__name__,
                )
                category = ADKErrorCategory.DOWNSTREAM_API_ERROR
                message = (
                    f"The selected {self.model_route.provider} model could not "
                    "answer. Check its connection in Admin → Workspace."
                )
            else:
                logger.warning(
                    "[%s] ADK streaming failed (%s)", request_id, type(e).__name__
                )
                category = _classify_adk_error(e)
                message = _user_safe_adk_message(category)
            yield {
                "type": "error",
                "category": category,
                "message": message,
                "prompt_tokens": turn_usage.prompt,
                "completion_tokens": turn_usage.completion,
                "thinking_tokens": turn_usage.thinking,
                "cached_tokens": turn_usage.cached,
                "total_tokens": turn_usage.total,
                "usage_by_agent": turn_usage.by_agent,
            }
            return

        # HITL (parity with run()): if the coordinator asked to confirm an
        # action, the answer becomes the server-rendered card and the invocation
        # id is parked so the NEXT turn (user says "yes") resumes the tool. This
        # holds even when the model streamed text before the pause: that used
        # to drop the confirmation, and the prompt asks for exactly that text.
        confirmation = None
        awaiting_confirmation = False
        if requested_confirmations:
            answer, confirmation = self._pending_confirmation(
                requested_confirmations, calls_by_id
            )
            awaiting_confirmation = confirmation is not None
            if db_session is not None:
                db_session.last_adk_invocation_id = (
                    event.invocation_id if event and confirmation else None
                )
        else:
            # Prefer the aggregated final-response text; fall back to the
            # concatenated deltas if this ADK build only carried text on partials.
            answer = final_text[-1] if final_text else "".join(streamed_text)
            if db_session is not None:
                db_session.last_adk_invocation_id = None
            if not answer:
                answer = (
                    "I wasn't able to generate a response. Could you try rephrasing "
                    "your question?"
                )

        hubspot_data = self._hubspot_data_buffer or self._extract_hubspot_from_tools(
            tool_results_raw
        )
        generated_sql = self._sql_buffer or self._extract_sql_from_tools(
            tool_results_raw
        )
        if turn_usage.cached:
            logger.info(
                "[%s] context cache HIT (stream) — %d cached prompt tokens (of %d "
                "over %d model calls)",
                request_id,
                turn_usage.cached,
                turn_usage.prompt,
                turn_usage.model_calls,
            )
        yield {
            "type": "done",
            "answer": answer,
            "awaiting_confirmation": awaiting_confirmation,
            "confirmation": confirmation,
            "citations": extracted_citations,
            "hubspot_data": hubspot_data,
            "generated_sql": generated_sql,
            "tool_calls": tool_calls_log,
            "prompt_tokens": turn_usage.prompt,
            "completion_tokens": turn_usage.completion,
            "thinking_tokens": turn_usage.thinking,
            "cached_tokens": turn_usage.cached,
            "total_tokens": turn_usage.total,
            "usage_by_agent": turn_usage.by_agent,
            "duration_ms": int((time.time() - start_time) * 1000),
        }

    # ── Structured Data Extraction Helpers ─────────────────────────

    _hubspot_data_buffer: str | None = None
    _sql_buffer: str | None = None

    def _set_hubspot(self, data: str):
        self._hubspot_data_buffer = data

    def _set_sql(self, data: str):
        self._sql_buffer = data

    def _extract_structured_data(
        self,
        tool_name: str,
        tool_output: Any,
        citations_list: list,
        set_hubspot_fn,
        set_sql_fn,
    ):
        """Extract citations, hubspot_data, and generated_sql from tool output dicts."""
        if not isinstance(tool_output, dict):
            return

        report = tool_output.get("report", "")

        # Knowledge base / product / web search → extract citation-like data
        if tool_name in (
            "search_knowledge_base",
            "search_my_files",
            "search_products",
            "search_web",
        ):
            self._extract_citations_from_report(report, citations_list)

        # Meeting Skills runs hand back their citations ready-made: one per
        # transcript line a point quotes, already numbered to match the [n]
        # marks in the write-up.
        if tool_name in ("summarize_meeting", "run_meeting_skill"):
            ready = tool_output.get("citations")
            if isinstance(ready, list):
                citations_list.extend(c for c in ready if isinstance(c, dict))

        # HubSpot search → extract hubspot_data and generated_sql
        if tool_name == "search_hubspot_data":
            if report:
                set_hubspot_fn(report)
            # If the report contains "(Generated SQL: ...)", extract it
            sql_match = re.search(r"\(Generated SQL:\s*(.+?)\)", report)
            if sql_match:
                set_sql_fn(sql_match.group(1).strip())

    # Matches both "**Source:** <path>" and the UC-1 "**Source (rag):** <path>"
    # form. The optional "(...)" qualifier was previously unmatched, so every
    # UC-1 knowledge-base hit resolved to "Unknown Source" in the UI.
    _SOURCE_RE = re.compile(r"\*\*Source(?:\s*\([^)]*\))?:\*\*\s*(.+?)(?:\n|$)")

    def _extract_citations_from_report(self, report: str, citations_list: list):
        """Parse source/snippet pairs from a knowledge base report.

        Only blocks with a real ``**Source:**`` marker become citations. Reports
        with no source — e.g. "No relevant documents found." or the
        low-confidence note — are NOT turned into phantom "Unknown Source"
        citations (a channel that surfaced bogus sources in the "Migration Plan"
        hallucination). See project_chat_hallucination_rca_migration_plan.
        """
        if not report:
            return

        # The report format from search_knowledge_base is:
        # **Source:** <path>\n<text>\n\n---\n\n**Source:** ...
        blocks = re.split(r"\n+---\n+", report)
        for idx, block in enumerate(blocks):
            block = block.strip()
            if not block:
                continue

            source_match = self._SOURCE_RE.search(block)
            if not source_match:
                # No source marker → not a real retrieval hit. Skip it rather
                # than manufacture an "Unknown Source" citation.
                continue
            source_path = source_match.group(1).strip()
            if not source_path:
                continue

            # Snippet is everything after the matched source line.
            snippet = block[source_match.end() :].strip()[:500]
            if not snippet:
                continue

            citations_list.append(
                {
                    "source_path": source_path,
                    "page": None,
                    "chunk_index": idx,
                    "snippet": snippet,
                }
            )

    @staticmethod
    def _extract_hubspot_from_tools(tool_results: dict) -> str | None:
        """Extract hubspot_data from raw tool results."""
        for name in ("search_hubspot_data",):
            if name in tool_results:
                output = tool_results[name]
                if isinstance(output, dict) and output.get("report"):
                    return output["report"]
        return None

    @staticmethod
    def _extract_sql_from_tools(tool_results: dict) -> str | None:
        """Extract generated_sql from raw tool results."""
        for name in ("search_hubspot_data",):
            if name in tool_results:
                output = tool_results[name]
                if isinstance(output, dict):
                    report = output.get("report", "")
                    sql_match = re.search(r"\(Generated SQL:\s*(.+?)\)", report)
                    if sql_match:
                        return sql_match.group(1).strip()
        return None

    @staticmethod
    def _infer_intent(tool_calls: list[str]) -> RouteIntent:
        """Map ADK tool call names back to our RouteIntent enum for tracing."""
        if not tool_calls:
            return RouteIntent.CHAT_RAG  # Direct response = general chat

        tool_set = set(t.lower() for t in tool_calls)

        if any("calendar_create_event" in t for t in tool_set):
            return RouteIntent.CALENDAR_READ
        if any("calendar" in t for t in tool_set):
            return RouteIntent.CALENDAR_READ
        if any("gmail_create_draft" in t for t in tool_set):
            return RouteIntent.EMAIL_DRAFT
        if any("gmail" in t for t in tool_set):
            return RouteIntent.EMAIL_READ
        if any("meeting" in t or "past_meeting" in t for t in tool_set):
            return RouteIntent.MEET_READ
        if any("daily_briefing" in t for t in tool_set):
            return RouteIntent.HOMEFEED
        if any("hubspot" in t for t in tool_set):
            return RouteIntent.CRM
        if any("knowledge_base" in t or "products" in t for t in tool_set):
            return RouteIntent.CHAT_RAG

        return RouteIntent.CHAT_RAG
