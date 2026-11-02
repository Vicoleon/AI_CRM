"""Feature-attributed LLM usage recording — best-effort, buffered, kill-safe.

Every LLM call (``generate_content`` / ``embed_content``) that flows through
the shared google-genai client (``rubik_core.models.get_genai_client``) is
captured here and, in batches, written as ``LLMUsageEvent`` rows that Admin →
AI Cost reads. Callers attribute the call to a product feature by wrapping the
call in :func:`feature_context`; anything not wrapped is recorded under
``"unattributed"`` (still counted — unattributed spend is real spend).

Design constraints this file respects:

* **Never breaks a live call path.** Every public entry point swallows its own
  exceptions and logs at debug. A cost-accounting bug must never surface as a
  chat / briefing / ingestion failure.
* **Bounded DB connections.** This project's Cloud SQL pool is small
  (pool_size=5, max_overflow=5) and has a history of exhaustion. So events are
  buffered in memory and flushed in batches through one short-lived session
  per workspace — never one session per call. The Admin report force-flushes before
  it reads, so the page is never stale.
* **Instantly disableable.** Set ``AI_COST_TRACKING_ENABLED=false`` to make
  every entry point a no-op without a code change.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import inspect
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from rubik_core.tenancy.context import (
    NoWorkspaceContext,
    current_workspace,
    use_workspace,
)
from rubik_core.tenancy.threads import context_thread

logger = logging.getLogger(__name__)

# ── Attribution context ──────────────────────────────────────────────────
# Propagates through async tasks and through Starlette's run_in_threadpool
# (it copies the caller's contextvars into the worker thread), so a feature
# set at a request/job entry point reaches the LLM call made deep inside it.
current_feature: ContextVar[str] = ContextVar("current_feature", default="unattributed")
current_user_email: ContextVar[str | None] = ContextVar(
    "current_user_email", default=None
)
current_request_id: ContextVar[str | None] = ContextVar(
    "current_request_id", default=None
)


@contextlib.contextmanager
def feature_context(
    feature: str,
    *,
    user_email: str | None = None,
    request_id: str | None = None,
) -> Iterator[None]:
    """Attribute every LLM call made inside this block to ``feature``.

    Usage::

        with feature_context("meeting_prep", user_email=user.email):
            client.models.generate_content(...)

    Nested contexts restore the outer feature on exit. ``user_email`` /
    ``request_id`` are optional enrichment stamped onto each recorded event.
    """
    tokens = [current_feature.set(feature)]
    if user_email is not None:
        tokens.append(current_user_email.set(user_email))
    if request_id is not None:
        tokens.append(current_request_id.set(request_id))
    try:
        yield
    finally:
        # Reset in reverse order using the saved tokens.
        current_feature.reset(tokens[0])
        i = 1
        if user_email is not None:
            current_user_email.reset(tokens[i])
            i += 1
        if request_id is not None:
            current_request_id.reset(tokens[i])


@contextlib.contextmanager
def default_feature(feature: str) -> Iterator[None]:
    """Claim untagged LLM calls for ``feature`` WITHOUT overriding a caller.

    For shared leaf utilities that several features call into — web search,
    HubSpot query planning, retrieval helpers. Hard-tagging those with
    :func:`attribute_to` would be wrong: a search issued during a chat turn is
    chat spend, and stamping it ``"web_search"`` would rob Chat of its true
    cost. This only fills in the blank, so the leaf stops landing in
    "unattributed" when nothing upstream claimed it, and stays invisible when
    something did.
    """
    if current_feature.get() != "unattributed":
        yield  # an upstream feature already owns this call — leave it alone
        return
    with feature_context(feature):
        yield


def attribute_to(
    feature: str, *, only_if_unset: bool = False, user_arg: str | None = None
):
    """Decorator: attribute every LLM call made inside the wrapped function to
    ``feature``. Works on both sync and async functions — the contextvar is set
    for the duration of the call and restored on exit, so it's safe on shared
    request/orchestrator entry points. One line, no body changes::

        @attribute_to("meeting_prep")
        async def generate_meeting_prep_analysis(...): ...

    Prefer decorating the function that does the work over the entry point that
    calls it: a contextvar set at an entry point does NOT survive a hand-off to
    a worker thread (raw ``ThreadPoolExecutor.submit``), whereas a decorator
    re-establishes the feature in whatever thread runs the function.

    ``only_if_unset=True`` uses :func:`default_feature` semantics — fill in the
    blank but never override a feature a caller already set. Use it on shared
    leaf helpers, not on feature entry points.

    ``user_arg`` names the wrapped function's argument that holds the person the
    work is FOR, and stamps that email on every call inside it, the way
    ``feature_context(..., user_email=...)`` does. A batch job has no request,
    so without it the whole priorities run (most of all AI spend) recorded no
    user, and AI Spend could not be split by person or department::

        @attribute_to("ai_priorities", user_arg="user_email")
        def run_priorities_for_user(user_email: str, ...): ...
    """

    scope = default_feature if only_if_unset else feature_context

    def decorator(func):
        signature = inspect.signature(func) if user_arg else None

        @contextlib.contextmanager
        def _attributed(args, kwargs):
            email = None
            if signature is not None:
                try:
                    bound = signature.bind_partial(*args, **kwargs).arguments
                    value = bound.get(user_arg)
                    email = str(value).strip().lower() if value else None
                except Exception:  # noqa: BLE001 — attribution never breaks a call
                    email = None
            with scope(feature):
                if email:
                    with feature_context(current_feature.get(), user_email=email):
                        yield
                else:
                    yield

        if inspect.isasyncgenfunction(func):

            @functools.wraps(func)
            async def asyncgen_wrapper(*args, **kwargs):
                with _attributed(args, kwargs):
                    async for item in func(*args, **kwargs):
                        yield item

            return asyncgen_wrapper

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                with _attributed(args, kwargs):
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            with _attributed(args, kwargs):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator


# ── Buffered recorder ────────────────────────────────────────────────────
# Workspace → events. The slug is stamped before enqueue, because daemon and
# atexit flushes have no request context and may drain several tenants at once.
_BUFFER: dict[str, list[dict[str, Any]]] = {}
_LOCK = threading.Lock()
_last_flush_monotonic = time.monotonic()

# Flush when the buffer reaches this many events OR this many seconds elapse
# since the last flush (checked opportunistically on each record). Small
# enough that low-traffic events still land promptly; large enough that a
# burst (priorities batch, ingestion) collapses to a handful of INSERTs.
_FLUSH_THRESHOLD = 25
_FLUSH_INTERVAL_S = 30.0


def _tracking_enabled() -> bool:
    return os.getenv("AI_COST_TRACKING_ENABLED", "true").strip().lower() != "false"


#: ``metadata_json`` key holding a row's thinking-token count. A row without it
#: was recorded before thinking was captured or is an
#: embedding, so its ``cost_usd`` excludes thinking; history is never rewritten.
THINKING_TOKENS_KEY = "thinking_tokens"


def extract_usage(response: Any) -> dict[str, int]:
    """Pull token counts off a google-genai response's ``usage_metadata``.

    Returns zeros for anything missing so callers never KeyError. Field names
    match the genai SDK: ``prompt_token_count`` (input, includes any cached),
    ``candidates_token_count`` (the visible answer), ``thoughts_token_count``
    (the model's thinking: NOT part of the answer count, billed at the output
    rate), ``total_token_count``, ``cached_content_token_count`` (cache-hit
    subset of prompt).
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {"prompt": 0, "completion": 0, "thinking": 0, "total": 0, "cached": 0}
    prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
    completion = int(getattr(usage, "candidates_token_count", 0) or 0)
    thinking = int(getattr(usage, "thoughts_token_count", 0) or 0)
    total = int(getattr(usage, "total_token_count", 0) or 0) or (
        prompt + completion + thinking
    )
    cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
    return {
        "prompt": prompt,
        "completion": completion,
        "thinking": thinking,
        "total": total,
        "cached": cached,
    }


def _caller_origin() -> str | None:
    """Best-effort ``module:function:lineno`` of the first frame OUTSIDE the
    cost-accounting + shared-client plumbing — i.e. the app code that actually
    made the untagged LLM call. Used only to enrich "unattributed" events so
    they can be traced to a code path. Never raises.
    """
    try:
        skip = ("/rubik_core/cost/", "/rubik_core/models.py")
        frame = sys._getframe(2)  # 0=here, 1=record_usage, 2=caller
        for _ in range(25):
            if frame is None:
                break
            fn = frame.f_code.co_filename
            if not any(s in fn for s in skip):
                mod = frame.f_globals.get("__name__", "?")
                return f"{mod}.{frame.f_code.co_name}:{frame.f_lineno}"
            frame = frame.f_back
    except Exception:  # noqa: BLE001
        pass
    return None


def record_usage(
    *,
    operation: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    total_tokens: int | None = None,
    thinking_tokens: int | None = None,
    feature: str | None = None,
    user_email: str | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Buffer one usage event. Never raises.

    ``feature`` / ``user_email`` / ``request_id`` default to the current
    :func:`feature_context`. Cost is computed now (against the live rate card)
    so the row is self-contained. Skip rows with no tokens at all — a failed
    call that produced no usage isn't spend.

    ``thinking_tokens`` is the model's thinking (Vertex
    ``thoughts_token_count``): priced into ``cost_usd`` at the output rate and
    kept as its own count under ``metadata_json[THINKING_TOKENS_KEY]``, so
    ``completion_tokens`` stays the visible answer. Pass it (0 included)
    whenever the call's usage was read; leave it ``None`` where thinking does
    not apply (embeddings). Admin → AI Cost counts the rows that carry the key
    to say how many calls in a range were measured with thinking.
    """
    if not _tracking_enabled():
        return
    try:
        from rubik_core.cost.pricing import compute_cost_usd

        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        cached = max(0, int(cached_tokens or 0))
        thinking = (
            max(0, int(thinking_tokens or 0)) if thinking_tokens is not None else None
        )
        total = (
            int(total_tokens)
            if total_tokens is not None
            else (prompt + completion + (thinking or 0))
        )
        if prompt == 0 and completion == 0 and not thinking and total == 0:
            return

        try:
            workspace = current_workspace()
        except NoWorkspaceContext:
            logger.warning("AI cost event discarded: no workspace context")
            return

        cost = compute_cost_usd(
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            thinking_tokens=thinking or 0,
            provider=(metadata or {}).get("provider"),
        )
        resolved_feature = feature or current_feature.get() or "unattributed"
        meta = dict(metadata) if metadata else None
        # Thinking rides in metadata_json rather than a column: the house rule
        # is no ALTER on a live table at boot (it locks the table), and
        # this table takes a write on every Gemini call.
        if thinking is not None:
            meta = {**(meta or {}), THINKING_TOKENS_KEY: thinking}
        # For anything that slips through untagged, stamp WHERE the call came
        # from (first app frame outside the cost/models plumbing) so the Admin
        # "Unattributed" bucket is never a black box — you can see the culprit
        # module:function and tag it. Only done for unattributed (cheap, and
        # avoids noise on the hot tagged paths).
        if resolved_feature == "unattributed":
            origin = _caller_origin()
            if origin:
                meta = {**(meta or {}), "origin": origin}
        event = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow(),
            "feature": resolved_feature,
            "operation": operation or "generate",
            "model": model or "",
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": cached,
            "total_tokens": total,
            "cost_usd": cost,
            "user_email": user_email or current_user_email.get(),
            "request_id": request_id or current_request_id.get(),
            "metadata_json": meta or None,
        }
        should_flush = False
        with _LOCK:
            _BUFFER.setdefault(workspace, []).append(event)
            if (
                sum(map(len, _BUFFER.values())) >= _FLUSH_THRESHOLD
                or (time.monotonic() - _last_flush_monotonic) >= _FLUSH_INTERVAL_S
            ):
                should_flush = True
        if should_flush:
            # Offload the DB write to a short-lived daemon thread so we never
            # block the caller — record_usage is invoked SYNCHRONOUSLY from the
            # async client wrapper (models._InstrumentedAsyncModels), so an
            # inline flush would run a blocking DB insert on the event loop.
            # flush_usage locks + drains atomically, so a redundant concurrent
            # thread just finds an empty buffer and returns.
            context_thread(flush_usage, name="ai-cost-flush", daemon=True).start()
    except Exception:  # noqa: BLE001 — recording must never break the caller
        logger.debug("record_usage failed", exc_info=True)


def flush_usage(force: bool = True) -> int:
    """Persist buffered events. Returns the count written. Never raises.

    ``force`` is accepted for symmetry/readability; the buffer is always
    drained when called. The Admin report calls this before querying so the
    page reflects in-flight events still sitting in memory.
    """
    global _last_flush_monotonic
    if not _tracking_enabled():
        return 0
    with _LOCK:
        if not _BUFFER:
            _last_flush_monotonic = time.monotonic()
            return 0
        batches = dict(_BUFFER)
        _BUFFER.clear()
        _last_flush_monotonic = time.monotonic()

    written = 0
    for workspace, batch in batches.items():

        def persist(workspace: str = workspace, batch: list = batch) -> int:
            # A trusted process buffer is independent of the request that
            # happened to trigger its flush. In particular, a support scope
            # must not constrain another workspace's queued telemetry.
            with use_workspace(workspace):
                db = None
                try:
                    from rubik_core.database.db import SessionLocal
                    from rubik_core.database.models import LLMUsageEvent

                    db = SessionLocal()
                    db.bulk_insert_mappings(LLMUsageEvent, batch)
                    db.commit()
                    return len(batch)
                except Exception:  # noqa: BLE001 — isolate one tenant's failure
                    logger.warning(
                        "AI cost flush failed for workspace %s; dropping %d events",
                        workspace,
                        len(batch),
                        exc_info=True,
                    )
                    if db is not None:
                        with contextlib.suppress(Exception):
                            db.rollback()
                    return 0
                finally:
                    if db is not None:
                        with contextlib.suppress(Exception):
                            db.close()

        try:
            written += contextvars.Context().run(persist)
        except Exception:  # noqa: BLE001 — one bad tenant must not block another
            logger.warning(
                "AI cost flush could not select workspace %s; dropping %d events",
                workspace,
                len(batch),
                exc_info=True,
            )
    return written


# Best-effort flush on interpreter shutdown so the tail of the buffer lands.
atexit.register(lambda: flush_usage())
